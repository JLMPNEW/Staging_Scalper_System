"""Pure financial-semantic controls for Consumer Defensive fundamentals.

This module deliberately has no database, network, or pipeline dependencies.  It
provides deterministic building blocks for validating facts before Stage 4 turns
them into model features:

* point-in-time FX anomaly classification using only earlier observations;
* revenue-concept selection through the gross-profit accounting identity;
* explicit normalization of capital-expenditure payment signs; and
* ratio construction that refuses to combine incompatible reporting contexts.

Callers retain responsibility for persistence.  Every operation returns lineage
or quality flags so a rejected value is explainable rather than silently coerced.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import math
from statistics import median
from typing import Iterable, Mapping, Sequence


_MAD_NORMALIZATION = 0.6744897501960817
_DAYS_IN_YEAR_RANGE = (330, 430)
_DAYS_IN_QUARTER_RANGE = (60, 150)


@dataclass(frozen=True)
class FxRateObservation:
    """One currency-to-reporting-currency daily conversion rate."""

    currency: str
    rate_date: str | date
    rate: float


@dataclass(frozen=True)
class RedenominationExemption:
    """An explicit, auditable interval in which an FX regime change is expected."""

    currency: str
    start_date: str | date
    end_date: str | date
    reason: str


@dataclass(frozen=True)
class FxRateDecision:
    """Classification and local robust statistics for one FX observation."""

    observation: FxRateObservation
    status: str
    local_median: float | None
    local_mad: float | None
    robust_z: float | None
    relative_deviation: float | None
    reason: str

    @property
    def is_usable(self) -> bool:
        """Whether the observation may be used rather than quarantined."""

        return self.status != "quarantined_outlier"


@dataclass(frozen=True)
class FinancialFact:
    """A reported fact with the context needed for semantic reconciliation."""

    metric: str
    value: float
    period_end: str | date
    taxonomy: str
    currency: str
    period_start: str | date | None = None
    accepted_at: str | date | datetime | None = None
    accession_number: str | None = None
    concept: str | None = None
    raw_fact_id: str | None = None
    frequency: str | None = None


@dataclass(frozen=True)
class RevenueCandidateScore:
    """Identity residual for a revenue candidate in the accepted context."""

    fact: FinancialFact
    absolute_residual: float
    relative_residual: float


@dataclass(frozen=True)
class RevenueSelection:
    """Result of accounting-identity-based revenue concept selection."""

    status: str
    selected: FinancialFact | None
    context: tuple[str, str, str, str, str] | None
    scores: tuple[RevenueCandidateScore, ...]
    quality_flags: tuple[str, ...]
    lineage: tuple[str, ...]
    normalized_cost_of_revenue: float | None = None


@dataclass(frozen=True)
class CapexNormalization:
    """A capex payment magnitude plus its unchanged reported representation."""

    concept: str
    reported_value: float
    normalized_value: float
    method: str
    sign_changed: bool


@dataclass(frozen=True)
class FinancialValue:
    """A selected financial value ready for same-context feature construction."""

    metric: str
    value: float
    period_end: str | date
    taxonomy: str
    currency: str
    period_start: str | date | None = None
    accepted_at: str | date | datetime | None = None
    basis: str = "reported"
    lineage: tuple[str, ...] = ()


@dataclass(frozen=True)
class FlowSelection:
    """Selection of a direct annual or safely assembled trailing flow."""

    status: str
    selected: FinancialValue | None
    quality_flags: tuple[str, ...]


@dataclass(frozen=True)
class RatioOutcome:
    """One ratio value, or an explicit null with quality flags."""

    value: float | None
    period_end: str | None
    taxonomy: str | None
    currency: str | None
    input_metrics: tuple[str, ...]
    quality_flags: tuple[str, ...]


@dataclass(frozen=True)
class FinancialRatioSet:
    """All supported ratios anchored to one reporting context."""

    ratios: Mapping[str, RatioOutcome]
    period_end: str | None
    taxonomy: str | None
    currency: str | None
    quality_flags: tuple[str, ...]


DEFAULT_CAPEX_PAYMENT_CONCEPTS = frozenset(
    {
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PurchaseOfPropertyPlantAndEquipment",
        "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
    }
)


def classify_fx_daily_rates(
    observations: Iterable[FxRateObservation],
    *,
    window: int = 21,
    minimum_history: int = 5,
    robust_z_threshold: float = 8.0,
    relative_deviation_threshold: float = 0.35,
    exemptions: Iterable[RedenominationExemption] = (),
) -> tuple[FxRateDecision, ...]:
    """Classify daily FX observations with a trailing rolling median and MAD.

    Only observations strictly earlier than the rate being classified enter its
    local distribution, so the decision is point-in-time safe.  Rates are
    evaluated in log space and must breach both the robust-z and relative-change
    thresholds before quarantine.  Explicit redenomination intervals override a
    statistical quarantine and remain visible as ``redenomination_exempt``.

    The return order is deterministic: currency, then date.
    """

    if window < 3:
        raise ValueError("window must be at least 3")
    if minimum_history < 2 or minimum_history > window:
        raise ValueError("minimum_history must be between 2 and window")
    if robust_z_threshold <= 0 or relative_deviation_threshold <= 0:
        raise ValueError("outlier thresholds must be positive")

    normalized: list[tuple[str, date, FxRateObservation]] = []
    seen: set[tuple[str, date]] = set()
    for observation in observations:
        currency = _normalize_currency(observation.currency)
        rate_date = _to_date(observation.rate_date, "rate_date")
        rate = _finite_number(observation.rate, "rate")
        if rate <= 0:
            raise ValueError("FX rates must be strictly positive")
        key = (currency, rate_date)
        if key in seen:
            raise ValueError(f"duplicate FX observation for {currency} on {rate_date.isoformat()}")
        seen.add(key)
        normalized.append((currency, rate_date, observation))

    exemption_rows: list[tuple[str, date, date, str]] = []
    for exemption in exemptions:
        currency = _normalize_currency(exemption.currency)
        start = _to_date(exemption.start_date, "exemption start_date")
        end = _to_date(exemption.end_date, "exemption end_date")
        if end < start:
            raise ValueError("redenomination exemption end_date precedes start_date")
        reason = exemption.reason.strip()
        if not reason:
            raise ValueError("redenomination exemptions require a reason")
        exemption_rows.append((currency, start, end, reason))

    by_currency: dict[str, list[tuple[date, FxRateObservation]]] = defaultdict(list)
    for currency, rate_date, observation in normalized:
        by_currency[currency].append((rate_date, observation))

    decisions: list[FxRateDecision] = []
    for currency in sorted(by_currency):
        rows = sorted(by_currency[currency], key=lambda row: row[0])
        for index, (rate_date, observation) in enumerate(rows):
            prior = rows[max(0, index - window) : index]
            local_median: float | None = None
            local_mad: float | None = None
            robust_z: float | None = None
            relative_deviation: float | None = None
            exemption_reason = _redenomination_reason(currency, rate_date, exemption_rows)

            if len(prior) >= minimum_history:
                prior_logs = [math.log(_finite_number(row.rate, "rate")) for _, row in prior]
                median_log = median(prior_logs)
                mad_log = median(abs(value - median_log) for value in prior_logs)
                observed_log = math.log(_finite_number(observation.rate, "rate"))
                local_median = math.exp(median_log)
                local_mad = mad_log
                relative_deviation = abs(observation.rate / local_median - 1.0)
                log_deviation = abs(observed_log - median_log)
                if mad_log <= 1e-15:
                    robust_z = 0.0 if log_deviation <= 1e-15 else math.inf
                else:
                    robust_z = _MAD_NORMALIZATION * log_deviation / mad_log

            if exemption_reason is not None:
                status = "redenomination_exempt"
                reason = f"explicit redenomination exemption: {exemption_reason}"
            elif len(prior) < minimum_history:
                status = "insufficient_history"
                reason = f"only {len(prior)} prior observations; {minimum_history} required"
            elif (
                robust_z is not None
                and relative_deviation is not None
                and robust_z > robust_z_threshold
                and relative_deviation > relative_deviation_threshold
            ):
                status = "quarantined_outlier"
                reason = "rate breached robust-z and relative-deviation thresholds"
            else:
                status = "valid"
                reason = "rate is consistent with its trailing robust distribution"

            decisions.append(
                FxRateDecision(
                    observation=observation,
                    status=status,
                    local_median=local_median,
                    local_mad=local_mad,
                    robust_z=robust_z,
                    relative_deviation=relative_deviation,
                    reason=reason,
                )
            )

    return tuple(decisions)


def select_revenue_candidate(
    candidates: Sequence[FinancialFact],
    *,
    cost_of_revenue: FinancialFact,
    gross_profit: FinancialFact,
    reporting_currency: str | None = None,
    maximum_relative_residual: float = 0.02,
    tie_tolerance: float = 1e-12,
) -> RevenueSelection:
    """Select revenue by ``revenue = gross profit + |cost of revenue|``.

    A candidate is eligible only when accession, start, end, taxonomy, and
    currency exactly match both identity reference facts.  Mixed currencies are
    rejected unless the caller explicitly supplies ``reporting_currency``.
    Ties remain ambiguous rather than being resolved by magnitude or concept
    priority.  ``scores`` and ``lineage`` preserve every eligible candidate.
    """

    if maximum_relative_residual < 0 or tie_tolerance < 0:
        raise ValueError("residual tolerances must be non-negative")
    if not candidates:
        return RevenueSelection(
            status="missing_candidates",
            selected=None,
            context=None,
            scores=(),
            quality_flags=("missing_revenue_candidates",),
            lineage=(),
        )

    try:
        cost_context = _fact_context(cost_of_revenue)
        gross_context = _fact_context(gross_profit)
    except ValueError as exc:
        return RevenueSelection(
            status="incomplete_reference_context",
            selected=None,
            context=None,
            scores=(),
            quality_flags=(f"incomplete_reference_context:{exc}",),
            lineage=(),
        )

    if cost_context != gross_context:
        return RevenueSelection(
            status="reference_context_mismatch",
            selected=None,
            context=None,
            scores=(),
            quality_flags=("cost_and_gross_profit_context_mismatch",),
            lineage=(),
        )

    context = cost_context
    base_context = context[:4]
    explicit_currency = _normalize_currency(reporting_currency) if reporting_currency else None
    if explicit_currency is not None and explicit_currency != context[4]:
        return RevenueSelection(
            status="reporting_currency_mismatch",
            selected=None,
            context=context,
            scores=(),
            quality_flags=("reporting_currency_differs_from_identity_context",),
            lineage=(),
        )

    parsed: list[tuple[FinancialFact, tuple[str, str, str, str, str]]] = []
    rejected_context = False
    for fact in candidates:
        _finite_number(fact.value, "candidate value")
        try:
            candidate_context = _fact_context(fact)
        except ValueError:
            rejected_context = True
            continue
        parsed.append((fact, candidate_context))

    same_base_currencies = {candidate_context[4] for _, candidate_context in parsed if candidate_context[:4] == base_context}
    if explicit_currency is None and len(same_base_currencies) > 1:
        return RevenueSelection(
            status="ambiguous_currency_context",
            selected=None,
            context=context,
            scores=(),
            quality_flags=("multiple_candidate_currencies_require_explicit_reporting_currency",),
            lineage=tuple(sorted(_fact_lineage(fact) for fact, _ in parsed)),
        )

    eligible = [fact for fact, candidate_context in parsed if candidate_context == context]
    if not eligible:
        flags = ["no_candidate_matches_identity_context"]
        if rejected_context:
            flags.append("candidate_with_incomplete_context_rejected")
        return RevenueSelection(
            status="no_exact_context_candidate",
            selected=None,
            context=context,
            scores=(),
            quality_flags=tuple(sorted(flags)),
            lineage=tuple(sorted(_fact_lineage(fact) for fact, _ in parsed)),
        )

    cost_magnitude = abs(_finite_number(cost_of_revenue.value, "cost_of_revenue value"))
    gross_value = _finite_number(gross_profit.value, "gross_profit value")
    target_revenue = gross_value + cost_magnitude
    scores: list[RevenueCandidateScore] = []
    for fact in eligible:
        value = _finite_number(fact.value, "candidate value")
        absolute = abs(value - target_revenue)
        scale = max(abs(value), abs(target_revenue), 1.0)
        scores.append(
            RevenueCandidateScore(
                fact=fact,
                absolute_residual=absolute,
                relative_residual=absolute / scale,
            )
        )

    scores.sort(
        key=lambda score: (
            score.relative_residual,
            score.absolute_residual,
            score.fact.concept or "",
            score.fact.raw_fact_id or "",
            score.fact.value,
        )
    )
    quality_flags: list[str] = []
    if rejected_context or len(eligible) != len(parsed):
        quality_flags.append("candidate_context_rejected")
    if cost_of_revenue.value < 0:
        quality_flags.append("cost_of_revenue_sign_normalized")
    lineage = tuple(_fact_lineage(score.fact) for score in scores)
    best = scores[0]

    tied = [
        score
        for score in scores[1:]
        if math.isclose(score.relative_residual, best.relative_residual, rel_tol=0.0, abs_tol=tie_tolerance)
        and math.isclose(score.absolute_residual, best.absolute_residual, rel_tol=0.0, abs_tol=tie_tolerance)
    ]
    if tied:
        quality_flags.append("accounting_identity_tie")
        return RevenueSelection(
            status="ambiguous_identity_tie",
            selected=None,
            context=context,
            scores=tuple(scores),
            quality_flags=tuple(sorted(quality_flags)),
            lineage=lineage,
            normalized_cost_of_revenue=cost_magnitude,
        )

    if best.relative_residual > maximum_relative_residual:
        quality_flags.append("gross_profit_identity_not_reconciled")
        return RevenueSelection(
            status="unreconciled_identity",
            selected=None,
            context=context,
            scores=tuple(scores),
            quality_flags=tuple(sorted(quality_flags)),
            lineage=lineage,
            normalized_cost_of_revenue=cost_magnitude,
        )

    return RevenueSelection(
        status="selected",
        selected=best.fact,
        context=context,
        scores=tuple(scores),
        quality_flags=tuple(sorted(quality_flags)),
        lineage=lineage,
        normalized_cost_of_revenue=cost_magnitude,
    )


def normalize_capex_payment(
    reported_value: float,
    concept: str,
    *,
    payment_concepts: frozenset[str] = DEFAULT_CAPEX_PAYMENT_CONCEPTS,
) -> CapexNormalization:
    """Normalize a capex *payment* fact to a non-negative cash-use magnitude.

    The raw value is never discarded.  Unknown concepts fail closed because an
    absolute-value transform is not valid for proceeds or net-disposal concepts.
    """

    if concept not in payment_concepts:
        raise ValueError(f"unsupported capex payment concept: {concept}")
    value = _finite_number(reported_value, "reported_value")
    if value < 0:
        method = "absolute_value_of_negative_payment"
    elif value > 0:
        method = "reported_positive_payment_magnitude"
    else:
        method = "reported_zero_payment_magnitude"
    return CapexNormalization(
        concept=concept,
        reported_value=value,
        normalized_value=abs(value),
        method=method,
        sign_changed=value < 0,
    )


def select_safe_flow_value(
    facts: Sequence[FinancialFact],
    *,
    as_of: str | date | datetime | None = None,
) -> FlowSelection:
    """Select a direct annual flow or assemble a safe four-quarter TTM value.

    Revisions are resolved by latest ``accepted_at`` within the exact start/end
    period.  Facts must share metric, taxonomy, and currency.  A direct annual is
    not used when a newer interim fact exists; that stale fallback returns null.
    Four-quarter TTM construction requires four non-overlapping quarter-like
    durations with plausible end-date spacing.
    """

    if not facts:
        return FlowSelection("missing", None, ("missing_flow_facts",))

    metrics = {fact.metric for fact in facts}
    taxonomies = {fact.taxonomy for fact in facts}
    currencies = {_normalize_currency(fact.currency) for fact in facts}
    if len(metrics) != 1:
        return FlowSelection("context_mismatch", None, ("mixed_metrics",))
    if len(taxonomies) != 1:
        return FlowSelection("context_mismatch", None, ("mixed_taxonomies",))
    if len(currencies) != 1:
        return FlowSelection("context_mismatch", None, ("mixed_currencies",))

    cutoff = _to_datetime(as_of, "as_of") if as_of is not None else None
    eligible: list[FinancialFact] = []
    missing_acceptance = False
    for fact in facts:
        _finite_number(fact.value, "flow value")
        if fact.period_start is None:
            continue
        if cutoff is not None:
            if fact.accepted_at is None:
                missing_acceptance = True
                continue
            if _to_datetime(fact.accepted_at, "accepted_at") > cutoff:
                continue
        eligible.append(fact)
    if not eligible:
        flags = ["no_point_in_time_eligible_flow_facts"]
        if missing_acceptance:
            flags.append("missing_acceptance_timestamp")
        return FlowSelection("missing", None, tuple(sorted(flags)))

    # Keep one accepted revision per exact reported period.
    by_period: dict[tuple[date, date], list[FinancialFact]] = defaultdict(list)
    for fact in eligible:
        start = _to_date(fact.period_start, "period_start")
        end = _to_date(fact.period_end, "period_end")
        if end < start:
            continue
        by_period[(start, end)].append(fact)

    selected_revisions: list[tuple[date, date, FinancialFact]] = []
    for (start, end), revisions in by_period.items():
        revisions.sort(key=_revision_sort_key)
        selected_revisions.append((start, end, revisions[-1]))

    selected_revisions.sort(key=lambda row: (row[1], row[0]), reverse=True)
    if not selected_revisions:
        return FlowSelection("missing", None, ("no_valid_flow_periods",))

    quarters = [row for row in selected_revisions if _DAYS_IN_QUARTER_RANGE[0] <= (row[1] - row[0]).days <= _DAYS_IN_QUARTER_RANGE[1]]
    if len(quarters) >= 4:
        latest_four = sorted(quarters[:4], key=lambda row: row[1])
        end_gaps = [(right[1] - left[1]).days for left, right in zip(latest_four, latest_four[1:])]
        non_overlapping = all(left[1] < right[0] for left, right in zip(latest_four, latest_four[1:]))
        total_span = (latest_four[-1][1] - latest_four[0][0]).days
        if non_overlapping and all(70 <= gap <= 110 for gap in end_gaps) and 330 <= total_span <= 430:
            first_start = latest_four[0][0]
            latest_end = latest_four[-1][1]
            chosen = [row[2] for row in latest_four]
            accepted_dates = [accepted for fact in chosen if (accepted := _accepted_datetime(fact)) is not None]
            selected = FinancialValue(
                metric=chosen[0].metric,
                value=sum(fact.value for fact in chosen),
                period_start=first_start.isoformat(),
                period_end=latest_end.isoformat(),
                taxonomy=chosen[0].taxonomy,
                currency=_normalize_currency(chosen[0].currency),
                accepted_at=max(accepted_dates, default=None),
                basis="ttm_four_quarters",
                lineage=tuple(_fact_lineage(fact) for fact in chosen),
            )
            return FlowSelection("selected_ttm", selected, ())

    annuals = [row for row in selected_revisions if _DAYS_IN_YEAR_RANGE[0] <= (row[1] - row[0]).days <= _DAYS_IN_YEAR_RANGE[1]]
    if annuals:
        annual_start, annual_end, annual = annuals[0]
        newer = [row for row in selected_revisions if row[1] > annual_end]
        if newer:
            current_start, current_end, current = max(
                newer,
                key=lambda row: (row[1], (row[1] - row[0]).days, _revision_sort_key(row[2])),
            )
            current_days = (current_end - current_start).days
            comparable = [
                row
                for row in selected_revisions
                if row != (current_start, current_end, current)
                and 330 <= (current_end - row[1]).days <= 400
                and abs((row[1] - row[0]).days - current_days) <= 20
            ]
            if comparable:
                prior_start, prior_end, prior = max(
                    comparable,
                    key=lambda row: (row[1], _revision_sort_key(row[2])),
                )
                accepted_dates = [
                    accepted
                    for fact in (annual, current, prior)
                    if (accepted := _accepted_datetime(fact)) is not None
                ]
                selected = FinancialValue(
                    metric=annual.metric,
                    value=annual.value + current.value - prior.value,
                    period_start=(current_end - timedelta(days=364)).isoformat(),
                    period_end=current_end.isoformat(),
                    taxonomy=annual.taxonomy,
                    currency=_normalize_currency(annual.currency),
                    accepted_at=max(accepted_dates, default=None),
                    basis="ttm_annual_plus_current_minus_prior",
                    lineage=tuple(_fact_lineage(fact) for fact in (annual, current, prior)),
                )
                return FlowSelection("selected_ttm_bridge", selected, ())
            return FlowSelection(
                "unreconciled_newer_interim",
                None,
                ("direct_annual_is_stale_relative_to_newer_interim",),
            )
        selected = FinancialValue(
            metric=annual.metric,
            value=annual.value,
            period_start=annual_start.isoformat(),
            period_end=annual_end.isoformat(),
            taxonomy=annual.taxonomy,
            currency=_normalize_currency(annual.currency),
            accepted_at=annual.accepted_at,
            basis="direct_annual",
            lineage=(_fact_lineage(annual),),
        )
        return FlowSelection("selected_annual", selected, ())

    return FlowSelection("missing", None, ("no_safe_annual_or_ttm_candidate",))


def construct_financial_ratios(
    values: Mapping[str, FinancialValue],
    *,
    anchor_metric: str = "revenue",
) -> FinancialRatioSet:
    """Construct supported ratios without crossing reporting contexts.

    ``anchor_metric`` establishes the period end, taxonomy, currency, and flow
    window.  Each ratio independently returns ``None`` when any required input is
    missing or incompatible.  Instant balance-sheet inputs need the same period
    end but no period start; flow inputs must also share the anchor's start date
    when both starts are known.
    """

    anchor = values.get(anchor_metric)
    if anchor is None:
        outcome = RatioOutcome(
            value=None,
            period_end=None,
            taxonomy=None,
            currency=None,
            input_metrics=(anchor_metric,),
            quality_flags=(f"missing_input:{anchor_metric}",),
        )
        return FinancialRatioSet(
            ratios={"gross_margin": outcome},
            period_end=None,
            taxonomy=None,
            currency=None,
            quality_flags=(f"missing_input:{anchor_metric}",),
        )

    try:
        anchor_end = _to_date(anchor.period_end, "anchor period_end").isoformat()
        anchor_currency = _normalize_currency(anchor.currency)
        _finite_number(anchor.value, "anchor value")
    except ValueError as exc:
        outcome = RatioOutcome(None, None, None, None, (anchor_metric,), (f"invalid_anchor:{exc}",))
        return FinancialRatioSet(
            ratios={"gross_margin": outcome},
            period_end=None,
            taxonomy=None,
            currency=None,
            quality_flags=outcome.quality_flags,
        )

    anchor_context = (anchor_end, anchor.taxonomy, anchor_currency)
    outcomes: dict[str, RatioOutcome] = {}

    def make_outcome(
        name: str,
        required: Sequence[str],
        calculator: object,
        *,
        flow_metrics: frozenset[str] = frozenset(),
    ) -> None:
        flags: list[str] = []
        selected: dict[str, FinancialValue] = {}
        for metric in required:
            value = values.get(metric)
            if value is None:
                flags.append(f"missing_input:{metric}")
                continue
            try:
                value_end = _to_date(value.period_end, f"{metric} period_end").isoformat()
                value_currency = _normalize_currency(value.currency)
                _finite_number(value.value, f"{metric} value")
            except ValueError as exc:
                flags.append(f"invalid_input:{metric}:{exc}")
                continue
            if value_end != anchor_context[0]:
                flags.append(f"period_end_mismatch:{metric}")
            if value.taxonomy != anchor_context[1]:
                flags.append(f"taxonomy_mismatch:{metric}")
            if value_currency != anchor_context[2]:
                flags.append(f"currency_mismatch:{metric}")
            if metric in flow_metrics and anchor.period_start is not None and value.period_start is not None:
                anchor_start = _to_date(anchor.period_start, "anchor period_start")
                value_start = _to_date(value.period_start, f"{metric} period_start")
                if value_start != anchor_start:
                    flags.append(f"period_start_mismatch:{metric}")
            selected[metric] = value

        result: float | None = None
        if not flags:
            try:
                # calculator is kept local and receives the validated metric map.
                result = calculator(selected)  # type: ignore[operator]
                if result is not None:
                    result = _finite_number(result, f"{name} result")
            except (ArithmeticError, ValueError):
                flags.append(f"invalid_arithmetic:{name}")
                result = None
        if result is not None:
            flags.extend(_ratio_plausibility_flags(name, result))
        outcomes[name] = RatioOutcome(
            value=result,
            period_end=anchor_context[0],
            taxonomy=anchor_context[1],
            currency=anchor_context[2],
            input_metrics=tuple(required),
            quality_flags=tuple(sorted(set(flags))),
        )

    revenue_flow = frozenset({anchor_metric, "gross_profit", "cost_of_revenue", "operating_income", "operating_cash_flow", "capex", "pretax_income", "income_tax_expense", "depreciation_and_amortization"})

    if "gross_profit" in values:
        make_outcome(
            "gross_margin",
            (anchor_metric, "gross_profit"),
            lambda selected: _safe_divide(selected["gross_profit"].value, selected[anchor_metric].value),
            flow_metrics=revenue_flow,
        )
    else:
        make_outcome(
            "gross_margin",
            (anchor_metric, "cost_of_revenue"),
            lambda selected: _safe_divide(
                selected[anchor_metric].value - abs(selected["cost_of_revenue"].value),
                selected[anchor_metric].value,
            ),
            flow_metrics=revenue_flow,
        )

    make_outcome(
        "operating_margin",
        (anchor_metric, "operating_income"),
        lambda selected: _safe_divide(selected["operating_income"].value, selected[anchor_metric].value),
        flow_metrics=revenue_flow,
    )

    def free_cash_flow_margin(selected: Mapping[str, FinancialValue]) -> float:
        free_cash_flow = selected["operating_cash_flow"].value - abs(selected["capex"].value)
        return _safe_divide(free_cash_flow, selected[anchor_metric].value)

    make_outcome(
        "free_cash_flow_margin",
        (anchor_metric, "operating_cash_flow", "capex"),
        free_cash_flow_margin,
        flow_metrics=revenue_flow,
    )

    def roic(selected: Mapping[str, FinancialValue]) -> float:
        pretax = selected["pretax_income"].value
        if pretax <= 0:
            tax_rate = 0.0
        else:
            tax_rate = min(max(selected["income_tax_expense"].value / pretax, 0.0), 0.5)
        nopat = selected["operating_income"].value * (1.0 - tax_rate)
        invested_capital = (
            selected["equity_average"].value
            + selected["debt_current_average"].value
            + selected["debt_noncurrent_average"].value
            - selected["cash_average"].value
        )
        return _safe_divide(nopat, invested_capital)

    make_outcome(
        "return_on_invested_capital",
        (
            "operating_income",
            "pretax_income",
            "income_tax_expense",
            "equity_average",
            "debt_current_average",
            "debt_noncurrent_average",
            "cash_average",
        ),
        roic,
        flow_metrics=revenue_flow,
    )

    def leverage(selected: Mapping[str, FinancialValue]) -> float:
        net_debt = selected["debt_current"].value + selected["debt_noncurrent"].value - selected["cash"].value
        ebitda = selected["operating_income"].value + abs(selected["depreciation_and_amortization"].value)
        return _safe_divide(net_debt, ebitda)

    make_outcome(
        "net_debt_to_ebitda",
        ("debt_current", "debt_noncurrent", "cash", "operating_income", "depreciation_and_amortization"),
        leverage,
        flow_metrics=revenue_flow,
    )

    make_outcome(
        "inventory_turnover",
        ("cost_of_revenue", "inventory_average"),
        lambda selected: _safe_divide(abs(selected["cost_of_revenue"].value), selected["inventory_average"].value),
        flow_metrics=revenue_flow,
    )

    all_flags = tuple(sorted({flag for outcome in outcomes.values() for flag in outcome.quality_flags}))
    return FinancialRatioSet(
        ratios=outcomes,
        period_end=anchor_context[0],
        taxonomy=anchor_context[1],
        currency=anchor_context[2],
        quality_flags=all_flags,
    )


def _fact_context(fact: FinancialFact) -> tuple[str, str, str, str, str]:
    accession = (fact.accession_number or "").strip()
    taxonomy = fact.taxonomy.strip()
    if not accession:
        raise ValueError("missing accession_number")
    if fact.period_start is None:
        raise ValueError("missing period_start")
    if not taxonomy:
        raise ValueError("missing taxonomy")
    start = _to_date(fact.period_start, "period_start").isoformat()
    end = _to_date(fact.period_end, "period_end").isoformat()
    if end < start:
        raise ValueError("period_end precedes period_start")
    return (accession, start, end, taxonomy, _normalize_currency(fact.currency))


def _fact_lineage(fact: FinancialFact) -> str:
    if fact.raw_fact_id:
        return fact.raw_fact_id
    return "|".join(
        (
            fact.accession_number or "",
            fact.concept or fact.metric,
            str(fact.period_start or ""),
            str(fact.period_end),
            fact.taxonomy,
            _normalize_currency(fact.currency),
            format(fact.value, ".17g"),
        )
    )


def _revision_sort_key(fact: FinancialFact) -> tuple[datetime, str, str]:
    accepted = _accepted_datetime(fact) or datetime.min
    return (accepted, fact.accession_number or "", fact.raw_fact_id or "")


def _accepted_datetime(fact: FinancialFact) -> datetime | None:
    if fact.accepted_at is None:
        return None
    return _to_datetime(fact.accepted_at, "accepted_at")


def _ratio_plausibility_flags(name: str, value: float) -> list[str]:
    bounds = {
        "gross_margin": (-1.0, 1.0),
        "operating_margin": (-1.0, 1.0),
        "free_cash_flow_margin": (-2.0, 1.0),
        "return_on_invested_capital": (-2.0, 2.0),
        "net_debt_to_ebitda": (-20.0, 20.0),
        "inventory_turnover": (0.0, 100.0),
    }
    lower, upper = bounds[name]
    if value < lower or value > upper:
        return [f"plausibility_outlier:{name}"]
    return []


def _safe_divide(numerator: float, denominator: float) -> float:
    if not math.isfinite(numerator) or not math.isfinite(denominator):
        raise ValueError("ratio inputs must be finite")
    if abs(denominator) <= 1e-15:
        raise ZeroDivisionError("ratio denominator is zero")
    return numerator / denominator


def _normalize_currency(currency: str | None) -> str:
    normalized = (currency or "").strip().upper()
    if not normalized:
        raise ValueError("currency is required")
    return normalized


def _finite_number(value: float, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _to_date(value: str | date | datetime, name: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an ISO date") from exc


def _to_datetime(value: str | date | datetime, name: str) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an ISO date or timestamp") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _redenomination_reason(
    currency: str,
    rate_date: date,
    exemptions: Sequence[tuple[str, date, date, str]],
) -> str | None:
    matches = [reason for exempt_currency, start, end, reason in exemptions if exempt_currency == currency and start <= rate_date <= end]
    if not matches:
        return None
    return "; ".join(sorted(set(matches)))


__all__ = [
    "CapexNormalization",
    "DEFAULT_CAPEX_PAYMENT_CONCEPTS",
    "FinancialFact",
    "FinancialRatioSet",
    "FinancialValue",
    "FlowSelection",
    "FxRateDecision",
    "FxRateObservation",
    "RatioOutcome",
    "RedenominationExemption",
    "RevenueCandidateScore",
    "RevenueSelection",
    "classify_fx_daily_rates",
    "construct_financial_ratios",
    "normalize_capex_payment",
    "select_revenue_candidate",
    "select_safe_flow_value",
]
