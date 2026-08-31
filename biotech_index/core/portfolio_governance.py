from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from biotech_index.core.calibration_metrics import finite_float


@dataclass(frozen=True)
class ProfitabilityPromotionRules:
    enabled: bool = True
    min_paired_daily_count: int = 252
    min_fold_count: int = 2
    min_fold_win_rate: float = 0.50
    min_candidate_profit_factor: float = 1.0
    max_drawdown_deterioration_pct: float = 5.0
    max_daily_cvar_deterioration_pct: float = 0.50
    min_provisional_composite_score: float = 0.0
    min_full_composite_score: float = 0.10
    min_full_deflated_sharpe_probability: float = 0.90
    require_positive_bootstrap_lcb_for_full: bool = True
    provisional_active_weight_cap: float = 0.25
    full_active_weight_cap: float = 1.0
    cagr_weight: float = 0.35
    calmar_weight: float = 0.20
    profit_factor_weight: float = 0.15
    drawdown_weight: float = 0.15
    cvar_weight: float = 0.10
    turnover_weight: float = 0.05
    cagr_scale_pct: float = 5.0
    calmar_scale: float = 0.50
    profit_factor_scale: float = 0.25
    drawdown_scale_pct: float = 5.0
    cvar_scale_pct: float = 0.50
    turnover_scale: float = 0.50

    def __post_init__(self) -> None:
        if self.min_paired_daily_count <= 0 or self.min_fold_count <= 0:
            raise ValueError("Profitability promotion support requirements must be positive")
        if not 0.0 <= self.min_fold_win_rate <= 1.0:
            raise ValueError("min_fold_win_rate must be between zero and one")
        for field in (
            "provisional_active_weight_cap",
            "full_active_weight_cap",
        ):
            if not 0.0 <= float(getattr(self, field)) <= 1.0:
                raise ValueError(f"{field} must be between zero and one")
        for field in (
            "cagr_scale_pct",
            "calmar_scale",
            "profit_factor_scale",
            "drawdown_scale_pct",
            "cvar_scale_pct",
            "turnover_scale",
        ):
            if float(getattr(self, field)) <= 0.0:
                raise ValueError(f"{field} must be positive")


@dataclass(frozen=True)
class ProfitabilityPromotionDecision:
    status: str
    authorized: bool
    provisional: bool
    active_weight_cap: float
    reason_codes: tuple[str, ...]
    metrics: Mapping[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "profitability_promotion_status": self.status,
            "profitability_promotion_authorized": self.authorized,
            "profitability_provisional_promotion": self.provisional,
            "profitability_active_weight_cap": self.active_weight_cap,
            "profitability_reason_codes": "|".join(self.reason_codes),
            **dict(self.metrics),
        }


def _bounded_delta(raw: object, scale: float) -> float:
    value = finite_float(raw)
    if value is None:
        return 0.0
    return max(-1.0, min(1.0, value / scale))


def profitability_composite_score(
    comparison: Mapping[str, object],
    rules: ProfitabilityPromotionRules,
) -> tuple[float, dict[str, float]]:
    components = {
        "cagr": _bounded_delta(comparison.get("delta_cagr_pct"), rules.cagr_scale_pct),
        "calmar": _bounded_delta(comparison.get("delta_calmar_ratio"), rules.calmar_scale),
        "profit_factor": _bounded_delta(
            comparison.get("delta_profit_factor"),
            rules.profit_factor_scale,
        ),
        "drawdown": _bounded_delta(
            comparison.get("delta_max_drawdown_pct"),
            rules.drawdown_scale_pct,
        ),
        "cvar": _bounded_delta(
            comparison.get("delta_daily_cvar_5pct"),
            rules.cvar_scale_pct,
        ),
        "turnover": _bounded_delta(
            -(finite_float(comparison.get("delta_gross_turnover_multiple")) or 0.0),
            rules.turnover_scale,
        ),
    }
    score = (
        rules.cagr_weight * components["cagr"]
        + rules.calmar_weight * components["calmar"]
        + rules.profit_factor_weight * components["profit_factor"]
        + rules.drawdown_weight * components["drawdown"]
        + rules.cvar_weight * components["cvar"]
        + rules.turnover_weight * components["turnover"]
    )
    return round(score, 6), components


def decide_profitability_promotion(
    comparison: Mapping[str, object],
    fold_comparisons: Iterable[Mapping[str, object]],
    rules: ProfitabilityPromotionRules,
) -> ProfitabilityPromotionDecision:
    folds = list(fold_comparisons)
    score, components = profitability_composite_score(comparison, rules)
    paired_days = int(finite_float(comparison.get("paired_daily_count")) or 0)
    candidate_wealth = finite_float(comparison.get("candidate_terminal_wealth"))
    incumbent_wealth = finite_float(comparison.get("incumbent_terminal_wealth"))
    candidate_pf = finite_float(comparison.get("candidate_profit_factor"))
    incumbent_pf = finite_float(comparison.get("incumbent_profit_factor"))
    candidate_drawdown = finite_float(comparison.get("candidate_max_drawdown_pct"))
    incumbent_drawdown = finite_float(comparison.get("incumbent_max_drawdown_pct"))
    candidate_cvar = finite_float(comparison.get("candidate_daily_cvar_5pct"))
    incumbent_cvar = finite_float(comparison.get("incumbent_daily_cvar_5pct"))
    bootstrap_lcb = finite_float(comparison.get("paired_annualized_delta_bootstrap_lcb_pct"))
    dsr = finite_float(comparison.get("candidate_deflated_sharpe_probability"))
    fold_wins = sum(
        1
        for row in folds
        if (finite_float(row.get("delta_terminal_wealth")) or 0.0) > 0.0
    )
    fold_win_rate = fold_wins / len(folds) if folds else 0.0
    reasons: list[str] = []

    if not rules.enabled:
        reasons.append("profitability_promotion_disabled")
    if paired_days < rules.min_paired_daily_count:
        reasons.append(f"paired_daily_count<{rules.min_paired_daily_count}")
    if len(folds) < rules.min_fold_count:
        reasons.append(f"fold_count<{rules.min_fold_count}")
    if fold_win_rate < rules.min_fold_win_rate:
        reasons.append(f"fold_win_rate<{rules.min_fold_win_rate}")
    profitable = (
        candidate_wealth is not None
        and incumbent_wealth is not None
        and candidate_wealth > incumbent_wealth
    )
    if not profitable:
        reasons.append("candidate_terminal_wealth_not_better")
    if candidate_pf is None:
        reasons.append("candidate_profit_factor_insufficient_support")
    elif candidate_pf < rules.min_candidate_profit_factor:
        reasons.append("candidate_profit_factor_below_floor")
    if candidate_drawdown is None or incumbent_drawdown is None:
        reasons.append("drawdown_insufficient_support")
    elif candidate_drawdown < incumbent_drawdown - abs(rules.max_drawdown_deterioration_pct):
        reasons.append("max_drawdown_materially_worse")
    if candidate_cvar is None or incumbent_cvar is None:
        reasons.append("daily_cvar_insufficient_support")
    elif candidate_cvar < incumbent_cvar - abs(rules.max_daily_cvar_deterioration_pct):
        reasons.append("daily_cvar_materially_worse")

    hard_failures = {
        "profitability_promotion_disabled",
        "candidate_terminal_wealth_not_better",
        "candidate_profit_factor_insufficient_support",
        "candidate_profit_factor_below_floor",
        "drawdown_insufficient_support",
        "max_drawdown_materially_worse",
        "daily_cvar_insufficient_support",
        "daily_cvar_materially_worse",
    }
    support_failure = any(
        reason.startswith(("paired_daily_count<", "fold_count<", "fold_win_rate<"))
        for reason in reasons
    )
    hard_failure = bool(hard_failures.intersection(reasons)) or support_failure
    full_confidence = (
        dsr is not None
        and dsr >= rules.min_full_deflated_sharpe_probability
        and (
            not rules.require_positive_bootstrap_lcb_for_full
            or (bootstrap_lcb is not None and bootstrap_lcb > 0.0)
        )
    )
    if not hard_failure and score >= rules.min_full_composite_score and full_confidence:
        status = "full_profitability_promotion"
        authorized = True
        provisional = False
        cap = rules.full_active_weight_cap
        reasons.append("net_profitability_and_full_confidence_pass")
    elif not hard_failure and score > rules.min_provisional_composite_score:
        status = "provisional_profitability_promotion"
        authorized = True
        provisional = True
        cap = rules.provisional_active_weight_cap
        if bootstrap_lcb is None or bootstrap_lcb <= 0.0:
            reasons.append("bootstrap_uncertainty_limits_deployment_weight")
        if dsr is None or dsr < rules.min_full_deflated_sharpe_probability:
            reasons.append("deflated_sharpe_limits_deployment_weight")
    elif profitable and score > rules.min_provisional_composite_score:
        status = "shadow_profitability_challenger"
        authorized = False
        provisional = False
        cap = 0.0
    elif not profitable and score > 0.0:
        status = "defensive_overlay_only"
        authorized = False
        provisional = False
        cap = 0.0
    else:
        status = "production_incumbent_retained"
        authorized = False
        provisional = False
        cap = 0.0

    metrics = {
        "profitability_composite_score": score,
        "profitability_component_scores": components,
        "profitability_fold_count": len(folds),
        "profitability_fold_wins": fold_wins,
        "profitability_fold_win_rate": round(fold_win_rate, 6),
        "profitability_candidate_pf_better": bool(
            candidate_pf is not None and incumbent_pf is not None and candidate_pf > incumbent_pf
        ),
        **dict(comparison),
    }
    return ProfitabilityPromotionDecision(
        status=status,
        authorized=authorized,
        provisional=provisional,
        active_weight_cap=cap,
        reason_codes=tuple(dict.fromkeys(reasons)),
        metrics=metrics,
    )


def evaluate_champion_challenger_monitoring(
    comparison: Mapping[str, object],
    *,
    min_live_paired_days: int,
    max_drawdown_deterioration_pct: float,
    max_daily_cvar_deterioration_pct: float,
    policy_hash_consistent: bool,
    contract_activation_authorized: bool = True,
) -> dict[str, object]:
    reasons: list[str] = []
    paired_days = int(finite_float(comparison.get("paired_daily_count")) or 0)
    wealth_delta = finite_float(comparison.get("delta_terminal_wealth"))
    drawdown_delta = finite_float(comparison.get("delta_max_drawdown_pct"))
    cvar_delta = finite_float(comparison.get("delta_daily_cvar_5pct"))
    if not policy_hash_consistent:
        reasons.append("policy_hash_mismatch")
    if not contract_activation_authorized:
        reasons.append("production_contract_not_authorized")
    if paired_days < max(1, int(min_live_paired_days)):
        reasons.append("insufficient_live_paired_days")
    if drawdown_delta is not None and drawdown_delta < -abs(max_drawdown_deterioration_pct):
        reasons.append("live_drawdown_deterioration")
    if cvar_delta is not None and cvar_delta < -abs(max_daily_cvar_deterioration_pct):
        reasons.append("live_cvar_deterioration")
    if wealth_delta is not None and wealth_delta < 0.0:
        reasons.append("live_terminal_wealth_below_champion")
    rollback_reasons = {
        "policy_hash_mismatch",
        "live_drawdown_deterioration",
        "live_cvar_deterioration",
    }
    if not contract_activation_authorized:
        status = "shadow_only_not_activatable"
        action = "do_not_scale"
    elif rollback_reasons.intersection(reasons):
        status = "rollback_to_champion"
        action = "xbi_residual_only"
    elif "insufficient_live_paired_days" in reasons:
        status = "continue_shadow_observation"
        action = "hold_provisional_weight"
    elif "live_terminal_wealth_below_champion" in reasons:
        status = "retain_provisional_weight"
        action = "do_not_scale"
    else:
        status = "eligible_to_scale"
        action = "advance_one_weight_stage"
    return {
        "monitoring_status": status,
        "monitoring_action": action,
        "monitoring_reason_codes": "|".join(reasons or ["monitoring_checks_passed"]),
        "policy_hash_consistent": policy_hash_consistent,
        "contract_activation_authorized": contract_activation_authorized,
        **dict(comparison),
    }
