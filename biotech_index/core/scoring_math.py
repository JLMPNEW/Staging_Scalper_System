from __future__ import annotations

import math


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    if not math.isfinite(value):
        return low
    return max(low, min(high, value))


def score_growth(growth: float | None, *, default: float = 45.0) -> float:
    """Shared trailing/forward growth score on a 0-100 scale."""
    if growth is None:
        return default
    if growth < -0.20:
        return 20.0
    if growth < 0.0:
        return 35.0 + growth * 75.0
    if growth < 0.10:
        return 50.0 + growth * 150.0
    if growth < 0.30:
        return 65.0 + (growth - 0.10) * 100.0
    if growth < 0.75:
        return 85.0 + (growth - 0.30) * 25.0
    return 100.0
