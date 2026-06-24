#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import sqlite3
import uuid
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

REQUIRED_PROBABILITY_KEYS = ("P_G_NOW", "P_G_LEAD", "P_PI_NOW", "P_PI_LEAD")
REGIME_ORDER = (
    "EXPANSION_DISINFLATION",
    "HEATING_UP",
    "SLOW_GROWTH",
    "STAGFLATION",
)
CURRENT_REGIME_COLUMNS = (
    "p_current_expansion_disinflation",
    "p_current_heating_up",
    "p_current_slow_growth",
    "p_current_stagflation",
)
NEXT_REGIME_COLUMNS = (
    "p_next_3m_expansion_disinflation",
    "p_next_3m_heating_up",
    "p_next_3m_slow_growth",
    "p_next_3m_stagflation",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the tier-1 raw regime layer from calibrated macro probabilities.")
    parser.add_argument("--config", type=Path, default=None, help="Path to macro raw YAML config.")
    parser.add_argument("--serving-db-path", type=Path, default=None, help="Optional serving SQLite path override.")
    parser.add_argument("--start-date", type=str, default=None, help="Optional raw regime start YYYY-MM-DD override.")
    parser.add_argument("--end-date", type=str, default=None, help="Optional raw regime end YYYY-MM-DD override.")
    return parser.parse_args()


def _resolve_probability_bounds(
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
        FROM macro_probabilities_daily
        """
    ).fetchone()
    if start_date is None and row is not None:
        start_date = parse_iso_date(row["min_as_of_date"])
    if end_date is None and row is not None:
        end_date = parse_iso_date(row["max_as_of_date"])
    if start_date is None or end_date is None:
        raise ValueError("Unable to resolve raw regime build dates from macro_probabilities_daily.")
    if end_date < start_date:
        raise ValueError(f"Raw regime end date {end_date.isoformat()} is before start date {start_date.isoformat()}.")
    return start_date, end_date


def _latest_probability_run_raw_ingest_id(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        """
        SELECT raw_ingest_run_id
        FROM macro_serving_run
        WHERE build_step = 'probability_layer'
          AND status = 'completed'
        ORDER BY COALESCE(completed_at_utc, started_at_utc) DESC, rowid DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    return str(row["raw_ingest_run_id"] or "") or None


def _load_probability_frame(
    conn: sqlite3.Connection,
    *,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    placeholders = ",".join("?" for _ in REQUIRED_PROBABILITY_KEYS)
    query = f"""
        SELECT
            as_of_date,
            probability_key,
            probability_value,
            coverage_flag
        FROM macro_probabilities_daily
        WHERE as_of_date BETWEEN ? AND ?
          AND probability_key IN ({placeholders})
        ORDER BY as_of_date, probability_key
    """
    frame = pd.read_sql_query(
        query,
        conn,
        params=[start_date, end_date, *REQUIRED_PROBABILITY_KEYS],
        parse_dates=["as_of_date"],
    )
    if frame.empty:
        raise ValueError("macro_probabilities_daily returned no rows for the requested raw regime build.")
    return frame


def _build_regime_frame(
    probability_frame: pd.DataFrame,
    *,
    transition_bias_deadband: float,
) -> pd.DataFrame:
    value_map = (
        probability_frame.pivot(index="as_of_date", columns="probability_key", values="probability_value")
        .sort_index()
    )
    coverage_map = (
        probability_frame.pivot(index="as_of_date", columns="probability_key", values="coverage_flag")
        .sort_index()
    )
    missing_keys = [key for key in REQUIRED_PROBABILITY_KEYS if key not in value_map.columns]
    if missing_keys:
        raise ValueError(f"Missing required probability history for Stage 7 build: {missing_keys}")

    out = pd.DataFrame(index=value_map.index.copy())
    for key in REQUIRED_PROBABILITY_KEYS:
        out[key.lower()] = pd.to_numeric(value_map[key], errors="coerce")
    input_cols = [key.lower() for key in REQUIRED_PROBABILITY_KEYS]
    coverage_ready = coverage_map.reindex(columns=REQUIRED_PROBABILITY_KEYS).fillna(0).astype(int)
    out["coverage_flag"] = (
        coverage_ready.eq(1).all(axis=1)
        & out[input_cols].notna().all(axis=1)
    ).astype(int)

    float_cols = list(CURRENT_REGIME_COLUMNS) + list(NEXT_REGIME_COLUMNS) + [
        "current_regime_probability",
        "next_regime_probability",
        "regime_confidence",
        "transition_bias_strength",
    ]
    for col in float_cols:
        out[col] = np.nan
    out["current_regime"] = None
    out["next_regime"] = None
    out["transition_bias"] = None

    covered_mask = out["coverage_flag"].eq(1)
    if not bool(covered_mask.any()):
        return out.reset_index(names="as_of_date")

    g_now = out.loc[covered_mask, "p_g_now"].to_numpy(dtype=float)
    g_lead = out.loc[covered_mask, "p_g_lead"].to_numpy(dtype=float)
    pi_now = out.loc[covered_mask, "p_pi_now"].to_numpy(dtype=float)
    pi_lead = out.loc[covered_mask, "p_pi_lead"].to_numpy(dtype=float)

    current_matrix = np.column_stack(
        [
            g_now * (1.0 - pi_now),
            g_now * pi_now,
            (1.0 - g_now) * (1.0 - pi_now),
            (1.0 - g_now) * pi_now,
        ]
    )
    next_matrix = np.column_stack(
        [
            g_lead * (1.0 - pi_lead),
            g_lead * pi_lead,
            (1.0 - g_lead) * (1.0 - pi_lead),
            (1.0 - g_lead) * pi_lead,
        ]
    )

    current_sum_dev = np.abs(current_matrix.sum(axis=1) - 1.0)
    next_sum_dev = np.abs(next_matrix.sum(axis=1) - 1.0)
    if float(np.max(current_sum_dev)) > 1e-8 or float(np.max(next_sum_dev)) > 1e-8:
        raise RuntimeError(
            "Stage 7 regime probabilities do not sum to 1 within tolerance. "
            f"max_current_dev={float(np.max(current_sum_dev)):.3e} max_next_dev={float(np.max(next_sum_dev)):.3e}"
        )

    regime_names = np.asarray(REGIME_ORDER, dtype=object)
    current_idx = np.argmax(current_matrix, axis=1)
    next_idx = np.argmax(next_matrix, axis=1)
    current_regime = regime_names[current_idx]
    next_regime = regime_names[next_idx]
    current_top = current_matrix[np.arange(len(current_matrix)), current_idx]
    next_top = next_matrix[np.arange(len(next_matrix)), next_idx]
    current_sorted = np.sort(current_matrix, axis=1)
    regime_confidence = current_sorted[:, -1] - current_sorted[:, -2]

    current_same_gain = next_matrix[np.arange(len(next_matrix)), current_idx] - current_matrix[np.arange(len(current_matrix)), current_idx]
    next_regime_gain = next_matrix[np.arange(len(next_matrix)), next_idx] - current_matrix[np.arange(len(current_matrix)), next_idx]
    transition_bias_strength = np.where(current_regime == next_regime, current_same_gain, next_regime_gain)

    transition_bias: list[str] = []
    for i in range(len(current_regime)):
        if abs(float(transition_bias_strength[i])) < float(transition_bias_deadband):
            transition_bias.append("STABLE")
        elif current_regime[i] == next_regime[i]:
            if float(transition_bias_strength[i]) > 0.0:
                transition_bias.append(f"REINFORCE_{current_regime[i]}")
            else:
                transition_bias.append(f"SOFTEN_{current_regime[i]}")
        else:
            transition_bias.append(f"TOWARD_{next_regime[i]}")

    for idx, column_name in enumerate(CURRENT_REGIME_COLUMNS):
        out.loc[covered_mask, column_name] = current_matrix[:, idx]
    for idx, column_name in enumerate(NEXT_REGIME_COLUMNS):
        out.loc[covered_mask, column_name] = next_matrix[:, idx]
    out.loc[covered_mask, "current_regime"] = current_regime.tolist()
    out.loc[covered_mask, "next_regime"] = next_regime.tolist()
    out.loc[covered_mask, "current_regime_probability"] = current_top
    out.loc[covered_mask, "next_regime_probability"] = next_top
    out.loc[covered_mask, "regime_confidence"] = regime_confidence
    out.loc[covered_mask, "transition_bias"] = transition_bias
    out.loc[covered_mask, "transition_bias_strength"] = transition_bias_strength

    return out.reset_index(names="as_of_date")


def main() -> None:
    configure_pipeline_logging()
    args = parse_args()
    config_path, cfg = load_macro_raw_config(args.config)
    serving_db_path = resolve_serving_db_path(cfg, config_path, override=args.serving_db_path)

    layer_cfg = cfg_get(cfg, "regime_layer", default={}) or {}
    transition_bias_deadband = float(cfg_get(layer_cfg, "transition_bias_deadband", default=0.05))

    conn = connect_sqlite(serving_db_path, row_factory=sqlite3.Row)
    serving_run_id = uuid.uuid4().hex
    run_started = False
    rows_written = 0
    try:
        init_db(conn)
        start_date, end_date = _resolve_probability_bounds(conn, start_override=args.start_date, end_override=args.end_date)
        raw_ingest_run_id = _latest_probability_run_raw_ingest_id(conn)
        probability_frame = _load_probability_frame(
            conn,
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
        )
        regime_frame = _build_regime_frame(
            probability_frame,
            transition_bias_deadband=transition_bias_deadband,
        )

        start_serving_run(
            conn,
            serving_run_id=serving_run_id,
            build_step="regime_raw_layer",
            raw_ingest_run_id=raw_ingest_run_id,
            as_of_start_date=start_date.isoformat(),
            as_of_end_date=end_date.isoformat(),
            metric_count=len(REGIME_ORDER),
            notes="Building raw 4-state macro regime probabilities from Stage 6 probability outputs.",
        )
        run_started = True

        clear_regime_range(
            conn,
            table_name="macro_regime_raw_daily",
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
        )

        rows = [
            (
                pd.Timestamp(row["as_of_date"]).date().isoformat(),
                float(row["p_g_now"]) if pd.notna(row["p_g_now"]) else None,
                float(row["p_g_lead"]) if pd.notna(row["p_g_lead"]) else None,
                float(row["p_pi_now"]) if pd.notna(row["p_pi_now"]) else None,
                float(row["p_pi_lead"]) if pd.notna(row["p_pi_lead"]) else None,
                float(row["p_current_expansion_disinflation"]) if pd.notna(row["p_current_expansion_disinflation"]) else None,
                float(row["p_current_heating_up"]) if pd.notna(row["p_current_heating_up"]) else None,
                float(row["p_current_slow_growth"]) if pd.notna(row["p_current_slow_growth"]) else None,
                float(row["p_current_stagflation"]) if pd.notna(row["p_current_stagflation"]) else None,
                float(row["p_next_3m_expansion_disinflation"]) if pd.notna(row["p_next_3m_expansion_disinflation"]) else None,
                float(row["p_next_3m_heating_up"]) if pd.notna(row["p_next_3m_heating_up"]) else None,
                float(row["p_next_3m_slow_growth"]) if pd.notna(row["p_next_3m_slow_growth"]) else None,
                float(row["p_next_3m_stagflation"]) if pd.notna(row["p_next_3m_stagflation"]) else None,
                str(row["current_regime"]) if pd.notna(row["current_regime"]) and row["current_regime"] is not None else None,
                str(row["next_regime"]) if pd.notna(row["next_regime"]) and row["next_regime"] is not None else None,
                float(row["current_regime_probability"]) if pd.notna(row["current_regime_probability"]) else None,
                float(row["next_regime_probability"]) if pd.notna(row["next_regime_probability"]) else None,
                float(row["regime_confidence"]) if pd.notna(row["regime_confidence"]) else None,
                str(row["transition_bias"]) if pd.notna(row["transition_bias"]) and row["transition_bias"] is not None else None,
                float(row["transition_bias_strength"]) if pd.notna(row["transition_bias_strength"]) else None,
                int(row["coverage_flag"]),
                utc_now_iso(),
            )
            for _, row in regime_frame.iterrows()
        ]
        rows_written = insert_many(
            conn,
            """
            INSERT INTO macro_regime_raw_daily (
                as_of_date, p_g_now, p_g_lead, p_pi_now, p_pi_lead,
                p_current_expansion_disinflation, p_current_heating_up, p_current_slow_growth, p_current_stagflation,
                p_next_3m_expansion_disinflation, p_next_3m_heating_up, p_next_3m_slow_growth, p_next_3m_stagflation,
                current_regime, next_regime, current_regime_probability, next_regime_probability,
                regime_confidence, transition_bias, transition_bias_strength, coverage_flag, updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

        covered = regime_frame[regime_frame["coverage_flag"].eq(1)].copy()
        current_counts = covered["current_regime"].value_counts(dropna=True).to_dict() if not covered.empty else {}
        next_counts = covered["next_regime"].value_counts(dropna=True).to_dict() if not covered.empty else {}
        logger.info(
            "Built raw regime layer: rows=%d covered_rows=%d current_regimes=%s next_regimes=%s",
            len(regime_frame),
            int(len(covered)),
            current_counts,
            next_counts,
        )

        finish_serving_run(
            conn,
            serving_run_id=serving_run_id,
            status="completed",
            rows_written=rows_written,
            notes="Raw regime layer built from probability outputs.",
        )
        logger.info(
            "Macro raw regime build complete: serving_run_id=%s rows_written=%d",
            serving_run_id,
            rows_written,
        )
    except BaseException as exc:
        if run_started:
            fail_notes = f"Raw regime layer failed: {type(exc).__name__}: {exc}"
            try:
                finish_serving_run(
                    conn,
                    serving_run_id=serving_run_id,
                    status="failed",
                    rows_written=rows_written,
                    notes=fail_notes,
                )
            except Exception:
                logger.exception("Failed to record failed raw regime layer run for serving_run_id=%s", serving_run_id)
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
