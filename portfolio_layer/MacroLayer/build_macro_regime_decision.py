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
class DecisionConfig:
    decision_frequency: str
    min_top_probability: float
    min_confidence: float
    switch_margin: float
    confirm_periods: int


@dataclass(frozen=True)
class TrackState:
    active_regime: str | None
    pending_regime: str | None
    pending_count: int


@dataclass(frozen=True)
class TrackOutcome:
    state: TrackState
    smoothed_regime: str | None
    top_probability: float | None
    confidence: float | None
    switch_margin: float | None
    switch_flag: int
    reason: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build the Stage 8.5 regime decision overlay from the smoothed Stage 8 regime layer."
        )
    )
    parser.add_argument("--config", type=Path, default=None, help="Path to macro raw YAML config.")
    parser.add_argument("--serving-db-path", type=Path, default=None, help="Optional serving SQLite path override.")
    parser.add_argument("--start-date", type=str, default=None, help="Optional decision-layer start YYYY-MM-DD override.")
    parser.add_argument("--end-date", type=str, default=None, help="Optional decision-layer end YYYY-MM-DD override.")
    return parser.parse_args()


def _require_probability(value: object, *, label: str) -> float:
    out = float(value)
    if not np.isfinite(out):
        raise ValueError(f"{label} must be finite; got {value!r}.")
    return out


def _require_unit_interval(value: object, *, label: str) -> float:
    out = _require_probability(value, label=label)
    if out < 0.0 or out > 1.0:
        raise ValueError(f"{label} must be in [0, 1]; got {out}.")
    return out


def _resolve_smoothed_bounds(
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
        FROM macro_regime_smoothed_daily
        """
    ).fetchone()
    history_start = parse_iso_date(row["min_as_of_date"]) if row is not None else None
    history_end = parse_iso_date(row["max_as_of_date"]) if row is not None else None
    if history_start is None or history_end is None:
        raise ValueError("Unable to resolve decision-layer build dates from macro_regime_smoothed_daily.")
    if write_start is None:
        write_start = history_start
    if write_end is None:
        write_end = history_end
    if write_end < write_start:
        raise ValueError(
            f"Decision-layer end date {write_end.isoformat()} is before start date {write_start.isoformat()}."
        )
    if write_start < history_start:
        raise ValueError(
            f"Decision-layer start date {write_start.isoformat()} is before available smoothed regime history "
            f"{history_start.isoformat()}."
        )
    if write_end > history_end:
        raise ValueError(
            f"Decision-layer end date {write_end.isoformat()} is after available smoothed regime history "
            f"{history_end.isoformat()}."
        )
    return history_start, write_start, write_end


def _latest_smoothed_regime_run_raw_ingest_id(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        """
        SELECT raw_ingest_run_id
        FROM macro_serving_run
        WHERE build_step = 'regime_smoothed_layer'
          AND status = 'completed'
        ORDER BY COALESCE(completed_at_utc, started_at_utc) DESC, rowid DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    return str(row["raw_ingest_run_id"] or "") or None


def _load_smoothed_frame(
    conn: sqlite3.Connection,
    *,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    frame = pd.read_sql_query(
        f"""
        SELECT
            as_of_date,
            {", ".join(SMOOTHED_CURRENT_COLUMNS)},
            {", ".join(SMOOTHED_NEXT_COLUMNS)},
            coverage_flag
        FROM macro_regime_smoothed_daily
        WHERE as_of_date BETWEEN ? AND ?
        ORDER BY as_of_date
        """,
        conn,
        params=[start_date, end_date],
        parse_dates=["as_of_date"],
    )
    if frame.empty:
        raise ValueError("macro_regime_smoothed_daily returned no rows for the requested decision-layer build.")
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


def _decision_dates(start_date: date, end_date: date, *, frequency: str) -> set[date]:
    try:
        schedule = pd.date_range(start=start_date.isoformat(), end=end_date.isoformat(), freq=frequency)
    except Exception as exc:
        raise ValueError(f"Invalid regime_layer.decision.decision_frequency={frequency!r}.") from exc
    return {pd.Timestamp(ts).date() for ts in schedule}


def _evaluate_track(
    *,
    state: TrackState,
    probs: np.ndarray | None,
    decision_date_flag: bool,
    cfg: DecisionConfig,
) -> TrackOutcome:
    if probs is None:
        return TrackOutcome(
            state=state,
            smoothed_regime=None,
            top_probability=None,
            confidence=None,
            switch_margin=None,
            switch_flag=0,
            reason="UNCOVERED",
        )

    smoothed_regime, top_probability, confidence, top_idx = _regime_summary(probs)
    if state.active_regime is None:
        return TrackOutcome(
            state=TrackState(active_regime=smoothed_regime, pending_regime=None, pending_count=0),
            smoothed_regime=smoothed_regime,
            top_probability=top_probability,
            confidence=confidence,
            switch_margin=None,
            switch_flag=0,
            reason="INITIALIZE",
        )

    if state.active_regime not in REGIME_ORDER:
        raise ValueError(f"Unknown active regime {state.active_regime!r}.")
    active_idx = REGIME_ORDER.index(state.active_regime)
    margin = float(top_probability - float(probs[active_idx])) if smoothed_regime != state.active_regime else None

    if not decision_date_flag:
        return TrackOutcome(
            state=state,
            smoothed_regime=smoothed_regime,
            top_probability=top_probability,
            confidence=confidence,
            switch_margin=margin,
            switch_flag=0,
            reason="NON_DECISION_DATE",
        )

    if smoothed_regime == state.active_regime:
        return TrackOutcome(
            state=TrackState(active_regime=state.active_regime, pending_regime=None, pending_count=0),
            smoothed_regime=smoothed_regime,
            top_probability=top_probability,
            confidence=confidence,
            switch_margin=None,
            switch_flag=0,
            reason="KEEP_INCUMBENT_TOP",
        )

    failures: list[str] = []
    if top_probability < cfg.min_top_probability:
        failures.append("LOW_TOP_PROBABILITY")
    if confidence < cfg.min_confidence:
        failures.append("LOW_CONFIDENCE")
    if margin is None or margin < cfg.switch_margin:
        failures.append("LOW_SWITCH_MARGIN")
    if failures:
        return TrackOutcome(
            state=TrackState(active_regime=state.active_regime, pending_regime=None, pending_count=0),
            smoothed_regime=smoothed_regime,
            top_probability=top_probability,
            confidence=confidence,
            switch_margin=margin,
            switch_flag=0,
            reason="+".join(failures),
        )

    pending_count = state.pending_count + 1 if state.pending_regime == smoothed_regime else 1
    if pending_count >= cfg.confirm_periods:
        return TrackOutcome(
            state=TrackState(active_regime=smoothed_regime, pending_regime=None, pending_count=0),
            smoothed_regime=smoothed_regime,
            top_probability=top_probability,
            confidence=confidence,
            switch_margin=margin,
            switch_flag=1,
            reason=f"SWITCH_ACCEPTED_{pending_count}_OF_{cfg.confirm_periods}",
        )
    return TrackOutcome(
        state=TrackState(active_regime=state.active_regime, pending_regime=smoothed_regime, pending_count=pending_count),
        smoothed_regime=smoothed_regime,
        top_probability=top_probability,
        confidence=confidence,
        switch_margin=margin,
        switch_flag=0,
        reason=f"PENDING_CONFIRMATION_{pending_count}_OF_{cfg.confirm_periods}",
    )


def _build_decision_frame(
    smoothed_frame: pd.DataFrame,
    *,
    write_start_date: date,
    write_end_date: date,
    cfg: DecisionConfig,
) -> pd.DataFrame:
    decision_dates = _decision_dates(
        pd.Timestamp(smoothed_frame["as_of_date"].min()).date(),
        pd.Timestamp(smoothed_frame["as_of_date"].max()).date(),
        frequency=cfg.decision_frequency,
    )
    current_state = TrackState(active_regime=None, pending_regime=None, pending_count=0)
    next_state = TrackState(active_regime=None, pending_regime=None, pending_count=0)
    rows: list[dict[str, object]] = []

    for row in smoothed_frame.itertuples(index=False):
        as_of_date = pd.Timestamp(row.as_of_date).date()
        decision_flag = int(as_of_date in decision_dates)
        coverage_flag = int(row.coverage_flag)
        current_probs = None
        next_probs = None
        if coverage_flag == 1:
            current_probs = _validate_probability_vector(
                np.asarray([getattr(row, col) for col in SMOOTHED_CURRENT_COLUMNS], dtype=float),
                label="Stage 8 smoothed current regime",
                as_of_date=as_of_date.isoformat(),
            )
            next_probs = _validate_probability_vector(
                np.asarray([getattr(row, col) for col in SMOOTHED_NEXT_COLUMNS], dtype=float),
                label="Stage 8 smoothed next regime",
                as_of_date=as_of_date.isoformat(),
            )

        current_outcome = _evaluate_track(
            state=current_state,
            probs=current_probs,
            decision_date_flag=bool(decision_flag),
            cfg=cfg,
        )
        next_outcome = _evaluate_track(
            state=next_state,
            probs=next_probs,
            decision_date_flag=bool(decision_flag),
            cfg=cfg,
        )
        current_state = current_outcome.state
        next_state = next_outcome.state

        if not (write_start_date <= as_of_date <= write_end_date):
            continue

        rows.append(
            {
                "as_of_date": as_of_date.isoformat(),
                "decision_date_flag": decision_flag,
                "smoothed_current_regime": current_outcome.smoothed_regime,
                "smoothed_next_regime": next_outcome.smoothed_regime,
                "active_current_regime": current_state.active_regime,
                "active_next_regime": next_state.active_regime,
                "current_top_probability": current_outcome.top_probability,
                "next_top_probability": next_outcome.top_probability,
                "current_confidence": current_outcome.confidence,
                "next_confidence": next_outcome.confidence,
                "current_switch_margin": current_outcome.switch_margin,
                "next_switch_margin": next_outcome.switch_margin,
                "current_switch_flag": int(current_outcome.switch_flag),
                "next_switch_flag": int(next_outcome.switch_flag),
                "regime_switch_flag": int(bool(current_outcome.switch_flag or next_outcome.switch_flag)),
                "current_pending_regime": current_state.pending_regime,
                "next_pending_regime": next_state.pending_regime,
                "current_pending_count": int(current_state.pending_count),
                "next_pending_count": int(next_state.pending_count),
                "regime_switch_pending_flag": int(bool(current_state.pending_count > 0 or next_state.pending_count > 0)),
                "regime_override_reason": f"CURRENT:{current_outcome.reason}|NEXT:{next_outcome.reason}",
                "coverage_flag": coverage_flag,
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    configure_pipeline_logging()
    args = parse_args()
    config_path, cfg = load_macro_raw_config(args.config)
    serving_db_path = resolve_serving_db_path(cfg, config_path, override=args.serving_db_path)

    layer_cfg = cfg_get(cfg, "regime_layer", default={}) or {}
    decision_cfg = cfg_get(layer_cfg, "decision", default={}) or {}
    cfg_obj = DecisionConfig(
        decision_frequency=str(cfg_get(decision_cfg, "decision_frequency", default="W-FRI")).strip() or "W-FRI",
        min_top_probability=_require_unit_interval(
            cfg_get(decision_cfg, "min_top_probability", default=0.50),
            label="regime_layer.decision.min_top_probability",
        ),
        min_confidence=_require_unit_interval(
            cfg_get(decision_cfg, "min_confidence", default=0.10),
            label="regime_layer.decision.min_confidence",
        ),
        switch_margin=_require_unit_interval(
            cfg_get(decision_cfg, "switch_margin", default=0.05),
            label="regime_layer.decision.switch_margin",
        ),
        confirm_periods=int(cfg_get(decision_cfg, "confirm_periods", default=2)),
    )
    if cfg_obj.confirm_periods < 1:
        raise ValueError("regime_layer.decision.confirm_periods must be >= 1.")

    conn = connect_sqlite(serving_db_path, row_factory=sqlite3.Row)
    serving_run_id = uuid.uuid4().hex
    run_started = False
    rows_written = 0
    try:
        init_db(conn)
        history_start, write_start, write_end = _resolve_smoothed_bounds(
            conn,
            start_override=args.start_date,
            end_override=args.end_date,
        )
        raw_ingest_run_id = _latest_smoothed_regime_run_raw_ingest_id(conn)

        start_serving_run(
            conn,
            serving_run_id=serving_run_id,
            build_step="regime_decision_layer",
            raw_ingest_run_id=raw_ingest_run_id,
            as_of_start_date=write_start.isoformat(),
            as_of_end_date=write_end.isoformat(),
            metric_count=len(REGIME_ORDER),
            notes=(
                "Building the Stage 8.5 regime decision overlay from smoothed regime probabilities "
                "with weekly cadence, confidence thresholds, switch margins, and confirmation."
            ),
        )
        run_started = True

        smoothed_frame = _load_smoothed_frame(
            conn,
            start_date=history_start.isoformat(),
            end_date=write_end.isoformat(),
        )
        decision_frame = _build_decision_frame(
            smoothed_frame,
            write_start_date=write_start,
            write_end_date=write_end,
            cfg=cfg_obj,
        )

        clear_regime_range(
            conn,
            table_name="macro_regime_decision_daily",
            start_date=write_start.isoformat(),
            end_date=write_end.isoformat(),
        )

        updated_at = utc_now_iso()
        rows = [
            (
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
                updated_at,
            )
            for _, row in decision_frame.iterrows()
        ]
        rows_written = insert_many(
            conn,
            """
            INSERT INTO macro_regime_decision_daily (
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
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

        covered = decision_frame[decision_frame["coverage_flag"].eq(1)].copy()
        active_counts = covered["active_current_regime"].value_counts(dropna=True).to_dict() if not covered.empty else {}
        switch_count = int(covered["regime_switch_flag"].sum()) if not covered.empty else 0
        pending_share = float(covered["regime_switch_pending_flag"].mean()) if not covered.empty else float("nan")
        logger.info(
            "Built regime decision layer: rows=%d covered_rows=%d switches=%d pending_share=%.3f active_regimes=%s",
            len(decision_frame),
            int(len(covered)),
            switch_count,
            pending_share,
            active_counts,
        )

        finish_serving_run(
            conn,
            serving_run_id=serving_run_id,
            status="completed",
            rows_written=rows_written,
            notes="Stage 8.5 regime decision overlay built from smoothed regime probabilities.",
        )
        logger.info(
            "Macro regime decision build complete: serving_run_id=%s rows_written=%d write_start=%s write_end=%s warmup_start=%s",
            serving_run_id,
            rows_written,
            write_start.isoformat(),
            write_end.isoformat(),
            history_start.isoformat(),
        )
    except BaseException as exc:
        if run_started:
            fail_notes = f"Regime decision layer failed: {type(exc).__name__}: {exc}"
            try:
                finish_serving_run(
                    conn,
                    serving_run_id=serving_run_id,
                    status="failed",
                    rows_written=rows_written,
                    notes=fail_notes,
                )
            except Exception:
                logger.exception("Failed to record failed regime decision run for serving_run_id=%s", serving_run_id)
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
