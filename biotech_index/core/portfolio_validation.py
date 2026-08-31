from __future__ import annotations

from typing import Any, Mapping

from biotech_index.core.calibration_metrics import finite_float


ValidationRuleContract = Any


def _pair(metrics: Mapping[str, object], key: str) -> tuple[float, float] | None:
    candidate = finite_float(metrics.get(f"candidate_{key}"))
    incumbent = finite_float(metrics.get(f"incumbent_{key}"))
    if candidate is None or incumbent is None:
        return None
    return candidate, incumbent


def validation_candidate_survives_multimetric(
    metrics: Mapping[str, object],
    rules: ValidationRuleContract,
) -> bool:
    """Screen validation challengers without treating one uncertain statistic as a veto.

    This is a shortlist gate only. It requires adequate support, investable breadth,
    profit-factor support, and explicit tail no-harm checks. Relative return, LCB,
    profit factor, hit rate, and tail metrics then vote as a balanced scorecard. The
    untouched daily net-of-cost replay remains the final promotion authority.
    """

    paired_dates = int(finite_float(metrics.get("paired_date_count")) or 0)
    active_dates = int(finite_float(metrics.get("active_date_count")) or 0)
    evaluation_dates = int(finite_float(metrics.get("evaluation_date_count")) or 0)
    active_coverage = 100.0 * active_dates / evaluation_dates if evaluation_dates > 0 else 0.0
    top3 = finite_float(metrics.get("candidate_top3_gain_contribution_pct"))
    if (
        paired_dates < rules.min_paired_dates
        or active_coverage < rules.min_active_date_coverage_pct
        or top3 is None
        or top3 > rules.max_top3_contribution_pct
    ):
        return False

    profit_factor_pair = _pair(metrics, "profit_factor")
    if profit_factor_pair is None or profit_factor_pair[0] < rules.prefer_profit_factor_at_least:
        return False
    candidate_pf, incumbent_pf = profit_factor_pair
    robust_values = [
        finite_float(metrics.get(field))
        for field in (
            "candidate_winsorized_profit_factor",
            "candidate_profit_factor_ex_largest_winner",
            "candidate_profit_factor_ex_top3_winners",
        )
    ]
    robust_supported = sum(
        value is not None and value >= rules.min_robust_profit_factor for value in robust_values
    )
    if rules.require_robust_profit_factor_support and robust_supported < 2:
        return False

    loss20 = _pair(metrics, "loss20_rate_pct")
    loss40 = _pair(metrics, "loss40_rate_pct")
    cvar = _pair(metrics, "cvar_return_pct")
    drawdown = _pair(metrics, "max_drawdown_pct")
    if loss20 is None or loss40 is None or cvar is None or drawdown is None:
        return False
    if (
        loss20[0] > loss20[1] + rules.max_loss20_deterioration_pct
        or loss40[0] > loss40[1] + rules.max_loss40_deterioration_pct
        or cvar[0] < cvar[1] - abs(rules.max_cvar_deterioration_pct)
        or drawdown[0] < drawdown[1] - abs(rules.max_drawdown_deterioration_pct)
    ):
        return False

    mean_return = _pair(metrics, "mean_return_pct")
    lcb = _pair(metrics, "lcb_return_pct")
    hit_rate = _pair(metrics, "hit_rate_pct")
    if mean_return is None or lcb is None or hit_rate is None:
        return False
    improvements = (
        int(mean_return[0] > mean_return[1])
        + int(lcb[0] > lcb[1])
        + int(hit_rate[0] > hit_rate[1])
        + int(candidate_pf > incumbent_pf)
        + int(loss20[0] <= loss20[1])
        + int(loss40[0] <= loss40[1])
        + int(cvar[0] >= cvar[1])
        + int(drawdown[0] >= drawdown[1])
    )
    directional_anchor = (
        mean_return[0] > mean_return[1]
        or lcb[0] > lcb[1]
        or candidate_pf > incumbent_pf
    )
    return bool(directional_anchor and improvements >= 4)

