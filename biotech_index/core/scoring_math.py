from __future__ import annotations

import math
from typing import Any, Mapping

GROWTH_MISSING_DEFAULT = 45.0
GROWTH_SEVERE_DECLINE = -0.20
GROWTH_ZERO = 0.0
GROWTH_MODEST = 0.10
GROWTH_STRONG = 0.30
GROWTH_EXCEPTIONAL = 0.75
GROWTH_CURVE_LEGACY = "legacy"
GROWTH_CURVE_SMOOTH_ZERO = "smooth_zero"
GROWTH_CURVES = frozenset({GROWTH_CURVE_LEGACY, GROWTH_CURVE_SMOOTH_ZERO})
GROWTH_DRAG_CURVE_LEGACY = "legacy"
GROWTH_DRAG_CURVE_SMOOTH_LINEAR = "smooth_linear"
GROWTH_DRAG_CURVES = frozenset({GROWTH_DRAG_CURVE_LEGACY, GROWTH_DRAG_CURVE_SMOOTH_LINEAR})
RISK_MODE_LEGACY = "legacy"
RISK_MODE_DECOMPOSED = "decomposed"
RISK_MODE_PREDICTIVE = "predictive"
RISK_PENALTY_MODES = frozenset({RISK_MODE_LEGACY, RISK_MODE_DECOMPOSED, RISK_MODE_PREDICTIVE})


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    """Clamp finite numeric scores into a bounded range."""
    if not math.isfinite(value):
        return low
    return max(low, min(high, value))


def convex_risk_drag(
    risk: float,
    weight: float,
    *,
    enabled: bool,
    convexity: float = 0.35,
    inflection: float = 50.0,
) -> float:
    """Shared convex risk penalty used by production and support scorers."""
    if not math.isfinite(risk):
        risk = 100.0
    if not math.isfinite(weight) or weight < 0.0:
        raise ValueError(f"risk penalty weight must be finite and >= 0, got {weight}")
    base_drag = weight * risk
    if not enabled:
        return base_drag
    if not math.isfinite(convexity) or convexity < 0.0:
        raise ValueError(f"risk penalty convexity must be finite and >= 0, got {convexity}")
    if not math.isfinite(inflection) or inflection >= 100.0:
        raise ValueError(f"risk penalty inflection must be finite and < 100.0, got {inflection}")
    excess = max(0.0, min(1.0, (risk - inflection) / max(1e-9, 100.0 - inflection)))
    return base_drag * (1.0 + convexity * excess)


def decomposed_risk_penalty_input(
    *,
    structural_risk: float,
    compensated_risk: float,
    compensated_free_band: float = 60.0,
    compensated_weight: float = 0.20,
) -> float:
    """Risk input for penalties when compensated and structural risks are split."""
    structural = clamp(structural_risk)
    compensated = clamp(compensated_risk)
    free_band = clamp(compensated_free_band)
    weight = max(0.0, min(1.0, compensated_weight))
    return clamp(structural + weight * max(0.0, compensated - free_band))


def weighted_predictive_risk_penalty_input(
    components: dict[str, float],
    weights: dict[str, float],
    *,
    free_bands: dict[str, float] | None = None,
    caps: dict[str, float] | None = None,
) -> float:
    """Penalty-only risk score from empirically validated components."""
    free_bands = free_bands or {}
    caps = caps or {}
    total_weight = sum(max(0.0, float(weight)) for weight in weights.values())
    if total_weight <= 0.0:
        return 0.0
    total = 0.0
    for key, weight in weights.items():
        clean_weight = max(0.0, float(weight))
        if clean_weight <= 0.0:
            continue
        value = clamp(float(components.get(key, 0.0)))
        free_band = clamp(float(free_bands.get(key, 0.0)))
        cap = clamp(float(caps.get(key, 100.0)))
        charged_value = min(cap, max(0.0, value - free_band))
        normalized_tail = clamp(100.0 * charged_value / max(1e-9, 100.0 - free_band))
        total += clean_weight * normalized_tail
    return clamp(total / total_weight)


def normalize_risk_penalty_mode(raw: object, *, default: str = RISK_MODE_LEGACY) -> str:
    """Normalize risk modes used by production, discovery, and calibration."""
    fallback = default if default in RISK_PENALTY_MODES else RISK_MODE_LEGACY
    value = str(raw or fallback).strip().lower().replace("-", "_")
    aliases = {
        "raw": RISK_MODE_LEGACY,
        "prod": RISK_MODE_LEGACY,
        "production": RISK_MODE_LEGACY,
        "structural": RISK_MODE_DECOMPOSED,
        "risk_penalty_input": RISK_MODE_DECOMPOSED,
        "predictive_penalty": RISK_MODE_PREDICTIVE,
        "predictive_risk": RISK_MODE_PREDICTIVE,
    }
    value = aliases.get(value, value)
    return value if value in RISK_PENALTY_MODES else fallback


def _float_or_default(raw: object, default: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def risk_score_from_components(
    risk_components: Mapping[str, Any],
    *,
    legacy_risk: float,
    mode: str,
) -> float:
    """Return the penalty risk score for a configured mode."""
    clean_mode = normalize_risk_penalty_mode(mode)
    legacy = clamp(_float_or_default(legacy_risk, 100.0))
    if clean_mode == RISK_MODE_PREDICTIVE:
        return clamp(_float_or_default(risk_components.get("predictive_risk_penalty_input_score"), legacy))
    if clean_mode == RISK_MODE_DECOMPOSED:
        return clamp(_float_or_default(risk_components.get("risk_penalty_input_score"), legacy))
    return legacy


def normalize_growth_curve(raw: object) -> str:
    """Normalize configured growth scoring curve names."""
    value = str(raw or GROWTH_CURVE_LEGACY).strip().lower().replace("-", "_")
    if value in {"smooth", "continuous", "continuous_zero"}:
        return GROWTH_CURVE_SMOOTH_ZERO
    if value in GROWTH_CURVES:
        return value
    return GROWTH_CURVE_LEGACY


def _score_growth_legacy(growth: float | None, *, default: float) -> float:
    if growth is None:
        return default
    if not math.isfinite(growth):
        return default
    if growth < GROWTH_SEVERE_DECLINE:
        return 20.0
    if growth < GROWTH_ZERO:
        return 35.0 + growth * 75.0
    if growth < GROWTH_MODEST:
        return 50.0 + growth * 150.0
    if growth < GROWTH_STRONG:
        return 65.0 + (growth - GROWTH_MODEST) * 100.0
    if growth < GROWTH_EXCEPTIONAL:
        return 85.0 + (growth - GROWTH_STRONG) * 25.0
    return 100.0


def _score_growth_smooth_zero(growth: float | None, *, default: float) -> float:
    """Challenger curve that removes zero-growth and exceptional-growth jumps."""
    if growth is None:
        return default
    if not math.isfinite(growth):
        return default
    if growth < GROWTH_SEVERE_DECLINE:
        return 20.0
    if growth < GROWTH_MODEST:
        return 20.0 + (growth - GROWTH_SEVERE_DECLINE) * (45.0 / (GROWTH_MODEST - GROWTH_SEVERE_DECLINE))
    if growth < GROWTH_STRONG:
        return 65.0 + (growth - GROWTH_MODEST) * 100.0
    if growth < GROWTH_EXCEPTIONAL:
        return 85.0 + (growth - GROWTH_STRONG) * (15.0 / (GROWTH_EXCEPTIONAL - GROWTH_STRONG))
    return 100.0


def score_growth(
    growth: float | None,
    *,
    default: float = GROWTH_MISSING_DEFAULT,
    curve: str = GROWTH_CURVE_LEGACY,
) -> float:
    """Shared trailing/forward growth score on a 0-100 scale.

    The legacy curve is kept for backward-compatible historical comparisons.
    Production scripts should pass the configured curve explicitly; current
    biotech config uses ``smooth_zero`` for both commercial and forward growth.
    """
    if normalize_growth_curve(curve) == GROWTH_CURVE_SMOOTH_ZERO:
        return _score_growth_smooth_zero(growth, default=default)
    return _score_growth_legacy(growth, default=default)


def normalize_pct(raw: object, default: float | None = None) -> float | None:
    """Normalize vendor percentage inputs to decimal form."""
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value):
        return default
    if abs(value) > 2.0:
        return value / 100.0
    return value


def _linear_score(value: float, points: list[tuple[float, float]]) -> float:
    ordered = sorted(points)
    if value <= ordered[0][0]:
        return clamp(ordered[0][1])
    if value >= ordered[-1][0]:
        return clamp(ordered[-1][1])
    for (left_x, left_y), (right_x, right_y) in zip(ordered, ordered[1:]):
        if left_x <= value <= right_x:
            span = max(1e-12, right_x - left_x)
            return clamp(left_y + (right_y - left_y) * (value - left_x) / span)
    return clamp(ordered[-1][1])


def score_commercial_entry_quality(
    *,
    distance_from_52w_high_pct: object = None,
    price_vs_200d_pct: object = None,
    return_3m_pct: object = None,
    relative_strength_3m_vs_xbi: object = None,
) -> float:
    """Commercial entry-quality score where higher is better.

    The target setup is an investable pullback: not chasing highs, not a broken
    falling knife, and still showing enough relative strength to avoid low-growth
    value traps.
    """
    dist_high = normalize_pct(distance_from_52w_high_pct)
    price_200d = normalize_pct(price_vs_200d_pct)
    ret_3m = normalize_pct(return_3m_pct)
    rel_strength = normalize_pct(relative_strength_3m_vs_xbi)
    dist_score = (
        55.0
        if dist_high is None
        else _linear_score(
            dist_high,
            [(-0.75, 20.0), (-0.45, 45.0), (-0.30, 88.0), (-0.15, 82.0), (-0.05, 48.0), (0.00, 28.0), (0.10, 15.0)],
        )
    )
    ma_score = (
        55.0
        if price_200d is None
        else _linear_score(
            price_200d,
            [(-0.60, 20.0), (-0.30, 42.0), (-0.10, 70.0), (0.05, 84.0), (0.25, 72.0), (0.45, 35.0), (0.70, 15.0)],
        )
    )
    ret_score = (
        55.0
        if ret_3m is None
        else _linear_score(
            ret_3m,
            [(-0.55, 22.0), (-0.25, 45.0), (-0.05, 68.0), (0.12, 82.0), (0.30, 64.0), (0.55, 30.0), (0.85, 12.0)],
        )
    )
    rs_score = (
        55.0
        if rel_strength is None
        else _linear_score(
            rel_strength,
            [(-0.40, 25.0), (-0.15, 45.0), (0.00, 62.0), (0.15, 78.0), (0.35, 82.0), (0.65, 55.0), (1.00, 30.0)],
        )
    )
    return clamp(0.34 * dist_score + 0.28 * ma_score + 0.20 * ret_score + 0.18 * rs_score)


def score_commercial_overextension(
    *,
    distance_from_52w_high_pct: object = None,
    price_vs_200d_pct: object = None,
    return_3m_pct: object = None,
    valuation_growth_mismatch_score: object = None,
    mature_defensive_score: object = None,
) -> float:
    """Commercial overextension score where higher is worse."""
    dist_high = normalize_pct(distance_from_52w_high_pct)
    price_200d = normalize_pct(price_vs_200d_pct)
    ret_3m = normalize_pct(return_3m_pct)
    near_high = 0.0 if dist_high is None else _linear_score(dist_high, [(-0.20, 0.0), (-0.08, 35.0), (-0.02, 75.0), (0.05, 100.0)])
    ma_extension = 0.0 if price_200d is None else _linear_score(price_200d, [(0.10, 0.0), (0.25, 45.0), (0.45, 85.0), (0.70, 100.0)])
    short_squeeze = 0.0 if ret_3m is None else _linear_score(ret_3m, [(0.10, 0.0), (0.30, 35.0), (0.55, 75.0), (0.85, 100.0)])
    valuation_mismatch = clamp(_float_or_default(valuation_growth_mismatch_score, 0.0))
    mature_defensive = clamp(_float_or_default(mature_defensive_score, 0.0))
    return clamp(0.28 * near_high + 0.24 * ma_extension + 0.20 * short_squeeze + 0.18 * valuation_mismatch + 0.10 * mature_defensive)


def score_valuation_growth_fit(
    *,
    quality_adjusted_valuation_score: object,
    forward_revenue_growth_pct: object = None,
    revenue_yoy_growth_pct: object = None,
    forward_ebitda_margin_pct: object = None,
) -> float:
    """Score whether valuation is justified by commercial growth/profitability."""
    qval = clamp(_float_or_default(quality_adjusted_valuation_score, 50.0))
    fwd_growth = normalize_pct(forward_revenue_growth_pct)
    trailing_growth = normalize_pct(revenue_yoy_growth_pct)
    growth_score = score_growth(fwd_growth if fwd_growth is not None else trailing_growth, default=45.0, curve=GROWTH_CURVE_SMOOTH_ZERO)
    ebitda_margin = normalize_pct(forward_ebitda_margin_pct)
    margin_score = (
        55.0
        if ebitda_margin is None
        else _linear_score(ebitda_margin, [(-0.10, 20.0), (0.00, 38.0), (0.10, 60.0), (0.25, 78.0), (0.40, 90.0)])
    )
    return clamp(0.44 * qval + 0.38 * growth_score + 0.18 * margin_score)


def score_commercial_expected_return_overlay(
    *,
    commercial: Mapping[str, Any],
    forward_guidance: Mapping[str, Any],
    momentum_score: float,
    risk_score: float,
    mature_defensive_score: float,
) -> dict[str, float]:
    """Composite shadow overlay for commercial profitable-growth names."""
    entry_quality = score_commercial_entry_quality(
        distance_from_52w_high_pct=commercial.get("distance_from_52w_high_pct"),
        price_vs_200d_pct=commercial.get("price_vs_200d_pct"),
        return_3m_pct=commercial.get("return_3m_pct"),
        relative_strength_3m_vs_xbi=commercial.get("relative_strength_3m_vs_xbi"),
    )
    valuation_growth_fit = score_valuation_growth_fit(
        quality_adjusted_valuation_score=commercial.get("quality_adjusted_valuation_score", commercial.get("valuation_score", 50.0)),
        forward_revenue_growth_pct=forward_guidance.get("forward_revenue_growth_pct"),
        revenue_yoy_growth_pct=commercial.get("revenue_yoy_growth_pct"),
        forward_ebitda_margin_pct=forward_guidance.get("forward_ebitda_margin_pct"),
    )
    overextension = score_commercial_overextension(
        distance_from_52w_high_pct=commercial.get("distance_from_52w_high_pct"),
        price_vs_200d_pct=commercial.get("price_vs_200d_pct"),
        return_3m_pct=commercial.get("return_3m_pct"),
        valuation_growth_mismatch_score=commercial.get("valuation_growth_mismatch_score", 0.0),
        mature_defensive_score=mature_defensive_score,
    )
    value_trap = clamp(_float_or_default(commercial.get("value_trap_score"), 0.0))
    leverage = clamp(_float_or_default(commercial.get("leverage_score"), 50.0))
    guidance = clamp(
        _float_or_default(
            forward_guidance.get("quality_adjusted_guidance_score", forward_guidance.get("guidance_score")),
            35.0,
        )
    )
    institutional_upside = clamp(
        _float_or_default(
            commercial.get("institutional_upside_capacity_score", commercial.get("upside_capacity_score")),
            50.0,
        )
    )
    commercial_value = clamp(_float_or_default(commercial.get("commercial_value_score"), 35.0))
    guidance_recency_penalty = clamp(_float_or_default(forward_guidance.get("guidance_recency_penalty"), 0.0))
    score = (
        0.20 * guidance
        + 0.18 * valuation_growth_fit
        + 0.16 * institutional_upside
        + 0.14 * commercial_value
        + 0.14 * entry_quality
        + 0.08 * clamp(momentum_score)
        + 0.06 * leverage
        + 0.04 * (100.0 - clamp(risk_score))
        - 0.12 * value_trap
        - 0.10 * clamp(mature_defensive_score)
        - 0.10 * overextension
        - 0.05 * guidance_recency_penalty
    )
    return {
        "commercial_entry_quality_score": round(clamp(entry_quality), 4),
        "commercial_overextension_score": round(clamp(overextension), 4),
        "valuation_growth_fit_score": round(clamp(valuation_growth_fit), 4),
        "commercial_expected_return_overlay_score": round(clamp(score), 4),
    }


def normalize_growth_drag_curve(raw: object) -> str:
    """Normalize configured growth-drag curve names."""
    curve = str(raw or GROWTH_DRAG_CURVE_LEGACY).strip().lower().replace("-", "_")
    if not curve:
        return GROWTH_DRAG_CURVE_LEGACY
    if curve == "linear":
        return GROWTH_DRAG_CURVE_SMOOTH_LINEAR
    if curve in GROWTH_DRAG_CURVES:
        return curve
    raise ValueError(
        f"Unsupported growth drag curve '{raw}'. Expected one of: {','.join(sorted(GROWTH_DRAG_CURVES))}"
    )


def _interpolate_piecewise(value: float, points: list[tuple[float, float]]) -> float:
    if not points:
        raise ValueError("interpolate_piecewise requires at least one point")
    ordered = sorted(points)
    if value <= ordered[0][0]:
        return ordered[0][1]
    if value >= ordered[-1][0]:
        return ordered[-1][1]
    for (left_x, left_y), (right_x, right_y) in zip(ordered, ordered[1:]):
        if left_x <= value <= right_x:
            span = right_x - left_x
            if abs(span) <= 1e-12:
                return right_y
            ratio = (value - left_x) / span
            return left_y + ratio * (right_y - left_y)
    return ordered[-1][1]


def score_growth_drag(*growth_values: object, curve: str = GROWTH_DRAG_CURVE_LEGACY) -> float:
    """Score mature-company growth drag on a 0-100 scale where higher is worse."""
    parsed: list[float] = []
    for raw in growth_values:
        try:
            value = float(str(raw).strip())
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            parsed.append(value)
    if not parsed:
        return 50.0
    best_growth = max(parsed)
    if normalize_growth_drag_curve(curve) == GROWTH_DRAG_CURVE_SMOOTH_LINEAR:
        return clamp(
            _interpolate_piecewise(
                best_growth,
                [
                    (-0.20, 100.0),
                    (-0.10, 75.0),
                    (0.0, 50.0),
                    (0.10, 25.0),
                    (0.20, 0.0),
                ],
            )
        )
    if best_growth >= 0.20:
        return 0.0
    if best_growth >= 0.10:
        return 25.0
    if best_growth >= 0.0:
        return 50.0
    if best_growth >= -0.10:
        return 75.0
    return 100.0
