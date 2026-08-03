#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import re
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

from macro_raw_config import (  # noqa: E402
    cfg_get,
    configure_pipeline_logging,
    connect_sqlite,
    load_macro_raw_config,
    parse_iso_date,
    resolve_path,
    utc_now_iso,
)
from macro_serving_common import resolve_serving_db_path  # noqa: E402
from macro_serving_storage import (  # noqa: E402
    clear_industry_macro_range,
    finish_serving_run,
    init_db,
    insert_many,
    start_serving_run,
)
from staging_portfolio_adapter import (  # noqa: E402
    MAX_STAGE2_PRICE_STALE_DAYS,
    load_staging_prices,
    load_staging_score_panel,
    staleness_gated_weekly,
)

logger = logging.getLogger(__name__)

REGIME_ORDER = (
    "EXPANSION_DISINFLATION",
    "HEATING_UP",
    "SLOW_GROWTH",
    "STAGFLATION",
)
CURRENT_PROB_COLUMNS = (
    "p_smoothed_current_expansion_disinflation",
    "p_smoothed_current_heating_up",
    "p_smoothed_current_slow_growth",
    "p_smoothed_current_stagflation",
)
SHOCK_FEATURE_SPECS: dict[str, list[tuple[str, str, float]]] = {
    "oil": [
        ("us_brent_spot", "pct_21d", 1.0),
        ("us_wti_spot", "pct_21d", 1.0),
        ("us_henry_hub_natgas", "pct_21d", 1.0),
    ],
    "commodity": [
        ("global_copper", "ann_3m_pct", 1.0),
        ("global_wheat", "ann_3m_pct", 1.0),
    ],
    "dollar": [
        ("us_nominal_broad_dollar", "pct_21d", 1.0),
        ("us_real_broad_dollar", "pct_12m", 1.0),
    ],
    "real_yield": [
        ("us_10y_real_yield", "level", -1.0),
    ],
    "credit": [
        ("us_hy_oas", "level", -1.0),
        ("us_nfci", "level", -1.0),
    ],
}
SHOCK_NAMES = tuple(SHOCK_FEATURE_SPECS.keys())

SECTOR_REGIME_PRIORS: dict[str, dict[str, float]] = {
    "Technology": {"EXPANSION_DISINFLATION": 1.20, "HEATING_UP": -0.40, "SLOW_GROWTH": 0.20, "STAGFLATION": -1.00},
    "Communication Services": {"EXPANSION_DISINFLATION": 0.80, "HEATING_UP": -0.20, "SLOW_GROWTH": 0.10, "STAGFLATION": -0.70},
    "Consumer Cyclical": {"EXPANSION_DISINFLATION": 0.90, "HEATING_UP": 0.20, "SLOW_GROWTH": -1.00, "STAGFLATION": -1.20},
    "Industrials": {"EXPANSION_DISINFLATION": 0.80, "HEATING_UP": 0.40, "SLOW_GROWTH": -0.80, "STAGFLATION": -0.70},
    "Financial Services": {"EXPANSION_DISINFLATION": 0.50, "HEATING_UP": 0.70, "SLOW_GROWTH": -0.90, "STAGFLATION": -0.80},
    "Healthcare": {"EXPANSION_DISINFLATION": 0.10, "HEATING_UP": -0.10, "SLOW_GROWTH": 0.90, "STAGFLATION": 0.20},
    "Consumer Defensive": {"EXPANSION_DISINFLATION": -0.20, "HEATING_UP": -0.10, "SLOW_GROWTH": 0.80, "STAGFLATION": 0.90},
    "Utilities": {"EXPANSION_DISINFLATION": -0.50, "HEATING_UP": -0.40, "SLOW_GROWTH": 0.70, "STAGFLATION": 0.80},
    "Real Estate": {"EXPANSION_DISINFLATION": 0.30, "HEATING_UP": -0.80, "SLOW_GROWTH": -0.20, "STAGFLATION": -0.90},
    "Energy": {"EXPANSION_DISINFLATION": -0.10, "HEATING_UP": 1.20, "SLOW_GROWTH": -0.10, "STAGFLATION": 1.40},
    "Basic Materials": {"EXPANSION_DISINFLATION": 0.40, "HEATING_UP": 0.90, "SLOW_GROWTH": -0.60, "STAGFLATION": 0.60},
}
DEFAULT_REGIME_PRIOR = {name: 0.0 for name in REGIME_ORDER}

SECTOR_SHOCK_PRIORS: dict[str, dict[str, float]] = {
    "Technology": {"oil": -0.10, "commodity": 0.10, "dollar": -0.70, "real_yield": -1.00, "credit": -0.50},
    "Communication Services": {"oil": -0.10, "commodity": 0.00, "dollar": -0.30, "real_yield": -0.60, "credit": -0.30},
    "Consumer Cyclical": {"oil": -0.80, "commodity": -0.20, "dollar": -0.30, "real_yield": -0.50, "credit": -0.80},
    "Industrials": {"oil": -0.40, "commodity": 0.20, "dollar": -0.40, "real_yield": -0.20, "credit": -0.60},
    "Financial Services": {"oil": 0.00, "commodity": 0.00, "dollar": 0.20, "real_yield": 0.60, "credit": -1.00},
    "Healthcare": {"oil": -0.10, "commodity": 0.00, "dollar": -0.20, "real_yield": -0.40, "credit": -0.20},
    "Consumer Defensive": {"oil": -0.40, "commodity": -0.30, "dollar": 0.10, "real_yield": -0.20, "credit": 0.20},
    "Utilities": {"oil": -0.20, "commodity": -0.10, "dollar": 0.20, "real_yield": -0.80, "credit": -0.30},
    "Real Estate": {"oil": -0.10, "commodity": 0.00, "dollar": 0.00, "real_yield": -1.20, "credit": -0.90},
    "Energy": {"oil": 1.50, "commodity": 0.60, "dollar": -0.20, "real_yield": 0.20, "credit": -0.10},
    "Basic Materials": {"oil": 0.10, "commodity": 1.20, "dollar": -0.60, "real_yield": -0.20, "credit": -0.40},
}
DEFAULT_SHOCK_PRIOR = {name: 0.0 for name in SHOCK_NAMES}

REGIME_KEYWORD_RULES: list[tuple[tuple[str, ...], dict[str, float]]] = [
    (("semiconductor", "electronic component"), {"EXPANSION_DISINFLATION": 0.40, "HEATING_UP": 0.30, "SLOW_GROWTH": -0.40, "STAGFLATION": -0.80}),
    (("software",), {"EXPANSION_DISINFLATION": 0.45, "HEATING_UP": -0.20, "SLOW_GROWTH": 0.10, "STAGFLATION": -0.70}),
    (("information technology services", "it services"), {"EXPANSION_DISINFLATION": 0.30, "HEATING_UP": -0.10, "SLOW_GROWTH": 0.10, "STAGFLATION": -0.50}),
    (("hardware", "instrument"), {"EXPANSION_DISINFLATION": 0.20, "HEATING_UP": 0.10, "SLOW_GROWTH": -0.20, "STAGFLATION": -0.50}),
    (("biotechnology",), {"EXPANSION_DISINFLATION": 0.20, "HEATING_UP": -0.20, "SLOW_GROWTH": 0.50, "STAGFLATION": -0.10}),
    (("medical devices", "medical instruments", "diagnostics", "research"), {"EXPANSION_DISINFLATION": 0.10, "HEATING_UP": -0.10, "SLOW_GROWTH": 0.40, "STAGFLATION": 0.10}),
    (("bank", "credit services"), {"EXPANSION_DISINFLATION": 0.10, "HEATING_UP": 0.40, "SLOW_GROWTH": -0.70, "STAGFLATION": -0.70}),
    (("insurance",), {"EXPANSION_DISINFLATION": 0.05, "HEATING_UP": 0.20, "SLOW_GROWTH": -0.10, "STAGFLATION": -0.20}),
    (("asset management", "capital markets", "exchange", "brokerage"), {"EXPANSION_DISINFLATION": 0.25, "HEATING_UP": 0.20, "SLOW_GROWTH": -0.50, "STAGFLATION": -0.50}),
    (("oil & gas e&p", "oil & gas equipment", "oil & gas midstream", "oil & gas value chain"), {"EXPANSION_DISINFLATION": -0.05, "HEATING_UP": 0.60, "SLOW_GROWTH": -0.15, "STAGFLATION": 0.65}),
    (("metals & mining", "specialty materials"), {"EXPANSION_DISINFLATION": 0.10, "HEATING_UP": 0.35, "SLOW_GROWTH": -0.20, "STAGFLATION": 0.35}),
    (("chemical",), {"EXPANSION_DISINFLATION": 0.10, "HEATING_UP": 0.20, "SLOW_GROWTH": -0.10, "STAGFLATION": 0.10}),
    (("reit - mortgage", "mortgage reit"), {"EXPANSION_DISINFLATION": -0.10, "HEATING_UP": -0.60, "SLOW_GROWTH": -0.20, "STAGFLATION": -0.70}),
    (("reit - ", "real estate & reits"), {"EXPANSION_DISINFLATION": 0.05, "HEATING_UP": -0.25, "SLOW_GROWTH": 0.05, "STAGFLATION": -0.30}),
    (("airline", "travel", "hospitality", "restaurant"), {"EXPANSION_DISINFLATION": 0.20, "HEATING_UP": -0.10, "SLOW_GROWTH": -0.60, "STAGFLATION": -0.80}),
    (("retail", "apparel", "luxury", "automotive"), {"EXPANSION_DISINFLATION": 0.15, "HEATING_UP": 0.10, "SLOW_GROWTH": -0.50, "STAGFLATION": -0.70}),
    (("utility", "independent power"), {"EXPANSION_DISINFLATION": -0.10, "HEATING_UP": -0.20, "SLOW_GROWTH": 0.30, "STAGFLATION": 0.40}),
]

SHOCK_KEYWORD_RULES: list[tuple[tuple[str, ...], dict[str, float]]] = [
    (("semiconductor", "electronic component"), {"dollar": -0.40, "real_yield": -0.40}),
    (("software",), {"real_yield": -0.40, "credit": -0.10}),
    (("hardware", "instrument"), {"dollar": -0.30, "real_yield": -0.20}),
    (("bank", "credit services"), {"real_yield": 0.40, "credit": -0.80}),
    (("insurance",), {"real_yield": 0.25, "credit": -0.20}),
    (("asset management", "capital markets", "exchange", "brokerage"), {"real_yield": 0.10, "credit": -0.50}),
    (("oil & gas e&p",), {"oil": 0.70, "commodity": 0.20}),
    (("oil & gas equipment",), {"oil": 0.55, "commodity": 0.10}),
    (("oil & gas midstream",), {"oil": 0.35, "commodity": 0.05}),
    (("oil & gas value chain",), {"oil": 0.45, "commodity": 0.10}),
    (("metals & mining",), {"commodity": 0.70, "dollar": -0.20}),
    (("chemical",), {"commodity": 0.25, "oil": -0.10}),
    (("airline", "travel", "hospitality"), {"oil": -0.70, "credit": -0.20}),
    (("transportation", "logistics"), {"oil": -0.50, "dollar": -0.10, "credit": -0.20}),
    (("reit - mortgage", "mortgage reit"), {"real_yield": -0.90, "credit": -0.80}),
    (("reit - ", "real estate & reits"), {"real_yield": -0.45, "credit": -0.45}),
    (("biotechnology",), {"real_yield": -0.30, "credit": -0.20}),
    (("medical devices", "diagnostics", "research"), {"real_yield": -0.15}),
]


@dataclass(frozen=True)
class IndustryMacroConfig:
    cadence: str
    strategy_key: str
    source_mode: str
    production_output_root: Path
    production_file_glob: str
    sec_snapshot_enabled: bool
    sec_db_path: Path | None
    sec_snapshot_table: str
    sec_snapshot_start_date: str | None
    sec_snapshot_end_date: str | None
    output_dir: Path
    min_industry_members: int
    min_aggregate_members: int
    min_sector_members: int
    context_max_age_days: int
    lookback_weeks: int
    half_life_weeks: float
    min_effective_history_weeks: float
    empirical_weight_cap: float
    regime_weight: float
    shock_weight: float
    industry_weight: float
    industry_aggregate_weight: float
    sector_weight: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the Stage 9 weekly industry-first macro fit layer.")
    parser.add_argument("--config", type=Path, default=None, help="Path to macro raw YAML config.")
    parser.add_argument("--serving-db-path", type=Path, default=None, help="Optional serving SQLite path override.")
    parser.add_argument("--start-date", type=str, default=None, help="Optional Stage 9 start YYYY-MM-DD override.")
    parser.add_argument("--end-date", type=str, default=None, help="Optional Stage 9 end YYYY-MM-DD override.")
    return parser.parse_args()


def _clip(value: float, *, lo: float = -2.0, hi: float = 2.0) -> float:
    return float(min(max(float(value), lo), hi))


def _resolve_layer_config(cfg: dict[str, Any], config_path: Path) -> IndustryMacroConfig:
    raw_cfg = dict(cfg_get(cfg, "industry_macro_layer", default={}) or {})
    production_output_root = resolve_path(config_path, str(raw_cfg.get("production_output_root", "output")))
    output_dir = resolve_path(config_path, str(raw_cfg.get("output_dir", "MacroLayer/out/industry_macro")))
    if production_output_root is None or output_dir is None:
        raise ValueError("Stage 9 config paths could not be resolved.")
    source_mode = str(raw_cfg.get("source_mode", "hybrid")).strip().lower() or "hybrid"
    if source_mode not in {
        "staging_snapshot_store",
        "backtest_history",
        "tier1_optimizer_universe",
        "sec_resolved_snapshot",
        "hybrid",
    }:
        raise ValueError(
            "industry_macro_layer.source_mode must be one of: "
            "staging_snapshot_store, backtest_history, tier1_optimizer_universe, sec_resolved_snapshot, hybrid."
        )
    sec_db_path_raw = str(raw_cfg.get("sec_db_path", "") or "").strip()
    sec_db_path = resolve_path(config_path, sec_db_path_raw) if sec_db_path_raw else None
    regime_weight = float(raw_cfg.get("regime_weight", 0.75))
    shock_weight = float(raw_cfg.get("shock_weight", 0.25))
    if abs((regime_weight + shock_weight) - 1.0) > 1e-9:
        raise ValueError("industry_macro_layer regime_weight + shock_weight must sum to 1.0.")
    industry_weight = float(raw_cfg.get("industry_weight", 0.50))
    aggregate_weight = float(raw_cfg.get("industry_aggregate_weight", 0.30))
    sector_weight = float(raw_cfg.get("sector_weight", 0.20))
    if abs((industry_weight + aggregate_weight + sector_weight) - 1.0) > 1e-9:
        raise ValueError("industry_macro_layer hierarchy weights must sum to 1.0.")
    return IndustryMacroConfig(
        cadence=str(raw_cfg.get("cadence", "W-FRI")).strip() or "W-FRI",
        strategy_key=str(raw_cfg.get("strategy_key", "mf_wf")).strip() or "mf_wf",
        source_mode=source_mode,
        production_output_root=production_output_root,
        production_file_glob=str(raw_cfg.get("production_file_glob", "tier1_optimizer_universe_*.csv")).strip()
        or "tier1_optimizer_universe_*.csv",
        sec_snapshot_enabled=bool(raw_cfg.get("sec_snapshot_enabled", True)),
        sec_db_path=sec_db_path,
        sec_snapshot_table=str(
            raw_cfg.get("sec_snapshot_table", "sec_fundamental_snapshot_filled_security_t1_resolved")
        ).strip()
        or "sec_fundamental_snapshot_filled_security_t1_resolved",
        sec_snapshot_start_date=str(raw_cfg.get("sec_snapshot_start_date", "") or "").strip() or None,
        sec_snapshot_end_date=str(raw_cfg.get("sec_snapshot_end_date", "") or "").strip() or None,
        output_dir=output_dir,
        min_industry_members=max(1, int(raw_cfg.get("min_industry_members", 4))),
        min_aggregate_members=max(1, int(raw_cfg.get("min_aggregate_members", 8))),
        min_sector_members=max(1, int(raw_cfg.get("min_sector_members", 12))),
        context_max_age_days=max(0, int(raw_cfg.get("context_max_age_days", 10))),
        lookback_weeks=max(4, int(raw_cfg.get("lookback_weeks", 52))),
        half_life_weeks=max(1.0, float(raw_cfg.get("half_life_weeks", 13.0))),
        min_effective_history_weeks=max(1.0, float(raw_cfg.get("min_effective_history_weeks", 12.0))),
        empirical_weight_cap=float(np.clip(raw_cfg.get("empirical_weight_cap", 0.35), 0.0, 1.0)),
        regime_weight=regime_weight,
        shock_weight=shock_weight,
        industry_weight=industry_weight,
        industry_aggregate_weight=aggregate_weight,
        sector_weight=sector_weight,
    )


def _latest_regime_decision_run_raw_ingest_id(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        """
        SELECT raw_ingest_run_id
        FROM macro_serving_run
        WHERE build_step = 'regime_decision_layer'
          AND status = 'completed'
        ORDER BY COALESCE(completed_at_utc, started_at_utc) DESC, rowid DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    return str(row["raw_ingest_run_id"] or "") or None


def _select_cadence_dates(dates: pd.Series | pd.Index, *, cadence: str) -> pd.DatetimeIndex:
    idx = pd.DatetimeIndex(pd.to_datetime(pd.Series(dates), errors="coerce").dropna().unique()).sort_values()
    if idx.empty:
        raise ValueError("No snapshot dates were available after score-history load.")
    scaffold = pd.DataFrame({"Date": idx}, index=idx)
    sampled = scaffold.resample(cadence).max()["Date"].dropna()
    sampled = pd.DatetimeIndex(pd.to_datetime(sampled, errors="coerce").dropna().unique()).sort_values()
    if sampled.empty:
        raise ValueError(f"No snapshot dates remained after cadence sampling with {cadence}.")
    return sampled


def _validate_sql_identifier(value: str, *, label: str) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", text):
        raise ValueError(f"Invalid {label}: {value!r}")
    return text


def _normalize_score_panel(frame: pd.DataFrame, *, source_name: str = "") -> pd.DataFrame:
    rename_map: dict[str, str] = {}
    if "AsOfDate" in frame.columns and "Date" not in frame.columns:
        rename_map["AsOfDate"] = "Date"
    if "as_of_date" in frame.columns and "Date" not in frame.columns:
        rename_map["as_of_date"] = "Date"
    if "ticker" in frame.columns and "Ticker" not in frame.columns:
        rename_map["ticker"] = "Ticker"
    if "FinalScore" in frame.columns and "Score" not in frame.columns:
        rename_map["FinalScore"] = "Score"
    out = frame.rename(columns=rename_map).copy()
    if "ScoreVersion" in out.columns:
        out = out.drop(columns=["ScoreVersion"])
    if "Score" not in out.columns:
        out["Score"] = 50.0
    if "Rating" not in out.columns:
        out["Rating"] = "Hold"
    required_cols = {"Date", "Ticker", "sector", "industry", "industry_aggregate"}
    missing = required_cols - set(out.columns)
    if missing:
        raise ValueError(f"Stage 9 score panel is missing required columns: {sorted(missing)}")

    out["Date"] = pd.to_datetime(out["Date"], errors="coerce").dt.normalize()
    out["Ticker"] = out["Ticker"].astype(str).str.upper().str.strip()
    out["Score"] = pd.to_numeric(out["Score"], errors="coerce")
    out["Rating"] = out["Rating"].fillna("").astype(str).str.strip()
    out["sector"] = out["sector"].fillna("").astype(str).str.strip()
    out["industry"] = out["industry"].fillna("").astype(str).str.strip()
    out["industry_aggregate"] = out["industry_aggregate"].fillna("").astype(str).str.strip()

    if "Company" in out.columns:
        out["Company"] = out["Company"].fillna("").astype(str).str.strip()
    else:
        out["Company"] = ""
    if "ScoreApproach" in out.columns:
        out["ScoreApproach"] = out["ScoreApproach"].fillna("").astype(str).str.strip()
    else:
        out["ScoreApproach"] = ""
    if "RunId" in out.columns:
        out["RunId"] = out["RunId"].fillna("").astype(str).str.strip()
    else:
        out["RunId"] = ""
    if "BaseOptimizerEligible" not in out.columns:
        out["BaseOptimizerEligible"] = True
    if "EarningsBlocked_7D" not in out.columns:
        out["EarningsBlocked_7D"] = False

    out.loc[out["industry_aggregate"] == "", "industry_aggregate"] = out.loc[
        out["industry_aggregate"] == "",
        "industry",
    ]
    out.loc[out["industry"] == "", "industry"] = out.loc[out["industry"] == "", "industry_aggregate"]
    source_priority = {
        "sec_resolved_snapshot": -1,
        "backtest_score_history": 0,
        "tier1_optimizer_universe": 1,
        "staging_snapshot_store": 2,
        "staging_run_output": 3,
    }
    if source_name:
        out["SnapshotSource"] = str(source_name)
        out["SourcePriority"] = source_priority.get(str(source_name), 0)
    else:
        if "SnapshotSource" not in out.columns:
            out["SnapshotSource"] = ""
        out["SnapshotSource"] = out["SnapshotSource"].fillna("").astype(str).str.strip()
        if "SourcePriority" not in out.columns:
            out["SourcePriority"] = out["SnapshotSource"].map(source_priority).fillna(0)
        out["SourcePriority"] = pd.to_numeric(out["SourcePriority"], errors="coerce").fillna(
            out["SnapshotSource"].map(source_priority).fillna(0)
        )
    out = out.loc[
        out["Date"].notna()
        & out["Score"].notna()
        & (out["Ticker"] != "")
        & (out["sector"] != "")
        & (out["industry"] != "")
        & (out["industry_aggregate"] != "")
    ].copy()
    return out.drop_duplicates(subset=["Date", "Ticker"], keep="last").sort_values(["Date", "Ticker"]).reset_index(drop=True)


def _load_sec_resolved_snapshot_panel(layer_cfg: IndustryMacroConfig) -> pd.DataFrame:
    if not layer_cfg.sec_snapshot_enabled:
        return pd.DataFrame()
    if layer_cfg.sec_db_path is None:
        if layer_cfg.source_mode == "sec_resolved_snapshot":
            raise ValueError("industry_macro_layer.sec_db_path is required for source_mode=sec_resolved_snapshot.")
        return pd.DataFrame()
    if not layer_cfg.sec_db_path.exists():
        if layer_cfg.source_mode == "sec_resolved_snapshot":
            raise FileNotFoundError(f"SEC fundamentals DB not found: {layer_cfg.sec_db_path}")
        logger.info("Stage 9 SEC snapshot source skipped because DB does not exist: %s", layer_cfg.sec_db_path)
        return pd.DataFrame()

    table_name = _validate_sql_identifier(layer_cfg.sec_snapshot_table, label="SEC snapshot table")
    conn = sqlite3.connect(str(layer_cfg.sec_db_path), timeout=30.0)
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", [table_name]).fetchone() is None:
            raise ValueError(f"SEC snapshot table does not exist: {table_name}")
        params: list[str] = []
        where: list[str] = []
        if layer_cfg.sec_snapshot_start_date:
            where.append("as_of_date >= ?")
            params.append(layer_cfg.sec_snapshot_start_date)
        if layer_cfg.sec_snapshot_end_date:
            where.append("as_of_date <= ?")
            params.append(layer_cfg.sec_snapshot_end_date)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        query = f"""
            SELECT
                as_of_date AS Date,
                COALESCE(NULLIF(TRIM(canonical_ticker), ''), NULLIF(TRIM(ticker), '')) AS Ticker,
                sector,
                industry,
                industry_aggregate,
                50.0 AS Score,
                'Hold' AS Rating,
                'sec_resolved_snapshot' AS ScoreApproach
            FROM {table_name}
            {where_sql}
        """
        frame = pd.read_sql_query(query, conn, params=params)
    finally:
        conn.close()
    if frame.empty:
        return pd.DataFrame()
    return _normalize_score_panel(frame, source_name="sec_resolved_snapshot")


def _load_backtest_score_panel(
    layer_cfg: IndustryMacroConfig,
    *,
    backtest_cfg: dict[str, Any],
    repo_root: Path,
    start_date: Any = None,
    end_date: Any = None,
) -> pd.DataFrame:
    del layer_cfg, backtest_cfg, repo_root
    return _normalize_score_panel(load_staging_score_panel(start_date=start_date, end_date=end_date))


def _discover_production_universe_paths(layer_cfg: IndustryMacroConfig) -> list[Path]:
    root = layer_cfg.production_output_root
    pattern = layer_cfg.production_file_glob
    candidates = {p.resolve() for p in root.glob(pattern)}
    candidates.update({p.resolve() for p in root.glob(f"*/{pattern}")})
    return sorted(candidates)


def _load_production_score_panel(layer_cfg: IndustryMacroConfig) -> tuple[pd.DataFrame, list[Path]]:
    paths = _discover_production_universe_paths(layer_cfg)
    frames: list[pd.DataFrame] = []
    for path in paths:
        frame = pd.read_csv(path)
        if frame is None or frame.empty:
            continue
        frames.append(_normalize_score_panel(frame, source_name="tier1_optimizer_universe"))
    if not frames:
        return pd.DataFrame(), paths
    out = pd.concat(frames, ignore_index=True)
    out = out.sort_values(["Date", "Ticker", "SourcePriority"]).drop_duplicates(
        subset=["Date", "Ticker"],
        keep="last",
    )
    return out.reset_index(drop=True), paths


def _load_weekly_score_panel(
    layer_cfg: IndustryMacroConfig,
    *,
    start_date: Any = None,
    end_date: Any = None,
) -> tuple[pd.DataFrame, pd.DatetimeIndex, dict[str, Any], Path]:
    backtest_cfg: dict[str, Any] = {}
    repo_root = REPO_ROOT
    panels: list[pd.DataFrame] = []
    source_messages: list[str] = []

    if layer_cfg.source_mode in {"sec_resolved_snapshot", "hybrid"}:
        sec_panel = _load_sec_resolved_snapshot_panel(layer_cfg)
        if not sec_panel.empty:
            panels.append(sec_panel)
            source_messages.append(f"sec_rows={len(sec_panel)}")
        elif layer_cfg.source_mode == "sec_resolved_snapshot":
            raise ValueError("Stage 9 source_mode=sec_resolved_snapshot but no SEC snapshot rows were loaded.")

    if layer_cfg.source_mode in {"staging_snapshot_store", "backtest_history", "hybrid"}:
        backtest_panel = _load_backtest_score_panel(
            layer_cfg,
            backtest_cfg=backtest_cfg,
            repo_root=repo_root,
            start_date=start_date,
            end_date=end_date,
        )
        if not backtest_panel.empty:
            panels.append(backtest_panel)
            source_messages.append(f"staging_snapshot_rows={len(backtest_panel)}")

    if layer_cfg.source_mode in {"tier1_optimizer_universe", "hybrid"}:
        production_panel, production_paths = _load_production_score_panel(layer_cfg)
        if production_panel.empty:
            if layer_cfg.source_mode == "tier1_optimizer_universe":
                raise ValueError(
                    "Stage 9 source_mode=tier1_optimizer_universe but no canonical "
                    f"tier1 optimizer universe files matched {layer_cfg.production_output_root / layer_cfg.production_file_glob}."
                )
            logger.info(
                "Stage 9 hybrid source did not find any canonical tier1 optimizer universe snapshots under %s.",
                layer_cfg.production_output_root,
            )
        else:
            panels.append(production_panel)
            source_messages.append(
                f"production_rows={len(production_panel)} production_files={len(production_paths)}"
            )

    if not panels:
        raise ValueError("Stage 9 could not load any score panel rows from the configured sources.")

    out = pd.concat(panels, ignore_index=True)
    production_dates = set(
        out.loc[out["SnapshotSource"].eq("tier1_optimizer_universe"), "Date"].dropna().unique().tolist()
    )
    staging_dates = set(
        out.loc[out["SnapshotSource"].isin({"staging_snapshot_store", "staging_run_output"}), "Date"]
        .dropna()
        .unique()
        .tolist()
    )
    authoritative_dates = production_dates | staging_dates
    if authoritative_dates:
        # A canonical Staging/production snapshot is authoritative for its as-of date. Do
        # not leave lower-priority legacy/SEC-only tickers in the same live date's universe.
        out = out.loc[
            ~out["Date"].isin(authoritative_dates)
            | out["SnapshotSource"].isin({"tier1_optimizer_universe", "staging_snapshot_store", "staging_run_output"})
        ].copy()
    out = out.sort_values(["Date", "Ticker", "SourcePriority"]).drop_duplicates(
        subset=["Date", "Ticker"],
        keep="last",
    ).reset_index(drop=True)
    weekly_dates = _select_cadence_dates(out["Date"], cadence=layer_cfg.cadence)
    out = out.loc[out["Date"].isin(weekly_dates)].copy()
    if out.empty:
        raise ValueError("Stage 9 score-history panel was empty after cadence filtering and metadata cleanup.")
    logger.info(
        "Stage 9 score panel loaded: source_mode=%s %s weekly_dates=%d",
        layer_cfg.source_mode,
        " ".join(source_messages),
        len(weekly_dates),
    )
    return out, weekly_dates, backtest_cfg, repo_root
def _load_snapshot_prices(
    panel: pd.DataFrame,
    *,
    weekly_dates: pd.DatetimeIndex,
    backtest_cfg: dict[str, Any],
    repo_root: Path,
    layer_cfg: IndustryMacroConfig,
) -> pd.DataFrame:
    tickers = sorted(item for item in panel["Ticker"].dropna().astype(str).str.upper().unique().tolist() if item)
    min_dt = pd.Timestamp(weekly_dates.min()).normalize() - pd.Timedelta(days=int(layer_cfg.lookback_weeks * 10))
    max_dt = pd.Timestamp(weekly_dates.max()).normalize() + pd.Timedelta(days=7)
    freshness_as_of = pd.Timestamp(weekly_dates.max()).normalize()
    prices = load_staging_prices(
        tickers=tickers,
        start_date=min_dt,
        end_date=max_dt,
        freshness_as_of=freshness_as_of,
    )
    return staleness_gated_weekly(prices, weekly_dates, max_stale_days=MAX_STAGE2_PRICE_STALE_DAYS)


def _resolve_history_bounds(
    conn: sqlite3.Connection,
    *,
    weekly_dates: pd.DatetimeIndex,
    start_override: str | None,
    end_override: str | None,
) -> tuple[pd.DatetimeIndex, date, date]:
    start_date = parse_iso_date(start_override)
    end_date = parse_iso_date(end_override)
    regime_row = conn.execute("SELECT MIN(as_of_date) AS min_as_of_date, MAX(as_of_date) AS max_as_of_date FROM macro_regime_decision_daily").fetchone()
    feature_row = conn.execute("SELECT MIN(as_of_date) AS min_as_of_date, MAX(as_of_date) AS max_as_of_date FROM macro_feature_daily").fetchone()
    composite_row = conn.execute("SELECT MIN(as_of_date) AS min_as_of_date, MAX(as_of_date) AS max_as_of_date FROM macro_composite_daily").fetchone()

    bound_rows = {
        "regime decisions": regime_row,
        "features": feature_row,
        "composites": composite_row,
    }
    missing_bounds = [
        name
        for name, row in bound_rows.items()
        if row is None or row["min_as_of_date"] is None or row["max_as_of_date"] is None
    ]
    if missing_bounds:
        raise ValueError(f"Stage 9 cannot resolve history bounds; empty inputs: {', '.join(missing_bounds)}.")

    history_start = max(
        pd.Timestamp(weekly_dates.min()).date(),
        parse_iso_date(regime_row["min_as_of_date"]),
        parse_iso_date(feature_row["min_as_of_date"]),
        parse_iso_date(composite_row["min_as_of_date"]),
    )
    history_end = min(
        pd.Timestamp(weekly_dates.max()).date(),
        parse_iso_date(regime_row["max_as_of_date"]),
        parse_iso_date(feature_row["max_as_of_date"]),
        parse_iso_date(composite_row["max_as_of_date"]),
    )
    if history_end < history_start:
        raise ValueError("Stage 9 could not find overlapping history across score snapshots, features, and regime outputs.")
    history_dates = weekly_dates[(weekly_dates.date >= history_start) & (weekly_dates.date <= history_end)]
    if history_dates.empty:
        raise ValueError("Stage 9 has no overlapping weekly dates after applying serving-history bounds.")
    if start_date is None:
        start_date = history_start
    if end_date is None:
        end_date = history_end
    if end_date < start_date:
        raise ValueError(f"Stage 9 end date {end_date.isoformat()} is before start date {start_date.isoformat()}.")
    if start_date < history_start or end_date > history_end:
        raise ValueError("Requested Stage 9 build window falls outside the available weekly history.")
    return history_dates, start_date, end_date


def _build_group_frames(panel_returns: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    universe = panel_returns.groupby("Date", as_index=False).agg(universe_return=("weekly_return", "mean")).rename(columns={"Date": "as_of_date"})

    def _agg(group_cols: list[str], key_name: str) -> pd.DataFrame:
        grouped = (
            panel_returns.groupby(["Date", *group_cols], as_index=False)
            .agg(basket_return=("weekly_return", "mean"), member_count=("Ticker", "nunique"))
            .rename(columns={"Date": "as_of_date"})
        )
        grouped = grouped.merge(universe, on="as_of_date", how="left")
        grouped["excess_return"] = grouped["basket_return"] - grouped["universe_return"]
        if key_name == "sector_key":
            grouped[key_name] = grouped["sector"]
        elif key_name == "industry_aggregate_key":
            grouped[key_name] = grouped["sector"] + "||" + grouped["industry_aggregate"]
        else:
            grouped[key_name] = grouped["sector"] + "||" + grouped["industry_aggregate"] + "||" + grouped["industry"]
        return grouped.sort_values(["as_of_date", key_name]).reset_index(drop=True)

    return (
        _agg(["sector"], "sector_key"),
        _agg(["sector", "industry_aggregate"], "industry_aggregate_key"),
        _agg(["sector", "industry_aggregate", "industry"], "industry_key"),
    )


def _covered_context_rows(frame: pd.DataFrame, *, coverage_col: str) -> pd.DataFrame:
    """Keep only fully covered macro context rows before as-of carry-forward."""
    if frame.empty or coverage_col not in frame.columns:
        return frame
    coverage = pd.to_numeric(frame[coverage_col], errors="coerce").fillna(0).astype(int)
    return frame.loc[coverage.eq(1)].copy()


def _load_weekly_context(
    conn: sqlite3.Connection,
    *,
    start_date: str,
    end_date: str,
    weekly_dates: pd.DatetimeIndex,
    max_age_days: int,
) -> pd.DataFrame:
    weekly_frame = pd.DataFrame({"as_of_date": weekly_dates})
    regime_frame = pd.read_sql_query(
        f"""
        SELECT as_of_date, {", ".join(CURRENT_PROB_COLUMNS)}, coverage_flag
        FROM macro_regime_smoothed_daily
        WHERE as_of_date BETWEEN ? AND ?
        ORDER BY as_of_date
        """,
        conn,
        params=[start_date, end_date],
        parse_dates=["as_of_date"],
    )
    regime_frame = _covered_context_rows(regime_frame, coverage_col="coverage_flag")
    decision_frame = pd.read_sql_query(
        """
        SELECT as_of_date, active_current_regime, active_next_regime, coverage_flag
        FROM macro_regime_decision_daily
        WHERE as_of_date BETWEEN ? AND ?
        ORDER BY as_of_date
        """,
        conn,
        params=[start_date, end_date],
        parse_dates=["as_of_date"],
    ).rename(columns={"coverage_flag": "decision_coverage_flag"})
    decision_frame = _covered_context_rows(decision_frame, coverage_col="decision_coverage_flag")
    composite_frame = pd.read_sql_query(
        """
        SELECT as_of_date, composite_value_smoothed AS shock_composite_value, coverage_flag
        FROM macro_composite_daily
        WHERE composite_key = 'SHOCK'
          AND as_of_date BETWEEN ? AND ?
        ORDER BY as_of_date
        """,
        conn,
        params=[start_date, end_date],
        parse_dates=["as_of_date"],
    ).rename(columns={"coverage_flag": "shock_coverage_flag"})
    composite_frame = _covered_context_rows(composite_frame, coverage_col="shock_coverage_flag")

    metric_keys = sorted({spec[0] for specs in SHOCK_FEATURE_SPECS.values() for spec in specs})
    feature_frame = pd.read_sql_query(
        f"""
        SELECT as_of_date, metric_key, feature_name, standardized_value
        FROM macro_feature_daily
        WHERE as_of_date BETWEEN ? AND ?
          AND metric_key IN ({",".join("?" for _ in metric_keys)})
        ORDER BY as_of_date
        """,
        conn,
        params=[start_date, end_date, *metric_keys],
        parse_dates=["as_of_date"],
    )
    feature_frame["metric_feature"] = feature_frame["metric_key"].astype(str) + "::" + feature_frame["feature_name"].astype(str)
    wanted = {f"{metric_key}::{feature_name}": sign for specs in SHOCK_FEATURE_SPECS.values() for metric_key, feature_name, sign in specs}
    feature_frame = feature_frame.loc[feature_frame["metric_feature"].isin(wanted)].copy()
    if feature_frame.empty:
        feature_wide = pd.DataFrame(columns=["as_of_date"])
    else:
        feature_wide = feature_frame.pivot_table(index="as_of_date", columns="metric_feature", values="standardized_value", aggfunc="last").reset_index()

    tolerance = pd.Timedelta(days=int(max_age_days))
    merged = pd.merge_asof(weekly_frame.sort_values("as_of_date"), regime_frame.sort_values("as_of_date"), on="as_of_date", direction="backward", tolerance=tolerance)
    merged = pd.merge_asof(merged.sort_values("as_of_date"), decision_frame.sort_values("as_of_date"), on="as_of_date", direction="backward", tolerance=tolerance)
    merged = pd.merge_asof(merged.sort_values("as_of_date"), composite_frame.sort_values("as_of_date"), on="as_of_date", direction="backward", tolerance=tolerance)
    merged = pd.merge_asof(merged.sort_values("as_of_date"), feature_wide.sort_values("as_of_date"), on="as_of_date", direction="backward", tolerance=tolerance)

    for family, specs in SHOCK_FEATURE_SPECS.items():
        cols: list[pd.Series] = []
        for metric_key, feature_name, sign in specs:
            col_name = f"{metric_key}::{feature_name}"
            col_data = merged.get(col_name)
            if col_data is None:
                cols.append(pd.Series(np.nan, index=merged.index, dtype="float64"))
            else:
                cols.append(pd.to_numeric(col_data, errors="coerce") * float(sign))
        merged[f"{family}_shock_value"] = pd.concat(cols, axis=1).mean(axis=1, skipna=True).clip(-3.0, 3.0)

    merged["coverage_flag"] = (
        merged["coverage_flag"].fillna(0).astype(int)
        & merged["decision_coverage_flag"].fillna(0).astype(int)
        & merged["shock_coverage_flag"].fillna(0).astype(int)
    ).astype(int)
    return merged.sort_values("as_of_date").reset_index(drop=True)


def _apply_adjustments(base: dict[str, float], *, text: str, rules: list[tuple[tuple[str, ...], dict[str, float]]]) -> dict[str, float]:
    out = dict(base)
    lowered = text.lower()
    for keywords, adjustments in rules:
        if any(keyword in lowered for keyword in keywords):
            for key, delta in adjustments.items():
                out[key] = _clip(out.get(key, 0.0) + float(delta))
    return out


def _regime_prior_map(*, sector: str, industry_aggregate: str, industry: str, level: str) -> dict[str, float]:
    base = dict(SECTOR_REGIME_PRIORS.get(sector, DEFAULT_REGIME_PRIOR))
    if level == "sector":
        return {key: _clip(val) for key, val in base.items()}
    group_text = industry_aggregate if level == "industry_aggregate" else f"{industry_aggregate} | {industry}"
    return _apply_adjustments(base, text=group_text, rules=REGIME_KEYWORD_RULES)


def _shock_prior_map(*, sector: str, industry_aggregate: str, industry: str, level: str) -> dict[str, float]:
    base = dict(SECTOR_SHOCK_PRIORS.get(sector, DEFAULT_SHOCK_PRIOR))
    if level == "sector":
        return {key: _clip(val) for key, val in base.items()}
    group_text = industry_aggregate if level == "industry_aggregate" else f"{industry_aggregate} | {industry}"
    return _apply_adjustments(base, text=group_text, rules=SHOCK_KEYWORD_RULES)


def _weighted_regime_score(prior_map: dict[str, float], probs: np.ndarray) -> float:
    return float(sum(float(prior_map[regime]) * float(prob) for regime, prob in zip(REGIME_ORDER, probs)))


def _weighted_shock_score(prior_map: dict[str, float], row: pd.Series) -> float:
    total = 0.0
    for family in SHOCK_NAMES:
        raw_value = row.get(f"{family}_shock_value", 0.0)
        try:
            shock_value = float(raw_value)
        except (TypeError, ValueError):
            shock_value = 0.0
        if not np.isfinite(shock_value):
            shock_value = 0.0
        total += float(prior_map.get(family, 0.0)) * shock_value
    return float(total)


def _normalize_probability_matrix(values: np.ndarray) -> np.ndarray:
    arr = np.asarray(values, dtype=float)
    if arr.ndim == 1:
        arr = arr.reshape(1, -1)
    arr = np.where(np.isfinite(arr), arr, 0.0)
    arr = np.clip(arr, 0.0, None)
    row_sums = arr.sum(axis=1)
    bad_rows = row_sums <= 0.0
    if bool(np.any(bad_rows)):
        arr[bad_rows, :] = 1.0 / float(len(REGIME_ORDER))
        row_sums = arr.sum(axis=1)
    return arr / row_sums[:, None]


def _zscore_cross_section(values: pd.Series) -> pd.Series:
    out = values.astype(float).copy()
    mask = out.notna()
    if int(mask.sum()) <= 1:
        out.loc[mask] = 0.0
        return out
    std = float(out.loc[mask].std(ddof=1))
    if not np.isfinite(std) or std <= 1e-12:
        out.loc[mask] = 0.0
        return out
    mean = float(out.loc[mask].mean())
    out.loc[mask] = (out.loc[mask] - mean) / std
    return out


def _compute_level_fit_frame(
    level_frame: pd.DataFrame,
    *,
    group_key_col: str,
    level_name: str,
    current_context: pd.DataFrame,
    layer_cfg: IndustryMacroConfig,
    min_members: int,
) -> pd.DataFrame:
    dates = pd.DatetimeIndex(sorted(level_frame["as_of_date"].unique()))
    level_frame = level_frame.copy()
    level_frame["as_of_date"] = pd.to_datetime(level_frame["as_of_date"], errors="coerce").dt.normalize()
    return_wide = level_frame.pivot(index="as_of_date", columns=group_key_col, values="excess_return").reindex(dates)
    metadata_cols = [group_key_col] + [col for col in ("sector", "industry_aggregate", "industry") if col in level_frame.columns]
    metadata = level_frame[metadata_cols].drop_duplicates(subset=[group_key_col]).set_index(group_key_col)

    regime_lookup: dict[str, dict[str, float]] = {}
    shock_lookup: dict[str, dict[str, float]] = {}
    for group_key, row in metadata.iterrows():
        sector = str(row.get("sector", "") or "")
        industry_aggregate = str(row.get("industry_aggregate", "") or "")
        industry = str(row.get("industry", "") or "")
        regime_lookup[str(group_key)] = _regime_prior_map(sector=sector, industry_aggregate=industry_aggregate, industry=industry, level=level_name)
        shock_lookup[str(group_key)] = _shock_prior_map(sector=sector, industry_aggregate=industry_aggregate, industry=industry, level=level_name)

    context = current_context.copy()
    context["as_of_date"] = pd.to_datetime(context["as_of_date"], errors="coerce").dt.normalize()
    context = context.set_index("as_of_date").reindex(dates)
    probs_wide = context[list(CURRENT_PROB_COLUMNS)].to_numpy(dtype=float)
    active_regimes = context["active_current_regime"].fillna("").astype(str).to_numpy()

    rows: list[dict[str, object]] = []
    for idx, as_of_date in enumerate(dates):
        current_probs = _normalize_probability_matrix(probs_wide[idx])[0]

        hist_returns = return_wide.iloc[max(0, idx - int(layer_cfg.lookback_weeks)):idx]
        empirical_mean = pd.Series(np.nan, index=return_wide.columns, dtype="float64")
        effective_history = pd.Series(np.nan, index=return_wide.columns, dtype="float64")
        if not hist_returns.empty:
            hist_probs = _normalize_probability_matrix(probs_wide[max(0, idx - int(layer_cfg.lookback_weeks)):idx])
            similarity = np.clip(hist_probs @ current_probs, 0.0, 1.0)
            age_steps = np.arange(len(hist_returns) - 1, -1, -1, dtype=float)
            decay = np.power(0.5, age_steps / float(layer_cfg.half_life_weeks))
            history_active = active_regimes[max(0, idx - int(layer_cfg.lookback_weeks)):idx]
            current_active = active_regimes[idx]
            active_match = ((history_active == current_active) & (history_active != "") & (current_active != "")).astype(float)
            weights = decay * (0.5 + 0.5 * similarity) * (1.0 + 0.25 * active_match)
            values = hist_returns.to_numpy(dtype=float)
            mask = np.isfinite(values)
            weights_2d = weights[:, None] * mask.astype(float)
            denom = weights_2d.sum(axis=0)
            numer = (np.where(mask, values, 0.0) * weights[:, None]).sum(axis=0)
            empirical_mean = pd.Series(np.divide(numer, denom, out=np.full(len(denom), np.nan, dtype=float), where=denom > 0.0), index=hist_returns.columns)
            denom_sq = np.square(weights_2d).sum(axis=0)
            effective_history = pd.Series(np.divide(np.square(denom), denom_sq, out=np.full(len(denom_sq), np.nan, dtype=float), where=denom_sq > 0.0), index=hist_returns.columns)

        empirical_z = _zscore_cross_section(empirical_mean)
        current_rows = level_frame.loc[level_frame["as_of_date"] == as_of_date].copy()
        current_ctx = context.loc[as_of_date]
        for row in current_rows.itertuples(index=False):
            group_key = str(getattr(row, group_key_col))
            regime_prior_score = _weighted_regime_score(regime_lookup[group_key], current_probs)
            shock_prior_score = _weighted_shock_score(shock_lookup[group_key], current_ctx)
            prior_score = float(layer_cfg.regime_weight * regime_prior_score + layer_cfg.shock_weight * shock_prior_score)
            empirical_score = float(empirical_z.get(group_key, np.nan))
            effective_weeks = float(effective_history.get(group_key, np.nan))
            member_count = int(getattr(row, "member_count"))
            history_quality = float(np.clip((effective_weeks if np.isfinite(effective_weeks) else 0.0) / layer_cfg.min_effective_history_weeks, 0.0, 1.0))
            member_quality = float(np.clip(member_count / float(min_members), 0.0, 1.0))
            empirical_weight = float(layer_cfg.empirical_weight_cap * history_quality * member_quality)
            if not np.isfinite(empirical_score):
                empirical_weight = 0.0
                empirical_score = prior_score
            level_fit_score = float((1.0 - empirical_weight) * prior_score + empirical_weight * empirical_score)
            item = {
                "as_of_date": pd.Timestamp(as_of_date).normalize(),
                group_key_col: group_key,
                "active_current_regime": str(current_ctx.get("active_current_regime", "") or ""),
                "active_next_regime": str(current_ctx.get("active_next_regime", "") or ""),
                "regime_prior_score": regime_prior_score,
                "shock_prior_score": shock_prior_score,
                "prior_score": prior_score,
                "empirical_score": empirical_score,
                "empirical_weight": empirical_weight,
                "level_fit_score": level_fit_score,
                "final_score": level_fit_score,
                "basket_return": float(getattr(row, "basket_return")) if np.isfinite(getattr(row, "basket_return")) else np.nan,
                "universe_return": float(getattr(row, "universe_return")) if np.isfinite(getattr(row, "universe_return")) else np.nan,
                "excess_return": float(getattr(row, "excess_return")) if np.isfinite(getattr(row, "excess_return")) else np.nan,
                "member_count": member_count,
                "effective_history_weeks": float(effective_weeks) if np.isfinite(effective_weeks) else np.nan,
                "oil_shock_value": float(current_ctx.get("oil_shock_value", np.nan)),
                "commodity_shock_value": float(current_ctx.get("commodity_shock_value", np.nan)),
                "dollar_shock_value": float(current_ctx.get("dollar_shock_value", np.nan)),
                "real_yield_shock_value": float(current_ctx.get("real_yield_shock_value", np.nan)),
                "credit_shock_value": float(current_ctx.get("credit_shock_value", np.nan)),
                "shock_composite_value": float(current_ctx.get("shock_composite_value", np.nan)),
                "coverage_flag": int(current_ctx.get("coverage_flag", 0)) if member_count >= min_members else 0,
            }
            for col in ("sector", "industry_aggregate", "industry"):
                if hasattr(row, col):
                    item[col] = getattr(row, col)
            rows.append(item)

    return pd.DataFrame(rows)


def _build_prior_frames(panel: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    groups = pd.concat(
        [
            panel[["sector"]].drop_duplicates().assign(level="sector", industry_aggregate="", industry=""),
            panel[["sector", "industry_aggregate"]].drop_duplicates().assign(level="industry_aggregate", industry=""),
            panel[["sector", "industry_aggregate", "industry"]].drop_duplicates().assign(level="industry"),
        ],
        axis=0,
        ignore_index=True,
        sort=False,
    ).drop_duplicates(subset=["level", "sector", "industry_aggregate", "industry"])
    regime_rows: list[dict[str, object]] = []
    shock_rows: list[dict[str, object]] = []
    for row in groups.itertuples(index=False):
        regime_map = _regime_prior_map(sector=str(row.sector or ""), industry_aggregate=str(row.industry_aggregate or ""), industry=str(row.industry or ""), level=str(row.level))
        shock_map = _shock_prior_map(sector=str(row.sector or ""), industry_aggregate=str(row.industry_aggregate or ""), industry=str(row.industry or ""), level=str(row.level))
        regime_rows.append({"level": row.level, "sector_name": row.sector, "industry_aggregate_name": row.industry_aggregate, "industry_name": row.industry, **{f"regime_{regime.lower()}": float(regime_map[regime]) for regime in REGIME_ORDER}})
        shock_rows.append({"level": row.level, "sector_name": row.sector, "industry_aggregate_name": row.industry_aggregate, "industry_name": row.industry, **{f"{name}_exposure": float(shock_map[name]) for name in SHOCK_NAMES}})
    return pd.DataFrame(regime_rows), pd.DataFrame(shock_rows)


def _write_atomic_csv(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    frame.to_csv(tmp_path, index=False)
    tmp_path.replace(path)


def _rows_from_frame(frame: pd.DataFrame, columns: list[str]) -> list[tuple[Any, ...]]:
    if frame.empty:
        return []
    return [tuple(row) for row in frame.loc[:, columns].itertuples(index=False, name=None)]


def _build_output_frames(
    *,
    sector_fit: pd.DataFrame,
    aggregate_fit: pd.DataFrame,
    industry_fit: pd.DataFrame,
    layer_cfg: IndustryMacroConfig,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    sector_out = sector_fit.rename(columns={"sector": "sector_name"}).copy()
    sector_out["updated_at_utc"] = utc_now_iso()

    aggregate_out = aggregate_fit.rename(columns={"sector": "sector_name", "industry_aggregate": "industry_aggregate_name"}).copy()
    aggregate_out["updated_at_utc"] = utc_now_iso()

    industry_out = industry_fit.rename(
        columns={"sector": "sector_name", "industry_aggregate": "industry_aggregate_name", "industry": "industry_name"}
    ).copy()
    aggregate_component = aggregate_out[["as_of_date", "sector_name", "industry_aggregate_name", "final_score"]].rename(
        columns={"final_score": "industry_aggregate_component_score"}
    )
    sector_component = sector_out[["as_of_date", "sector_name", "final_score"]].rename(
        columns={"final_score": "sector_component_score"}
    )
    industry_out = industry_out.merge(
        aggregate_component,
        on=["as_of_date", "sector_name", "industry_aggregate_name"],
        how="left",
    )
    industry_out = industry_out.merge(
        sector_component,
        on=["as_of_date", "sector_name"],
        how="left",
    )
    industry_out["industry_aggregate_component_score"] = industry_out["industry_aggregate_component_score"].fillna(
        industry_out["level_fit_score"]
    )
    industry_out["sector_component_score"] = industry_out["sector_component_score"].fillna(
        industry_out["industry_aggregate_component_score"]
    )
    industry_out["final_score"] = (
        float(layer_cfg.industry_weight) * industry_out["level_fit_score"].astype(float)
        + float(layer_cfg.industry_aggregate_weight) * industry_out["industry_aggregate_component_score"].astype(float)
        + float(layer_cfg.sector_weight) * industry_out["sector_component_score"].astype(float)
    )
    industry_out["updated_at_utc"] = utc_now_iso()
    return sector_out, aggregate_out, industry_out


def main() -> None:
    configure_pipeline_logging()
    args = parse_args()
    config_path, cfg = load_macro_raw_config(args.config)
    layer_cfg = _resolve_layer_config(cfg, config_path)
    layer_cfg.output_dir.mkdir(parents=True, exist_ok=True)

    score_panel, weekly_dates, backtest_cfg, repo_root = _load_weekly_score_panel(
        layer_cfg,
        start_date=None,
        end_date=args.end_date,
    )
    serving_db_path = resolve_serving_db_path(cfg, config_path, override=args.serving_db_path)
    conn = connect_sqlite(serving_db_path, row_factory=sqlite3.Row)
    init_db(conn)

    history_dates, write_start_date, write_end_date = _resolve_history_bounds(
        conn,
        weekly_dates=weekly_dates,
        start_override=args.start_date,
        end_override=args.end_date,
    )
    history_dates = history_dates[history_dates.date <= write_end_date]
    score_panel = score_panel.loc[score_panel["Date"].isin(history_dates)].copy()
    snapshot_prices = _load_snapshot_prices(
        score_panel,
        weekly_dates=history_dates,
        backtest_cfg=backtest_cfg,
        repo_root=repo_root,
        layer_cfg=layer_cfg,
    )
    returns = snapshot_prices.pct_change(fill_method=None)
    return_long = returns.rename_axis(index="Date", columns="Ticker").stack().rename("weekly_return").reset_index()
    return_long["Date"] = pd.to_datetime(return_long["Date"], errors="coerce").dt.normalize()
    return_long["Ticker"] = return_long["Ticker"].astype(str).str.upper().str.strip()
    panel_returns = score_panel.merge(return_long, on=["Date", "Ticker"], how="left").dropna(subset=["weekly_return"]).reset_index(drop=True)
    if panel_returns.empty:
        raise ValueError("Stage 9 panel returns are empty after merging weekly snapshots with price returns.")

    sector_frame, aggregate_frame, industry_frame = _build_group_frames(panel_returns)
    context = _load_weekly_context(
        conn,
        start_date=history_dates.min().date().isoformat(),
        end_date=write_end_date.isoformat(),
        weekly_dates=history_dates,
        max_age_days=layer_cfg.context_max_age_days,
    )
    regime_prior_frame, shock_prior_frame = _build_prior_frames(score_panel)
    _write_atomic_csv(layer_cfg.output_dir / "industry_regime_prior.csv", regime_prior_frame)
    _write_atomic_csv(layer_cfg.output_dir / "industry_shock_prior.csv", shock_prior_frame)

    sector_fit = _compute_level_fit_frame(sector_frame, group_key_col="sector_key", level_name="sector", current_context=context, layer_cfg=layer_cfg, min_members=layer_cfg.min_sector_members)
    aggregate_fit = _compute_level_fit_frame(aggregate_frame, group_key_col="industry_aggregate_key", level_name="industry_aggregate", current_context=context, layer_cfg=layer_cfg, min_members=layer_cfg.min_aggregate_members)
    industry_fit = _compute_level_fit_frame(industry_frame, group_key_col="industry_key", level_name="industry", current_context=context, layer_cfg=layer_cfg, min_members=layer_cfg.min_industry_members)

    sector_out, aggregate_out, industry_out = _build_output_frames(
        sector_fit=sector_fit,
        aggregate_fit=aggregate_fit,
        industry_fit=industry_fit,
        layer_cfg=layer_cfg,
    )
    sector_write = sector_out.loc[(sector_out["as_of_date"].dt.date >= write_start_date) & (sector_out["as_of_date"].dt.date <= write_end_date)].copy()
    aggregate_write = aggregate_out.loc[(aggregate_out["as_of_date"].dt.date >= write_start_date) & (aggregate_out["as_of_date"].dt.date <= write_end_date)].copy()
    industry_write = industry_out.loc[(industry_out["as_of_date"].dt.date >= write_start_date) & (industry_out["as_of_date"].dt.date <= write_end_date)].copy()

    serving_run_id = uuid.uuid4().hex
    raw_ingest_run_id = _latest_regime_decision_run_raw_ingest_id(conn)
    rows_written = 0
    run_started = False
    try:
        start_serving_run(
            conn,
            serving_run_id=serving_run_id,
            build_step="industry_macro_layer",
            raw_ingest_run_id=raw_ingest_run_id,
            as_of_start_date=write_start_date.isoformat(),
            as_of_end_date=write_end_date.isoformat(),
            metric_count=int(sector_write["sector_name"].nunique() + aggregate_write["industry_aggregate_name"].nunique() + industry_write["industry_name"].nunique()),
            notes=(
                f"cadence={layer_cfg.cadence} source_mode={layer_cfg.source_mode} "
                f"strategy_key={layer_cfg.strategy_key}"
            ),
        )
        run_started = True
        for table_name in ("sector_macro_fit_daily", "industry_aggregate_macro_fit_daily", "industry_macro_fit_daily"):
            clear_industry_macro_range(conn, table_name=table_name, start_date=write_start_date.isoformat(), end_date=write_end_date.isoformat())

        rows_written += insert_many(
            conn,
            """
            INSERT OR REPLACE INTO sector_macro_fit_daily (
                as_of_date, sector_name, active_current_regime, active_next_regime, regime_prior_score, shock_prior_score,
                prior_score, empirical_score, empirical_weight, level_fit_score, final_score, basket_return, universe_return,
                excess_return, member_count, effective_history_weeks, oil_shock_value, commodity_shock_value, dollar_shock_value,
                real_yield_shock_value, credit_shock_value, shock_composite_value, coverage_flag, updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _rows_from_frame(sector_write.assign(as_of_date=sector_write["as_of_date"].dt.strftime("%Y-%m-%d")), [
                "as_of_date", "sector_name", "active_current_regime", "active_next_regime", "regime_prior_score", "shock_prior_score",
                "prior_score", "empirical_score", "empirical_weight", "level_fit_score", "final_score", "basket_return", "universe_return",
                "excess_return", "member_count", "effective_history_weeks", "oil_shock_value", "commodity_shock_value", "dollar_shock_value",
                "real_yield_shock_value", "credit_shock_value", "shock_composite_value", "coverage_flag", "updated_at_utc",
            ]),
            chunk_size=50_000,
        )
        rows_written += insert_many(
            conn,
            """
            INSERT OR REPLACE INTO industry_aggregate_macro_fit_daily (
                as_of_date, sector_name, industry_aggregate_name, active_current_regime, active_next_regime, regime_prior_score,
                shock_prior_score, prior_score, empirical_score, empirical_weight, level_fit_score, final_score, basket_return,
                universe_return, excess_return, member_count, effective_history_weeks, oil_shock_value, commodity_shock_value,
                dollar_shock_value, real_yield_shock_value, credit_shock_value, shock_composite_value, coverage_flag, updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _rows_from_frame(aggregate_write.assign(as_of_date=aggregate_write["as_of_date"].dt.strftime("%Y-%m-%d")), [
                "as_of_date", "sector_name", "industry_aggregate_name", "active_current_regime", "active_next_regime", "regime_prior_score",
                "shock_prior_score", "prior_score", "empirical_score", "empirical_weight", "level_fit_score", "final_score", "basket_return",
                "universe_return", "excess_return", "member_count", "effective_history_weeks", "oil_shock_value", "commodity_shock_value",
                "dollar_shock_value", "real_yield_shock_value", "credit_shock_value", "shock_composite_value", "coverage_flag", "updated_at_utc",
            ]),
            chunk_size=50_000,
        )
        rows_written += insert_many(
            conn,
            """
            INSERT OR REPLACE INTO industry_macro_fit_daily (
                as_of_date, sector_name, industry_aggregate_name, industry_name, active_current_regime, active_next_regime,
                regime_prior_score, shock_prior_score, prior_score, empirical_score, empirical_weight, level_fit_score,
                industry_aggregate_component_score, sector_component_score, final_score, basket_return, universe_return,
                excess_return, member_count, effective_history_weeks, oil_shock_value, commodity_shock_value, dollar_shock_value,
                real_yield_shock_value, credit_shock_value, shock_composite_value, coverage_flag, updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _rows_from_frame(industry_write.assign(as_of_date=industry_write["as_of_date"].dt.strftime("%Y-%m-%d")), [
                "as_of_date", "sector_name", "industry_aggregate_name", "industry_name", "active_current_regime", "active_next_regime",
                "regime_prior_score", "shock_prior_score", "prior_score", "empirical_score", "empirical_weight", "level_fit_score",
                "industry_aggregate_component_score", "sector_component_score", "final_score", "basket_return", "universe_return",
                "excess_return", "member_count", "effective_history_weeks", "oil_shock_value", "commodity_shock_value", "dollar_shock_value",
                "real_yield_shock_value", "credit_shock_value", "shock_composite_value", "coverage_flag", "updated_at_utc",
            ]),
            chunk_size=50_000,
        )
        finish_serving_run(conn, serving_run_id=serving_run_id, status="completed", rows_written=rows_written)
    except BaseException as exc:
        if run_started:
            finish_serving_run(conn, serving_run_id=serving_run_id, status="failed", rows_written=rows_written, notes=str(exc))
        raise
    finally:
        conn.close()

    logger.info(
        "Stage 9 industry macro layer complete: weekly_dates=%d sector_rows=%d aggregate_rows=%d industry_rows=%d output_dir=%s",
        len(history_dates),
        len(sector_write),
        len(aggregate_write),
        len(industry_write),
        layer_cfg.output_dir,
    )


if __name__ == "__main__":
    main()
