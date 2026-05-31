"""Reusable helpers for Tier-1 scoring experiments.

The production daily scorer uses the current configured composite model. This
module keeps higher-order scoring utilities isolated so IC-weighted experiments
can be tested without changing the live score contract.
"""
from __future__ import annotations

import logging
import math
from statistics import mean, stdev


LOGGER = logging.getLogger(__name__)


def _finite_float(raw: object) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def winsorize(
    values: list[tuple[int, float]],
    *,
    low_pct: float = 0.05,
    high_pct: float = 0.95,
) -> list[tuple[int, float]]:
    """Clip ``(id, value)`` pairs at nearest-rank quantile bounds."""
    if not 0.0 <= low_pct < high_pct <= 1.0:
        raise ValueError(f"low_pct ({low_pct}) must be < high_pct ({high_pct}), both in [0, 1]")
    values = [(idx, value) for idx, raw_value in values if (value := _finite_float(raw_value)) is not None]
    if len(values) < 4:
        return list(values)
    sorted_values = sorted(value for _, value in values)
    low_bound = sorted_values[max(0, min(len(sorted_values) - 1, math.ceil(low_pct * len(sorted_values)) - 1))]
    high_bound = sorted_values[max(0, min(len(sorted_values) - 1, math.ceil(high_pct * len(sorted_values)) - 1))]
    if low_bound > high_bound:
        low_bound, high_bound = high_bound, low_bound
    return [(idx, max(low_bound, min(high_bound, value))) for idx, value in values]


def zscore_normalize(
    values: list[tuple[int, float]],
    *,
    clip_sigma: float = 3.0,
    winsor_low_pct: float = 0.05,
    winsor_high_pct: float = 0.95,
) -> dict[int, float]:
    """Winsorize, z-score, clip, and rescale to 0-100."""
    values = [(idx, value) for idx, raw_value in values if (value := _finite_float(raw_value)) is not None]
    if not values:
        return {}
    if len(values) == 1:
        return {values[0][0]: 50.0}
    winsorized = winsorize(values, low_pct=winsor_low_pct, high_pct=winsor_high_pct)
    raw = [value for _, value in winsorized]
    avg = mean(raw)
    sigma = stdev(raw) if len(raw) >= 2 else 0.0
    if sigma < 1e-12:
        return {idx: 50.0 for idx, _ in values}
    out: dict[int, float] = {}
    for idx, value in winsorized:
        z_value = max(-clip_sigma, min(clip_sigma, (value - avg) / sigma))
        out[idx] = round(max(0.0, min(100.0, 50.0 + (z_value / clip_sigma) * 50.0)), 2)
    return out


def _fractional_rank(values: list[float]) -> list[float]:
    indexed = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    idx = 0
    while idx < len(indexed):
        end = idx
        while end < len(indexed) - 1 and indexed[end + 1][1] == indexed[idx][1]:
            end += 1
        avg_rank = (idx + end) / 2.0 + 1.0
        for rank_idx in range(idx, end + 1):
            ranks[indexed[rank_idx][0]] = avg_rank
        idx = end + 1
    return ranks


def compute_factor_ic(factor_scores: dict[int, float], forward_returns: dict[int, float]) -> float | None:
    """Return Spearman rank IC for overlapping factor scores and forward returns."""
    ids = sorted(
        item
        for item in set(factor_scores) & set(forward_returns)
        if _finite_float(factor_scores[item]) is not None and _finite_float(forward_returns[item]) is not None
    )
    if len(ids) < 5:
        LOGGER.debug("compute_factor_ic: only %d overlapping observations", len(ids))
        return None
    factor_ranks = _fractional_rank([float(factor_scores[item]) for item in ids])
    return_ranks = _fractional_rank([float(forward_returns[item]) for item in ids])
    factor_avg = mean(factor_ranks)
    return_avg = mean(return_ranks)
    covariance = sum((a - factor_avg) * (b - return_avg) for a, b in zip(factor_ranks, return_ranks, strict=True)) / len(ids)
    factor_std = math.sqrt(sum((value - factor_avg) ** 2 for value in factor_ranks) / len(ids))
    return_std = math.sqrt(sum((value - return_avg) ** 2 for value in return_ranks) / len(ids))
    if factor_std < 1e-12 or return_std < 1e-12:
        return 0.0
    return max(-1.0, min(1.0, covariance / (factor_std * return_std)))


def blended_score(
    component_scores: dict[str, float],
    component_available: dict[str, bool],
    *,
    config_weights: dict[str, float],
    historical_ics: dict[str, float] | None = None,
    ic_min_absolute: float = 0.05,
    ic_blend_fraction: float = 0.50,
    neutral: float = 50.0,
) -> float:
    """Blend available component scores with optional positive-IC weight tilt."""
    active = [
        key
        for key, available in component_available.items()
        if available
        and key in component_scores
        and key in config_weights
        and _finite_float(component_scores[key]) is not None
        and _finite_float(config_weights[key]) is not None
    ]
    if not active:
        return neutral

    use_ic = historical_ics is not None and any(abs(historical_ics.get(key, 0.0)) >= ic_min_absolute for key in active)
    positive_ic: dict[str, float] = {}
    total_positive_ic = 0.0
    if use_ic and historical_ics is not None:
        positive_ic = {
            key: max(0.0, historical_ics.get(key, 0.0))
            for key in active
            if abs(historical_ics.get(key, 0.0)) >= ic_min_absolute
        }
        total_positive_ic = sum(positive_ic.values())
        use_ic = total_positive_ic > 1e-12

    total_config_weight = sum(float(config_weights[key]) for key in active)
    if total_config_weight <= 1e-12:
        return neutral

    effective_weights: dict[str, float] = {}
    for key in active:
        config_share = float(config_weights[key]) / total_config_weight
        if use_ic:
            ic_share = positive_ic.get(key, 0.0) / total_positive_ic
            effective_weights[key] = (1.0 - ic_blend_fraction) * config_share + ic_blend_fraction * ic_share
        else:
            effective_weights[key] = config_share

    total_effective_weight = sum(effective_weights.values())
    if total_effective_weight <= 1e-12:
        return neutral
    raw = sum(float(component_scores[key]) * effective_weights[key] for key in active) / total_effective_weight
    return max(0.0, min(100.0, raw))


def classify_with_conviction(
    composite_score: float,
    component_scores: dict[str, float],
    component_available: dict[str, bool],
    *,
    gates: dict[str, float],
    hard_red_flag: bool = False,
    value_trap_score: float = 0.0,
) -> tuple[str, str]:
    """Classify a score row and return a simple High/Medium/Low conviction label."""
    composite_score = _finite_float(composite_score) or 0.0
    value_trap_score = _finite_float(value_trap_score) or 0.0
    component_scores = {
        key: value
        for key, raw_value in component_scores.items()
        if (value := _finite_float(raw_value)) is not None
    }
    fail: list[str] = []
    if composite_score < gates.get("composite_min", 70.0):
        fail.append("composite")
    if component_scores.get("fundamental_quality", 0.0) < gates.get("fundamental_quality_min", 60.0):
        fail.append("fundamental")
    if component_available.get("fda_product", False) and component_scores.get("fda_product", 50.0) < gates.get("fda_product_min", 50.0):
        fail.append("fda")
    if component_scores.get("reimbursement", 50.0) < gates.get("reimbursement_min", 45.0):
        fail.append("reimbursement")
    if component_scores.get("valuation", 50.0) < gates.get("valuation_min", 55.0):
        fail.append("valuation")
    if component_scores.get("technical_entry", 50.0) < gates.get("technical_entry_min", 50.0):
        fail.append("technical")
    if value_trap_score >= gates.get("value_trap_hard_max", 85.0):
        fail.append("value_trap_hard")
    elif value_trap_score >= gates.get("value_trap_max", 40.0):
        fail.append("value_trap_soft")
    if hard_red_flag:
        fail.append("hard_red")

    if hard_red_flag or "fda" in fail:
        classification = "manual_review_regulatory_risk"
    elif not fail:
        classification = "tier_1_long_candidate"
    elif component_scores.get("fundamental_quality", 0.0) >= 75.0 and "valuation" in fail:
        classification = "quality_watchlist_wait_for_price"
    elif component_scores.get("valuation", 50.0) >= 70.0 and "fundamental" in fail:
        classification = "cheap_but_needs_proof"
    elif composite_score >= 60.0:
        classification = "watchlist"
    else:
        classification = "avoid"

    keys = list(component_available)
    live_scores = [component_scores[key] for key in keys if component_available.get(key) and key in component_scores]
    completeness = len(live_scores) / len(keys) if keys else 0.0
    if len(live_scores) >= 2:
        live_avg = sum(live_scores) / len(live_scores)
        dispersion = math.sqrt(sum((score - live_avg) ** 2 for score in live_scores) / len(live_scores))
    else:
        dispersion = 999.0

    if classification in {"manual_review_regulatory_risk", "avoid"}:
        conviction = "Low"
    elif completeness >= 0.85 and dispersion <= 12.0:
        conviction = "High"
    elif completeness >= 0.60 and dispersion <= 22.0:
        conviction = "Medium"
    else:
        conviction = "Low"
    return classification, conviction
