#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
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
    clear_regime_range,
    finish_serving_run,
    init_db,
    insert_many,
    start_serving_run,
)

logger = logging.getLogger(__name__)

REGIME_ORDER = (
    "EXPANSION_DISINFLATION",
    "HEATING_UP",
    "SLOW_GROWTH",
    "STAGFLATION",
)
REGIME_COORDINATES: dict[str, tuple[int, int]] = {
    "EXPANSION_DISINFLATION": (1, 0),
    "HEATING_UP": (1, 1),
    "SLOW_GROWTH": (0, 0),
    "STAGFLATION": (0, 1),
}
RAW_CURRENT_COLUMNS = (
    "p_current_expansion_disinflation",
    "p_current_heating_up",
    "p_current_slow_growth",
    "p_current_stagflation",
)
RAW_NEXT_COLUMNS = (
    "p_next_3m_expansion_disinflation",
    "p_next_3m_heating_up",
    "p_next_3m_slow_growth",
    "p_next_3m_stagflation",
)
SMOOTHED_CURRENT_COLUMNS = (
    "p_smoothed_current_expansion_disinflation",
    "p_smoothed_current_heating_up",
    "p_smoothed_current_slow_growth",
    "p_smoothed_current_stagflation",
)
SMOOTHED_NEXT_COLUMNS = (
    "p_smoothed_next_3m_expansion_disinflation",
    "p_smoothed_next_3m_heating_up",
    "p_smoothed_next_3m_slow_growth",
    "p_smoothed_next_3m_stagflation",
)


@dataclass(frozen=True)
class SmoothingConfig:
    transition_prior_strength: float
    persistence_weight: float
    adjacent_weight: float
    opposite_weight: float
    current_blend: float
    next_blend: float
    transition_bias_deadband: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build the tier-1 smoothed regime layer from Stage 7 raw regime probabilities "
            "using a shrunk transition matrix and persistence-aware filtering."
        )
    )
    parser.add_argument("--config", type=Path, default=None, help="Path to macro raw YAML config.")
    parser.add_argument("--serving-db-path", type=Path, default=None, help="Optional serving SQLite path override.")
    parser.add_argument("--start-date", type=str, default=None, help="Optional smoothed regime start YYYY-MM-DD override.")
    parser.add_argument("--end-date", type=str, default=None, help="Optional smoothed regime end YYYY-MM-DD override.")
    return parser.parse_args()


def _require_probability(value: object, *, label: str) -> float:
    out = float(value)
    if not np.isfinite(out):
        raise ValueError(f"{label} must be finite; got {value!r}.")
    return out


def _require_weight(value: object, *, label: str) -> float:
    out = _require_probability(value, label=label)
    if out <= 0.0:
        raise ValueError(f"{label} must be > 0; got {out}.")
    return out


def _require_unit_interval(value: object, *, label: str) -> float:
    out = _require_probability(value, label=label)
    if out < 0.0 or out > 1.0:
        raise ValueError(f"{label} must be in [0, 1]; got {out}.")
    return out


def _resolve_raw_bounds(
    conn: sqlite3.Connection,
    *,
    start_override: str | None,
    end_override: str | None,
) -> tuple[date, date, date]:
    write_start = parse_iso_date(start_override)
    write_end = parse_iso_date(end_override)
    row = conn.execute(
        """
        SELECT MIN(as_of_date) AS min_as_of_date, MAX(as_of_date) AS max_as_of_date
        FROM macro_regime_raw_daily
        """
    ).fetchone()
    history_start = parse_iso_date(row["min_as_of_date"]) if row is not None else None
    history_end = parse_iso_date(row["max_as_of_date"]) if row is not None else None
    if history_start is None or history_end is None:
        raise ValueError("Unable to resolve smoothed regime build dates from macro_regime_raw_daily.")
    if write_start is None:
        write_start = history_start
    if write_end is None:
        write_end = history_end
    if write_end < write_start:
        raise ValueError(
            f"Smoothed regime end date {write_end.isoformat()} is before start date {write_start.isoformat()}."
        )
    if write_start < history_start:
        raise ValueError(
            f"Smoothed regime start date {write_start.isoformat()} is before available raw regime history "
            f"{history_start.isoformat()}."
        )
    if write_end > history_end:
        raise ValueError(
            f"Smoothed regime end date {write_end.isoformat()} is after available raw regime history "
            f"{history_end.isoformat()}."
        )
    return history_start, write_start, write_end


def _latest_raw_regime_run_raw_ingest_id(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        """
        SELECT raw_ingest_run_id
        FROM macro_serving_run
        WHERE build_step = 'regime_raw_layer'
          AND status = 'completed'
        ORDER BY COALESCE(completed_at_utc, started_at_utc) DESC, rowid DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    return str(row["raw_ingest_run_id"] or "") or None


def _load_raw_regime_frame(
    conn: sqlite3.Connection,
    *,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    frame = pd.read_sql_query(
        f"""
        SELECT
            as_of_date,
            {", ".join(RAW_CURRENT_COLUMNS)},
            {", ".join(RAW_NEXT_COLUMNS)},
            current_regime,
            next_regime,
            current_regime_probability,
            next_regime_probability,
            regime_confidence,
            coverage_flag
        FROM macro_regime_raw_daily
        WHERE as_of_date BETWEEN ? AND ?
        ORDER BY as_of_date
        """,
        conn,
        params=[start_date, end_date],
        parse_dates=["as_of_date"],
    )
    if frame.empty:
        raise ValueError("macro_regime_raw_daily returned no rows for the requested smoothed regime build.")
    return frame


def _normalize_probabilities(values: np.ndarray) -> np.ndarray:
    arr = np.asarray(values, dtype=float)
    arr = np.clip(arr, 0.0, None)
    total = float(arr.sum())
    if not np.isfinite(total) or total <= 0.0:
        return np.ones(len(arr), dtype=float) / float(len(arr))
    return arr / total


def _validate_probability_vector(values: np.ndarray, *, label: str, as_of_date: str) -> np.ndarray:
    arr = np.asarray(values, dtype=float)
    if not np.isfinite(arr).all():
        raise ValueError(f"{label} contains non-finite values on {as_of_date}.")
    sum_value = float(arr.sum())
    if abs(sum_value - 1.0) > 1e-8:
        raise RuntimeError(f"{label} probabilities do not sum to 1 on {as_of_date}; sum={sum_value:.8f}.")
    return arr


def _regime_summary(values: np.ndarray) -> tuple[str, float, float, int]:
    probs = _normalize_probabilities(values)
    idx = int(np.argmax(probs))
    sorted_probs = np.sort(probs)
    confidence = float(sorted_probs[-1] - sorted_probs[-2]) if len(sorted_probs) >= 2 else float("nan")
    return REGIME_ORDER[idx], float(probs[idx]), confidence, idx


def _transition_bias(
    current_probs: np.ndarray,
    next_probs: np.ndarray,
    *,
    deadband: float,
) -> tuple[str, float]:
    current_regime, _, _, current_idx = _regime_summary(current_probs)
    next_regime, _, _, next_idx = _regime_summary(next_probs)
    if current_regime == next_regime:
        strength = float(next_probs[current_idx] - current_probs[current_idx])
        if abs(strength) < deadband:
            return "STABLE", strength
        if strength > 0.0:
            return f"REINFORCE_{current_regime}", strength
        return f"SOFTEN_{current_regime}", strength
    strength = float(next_probs[next_idx] - current_probs[next_idx])
    if abs(strength) < deadband:
        return "STABLE", strength
    return f"TOWARD_{next_regime}", strength


def _build_transition_prior(cfg: SmoothingConfig) -> np.ndarray:
    prior = np.zeros((len(REGIME_ORDER), len(REGIME_ORDER)), dtype=float)
    for row_idx, from_regime in enumerate(REGIME_ORDER):
        gx_from, pi_from = REGIME_COORDINATES[from_regime]
        for col_idx, to_regime in enumerate(REGIME_ORDER):
            gx_to, pi_to = REGIME_COORDINATES[to_regime]
            distance = abs(gx_from - gx_to) + abs(pi_from - pi_to)
            if row_idx == col_idx:
                weight = cfg.persistence_weight
            elif distance == 1:
                weight = cfg.adjacent_weight
            else:
                weight = cfg.opposite_weight
            prior[row_idx, col_idx] = float(weight)
    row_sums = prior.sum(axis=1, keepdims=True)
    if np.any(row_sums <= 0.0):
        raise RuntimeError("Transition prior contains a non-positive row sum.")
    return prior / row_sums


def _empirical_transition_matrix(counts: np.ndarray) -> np.ndarray:
    empirical = np.full(counts.shape, np.nan, dtype=float)
    row_totals = counts.sum(axis=1)
    mask = row_totals > 0
    if np.any(mask):
        empirical[mask, :] = counts[mask, :] / row_totals[mask, None]
    return empirical


def _shrunk_transition_matrix(
    counts: np.ndarray,
    *,
    prior_matrix: np.ndarray,
    prior_strength: float,
) -> np.ndarray:
    shrunk = counts.astype(float) + float(prior_strength) * prior_matrix
    row_sums = shrunk.sum(axis=1, keepdims=True)
    if np.any(row_sums <= 0.0):
        raise RuntimeError("Shrunk transition matrix contains a non-positive row sum.")
    return shrunk / row_sums


def _build_smoothed_outputs(
    raw_frame: pd.DataFrame,
    *,
    write_start_date: date,
    write_end_date: date,
    cfg: SmoothingConfig,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    prior_matrix = _build_transition_prior(cfg)
    transition_counts = np.zeros((len(REGIME_ORDER), len(REGIME_ORDER)), dtype=int)

    smoothed_rows: list[dict[str, object]] = []
    matrix_rows: list[dict[str, object]] = []
    diagnostic_rows: list[dict[str, object]] = []

    state_for_prediction: np.ndarray | None = None
    previous_row_covered = False
    previous_raw_idx: int | None = None
    previous_smoothed_idx: int | None = None

    for row in raw_frame.itertuples(index=False):
        as_of_date = pd.Timestamp(row.as_of_date).date()
        as_of_date_str = as_of_date.isoformat()
        counts_before = transition_counts.copy()
        empirical_matrix = _empirical_transition_matrix(counts_before)
        transition_matrix = _shrunk_transition_matrix(
            counts_before,
            prior_matrix=prior_matrix,
            prior_strength=cfg.transition_prior_strength,
        )
        transition_count_before = int(counts_before.sum())
        predicted_current = (
            _normalize_probabilities(state_for_prediction @ transition_matrix)
            if state_for_prediction is not None
            else None
        )

        raw_current_regime: str | None = None
        raw_next_regime: str | None = None
        raw_current_prob: float | None = None
        raw_next_prob: float | None = None
        raw_confidence: float | None = None
        predicted_current_regime: str | None = None
        predicted_current_prob: float | None = None
        smoothed_current_regime: str | None = None
        smoothed_next_regime: str | None = None
        smoothed_current_prob: float | None = None
        smoothed_next_prob: float | None = None
        smoothed_confidence: float | None = None
        smoothed_transition_bias: str | None = None
        smoothed_transition_bias_strength: float | None = None
        raw_to_smoothed_shift_l1: float | None = None
        next_raw_to_smoothed_shift_l1: float | None = None
        raw_flip_flag = 0
        smoothed_flip_flag = 0
        agreement_flag = 0
        coverage_flag = int(row.coverage_flag)
        smoothed_current_probs = np.full(len(REGIME_ORDER), np.nan, dtype=float)
        smoothed_next_probs = np.full(len(REGIME_ORDER), np.nan, dtype=float)

        if coverage_flag == 1:
            raw_current_probs = _validate_probability_vector(
                np.asarray([getattr(row, col) for col in RAW_CURRENT_COLUMNS], dtype=float),
                label="Stage 7 current regime",
                as_of_date=as_of_date_str,
            )
            raw_next_probs = _validate_probability_vector(
                np.asarray([getattr(row, col) for col in RAW_NEXT_COLUMNS], dtype=float),
                label="Stage 7 next regime",
                as_of_date=as_of_date_str,
            )
            raw_current_regime, raw_current_prob, raw_confidence, raw_current_idx = _regime_summary(raw_current_probs)
            raw_next_regime, raw_next_prob, _, _ = _regime_summary(raw_next_probs)

            if predicted_current is None:
                predicted_current = raw_current_probs.copy()
            predicted_current_regime, predicted_current_prob, _, _ = _regime_summary(predicted_current)

            smoothed_current_probs = _normalize_probabilities(
                (1.0 - cfg.current_blend) * raw_current_probs + cfg.current_blend * predicted_current
            )
            predicted_next = _normalize_probabilities(smoothed_current_probs @ transition_matrix)
            smoothed_next_probs = _normalize_probabilities(
                (1.0 - cfg.next_blend) * raw_next_probs + cfg.next_blend * predicted_next
            )

            smoothed_current_regime, smoothed_current_prob, smoothed_confidence, smoothed_current_idx = _regime_summary(
                smoothed_current_probs
            )
            smoothed_next_regime, smoothed_next_prob, _, _ = _regime_summary(smoothed_next_probs)
            smoothed_transition_bias, smoothed_transition_bias_strength = _transition_bias(
                smoothed_current_probs,
                smoothed_next_probs,
                deadband=cfg.transition_bias_deadband,
            )
            raw_to_smoothed_shift_l1 = float(np.abs(smoothed_current_probs - raw_current_probs).sum())
            next_raw_to_smoothed_shift_l1 = float(np.abs(smoothed_next_probs - raw_next_probs).sum())
            raw_flip_flag = int(previous_row_covered and previous_raw_idx is not None and raw_current_idx != previous_raw_idx)
            smoothed_flip_flag = int(
                previous_row_covered
                and previous_smoothed_idx is not None
                and smoothed_current_idx != previous_smoothed_idx
            )
            agreement_flag = int(smoothed_current_regime == raw_current_regime)

            if previous_row_covered and previous_raw_idx is not None:
                transition_counts[previous_raw_idx, raw_current_idx] += 1
            previous_raw_idx = raw_current_idx
            previous_smoothed_idx = smoothed_current_idx
            previous_row_covered = True
            state_for_prediction = smoothed_current_probs.copy()
        else:
            if predicted_current is not None:
                state_for_prediction = predicted_current.copy()
                predicted_current_regime, predicted_current_prob, _, _ = _regime_summary(predicted_current)
            previous_row_covered = False

        if not (write_start_date <= as_of_date <= write_end_date):
            continue

        for from_idx, from_regime in enumerate(REGIME_ORDER):
            total_from_before = int(counts_before[from_idx, :].sum())
            for to_idx, to_regime in enumerate(REGIME_ORDER):
                empirical_prob = (
                    float(empirical_matrix[from_idx, to_idx])
                    if np.isfinite(empirical_matrix[from_idx, to_idx])
                    else None
                )
                matrix_rows.append(
                    {
                        "as_of_date": as_of_date_str,
                        "from_regime": from_regime,
                        "to_regime": to_regime,
                        "prior_transition_probability": float(prior_matrix[from_idx, to_idx]),
                        "empirical_transition_probability": empirical_prob,
                        "transition_probability": float(transition_matrix[from_idx, to_idx]),
                        "empirical_transition_count": int(counts_before[from_idx, to_idx]),
                        "total_from_count": total_from_before,
                    }
                )

        smoothed_rows.append(
            {
                "as_of_date": as_of_date_str,
                **{
                    column_name: (
                        float(smoothed_current_probs[idx]) if np.isfinite(smoothed_current_probs[idx]) else None
                    )
                    for idx, column_name in enumerate(SMOOTHED_CURRENT_COLUMNS)
                },
                **{
                    column_name: (
                        float(smoothed_next_probs[idx]) if np.isfinite(smoothed_next_probs[idx]) else None
                    )
                    for idx, column_name in enumerate(SMOOTHED_NEXT_COLUMNS)
                },
                "raw_current_regime": raw_current_regime,
                "smoothed_current_regime": smoothed_current_regime,
                "raw_next_regime": raw_next_regime,
                "smoothed_next_regime": smoothed_next_regime,
                "raw_current_regime_probability": raw_current_prob,
                "smoothed_current_regime_probability": smoothed_current_prob,
                "raw_next_regime_probability": raw_next_prob,
                "smoothed_next_regime_probability": smoothed_next_prob,
                "raw_regime_confidence": raw_confidence,
                "smoothed_regime_confidence": smoothed_confidence,
                "smoothed_transition_bias": smoothed_transition_bias,
                "smoothed_transition_bias_strength": smoothed_transition_bias_strength,
                "raw_to_smoothed_shift_l1": raw_to_smoothed_shift_l1,
                "next_raw_to_smoothed_shift_l1": next_raw_to_smoothed_shift_l1,
                "coverage_flag": coverage_flag,
            }
        )
        diagnostic_rows.append(
            {
                "as_of_date": as_of_date_str,
                "transition_count_before": transition_count_before,
                "raw_current_regime": raw_current_regime,
                "predicted_current_regime": predicted_current_regime,
                "smoothed_current_regime": smoothed_current_regime,
                "raw_next_regime": raw_next_regime,
                "smoothed_next_regime": smoothed_next_regime,
                "raw_current_regime_probability": raw_current_prob,
                "predicted_current_regime_probability": predicted_current_prob,
                "smoothed_current_regime_probability": smoothed_current_prob,
                "raw_next_regime_probability": raw_next_prob,
                "smoothed_next_regime_probability": smoothed_next_prob,
                "raw_regime_confidence": raw_confidence,
                "smoothed_regime_confidence": smoothed_confidence,
                "raw_to_smoothed_shift_l1": raw_to_smoothed_shift_l1,
                "next_raw_to_smoothed_shift_l1": next_raw_to_smoothed_shift_l1,
                "raw_flip_flag": raw_flip_flag,
                "smoothed_flip_flag": smoothed_flip_flag,
                "raw_smoothed_agreement_flag": agreement_flag,
                "coverage_flag": coverage_flag,
            }
        )

    return (
        pd.DataFrame(smoothed_rows),
        pd.DataFrame(matrix_rows),
        pd.DataFrame(diagnostic_rows),
    )


def main() -> None:
    configure_pipeline_logging()
    args = parse_args()
    config_path, cfg = load_macro_raw_config(args.config)
    serving_db_path = resolve_serving_db_path(cfg, config_path, override=args.serving_db_path)

    layer_cfg = cfg_get(cfg, "regime_layer", default={}) or {}
    smoothing_cfg = cfg_get(layer_cfg, "smoothing", default={}) or {}
    cfg_obj = SmoothingConfig(
        transition_prior_strength=_require_probability(
            cfg_get(smoothing_cfg, "transition_prior_strength", default=24.0),
            label="regime_layer.smoothing.transition_prior_strength",
        ),
        persistence_weight=_require_weight(
            cfg_get(smoothing_cfg, "persistence_weight", default=6.0),
            label="regime_layer.smoothing.persistence_weight",
        ),
        adjacent_weight=_require_weight(
            cfg_get(smoothing_cfg, "adjacent_weight", default=1.5),
            label="regime_layer.smoothing.adjacent_weight",
        ),
        opposite_weight=_require_weight(
            cfg_get(smoothing_cfg, "opposite_weight", default=0.35),
            label="regime_layer.smoothing.opposite_weight",
        ),
        current_blend=_require_unit_interval(
            cfg_get(smoothing_cfg, "current_blend", default=0.35),
            label="regime_layer.smoothing.current_blend",
        ),
        next_blend=_require_unit_interval(
            cfg_get(smoothing_cfg, "next_blend", default=0.35),
            label="regime_layer.smoothing.next_blend",
        ),
        transition_bias_deadband=_require_unit_interval(
            cfg_get(layer_cfg, "transition_bias_deadband", default=0.05),
            label="regime_layer.transition_bias_deadband",
        ),
    )
    if cfg_obj.transition_prior_strength < 0.0:
        raise ValueError("regime_layer.smoothing.transition_prior_strength must be >= 0.")

    conn = connect_sqlite(serving_db_path, row_factory=sqlite3.Row)
    serving_run_id = uuid.uuid4().hex
    run_started = False
    rows_written = 0
    try:
        init_db(conn)
        history_start, write_start, write_end = _resolve_raw_bounds(
            conn,
            start_override=args.start_date,
            end_override=args.end_date,
        )
        raw_ingest_run_id = _latest_raw_regime_run_raw_ingest_id(conn)

        start_serving_run(
            conn,
            serving_run_id=serving_run_id,
            build_step="regime_smoothed_layer",
            raw_ingest_run_id=raw_ingest_run_id,
            as_of_start_date=write_start.isoformat(),
            as_of_end_date=write_end.isoformat(),
            metric_count=len(REGIME_ORDER),
            notes=(
                "Building the Stage 8 smoothed macro regime layer with a shrunk transition matrix "
                "and persistence-aware filtering."
            ),
        )
        run_started = True

        raw_frame = _load_raw_regime_frame(
            conn,
            start_date=history_start.isoformat(),
            end_date=write_end.isoformat(),
        )
        smoothed_frame, matrix_frame, diagnostic_frame = _build_smoothed_outputs(
            raw_frame,
            write_start_date=write_start,
            write_end_date=write_end,
            cfg=cfg_obj,
        )

        clear_regime_range(
            conn,
            table_name="macro_regime_smoothed_daily",
            start_date=write_start.isoformat(),
            end_date=write_end.isoformat(),
        )
        clear_regime_range(
            conn,
            table_name="macro_transition_matrix",
            start_date=write_start.isoformat(),
            end_date=write_end.isoformat(),
        )
        clear_regime_range(
            conn,
            table_name="macro_transition_diagnostics",
            start_date=write_start.isoformat(),
            end_date=write_end.isoformat(),
        )

        updated_at = utc_now_iso()
        smoothed_rows = [
            (
                row["as_of_date"],
                row["p_smoothed_current_expansion_disinflation"],
                row["p_smoothed_current_heating_up"],
                row["p_smoothed_current_slow_growth"],
                row["p_smoothed_current_stagflation"],
                row["p_smoothed_next_3m_expansion_disinflation"],
                row["p_smoothed_next_3m_heating_up"],
                row["p_smoothed_next_3m_slow_growth"],
                row["p_smoothed_next_3m_stagflation"],
                row["raw_current_regime"],
                row["smoothed_current_regime"],
                row["raw_next_regime"],
                row["smoothed_next_regime"],
                row["raw_current_regime_probability"],
                row["smoothed_current_regime_probability"],
                row["raw_next_regime_probability"],
                row["smoothed_next_regime_probability"],
                row["raw_regime_confidence"],
                row["smoothed_regime_confidence"],
                row["smoothed_transition_bias"],
                row["smoothed_transition_bias_strength"],
                row["raw_to_smoothed_shift_l1"],
                row["next_raw_to_smoothed_shift_l1"],
                int(row["coverage_flag"]),
                updated_at,
            )
            for _, row in smoothed_frame.iterrows()
        ]
        matrix_rows = [
            (
                row["as_of_date"],
                row["from_regime"],
                row["to_regime"],
                row["prior_transition_probability"],
                row["empirical_transition_probability"],
                row["transition_probability"],
                int(row["empirical_transition_count"]),
                int(row["total_from_count"]),
                updated_at,
            )
            for _, row in matrix_frame.iterrows()
        ]
        diagnostic_rows = [
            (
                row["as_of_date"],
                int(row["transition_count_before"]),
                row["raw_current_regime"],
                row["predicted_current_regime"],
                row["smoothed_current_regime"],
                row["raw_next_regime"],
                row["smoothed_next_regime"],
                row["raw_current_regime_probability"],
                row["predicted_current_regime_probability"],
                row["smoothed_current_regime_probability"],
                row["raw_next_regime_probability"],
                row["smoothed_next_regime_probability"],
                row["raw_regime_confidence"],
                row["smoothed_regime_confidence"],
                row["raw_to_smoothed_shift_l1"],
                row["next_raw_to_smoothed_shift_l1"],
                int(row["raw_flip_flag"]),
                int(row["smoothed_flip_flag"]),
                int(row["raw_smoothed_agreement_flag"]),
                int(row["coverage_flag"]),
                updated_at,
            )
            for _, row in diagnostic_frame.iterrows()
        ]

        rows_written += insert_many(
            conn,
            """
            INSERT INTO macro_regime_smoothed_daily (
                as_of_date,
                p_smoothed_current_expansion_disinflation,
                p_smoothed_current_heating_up,
                p_smoothed_current_slow_growth,
                p_smoothed_current_stagflation,
                p_smoothed_next_3m_expansion_disinflation,
                p_smoothed_next_3m_heating_up,
                p_smoothed_next_3m_slow_growth,
                p_smoothed_next_3m_stagflation,
                raw_current_regime,
                smoothed_current_regime,
                raw_next_regime,
                smoothed_next_regime,
                raw_current_regime_probability,
                smoothed_current_regime_probability,
                raw_next_regime_probability,
                smoothed_next_regime_probability,
                raw_regime_confidence,
                smoothed_regime_confidence,
                smoothed_transition_bias,
                smoothed_transition_bias_strength,
                raw_to_smoothed_shift_l1,
                next_raw_to_smoothed_shift_l1,
                coverage_flag,
                updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            smoothed_rows,
        )
        rows_written += insert_many(
            conn,
            """
            INSERT INTO macro_transition_matrix (
                as_of_date,
                from_regime,
                to_regime,
                prior_transition_probability,
                empirical_transition_probability,
                transition_probability,
                empirical_transition_count,
                total_from_count,
                updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            matrix_rows,
            chunk_size=50_000,
        )
        rows_written += insert_many(
            conn,
            """
            INSERT INTO macro_transition_diagnostics (
                as_of_date,
                transition_count_before,
                raw_current_regime,
                predicted_current_regime,
                smoothed_current_regime,
                raw_next_regime,
                smoothed_next_regime,
                raw_current_regime_probability,
                predicted_current_regime_probability,
                smoothed_current_regime_probability,
                raw_next_regime_probability,
                smoothed_next_regime_probability,
                raw_regime_confidence,
                smoothed_regime_confidence,
                raw_to_smoothed_shift_l1,
                next_raw_to_smoothed_shift_l1,
                raw_flip_flag,
                smoothed_flip_flag,
                raw_smoothed_agreement_flag,
                coverage_flag,
                updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            diagnostic_rows,
        )

        covered = smoothed_frame[smoothed_frame["coverage_flag"].eq(1)].copy()
        current_counts = covered["smoothed_current_regime"].value_counts(dropna=True).to_dict() if not covered.empty else {}
        diag_covered = diagnostic_frame[diagnostic_frame["coverage_flag"].eq(1)].copy()
        raw_flip_count = int(diag_covered["raw_flip_flag"].sum()) if not diag_covered.empty else 0
        smoothed_flip_count = int(diag_covered["smoothed_flip_flag"].sum()) if not diag_covered.empty else 0
        agreement_share = float(diag_covered["raw_smoothed_agreement_flag"].mean()) if not diag_covered.empty else float("nan")
        logger.info(
            "Built smoothed regime layer: rows=%d covered_rows=%d raw_flips=%d smoothed_flips=%d agreement_share=%.3f current_regimes=%s",
            len(smoothed_frame),
            int(len(covered)),
            raw_flip_count,
            smoothed_flip_count,
            agreement_share,
            current_counts,
        )

        finish_serving_run(
            conn,
            serving_run_id=serving_run_id,
            status="completed",
            rows_written=rows_written,
            notes="Stage 8 smoothed regime layer built from raw regime probabilities.",
        )
        logger.info(
            "Macro smoothed regime build complete: serving_run_id=%s rows_written=%d write_start=%s write_end=%s warmup_start=%s",
            serving_run_id,
            rows_written,
            write_start.isoformat(),
            write_end.isoformat(),
            history_start.isoformat(),
        )
    except BaseException as exc:
        if run_started:
            fail_notes = f"Smoothed regime layer failed: {type(exc).__name__}: {exc}"
            try:
                finish_serving_run(
                    conn,
                    serving_run_id=serving_run_id,
                    status="failed",
                    rows_written=rows_written,
                    notes=fail_notes,
                )
            except Exception:
                logger.exception("Failed to record failed smoothed regime run for serving_run_id=%s", serving_run_id)
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
