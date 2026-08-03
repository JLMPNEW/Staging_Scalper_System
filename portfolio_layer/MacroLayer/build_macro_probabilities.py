#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import math
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from macro_raw_config import (
    cfg_get,
    configure_pipeline_logging,
    connect_sqlite,
    load_macro_raw_config,
    parse_iso_date,
    utc_now_iso,
)
from macro_serving_common import resolve_serving_db_path
from macro_serving_storage import (
    clear_probability_range,
    finish_serving_run,
    init_db,
    insert_many,
    start_serving_run,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProbabilitySpec:
    probability_key: str
    source_composite_key: str
    target_composite_key: str
    target_start_month_offset: int
    target_window_months: int
    label_threshold: float = 0.0
    calibration_mode: str = "forecast_logistic"


PROBABILITY_SPECS: tuple[ProbabilitySpec, ...] = (
    ProbabilitySpec("P_G_NOW", "G_NOW", "G_NOW", 0, 1, 0.0, "direct_standardized_logistic"),
    ProbabilitySpec("P_G_LEAD", "G_LEAD", "G_NOW", 1, 3, 0.0),
    ProbabilitySpec("P_PI_NOW", "PI_NOW", "PI_NOW", 0, 1, 0.0, "direct_standardized_logistic"),
    ProbabilitySpec("P_PI_LEAD", "PI_LEAD", "PI_NOW", 1, 3, 0.0),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the tier-1 macro probability layer.")
    parser.add_argument("--config", type=Path, default=None, help="Path to macro raw YAML config.")
    parser.add_argument("--serving-db-path", type=Path, default=None, help="Optional serving SQLite path override.")
    parser.add_argument("--start-date", type=str, default=None, help="Optional daily probability start YYYY-MM-DD override.")
    parser.add_argument("--end-date", type=str, default=None, help="Optional daily probability end YYYY-MM-DD override.")
    parser.add_argument("--probability-keys", nargs="*", default=None, help="Optional probability_key filter.")
    return parser.parse_args()


def _resolve_composite_bounds(
    conn: sqlite3.Connection,
    *,
    start_override: str | None,
    end_override: str | None,
) -> tuple[date, date]:
    start_date = parse_iso_date(start_override)
    end_date = parse_iso_date(end_override)
    row = conn.execute(
        """
        SELECT MIN(as_of_date) AS min_as_of_date, MAX(as_of_date) AS max_as_of_date
        FROM macro_composite_daily
        """
    ).fetchone()
    if start_date is None and row is not None:
        start_date = parse_iso_date(row["min_as_of_date"])
    if end_date is None and row is not None:
        end_date = parse_iso_date(row["max_as_of_date"])
    if start_date is None or end_date is None:
        raise ValueError("Unable to resolve probability build dates from macro_composite_daily.")
    if end_date < start_date:
        raise ValueError(f"Probability end date {end_date.isoformat()} is before start date {start_date.isoformat()}.")
    return start_date, end_date


def _latest_composite_run_raw_ingest_id(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        """
        SELECT raw_ingest_run_id
        FROM macro_serving_run
        WHERE build_step = 'composite_layer'
          AND status = 'completed'
        ORDER BY COALESCE(completed_at_utc, started_at_utc) DESC, rowid DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    return str(row["raw_ingest_run_id"] or "") or None


def _load_composite_frame(
    conn: sqlite3.Connection,
    *,
    end_date: str,
    composite_keys: list[str],
) -> pd.DataFrame:
    placeholders = ",".join("?" for _ in composite_keys)
    query = f"""
        SELECT
            as_of_date,
            composite_key,
            COALESCE(composite_value_smoothed, composite_value_raw) AS composite_value,
            coverage_flag
        FROM macro_composite_daily
        WHERE as_of_date <= ?
          AND composite_key IN ({placeholders})
        ORDER BY as_of_date, composite_key
    """
    frame = pd.read_sql_query(query, conn, params=[end_date, *composite_keys], parse_dates=["as_of_date"])
    if frame.empty:
        raise ValueError("macro_composite_daily returned no rows for the requested probability build.")
    return frame


def _build_monthly_snapshot_maps(composite_frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    covered = composite_frame[
        composite_frame["coverage_flag"].fillna(0).astype(int).eq(1) & composite_frame["composite_value"].notna()
    ].copy()
    if covered.empty:
        raise ValueError("No covered composite rows are available for probability calibration.")
    covered["ym"] = covered["as_of_date"].dt.to_period("M")
    monthly = (
        covered.sort_values(["composite_key", "as_of_date"])
        .groupby(["composite_key", "ym"], as_index=False)
        .tail(1)
        .copy()
    )
    value_map = monthly.pivot(index="ym", columns="composite_key", values="composite_value").sort_index()
    date_map = monthly.pivot(index="ym", columns="composite_key", values="as_of_date").sort_index()
    return value_map, date_map


def _build_monthly_probability_dataset(
    *,
    value_map: pd.DataFrame,
    date_map: pd.DataFrame,
    spec: ProbabilitySpec,
) -> pd.DataFrame:
    if spec.source_composite_key not in value_map.columns:
        raise ValueError(f"Missing source composite history for probability_key={spec.probability_key}.")
    if spec.target_composite_key not in value_map.columns:
        raise ValueError(f"Missing target composite history for probability_key={spec.probability_key}.")

    target_slices = []
    for offset in range(spec.target_start_month_offset, spec.target_start_month_offset + spec.target_window_months):
        target_slices.append(value_map[spec.target_composite_key].shift(-offset))
    target_window = pd.concat(target_slices, axis=1)
    target_window_value = target_window.mean(axis=1)
    target_window_value = target_window_value.where(target_window.notna().sum(axis=1) == spec.target_window_months)
    label = (target_window_value >= float(spec.label_threshold)).astype(float)
    label = label.where(target_window_value.notna())
    target_end_offset = spec.target_start_month_offset + spec.target_window_months - 1
    observed_target_date = date_map[spec.target_composite_key].shift(-target_end_offset)
    target_end_periods = pd.PeriodIndex(value_map.index, freq="M") + target_end_offset
    completed_month_date = pd.Series(
        target_end_periods.to_timestamp(how="end").normalize(),
        index=value_map.index,
    )
    label_available_date = pd.concat(
        [pd.to_datetime(observed_target_date, errors="coerce"), completed_month_date],
        axis=1,
    ).max(axis=1)
    return pd.DataFrame(
        {
            "source_value": value_map[spec.source_composite_key],
            "source_snapshot_date": date_map[spec.source_composite_key],
            "target_window_value": target_window_value,
            "label_value": label,
            "label_available_date": label_available_date,
        }
    ).sort_index()


def _sigmoid(x: np.ndarray) -> np.ndarray:
    x_arr = np.asarray(x, dtype=float)
    out = np.empty_like(x_arr, dtype=float)
    pos = x_arr >= 0.0
    out[pos] = 1.0 / (1.0 + np.exp(-x_arr[pos]))
    exp_x = np.exp(x_arr[~pos])
    out[~pos] = exp_x / (1.0 + exp_x)
    return out


def _logit(p: float, *, eps: float) -> float:
    clipped = float(np.clip(p, eps, 1.0 - eps))
    return math.log(clipped / (1.0 - clipped))


def _binary_auc(y_true: np.ndarray, y_score: np.ndarray) -> float | None:
    y = np.asarray(y_true, dtype=int)
    score = np.asarray(y_score, dtype=float)
    pos = int((y == 1).sum())
    neg = int((y == 0).sum())
    if pos == 0 or neg == 0:
        return None
    ranks = pd.Series(score).rank(method="average").to_numpy(dtype=float)
    auc = (float(ranks[y == 1].sum()) - (pos * (pos + 1) / 2.0)) / float(pos * neg)
    return float(auc)


def _fit_monotonic_logistic(
    x: np.ndarray,
    y: np.ndarray,
    *,
    ridge_penalty: float,
    logloss_clip: float,
    output_probability_floor: float,
    max_iter: int = 100,
    tol: float = 1e-8,
) -> dict[str, float | int | None]:
    x_arr = np.asarray(x, dtype=float)
    y_arr = np.asarray(y, dtype=float)
    predictor_mean = float(np.mean(x_arr)) if x_arr.size else 0.0
    predictor_std = float(np.std(x_arr, ddof=0)) if x_arr.size else 1.0
    if not math.isfinite(predictor_std) or predictor_std <= 1e-8:
        predictor_std = 1.0
    x_scaled = (x_arr - predictor_mean) / predictor_std

    pos_rate = float(np.mean(y_arr)) if y_arr.size else 0.5
    beta = np.array([_logit(pos_rate, eps=logloss_clip), 0.0], dtype=float)
    penalty = np.array([0.0, float(max(0.0, ridge_penalty))], dtype=float)
    X = np.column_stack([np.ones(len(x_scaled), dtype=float), x_scaled])

    for _ in range(max_iter):
        p = _sigmoid(X @ beta)
        w = np.clip(p * (1.0 - p), 1e-8, None)
        grad = X.T @ (y_arr - p) - penalty * beta
        hessian = (X.T * w) @ X + np.diag(penalty)
        try:
            delta = np.linalg.solve(hessian, grad)
        except np.linalg.LinAlgError:
            delta = np.linalg.pinv(hessian) @ grad
        beta_next = beta + delta
        if not np.isfinite(beta_next).all():
            break
        if float(np.max(np.abs(beta_next - beta))) < tol:
            beta = beta_next
            break
        beta = beta_next

    slope_clipped_flag = 0
    if not np.isfinite(beta).all():
        beta = np.array([_logit(pos_rate, eps=logloss_clip), 0.0], dtype=float)
    if float(beta[1]) < 0.0:
        beta = np.array([_logit(pos_rate, eps=logloss_clip), 0.0], dtype=float)
        slope_clipped_flag = 1

    train_probability = _sigmoid(X @ beta)
    train_probability = np.clip(
        train_probability,
        float(output_probability_floor),
        1.0 - float(output_probability_floor),
    )
    p_eval = np.clip(train_probability, logloss_clip, 1.0 - logloss_clip)
    train_brier = float(np.mean((train_probability - y_arr) ** 2))
    train_log_loss = float(-np.mean(y_arr * np.log(p_eval) + (1.0 - y_arr) * np.log(1.0 - p_eval)))
    train_auc = _binary_auc(y_arr, train_probability)
    return {
        "predictor_mean": predictor_mean,
        "predictor_std": predictor_std,
        "intercept_value": float(beta[0]),
        "slope_value": float(beta[1]),
        "slope_clipped_flag": int(slope_clipped_flag),
        "train_brier_score": train_brier,
        "train_log_loss": train_log_loss,
        "train_auc": train_auc,
        "train_probability_p05": float(np.quantile(train_probability, 0.05)),
        "train_probability_p50": float(np.quantile(train_probability, 0.50)),
        "train_probability_p95": float(np.quantile(train_probability, 0.95)),
        "saturation_low_share": float(np.mean(train_probability <= 0.05)),
        "saturation_high_share": float(np.mean(train_probability >= 0.95)),
    }


def _build_calibration_and_diagnostics(
    *,
    dataset: pd.DataFrame,
    spec: ProbabilitySpec,
    min_training_months: int,
    min_positive_months: int,
    min_negative_months: int,
    ridge_penalty: float,
    logloss_clip: float,
    output_probability_floor: float,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    calibration_rows: list[dict[str, object]] = []
    diagnostics_rows: list[dict[str, object]] = []
    previous_fitted_intercept: float | None = None
    previous_fitted_slope: float | None = None

    anchors = dataset[dataset["source_snapshot_date"].notna()].copy().sort_values("source_snapshot_date")
    label_date = pd.to_datetime(dataset["label_available_date"], errors="coerce").dt.normalize()
    for _, anchor_row in anchors.iterrows():
        anchor_date = pd.Timestamp(anchor_row["source_snapshot_date"]).normalize()
        if spec.calibration_mode == "direct_standardized_logistic":
            source_date = pd.to_datetime(dataset["source_snapshot_date"], errors="coerce").dt.normalize()
            train_mask = dataset["source_value"].notna() & source_date.notna() & (source_date < anchor_date)
            train = dataset.loc[train_mask, ["source_value"]].copy()
            train["label_value"] = (train["source_value"] >= float(spec.label_threshold)).astype(float)
        else:
            train_mask = (
                dataset["source_value"].notna()
                & dataset["label_value"].notna()
                & label_date.notna()
                & (label_date <= anchor_date)
            )
            train = dataset.loc[train_mask, ["source_value", "label_value"]].copy()
        training_sample_count = int(len(train))
        positive_sample_count = int(train["label_value"].sum()) if training_sample_count > 0 else 0
        negative_sample_count = training_sample_count - positive_sample_count
        positive_rate = float(positive_sample_count / training_sample_count) if training_sample_count > 0 else None
        ready = int(
            training_sample_count >= int(min_training_months)
            and (
                spec.calibration_mode == "direct_standardized_logistic"
                or (
                    positive_sample_count >= int(min_positive_months)
                    and negative_sample_count >= int(min_negative_months)
                )
            )
        )

        predictor_mean = float(train["source_value"].mean()) if training_sample_count > 0 else 0.0
        predictor_std = float(train["source_value"].std(ddof=0)) if training_sample_count > 0 else 1.0
        if not math.isfinite(predictor_std) or predictor_std <= 1e-8:
            predictor_std = 1.0
        intercept_value = _logit(positive_rate if positive_rate is not None else 0.5, eps=logloss_clip)
        slope_value = 0.0
        slope_clipped_flag = 0
        train_brier_score: float | None = None
        train_log_loss: float | None = None
        train_auc: float | None = None
        train_probability_p05: float | None = None
        train_probability_p50: float | None = None
        train_probability_p95: float | None = None
        saturation_low_share: float | None = None
        saturation_high_share: float | None = None

        if spec.calibration_mode == "direct_standardized_logistic":
            intercept_value = 0.0
            slope_value = 1.0
        elif ready == 1:
            fit = _fit_monotonic_logistic(
                train["source_value"].to_numpy(dtype=float),
                train["label_value"].to_numpy(dtype=float),
                ridge_penalty=ridge_penalty,
                logloss_clip=logloss_clip,
                output_probability_floor=output_probability_floor,
            )
            predictor_mean = float(fit["predictor_mean"])
            predictor_std = float(fit["predictor_std"])
            intercept_value = float(fit["intercept_value"])
            slope_value = float(fit["slope_value"])
            slope_clipped_flag = int(fit["slope_clipped_flag"])
            train_brier_score = float(fit["train_brier_score"])
            train_log_loss = float(fit["train_log_loss"])
            train_auc = float(fit["train_auc"]) if fit["train_auc"] is not None else None
            train_probability_p05 = float(fit["train_probability_p05"])
            train_probability_p50 = float(fit["train_probability_p50"])
            train_probability_p95 = float(fit["train_probability_p95"])
            saturation_low_share = float(fit["saturation_low_share"])
            saturation_high_share = float(fit["saturation_high_share"])

        calibration_rows.append(
            {
                "calibration_as_of_date": anchor_date,
                "probability_key": spec.probability_key,
                "source_composite_key": spec.source_composite_key,
                "target_composite_key": spec.target_composite_key,
                "target_start_month_offset": int(spec.target_start_month_offset),
                "target_window_months": int(spec.target_window_months),
                "label_threshold": float(spec.label_threshold),
                "training_sample_count": training_sample_count,
                "positive_sample_count": positive_sample_count,
                "negative_sample_count": negative_sample_count,
                "positive_rate": positive_rate,
                "predictor_mean": predictor_mean,
                "predictor_std": predictor_std,
                "intercept_value": intercept_value,
                "slope_value": slope_value,
                "slope_clipped_flag": slope_clipped_flag,
                "calibration_ready_flag": ready,
            }
        )
        diagnostics_rows.append(
            {
                "calibration_as_of_date": anchor_date,
                "probability_key": spec.probability_key,
                "source_composite_key": spec.source_composite_key,
                "train_brier_score": train_brier_score,
                "train_log_loss": train_log_loss,
                "train_auc": train_auc,
                "train_probability_p05": train_probability_p05,
                "train_probability_p50": train_probability_p50,
                "train_probability_p95": train_probability_p95,
                "saturation_low_share": saturation_low_share,
                "saturation_high_share": saturation_high_share,
                "coefficient_delta_intercept": (
                    None
                    if ready != 1 or previous_fitted_intercept is None
                    else float(intercept_value - previous_fitted_intercept)
                ),
                "coefficient_delta_slope": (
                    None
                    if ready != 1 or previous_fitted_slope is None
                    else float(slope_value - previous_fitted_slope)
                ),
            }
        )
        if ready == 1:
            previous_fitted_intercept = float(intercept_value)
            previous_fitted_slope = float(slope_value)

    calibration_frame = pd.DataFrame(calibration_rows)
    diagnostics_frame = pd.DataFrame(diagnostics_rows)
    if not calibration_frame.empty:
        calibration_frame.sort_values(["probability_key", "calibration_as_of_date"], inplace=True)
    if not diagnostics_frame.empty:
        diagnostics_frame.sort_values(["probability_key", "calibration_as_of_date"], inplace=True)
    return calibration_frame, diagnostics_frame


def _build_daily_probability_frame(
    *,
    source_daily: pd.DataFrame,
    calibration_frame: pd.DataFrame,
    spec: ProbabilitySpec,
    start_date: date,
    end_date: date,
    output_probability_floor: float,
) -> pd.DataFrame:
    daily = source_daily[source_daily["composite_key"] == spec.source_composite_key].copy()
    daily.rename(columns={"composite_value": "source_composite_value", "coverage_flag": "source_coverage_flag"}, inplace=True)
    daily = daily.sort_values("as_of_date")

    calibration = calibration_frame[calibration_frame["probability_key"] == spec.probability_key].copy()
    calibration = calibration.sort_values("calibration_as_of_date")
    if calibration.empty:
        daily["calibration_as_of_date"] = pd.NaT
        daily["training_sample_count"] = 0
        daily["positive_rate"] = np.nan
        daily["probability_value"] = np.nan
        daily["coverage_flag"] = 0
    else:
        merged = pd.merge_asof(
            daily,
            calibration,
            left_on="as_of_date",
            right_on="calibration_as_of_date",
            direction="backward",
        )
        usable = (
            merged["source_coverage_flag"].fillna(0).astype(int).eq(1)
            & merged["calibration_ready_flag"].fillna(0).astype(int).eq(1)
            & merged["source_composite_value"].notna()
            & merged["predictor_mean"].notna()
            & merged["predictor_std"].notna()
            & merged["intercept_value"].notna()
            & merged["slope_value"].notna()
        )
        merged["probability_value"] = np.nan
        scaled = (merged["source_composite_value"] - merged["predictor_mean"]) / merged["predictor_std"].replace(0.0, 1.0)
        merged.loc[usable, "probability_value"] = np.clip(
            _sigmoid(
            merged.loc[usable, "intercept_value"].to_numpy(dtype=float)
            + merged.loc[usable, "slope_value"].to_numpy(dtype=float) * scaled.loc[usable].to_numpy(dtype=float)
            ),
            float(output_probability_floor),
            1.0 - float(output_probability_floor),
        )
        merged["coverage_flag"] = (usable & merged["probability_value"].notna()).astype(int)
        daily = merged

    mask = (daily["as_of_date"] >= pd.Timestamp(start_date)) & (daily["as_of_date"] <= pd.Timestamp(end_date))
    out = daily.loc[mask].copy()
    out["probability_key"] = spec.probability_key
    out["source_composite_key"] = spec.source_composite_key
    out["target_composite_key"] = spec.target_composite_key
    out["target_start_month_offset"] = int(spec.target_start_month_offset)
    out["target_window_months"] = int(spec.target_window_months)
    out["label_threshold"] = float(spec.label_threshold)
    out["training_sample_count"] = pd.to_numeric(out.get("training_sample_count", 0), errors="coerce").fillna(0).astype(int)
    return out[
        [
            "as_of_date",
            "probability_key",
            "source_composite_key",
            "source_composite_value",
            "probability_value",
            "calibration_as_of_date",
            "target_composite_key",
            "target_start_month_offset",
            "target_window_months",
            "label_threshold",
            "training_sample_count",
            "positive_rate",
            "coverage_flag",
        ]
    ].copy()


def main() -> None:
    configure_pipeline_logging()
    args = parse_args()
    config_path, cfg = load_macro_raw_config(args.config)
    serving_db_path = resolve_serving_db_path(cfg, config_path, override=args.serving_db_path)

    probability_filter = {str(item).strip().upper() for item in (args.probability_keys or []) if str(item).strip()}
    specs = [item for item in PROBABILITY_SPECS if not probability_filter or item.probability_key in probability_filter]
    if not specs:
        raise ValueError("No probability specs matched the requested build.")

    layer_cfg = cfg_get(cfg, "probability_layer", default={}) or {}
    min_training_months = int(cfg_get(layer_cfg, "calibration_min_months", default=36))
    min_positive_months = int(cfg_get(layer_cfg, "calibration_min_positive_months", default=6))
    min_negative_months = int(cfg_get(layer_cfg, "calibration_min_negative_months", default=6))
    ridge_penalty = float(cfg_get(layer_cfg, "ridge_penalty", default=2.5))
    logloss_clip = float(cfg_get(layer_cfg, "logloss_clip", default=1e-6))
    output_probability_floor = float(cfg_get(layer_cfg, "output_probability_floor", default=0.02))

    conn = connect_sqlite(serving_db_path, row_factory=sqlite3.Row)
    serving_run_id = uuid.uuid4().hex
    run_started = False
    rows_written = 0
    try:
        init_db(conn)
        start_date, end_date = _resolve_composite_bounds(conn, start_override=args.start_date, end_override=args.end_date)
        raw_ingest_run_id = _latest_composite_run_raw_ingest_id(conn)
        composite_keys = sorted({item.source_composite_key for item in specs}.union({item.target_composite_key for item in specs}))
        composite_frame = _load_composite_frame(conn, end_date=end_date.isoformat(), composite_keys=composite_keys)
        value_map, date_map = _build_monthly_snapshot_maps(composite_frame)

        start_serving_run(
            conn,
            serving_run_id=serving_run_id,
            build_step="probability_layer",
            raw_ingest_run_id=raw_ingest_run_id,
            as_of_start_date=start_date.isoformat(),
            as_of_end_date=end_date.isoformat(),
            metric_count=len(specs),
            notes="Building macro probability layer from Stage 5 composites using month-end expanding-window monotonic logistic calibration.",
        )
        run_started = True

        calibration_frames: list[pd.DataFrame] = []
        diagnostics_frames: list[pd.DataFrame] = []
        daily_frames: list[pd.DataFrame] = []
        first_calibration_date: pd.Timestamp | None = None

        logger.info(
            "Building macro probability layer: probabilities=%d as_of_start=%s as_of_end=%s",
            len(specs),
            start_date.isoformat(),
            end_date.isoformat(),
        )
        for spec in specs:
            dataset = _build_monthly_probability_dataset(value_map=value_map, date_map=date_map, spec=spec)
            calibration_frame, diagnostics_frame = _build_calibration_and_diagnostics(
                dataset=dataset,
                spec=spec,
                min_training_months=min_training_months,
                min_positive_months=min_positive_months,
                min_negative_months=min_negative_months,
                ridge_penalty=ridge_penalty,
                logloss_clip=logloss_clip,
                output_probability_floor=output_probability_floor,
            )
            daily_frame = _build_daily_probability_frame(
                source_daily=composite_frame,
                calibration_frame=calibration_frame,
                spec=spec,
                start_date=start_date,
                end_date=end_date,
                output_probability_floor=output_probability_floor,
            )
            if not calibration_frame.empty:
                calibration_frames.append(calibration_frame)
                min_anchor = pd.to_datetime(calibration_frame["calibration_as_of_date"]).min()
                first_calibration_date = min_anchor if first_calibration_date is None else min(first_calibration_date, min_anchor)
            if not diagnostics_frame.empty:
                diagnostics_frames.append(diagnostics_frame)
            daily_frames.append(daily_frame)
            ready_count = int(calibration_frame["calibration_ready_flag"].sum()) if not calibration_frame.empty else 0
            ready_frame = calibration_frame[calibration_frame["calibration_ready_flag"].eq(1)].copy() if not calibration_frame.empty else pd.DataFrame()
            ready_start = (
                pd.to_datetime(ready_frame["calibration_as_of_date"]).min().date().isoformat()
                if not ready_frame.empty
                else "<none>"
            )
            ready_end = (
                pd.to_datetime(ready_frame["calibration_as_of_date"]).max().date().isoformat()
                if not ready_frame.empty
                else "<none>"
            )
            logger.info(
                "Prepared probability spec=%s calibration_rows=%d ready_rows=%d ready_start=%s ready_end=%s daily_rows=%d",
                spec.probability_key,
                len(calibration_frame),
                ready_count,
                ready_start,
                ready_end,
                len(daily_frame),
            )

        calibration_all = pd.concat(calibration_frames, ignore_index=True, sort=False) if calibration_frames else pd.DataFrame()
        diagnostics_all = pd.concat(diagnostics_frames, ignore_index=True, sort=False) if diagnostics_frames else pd.DataFrame()
        daily_all = pd.concat(daily_frames, ignore_index=True, sort=False) if daily_frames else pd.DataFrame()
        calibration_start_date = (
            pd.Timestamp(first_calibration_date).date().isoformat()
            if first_calibration_date is not None
            else start_date.isoformat()
        )
        probability_keys = [item.probability_key for item in specs]

        clear_probability_range(
            conn,
            table_name="macro_probability_calibration",
            date_column="calibration_as_of_date",
            start_date=calibration_start_date,
            end_date=end_date.isoformat(),
            probability_keys=probability_keys if probability_filter else None,
        )
        clear_probability_range(
            conn,
            table_name="macro_probability_diagnostics",
            date_column="calibration_as_of_date",
            start_date=calibration_start_date,
            end_date=end_date.isoformat(),
            probability_keys=probability_keys if probability_filter else None,
        )
        clear_probability_range(
            conn,
            table_name="macro_probabilities_daily",
            date_column="as_of_date",
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
            probability_keys=probability_keys if probability_filter else None,
        )

        calibration_rows = [
            (
                pd.Timestamp(row["calibration_as_of_date"]).date().isoformat(),
                str(row["probability_key"]),
                str(row["source_composite_key"]),
                str(row["target_composite_key"]),
                int(row["target_start_month_offset"]),
                int(row["target_window_months"]),
                float(row["label_threshold"]),
                int(row["training_sample_count"]),
                int(row["positive_sample_count"]),
                int(row["negative_sample_count"]),
                float(row["positive_rate"]) if pd.notna(row["positive_rate"]) else None,
                float(row["predictor_mean"]) if pd.notna(row["predictor_mean"]) else None,
                float(row["predictor_std"]) if pd.notna(row["predictor_std"]) else None,
                float(row["intercept_value"]) if pd.notna(row["intercept_value"]) else None,
                float(row["slope_value"]) if pd.notna(row["slope_value"]) else None,
                int(row["slope_clipped_flag"]),
                int(row["calibration_ready_flag"]),
                utc_now_iso(),
            )
            for _, row in calibration_all.iterrows()
        ]
        diagnostics_rows = [
            (
                pd.Timestamp(row["calibration_as_of_date"]).date().isoformat(),
                str(row["probability_key"]),
                str(row["source_composite_key"]),
                float(row["train_brier_score"]) if pd.notna(row["train_brier_score"]) else None,
                float(row["train_log_loss"]) if pd.notna(row["train_log_loss"]) else None,
                float(row["train_auc"]) if pd.notna(row["train_auc"]) else None,
                float(row["train_probability_p05"]) if pd.notna(row["train_probability_p05"]) else None,
                float(row["train_probability_p50"]) if pd.notna(row["train_probability_p50"]) else None,
                float(row["train_probability_p95"]) if pd.notna(row["train_probability_p95"]) else None,
                float(row["saturation_low_share"]) if pd.notna(row["saturation_low_share"]) else None,
                float(row["saturation_high_share"]) if pd.notna(row["saturation_high_share"]) else None,
                float(row["coefficient_delta_intercept"]) if pd.notna(row["coefficient_delta_intercept"]) else None,
                float(row["coefficient_delta_slope"]) if pd.notna(row["coefficient_delta_slope"]) else None,
                utc_now_iso(),
            )
            for _, row in diagnostics_all.iterrows()
        ]
        daily_rows = [
            (
                pd.Timestamp(row["as_of_date"]).date().isoformat(),
                str(row["probability_key"]),
                str(row["source_composite_key"]),
                float(row["source_composite_value"]) if pd.notna(row["source_composite_value"]) else None,
                float(row["probability_value"]) if pd.notna(row["probability_value"]) else None,
                pd.Timestamp(row["calibration_as_of_date"]).date().isoformat() if pd.notna(row["calibration_as_of_date"]) else None,
                str(row["target_composite_key"]),
                int(row["target_start_month_offset"]),
                int(row["target_window_months"]),
                float(row["label_threshold"]),
                int(row["training_sample_count"]),
                float(row["positive_rate"]) if pd.notna(row["positive_rate"]) else None,
                int(row["coverage_flag"]),
                utc_now_iso(),
            )
            for _, row in daily_all.iterrows()
        ]

        rows_written += insert_many(
            conn,
            sql="""
                INSERT INTO macro_probability_calibration (
                    calibration_as_of_date, probability_key, source_composite_key, target_composite_key,
                    target_start_month_offset, target_window_months, label_threshold, training_sample_count,
                    positive_sample_count, negative_sample_count, positive_rate, predictor_mean, predictor_std,
                    intercept_value, slope_value, slope_clipped_flag, calibration_ready_flag, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows=calibration_rows,
            chunk_size=50000,
        )
        rows_written += insert_many(
            conn,
            sql="""
                INSERT INTO macro_probability_diagnostics (
                    calibration_as_of_date, probability_key, source_composite_key, train_brier_score,
                    train_log_loss, train_auc, train_probability_p05, train_probability_p50, train_probability_p95,
                    saturation_low_share, saturation_high_share, coefficient_delta_intercept, coefficient_delta_slope,
                    updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows=diagnostics_rows,
            chunk_size=50000,
        )
        rows_written += insert_many(
            conn,
            sql="""
                INSERT INTO macro_probabilities_daily (
                    as_of_date, probability_key, source_composite_key, source_composite_value, probability_value,
                    calibration_as_of_date, target_composite_key, target_start_month_offset, target_window_months,
                    label_threshold, training_sample_count, positive_rate, coverage_flag, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows=daily_rows,
            chunk_size=50000,
        )

        finish_serving_run(
            conn,
            serving_run_id=serving_run_id,
            status="completed",
            rows_written=rows_written,
            notes=f"Probability layer built for {len(specs)} probability outputs.",
        )
        logger.info(
            "Macro probability build complete: serving_run_id=%s rows_written=%d probabilities=%d",
            serving_run_id,
            rows_written,
            len(specs),
        )
    except BaseException as exc:
        if run_started:
            fail_notes = f"Probability layer failed: {type(exc).__name__}: {exc}"
            try:
                finish_serving_run(
                    conn,
                    serving_run_id=serving_run_id,
                    status="failed",
                    rows_written=rows_written,
                    notes=fail_notes,
                )
            except Exception:
                logger.exception("Failed to record failed probability layer run for serving_run_id=%s", serving_run_id)
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
