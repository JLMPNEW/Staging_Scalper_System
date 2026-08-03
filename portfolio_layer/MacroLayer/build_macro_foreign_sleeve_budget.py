#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import math
import sqlite3
import sys
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from macro_allocation import bounded_normalize
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
from macro_serving_storage import (
    clear_foreign_sleeve_budget_range,
    finish_serving_run,
    init_db,
    insert_many,
    start_serving_run,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ForeignSleeveBudgetConfig:
    output_dir: Path
    build_scope: str
    enabled: bool
    eligible_state: str
    require_positive_alpha: bool
    min_country_confidence: float
    min_fused_alpha: float
    max_candidates: int
    min_selected_candidates: int
    min_budget: float
    max_budget: float
    activation_score_threshold: float
    full_budget_score_threshold: float
    activation_top_n: int
    confidence_power: float
    weight_method: str
    softmax_beta: float
    max_single_etf_sleeve_weight: float
    renormalize_after_caps: bool
    budget_csv_name: str
    candidate_csv_name: str
    acceptance: dict[str, Any]


BUDGET_COLUMNS = [
    "as_of_date",
    "active_flag",
    "foreign_budget",
    "min_budget",
    "max_budget",
    "activation_score",
    "activation_score_threshold",
    "full_budget_score_threshold",
    "foreign_candidate_count",
    "eligible_candidate_count",
    "selected_candidate_count",
    "positive_candidate_count",
    "avg_selected_confidence",
    "max_foreign_fused_alpha",
    "activation_reason",
    "output_csv",
    "coverage_flag",
    "updated_at_utc",
]


CANDIDATE_COLUMNS = [
    "as_of_date",
    "ticker",
    "market_name",
    "region",
    "country_class",
    "source_state",
    "country_confidence",
    "tactical_z",
    "country_macro_fit_z",
    "foreign_fused_alpha",
    "candidate_score",
    "sleeve_weight",
    "portfolio_weight_at_budget",
    "candidate_rank",
    "candidate_percentile",
    "eligible_flag",
    "selected_flag",
    "active_flag",
    "rejection_reason",
    "coverage_flag",
    "updated_at_utc",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build Stage 12C optional foreign sleeve budget and ETF weights.")
    parser.add_argument("--config", type=Path, default=None, help="Path to macro raw YAML config.")
    parser.add_argument("--serving-db-path", type=Path, default=None, help="Optional serving SQLite path override.")
    parser.add_argument("--start-date", type=str, default=None, help="Optional Stage 12C start YYYY-MM-DD override.")
    parser.add_argument("--end-date", type=str, default=None, help="Optional Stage 12C end YYYY-MM-DD override.")
    return parser.parse_args()


def _resolve_layer_config(cfg: dict[str, Any], config_path: Path) -> ForeignSleeveBudgetConfig:
    raw_cfg = dict(cfg_get(cfg, "foreign_sleeve_budget_layer", default={}) or {})
    output_dir = resolve_path(config_path, str(raw_cfg.get("output_dir", "MacroLayer/out/foreign_sleeve_budget")))
    if output_dir is None:
        raise ValueError("foreign_sleeve_budget_layer.output_dir could not be resolved.")
    build_scope = str(raw_cfg.get("build_scope", "all")).strip().lower() or "all"
    if build_scope not in {"all", "latest"}:
        raise ValueError("foreign_sleeve_budget_layer.build_scope must be one of: all, latest.")
    candidate_cfg = dict(raw_cfg.get("candidate", {}) or {})
    budget_cfg = dict(raw_cfg.get("budget", {}) or {})
    weights_cfg = dict(raw_cfg.get("weights", {}) or {})
    exports_cfg = dict(raw_cfg.get("exports", {}) or {})
    weight_method = str(weights_cfg.get("method", "softmax")).strip().lower() or "softmax"
    if weight_method not in {"softmax", "proportional"}:
        raise ValueError("foreign_sleeve_budget_layer.weights.method must be one of: softmax, proportional.")
    min_budget = max(0.0, float(budget_cfg.get("min_budget", 0.05)))
    max_budget = max(0.0, float(budget_cfg.get("max_budget", 0.20)))
    if max_budget < min_budget:
        raise ValueError("foreign_sleeve_budget_layer.budget.max_budget must be >= min_budget.")
    activation = float(budget_cfg.get("activation_score_threshold", 0.25))
    full_budget = float(budget_cfg.get("full_budget_score_threshold", 1.00))
    if full_budget <= activation:
        raise ValueError("foreign_sleeve_budget_layer.budget.full_budget_score_threshold must be > activation_score_threshold.")
    max_single_etf_sleeve_weight = max(0.0, float(weights_cfg.get("max_single_etf_sleeve_weight", 0.60)))
    min_selected_candidates = max(1, int(candidate_cfg.get("min_selected_candidates", 1)))
    if 0.0 < max_single_etf_sleeve_weight < 1.0:
        min_selected_candidates = max(min_selected_candidates, int(math.ceil(1.0 / max_single_etf_sleeve_weight - 1e-12)))

    return ForeignSleeveBudgetConfig(
        output_dir=output_dir,
        build_scope=build_scope,
        enabled=parse_boolish(raw_cfg.get("enabled"), default=True),
        eligible_state=str(raw_cfg.get("eligible_state", "Eligible")).strip() or "Eligible",
        require_positive_alpha=parse_boolish(candidate_cfg.get("require_positive_alpha"), default=True),
        min_country_confidence=float(candidate_cfg.get("min_country_confidence", 0.35)),
        min_fused_alpha=float(candidate_cfg.get("min_fused_alpha", 0.0)),
        max_candidates=max(1, int(candidate_cfg.get("max_candidates", 5))),
        min_selected_candidates=min_selected_candidates,
        min_budget=min_budget,
        max_budget=max_budget,
        activation_score_threshold=activation,
        full_budget_score_threshold=full_budget,
        activation_top_n=max(1, int(budget_cfg.get("activation_top_n", 3))),
        confidence_power=max(0.0, float(budget_cfg.get("confidence_power", 1.0))),
        weight_method=weight_method,
        softmax_beta=max(1e-9, float(weights_cfg.get("softmax_beta", 1.0))),
        max_single_etf_sleeve_weight=max_single_etf_sleeve_weight,
        renormalize_after_caps=parse_boolish(weights_cfg.get("renormalize_after_caps"), default=True),
        budget_csv_name=str(exports_cfg.get("budget_csv_name", "foreign_sleeve_budget_latest.csv")).strip()
        or "foreign_sleeve_budget_latest.csv",
        candidate_csv_name=str(exports_cfg.get("candidate_csv_name", "foreign_sleeve_candidates_latest.csv")).strip()
        or "foreign_sleeve_candidates_latest.csv",
        acceptance=dict(raw_cfg.get("acceptance", {}) or {}),
    )


def _resolve_build_bounds(
    conn: sqlite3.Connection,
    *,
    layer_cfg: ForeignSleeveBudgetConfig,
    start_override: str | None,
    end_override: str | None,
) -> tuple[date, date]:
    row = conn.execute(
        """
        SELECT MIN(as_of_date) AS min_date, MAX(as_of_date) AS max_date
        FROM portfolio_allocation_summary
        """
    ).fetchone()
    min_date = parse_iso_date(row["min_date"]) if row is not None else None
    max_date = parse_iso_date(row["max_date"]) if row is not None else None
    if min_date is None or max_date is None:
        raise ValueError("portfolio_allocation_summary is empty. Build Stage 12A before Stage 12C.")
    if layer_cfg.build_scope == "latest" and not start_override and not end_override:
        return max_date, max_date
    start_date = parse_iso_date(start_override) or min_date
    end_date = parse_iso_date(end_override) or max_date
    if end_date < start_date:
        raise ValueError(f"Stage 12C end date {end_date.isoformat()} is before start date {start_date.isoformat()}.")
    if start_date < min_date or end_date > max_date:
        raise ValueError(
            f"Stage 12C requested range {start_date.isoformat()}..{end_date.isoformat()} is outside "
            f"available Stage 12A range {min_date.isoformat()}..{max_date.isoformat()}."
        )
    return start_date, end_date


def _load_build_dates(conn: sqlite3.Connection, *, start_date: str, end_date: str) -> pd.DatetimeIndex:
    frame = pd.read_sql_query(
        """
        SELECT as_of_date
        FROM portfolio_allocation_summary
        WHERE as_of_date BETWEEN ? AND ?
        ORDER BY as_of_date
        """,
        conn,
        params=[start_date, end_date],
        parse_dates=["as_of_date"],
    )
    if frame.empty:
        raise ValueError(f"No Stage 12A allocation summary dates found for {start_date}..{end_date}.")
    return pd.DatetimeIndex(frame["as_of_date"].dropna().unique()).sort_values()


def _load_foreign_inputs(conn: sqlite3.Connection, *, start_date: str, end_date: str) -> pd.DataFrame:
    frame = pd.read_sql_query(
        """
        SELECT
            as_of_date,
            ticker,
            market_name,
            region,
            country_class,
            state,
            country_confidence,
            tactical_z,
            country_macro_fit_z,
            foreign_fused_alpha,
            country_macro_coverage_flag,
            score_pct
        FROM portfolio_inputs_daily
        WHERE asset_type = 'FOREIGN_ETF'
          AND as_of_date BETWEEN ? AND ?
        """,
        conn,
        params=[start_date, end_date],
        parse_dates=["as_of_date"],
    )
    if frame.empty:
        return pd.DataFrame()
    for col in ("ticker", "market_name", "region", "country_class", "state"):
        frame[col] = frame[col].fillna("").astype(str).str.strip()
    frame["ticker"] = frame["ticker"].astype(str).str.upper().str.strip()
    return frame.dropna(subset=["as_of_date", "ticker"]).reset_index(drop=True)


def _cap_and_normalize(weights: pd.Series, *, cap: float, renormalize: bool) -> pd.Series:
    if not renormalize:
        logger.warning("renormalize_after_caps=false is deprecated; exact bounded normalization is enforced.")
    return bounded_normalize(weights, lower=0.0, upper=cap, target_sum=1.0)

def _candidate_weights(selected: pd.DataFrame, layer_cfg: ForeignSleeveBudgetConfig) -> pd.Series:
    if selected.empty:
        return pd.Series(dtype="float64")
    scores = pd.to_numeric(selected["candidate_score"], errors="coerce").fillna(0.0)
    if layer_cfg.weight_method == "proportional":
        raw = scores.clip(lower=0.0)
        if float(raw.sum()) <= 0.0:
            raw = pd.Series(1.0, index=scores.index, dtype="float64")
    else:
        centered = scores - float(scores.max())
        raw = pd.Series(np.exp(layer_cfg.softmax_beta * centered), index=scores.index)
    return _cap_and_normalize(
        raw,
        cap=layer_cfg.max_single_etf_sleeve_weight,
        renormalize=layer_cfg.renormalize_after_caps,
    )


def _activation_budget(
    selected_candidates: pd.DataFrame,
    *,
    layer_cfg: ForeignSleeveBudgetConfig,
) -> tuple[int, float, float, float, str]:
    if not layer_cfg.enabled:
        return 0, 0.0, 0.0, np.nan, "disabled"
    if len(selected_candidates) < layer_cfg.min_selected_candidates:
        return 0, 0.0, 0.0, np.nan, "insufficient_selected_candidates"
    top = selected_candidates.sort_values("candidate_score", ascending=False).head(layer_cfg.activation_top_n)
    activation_score = float(pd.to_numeric(top["candidate_score"], errors="coerce").mean())
    avg_confidence = float(pd.to_numeric(top["country_confidence"], errors="coerce").mean())
    if not np.isfinite(activation_score) or activation_score < layer_cfg.activation_score_threshold:
        return 0, 0.0, activation_score, avg_confidence, "activation_score_below_threshold"
    strength = (activation_score - layer_cfg.activation_score_threshold) / (
        layer_cfg.full_budget_score_threshold - layer_cfg.activation_score_threshold
    )
    strength = min(1.0, max(0.0, strength))
    confidence_multiplier = min(1.0, max(0.0, avg_confidence)) ** layer_cfg.confidence_power
    budget = layer_cfg.min_budget + (layer_cfg.max_budget - layer_cfg.min_budget) * strength * confidence_multiplier
    budget = min(layer_cfg.max_budget, max(layer_cfg.min_budget, budget))
    return 1, float(budget), activation_score, avg_confidence, "active"


def _reason_for_row(row: pd.Series, *, layer_cfg: ForeignSleeveBudgetConfig, active: bool, selected: bool) -> str:
    if selected and active:
        return "selected"
    if selected and not active:
        return "sleeve_inactive"
    if str(row.get("state") or "") != layer_cfg.eligible_state:
        return "state_not_eligible"
    coverage_value = row.get("country_macro_coverage_flag")
    coverage_flag = int(coverage_value) if pd.notna(coverage_value) else 0
    if coverage_flag != 1:
        return "country_macro_coverage_missing"
    confidence_value = row.get("country_confidence")
    confidence = float(confidence_value) if pd.notna(confidence_value) else 0.0
    if confidence < layer_cfg.min_country_confidence:
        return "confidence_below_min"
    alpha_value = row.get("foreign_fused_alpha")
    alpha = float(alpha_value) if pd.notna(alpha_value) else 0.0
    if layer_cfg.require_positive_alpha and alpha <= 0.0:
        return "alpha_not_positive"
    if alpha < layer_cfg.min_fused_alpha:
        return "alpha_below_min"
    return "not_top_candidate"


def _build_frames(
    dates: pd.DatetimeIndex,
    foreign: pd.DataFrame,
    *,
    layer_cfg: ForeignSleeveBudgetConfig,
    budget_csv: Path,
    candidate_csv: Path,
    updated_at: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    budget_rows: list[dict[str, Any]] = []
    candidate_frames: list[pd.DataFrame] = []
    for as_of_date in dates:
        day = foreign.loc[pd.to_datetime(foreign["as_of_date"]).eq(pd.Timestamp(as_of_date))].copy() if not foreign.empty else pd.DataFrame()
        if day.empty:
            budget_rows.append(
                {
                    "as_of_date": pd.Timestamp(as_of_date),
                    "active_flag": 0,
                    "foreign_budget": 0.0,
                    "min_budget": layer_cfg.min_budget,
                    "max_budget": layer_cfg.max_budget,
                    "activation_score": np.nan,
                    "activation_score_threshold": layer_cfg.activation_score_threshold,
                    "full_budget_score_threshold": layer_cfg.full_budget_score_threshold,
                    "foreign_candidate_count": 0,
                    "eligible_candidate_count": 0,
                    "selected_candidate_count": 0,
                    "positive_candidate_count": 0,
                    "avg_selected_confidence": np.nan,
                    "max_foreign_fused_alpha": np.nan,
                    "activation_reason": "no_foreign_rows",
                    "output_csv": str(candidate_csv),
                    "coverage_flag": 1,
                    "updated_at_utc": updated_at,
                }
            )
            continue

        for col in ("country_confidence", "tactical_z", "country_macro_fit_z", "foreign_fused_alpha"):
            day[col] = pd.to_numeric(day[col], errors="coerce")
        day["country_macro_coverage_flag"] = pd.to_numeric(day["country_macro_coverage_flag"], errors="coerce").fillna(0).astype(int)
        day["candidate_score"] = day["foreign_fused_alpha"].fillna(-np.inf)
        eligible = (
            day["state"].eq(layer_cfg.eligible_state)
            & day["country_macro_coverage_flag"].eq(1)
            & day["country_confidence"].fillna(0.0).ge(layer_cfg.min_country_confidence)
            & day["foreign_fused_alpha"].fillna(-np.inf).ge(layer_cfg.min_fused_alpha)
        )
        if layer_cfg.require_positive_alpha:
            eligible = eligible & day["foreign_fused_alpha"].fillna(-np.inf).gt(0.0)
        day["eligible_flag"] = eligible.astype(int)
        candidates = day.loc[eligible].sort_values("candidate_score", ascending=False).head(layer_cfg.max_candidates).copy()
        active_flag, budget, activation_score, avg_confidence, activation_reason = _activation_budget(
            candidates,
            layer_cfg=layer_cfg,
        )
        active = bool(active_flag)
        selected_index = candidates.index if active else pd.Index([])
        day["selected_flag"] = day.index.isin(selected_index).astype(int)
        day["active_flag"] = active_flag
        day["sleeve_weight"] = 0.0
        if active and not candidates.empty:
            weights = _candidate_weights(candidates, layer_cfg)
            day.loc[weights.index, "sleeve_weight"] = weights
        day["portfolio_weight_at_budget"] = day["sleeve_weight"] * budget
        day["candidate_rank"] = day["foreign_fused_alpha"].rank(method="first", ascending=False).astype("Int64")
        day["candidate_percentile"] = day["foreign_fused_alpha"].rank(method="first", ascending=True, pct=True).fillna(0.0)
        day["rejection_reason"] = [
            _reason_for_row(row, layer_cfg=layer_cfg, active=active, selected=bool(row["selected_flag"]))
            for _, row in day.iterrows()
        ]
        day["coverage_flag"] = day["country_macro_coverage_flag"].astype(int)
        day["updated_at_utc"] = updated_at
        candidate_frames.append(
            pd.DataFrame(
                {
                    "as_of_date": pd.Timestamp(as_of_date),
                    "ticker": day["ticker"].astype(str).str.upper().str.strip(),
                    "market_name": day["market_name"].fillna("").astype(str).str.strip(),
                    "region": day["region"].fillna("").astype(str).str.strip(),
                    "country_class": day["country_class"].fillna("").astype(str).str.strip(),
                    "source_state": day["state"].fillna("").astype(str).str.strip(),
                    "country_confidence": day["country_confidence"],
                    "tactical_z": day["tactical_z"],
                    "country_macro_fit_z": day["country_macro_fit_z"],
                    "foreign_fused_alpha": day["foreign_fused_alpha"],
                    "candidate_score": day["candidate_score"].replace(-np.inf, np.nan),
                    "sleeve_weight": day["sleeve_weight"],
                    "portfolio_weight_at_budget": day["portfolio_weight_at_budget"],
                    "candidate_rank": day["candidate_rank"],
                    "candidate_percentile": day["candidate_percentile"],
                    "eligible_flag": day["eligible_flag"],
                    "selected_flag": day["selected_flag"],
                    "active_flag": day["active_flag"],
                    "rejection_reason": day["rejection_reason"],
                    "coverage_flag": day["coverage_flag"],
                    "updated_at_utc": updated_at,
                }
            )
        )
        selected = day.loc[day["selected_flag"].eq(1)]
        budget_rows.append(
            {
                "as_of_date": pd.Timestamp(as_of_date),
                "active_flag": active_flag,
                "foreign_budget": budget,
                "min_budget": layer_cfg.min_budget,
                "max_budget": layer_cfg.max_budget,
                "activation_score": activation_score,
                "activation_score_threshold": layer_cfg.activation_score_threshold,
                "full_budget_score_threshold": layer_cfg.full_budget_score_threshold,
                "foreign_candidate_count": int(len(day)),
                "eligible_candidate_count": int(day["eligible_flag"].sum()),
                "selected_candidate_count": int(day["selected_flag"].sum()),
                "positive_candidate_count": int(day["foreign_fused_alpha"].fillna(-np.inf).gt(0.0).sum()),
                "avg_selected_confidence": float(selected["country_confidence"].mean()) if not selected.empty else np.nan,
                "max_foreign_fused_alpha": float(day["foreign_fused_alpha"].max()) if day["foreign_fused_alpha"].notna().any() else np.nan,
                "activation_reason": activation_reason,
                "output_csv": str(candidate_csv),
                "coverage_flag": 1,
                "updated_at_utc": updated_at,
            }
        )
    budget = pd.DataFrame(budget_rows, columns=BUDGET_COLUMNS)
    candidates = pd.concat(candidate_frames, ignore_index=True, sort=False) if candidate_frames else pd.DataFrame(columns=CANDIDATE_COLUMNS)
    return budget, candidates[CANDIDATE_COLUMNS].copy()


def _write_atomic_csv(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    frame.to_csv(tmp_path, index=False)
    tmp_path.replace(path)


def _frame_rows(frame: pd.DataFrame, columns: list[str]) -> list[tuple[Any, ...]]:
    if frame.empty:
        return []
    prepared = frame.loc[:, columns].copy()
    prepared["as_of_date"] = pd.to_datetime(prepared["as_of_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    prepared = prepared.astype(object).where(pd.notna(prepared), None)

    def to_db_value(value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, np.integer):
            return int(value)
        if isinstance(value, np.floating):
            return float(value)
        if isinstance(value, np.bool_):
            return int(bool(value))
        if isinstance(value, pd.Timestamp):
            return value.strftime("%Y-%m-%d")
        return value

    prepared = prepared.map(to_db_value)
    return [tuple(row) for row in prepared.itertuples(index=False, name=None)]


def _latest_portfolio_input_run_raw_ingest_id(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        """
        SELECT raw_ingest_run_id
        FROM macro_serving_run
        WHERE build_step = 'portfolio_input_layer'
          AND status = 'completed'
        ORDER BY COALESCE(completed_at_utc, started_at_utc) DESC, rowid DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    return str(row["raw_ingest_run_id"] or "") or None


def main() -> None:
    configure_pipeline_logging()
    args = parse_args()
    config_path, cfg = load_macro_raw_config(args.config)
    layer_cfg = _resolve_layer_config(cfg, config_path)
    serving_db_path = resolve_serving_db_path(cfg, config_path, override=args.serving_db_path)
    conn = connect_sqlite(serving_db_path, row_factory=sqlite3.Row)
    serving_run_id = uuid.uuid4().hex
    rows_written = 0
    run_started = False
    try:
        init_db(conn)
        start_date, end_date = _resolve_build_bounds(
            conn,
            layer_cfg=layer_cfg,
            start_override=args.start_date,
            end_override=args.end_date,
        )
        updated_at = utc_now_iso()
        dates = _load_build_dates(conn, start_date=start_date.isoformat(), end_date=end_date.isoformat())
        foreign = _load_foreign_inputs(conn, start_date=start_date.isoformat(), end_date=end_date.isoformat())
        budget_csv = layer_cfg.output_dir / layer_cfg.budget_csv_name
        candidate_csv = layer_cfg.output_dir / layer_cfg.candidate_csv_name
        budget, candidates = _build_frames(
            dates,
            foreign,
            layer_cfg=layer_cfg,
            budget_csv=budget_csv,
            candidate_csv=candidate_csv,
            updated_at=updated_at,
        )

        start_serving_run(
            conn,
            serving_run_id=serving_run_id,
            build_step="foreign_sleeve_budget_layer",
            raw_ingest_run_id=_latest_portfolio_input_run_raw_ingest_id(conn),
            as_of_start_date=start_date.isoformat(),
            as_of_end_date=end_date.isoformat(),
            metric_count=int(candidates["ticker"].nunique()) if not candidates.empty else 0,
            notes=(
                f"build_scope={layer_cfg.build_scope} enabled={layer_cfg.enabled} "
                f"budget_range={layer_cfg.min_budget:.4f}..{layer_cfg.max_budget:.4f}"
            ),
        )
        run_started = True
        for table_name in ("foreign_sleeve_budget_daily", "foreign_sleeve_candidate_daily"):
            clear_foreign_sleeve_budget_range(
                conn,
                table_name=table_name,
                start_date=start_date.isoformat(),
                end_date=end_date.isoformat(),
            )
        rows_written += insert_many(
            conn,
            """
            INSERT OR REPLACE INTO foreign_sleeve_budget_daily (
                as_of_date, active_flag, foreign_budget, min_budget, max_budget,
                activation_score, activation_score_threshold, full_budget_score_threshold,
                foreign_candidate_count, eligible_candidate_count, selected_candidate_count,
                positive_candidate_count, avg_selected_confidence, max_foreign_fused_alpha,
                activation_reason, output_csv, coverage_flag, updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _frame_rows(budget, BUDGET_COLUMNS),
            chunk_size=50_000,
        )
        rows_written += insert_many(
            conn,
            """
            INSERT OR REPLACE INTO foreign_sleeve_candidate_daily (
                as_of_date, ticker, market_name, region, country_class, source_state,
                country_confidence, tactical_z, country_macro_fit_z, foreign_fused_alpha,
                candidate_score, sleeve_weight, portfolio_weight_at_budget, candidate_rank,
                candidate_percentile, eligible_flag, selected_flag, active_flag,
                rejection_reason, coverage_flag, updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _frame_rows(candidates, CANDIDATE_COLUMNS),
            chunk_size=50_000,
        )

        latest_key = pd.to_datetime(budget["as_of_date"], errors="coerce").max().strftime("%Y-%m-%d")
        _write_atomic_csv(
            budget_csv,
            budget.loc[pd.to_datetime(budget["as_of_date"]).dt.strftime("%Y-%m-%d").eq(latest_key)].reset_index(drop=True),
        )
        _write_atomic_csv(
            candidate_csv,
            candidates.loc[pd.to_datetime(candidates["as_of_date"]).dt.strftime("%Y-%m-%d").eq(latest_key)]
            .sort_values(["selected_flag", "candidate_rank"], ascending=[False, True])
            .reset_index(drop=True),
        )
        finish_serving_run(conn, serving_run_id=serving_run_id, status="completed", rows_written=rows_written)
        logger.info(
            "Stage 12C foreign sleeve budget complete: rows_written=%d range=%s..%s output_dir=%s",
            rows_written,
            start_date.isoformat(),
            end_date.isoformat(),
            layer_cfg.output_dir,
        )
    except BaseException as exc:
        if run_started:
            try:
                finish_serving_run(
                    conn,
                    serving_run_id=serving_run_id,
                    status="failed",
                    rows_written=rows_written,
                    notes=f"{type(exc).__name__}: {exc}",
                )
            except Exception:
                logger.exception("Failed to mark Stage 12C serving run as failed.")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
