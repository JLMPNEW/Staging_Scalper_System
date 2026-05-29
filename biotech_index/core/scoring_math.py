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
GROWTH_DRAG_CURVE_LEGACY = "legacy"
GROWTH_DRAG_CURVE_SMOOTH_LINEAR = "smooth_linear"
GROWTH_DRAG_CURVES = frozenset({GROWTH_DRAG_CURVE_LEGACY, GROWTH_DRAG_CURVE_SMOOTH_LINEAR})


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
