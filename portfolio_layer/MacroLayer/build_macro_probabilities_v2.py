#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd

from macro_probability_v2 import (
    MODEL_VERSION_DEFAULT,
    PROBABILITY_V2_SPECS,
    ProbabilityV2Spec,
    ProbabilityV2Variant,
    binary_auc,
    calibration_line,
    fit_ridge_logistic,
    logit,
    predict_ridge_logistic,
    regime_probabilities,
    sigmoid,
    target_period_bounds,
    variant_for,
)
from macro_raw_config import (
    cfg_get,
    configure_pipeline_logging,
    connect_sqlite,
    load_macro_raw_config,
    parse_iso_date,
    parse_boolish,
    resolve_path,
    utc_now_iso,
)
from macro_serving_common import resolve_serving_db_path
from macro_serving_storage import finish_serving_run, init_db, start_serving_run

logger = logging.getLogger(__name__)

INFLATION_LABEL_METRICS = ("us_headline_cpi", "us_core_cpi", "us_headline_pce", "us_core_pce")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build shadow v2 macro probabilities from independent realized outcomes.")
    parser.add_argument("--config", type=Path, default=None, help="Path to macro raw YAML config.")
    parser.add_argument("--serving-db-path", type=Path, default=None, help="Optional serving SQLite path override.")
    parser.add_argument("--start-date", type=str, default=None, help="Optional daily output start YYYY-MM-DD override.")
    parser.add_argument("--end-date", type=str, default=None, help="Optional build end YYYY-MM-DD override.")
    parser.add_argument("--model-version", type=str, default=None, help="Optional model-version override.")
    parser.add_argument(
        "--layer-block",
        type=str,
        default="probability_v2",
        help="Config block for this candidate (probability_v2 | probability_v2_1).",
    )
    return parser.parse_args()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _atomic_write_csv(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    frame.to_csv(temporary, index=False)
    os.replace(temporary, path)


def _resolve_end_date(conn: sqlite3.Connection, override: str | None) -> pd.Timestamp:
    parsed = parse_iso_date(override)
    if parsed is not None:
        return pd.Timestamp(parsed)
    row = conn.execute("SELECT MAX(as_of_date) AS max_date FROM macro_composite_daily").fetchone()
    if row is None or not row["max_date"]:
        raise ValueError("Unable to resolve v2 end date from macro_composite_daily.")
    return pd.Timestamp(str(row["max_date"]))


def _latest_composite_raw_ingest_id(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        """
        SELECT raw_ingest_run_id
        FROM macro_serving_run
        WHERE build_step = 'composite_layer' AND status = 'completed'
        ORDER BY COALESCE(completed_at_utc, started_at_utc) DESC, rowid DESC
        LIMIT 1
        """
    ).fetchone()
    return None if row is None else str(row["raw_ingest_run_id"] or "") or None


def _row_mean(frame: pd.DataFrame, columns: Iterable[str]) -> pd.Series:
    existing = [column for column in columns if column in frame.columns]
    if not existing:
        return pd.Series(np.nan, index=frame.index, dtype=float)
    return frame[existing].mean(axis=1, skipna=True)


def _load_predictors(
    conn: sqlite3.Connection,
    *,
    end_date: pd.Timestamp,
    variant: ProbabilityV2Variant,
) -> pd.DataFrame:
    composite_keys = variant.composite_keys
    feature_metrics = variant.feature_metrics
    placeholders = ",".join("?" for _ in composite_keys)
    composites = pd.read_sql_query(
        f"""
        SELECT as_of_date, composite_key,
               CASE WHEN coverage_flag = 1 THEN COALESCE(composite_value_smoothed, composite_value_raw) END AS value
        FROM macro_composite_daily
        WHERE as_of_date <= ? AND composite_key IN ({placeholders})
        ORDER BY as_of_date, composite_key
        """,
        conn,
        params=[end_date.date().isoformat(), *composite_keys],
        parse_dates=["as_of_date"],
    )
    if composites.empty:
        raise ValueError("No composite history is available for v2 calibration.")
    predictor_frame = composites.pivot(index="as_of_date", columns="composite_key", values="value").sort_index()
    for key in composite_keys:
        if key not in predictor_frame.columns:
            predictor_frame[key] = np.nan

    feature_placeholders = ",".join("?" for _ in feature_metrics)
    features = pd.read_sql_query(
        f"""
        SELECT as_of_date, metric_key,
               CASE WHEN coverage_flag = 1 THEN standardized_value END AS standardized_value,
               CASE WHEN coverage_flag = 1 THEN zscore_value END AS zscore_value,
               CASE WHEN coverage_flag = 1 THEN transformed_value END AS transformed_value,
               CASE WHEN coverage_flag = 1 THEN raw_value_selected END AS raw_value_selected
        FROM macro_feature_daily
        WHERE as_of_date <= ? AND metric_key IN ({feature_placeholders})
        ORDER BY as_of_date, metric_key
        """,
        conn,
        params=[end_date.date().isoformat(), *feature_metrics],
        parse_dates=["as_of_date"],
    )
    standardized = features.pivot(index="as_of_date", columns="metric_key", values="standardized_value")
    zscores = features.pivot(index="as_of_date", columns="metric_key", values="zscore_value")
    transformed = features.pivot(index="as_of_date", columns="metric_key", values="transformed_value")
    raw_values = features.pivot(index="as_of_date", columns="metric_key", values="raw_value_selected")
    standardized = standardized.reindex(predictor_frame.index)
    zscores = zscores.reindex(predictor_frame.index)
    transformed = transformed.reindex(predictor_frame.index)
    raw_values = raw_values.reindex(predictor_frame.index)

    predictor_frame["growth_activity"] = _row_mean(
        standardized,
        ("us_ads_index", "us_cfnai_ma3", "us_nonfarm_payrolls", "us_initial_claims"),
    )
    predictor_frame["financial_conditions"] = _row_mean(standardized, variant.financial_conditions_metrics)
    predictor_frame["policy_tightness"] = _row_mean(zscores, ("us_effective_fed_funds", "us_10y_real_yield"))
    predictor_frame["gdp_growth_latest"] = transformed.get("us_real_gdp", np.nan)
    inflation_level_columns = [
        column
        for column in ("us_core_cpi", "us_core_pce", "us_headline_cpi", "us_headline_pce")
        if column in transformed.columns
    ]
    inflation_level_count = transformed[inflation_level_columns].notna().sum(axis=1)
    predictor_frame["inflation_level_yoy"] = transformed[inflation_level_columns].mean(axis=1).where(
        inflation_level_count >= 3
    )
    predictor_frame["core_inflation"] = _row_mean(zscores, ("us_core_cpi", "us_core_pce"))
    predictor_frame["headline_inflation"] = _row_mean(zscores, ("us_headline_cpi", "us_headline_pce"))
    predictor_frame["inflation_expectations"] = _row_mean(zscores, ("us_5y_breakeven",))
    predictor_frame["energy_shock"] = _row_mean(zscores, ("us_wti_spot", "us_brent_spot"))
    energy_level = _row_mean(raw_values, ("us_wti_spot", "us_brent_spot"))
    previous_dates = energy_level.index - pd.DateOffset(years=1)
    previous_energy = energy_level.reindex(previous_dates)
    previous_energy.index = energy_level.index
    predictor_frame["energy_yoy"] = energy_level.div(previous_energy).sub(1.0).where(previous_energy.gt(0.0))
    predictor_frame.index = pd.to_datetime(predictor_frame.index).normalize()
    return predictor_frame.sort_index()


def _first_release_events(
    conn: sqlite3.Connection,
    *,
    metric_keys: Iterable[str],
    end_date: pd.Timestamp,
) -> pd.DataFrame:
    keys = tuple(metric_keys)
    placeholders = ",".join("?" for _ in keys)
    frame = pd.read_sql_query(
        f"""
        SELECT as_of_date, metric_key, transformed_value, observation_period_selected,
               effective_available_date_selected
        FROM macro_feature_event
        WHERE as_of_date <= ?
          AND metric_key IN ({placeholders})
          AND transformed_value IS NOT NULL
          AND observation_period_selected IS NOT NULL
          AND effective_available_date_selected IS NOT NULL
        ORDER BY metric_key, observation_period_selected, as_of_date
        """,
        conn,
        params=[end_date.date().isoformat(), *keys],
        parse_dates=["as_of_date", "observation_period_selected", "effective_available_date_selected"],
    )
    if frame.empty:
        raise ValueError(f"No realized-outcome events found for metrics={keys}.")
    frame.sort_values(["metric_key", "observation_period_selected", "as_of_date"], inplace=True)
    return frame.groupby(["metric_key", "observation_period_selected"], as_index=False).first()


def _load_realized_labels(
    conn: sqlite3.Connection,
    *,
    end_date: pd.Timestamp,
    minimum_inflation_components: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    gdp = _first_release_events(conn, metric_keys=("us_real_gdp",), end_date=end_date)
    gdp["period_start"] = gdp["observation_period_selected"].dt.to_period("Q").dt.start_time.dt.normalize()
    growth_labels = gdp[
        ["period_start", "transformed_value", "effective_available_date_selected"]
    ].rename(
        columns={
            "transformed_value": "target_value",
            "effective_available_date_selected": "label_available_date",
        }
    )
    growth_labels = growth_labels.drop_duplicates("period_start", keep="first").set_index("period_start").sort_index()

    inflation = _first_release_events(conn, metric_keys=INFLATION_LABEL_METRICS, end_date=end_date)
    inflation["period_start"] = inflation["observation_period_selected"].dt.to_period("M").dt.start_time.dt.normalize()
    values = inflation.pivot(index="period_start", columns="metric_key", values="transformed_value")
    releases = inflation.pivot(index="period_start", columns="metric_key", values="effective_available_date_selected")
    component_count = values.notna().sum(axis=1)
    inflation_labels = pd.DataFrame(index=values.index)
    inflation_labels["target_value"] = values.mean(axis=1, skipna=True).where(
        component_count >= int(minimum_inflation_components)
    )
    inflation_labels["label_available_date"] = releases.max(axis=1).where(inflation_labels["target_value"].notna())
    inflation_labels["component_count"] = component_count
    return growth_labels, inflation_labels.sort_index()


def _monthly_predictors(predictors: pd.DataFrame) -> pd.DataFrame:
    frame = predictors.reset_index(names="as_of_date")
    frame["month"] = frame["as_of_date"].dt.to_period("M")
    return frame.sort_values("as_of_date").groupby("month", as_index=False).tail(1).set_index("as_of_date")


def _target_value_for_period(
    *,
    spec: ProbabilityV2Spec,
    target_start: pd.Timestamp,
    target_end: pd.Timestamp,
    growth_labels: pd.DataFrame,
    inflation_labels: pd.DataFrame,
) -> tuple[float | None, pd.Timestamp | None, str]:
    if spec.target_kind == "growth":
        if target_start not in growth_labels.index:
            return None, None, "us_real_gdp:first_release:qoq_ann_pct"
        row = growth_labels.loc[target_start]
        return float(row["target_value"]), pd.Timestamp(row["label_available_date"]), "us_real_gdp:first_release:qoq_ann_pct"

    months = pd.period_range(target_start.to_period("M"), target_end.to_period("M"), freq="M")
    period_starts = months.start_time.normalize()
    selected = inflation_labels.reindex(period_starts)
    if selected["target_value"].isna().any() or selected["label_available_date"].isna().any():
        return None, None, "cpi_pce_4way:first_release:yoy_pct"
    return (
        float(selected["target_value"].mean()),
        pd.Timestamp(selected["label_available_date"].max()),
        "cpi_pce_4way:first_release:yoy_pct",
    )


def _build_target_frame(
    *,
    monthly_predictors: pd.DataFrame,
    spec: ProbabilityV2Spec,
    growth_labels: pd.DataFrame,
    inflation_labels: pd.DataFrame,
    label_threshold: float,
) -> pd.DataFrame:
    source = monthly_predictors.copy()
    if spec.training_frequency == "quarterly":
        source = source[source.index.month.isin((3, 6, 9, 12))].copy()
    rows: list[dict[str, Any]] = []
    for predictor_as_of_date, predictor_row in source.iterrows():
        target_start, target_end = target_period_bounds(
            pd.Timestamp(predictor_as_of_date),
            target_kind=spec.target_kind,
            target_horizon=spec.target_horizon,
        )
        target_value, label_available_date, label_source = _target_value_for_period(
            spec=spec,
            target_start=target_start,
            target_end=target_end,
            growth_labels=growth_labels,
            inflation_labels=inflation_labels,
        )
        mandatory_complete = all(pd.notna(predictor_row.get(name)) for name in spec.mandatory_predictors)
        row: dict[str, Any] = {
            "probability_key": spec.probability_key,
            "predictor_as_of_date": pd.Timestamp(predictor_as_of_date).normalize(),
            "target_period_start": target_start,
            "target_period_end": target_end,
            "target_value": target_value,
            "label_value": None if target_value is None else int(float(target_value) >= float(label_threshold)),
            "label_available_date": label_available_date,
            "label_source": label_source,
            "label_threshold": float(label_threshold),
            "predictor_complete_flag": int(mandatory_complete),
        }
        row.update({name: predictor_row.get(name, np.nan) for name in spec.predictor_names})
        rows.append(row)
    return pd.DataFrame(rows)


def _build_models(
    *,
    monthly_predictors: pd.DataFrame,
    target_frame: pd.DataFrame,
    spec: ProbabilityV2Spec,
    ridge_penalty: float,
    minimum_samples: int,
    minimum_positive_samples: int,
    minimum_negative_samples: int,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    label_dates = pd.to_datetime(target_frame["label_available_date"], errors="coerce").dt.normalize()
    for calibration_as_of_date in monthly_predictors.index:
        training_mask = (
            target_frame["label_value"].notna()
            & target_frame["predictor_complete_flag"].eq(1)
            & label_dates.notna()
            & (label_dates <= pd.Timestamp(calibration_as_of_date))
        )
        training = target_frame.loc[training_mask].copy()
        model = fit_ridge_logistic(
            training[list(spec.predictor_names)].to_numpy(dtype=float),
            training["label_value"].to_numpy(dtype=float),
            predictor_names=spec.predictor_names,
            ridge_penalty=ridge_penalty,
            min_training_samples=minimum_samples,
            min_positive_samples=minimum_positive_samples,
            min_negative_samples=minimum_negative_samples,
        )
        max_label_date = label_dates.loc[training.index].max() if not training.empty else pd.NaT
        rows.append(
            {
                "calibration_as_of_date": pd.Timestamp(calibration_as_of_date).normalize(),
                "probability_key": spec.probability_key,
                "target_name": "real_gdp_qoq_ann" if spec.target_kind == "growth" else "cpi_pce_4way_yoy",
                "target_horizon": spec.target_horizon,
                "mandatory_predictors": list(spec.mandatory_predictors),
                "model": model,
                "ridge_penalty": float(ridge_penalty),
                "max_label_available_date": max_label_date,
            }
        )
    return pd.DataFrame(rows)


def _build_predictions(
    *,
    predictors: pd.DataFrame,
    models: pd.DataFrame,
    spec: ProbabilityV2Spec,
    probability_floor: float,
) -> pd.DataFrame:
    out = pd.DataFrame(index=predictors.index.copy())
    out["probability_key"] = spec.probability_key
    out["probability_value"] = np.nan
    out["calibration_as_of_date"] = pd.NaT
    out["training_sample_count"] = 0
    out["positive_rate"] = np.nan
    out["predictor_coverage_ratio"] = predictors[list(spec.predictor_names)].notna().mean(axis=1)
    out["coverage_flag"] = 0
    ordered_models = models.sort_values("calibration_as_of_date").reset_index(drop=True)
    for index, model_row in ordered_models.iterrows():
        calibration_date = pd.Timestamp(model_row["calibration_as_of_date"])
        next_date = (
            pd.Timestamp(ordered_models.iloc[index + 1]["calibration_as_of_date"])
            if index + 1 < len(ordered_models)
            else None
        )
        mask = predictors.index >= calibration_date
        if next_date is not None:
            mask &= predictors.index < next_date
        if not bool(mask.any()):
            continue
        model: Mapping[str, Any] = model_row["model"]
        values = predictors.loc[mask, list(spec.predictor_names)].to_numpy(dtype=float)
        mandatory_complete = predictors.loc[mask, list(spec.mandatory_predictors)].notna().all(axis=1).to_numpy()
        probability = predict_ridge_logistic(model, values, probability_floor=probability_floor)
        probability[~mandatory_complete] = np.nan
        out.loc[mask, "probability_value"] = probability
        out.loc[mask, "calibration_as_of_date"] = calibration_date
        out.loc[mask, "training_sample_count"] = int(model["training_sample_count"])
        out.loc[mask, "positive_rate"] = model["positive_rate"]
        out.loc[mask, "coverage_flag"] = (np.isfinite(probability) & mandatory_complete).astype(int)

    target_bounds = [
        target_period_bounds(date_value, target_kind=spec.target_kind, target_horizon=spec.target_horizon)
        for date_value in out.index
    ]
    out["target_period_start"] = [item[0] for item in target_bounds]
    out["target_period_end"] = [item[1] for item in target_bounds]
    return out.reset_index(names="as_of_date")


def _recalibration_pairs(predictions: pd.DataFrame, target_frame: pd.DataFrame) -> pd.DataFrame:
    """Resolved (raw walk-forward prediction, label) pairs with their availability dates."""
    raw_by_date = predictions.set_index("as_of_date")["probability_value"]
    label_dates = pd.to_datetime(target_frame["label_available_date"], errors="coerce").dt.normalize()
    pairs = target_frame.loc[
        target_frame["label_value"].notna() & label_dates.notna(),
        ["predictor_as_of_date", "label_value"],
    ].copy()
    pairs["label_available_date"] = label_dates.loc[pairs.index]
    pairs["raw_probability"] = pd.to_numeric(pairs["predictor_as_of_date"].map(raw_by_date), errors="coerce")
    return pairs.loc[np.isfinite(pairs["raw_probability"].astype(float))].reset_index(drop=True)


def _apply_trailing_recalibration(
    predictions: pd.DataFrame,
    *,
    own_pairs: pd.DataFrame,
    pool_pairs: pd.DataFrame,
    probability_floor: float,
    min_pairs: int,
    min_positive: int,
    min_negative: int,
    conditional: bool,
) -> pd.DataFrame:
    """Trailing PIT recalibration (V2_2/V2_3_CANDIDATE_SPEC.md).

    Per calibration window: fit the correction line on pool_pairs with labels available at or
    before the window's calibration date and apply it to the window's predictions. When
    `conditional` (V2.3), the cell recalibrates ONLY if its OWN trailing raw slope falls
    outside the gate band [0.5, 1.5]; an unready own-fit passes raw through. A not-ready pool
    fit or non-positive slope also passes raw through.
    """
    out = predictions.copy()
    floor = float(probability_floor)
    calibration_dates = pd.to_datetime(out["calibration_as_of_date"], errors="coerce")
    latched = False
    for calibration_date in sorted(calibration_dates.dropna().unique()):
        window = (calibration_dates == calibration_date).to_numpy()
        cutoff = pd.Timestamp(calibration_date)
        if conditional and not latched:
            own = own_pairs.loc[own_pairs["label_available_date"] <= cutoff]
            if not own.empty:
                _own_intercept, own_slope = calibration_line(
                    own["label_value"].to_numpy(dtype=float),
                    own["raw_probability"].to_numpy(dtype=float),
                )
                if own_slope is not None and not (0.5 <= own_slope <= 1.5):
                    latched = True
        if conditional and not latched:
            continue
        eligible = pool_pairs.loc[pool_pairs["label_available_date"] <= cutoff]
        y = eligible["label_value"].to_numpy(dtype=float)
        positive_count = int((y == 1.0).sum())
        negative_count = int((y == 0.0).sum())
        if len(eligible) < int(min_pairs) or positive_count < int(min_positive) or negative_count < int(min_negative):
            continue
        intercept, slope = calibration_line(y, eligible["raw_probability"].to_numpy(dtype=float))
        if intercept is None or slope is None or slope <= 0.0:
            continue
        # pandas may expose a read-only view (notably with copy-on-write enabled). Recalibration
        # mutates this temporary vector before assigning it back, so require an owned buffer.
        raw = pd.to_numeric(out.loc[window, "probability_value"], errors="coerce").to_numpy(
            dtype=float,
            copy=True,
        )
        finite = np.isfinite(raw)
        if not finite.any():
            continue
        logits = np.asarray([logit(value) for value in raw[finite]], dtype=float)
        raw[finite] = np.clip(sigmoid(intercept + slope * logits), floor, 1.0 - floor)
        out.loc[window, "probability_value"] = raw
    return out


def _diagnostics(
    *,
    target_frame: pd.DataFrame,
    predictions: pd.DataFrame,
    probability_key: str,
    diagnostic_as_of_date: pd.Timestamp,
    minimum_oos_samples: int,
    minimum_auc: float,
    minimum_brier_skill: float,
) -> dict[str, Any]:
    joined = target_frame.merge(
        predictions[
            ["as_of_date", "probability_value", "positive_rate", "coverage_flag"]
        ],
        left_on="predictor_as_of_date",
        right_on="as_of_date",
        how="left",
        validate="one_to_one",
    )
    usable = joined[
        joined["label_value"].notna()
        & joined["probability_value"].notna()
        & joined["positive_rate"].notna()
        & joined["coverage_flag"].eq(1)
        & (pd.to_datetime(joined["label_available_date"]) <= diagnostic_as_of_date)
    ].copy()
    y = usable["label_value"].to_numpy(dtype=float)
    probability = usable["probability_value"].to_numpy(dtype=float)
    climatology = usable["positive_rate"].to_numpy(dtype=float)
    sample_count = int(len(usable))
    positive_count = int(np.sum(y == 1.0))
    negative_count = sample_count - positive_count
    brier = float(np.mean((probability - y) ** 2)) if sample_count else None
    climatology_brier = float(np.mean((climatology - y) ** 2)) if sample_count else None
    brier_skill = (
        None
        if brier is None or climatology_brier is None or climatology_brier <= 1e-12
        else float(1.0 - brier / climatology_brier)
    )
    clipped = np.clip(probability, 1e-6, 1.0 - 1e-6)
    log_loss = (
        float(-np.mean(y * np.log(clipped) + (1.0 - y) * np.log(1.0 - clipped)))
        if sample_count
        else None
    )
    auc = binary_auc(y, probability) if sample_count else None
    calibration_intercept, calibration_slope = calibration_line(y, probability)
    if sample_count < int(minimum_oos_samples) or positive_count == 0 or negative_count == 0:
        status = "INSUFFICIENT_DATA"
        reason = f"oos_samples={sample_count} required={minimum_oos_samples} positives={positive_count} negatives={negative_count}"
    elif auc is not None and brier_skill is not None and auc >= minimum_auc and brier_skill >= minimum_brier_skill:
        status = "VALIDATED_SHADOW"
        reason = f"auc={auc:.4f} brier_skill={brier_skill:.4f}"
    else:
        status = "NOT_VALIDATED"
        reason = f"auc={auc!r} brier_skill={brier_skill!r}"
    return {
        "diagnostic_as_of_date": diagnostic_as_of_date,
        "probability_key": probability_key,
        "oos_sample_count": sample_count,
        "positive_sample_count": positive_count,
        "negative_sample_count": negative_count,
        "oos_brier_score": brier,
        "climatology_brier_score": climatology_brier,
        "brier_skill_score": brier_skill,
        "oos_log_loss": log_loss,
        "oos_auc": auc,
        "calibration_intercept": calibration_intercept,
        "calibration_slope": calibration_slope,
        "evidence_status": status,
        "evidence_reason": reason,
    }


def _build_regime_frame(
    predictions: pd.DataFrame,
    predictors: pd.DataFrame,
    *,
    energy_shock_threshold: float,
    energy_yoy_threshold: float,
) -> pd.DataFrame:
    values = predictions.pivot(index="as_of_date", columns="probability_key", values="probability_value")
    coverage = predictions.pivot(index="as_of_date", columns="probability_key", values="coverage_flag")
    required = [spec.probability_key for spec in PROBABILITY_V2_SPECS]
    out_rows: list[dict[str, Any]] = []
    for as_of_date, row in values.iterrows():
        available = all(key in row.index and pd.notna(row[key]) for key in required)
        covered = available and all(int(coverage.at[as_of_date, key]) == 1 for key in required)
        result: dict[str, Any] = {
            "as_of_date": pd.Timestamp(as_of_date),
            "p_g_now": row.get("P_G_NOW_V2"),
            "p_g_lead": row.get("P_G_LEAD_V2"),
            "p_pi_now": row.get("P_PI_NOW_V2"),
            "p_pi_lead": row.get("P_PI_LEAD_V2"),
            "coverage_flag": int(covered),
        }
        if covered:
            current = regime_probabilities(float(result["p_g_now"]), float(result["p_pi_now"]))
            next_regime = regime_probabilities(float(result["p_g_lead"]), float(result["p_pi_lead"]))
            for key, value in current.items():
                result[f"current_{key}"] = value
            for key, value in next_regime.items():
                result[f"next_{key}"] = value
        if pd.Timestamp(as_of_date) in predictors.index:
            energy_value = predictors.at[pd.Timestamp(as_of_date), "energy_shock"]
            energy_yoy = predictors.at[pd.Timestamp(as_of_date), "energy_yoy"]
        else:
            energy_value = np.nan
            energy_yoy = np.nan
        normalized_yoy = (
            float(energy_yoy) * float(energy_shock_threshold) / float(energy_yoy_threshold)
            if pd.notna(energy_yoy) and float(energy_yoy_threshold) > 0.0
            else np.nan
        )
        energy_score_candidates = [float(value) for value in (energy_value, normalized_yoy) if pd.notna(value)]
        result["energy_shock_score"] = max(energy_score_candidates) if energy_score_candidates else None
        result["energy_shock_flag"] = int(
            (pd.notna(energy_value) and float(energy_value) >= float(energy_shock_threshold))
            or (pd.notna(energy_yoy) and float(energy_yoy) >= float(energy_yoy_threshold))
        )
        out_rows.append(result)
    return pd.DataFrame(out_rows)


def _json_list(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _replace_database_rows(
    conn: sqlite3.Connection,
    *,
    model_version: str,
    output_start: pd.Timestamp,
    output_end: pd.Timestamp,
    targets: pd.DataFrame,
    models: pd.DataFrame,
    predictions: pd.DataFrame,
    diagnostics: pd.DataFrame,
    regimes: pd.DataFrame,
) -> int:
    now = utc_now_iso()
    target_rows = [
        (
            model_version,
            str(row["probability_key"]),
            pd.Timestamp(row["predictor_as_of_date"]).date().isoformat(),
            pd.Timestamp(row["target_period_start"]).date().isoformat(),
            pd.Timestamp(row["target_period_end"]).date().isoformat(),
            float(row["target_value"]) if pd.notna(row["target_value"]) else None,
            int(row["label_value"]) if pd.notna(row["label_value"]) else None,
            pd.Timestamp(row["label_available_date"]).date().isoformat() if pd.notna(row["label_available_date"]) else None,
            str(row["label_source"]),
            float(row["label_threshold"]),
            int(row["predictor_complete_flag"]),
            now,
        )
        for _, row in targets.iterrows()
    ]
    model_rows = []
    for _, row in models.iterrows():
        model: Mapping[str, Any] = row["model"]
        model_rows.append(
            (
                model_version,
                pd.Timestamp(row["calibration_as_of_date"]).date().isoformat(),
                str(row["probability_key"]),
                str(row["target_name"]),
                str(row["target_horizon"]),
                _json_list(model["predictor_names"]),
                _json_list(row["mandatory_predictors"]),
                _json_list(model["predictor_mean"]),
                _json_list(model["predictor_std"]),
                _json_list(model["coefficients"]),
                float(model["intercept"]),
                float(row["ridge_penalty"]),
                int(model["training_sample_count"]),
                int(model["positive_sample_count"]),
                int(model["negative_sample_count"]),
                float(model["positive_rate"]) if model["positive_rate"] is not None else None,
                pd.Timestamp(row["max_label_available_date"]).date().isoformat()
                if pd.notna(row["max_label_available_date"])
                else None,
                int(bool(model["ready"])),
                now,
            )
        )
    prediction_rows = [
        (
            model_version,
            pd.Timestamp(row["as_of_date"]).date().isoformat(),
            str(row["probability_key"]),
            float(row["probability_value"]) if pd.notna(row["probability_value"]) else None,
            pd.Timestamp(row["calibration_as_of_date"]).date().isoformat()
            if pd.notna(row["calibration_as_of_date"])
            else None,
            pd.Timestamp(row["target_period_start"]).date().isoformat(),
            pd.Timestamp(row["target_period_end"]).date().isoformat(),
            int(row["training_sample_count"]),
            float(row["positive_rate"]) if pd.notna(row["positive_rate"]) else None,
            float(row["predictor_coverage_ratio"]) if pd.notna(row["predictor_coverage_ratio"]) else None,
            int(row["coverage_flag"]),
            now,
        )
        for _, row in predictions.iterrows()
    ]
    diagnostic_rows = [
        (
            model_version,
            pd.Timestamp(row["diagnostic_as_of_date"]).date().isoformat(),
            str(row["probability_key"]),
            int(row["oos_sample_count"]),
            int(row["positive_sample_count"]),
            int(row["negative_sample_count"]),
            float(row["oos_brier_score"]) if pd.notna(row["oos_brier_score"]) else None,
            float(row["climatology_brier_score"]) if pd.notna(row["climatology_brier_score"]) else None,
            float(row["brier_skill_score"]) if pd.notna(row["brier_skill_score"]) else None,
            float(row["oos_log_loss"]) if pd.notna(row["oos_log_loss"]) else None,
            float(row["oos_auc"]) if pd.notna(row["oos_auc"]) else None,
            float(row["calibration_intercept"]) if pd.notna(row["calibration_intercept"]) else None,
            float(row["calibration_slope"]) if pd.notna(row["calibration_slope"]) else None,
            str(row["evidence_status"]),
            str(row["evidence_reason"]),
            now,
        )
        for _, row in diagnostics.iterrows()
    ]
    regime_rows = [
        (
            model_version,
            pd.Timestamp(row["as_of_date"]).date().isoformat(),
            float(row["p_g_now"]) if pd.notna(row["p_g_now"]) else None,
            float(row["p_g_lead"]) if pd.notna(row["p_g_lead"]) else None,
            float(row["p_pi_now"]) if pd.notna(row["p_pi_now"]) else None,
            float(row["p_pi_lead"]) if pd.notna(row["p_pi_lead"]) else None,
            float(row["current_expansion_disinflation"]) if pd.notna(row.get("current_expansion_disinflation")) else None,
            float(row["current_heating_up"]) if pd.notna(row.get("current_heating_up")) else None,
            float(row["current_slow_growth"]) if pd.notna(row.get("current_slow_growth")) else None,
            float(row["current_stagflation"]) if pd.notna(row.get("current_stagflation")) else None,
            float(row["next_expansion_disinflation"]) if pd.notna(row.get("next_expansion_disinflation")) else None,
            float(row["next_heating_up"]) if pd.notna(row.get("next_heating_up")) else None,
            float(row["next_slow_growth"]) if pd.notna(row.get("next_slow_growth")) else None,
            float(row["next_stagflation"]) if pd.notna(row.get("next_stagflation")) else None,
            str(row["current_regime"]) if pd.notna(row.get("current_regime")) else None,
            str(row["next_regime"]) if pd.notna(row.get("next_regime")) else None,
            float(row["current_top_probability"]) if pd.notna(row.get("current_top_probability")) else None,
            float(row["next_top_probability"]) if pd.notna(row.get("next_top_probability")) else None,
            float(row["current_confidence"]) if pd.notna(row.get("current_confidence")) else None,
            float(row["next_confidence"]) if pd.notna(row.get("next_confidence")) else None,
            float(row["energy_shock_score"]) if pd.notna(row["energy_shock_score"]) else None,
            int(row["energy_shock_flag"]),
            1,
            int(row["coverage_flag"]),
            now,
        )
        for _, row in regimes.iterrows()
    ]

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM macro_probability_v2_target WHERE model_version = ?", (model_version,))
        conn.execute("DELETE FROM macro_probability_v2_model WHERE model_version = ?", (model_version,))
        conn.execute("DELETE FROM macro_probability_v2_diagnostics WHERE model_version = ?", (model_version,))
        for table_name in ("macro_probability_v2_daily", "macro_regime_v2_daily"):
            conn.execute(
                f"DELETE FROM {table_name} WHERE model_version = ? AND as_of_date BETWEEN ? AND ?",
                (model_version, output_start.date().isoformat(), output_end.date().isoformat()),
            )
        conn.executemany(
            """
            INSERT INTO macro_probability_v2_target (
                model_version, probability_key, predictor_as_of_date, target_period_start,
                target_period_end, target_value, label_value, label_available_date, label_source,
                label_threshold, predictor_complete_flag, updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            target_rows,
        )
        conn.executemany(
            """
            INSERT INTO macro_probability_v2_model (
                model_version, calibration_as_of_date, probability_key, target_name, target_horizon,
                predictor_names_json, mandatory_predictors_json, predictor_mean_json, predictor_std_json,
                coefficients_json, intercept_value, ridge_penalty, training_sample_count,
                positive_sample_count, negative_sample_count, positive_rate, max_label_available_date,
                calibration_ready_flag, updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            model_rows,
        )
        conn.executemany(
            """
            INSERT INTO macro_probability_v2_daily (
                model_version, as_of_date, probability_key, probability_value, calibration_as_of_date,
                target_period_start, target_period_end, training_sample_count, positive_rate,
                predictor_coverage_ratio, coverage_flag, updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            prediction_rows,
        )
        conn.executemany(
            """
            INSERT INTO macro_probability_v2_diagnostics (
                model_version, diagnostic_as_of_date, probability_key, oos_sample_count,
                positive_sample_count, negative_sample_count, oos_brier_score, climatology_brier_score,
                brier_skill_score, oos_log_loss, oos_auc, calibration_intercept, calibration_slope,
                evidence_status, evidence_reason, updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            diagnostic_rows,
        )
        conn.executemany(
            """
            INSERT INTO macro_regime_v2_daily (
                model_version, as_of_date, p_g_now, p_g_lead, p_pi_now, p_pi_lead,
                p_current_expansion_disinflation, p_current_heating_up, p_current_slow_growth,
                p_current_stagflation, p_next_expansion_disinflation, p_next_heating_up,
                p_next_slow_growth, p_next_stagflation, current_regime, next_regime,
                current_regime_probability, next_regime_probability, current_regime_confidence,
                next_regime_confidence, energy_shock_score, energy_shock_flag, shadow_only_flag,
                coverage_flag, updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            regime_rows,
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return len(target_rows) + len(model_rows) + len(prediction_rows) + len(diagnostic_rows) + len(regime_rows)


def main() -> None:
    configure_pipeline_logging()
    args = parse_args()
    config_path, cfg = load_macro_raw_config(args.config)
    layer_block = str(args.layer_block or "probability_v2").strip()
    layer_cfg = cfg_get(cfg, layer_block, default={}) or {}
    if not layer_cfg:
        raise ValueError(f"Config block {layer_block!r} is missing or empty.")
    if not parse_boolish(cfg_get(layer_cfg, "shadow_only", default=None), default=False):
        raise ValueError(f"{layer_block}.shadow_only must remain true until formal promotion.")
    model_version = str(args.model_version or cfg_get(layer_cfg, "model_version", default=MODEL_VERSION_DEFAULT)).strip()
    if not model_version:
        raise ValueError(f"{layer_block}.model_version must be non-empty.")
    variant = variant_for(model_version)
    serving_db_path = resolve_serving_db_path(cfg, config_path, override=args.serving_db_path)
    conn = connect_sqlite(serving_db_path, row_factory=sqlite3.Row)
    serving_run_id = uuid.uuid4().hex
    run_started = False
    try:
        init_db(conn)
        end_date = _resolve_end_date(conn, args.end_date)
        configured_start = parse_iso_date(str(cfg_get(layer_cfg, "history_start_date", default="2002-01-01")))
        requested_start = parse_iso_date(args.start_date)
        canonical_start = configured_start or requested_start or end_date.date()
        if requested_start is not None and requested_start < canonical_start:
            raise ValueError(
                f"v2 output start {requested_start} is before configured history start {canonical_start}."
            )
        if requested_start is not None and requested_start > canonical_start:
            logger.info(
                "Ignoring partial v2 start %s; model-version history is rebuilt from %s.",
                requested_start,
                canonical_start,
            )
        output_start = pd.Timestamp(canonical_start)
        if output_start > end_date:
            raise ValueError(f"v2 output start {output_start.date()} is after end {end_date.date()}.")
        raw_ingest_run_id = _latest_composite_raw_ingest_id(conn)
        start_serving_run(
            conn,
            serving_run_id=serving_run_id,
            build_step="probability_v2_research",
            raw_ingest_run_id=raw_ingest_run_id,
            as_of_start_date=output_start.date().isoformat(),
            as_of_end_date=end_date.date().isoformat(),
            metric_count=len(variant.specs),
            notes=f"Shadow v2 independent-outcome calibration model_version={model_version}.",
        )
        run_started = True

        predictors = _load_predictors(conn, end_date=end_date, variant=variant)
        history_floor = pd.Timestamp(configured_start) if configured_start is not None else predictors.index.min()
        predictors = predictors[(predictors.index >= history_floor) & (predictors.index <= end_date)].copy()
        monthly = _monthly_predictors(predictors)
        minimum_inflation_components = int(cfg_get(layer_cfg, "minimum_inflation_components", default=4))
        growth_labels, inflation_labels = _load_realized_labels(
            conn,
            end_date=end_date,
            minimum_inflation_components=minimum_inflation_components,
        )
        growth_threshold = float(cfg_get(layer_cfg, "growth_resilient_qoq_ann_threshold", default=0.02))
        inflation_threshold = float(cfg_get(layer_cfg, "inflation_pressure_yoy_threshold", default=0.025))
        ridge_penalty = float(cfg_get(layer_cfg, "ridge_penalty", default=5.0))
        probability_floor = float(cfg_get(layer_cfg, "output_probability_floor", default=0.02))
        growth_min_samples = int(cfg_get(layer_cfg, "growth_min_training_samples", default=40))
        inflation_min_samples = int(cfg_get(layer_cfg, "inflation_min_training_samples", default=60))
        lead_min_samples = int(cfg_get(layer_cfg, "lead_min_training_samples", default=40))
        minimum_positive = int(cfg_get(layer_cfg, "minimum_positive_samples", default=8))
        minimum_negative = int(cfg_get(layer_cfg, "minimum_negative_samples", default=8))
        minimum_oos_growth = int(cfg_get(layer_cfg, "evidence", "growth_min_oos_samples", default=24))
        minimum_oos_inflation = int(cfg_get(layer_cfg, "evidence", "inflation_min_oos_samples", default=60))
        minimum_oos_inflation_lead = int(
            cfg_get(layer_cfg, "evidence", "inflation_lead_min_oos_samples", default=16)
        )
        minimum_auc = float(cfg_get(layer_cfg, "evidence", "minimum_auc", default=0.52))
        minimum_brier_skill = float(cfg_get(layer_cfg, "evidence", "minimum_brier_skill", default=0.0))

        target_frames: list[pd.DataFrame] = []
        model_frames: list[pd.DataFrame] = []
        prediction_frames: list[pd.DataFrame] = []
        diagnostic_rows: list[dict[str, Any]] = []
        built_specs: list[tuple[ProbabilityV2Spec, pd.DataFrame, pd.DataFrame, pd.DataFrame]] = []
        for spec in variant.specs:
            threshold = growth_threshold if spec.target_kind == "growth" else inflation_threshold
            target_frame = _build_target_frame(
                monthly_predictors=monthly,
                spec=spec,
                growth_labels=growth_labels,
                inflation_labels=inflation_labels,
                label_threshold=threshold,
            )
            models = _build_models(
                monthly_predictors=monthly,
                target_frame=target_frame,
                spec=spec,
                ridge_penalty=ridge_penalty,
                minimum_samples=(
                    growth_min_samples
                    if spec.target_kind == "growth"
                    else lead_min_samples
                    if spec.target_horizon == "lead"
                    else inflation_min_samples
                ),
                minimum_positive_samples=minimum_positive,
                minimum_negative_samples=minimum_negative,
            )
            predictions = _build_predictions(
                predictors=predictors,
                models=models,
                spec=spec,
                probability_floor=probability_floor,
            )
            built_specs.append((spec, target_frame, models, predictions))

        # Recalibration pass runs AFTER all raw predictions exist so pooled policies
        # (V2.3) can share pairs across cells; "always" (V2.2) pools = own pairs.
        if variant.recalibrate:
            pair_by_key = {
                spec.probability_key: _recalibration_pairs(predictions, target_frame)
                for spec, target_frame, _models, predictions in built_specs
            }
            pool_names = dict(variant.recalibration_pools)
            pooled_frames: dict[str, list[pd.DataFrame]] = {}
            for key, pool_name in pool_names.items():
                if key in pair_by_key:
                    pooled_frames.setdefault(pool_name, []).append(pair_by_key[key])
            pooled_pairs = {
                name: pd.concat(frames, ignore_index=True) for name, frames in pooled_frames.items()
            }
            conditional = str(variant.recalibration_policy) == "conditional_pooled"
            built_specs = [
                (
                    spec,
                    target_frame,
                    models,
                    _apply_trailing_recalibration(
                        predictions,
                        own_pairs=pair_by_key[spec.probability_key],
                        pool_pairs=(
                            pooled_pairs.get(pool_names.get(spec.probability_key, ""), pair_by_key[spec.probability_key])
                            if conditional
                            else pair_by_key[spec.probability_key]
                        ),
                        probability_floor=probability_floor,
                        min_pairs=variant.recalibration_min_pairs,
                        min_positive=variant.recalibration_min_positive,
                        min_negative=variant.recalibration_min_negative,
                        conditional=conditional,
                    ),
                )
                for spec, target_frame, models, predictions in built_specs
            ]

        for spec, target_frame, models, predictions in built_specs:
            diagnostic_rows.append(
                _diagnostics(
                    target_frame=target_frame,
                    predictions=predictions,
                    probability_key=spec.probability_key,
                    diagnostic_as_of_date=end_date,
                    minimum_oos_samples=(
                        minimum_oos_growth
                        if spec.target_kind == "growth"
                        else minimum_oos_inflation_lead
                        if spec.target_horizon == "lead"
                        else minimum_oos_inflation
                    ),
                    minimum_auc=minimum_auc,
                    minimum_brier_skill=minimum_brier_skill,
                )
            )
            target_frames.append(target_frame)
            model_frames.append(models)
            prediction_frames.append(predictions)

        targets = pd.concat(target_frames, ignore_index=True, sort=False)
        models = pd.concat(model_frames, ignore_index=True, sort=False)
        all_predictions = pd.concat(prediction_frames, ignore_index=True, sort=False)
        diagnostics = pd.DataFrame(diagnostic_rows)
        all_regimes = _build_regime_frame(
            all_predictions,
            predictors,
            energy_shock_threshold=float(cfg_get(layer_cfg, "energy_shock_z_threshold", default=1.5)),
            energy_yoy_threshold=float(cfg_get(layer_cfg, "energy_shock_yoy_threshold", default=0.25)),
        )
        prediction_output = all_predictions[
            (all_predictions["as_of_date"] >= output_start) & (all_predictions["as_of_date"] <= end_date)
        ].copy()
        regime_output = all_regimes[
            (all_regimes["as_of_date"] >= output_start) & (all_regimes["as_of_date"] <= end_date)
        ].copy()
        rows_written = _replace_database_rows(
            conn,
            model_version=model_version,
            output_start=output_start,
            output_end=end_date,
            targets=targets,
            models=models,
            predictions=prediction_output,
            diagnostics=diagnostics,
            regimes=regime_output,
        )

        output_root_raw = str(cfg_get(layer_cfg, "output_dir", default="MacroLayer/out/regime_v2"))
        output_root = resolve_path(config_path, output_root_raw)
        if output_root is None:
            raise ValueError("Unable to resolve probability_v2.output_dir.")
        output_dir = output_root / end_date.date().isoformat()
        latest_probabilities = prediction_output[prediction_output["as_of_date"].eq(end_date)].copy()
        latest_regime = regime_output[regime_output["as_of_date"].eq(end_date)].copy()
        probability_path = output_dir / "macro_probabilities_v2_latest.csv"
        regime_path = output_dir / "macro_regime_v2_latest.csv"
        diagnostics_path = output_dir / "macro_probability_v2_diagnostics.csv"
        _atomic_write_csv(probability_path, latest_probabilities)
        _atomic_write_csv(regime_path, latest_regime)
        _atomic_write_csv(diagnostics_path, diagnostics)
        latest_predictors = predictors.loc[end_date].to_dict() if end_date in predictors.index else {}
        latest_predictors = {
            key: (float(value) if pd.notna(value) and math.isfinite(float(value)) else None)
            for key, value in latest_predictors.items()
        }
        manifest = {
            "model_version": model_version,
            "shadow_only": True,
            "build_end_date": end_date.date().isoformat(),
            "output_start_date": output_start.date().isoformat(),
            "target_contract": {
                "growth": {"source": "us_real_gdp:first_release:qoq_ann_pct", "threshold": growth_threshold},
                "inflation": {
                    "source": "cpi_pce_4way:first_release:yoy_pct",
                    "threshold": inflation_threshold,
                    "minimum_components": minimum_inflation_components,
                },
            },
            "config_sha256": _sha256_file(config_path),
            "builder_sha256": _sha256_file(Path(__file__)),
            "probability_engine_sha256": _sha256_file(Path(__file__).resolve().parent / "macro_probability_v2.py"),
            "files": {
                probability_path.name: _sha256_file(probability_path),
                regime_path.name: _sha256_file(regime_path),
                diagnostics_path.name: _sha256_file(diagnostics_path),
            },
            "rows_written": rows_written,
            "latest_predictors": latest_predictors,
            "diagnostics": diagnostics.to_dict(orient="records"),
            "created_at_utc": utc_now_iso(),
        }
        _atomic_write_text(output_dir / "macro_regime_v2_manifest.json", json.dumps(manifest, indent=2, default=str) + "\n")
        finish_serving_run(
            conn,
            serving_run_id=serving_run_id,
            status="completed",
            rows_written=rows_written,
            notes=f"Shadow v2 calibration complete; artifacts={output_dir}.",
        )
        logger.info(
            "Macro v2 calibration complete model=%s end=%s rows=%d latest_regime=%s",
            model_version,
            end_date.date().isoformat(),
            rows_written,
            latest_regime.to_dict(orient="records"),
        )
    except BaseException as exc:
        if run_started:
            try:
                finish_serving_run(
                    conn,
                    serving_run_id=serving_run_id,
                    status="failed",
                    rows_written=0,
                    notes=f"Shadow v2 calibration failed: {type(exc).__name__}: {exc}",
                )
            except Exception:
                logger.exception("Unable to record failed v2 serving run.")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
