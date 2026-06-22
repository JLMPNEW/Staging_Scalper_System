"""Shared Stage 5 book utilities for shadow rotation tilts."""
from __future__ import annotations

from collections import defaultdict

import numpy as np

from portfolio_layer.optimizer.optimizer_core import project_to_capped_simplex


def apply_rotation_tilt(
    base_weights: dict[str, float],
    pipeline_by_ticker: dict[str, str],
    multiplier_by_pipeline: dict[str, float],
    *,
    gross: float,
    max_weight: float,
) -> dict[str, float]:
    """Scale a long-only book by sector multipliers, then re-project to gross/caps."""
    tickers = sorted(t for t, w in base_weights.items() if float(w) > 0.0)
    raw = np.array([
        max(0.0, float(base_weights[t]))
        * float(multiplier_by_pipeline.get(pipeline_by_ticker.get(t, ""), 1.0))
        for t in tickers
    ], dtype=float)
    projected = project_to_capped_simplex(raw, gross=float(gross), max_weight=float(max_weight))
    return {t: float(w) for t, w in zip(tickers, projected) if float(w) > 0.0}


def aggregate_by_pipeline(
    weights: dict[str, float],
    pipeline_by_ticker: dict[str, str],
) -> dict[str, float]:
    out: dict[str, float] = defaultdict(float)
    for ticker, weight in weights.items():
        pipeline = pipeline_by_ticker.get(str(ticker).strip().upper(), "")
        out[pipeline] += float(weight)
    return dict(out)
