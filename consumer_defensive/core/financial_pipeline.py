"""Stage 4 financial canonicalization and feature-lineage orchestration.

This module joins the pure controls in :mod:`financial_semantics` into a
database-friendly contract.  It remains deterministic and has no I/O of its
own: callers supply raw/canonical rows and persist returned decisions.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
import hashlib
import json
import math
from typing import Any, Iterable, Mapping, Sequence

from consumer_defensive.core.financial_semantics import (
    FinancialFact,
    FinancialValue,
    construct_financial_ratios,
    normalize_capex_payment,
    select_revenue_candidate,
    select_safe_flow_value,
)


FEATURE_DEFINITION_VERSION = "consumer_defensive_financial_features_v2_average_balance"
FLOW_METRIC_NAMES = {
    "revenue": "revenue",
    "cost_of_revenue": "cost_of_revenue",
    "gross_profit": "gross_profit",
    "operating_income": "operating_income",
    "operating_cash_flow": "operating_cash_flow",
    "capital_expenditures": "capex",
    "pretax_income": "pretax_income",
    "income_tax_expense": "income_tax_expense",
    "depreciation_amortization": "depreciation_and_amortization",
}
INSTANT_METRICS = ("cash", "inventory", "equity", "debt_current", "debt_noncurrent")
FEATURE_COLUMNS = (
    "revenue_ttm_usd",
    "gross_margin",
    "operating_margin",
    "free_cash_flow_margin",
    "return_on_invested_capital",
    "net_debt_to_ebitda",
    "inventory_turnover",
)
RAW_OBSERVATION_FIELDS = (
    'ticker','cik','accession_number','taxonomy','concept','value_text',
    'numeric_value','unit','period_start','period_end','filed_date','accepted_at',
    'form_type','frame','dimensions_json','source_id','source_detail',
)


def _stable_observation_identity(row: Mapping[str, Any]) -> str:
    existing = str(row.get('source_observation_id') or '')
    if existing:
        return existing
    payload = [row.get(field) for field in RAW_OBSERVATION_FIELDS]
    return hashlib.sha256(json.dumps(
        payload,ensure_ascii=True,separators=(',', ':'),allow_nan=False,
    ).encode()).hexdigest()


@dataclass(frozen=True)
class CanonicalDecision:
    raw_fact_id: int
    source_observation_id: str
    ticker: str
    accession_number: str
    taxonomy: str
    source_concept: str
    metric: str
    component: str
    statement_type: str
    period_start: str | None
    period_end: str
    accepted_at: str
    reported_currency: str
    reported_value: float
    normalized_value: float
    selection_method: str
    sign_normalization_method: str
    quality_flags: tuple[str, ...]


@dataclass(frozen=True)
class CanonicalSelectionResult:
    decisions: tuple[CanonicalDecision, ...]
    audit_counts: Mapping[str, int]


@dataclass(frozen=True)
class FinancialFeatureBundle:
    values: Mapping[str, float | None]
    basis_period_end: str | None
    feature_definition_version: str
    lineage: Mapping[str, Any]
    quality_status: str
    quality_reasons: tuple[str, ...]


def select_canonical_financial_facts(
    raw_rows: Iterable[Mapping[str, Any]],
    *,
    concept_index: Mapping[str, tuple[str, str, str, int]],
    supported_currencies: set[str],
) -> CanonicalSelectionResult:
    """Resolve filing taxonomy/currency and choose one explainable fact.

    Filing-level reporting taxonomy and currency are selected by a unique
    plurality of distinct mapped contexts.  Ties fail closed.  Within an exact
    filing period, revenue uses the gross-profit accounting identity when that
    identity is available.  Other concepts follow the reviewed map priority;
    equal-priority conflicting values are rejected rather than row-id selected.
    """

    rows: list[dict[str, Any]] = []
    audit: Counter[str] = Counter()
    for raw in raw_rows:
        row = dict(raw)
        concept = str(row.get("concept") or "")
        mapped = concept_index.get(concept)
        currency = str(row.get("unit") or "").upper()
        accession = str(row.get("accession_number") or "").strip()
        if mapped is None:
            continue
        if not accession:
            audit["missing_accession"] += 1
            continue
        if currency not in supported_currencies:
            audit["unsupported_unit"] += 1
            continue
        if not row.get("period_end") or not row.get("accepted_at"):
            audit["incomplete_period_or_acceptance"] += 1
            continue
        value = _finite_or_none(row.get("numeric_value"))
        if value is None:
            audit["nonfinite_value"] += 1
            continue
        row.update(
            {
                "metric": mapped[0],
                "statement_type": mapped[1],
                "component": mapped[2],
                "priority": mapped[3],
                "currency": currency,
                "numeric_value": value,
                'source_observation_id': _stable_observation_identity(row),
            }
        )
        rows.append(row)

    filing_rows: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        filing_rows[(str(row["ticker"]), str(row["accession_number"]), str(row["accepted_at"]))].append(row)

    accepted_rows: list[dict[str, Any]] = []
    for filing_key, candidates in filing_rows.items():
        taxonomy_sets: dict[str, set[tuple[str, str, str, str]]] = defaultdict(set)
        for row in candidates:
            taxonomy_sets[str(row.get("taxonomy") or "")].add(_distinct_context(row))
        taxonomy = _unique_plurality(taxonomy_sets)
        if not taxonomy:
            audit["ambiguous_filing_taxonomy"] += len(candidates)
            continue
        taxonomy_candidates = [row for row in candidates if str(row.get("taxonomy") or "") == taxonomy]
        currency_sets: dict[str, set[tuple[str, str, str, str]]] = defaultdict(set)
        for row in taxonomy_candidates:
            currency_sets[str(row["currency"])].add(_distinct_context(row))
        currency = _unique_plurality(currency_sets)
        if not currency:
            audit["ambiguous_reporting_currency"] += len(taxonomy_candidates)
            continue
        selected = [row for row in taxonomy_candidates if str(row["currency"]) == currency]
        accepted_rows.extend(selected)
        audit["non_dominant_context_rejected"] += len(candidates) - len(selected)

    exact_groups: dict[tuple[str, str, str, str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in accepted_rows:
        exact_groups[
            (
                str(row["ticker"]),
                str(row["accession_number"]),
                str(row.get("taxonomy") or ""),
                str(row["currency"]),
                str(row.get("period_start") or ""),
                str(row["period_end"]),
                str(row["accepted_at"]),
            )
        ].append(row)

    decisions: list[CanonicalDecision] = []
    for group in exact_groups.values():
        by_metric: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in group:
            by_metric[(str(row["metric"]), str(row["component"]))].append(row)

        chosen: dict[tuple[str, str], tuple[dict[str, Any], tuple[str, ...], str]] = {}
        for metric_component, candidates in by_metric.items():
            picked, flags = _priority_candidate(candidates)
            if picked is None:
                audit["ambiguous_equal_priority_value"] += 1
                continue
            chosen[metric_component] = (picked, flags, "reviewed_concept_priority")

        revenue_candidates = by_metric.get(("revenue", "total"), [])
        cogs = chosen.get(("cost_of_revenue", "total"))
        gross = chosen.get(("gross_profit", "total"))
        if revenue_candidates and cogs and gross:
            facts = [_financial_fact(row, "revenue") for row in revenue_candidates]
            identity = select_revenue_candidate(
                facts,
                cost_of_revenue=_financial_fact(cogs[0], "cost_of_revenue"),
                gross_profit=_financial_fact(gross[0], "gross_profit"),
                reporting_currency=str(group[0]["currency"]),
            )
            if identity.selected is not None:
                selected_id = int(str(identity.selected.raw_fact_id))
                selected_row = next(row for row in revenue_candidates if int(row["raw_fact_id"]) == selected_id)
                chosen[("revenue", "total")] = (
                    selected_row,
                    tuple(identity.quality_flags),
                    "gross_profit_identity",
                )
                audit["revenue_identity_selected"] += 1
            else:
                distinct_values = {float(row["numeric_value"]) for row in revenue_candidates}
                if len(revenue_candidates) == 1 or len(distinct_values) == 1:
                    picked, priority_flags = _priority_candidate(revenue_candidates)
                    if picked is not None:
                        chosen[("revenue", "total")] = (
                            picked,
                            tuple(sorted(set(priority_flags + identity.quality_flags))),
                            "concept_priority_identity_review",
                        )
                        audit["revenue_identity_review_retained"] += 1
                else:
                    chosen.pop(("revenue", "total"), None)
                    audit["ambiguous_revenue_identity"] += 1
        elif revenue_candidates:
            picked = chosen.get(("revenue", "total"))
            if picked:
                chosen[("revenue", "total")] = (
                    picked[0],
                    tuple(sorted(set(picked[1] + ("gross_profit_identity_unavailable",)))),
                    "reviewed_concept_priority_no_identity",
                )

        for (metric, component), (row, flags, method) in chosen.items():
            reported = float(row["numeric_value"])
            normalized = reported
            sign_method = "none"
            if metric == "capital_expenditures":
                normalized_capex = normalize_capex_payment(reported, str(row["concept"]))
                normalized = normalized_capex.normalized_value
                sign_method = normalized_capex.method
                if normalized_capex.sign_changed:
                    flags = tuple(sorted(set(flags + ("capex_sign_normalized",))))
            elif metric in {"cost_of_revenue", "depreciation_amortization"}:
                normalized = abs(reported)
                if reported < 0:
                    sign_method = "absolute_value_of_reported_expense"
                    flags = tuple(sorted(set(flags + ("expense_sign_normalized",))))
            decisions.append(
                CanonicalDecision(
                    raw_fact_id=int(row["raw_fact_id"]),
                    source_observation_id=str(row['source_observation_id']),
                    ticker=str(row["ticker"]),
                    accession_number=str(row["accession_number"]),
                    taxonomy=str(row.get("taxonomy") or ""),
                    source_concept=str(row["concept"]),
                    metric=metric,
                    component=component,
                    statement_type=str(row["statement_type"]),
                    period_start=str(row.get("period_start") or "") or None,
                    period_end=str(row["period_end"]),
                    accepted_at=str(row["accepted_at"]),
                    reported_currency=str(row["currency"]),
                    reported_value=reported,
                    normalized_value=normalized,
                    selection_method=method,
                    sign_normalization_method=sign_method,
                    quality_flags=tuple(sorted(set(flags))),
                )
            )
            audit["selected"] += 1

    decisions.sort(
        key=lambda row: (
            row.ticker,
            row.metric,
            row.component,
            row.period_end,
            row.period_start or "",
            row.accepted_at,
            row.source_observation_id,
        )
    )
    return CanonicalSelectionResult(tuple(decisions), dict(sorted(audit.items())))


def build_financial_feature_bundle(
    canonical_rows: Iterable[Mapping[str, Any]],
    *,
    as_of: str,
    listing_start_date: str | None,
    listing_end_date: str | None,
    maximum_period_age_days: int,
    inline_xbrl_fallback_required: bool = False,
) -> FinancialFeatureBundle:
    """Build one point-in-time feature row with explicit basis lineage."""

    asof_date = date.fromisoformat(as_of[:10])
    reasons: set[str] = set()
    empty_values = {column: None for column in FEATURE_COLUMNS}
    if listing_start_date and asof_date < date.fromisoformat(listing_start_date[:10]):
        return _empty_bundle(empty_values, "ineligible", "before_listing_start")
    if listing_end_date and asof_date > date.fromisoformat(listing_end_date[:10]):
        return _empty_bundle(empty_values, "ineligible", "after_listing_end")

    rows = [dict(row) for row in canonical_rows]
    flow_candidates: dict[str, list[FinancialValue]] = defaultdict(list)
    flow_audit: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for canonical_metric, feature_metric in FLOW_METRIC_NAMES.items():
        metric_rows = [row for row in rows if str(row.get("canonical_metric")) == canonical_metric]
        grouped: dict[tuple[str, str], list[FinancialFact]] = defaultdict(list)
        for row in metric_rows:
            grouped[(str(row.get("taxonomy") or ""), str(row.get("reported_currency") or ""))].append(
                _canonical_financial_fact(row, feature_metric)
            )
        for context, facts in grouped.items():
            selected = select_safe_flow_value(facts, as_of=as_of[:10] + "T23:59:59Z")
            flow_audit[feature_metric].append(
                {
                    "taxonomy": context[0],
                    "currency": context[1],
                    "status": selected.status,
                    "quality_flags": list(selected.quality_flags),
                }
            )
            if selected.selected is not None:
                flow_candidates[feature_metric].append(selected.selected)

    anchors = flow_candidates.get("revenue", [])
    if not anchors:
        lineage = {"flow_selection": flow_audit, "basis": None}
        return FinancialFeatureBundle(
            values=empty_values,
            basis_period_end=None,
            feature_definition_version=FEATURE_DEFINITION_VERSION,
            lineage=lineage,
            quality_status="missing",
            quality_reasons=("no_safe_revenue_basis",),
        )
    latest_end = max(str(value.period_end) for value in anchors)
    latest_anchors = [value for value in anchors if str(value.period_end) == latest_end]
    contexts = {(value.taxonomy, value.currency, str(value.period_start or "")) for value in latest_anchors}
    if len(contexts) != 1:
        lineage = {"flow_selection": flow_audit, "basis": None}
        return FinancialFeatureBundle(
            values=empty_values,
            basis_period_end=None,
            feature_definition_version=FEATURE_DEFINITION_VERSION,
            lineage=lineage,
            quality_status="missing",
            quality_reasons=("ambiguous_revenue_reporting_context",),
        )
    anchor = max(latest_anchors, key=lambda value: str(value.accepted_at or ""))
    basis = (str(anchor.period_end), anchor.taxonomy, anchor.currency, str(anchor.period_start or ""))
    selected_values: dict[str, FinancialValue] = {"revenue": anchor}
    rejected_flow_lineage: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for metric, candidates in flow_candidates.items():
        if metric == "revenue":
            continue
        exact = [
            value
            for value in candidates
            if (
                str(value.period_end), value.taxonomy, value.currency, str(value.period_start or "")
            ) == basis
        ]
        if len(exact) == 1:
            selected_values[metric] = exact[0]
        elif len(exact) > 1:
            reasons.add(f"ambiguous_flow_context:{metric}")
        elif candidates:
            reasons.add(f"available_but_context_mismatched:{metric}")
            for value in candidates:
                mismatch_flags = _flow_basis_mismatch_flags(value, basis, metric)
                reasons.update(mismatch_flags)
                rejected_flow_lineage[metric].append(
                    {
                        "period_start": str(value.period_start or ""),
                        "period_end": str(value.period_end),
                        "taxonomy": value.taxonomy,
                        "reported_currency": value.currency,
                        "basis": value.basis,
                        "lineage": list(value.lineage),
                        "quality_flags": list(mismatch_flags),
                    }
                )

    for metric, audit_rows in flow_audit.items():
        if metric != "revenue" and metric not in flow_candidates and audit_rows:
            reasons.add(f"source_present_but_no_safe_flow:{metric}")

    instant_lineage: dict[str, Any] = {}
    for metric in INSTANT_METRICS:
        current = _instant_value(rows, metric, basis[0], basis[1], basis[2])
        if current is not None:
            selected_values[metric] = current
            instant_lineage[metric] = list(current.lineage)
        prior = _prior_instant_value(rows, metric, basis[0], basis[1], basis[2])
        if current is not None and prior is not None:
            average_name = f"{metric}_average"
            selected_values[average_name] = FinancialValue(
                metric=average_name,
                value=(current.value + prior.value) / 2.0,
                period_start=None,
                period_end=basis[0],
                taxonomy=basis[1],
                currency=basis[2],
                accepted_at=max(str(current.accepted_at or ""), str(prior.accepted_at or "")),
                basis="average_beginning_ending_balance",
                lineage=tuple(current.lineage + prior.lineage),
            )
            instant_lineage[average_name] = list(current.lineage + prior.lineage)

    ratio_set = construct_financial_ratios(selected_values)
    outputs: dict[str, float | None] = {
        "revenue_ttm_usd": anchor.value,
        "gross_margin": _ratio_value(ratio_set, "gross_margin"),
        "operating_margin": _ratio_value(ratio_set, "operating_margin"),
        "free_cash_flow_margin": _ratio_value(ratio_set, "free_cash_flow_margin"),
        "return_on_invested_capital": _ratio_value(ratio_set, "return_on_invested_capital"),
        "net_debt_to_ebitda": _ratio_value(ratio_set, "net_debt_to_ebitda"),
        "inventory_turnover": _ratio_value(ratio_set, "inventory_turnover"),
    }
    reasons.update(ratio_set.quality_flags)
    age_days = (asof_date - date.fromisoformat(basis[0])).days
    if age_days < 0:
        reasons.add("future_basis_period")
        outputs = empty_values
    elif age_days > maximum_period_age_days:
        reasons.add(f"stale_basis_period:{age_days}_days")
        outputs = empty_values
    if inline_xbrl_fallback_required:
        reasons.add("inline_xbrl_fallback_required")
    for row in rows:
        if str(row.get("period_end")) != basis[0]:
            continue
        try:
            reasons.update(str(flag) for flag in json.loads(str(row.get("quality_flags_json") or "[]")))
        except (TypeError, ValueError, json.JSONDecodeError):
            reasons.add("invalid_canonical_quality_flags_json")

    populated = sum(value is not None for value in outputs.values())
    if populated == 0:
        status = "stale" if any(reason.startswith("stale_basis_period") for reason in reasons) else "missing"
    elif populated == len(outputs) and not reasons:
        status = "complete"
    else:
        status = "partial"
    lineage = {
        "basis": {
            "period_start": basis[3],
            "period_end": basis[0],
            "taxonomy": basis[1],
            "reported_currency": basis[2],
            "age_days": age_days,
        },
        "flow_selection": flow_audit,
        "selected_flow_lineage": {
            metric: list(value.lineage)
            for metric, value in selected_values.items()
            if metric in FLOW_METRIC_NAMES.values()
        },
        "rejected_flow_lineage": dict(sorted(rejected_flow_lineage.items())),
        "instant_lineage": instant_lineage,
        "ratio_quality_flags": {
            metric: list(outcome.quality_flags) for metric, outcome in ratio_set.ratios.items()
        },
    }
    return FinancialFeatureBundle(
        values=outputs,
        basis_period_end=basis[0],
        feature_definition_version=FEATURE_DEFINITION_VERSION,
        lineage=lineage,
        quality_status=status,
        quality_reasons=tuple(sorted(reasons)),
    )


def legacy_feature_values(rows: Sequence[Sequence[Any]]) -> dict[str, float | None]:
    """Compatibility wrapper for legacy unit fixtures; production stores lineage."""

    canonical: list[dict[str, Any]] = []
    latest_acceptance = "1900-01-01"
    for position, row in enumerate(rows):
        metric = str(row[0])
        accepted = str(row[4])
        latest_acceptance = max(latest_acceptance, accepted[:10])
        canonical.append(
            {
                "canonical_metric": metric,
                "canonical_component": str(row[1]),
                "accession_number": f"legacy-{position}",
                "taxonomy": "legacy",
                "source_concept": metric,
                "period_start": row[2],
                "period_end": row[3],
                "accepted_at": accepted,
                "frequency": row[5],
                "value_usd": row[6],
                "reported_currency": "USD",
                "source_raw_fact_id": position + 1,
                "quality_flags_json": "[]",
            }
        )
    bundle = build_financial_feature_bundle(
        canonical,
        as_of=latest_acceptance,
        listing_start_date=None,
        listing_end_date=None,
        maximum_period_age_days=100_000,
    )
    return dict(bundle.values)


def _priority_candidate(
    candidates: Sequence[dict[str, Any]],
) -> tuple[dict[str, Any] | None, tuple[str, ...]]:
    minimum_priority = min(int(row["priority"]) for row in candidates)
    finalists = [row for row in candidates if int(row["priority"]) == minimum_priority]
    values = {float(row["numeric_value"]) for row in finalists}
    if len(values) > 1:
        return None, ("conflicting_equal_priority_values",)
    finalists.sort(key=lambda row: (
        str(row.get('concept') or ''),str(row.get('source_observation_id') or '')
    ))
    flags = ("duplicate_equal_value_context",) if len(finalists) > 1 else ()
    return finalists[0], flags


def _financial_fact(row: Mapping[str, Any], metric: str) -> FinancialFact:
    return FinancialFact(
        metric=metric,
        value=float(row["numeric_value"]),
        period_start=str(row.get("period_start") or "") or None,
        period_end=str(row["period_end"]),
        accepted_at=str(row["accepted_at"]),
        accession_number=str(row["accession_number"]),
        taxonomy=str(row.get("taxonomy") or ""),
        currency=str(row["currency"]),
        concept=str(row.get("concept") or ""),
        raw_fact_id=str(row["raw_fact_id"]),
    )


def _canonical_financial_fact(row: Mapping[str, Any], metric: str) -> FinancialFact:
    return FinancialFact(
        metric=metric,
        value=float(row["value_usd"]),
        period_start=str(row.get("period_start") or "") or None,
        period_end=str(row["period_end"]),
        accepted_at=str(row["accepted_at"]),
        accession_number=str(row.get("accession_number") or ""),
        taxonomy=str(row.get("taxonomy") or ""),
        currency=str(row.get("reported_currency") or ""),
        concept=str(row.get("source_concept") or ""),
        raw_fact_id=str(
            row.get('source_observation_id')
            or row.get('source_raw_fact_id') or ''
        ),
        frequency=str(row.get("frequency") or ""),
    )


def _instant_value(
    rows: Sequence[dict[str, Any]], metric: str, period_end: str, taxonomy: str, currency: str
) -> FinancialValue | None:
    candidates = [
        row
        for row in rows
        if str(row.get("canonical_metric")) == metric
        and str(row.get("period_end")) == period_end
        and str(row.get("taxonomy") or "") == taxonomy
        and str(row.get("reported_currency") or "") == currency
    ]
    if not candidates:
        return None
    by_component: dict[str, dict[str, Any]] = {}
    for row in candidates:
        component = str(row.get("canonical_component") or "total")
        previous = by_component.get(component)
        if previous is None or str(row.get("accepted_at") or "") > str(previous.get("accepted_at") or ""):
            by_component[component] = row
    if not by_component:
        return None
    return FinancialValue(
        metric=metric,
        value=sum(float(row["value_usd"]) for row in by_component.values()),
        period_start=None,
        period_end=period_end,
        taxonomy=taxonomy,
        currency=currency,
        accepted_at=max(str(row.get("accepted_at") or "") for row in by_component.values()),
        basis="reported_instant",
        lineage=tuple(sorted(str(
            row.get('source_observation_id')
            or row.get('source_raw_fact_id') or ''
        ) for row in by_component.values())),
    )


def _prior_instant_value(
    rows: Sequence[dict[str, Any]], metric: str, period_end: str, taxonomy: str, currency: str
) -> FinancialValue | None:
    current_end = date.fromisoformat(period_end)
    prior_dates = sorted(
        {
            str(row.get("period_end"))
            for row in rows
            if str(row.get("canonical_metric")) == metric
            and str(row.get("taxonomy") or "") == taxonomy
            and str(row.get("reported_currency") or "") == currency
            and row.get("period_end")
            and 330 <= (current_end - date.fromisoformat(str(row["period_end"]))).days <= 400
        },
        key=lambda value: abs((current_end - date.fromisoformat(value)).days - 365),
    )
    return _instant_value(rows, metric, prior_dates[0], taxonomy, currency) if prior_dates else None


def _ratio_value(ratio_set: Any, name: str) -> float | None:
    outcome = ratio_set.ratios.get(name)
    return outcome.value if outcome is not None else None


def _flow_basis_mismatch_flags(
    value: FinancialValue,
    basis: tuple[str, str, str, str],
    metric: str,
) -> tuple[str, ...]:
    flags: list[str] = []
    if str(value.period_end) != basis[0]:
        flags.append(f"period_end_mismatch:{metric}")
    if value.taxonomy != basis[1]:
        flags.append(f"taxonomy_mismatch:{metric}")
    if value.currency != basis[2]:
        flags.append(f"currency_mismatch:{metric}")
    if str(value.period_start or "") != basis[3]:
        flags.append(f"period_start_mismatch:{metric}")
    return tuple(flags)


def _distinct_context(row: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row.get("concept") or ""),
        str(row.get("period_start") or ""),
        str(row.get("period_end") or ""),
        str(row.get("component") or ""),
    )


def _unique_plurality(groups: Mapping[str, set[Any]]) -> str | None:
    ranked = sorted(((len(values), key) for key, values in groups.items() if key), reverse=True)
    if not ranked:
        return None
    if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
        return None
    return ranked[0][1]


def _finite_or_none(raw: Any) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _empty_bundle(
    values: Mapping[str, float | None], status: str, reason: str
) -> FinancialFeatureBundle:
    return FinancialFeatureBundle(
        values=values,
        basis_period_end=None,
        feature_definition_version=FEATURE_DEFINITION_VERSION,
        lineage={"basis": None, "reason": reason},
        quality_status=status,
        quality_reasons=(reason,),
    )


__all__ = [
    "CanonicalDecision",
    "CanonicalSelectionResult",
    "FEATURE_DEFINITION_VERSION",
    "FinancialFeatureBundle",
    "build_financial_feature_bundle",
    "legacy_feature_values",
    "select_canonical_financial_facts",
]
