from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from datetime import date
from typing import Mapping, Sequence, cast


FINANCIAL_REPAIR_VERSION = (
    "transportation_dp6m_financial_input_repair_v1"
)
FINANCIAL_REPAIR_PAIR_FIELDS = (
    "repair_version",
    "repair_id",
    "pair_key",
    "ticker",
    "universe_role",
    "calibration_cohort",
    "primary_archetype",
    "metric_id",
    "output_feature",
    "formula",
    "unit_contract",
    "period_type",
    "bounds_policy",
    "current_feature_source_id",
    "current_feature_period_end",
    "current_output_value",
    "financial_availability_status",
    "financial_availability_reason",
    "required_dependency_ids",
    "missing_dependency_ids",
    "latest_dependency_periods_json",
    "repair_classification",
    "qa_flags",
    "source_hierarchy",
    "expected_output_evidence_label",
    "diagnostic_confidence",
    "freshness_status",
    "loadability_status",
    "required_action",
    "candidate_source_lane_ids",
    "search_terms",
    "endpoint_id",
    "endpoint_type",
    "discovery_url",
    "retrieval_included_in_one_pass",
    "feature_rebuild_authorized",
)
FINANCIAL_DEPENDENCY_FIELDS = (
    "repair_version",
    "repair_id",
    "pair_key",
    "ticker",
    "metric_id",
    "dependency_id",
    "dependency_role",
    "canonical_metric_ids",
    "feature_fields",
    "required_period_alignment",
    "required_unit",
    "minimum_distinct_periods",
    "existing_fact_count",
    "existing_distinct_period_count",
    "latest_period_end",
    "latest_source_id",
    "latest_accession_number",
    "latest_form_type",
    "freshness_status",
    "requirement_status",
    "required_action",
    "source_id_policy",
    "evidence_label",
    "confidence",
)

SOURCE_HIERARCHY = (
    "loaded_canonical_financial_fact>"
    "filed_or_issuer_primary_annual_or_interim_statement>"
    "issuer_earnings_release_or_financial_supplement>"
    "issuer_or_exchange_registration_or_carveout_statement>"
    "foreign_home_market_annual_report>missing_required_source"
)
FINANCIAL_SOURCE_LANES = (
    "issuer_ir_annual_report_pdf",
    "issuer_ir_earnings_release",
    "issuer_ir_operating_statistics_supplement",
    "primary_local_exchange_regulatory_filing",
    "foreign_issuer_home_market_annual_report",
)


def _dependency(
    dependency_id: str,
    *,
    role: str,
    canonical_metrics: Sequence[str],
    feature_fields: Sequence[str],
    period_alignment: str,
    unit: str,
    minimum_periods: int = 1,
) -> dict[str, object]:
    return {
        "dependency_id": dependency_id,
        "role": role,
        "canonical_metrics": tuple(canonical_metrics),
        "feature_fields": tuple(feature_fields),
        "period_alignment": period_alignment,
        "unit": unit,
        "minimum_periods": minimum_periods,
    }


FINANCIAL_REPAIR_RULES: dict[str, dict[str, object]] = {
    "pre_revenue_flag": {
        "output_feature": "revenue_ttm_usd",
        "availability_metric": "revenue",
        "formula": (
            "1_if_explicit_revenue_ttm_usd_le_0_else_0;"
            "missing_revenue_is_not_zero"
        ),
        "unit_contract": "boolean",
        "period_type": "point_in_time",
        "bounds_policy": "boolean",
        "dependencies": (
            _dependency(
                "revenue_ttm",
                role="reported_revenue_basis",
                canonical_metrics=("revenue",),
                feature_fields=("revenue_ttm_usd",),
                period_alignment="complete_ttm_window",
                unit="USD",
            ),
        ),
        "search_terms": ("revenue", "net revenue", "operating revenue"),
    },
    "cash_runway_years": {
        "output_feature": "cash_runway_years",
        "availability_metric": "cash_runway_years",
        "formula": (
            "cash_and_equivalents_usd_divided_by_cash_burn_ttm_usd;"
            "not_applicable_when_cash_burn_le_0"
        ),
        "unit_contract": "years",
        "period_type": "point_in_time",
        "bounds_policy": "years_0_100",
        "dependencies": (
            _dependency(
                "cash_balance",
                role="runway_numerator",
                canonical_metrics=("cash_and_equivalents",),
                feature_fields=("cash_and_equivalents_usd",),
                period_alignment="latest_balance_at_or_before_ttm_end",
                unit="USD",
            ),
            _dependency(
                "cash_burn_ttm",
                role="runway_denominator",
                canonical_metrics=("operating_cash_flow", "capex"),
                feature_fields=(
                    "operating_cash_flow_ttm_usd",
                    "capex_ttm_usd",
                ),
                period_alignment="matching_complete_ttm_windows",
                unit="USD_per_year",
            ),
        ),
        "search_terms": (
            "cash and cash equivalents",
            "net cash provided by operating activities",
            "capital expenditures",
        ),
    },
    "quarterly_cash_burn": {
        "output_feature": "cash_burn_ttm_usd",
        "availability_metric": "cash_runway_years",
        "formula": (
            "max(0,-free_cash_flow_ttm_usd)_divided_by_4;"
            "free_cash_flow_equals_operating_cash_flow_plus_capex"
        ),
        "unit_contract": "currency_per_quarter",
        "period_type": "fiscal_period",
        "bounds_policy": "nonnegative",
        "dependencies": (
            _dependency(
                "cash_burn_ttm",
                role="quarterly_burn_source",
                canonical_metrics=("operating_cash_flow", "capex"),
                feature_fields=(
                    "operating_cash_flow_ttm_usd",
                    "capex_ttm_usd",
                ),
                period_alignment="matching_complete_ttm_windows",
                unit="USD_per_year",
            ),
        ),
        "search_terms": (
            "net cash provided by operating activities",
            "capital expenditures",
        ),
    },
    "capital_raise_dependence": {
        "output_feature": "capital_raise_dependence",
        "availability_metric": "capital_raise_dependence",
        "formula": (
            "(equity_issuance_ttm_usd+debt_issuance_ttm_usd)"
            "_divided_by_cash_burn_ttm_usd;"
            "equals_0_when_cash_burn_le_0"
        ),
        "unit_contract": "ratio",
        "period_type": "fiscal_period",
        "bounds_policy": "ratio_0_10",
        "dependencies": (
            _dependency(
                "cash_burn_ttm",
                role="dependence_denominator",
                canonical_metrics=("operating_cash_flow", "capex"),
                feature_fields=(
                    "operating_cash_flow_ttm_usd",
                    "capex_ttm_usd",
                ),
                period_alignment="matching_complete_ttm_windows",
                unit="USD_per_year",
            ),
            _dependency(
                "equity_issuance_ttm",
                role="capital_raise_numerator_component",
                canonical_metrics=("equity_issuance_proceeds",),
                feature_fields=("equity_issuance_proceeds_ttm_usd",),
                period_alignment="same_ttm_window_as_cash_burn",
                unit="USD",
            ),
            _dependency(
                "debt_issuance_ttm",
                role="capital_raise_numerator_component",
                canonical_metrics=("debt_issuance_proceeds",),
                feature_fields=("debt_issuance_proceeds_ttm_usd",),
                period_alignment="same_ttm_window_as_cash_burn",
                unit="USD",
            ),
        ),
        "search_terms": (
            "proceeds from issuance of shares",
            "proceeds from borrowings",
            "net cash provided by operating activities",
            "capital expenditures",
        ),
    },
    "diluted_share_growth": {
        "output_feature": "diluted_shares_yoy_growth",
        "availability_metric": "diluted_shares_yoy_growth",
        "formula": (
            "current_diluted_weighted_average_shares_divided_by_prior_"
            "comparable_diluted_shares_minus_1;"
            "basic_shares_allowed_only_when_basic_and_diluted_eps_equal;"
            "development_stage_period_end_shares_are_last_resort_proxy"
        ),
        "unit_contract": "ratio",
        "period_type": "fiscal_period",
        "bounds_policy": "growth_ratio",
        "dependencies": (
            _dependency(
                "comparable_share_pair",
                role="current_and_prior_share_basis",
                canonical_metrics=(
                    "diluted_shares",
                    "basic_shares",
                    "shares_outstanding",
                ),
                feature_fields=(),
                period_alignment=(
                    "two_comparable_annual_periods_same_share_basis"
                ),
                unit="shares",
                minimum_periods=2,
            ),
        ),
        "search_terms": (
            "diluted weighted average shares",
            "basic weighted average shares",
            "shares outstanding",
        ),
    },
    "stock_compensation_to_revenue": {
        "output_feature": "sbc_pct_revenue",
        "availability_metric": "sbc_pct_revenue",
        "formula": (
            "stock_based_compensation_divided_by_same_period_revenue;"
            "not_applicable_when_explicit_revenue_is_zero;"
            "missing_revenue_is_not_zero"
        ),
        "unit_contract": "ratio",
        "period_type": "fiscal_period",
        "bounds_policy": "ratio_0_100",
        "dependencies": (
            _dependency(
                "stock_based_compensation",
                role="ratio_numerator",
                canonical_metrics=("stock_based_compensation",),
                feature_fields=("stock_based_compensation",),
                period_alignment="same_fiscal_period_as_revenue",
                unit="reported_currency",
            ),
            _dependency(
                "revenue",
                role="ratio_denominator",
                canonical_metrics=("revenue",),
                feature_fields=("revenue",),
                period_alignment=(
                    "same_fiscal_period_as_stock_based_compensation"
                ),
                unit="reported_currency",
            ),
        ),
        "search_terms": (
            "stock based compensation",
            "share based compensation",
            "revenue",
        ),
    },
}


def _stable_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _number(value: object) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        number = float(str(value))
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _freshness(
    *,
    latest_period: str,
    asof_date: str,
    max_staleness_days: int,
) -> str:
    if not latest_period:
        return "missing"
    try:
        lag = (
            date.fromisoformat(asof_date[:10])
            - date.fromisoformat(latest_period[:10])
        ).days
    except ValueError:
        return "unknown"
    return "current" if lag <= max_staleness_days else "stale"


def _worst_freshness(values: Sequence[str]) -> str:
    rank = {
        "missing": 4,
        "stale": 3,
        "unknown": 2,
        "current": 1,
    }
    return max(values, key=lambda value: rank.get(value, 5))


def _canonical_index(
    rows: Sequence[Mapping[str, object]],
) -> dict[tuple[str, str], list[Mapping[str, object]]]:
    output: dict[
        tuple[str, str],
        list[Mapping[str, object]],
    ] = {}
    for row in rows:
        key = (
            str(row.get("ticker") or "").upper(),
            str(row.get("canonical_metric") or ""),
        )
        output.setdefault(key, []).append(row)
    return output


def build_financial_repair_contracts(
    *,
    residual_rows: Sequence[Mapping[str, str]],
    feature_rows: Mapping[str, Mapping[str, object]],
    availability_rows: Mapping[
        tuple[str, str],
        Mapping[str, object],
    ],
    canonical_rows: Sequence[Mapping[str, object]],
    endpoint_rows: Mapping[str, Mapping[str, str]],
    asof_date: str,
    max_staleness_days: int = 550,
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[str],
]:
    financial = [
        row for row in residual_rows if row["source_lane"] == "FIN-D"
    ]
    canonical = _canonical_index(canonical_rows)
    pair_output: list[dict[str, object]] = []
    dependency_output: list[dict[str, object]] = []
    errors: list[str] = []
    for residual in sorted(
        financial,
        key=lambda row: (row["metric_id"], row["ticker"]),
    ):
        ticker = residual["ticker"].upper()
        metric_id = residual["metric_id"]
        pair_key = residual["pair_key"]
        rule = FINANCIAL_REPAIR_RULES.get(metric_id)
        if rule is None:
            errors.append(f"{pair_key}: no financial repair rule")
            continue
        endpoint = endpoint_rows.get(ticker)
        if endpoint is None:
            errors.append(f"{pair_key}: no sealed issuer endpoint")
            continue
        feature = feature_rows.get(ticker, {})
        output_feature = str(rule["output_feature"])
        current_output = _number(feature.get(output_feature))
        if current_output is not None:
            errors.append(
                f"{pair_key}: residual output unexpectedly has a value"
            )
        availability_metric = str(rule["availability_metric"])
        availability = availability_rows.get(
            (ticker, availability_metric),
            {},
        )
        repair_id = "trnfin_" + _stable_sha256(
            {
                "version": FINANCIAL_REPAIR_VERSION,
                "pair_key": pair_key,
                "formula": rule["formula"],
            }
        )[:24]
        dependency_ids: list[str] = []
        missing_dependencies: list[str] = []
        dependency_periods: dict[str, str] = {}
        dependency_freshness: list[str] = []
        raw_dependencies = cast(
            Sequence[Mapping[str, object]],
            rule["dependencies"],
        )
        for raw_dependency in raw_dependencies:
            if not isinstance(raw_dependency, Mapping):
                errors.append(f"{pair_key}: invalid dependency contract")
                continue
            dependency_id = str(raw_dependency["dependency_id"])
            dependency_ids.append(dependency_id)
            metrics = tuple(
                str(value)
                for value in cast(
                    Sequence[object],
                    raw_dependency["canonical_metrics"],
                )
            )
            facts = [
                fact
                for canonical_metric in metrics
                for fact in canonical.get(
                    (ticker, canonical_metric),
                    (),
                )
            ]
            facts.sort(
                key=lambda row: (
                    str(row.get("period_end") or ""),
                    str(row.get("filing_date") or ""),
                    str(row.get("source_id") or ""),
                ),
                reverse=True,
            )
            periods = {
                str(row.get("period_end") or "")[:10]
                for row in facts
                if str(row.get("period_end") or "")[:10]
            }
            latest = facts[0] if facts else {}
            latest_period = str(
                latest.get("period_end") or ""
            )[:10]
            freshness = _freshness(
                latest_period=latest_period,
                asof_date=asof_date,
                max_staleness_days=max_staleness_days,
            )
            dependency_freshness.append(freshness)
            dependency_periods[dependency_id] = latest_period
            feature_fields = tuple(
                str(value)
                for value in cast(
                    Sequence[object],
                    raw_dependency["feature_fields"],
                )
            )
            feature_inputs_present = bool(feature_fields) and all(
                _number(feature.get(field)) is not None
                for field in feature_fields
            )
            minimum_periods = int(
                str(raw_dependency["minimum_periods"])
            )
            if feature_inputs_present:
                requirement_status = "PRESENT_IN_ALIGNED_FEATURE"
                required_action = "REUSE_EXISTING_ALIGNED_FEATURE_INPUT"
                evidence_label = "fact_source_reported"
                confidence = "high"
            elif len(periods) >= minimum_periods:
                requirement_status = (
                    "PRESENT_REQUIRES_PERIOD_OR_COMPARABILITY_REPAIR"
                )
                required_action = (
                    "REPAIR_ALIGNMENT_FROM_EXISTING_CANONICAL_FACTS"
                )
                evidence_label = "fact_source_reported"
                confidence = "medium"
            else:
                requirement_status = "MISSING_REQUIRED_SOURCE"
                required_action = (
                    "RETRIEVE_PRIMARY_FINANCIAL_SOURCE_IN_ONE_PASS"
                )
                evidence_label = "missing_required_source"
                confidence = "high"
                missing_dependencies.append(dependency_id)
            dependency_output.append(
                {
                    "repair_version": FINANCIAL_REPAIR_VERSION,
                    "repair_id": repair_id,
                    "pair_key": pair_key,
                    "ticker": ticker,
                    "metric_id": metric_id,
                    "dependency_id": dependency_id,
                    "dependency_role": raw_dependency["role"],
                    "canonical_metric_ids": "|".join(metrics),
                    "feature_fields": "|".join(feature_fields),
                    "required_period_alignment": raw_dependency[
                        "period_alignment"
                    ],
                    "required_unit": raw_dependency["unit"],
                    "minimum_distinct_periods": minimum_periods,
                    "existing_fact_count": len(facts),
                    "existing_distinct_period_count": len(periods),
                    "latest_period_end": latest_period,
                    "latest_source_id": (
                        latest.get("source_id") or ""
                    ),
                    "latest_accession_number": (
                        latest.get("accession_number") or ""
                    ),
                    "latest_form_type": (
                        latest.get("form_type") or ""
                    ),
                    "freshness_status": freshness,
                    "requirement_status": requirement_status,
                    "required_action": required_action,
                    "source_id_policy": (
                        latest.get("source_id")
                        or "MISSING_REQUIRED_SOURCE"
                    ),
                    "evidence_label": evidence_label,
                    "confidence": confidence,
                }
            )
        cash_burn = _number(feature.get("cash_burn_ttm_usd"))
        revenue = _number(feature.get("revenue"))
        formula_not_applicable = (
            metric_id == "cash_runway_years"
            and cash_burn is not None
            and cash_burn <= 0
        ) or (
            metric_id == "stock_compensation_to_revenue"
            and revenue is not None
            and revenue <= 0
        )
        if formula_not_applicable:
            classification = "FORMULA_DEFINED_NOT_APPLICABLE"
            required_action = (
                "RECLASSIFY_NOT_APPLICABLE_IN_FINAL_COVERAGE"
            )
            loadability = "NOT_APPLICABLE_RECLASSIFICATION_PENDING"
            retrieval = 0
            qa_flags = "CONDITIONAL_APPLICABILITY_NOT_IN_COVERAGE_GATE"
            confidence = "high"
        elif missing_dependencies:
            classification = "SOURCE_OR_PERIOD_GAP"
            required_action = (
                "RETRIEVE_MISSING_FINANCIAL_INPUTS_IN_ONE_PASS"
            )
            loadability = "BLOCKED_MISSING_REQUIRED_SOURCE"
            retrieval = 1
            qa_flags = "MISSING_REQUIRED_SOURCE"
            confidence = "high"
        else:
            classification = "ALIGNMENT_OR_FORMULA_PIPELINE_GAP"
            required_action = (
                "REPAIR_ALIGNMENT_FROM_EXISTING_CANONICAL_FACTS_FIRST"
            )
            loadability = "BLOCKED_ALIGNMENT_REPAIR"
            retrieval = 0
            qa_flags = "PERIOD_OR_COMPARABILITY_ALIGNMENT_REQUIRED"
            confidence = "medium"
        pair_output.append(
            {
                "repair_version": FINANCIAL_REPAIR_VERSION,
                "repair_id": repair_id,
                "pair_key": pair_key,
                "ticker": ticker,
                "universe_role": residual["universe_role"],
                "calibration_cohort": residual[
                    "calibration_cohort"
                ],
                "primary_archetype": residual[
                    "primary_archetype"
                ],
                "metric_id": metric_id,
                "output_feature": output_feature,
                "formula": rule["formula"],
                "unit_contract": rule["unit_contract"],
                "period_type": rule["period_type"],
                "bounds_policy": rule["bounds_policy"],
                "current_feature_source_id": (
                    feature.get("source_id") or ""
                ),
                "current_feature_period_end": (
                    feature.get("fiscal_period_end") or ""
                ),
                "current_output_value": (
                    "" if current_output is None else current_output
                ),
                "financial_availability_status": (
                    availability.get("availability_status") or ""
                ),
                "financial_availability_reason": (
                    availability.get("status_reason") or ""
                ),
                "required_dependency_ids": "|".join(dependency_ids),
                "missing_dependency_ids": "|".join(
                    missing_dependencies
                ),
                "latest_dependency_periods_json": json.dumps(
                    dependency_periods,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                "repair_classification": classification,
                "qa_flags": qa_flags,
                "source_hierarchy": SOURCE_HIERARCHY,
                "expected_output_evidence_label": (
                    "derived_calculation"
                ),
                "diagnostic_confidence": confidence,
                "freshness_status": _worst_freshness(
                    dependency_freshness
                ),
                "loadability_status": loadability,
                "required_action": required_action,
                "candidate_source_lane_ids": "|".join(
                    FINANCIAL_SOURCE_LANES
                ),
                "search_terms": "|".join(
                    cast(Sequence[str], rule["search_terms"])
                ),
                "endpoint_id": endpoint["endpoint_id"],
                "endpoint_type": endpoint["endpoint_type"],
                "discovery_url": endpoint["discovery_url"],
                "retrieval_included_in_one_pass": retrieval,
                "feature_rebuild_authorized": 0,
            }
        )
    return pair_output, dependency_output, errors


def summarize_financial_repair(
    rows: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    return {
        "financial_repair_pair_count": len(rows),
        "financial_repair_metric_count": len(
            {str(row["metric_id"]) for row in rows}
        ),
        "financial_repair_ticker_count": len(
            {str(row["ticker"]) for row in rows}
        ),
        "repair_classification_counts": dict(
            sorted(
                Counter(
                    str(row["repair_classification"]) for row in rows
                ).items()
            )
        ),
        "retrieval_included_in_one_pass_count": sum(
            int(str(row["retrieval_included_in_one_pass"]))
            for row in rows
        ),
        "not_applicable_reclassification_count": sum(
            str(row["repair_classification"])
            == "FORMULA_DEFINED_NOT_APPLICABLE"
            for row in rows
        ),
    }
