from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd


MODEL_VERSION_DEFAULT = "macro_regime_v2_independent_outcomes_v1"
REGIME_ORDER = (
    "EXPANSION_DISINFLATION",
    "HEATING_UP",
    "SLOW_GROWTH",
    "STAGFLATION",
)


@dataclass(frozen=True)
class ProbabilityV2Spec:
    probability_key: str
    target_kind: str
    target_horizon: str
    predictor_names: tuple[str, ...]
    mandatory_predictors: tuple[str, ...]
    training_frequency: str


GROWTH_PREDICTORS = (
    "G_NOW",
    "G_LEAD",
    "gdp_growth_latest",
    "growth_activity",
    "financial_conditions",
    "policy_tightness",
    "SHOCK",
)
INFLATION_PREDICTORS = (
    "PI_NOW",
    "PI_LEAD",
    "inflation_level_yoy",
    "core_inflation",
    "headline_inflation",
    "inflation_expectations",
    "energy_shock",
    "energy_yoy",
    "policy_tightness",
)

PROBABILITY_V2_SPECS = (
    ProbabilityV2Spec("P_G_NOW_V2", "growth", "now", GROWTH_PREDICTORS, ("G_NOW", "G_LEAD"), "quarterly"),
    ProbabilityV2Spec("P_G_LEAD_V2", "growth", "lead", GROWTH_PREDICTORS, ("G_NOW", "G_LEAD"), "quarterly"),
    ProbabilityV2Spec(
        "P_PI_NOW_V2",
        "inflation",
        "now",
        INFLATION_PREDICTORS,
        ("PI_NOW", "PI_LEAD", "inflation_level_yoy"),
        "monthly",
    ),
    ProbabilityV2Spec(
        "P_PI_LEAD_V2",
        "inflation",
        "lead",
        INFLATION_PREDICTORS,
        ("PI_NOW", "PI_LEAD", "inflation_level_yoy"),
        "quarterly",
    ),
)

# --- V2.1 candidate variant (frozen: V2_1_CANDIDATE_SPEC.md) ------------------------------
# Same architecture, targets, labels, probability keys, and thresholds as V2. Deltas only:
# G_LEAD -> G_LEAD_V21 (NFCI/ANFCI replaced by pre-registered market-stress components),
# PI_NOW -> PI_NOW_V21 (all-employee AHE replaced by AHETPI prod/nonsup AHE, no splicing),
# and the derived financial_conditions predictor swaps its NFCI leg for the stress pair.

MODEL_VERSION_V21 = "macro_regime_v2_1_independent_outcomes_v1"
MODEL_VERSION_V22 = "macro_regime_v2_2_recalibrated_v1"
MODEL_VERSION_V23 = "macro_regime_v2_3_conditional_recal_v1"

GROWTH_PREDICTORS_V21 = (
    "G_NOW",
    "G_LEAD_V21",
    "gdp_growth_latest",
    "growth_activity",
    "financial_conditions",
    "policy_tightness",
    "SHOCK",
)
INFLATION_PREDICTORS_V21 = (
    "PI_NOW_V21",
    "PI_LEAD",
    "inflation_level_yoy",
    "core_inflation",
    "headline_inflation",
    "inflation_expectations",
    "energy_shock",
    "energy_yoy",
    "policy_tightness",
)

PROBABILITY_V2_1_SPECS = (
    ProbabilityV2Spec("P_G_NOW_V2", "growth", "now", GROWTH_PREDICTORS_V21, ("G_NOW", "G_LEAD_V21"), "quarterly"),
    ProbabilityV2Spec("P_G_LEAD_V2", "growth", "lead", GROWTH_PREDICTORS_V21, ("G_NOW", "G_LEAD_V21"), "quarterly"),
    ProbabilityV2Spec(
        "P_PI_NOW_V2",
        "inflation",
        "now",
        INFLATION_PREDICTORS_V21,
        ("PI_NOW_V21", "PI_LEAD", "inflation_level_yoy"),
        "monthly",
    ),
    ProbabilityV2Spec(
        "P_PI_LEAD_V2",
        "inflation",
        "lead",
        INFLATION_PREDICTORS_V21,
        ("PI_NOW_V21", "PI_LEAD", "inflation_level_yoy"),
        "quarterly",
    ),
)


@dataclass(frozen=True)
class ProbabilityV2Variant:
    model_version: str
    specs: tuple[ProbabilityV2Spec, ...]
    composite_keys: tuple[str, ...]
    feature_metrics: tuple[str, ...]
    financial_conditions_metrics: tuple[str, ...]
    # V2.2+ (V2_2_CANDIDATE_SPEC.md): trailing PIT recalibration of walk-forward probabilities.
    recalibrate: bool = False
    recalibration_min_pairs: int = 20
    recalibration_min_positive: int = 5
    recalibration_min_negative: int = 5
    # V2.3 (V2_3_CANDIDATE_SPEC.md): "always" applies the cell's own trailing line
    # unconditionally; "conditional_pooled" triggers on the cell's own trailing slope being
    # outside the gate band and applies a line fitted on the cell's pool.
    recalibration_policy: str = "always"
    recalibration_pools: tuple[tuple[str, str], ...] = ()


_V2_FEATURE_METRICS = (
    "us_ads_index",
    "us_cfnai_ma3",
    "us_nonfarm_payrolls",
    "us_initial_claims",
    "us_hy_oas",
    "us_nfci",
    "us_effective_fed_funds",
    "us_10y_real_yield",
    "us_core_cpi",
    "us_core_pce",
    "us_headline_cpi",
    "us_headline_pce",
    "us_5y_breakeven",
    "us_wti_spot",
    "us_brent_spot",
)

PROBABILITY_V2_VARIANTS: dict[str, ProbabilityV2Variant] = {
    MODEL_VERSION_DEFAULT: ProbabilityV2Variant(
        model_version=MODEL_VERSION_DEFAULT,
        specs=PROBABILITY_V2_SPECS,
        composite_keys=("G_NOW", "G_LEAD", "PI_NOW", "PI_LEAD", "SHOCK"),
        feature_metrics=_V2_FEATURE_METRICS,
        financial_conditions_metrics=("us_hy_oas", "us_nfci"),
    ),
    MODEL_VERSION_V21: ProbabilityV2Variant(
        model_version=MODEL_VERSION_V21,
        specs=PROBABILITY_V2_1_SPECS,
        composite_keys=("G_NOW", "G_LEAD_V21", "PI_NOW_V21", "PI_LEAD", "SHOCK"),
        feature_metrics=tuple(
            metric for metric in _V2_FEATURE_METRICS if metric != "us_nfci"
        ) + ("us_ig_spread_baa10y", "us_equity_vol"),
        financial_conditions_metrics=("us_hy_oas", "us_ig_spread_baa10y", "us_equity_vol"),
    ),
    # V2.2 = V2.1 inputs + trailing PIT recalibration (V2_2_CANDIDATE_SPEC.md). No other delta.
    MODEL_VERSION_V22: ProbabilityV2Variant(
        model_version=MODEL_VERSION_V22,
        specs=PROBABILITY_V2_1_SPECS,
        composite_keys=("G_NOW", "G_LEAD_V21", "PI_NOW_V21", "PI_LEAD", "SHOCK"),
        feature_metrics=tuple(
            metric for metric in _V2_FEATURE_METRICS if metric != "us_nfci"
        ) + ("us_ig_spread_baa10y", "us_equity_vol"),
        financial_conditions_metrics=("us_hy_oas", "us_ig_spread_baa10y", "us_equity_vol"),
        recalibrate=True,
    ),
    # V2.3 = V2.1 inputs + CONDITIONAL pooled recalibration (V2_3_CANDIDATE_SPEC.md).
    MODEL_VERSION_V23: ProbabilityV2Variant(
        model_version=MODEL_VERSION_V23,
        specs=PROBABILITY_V2_1_SPECS,
        composite_keys=("G_NOW", "G_LEAD_V21", "PI_NOW_V21", "PI_LEAD", "SHOCK"),
        feature_metrics=tuple(
            metric for metric in _V2_FEATURE_METRICS if metric != "us_nfci"
        ) + ("us_ig_spread_baa10y", "us_equity_vol"),
        financial_conditions_metrics=("us_hy_oas", "us_ig_spread_baa10y", "us_equity_vol"),
        recalibrate=True,
        recalibration_policy="conditional_pooled",
        recalibration_pools=(
            ("P_G_NOW_V2", "growth"),
            ("P_G_LEAD_V2", "growth"),
            ("P_PI_NOW_V2", "pi_now"),
            ("P_PI_LEAD_V2", "pi_lead"),
        ),
    ),
}


def variant_for(model_version: str) -> ProbabilityV2Variant:
    """Resolve the frozen input-variant for a v2-family model version.

    Unknown versions fail closed rather than silently inheriting V2's inputs.
    """
    variant = PROBABILITY_V2_VARIANTS.get(str(model_version or "").strip())
    if variant is None:
        raise ValueError(
            f"Unknown v2-family model_version {model_version!r}; "
            f"registered variants: {sorted(PROBABILITY_V2_VARIANTS)}"
        )
    return variant


def sigmoid(values: np.ndarray) -> np.ndarray:
    arr = np.asarray(values, dtype=float)
    out = np.empty_like(arr, dtype=float)
    positive = arr >= 0.0
    out[positive] = 1.0 / (1.0 + np.exp(-arr[positive]))
    exp_values = np.exp(arr[~positive])
    out[~positive] = exp_values / (1.0 + exp_values)
    return out


def logit(value: float, *, eps: float = 1e-6) -> float:
    clipped = float(np.clip(value, eps, 1.0 - eps))
    return math.log(clipped / (1.0 - clipped))


def binary_auc(y_true: np.ndarray, y_score: np.ndarray) -> float | None:
    y = np.asarray(y_true, dtype=int)
    score = np.asarray(y_score, dtype=float)
    finite = np.isfinite(score) & np.isin(y, [0, 1])
    y = y[finite]
    score = score[finite]
    positive_count = int((y == 1).sum())
    negative_count = int((y == 0).sum())
    if positive_count == 0 or negative_count == 0:
        return None
    ranks = pd.Series(score).rank(method="average").to_numpy(dtype=float)
    numerator = float(ranks[y == 1].sum()) - positive_count * (positive_count + 1) / 2.0
    return float(numerator / (positive_count * negative_count))


def _column_stats(values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    means = np.zeros(values.shape[1], dtype=float)
    standard_deviations = np.ones(values.shape[1], dtype=float)
    for index in range(values.shape[1]):
        column = values[:, index]
        finite = column[np.isfinite(column)]
        if finite.size == 0:
            continue
        means[index] = float(np.mean(finite))
        standard_deviation = float(np.std(finite, ddof=0))
        if math.isfinite(standard_deviation) and standard_deviation > 1e-8:
            standard_deviations[index] = standard_deviation
    return means, standard_deviations


def _scale_and_impute(values: np.ndarray, means: np.ndarray, standard_deviations: np.ndarray) -> np.ndarray:
    scaled = (np.asarray(values, dtype=float) - means) / standard_deviations
    return np.where(np.isfinite(scaled), scaled, 0.0)


def _penalized_objective(
    design: np.ndarray,
    y: np.ndarray,
    beta: np.ndarray,
    ridge_penalty: float,
    logloss_clip: float,
) -> float:
    probabilities = np.clip(sigmoid(design @ beta), logloss_clip, 1.0 - logloss_clip)
    loss = -float(np.sum(y * np.log(probabilities) + (1.0 - y) * np.log(1.0 - probabilities)))
    return loss + 0.5 * float(ridge_penalty) * float(np.dot(beta[1:], beta[1:]))


def fit_ridge_logistic(
    values: np.ndarray,
    labels: np.ndarray,
    *,
    predictor_names: Sequence[str],
    ridge_penalty: float,
    min_training_samples: int,
    min_positive_samples: int,
    min_negative_samples: int,
    logloss_clip: float = 1e-6,
    max_iter: int = 150,
    tolerance: float = 1e-8,
) -> dict[str, Any]:
    x = np.asarray(values, dtype=float)
    y = np.asarray(labels, dtype=float)
    if x.ndim != 2 or x.shape[1] != len(predictor_names):
        raise ValueError("Predictor matrix shape does not match predictor_names.")
    finite_labels = np.isfinite(y) & np.isin(y, [0.0, 1.0])
    x = x[finite_labels]
    y = y[finite_labels]
    sample_count = int(len(y))
    positive_count = int(np.sum(y == 1.0))
    negative_count = sample_count - positive_count
    positive_rate = float(np.mean(y)) if sample_count else None
    ready = (
        sample_count >= int(min_training_samples)
        and positive_count >= int(min_positive_samples)
        and negative_count >= int(min_negative_samples)
    )

    means, standard_deviations = _column_stats(x) if sample_count else (
        np.zeros(len(predictor_names), dtype=float),
        np.ones(len(predictor_names), dtype=float),
    )
    coefficients = np.zeros(len(predictor_names), dtype=float)
    intercept = logit(positive_rate if positive_rate is not None else 0.5, eps=logloss_clip)
    converged = False

    if ready:
        scaled = _scale_and_impute(x, means, standard_deviations)
        design = np.column_stack([np.ones(sample_count, dtype=float), scaled])
        beta = np.concatenate(([intercept], coefficients))
        penalty = np.concatenate(([0.0], np.full(len(predictor_names), max(0.0, float(ridge_penalty)))))
        objective = _penalized_objective(design, y, beta, ridge_penalty, logloss_clip)
        for _ in range(max_iter):
            probabilities = sigmoid(design @ beta)
            weights = np.clip(probabilities * (1.0 - probabilities), 1e-8, None)
            gradient = design.T @ (probabilities - y) + penalty * beta
            hessian = (design.T * weights) @ design + np.diag(penalty)
            try:
                step = np.linalg.solve(hessian, gradient)
            except np.linalg.LinAlgError:
                step = np.linalg.pinv(hessian) @ gradient
            if not np.isfinite(step).all():
                break
            step_scale = 1.0
            accepted = False
            while step_scale >= 1e-6:
                candidate = beta - step_scale * step
                candidate_objective = _penalized_objective(
                    design,
                    y,
                    candidate,
                    ridge_penalty,
                    logloss_clip,
                )
                if math.isfinite(candidate_objective) and candidate_objective <= objective + 1e-10:
                    accepted = True
                    beta = candidate
                    objective = candidate_objective
                    break
                step_scale *= 0.5
            if not accepted:
                break
            if float(np.max(np.abs(step_scale * step))) < tolerance:
                converged = True
                break
        if np.isfinite(beta).all():
            intercept = float(beta[0])
            coefficients = beta[1:].astype(float)
        else:
            ready = False
            coefficients.fill(0.0)
        if not converged:
            ready = False

    return {
        "predictor_names": list(predictor_names),
        "predictor_mean": means.tolist(),
        "predictor_std": standard_deviations.tolist(),
        "coefficients": coefficients.tolist(),
        "intercept": float(intercept),
        "training_sample_count": sample_count,
        "positive_sample_count": positive_count,
        "negative_sample_count": negative_count,
        "positive_rate": positive_rate,
        "ready": bool(ready),
        "converged": bool(converged),
    }


def predict_ridge_logistic(model: Mapping[str, Any], values: np.ndarray, *, probability_floor: float) -> np.ndarray:
    matrix = np.asarray(values, dtype=float)
    if matrix.ndim == 1:
        matrix = matrix.reshape(1, -1)
    predictor_names = list(model["predictor_names"])
    if matrix.ndim != 2 or matrix.shape[1] != len(predictor_names):
        raise ValueError("Prediction matrix shape does not match fitted model.")
    if not bool(model.get("ready", False)):
        return np.full(matrix.shape[0], np.nan, dtype=float)
    means = np.asarray(model["predictor_mean"], dtype=float)
    standard_deviations = np.asarray(model["predictor_std"], dtype=float)
    coefficients = np.asarray(model["coefficients"], dtype=float)
    scaled = _scale_and_impute(matrix, means, standard_deviations)
    probability = sigmoid(float(model["intercept"]) + scaled @ coefficients)
    floor = float(probability_floor)
    return np.clip(probability, floor, 1.0 - floor)


def target_period_bounds(as_of: pd.Timestamp, *, target_kind: str, target_horizon: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    timestamp = pd.Timestamp(as_of).normalize()
    if target_kind == "growth":
        period = timestamp.to_period("Q") + (1 if target_horizon == "lead" else 0)
        return period.start_time.normalize(), period.end_time.normalize()
    if target_kind == "inflation":
        start_period = timestamp.to_period("M") + (1 if target_horizon == "lead" else 0)
        end_period = start_period + (2 if target_horizon == "lead" else 0)
        return start_period.start_time.normalize(), end_period.end_time.normalize()
    raise ValueError(f"Unsupported target_kind={target_kind!r}")


def regime_probabilities(growth_probability: float, inflation_probability: float) -> dict[str, float | str]:
    growth = float(growth_probability)
    inflation = float(inflation_probability)
    values = np.asarray(
        [
            growth * (1.0 - inflation),
            growth * inflation,
            (1.0 - growth) * (1.0 - inflation),
            (1.0 - growth) * inflation,
        ],
        dtype=float,
    )
    if not np.isfinite(values).all() or abs(float(values.sum()) - 1.0) > 1e-8:
        raise ValueError("Regime quadrant probabilities are invalid.")
    order = np.argsort(values)
    winner = int(order[-1])
    return {
        "expansion_disinflation": float(values[0]),
        "heating_up": float(values[1]),
        "slow_growth": float(values[2]),
        "stagflation": float(values[3]),
        "regime": REGIME_ORDER[winner],
        "top_probability": float(values[winner]),
        "confidence": float(values[order[-1]] - values[order[-2]]),
    }


def calibration_line(y_true: np.ndarray, probability: np.ndarray) -> tuple[float | None, float | None]:
    """Fit y ~ sigmoid(intercept + slope * logit(p)) and return (intercept, slope) in LOGIT space.

    fit_ridge_logistic standardizes its design matrix, so its raw coefficient is per-SD of the
    predicted logit — a quantity invariant under affine recalibration and inflated for models
    with wide logit spread. The returned values here are de-standardized so that slope carries
    the conventional Platt meaning: 1.0 = calibrated, >1 = under-confident, <1 = over-confident.
    """
    y = np.asarray(y_true, dtype=float)
    p = np.asarray(probability, dtype=float)
    finite = np.isfinite(y) & np.isfinite(p) & np.isin(y, [0.0, 1.0])
    y = y[finite]
    p = p[finite]
    if len(y) < 10 or len(np.unique(y)) < 2:
        return None, None
    logit_values = np.asarray([logit(float(item), eps=1e-6) for item in p], dtype=float)
    fit = fit_ridge_logistic(
        logit_values.reshape(-1, 1),
        y,
        predictor_names=["predicted_logit"],
        ridge_penalty=0.0,
        min_training_samples=10,
        min_positive_samples=2,
        min_negative_samples=2,
    )
    if not fit["ready"]:
        return None, None
    mean = float(fit["predictor_mean"][0])
    standard_deviation = float(fit["predictor_std"][0])
    if not math.isfinite(standard_deviation) or standard_deviation <= 1e-8:
        return None, None
    slope = float(fit["coefficients"][0]) / standard_deviation
    intercept = float(fit["intercept"]) - slope * mean
    return intercept, slope
