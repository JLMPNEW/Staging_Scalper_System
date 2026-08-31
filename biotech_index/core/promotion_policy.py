from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from biotech_index.core.calibration_metrics import finite_float


@dataclass(frozen=True)
class PromotionRules:
    min_outer_folds: int = 2
    min_fold_win_rate: float = 0.60
    require_positive_paired_delta_lcb: bool = True
    prefer_profit_factor_at_least: float = 1.0
    min_profit_factor_improvement: float = 0.0
    min_paired_delta_profit_factor: float = 1.0
    max_loss20_deterioration_pct: float = 2.0
    max_loss40_deterioration_pct: float = 1.0
    max_cvar_deterioration_pct: float = 5.0
    max_drawdown_deterioration_pct: float = 5.0
    max_top3_contribution_pct: float = 55.0
    min_paired_dates: int = 20
    min_active_date_coverage_pct: float = 25.0
    max_calibration_fallback_frequency_pct: float = 40.0
    min_robust_profit_factor: float = 1.0
    require_robust_profit_factor_support: bool = True
    require_secondary_horizon_no_harm: bool = True
    max_secondary_horizon_lcb_underperformance_pct: float = 3.0
    require_cohort_no_harm: bool = True
    max_cohort_lcb_underperformance_pct: float = 5.0
    min_cohort_paired_dates: int = 8
    required_secondary_horizons: tuple[int, ...] = ()
    required_no_harm_cohorts: tuple[str, ...] = ()
    provisional_active_weight_cap: float = 0.55


@dataclass(frozen=True)
class PromotionDecision:
    status: str
    authorized: bool
    provisional: bool
    reason_codes: tuple[str, ...]
    metrics: Mapping[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "promotion_status": self.status,
            "production_promotion_authorized": self.authorized,
            "provisional_promotion": self.provisional,
            "promotion_reason_codes": "|".join(self.reason_codes),
            **dict(self.metrics),
        }


def no_harm_reason_codes(
    *,
    primary_horizon: int,
    horizon_comparisons: Mapping[int, Mapping[str, object]],
    cohort_comparisons: Iterable[Mapping[str, object]],
    rules: PromotionRules,
) -> tuple[str, ...]:
    """Return supported secondary-horizon and cohort material-harm failures."""
    reasons: list[str] = []
    if rules.require_secondary_horizon_no_harm:
        horizons = set(horizon_comparisons).union(rules.required_secondary_horizons)
        for horizon in sorted(horizons):
            if horizon == primary_horizon:
                continue
            comparison = horizon_comparisons.get(horizon, {})
            paired_dates = int(finite_float(comparison.get("paired_date_count")) or 0)
            delta_lcb = finite_float(comparison.get("paired_delta_bootstrap_lcb_pct"))
            if paired_dates < rules.min_paired_dates or delta_lcb is None:
                if horizon in rules.required_secondary_horizons:
                    reasons.append(f"secondary_horizon_insufficient_evidence:{horizon}d")
                continue
            if delta_lcb < -abs(rules.max_secondary_horizon_lcb_underperformance_pct):
                reasons.append(f"secondary_horizon_no_harm_failure:{horizon}d")
    if rules.require_cohort_no_harm:
        aggregate_cohorts = {
            str(comparison.get("cohort") or "unknown").strip() or "unknown": comparison
            for comparison in cohort_comparisons
            if str(comparison.get("fold_id") or "") == "aggregate"
            and int(finite_float(comparison.get("horizon_days")) or 0) == primary_horizon
        }
        cohorts = set(aggregate_cohorts).union(rules.required_no_harm_cohorts)
        for cohort in sorted(cohorts):
            comparison = aggregate_cohorts.get(cohort, {})
            paired_dates = int(finite_float(comparison.get("paired_date_count")) or 0)
            delta_lcb = finite_float(comparison.get("paired_delta_bootstrap_lcb_pct"))
            if paired_dates < rules.min_cohort_paired_dates or delta_lcb is None:
                if cohort in rules.required_no_harm_cohorts:
                    reasons.append(f"cohort_insufficient_evidence:{cohort}")
                continue
            if delta_lcb < -abs(rules.max_cohort_lcb_underperformance_pct):
                reasons.append(f"cohort_no_harm_failure:{cohort}")
    return tuple(sorted(set(reasons)))


def apply_no_harm_gate(
    decision: PromotionDecision,
    failures: Iterable[str],
) -> PromotionDecision:
    """Revoke production authorization when a supported no-harm gate fails."""
    new_failures = tuple(sorted({str(value).strip() for value in failures if str(value).strip()}))
    if not new_failures:
        return decision
    reasons = tuple(dict.fromkeys((*decision.reason_codes, *new_failures)))
    metrics = {**dict(decision.metrics), "no_harm_failure_count": len(new_failures)}
    material_failures = tuple(reason for reason in new_failures if "no_harm_failure" in reason)
    if decision.authorized and material_failures:
        return PromotionDecision(
            status="research_only_no_harm_failure",
            authorized=False,
            provisional=False,
            reason_codes=reasons,
            metrics=metrics,
        )
    if decision.authorized:
        return PromotionDecision(
            status="provisional_blended_promotion",
            authorized=True,
            provisional=True,
            reason_codes=reasons,
            metrics=metrics,
        )
    return PromotionDecision(
        status=decision.status,
        authorized=False,
        provisional=False,
        reason_codes=reasons,
        metrics=metrics,
    )


def apply_deployment_readiness_gate(
    decision: PromotionDecision,
    *,
    deployment_ready: bool,
    reason: str = "live_scorer_parity_not_implemented",
) -> PromotionDecision:
    """Prevent statistical evidence from authorizing an unreproducible live policy."""
    clean_reason = str(reason).strip() or "live_scorer_parity_not_implemented"
    metrics = {**dict(decision.metrics), "live_deployment_ready": bool(deployment_ready)}
    if deployment_ready:
        return PromotionDecision(
            status=decision.status,
            authorized=decision.authorized,
            provisional=decision.provisional,
            reason_codes=decision.reason_codes,
            metrics=metrics,
        )
    reasons = tuple(dict.fromkeys((*decision.reason_codes, clean_reason)))
    return PromotionDecision(
        status="research_only_deployment_not_ready" if decision.authorized else decision.status,
        authorized=False,
        provisional=False,
        reason_codes=reasons,
        metrics=metrics,
    )


def deployment_active_weight(
    decision: PromotionDecision,
    calibrated_weight: float,
    rules: PromotionRules,
) -> float:
    """Return the governed live active weight for an authorized decision."""
    weight = max(0.0, min(1.0, float(calibrated_weight)))
    if not decision.authorized:
        return 0.0
    if decision.provisional:
        return min(weight, max(0.0, min(1.0, rules.provisional_active_weight_cap)))
    return weight


def decide_promotion(
    aggregate_comparison: Mapping[str, object],
    fold_comparisons: Iterable[Mapping[str, object]],
    rules: PromotionRules,
) -> PromotionDecision:
    folds = list(fold_comparisons)
    reasons: list[str] = []
    paired_dates = int(finite_float(aggregate_comparison.get("paired_date_count")) or 0)
    delta_lcb = finite_float(aggregate_comparison.get("paired_delta_bootstrap_lcb_pct"))
    candidate_pf = finite_float(aggregate_comparison.get("candidate_profit_factor"))
    incumbent_pf = finite_float(aggregate_comparison.get("incumbent_profit_factor"))
    delta_pf = finite_float(aggregate_comparison.get("delta_profit_factor"))
    candidate_loss20 = finite_float(aggregate_comparison.get("candidate_loss20_rate_pct"))
    incumbent_loss20 = finite_float(aggregate_comparison.get("incumbent_loss20_rate_pct"))
    candidate_loss40 = finite_float(aggregate_comparison.get("candidate_loss40_rate_pct"))
    incumbent_loss40 = finite_float(aggregate_comparison.get("incumbent_loss40_rate_pct"))
    candidate_cvar = finite_float(aggregate_comparison.get("candidate_cvar_return_pct"))
    incumbent_cvar = finite_float(aggregate_comparison.get("incumbent_cvar_return_pct"))
    candidate_drawdown = finite_float(aggregate_comparison.get("candidate_max_drawdown_pct"))
    incumbent_drawdown = finite_float(aggregate_comparison.get("incumbent_max_drawdown_pct"))
    top3 = finite_float(aggregate_comparison.get("candidate_top3_gain_contribution_pct"))
    winsor_pf = finite_float(aggregate_comparison.get("candidate_winsorized_profit_factor"))
    ex_largest_pf = finite_float(aggregate_comparison.get("candidate_profit_factor_ex_largest_winner"))
    ex_top3_pf = finite_float(aggregate_comparison.get("candidate_profit_factor_ex_top3_winners"))
    active_date_coverage = finite_float(aggregate_comparison.get("candidate_active_date_coverage_pct"))
    fallback_frequency = finite_float(aggregate_comparison.get("calibration_fallback_frequency_pct"))
    fold_delta_lcbs = [finite_float(fold.get("paired_delta_bootstrap_lcb_pct")) for fold in folds]
    wins = sum(1 for value in fold_delta_lcbs if value is not None and value > 0.0)
    fold_win_rate = wins / len(folds) if folds else 0.0

    if len(folds) < rules.min_outer_folds:
        reasons.append(f"outer_folds<{rules.min_outer_folds}")
    if paired_dates < rules.min_paired_dates:
        reasons.append(f"paired_dates<{rules.min_paired_dates}")
    if rules.require_positive_paired_delta_lcb and (delta_lcb is None or delta_lcb <= 0.0):
        reasons.append("paired_delta_lcb_not_positive")
    if candidate_pf is None or incumbent_pf is None:
        reasons.append("profit_factor_insufficient_support")
    elif candidate_pf < incumbent_pf + rules.min_profit_factor_improvement:
        reasons.append("profit_factor_not_better_than_incumbent")
    if candidate_pf is not None and candidate_pf < rules.prefer_profit_factor_at_least:
        reasons.append("candidate_profit_factor_below_absolute_floor")
    if delta_pf is None:
        reasons.append("paired_delta_profit_factor_insufficient_support")
    elif delta_pf < rules.min_paired_delta_profit_factor:
        reasons.append("paired_delta_profit_factor_below_floor")
    robust_values = (winsor_pf, ex_largest_pf, ex_top3_pf)
    if any(value is not None and value < rules.min_robust_profit_factor for value in robust_values):
        reasons.append("robust_profit_factor_below_floor")
    if rules.require_robust_profit_factor_support and any(value is None for value in robust_values):
        reasons.append("robust_profit_factor_insufficient_support")
    if fold_win_rate < rules.min_fold_win_rate:
        reasons.append(f"fold_win_rate<{rules.min_fold_win_rate}")
    if (
        candidate_loss20 is not None
        and incumbent_loss20 is not None
        and candidate_loss20 > incumbent_loss20 + rules.max_loss20_deterioration_pct
    ):
        reasons.append("loss20_materially_worse")
    if (
        candidate_loss40 is not None
        and incumbent_loss40 is not None
        and candidate_loss40 > incumbent_loss40 + rules.max_loss40_deterioration_pct
    ):
        reasons.append("loss40_materially_worse")
    if any(
        value is None
        for value in (candidate_loss20, incumbent_loss20, candidate_loss40, incumbent_loss40)
    ):
        reasons.append("loss_rate_insufficient_support")
    if (
        candidate_cvar is None
        or incumbent_cvar is None
        or candidate_drawdown is None
        or incumbent_drawdown is None
    ):
        reasons.append("tail_risk_insufficient_support")
    else:
        if candidate_cvar < incumbent_cvar - abs(rules.max_cvar_deterioration_pct):
            reasons.append("cvar_materially_worse")
        if candidate_drawdown < incumbent_drawdown - abs(rules.max_drawdown_deterioration_pct):
            reasons.append("max_drawdown_materially_worse")
    if top3 is not None and top3 > rules.max_top3_contribution_pct:
        reasons.append("top3_contribution_too_high")
    if active_date_coverage is None:
        reasons.append("active_date_coverage_insufficient_support")
    elif active_date_coverage < rules.min_active_date_coverage_pct:
        reasons.append("active_date_coverage_below_floor")
    if fallback_frequency is None:
        reasons.append("fallback_frequency_insufficient_support")
    elif fallback_frequency > rules.max_calibration_fallback_frequency_pct:
        reasons.append("fallback_frequency_too_high")

    hard_failures = {
        "paired_delta_lcb_not_positive",
        "profit_factor_insufficient_support",
        "profit_factor_not_better_than_incumbent",
        "loss20_materially_worse",
        "loss40_materially_worse",
        "top3_contribution_too_high",
        "candidate_profit_factor_below_absolute_floor",
        "paired_delta_profit_factor_insufficient_support",
        "paired_delta_profit_factor_below_floor",
        "robust_profit_factor_below_floor",
        "robust_profit_factor_insufficient_support",
        "loss_rate_insufficient_support",
        "tail_risk_insufficient_support",
        "cvar_materially_worse",
        "max_drawdown_materially_worse",
        "active_date_coverage_insufficient_support",
        "active_date_coverage_below_floor",
        "fallback_frequency_insufficient_support",
        "fallback_frequency_too_high",
    }
    has_hard_failure = bool(hard_failures.intersection(reasons)) or any(
        reason.startswith("paired_dates<") for reason in reasons
    )
    if not reasons:
        status = "full_promotion"
        authorized = True
        provisional = False
    elif not has_hard_failure and delta_lcb is not None and delta_lcb > 0.0:
        status = "provisional_blended_promotion"
        authorized = True
        provisional = True
    elif (
        delta_lcb is not None
        and delta_lcb > 0.0
        and candidate_pf is not None
        and incumbent_pf is not None
        and candidate_pf > incumbent_pf
    ):
        status = "research_only_relative_improvement"
        authorized = False
        provisional = False
    else:
        status = "benchmark_dominant_fallback"
        authorized = False
        provisional = False

    metrics = {
        "outer_fold_count": len(folds),
        "outer_fold_wins": wins,
        "outer_fold_win_rate": round(fold_win_rate, 6),
        "profit_factor_preference_met": bool(
            candidate_pf is not None and candidate_pf >= rules.prefer_profit_factor_at_least
        ),
        "robust_profit_factor_support_met": all(value is not None for value in robust_values),
        "robust_profit_factor_floor_met": all(
            value is not None and value >= rules.min_robust_profit_factor for value in robust_values
        ),
        **dict(aggregate_comparison),
    }
    return PromotionDecision(
        status=status,
        authorized=authorized,
        provisional=provisional,
        reason_codes=tuple(reasons or ["all_relative_promotion_gates_passed"]),
        metrics=metrics,
    )
