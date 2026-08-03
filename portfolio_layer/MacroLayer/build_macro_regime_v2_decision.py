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
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from build_macro_regime_decision import DecisionConfig, _build_decision_frame
from build_macro_regime_smoothed import SmoothingConfig, _build_smoothed_outputs
from macro_probability_v2 import MODEL_VERSION_DEFAULT, REGIME_ORDER
from macro_raw_config import (
    cfg_get,
    configure_pipeline_logging,
    connect_sqlite,
    load_macro_raw_config,
    parse_boolish,
    parse_iso_date,
    resolve_path,
    utc_now_iso,
)
from macro_serving_common import resolve_serving_db_path
from macro_serving_storage import finish_serving_run, init_db, start_serving_run

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build smoothed and hysteresis-controlled v2 regime decisions.")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--serving-db-path", type=Path, default=None)
    parser.add_argument("--start-date", type=str, default=None)
    parser.add_argument("--end-date", type=str, default=None)
    parser.add_argument("--model-version", type=str, default=None)
    parser.add_argument("--layer-block", type=str, default="probability_v2",
                        help="Config block for this candidate (probability_v2 | probability_v2_1).")
    return parser.parse_args()


def _finite_float(value: Any, *, label: str, minimum: float | None = None, maximum: float | None = None) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be numeric; got {value!r}.") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{label} must be finite; got {value!r}.")
    if minimum is not None and parsed < minimum:
        raise ValueError(f"{label} must be >= {minimum}; got {parsed}.")
    if maximum is not None and parsed > maximum:
        raise ValueError(f"{label} must be <= {maximum}; got {parsed}.")
    return parsed


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


def _resolve_bounds(
    conn: sqlite3.Connection,
    *,
    model_version: str,
    start_override: str | None,
    end_override: str | None,
) -> tuple[date, date, date]:
    row = conn.execute(
        """
        SELECT MIN(as_of_date) AS min_date, MAX(as_of_date) AS max_date
        FROM macro_regime_v2_daily
        WHERE model_version = ?
        """,
        (model_version,),
    ).fetchone()
    history_start = parse_iso_date(None if row is None else row["min_date"])
    history_end = parse_iso_date(None if row is None else row["max_date"])
    if history_start is None or history_end is None:
        raise ValueError(f"No raw v2 regime history exists for model_version={model_version}.")
    write_start = parse_iso_date(start_override) or history_start
    write_end = parse_iso_date(end_override) or history_end
    if write_start < history_start or write_end > history_end or write_end < write_start:
        raise ValueError(
            f"Invalid v2 decision range history={history_start}..{history_end} write={write_start}..{write_end}."
        )
    return history_start, write_start, write_end


def _load_raw_frame(
    conn: sqlite3.Connection,
    *,
    model_version: str,
    start_date: date,
    end_date: date,
) -> pd.DataFrame:
    frame = pd.read_sql_query(
        """
        SELECT
            as_of_date,
            p_current_expansion_disinflation,
            p_current_heating_up,
            p_current_slow_growth,
            p_current_stagflation,
            p_next_expansion_disinflation AS p_next_3m_expansion_disinflation,
            p_next_heating_up AS p_next_3m_heating_up,
            p_next_slow_growth AS p_next_3m_slow_growth,
            p_next_stagflation AS p_next_3m_stagflation,
            current_regime,
            next_regime,
            current_regime_probability,
            next_regime_probability,
            current_regime_confidence AS regime_confidence,
            coverage_flag
        FROM macro_regime_v2_daily
        WHERE model_version = ? AND as_of_date BETWEEN ? AND ?
        ORDER BY as_of_date
        """,
        conn,
        params=[model_version, start_date.isoformat(), end_date.isoformat()],
        parse_dates=["as_of_date"],
    )
    if frame.empty:
        raise ValueError("Raw v2 regime query returned no rows.")
    return frame


def _load_configs(cfg: dict[str, Any]) -> tuple[SmoothingConfig, DecisionConfig]:
    regime_cfg = cfg_get(cfg, "regime_layer", default={}) or {}
    smoothing = cfg_get(regime_cfg, "smoothing", default={}) or {}
    decision = cfg_get(regime_cfg, "decision", default={}) or {}
    smoothing_config = SmoothingConfig(
        transition_prior_strength=_finite_float(
            cfg_get(smoothing, "transition_prior_strength", default=24.0),
            label="transition_prior_strength",
            minimum=0.0,
        ),
        persistence_weight=_finite_float(
            cfg_get(smoothing, "persistence_weight", default=6.0), label="persistence_weight", minimum=1e-12
        ),
        adjacent_weight=_finite_float(
            cfg_get(smoothing, "adjacent_weight", default=1.5), label="adjacent_weight", minimum=1e-12
        ),
        opposite_weight=_finite_float(
            cfg_get(smoothing, "opposite_weight", default=0.35), label="opposite_weight", minimum=1e-12
        ),
        current_blend=_finite_float(
            cfg_get(smoothing, "current_blend", default=0.35), label="current_blend", minimum=0.0, maximum=1.0
        ),
        next_blend=_finite_float(
            cfg_get(smoothing, "next_blend", default=0.35), label="next_blend", minimum=0.0, maximum=1.0
        ),
        next_horizon_steps=int(cfg_get(smoothing, "next_horizon_steps", default=63)),
        transition_bias_deadband=_finite_float(
            cfg_get(regime_cfg, "transition_bias_deadband", default=0.05),
            label="transition_bias_deadband",
            minimum=0.0,
            maximum=1.0,
        ),
    )
    if smoothing_config.next_horizon_steps < 1:
        raise ValueError("regime_layer.smoothing.next_horizon_steps must be >= 1.")
    confirm_periods = int(cfg_get(decision, "confirm_periods", default=2))
    if confirm_periods < 1:
        raise ValueError("regime_layer.decision.confirm_periods must be >= 1.")
    decision_config = DecisionConfig(
        decision_frequency=str(cfg_get(decision, "decision_frequency", default="W-FRI")).strip() or "W-FRI",
        min_top_probability=_finite_float(
            cfg_get(decision, "min_top_probability", default=0.50),
            label="min_top_probability",
            minimum=0.0,
            maximum=1.0,
        ),
        min_confidence=_finite_float(
            cfg_get(decision, "min_confidence", default=0.10),
            label="min_confidence",
            minimum=0.0,
            maximum=1.0,
        ),
        switch_margin=_finite_float(
            cfg_get(decision, "switch_margin", default=0.05),
            label="switch_margin",
            minimum=0.0,
            maximum=1.0,
        ),
        confirm_periods=confirm_periods,
        min_incumbent_probability=_finite_float(
            cfg_get(decision, "min_incumbent_probability", default=0.15),
            label="min_incumbent_probability",
            minimum=0.0,
            maximum=1.0,
        ),
        incumbent_breach_periods=int(cfg_get(decision, "incumbent_breach_periods", default=2)),
    )
    if decision_config.incumbent_breach_periods < 1:
        raise ValueError("regime_layer.decision.incumbent_breach_periods must be >= 1.")
    return smoothing_config, decision_config


def _replace_rows(
    conn: sqlite3.Connection,
    *,
    model_version: str,
    write_start: date,
    write_end: date,
    smoothed: pd.DataFrame,
    matrix: pd.DataFrame,
    diagnostics: pd.DataFrame,
    decisions: pd.DataFrame,
) -> int:
    now = utc_now_iso()
    smoothed_rows = [
        (
            model_version,
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
            now,
        )
        for _, row in smoothed.iterrows()
    ]
    matrix_rows = [
        (
            model_version,
            row["as_of_date"],
            row["from_regime"],
            row["to_regime"],
            row["prior_transition_probability"],
            row["empirical_transition_probability"],
            row["transition_probability"],
            int(row["empirical_transition_count"]),
            int(row["total_from_count"]),
            now,
        )
        for _, row in matrix.iterrows()
    ]
    diagnostic_rows = [
        (
            model_version,
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
            now,
        )
        for _, row in diagnostics.iterrows()
    ]
    decision_rows = [
        (
            model_version,
            row["as_of_date"],
            int(row["decision_date_flag"]),
            row["smoothed_current_regime"],
            row["smoothed_next_regime"],
            row["active_current_regime"],
            row["active_next_regime"],
            row["current_top_probability"],
            row["next_top_probability"],
            row["current_confidence"],
            row["next_confidence"],
            row["current_switch_margin"],
            row["next_switch_margin"],
            int(row["current_switch_flag"]),
            int(row["next_switch_flag"]),
            int(row["regime_switch_flag"]),
            row["current_pending_regime"],
            row["next_pending_regime"],
            int(row["current_pending_count"]),
            int(row["next_pending_count"]),
            int(row["regime_switch_pending_flag"]),
            row["regime_override_reason"],
            int(row["coverage_flag"]),
            now,
        )
        for _, row in decisions.iterrows()
    ]

    conn.execute("BEGIN IMMEDIATE")
    try:
        for table in (
            "macro_regime_v2_smoothed_daily",
            "macro_transition_v2_matrix",
            "macro_transition_v2_diagnostics",
            "macro_regime_v2_decision_daily",
        ):
            conn.execute(
                f"DELETE FROM {table} WHERE model_version = ? AND as_of_date BETWEEN ? AND ?",
                (model_version, write_start.isoformat(), write_end.isoformat()),
            )
        conn.executemany(
            """
            INSERT INTO macro_regime_v2_smoothed_daily (
                model_version,
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
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            smoothed_rows,
        )
        conn.executemany(
            """
            INSERT INTO macro_transition_v2_matrix (
                model_version,
                as_of_date,
                from_regime,
                to_regime,
                prior_transition_probability,
                empirical_transition_probability,
                transition_probability,
                empirical_transition_count,
                total_from_count,
                updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            matrix_rows,
        )
        conn.executemany(
            """
            INSERT INTO macro_transition_v2_diagnostics (
                model_version,
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
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            diagnostic_rows,
        )
        conn.executemany(
            """
            INSERT INTO macro_regime_v2_decision_daily (
                model_version,
                as_of_date,
                decision_date_flag,
                smoothed_current_regime,
                smoothed_next_regime,
                active_current_regime,
                active_next_regime,
                current_top_probability,
                next_top_probability,
                current_confidence,
                next_confidence,
                current_switch_margin,
                next_switch_margin,
                current_switch_flag,
                next_switch_flag,
                regime_switch_flag,
                current_pending_regime,
                next_pending_regime,
                current_pending_count,
                next_pending_count,
                regime_switch_pending_flag,
                regime_override_reason,
                coverage_flag,
                updated_at_utc
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            decision_rows,
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return len(smoothed_rows) + len(matrix_rows) + len(diagnostic_rows) + len(decision_rows)


def main() -> None:
    configure_pipeline_logging()
    args = parse_args()
    config_path, cfg = load_macro_raw_config(args.config)
    layer_block = str(args.layer_block or "probability_v2").strip()
    v2_cfg = cfg_get(cfg, layer_block, default={}) or {}
    if not v2_cfg:
        raise ValueError(f"Config block {layer_block!r} is missing or empty.")
    if not parse_boolish(cfg_get(v2_cfg, "shadow_only", default=None), default=False):
        raise ValueError(f"V2 decision build requires {layer_block}.shadow_only=true before promotion.")
    model_version = str(args.model_version or cfg_get(v2_cfg, "model_version", default=MODEL_VERSION_DEFAULT)).strip()
    serving_db_path = resolve_serving_db_path(cfg, config_path, override=args.serving_db_path)
    conn = connect_sqlite(serving_db_path, row_factory=sqlite3.Row)
    serving_run_id = uuid.uuid4().hex
    run_started = False
    rows_written = 0
    try:
        init_db(conn)
        history_start, write_start, write_end = _resolve_bounds(
            conn,
            model_version=model_version,
            start_override=args.start_date,
            end_override=args.end_date,
        )
        if write_start != history_start:
            logger.info(
                "Ignoring partial v2 decision start %s; path-dependent state is rebuilt from %s.",
                write_start,
                history_start,
            )
            write_start = history_start
        smoothing_config, decision_config = _load_configs(cfg)
        start_serving_run(
            conn,
            serving_run_id=serving_run_id,
            build_step="regime_v2_decision_research",
            raw_ingest_run_id=None,
            as_of_start_date=write_start.isoformat(),
            as_of_end_date=write_end.isoformat(),
            metric_count=len(REGIME_ORDER),
            notes=f"Shadow v2 smoothing and decision model_version={model_version}.",
        )
        run_started = True
        raw_frame = _load_raw_frame(
            conn,
            model_version=model_version,
            start_date=history_start,
            end_date=write_end,
        )
        smoothed_history, matrix_history, diagnostics_history = _build_smoothed_outputs(
            raw_frame,
            write_start_date=history_start,
            write_end_date=write_end,
            cfg=smoothing_config,
        )
        smoothed = smoothed_history[smoothed_history["as_of_date"].ge(write_start.isoformat())].copy()
        matrix = matrix_history[matrix_history["as_of_date"].ge(write_start.isoformat())].copy()
        diagnostics = diagnostics_history[diagnostics_history["as_of_date"].ge(write_start.isoformat())].copy()
        decisions = _build_decision_frame(
            smoothed_history,
            write_start_date=write_start,
            write_end_date=write_end,
            cfg=decision_config,
        )
        rows_written = _replace_rows(
            conn,
            model_version=model_version,
            write_start=write_start,
            write_end=write_end,
            smoothed=smoothed,
            matrix=matrix,
            diagnostics=diagnostics,
            decisions=decisions,
        )

        output_root = resolve_path(
            config_path,
            str(cfg_get(v2_cfg, "output_dir", default="MacroLayer/out/regime_v2")),
        )
        if output_root is None:
            raise ValueError("Unable to resolve probability_v2.output_dir.")
        output_dir = output_root / write_end.isoformat()
        latest = decisions[decisions["as_of_date"].eq(write_end.isoformat())].copy()
        latest_path = output_dir / "macro_regime_v2_decision_latest.csv"
        _atomic_write_csv(latest_path, latest)
        manifest_path = output_dir / "macro_regime_v2_decision_manifest.json"
        manifest = {
            "model_version": model_version,
            "shadow_only": True,
            "build_end_date": write_end.isoformat(),
            "config_sha256": _sha256_file(config_path),
            "builder_sha256": _sha256_file(Path(__file__)),
            "probability_builder_sha256": _sha256_file(Path(__file__).resolve().parent / "build_macro_probabilities_v2.py"),
            "probability_engine_sha256": _sha256_file(Path(__file__).resolve().parent / "macro_probability_v2.py"),
            "smoothing_engine_sha256": _sha256_file(Path(__file__).resolve().parent / "build_macro_regime_smoothed.py"),
            "decision_engine_sha256": _sha256_file(Path(__file__).resolve().parent / "build_macro_regime_decision.py"),
            "files": {latest_path.name: _sha256_file(latest_path)},
            "rows_written": rows_written,
            "created_at_utc": utc_now_iso(),
        }
        _atomic_write_text(manifest_path, json.dumps(manifest, indent=2) + "\n")
        finish_serving_run(
            conn,
            serving_run_id=serving_run_id,
            status="completed",
            rows_written=rows_written,
            notes=f"Shadow v2 decision chain complete; artifacts={output_dir}.",
        )
        logger.info("V2 decision chain complete rows=%d latest=%s", rows_written, latest.to_dict(orient="records"))
    except BaseException as exc:
        if run_started:
            try:
                finish_serving_run(
                    conn,
                    serving_run_id=serving_run_id,
                    status="failed",
                    rows_written=rows_written,
                    notes=f"V2 decision chain failed: {type(exc).__name__}: {exc}",
                )
            except Exception:
                logger.exception("Unable to record failed v2 decision run.")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
