from __future__ import annotations

import math

GROWTH_MISSING_DEFAULT = 45.0
GROWTH_SEVERE_DECLINE = -0.20
GROWTH_ZERO = 0.0
GROWTH_MODEST = 0.10
GROWTH_STRONG = 0.30
GROWTH_EXCEPTIONAL = 0.75
GROWTH_CURVE_LEGACY = "legacy"
GROWTH_CURVE_SMOOTH_ZERO = "smooth_zero"
GROWTH_CURVES = frozenset({GROWTH_CURVE_LEGACY, GROWTH_CURVE_SMOOTH_ZERO})


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    """Clamp finite numeric scores into a bounded range."""
    if not math.isfinite(value):
        return low
    return max(low, min(high, value))


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

    The current production calibration intentionally separates negative growth
    from positive growth with a discontinuity at zero. Do not smooth these
    thresholds in production without recalibrating scoring and ranking outputs.
    Use curve="smooth_zero" only as a challenger curve.
    """
    if normalize_growth_curve(curve) == GROWTH_CURVE_SMOOTH_ZERO:
        return _score_growth_smooth_zero(growth, default=default)
    return _score_growth_legacy(growth, default=default)
