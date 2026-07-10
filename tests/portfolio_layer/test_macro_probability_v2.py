from __future__ import annotations

# pyright: reportMissingImports=false

import sys
import math
from pathlib import Path

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MACRO_LAYER_ROOT = PROJECT_ROOT / "portfolio_layer" / "MacroLayer"
if str(MACRO_LAYER_ROOT) not in sys.path:
    sys.path.insert(0, str(MACRO_LAYER_ROOT))

from macro_probability_v2 import (  # noqa: E402
    PROBABILITY_V2_SPECS,
    fit_ridge_logistic,
    predict_ridge_logistic,
    regime_probabilities,
    target_period_bounds,
)


def test_ridge_logistic_recovers_multivariate_signal_with_missing_optional_values() -> None:
    generator = np.random.default_rng(1701)
    values = generator.normal(size=(500, 3))
    values[::11, 2] = np.nan
    latent = 1.3 * values[:, 0] - 0.8 * values[:, 1] + generator.normal(scale=0.6, size=500)
    labels = (latent > 0.0).astype(float)
    model = fit_ridge_logistic(
        values,
        labels,
        predictor_names=("growth", "inflation", "optional"),
        ridge_penalty=2.0,
        min_training_samples=100,
        min_positive_samples=20,
        min_negative_samples=20,
    )
    probability = predict_ridge_logistic(model, values, probability_floor=0.02)
    assert model["ready"] is True
    assert np.isfinite(probability).all()
    assert float(np.mean(probability[labels == 1])) > float(np.mean(probability[labels == 0])) + 0.30


def test_ridge_logistic_fails_closed_when_only_one_class_exists() -> None:
    values = np.arange(80, dtype=float).reshape(-1, 1)
    labels = np.ones(80, dtype=float)
    model = fit_ridge_logistic(
        values,
        labels,
        predictor_names=("signal",),
        ridge_penalty=1.0,
        min_training_samples=40,
        min_positive_samples=8,
        min_negative_samples=8,
    )
    probability = predict_ridge_logistic(model, values, probability_floor=0.02)
    assert model["ready"] is False
    assert np.isnan(probability).all()


def test_target_periods_do_not_confuse_now_and_lead() -> None:
    growth_now = target_period_bounds(pd.Timestamp("2026-07-07"), target_kind="growth", target_horizon="now")
    growth_lead = target_period_bounds(pd.Timestamp("2026-07-07"), target_kind="growth", target_horizon="lead")
    inflation_now = target_period_bounds(pd.Timestamp("2026-07-07"), target_kind="inflation", target_horizon="now")
    inflation_lead = target_period_bounds(pd.Timestamp("2026-07-07"), target_kind="inflation", target_horizon="lead")
    assert growth_now == (pd.Timestamp("2026-07-01"), pd.Timestamp("2026-09-30"))
    assert growth_lead == (pd.Timestamp("2026-10-01"), pd.Timestamp("2026-12-31"))
    assert inflation_now == (pd.Timestamp("2026-07-01"), pd.Timestamp("2026-07-31"))
    assert inflation_lead == (pd.Timestamp("2026-08-01"), pd.Timestamp("2026-10-31"))


def test_quadrant_mapping_identifies_stagflation_and_sums_to_one() -> None:
    result = regime_probabilities(0.20, 0.80)
    assert result["regime"] == "STAGFLATION"
    total = sum(float(result[name]) for name in ("expansion_disinflation", "heating_up", "slow_growth", "stagflation"))
    assert abs(total - 1.0) <= 1e-12
    assert math.isclose(float(result["stagflation"]), 0.64)


def test_three_month_inflation_lead_uses_non_overlapping_quarterly_anchors() -> None:
    specs = {spec.probability_key: spec for spec in PROBABILITY_V2_SPECS}
    assert specs["P_PI_NOW_V2"].training_frequency == "monthly"
    assert specs["P_PI_LEAD_V2"].training_frequency == "quarterly"
