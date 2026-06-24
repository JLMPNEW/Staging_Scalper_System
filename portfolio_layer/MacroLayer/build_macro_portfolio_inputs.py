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
    clear_portfolio_input_range,
    finish_serving_run,
    init_db,
    insert_many,
    start_serving_run,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PortfolioInputConfig:
    output_dir: Path
    build_scope: str
    macro_overlay_enabled: bool
    stock_enabled: bool
    stock_require_coverage: bool
    stock_include_base_optimizer_ineligible: bool
    stock_final_score_mode: str
    stock_final_score_center: float
    stock_final_score_scale: float
    stock_final_score_min: float
    stock_final_score_max: float
    stock_state_when_eligible: str
    stock_state_when_ineligible: str
    stock_optimizer_csv_name: str
    foreign_enabled: bool
    foreign_output_root: Path
    foreign_file_glob: str
    foreign_date_source: str
    foreign_stale_after_days: int
    foreign_tactical_score_column: str
    foreign_state_column: str
    foreign_selected_top_m_column: str
    foreign_market_name_column: str
    foreign_eligible_states: set[str]
    foreign_require_selected_top_m: bool
    foreign_min_country_confidence: float
    foreign_min_fused_alpha: float
    foreign_state_when_eligible: str
    foreign_state_when_ineligible: str
    foreign_fused_alpha_weights: dict[str, float]
    foreign_score_pct_method: str
    foreign_optimizer_csv_name: str
    combined_optimizer_csv_name: str
    acceptance: dict[str, Any]


PORTFOLIO_COLUMNS = [
    "as_of_date",
    "ticker",
    "asset_type",
    "sleeve",
    "company",
    "market_name",
    "sector_name",
    "industry_aggregate_name",
    "industry_name",
    "rating",
    "region",
    "country_class",
    "base_final_score",
    "final_score",
    "selection_score",
    "weight_score",
    "score_pct",
    "state",
    "entry_score",
    "expected_return_score",
    "base_optimizer_eligible",
    "earnings_blocked_7d",
    "macro_overlay_enabled",
    "stock_macro_coverage_flag",
    "country_macro_coverage_flag",
    "macro_stock_fit_z",
    "industry_macro_fit",
    "industry_aggregate_macro_fit",
    "sector_macro_fit",
    "sector_tactical_lift_z",
    "shock_fit",
    "tactical_z",
    "country_macro_fit_z",
    "country_confidence",
    "foreign_fused_alpha",
    "agreement_z",
    "optimizer_score_source",
    "source_snapshot",
    "run_id",
    "updated_at_utc",
]


SUMMARY_COLUMNS = [
    "as_of_date",
    "stock_count",
    "stock_eligible_count",
    "foreign_count",
    "foreign_eligible_count",
    "foreign_positive_count",
    "macro_overlay_enabled",
    "avg_stock_selection_score",
    "avg_stock_weight_score",
    "avg_foreign_fused_alpha",
    "max_foreign_fused_alpha",
    "stock_output_csv",
    "foreign_output_csv",
    "combined_output_csv",
    "updated_at_utc",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build Stage 12A optimizer-ready macro portfolio inputs.")
    parser.add_argument("--config", type=Path, default=None, help="Path to macro raw YAML config.")
    parser.add_argument("--serving-db-path", type=Path, default=None, help="Optional serving SQLite path override.")
    parser.add_argument("--start-date", type=str, default=None, help="Optional Stage 12A start YYYY-MM-DD override.")
    parser.add_argument("--end-date", type=str, default=None, help="Optional Stage 12A end YYYY-MM-DD override.")
    return parser.parse_args()


def _normalize_weight_dict(raw: dict[str, Any], keys: tuple[str, ...], *, label: str) -> dict[str, float]:
    weights = {key: float(raw.get(key, 0.0)) for key in keys}
    if any(value < 0.0 for value in weights.values()):
        raise ValueError(f"{label} weights must be non-negative.")
    total = sum(weights.values())
    if total <= 0.0:
        raise ValueError(f"{label} weights must sum to a positive value.")
    return {key: value / total for key, value in weights.items()}


def _resolve_layer_config(cfg: dict[str, Any], config_path: Path) -> PortfolioInputConfig:
    raw_cfg = dict(cfg_get(cfg, "portfolio_input_layer", default={}) or {})
    output_dir = resolve_path(config_path, str(raw_cfg.get("output_dir", "MacroLayer/out/portfolio_inputs")))
    if output_dir is None:
        raise ValueError("portfolio_input_layer.output_dir could not be resolved.")
    stock_cfg = dict(raw_cfg.get("stock", {}) or {})
    foreign_cfg = dict(raw_cfg.get("foreign", {}) or {})
    combined_cfg = dict(raw_cfg.get("combined", {}) or {})
    foreign_root = resolve_path(config_path, str(foreign_cfg.get("source_output_root", "output")))
    if foreign_root is None:
        raise ValueError("portfolio_input_layer.foreign.source_output_root could not be resolved.")
    build_scope = str(raw_cfg.get("build_scope", "all")).strip().lower() or "all"
    if build_scope not in {"all", "latest"}:
        raise ValueError("portfolio_input_layer.build_scope must be one of: all, latest.")
    score_pct_method = str(foreign_cfg.get("score_pct_method", "percentile")).strip().lower() or "percentile"
    if score_pct_method not in {"percentile", "minmax"}:
        raise ValueError("portfolio_input_layer.foreign.score_pct_method must be one of: percentile, minmax.")
    return PortfolioInputConfig(
        output_dir=output_dir,
        build_scope=build_scope,
        macro_overlay_enabled=parse_boolish(raw_cfg.get("macro_overlay_enabled"), default=True),
        stock_enabled=parse_boolish(stock_cfg.get("enabled"), default=True),
        stock_require_coverage=parse_boolish(stock_cfg.get("require_stock_macro_coverage"), default=True),
        stock_include_base_optimizer_ineligible=parse_boolish(
            stock_cfg.get("include_base_optimizer_ineligible"),
            default=True,
        ),
        stock_final_score_mode=str(stock_cfg.get("final_score_mode", "selection_score_rescaled")).strip().lower()
        or "selection_score_rescaled",
        stock_final_score_center=float(stock_cfg.get("final_score_center", 50.0)),
        stock_final_score_scale=float(stock_cfg.get("final_score_scale", 10.0)),
        stock_final_score_min=float(stock_cfg.get("final_score_min", 0.0)),
        stock_final_score_max=float(stock_cfg.get("final_score_max", 100.0)),
        stock_state_when_eligible=str(stock_cfg.get("state_when_eligible", "Eligible")).strip() or "Eligible",
        stock_state_when_ineligible=str(stock_cfg.get("state_when_ineligible", "Ineligible")).strip() or "Ineligible",
        stock_optimizer_csv_name=str(stock_cfg.get("optimizer_csv_name", "tier1_optimizer_universe_macro_latest.csv")).strip()
        or "tier1_optimizer_universe_macro_latest.csv",
        foreign_enabled=parse_boolish(foreign_cfg.get("enabled"), default=True),
        foreign_output_root=foreign_root,
        foreign_file_glob=str(foreign_cfg.get("source_file_glob", "foreign_rotation_latest_*.csv")).strip()
        or "foreign_rotation_latest_*.csv",
        foreign_date_source=str(foreign_cfg.get("date_source", "parent_directory")).strip() or "parent_directory",
        foreign_stale_after_days=max(0, int(foreign_cfg.get("stale_after_days", 14))),
        foreign_tactical_score_column=str(foreign_cfg.get("tactical_score_column", "ScorePct")).strip() or "ScorePct",
        foreign_state_column=str(foreign_cfg.get("state_column", "State")).strip() or "State",
        foreign_selected_top_m_column=str(foreign_cfg.get("selected_top_m_column", "SelectedTopM")).strip() or "SelectedTopM",
        foreign_market_name_column=str(foreign_cfg.get("market_name_column", "MarketName")).strip() or "MarketName",
        foreign_eligible_states={
            str(item).strip()
            for item in list(foreign_cfg.get("eligible_states", ["Eligible"]) or [])
            if str(item).strip()
        },
        foreign_require_selected_top_m=parse_boolish(foreign_cfg.get("require_selected_top_m"), default=False),
        foreign_min_country_confidence=float(foreign_cfg.get("min_country_confidence", 0.35)),
        foreign_min_fused_alpha=float(foreign_cfg.get("min_fused_alpha", 0.0)),
        foreign_state_when_eligible=str(foreign_cfg.get("state_when_eligible", "Eligible")).strip() or "Eligible",
        foreign_state_when_ineligible=str(foreign_cfg.get("state_when_ineligible", "Avoid")).strip() or "Avoid",
        foreign_fused_alpha_weights=_normalize_weight_dict(
            dict(foreign_cfg.get("fused_alpha_weights", {}) or {}),
            ("tactical_z", "confidence_adjusted_country_macro_fit_z", "agreement_z"),
            label="portfolio_input_layer.foreign.fused_alpha_weights",
        ),
        foreign_score_pct_method=score_pct_method,
        foreign_optimizer_csv_name=str(foreign_cfg.get("optimizer_csv_name", "foreign_rotation_macro_latest.csv")).strip()
        or "foreign_rotation_macro_latest.csv",
        combined_optimizer_csv_name=str(combined_cfg.get("optimizer_csv_name", "portfolio_inputs_macro_latest.csv")).strip()
        or "portfolio_inputs_macro_latest.csv",
        acceptance=dict(raw_cfg.get("acceptance", {}) or {}),
    )


def _parse_snapshot_date(path: Path, *, date_source: str) -> pd.Timestamp | None:
    candidates: list[str] = []
    if date_source == "parent_directory":
        candidates.append(path.parent.name)
    candidates.append(path.stem)
    for candidate in candidates:
        matches = re.findall(r"(20\d{6})", str(candidate))
        for match in reversed(matches):
            parsed = pd.to_datetime(match, format="%Y%m%d", errors="coerce")
            if pd.notna(parsed):
                return pd.Timestamp(parsed).normalize()
    return None


def _discover_paths(root: Path, pattern: str) -> list[Path]:
    candidates = {p.resolve() for p in root.glob(pattern)}
    candidates.update({p.resolve() for p in root.glob(f"*/{pattern}")})
    return sorted(candidates)


def _coerce_bool_series(series: pd.Series, *, default: bool) -> pd.Series:
    text = series.astype("string").str.strip().str.lower()
    out = pd.Series(default, index=series.index, dtype=bool)
    out = out.mask(text.isin({"1", "true", "t", "yes", "y"}), True)
    out = out.mask(text.isin({"0", "false", "f", "no", "n"}), False)
    numeric = pd.to_numeric(series, errors="coerce")
    out = out.mask(numeric.eq(1), True)
    out = out.mask(numeric.eq(0), False)
    return out


def _zscore_by_date(frame: pd.DataFrame, column: str, *, fill: float = 0.0) -> pd.Series:
    values = pd.to_numeric(frame[column], errors="coerce")

    def transform(sub: pd.Series) -> pd.Series:
        valid = sub.dropna()
        if len(valid) <= 1:
            return pd.Series(fill, index=sub.index, dtype="float64")
        std = float(valid.std(ddof=1))
        if not np.isfinite(std) or std <= 1e-12:
            return pd.Series(fill, index=sub.index, dtype="float64")
        return ((sub - float(valid.mean())) / std).fillna(fill)

    return values.groupby(frame["as_of_date"], group_keys=False).transform(transform)


def _percentile_by_date(frame: pd.DataFrame, column: str) -> pd.Series:
    values = pd.to_numeric(frame[column], errors="coerce")
    ranks = values.groupby(frame["as_of_date"]).rank(method="first", ascending=True, pct=True)
    return ranks.fillna(0.0).clip(0.0, 1.0)


def _minmax_by_date(frame: pd.DataFrame, column: str) -> pd.Series:
    values = pd.to_numeric(frame[column], errors="coerce")

    def transform(sub: pd.Series) -> pd.Series:
        valid = sub.dropna()
        if valid.empty:
            return pd.Series(0.0, index=sub.index, dtype="float64")
        lo = float(valid.min())
        hi = float(valid.max())
        if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
            return pd.Series(0.5, index=sub.index, dtype="float64")
        return ((sub - lo) / (hi - lo)).fillna(0.0)

    return values.groupby(frame["as_of_date"], group_keys=False).transform(transform).clip(0.0, 1.0)


def _resolve_build_bounds(
    conn: sqlite3.Connection,
    *,
    layer_cfg: PortfolioInputConfig,
    start_override: str | None,
    end_override: str | None,
) -> tuple[date, date]:
    if layer_cfg.stock_enabled:
        source_table = "stock_selection_score_daily"
        source_label = "Stage 11"
        row = conn.execute(
            """
            SELECT MIN(as_of_date) AS min_date, MAX(as_of_date) AS max_date
            FROM stock_selection_score_daily
            WHERE coverage_flag = 1
            """
        ).fetchone()
    elif layer_cfg.foreign_enabled:
        source_table = "country_macro_rank_daily"
        source_label = "Stage 10"
        row = conn.execute(
            """
            SELECT MIN(as_of_date) AS min_date, MAX(as_of_date) AS max_date
            FROM country_macro_rank_daily
            WHERE coverage_flag = 1
            """
        ).fetchone()
    else:
        raise ValueError("Stage 12A has both stock.enabled=false and foreign.enabled=false; nothing to build.")
    min_date = parse_iso_date(row["min_date"]) if row is not None else None
    max_date = parse_iso_date(row["max_date"]) if row is not None else None
    if min_date is None or max_date is None:
        raise ValueError(f"{source_table} has no covered rows. Build {source_label} before Stage 12A.")
    if layer_cfg.build_scope == "latest" and not start_override and not end_override:
        return max_date, max_date
    start_date = parse_iso_date(start_override) or min_date
    end_date = parse_iso_date(end_override) or max_date
    if end_date < start_date:
        raise ValueError(f"Stage 12A end date {end_date.isoformat()} is before start date {start_date.isoformat()}.")
    if start_date < min_date or end_date > max_date:
        raise ValueError(
            f"Stage 12A requested range {start_date.isoformat()}..{end_date.isoformat()} is outside "
            f"available {source_label} range {min_date.isoformat()}..{max_date.isoformat()}."
        )
    return start_date, end_date


def _load_foreign_build_dates(conn: sqlite3.Connection, *, start_date: str, end_date: str) -> pd.DatetimeIndex:
    frame = pd.read_sql_query(
        """
        SELECT DISTINCT as_of_date
        FROM country_macro_rank_daily
        WHERE coverage_flag = 1
          AND as_of_date BETWEEN ? AND ?
        ORDER BY as_of_date
        """,
        conn,
        params=[start_date, end_date],
        parse_dates=["as_of_date"],
    )
    if frame.empty:
        return pd.DatetimeIndex([])
    return pd.DatetimeIndex(frame["as_of_date"].dropna().unique()).sort_values()


def _load_stock_overlay(conn: sqlite3.Connection, *, start_date: str, end_date: str) -> pd.DataFrame:
    frame = pd.read_sql_query(
        """
        SELECT
            f.as_of_date,
            f.ticker,
            f.company,
            f.sector_name,
            f.industry_aggregate_name,
            f.industry_name,
            f.rating,
            f.base_score,
            f.industry_macro_fit,
            f.industry_aggregate_macro_fit,
            f.sector_macro_fit,
            f.sector_tactical_lift_z,
            f.shock_fit,
            f.macro_stock_fit_z,
            f.base_optimizer_eligible,
            f.earnings_blocked_7d,
            f.snapshot_source,
            f.score_approach,
            f.run_id,
            f.coverage_flag AS stock_macro_coverage_flag,
            s.selection_score,
            s.selection_rank,
            s.selection_percentile,
            w.weight_score,
            w.weight_rank,
            w.weight_percentile
        FROM stock_macro_fit_daily f
        JOIN stock_selection_score_daily s
          ON s.as_of_date = f.as_of_date
         AND s.ticker = f.ticker
        JOIN stock_weight_score_daily w
          ON w.as_of_date = f.as_of_date
         AND w.ticker = f.ticker
        WHERE f.as_of_date BETWEEN ? AND ?
        """,
        conn,
        params=[start_date, end_date],
        parse_dates=["as_of_date"],
    )
    if frame.empty:
        raise ValueError(f"No Stage 11 stock overlay rows found for {start_date}..{end_date}.")
    return frame


def _stock_final_score(stock: pd.DataFrame, layer_cfg: PortfolioInputConfig) -> pd.Series:
    base = pd.to_numeric(stock["base_score"], errors="coerce")
    selection = pd.to_numeric(stock["selection_score"], errors="coerce")
    if not layer_cfg.macro_overlay_enabled or layer_cfg.stock_final_score_mode == "base_final_score":
        return base
    if layer_cfg.stock_final_score_mode != "selection_score_rescaled":
        raise ValueError(
            "portfolio_input_layer.stock.final_score_mode must be one of: selection_score_rescaled, base_final_score."
        )
    transformed = layer_cfg.stock_final_score_center + layer_cfg.stock_final_score_scale * selection
    return transformed.clip(layer_cfg.stock_final_score_min, layer_cfg.stock_final_score_max)


def _build_stock_inputs(stock: pd.DataFrame, layer_cfg: PortfolioInputConfig, *, updated_at: str) -> pd.DataFrame:
    out = pd.DataFrame(
        {
            "as_of_date": pd.to_datetime(stock["as_of_date"], errors="coerce").dt.normalize(),
            "ticker": stock["ticker"].astype(str).str.upper().str.strip(),
            "asset_type": "US_STOCK",
            "sleeve": "US",
            "company": stock["company"].fillna("").astype(str).str.strip(),
            "market_name": "",
            "sector_name": stock["sector_name"].fillna("").astype(str).str.strip(),
            "industry_aggregate_name": stock["industry_aggregate_name"].fillna("").astype(str).str.strip(),
            "industry_name": stock["industry_name"].fillna("").astype(str).str.strip(),
            "rating": stock["rating"].fillna("").astype(str).str.strip(),
            "region": "United States",
            "country_class": "",
            "base_final_score": pd.to_numeric(stock["base_score"], errors="coerce"),
            "final_score": _stock_final_score(stock, layer_cfg),
            "selection_score": pd.to_numeric(stock["selection_score"], errors="coerce"),
            "weight_score": pd.to_numeric(stock["weight_score"], errors="coerce"),
            "score_pct": pd.to_numeric(stock["selection_percentile"], errors="coerce").clip(0.0, 1.0),
            "entry_score": pd.to_numeric(stock["selection_score"], errors="coerce"),
            "expected_return_score": pd.to_numeric(stock["weight_score"], errors="coerce"),
            "base_optimizer_eligible": pd.to_numeric(stock["base_optimizer_eligible"], errors="coerce").fillna(0).astype(int),
            "earnings_blocked_7d": pd.to_numeric(stock["earnings_blocked_7d"], errors="coerce").fillna(0).astype(int),
            "macro_overlay_enabled": int(layer_cfg.macro_overlay_enabled),
            "stock_macro_coverage_flag": pd.to_numeric(stock["stock_macro_coverage_flag"], errors="coerce").fillna(0).astype(int),
            "country_macro_coverage_flag": 0,
            "macro_stock_fit_z": pd.to_numeric(stock["macro_stock_fit_z"], errors="coerce"),
            "industry_macro_fit": pd.to_numeric(stock["industry_macro_fit"], errors="coerce"),
            "industry_aggregate_macro_fit": pd.to_numeric(stock["industry_aggregate_macro_fit"], errors="coerce"),
            "sector_macro_fit": pd.to_numeric(stock["sector_macro_fit"], errors="coerce"),
            "sector_tactical_lift_z": pd.to_numeric(stock["sector_tactical_lift_z"], errors="coerce"),
            "shock_fit": pd.to_numeric(stock["shock_fit"], errors="coerce"),
            "tactical_z": np.nan,
            "country_macro_fit_z": np.nan,
            "country_confidence": np.nan,
            "foreign_fused_alpha": np.nan,
            "agreement_z": np.nan,
            "optimizer_score_source": "macro_selection_weight" if layer_cfg.macro_overlay_enabled else "base_final_score",
            "source_snapshot": stock["snapshot_source"].fillna("").astype(str).str.strip(),
            "run_id": stock["run_id"].fillna("").astype(str).str.strip(),
            "updated_at_utc": updated_at,
        }
    )
    eligible = out["base_optimizer_eligible"].eq(1)
    if layer_cfg.stock_require_coverage:
        eligible = eligible & out["stock_macro_coverage_flag"].eq(1)
    out["state"] = np.where(eligible, layer_cfg.stock_state_when_eligible, layer_cfg.stock_state_when_ineligible)
    if not layer_cfg.stock_include_base_optimizer_ineligible:
        out = out.loc[out["base_optimizer_eligible"].eq(1)].copy()
    return out[PORTFOLIO_COLUMNS].dropna(subset=["as_of_date", "ticker", "final_score"]).reset_index(drop=True)


def _load_foreign_tactical(layer_cfg: PortfolioInputConfig) -> pd.DataFrame:
    if not layer_cfg.foreign_enabled:
        return pd.DataFrame()
    paths = _discover_paths(layer_cfg.foreign_output_root, layer_cfg.foreign_file_glob)
    frames: list[pd.DataFrame] = []
    for path in paths:
        snapshot_date = _parse_snapshot_date(path, date_source=layer_cfg.foreign_date_source)
        if snapshot_date is None:
            logger.warning("Skipping foreign tactical file with no parseable date: %s", path)
            continue
        frame = pd.read_csv(path)
        required = {"Ticker", layer_cfg.foreign_tactical_score_column}
        missing = required - set(frame.columns)
        if missing:
            logger.warning("Skipping foreign tactical file %s because required columns are missing: %s", path, sorted(missing))
            continue
        out = pd.DataFrame(
            {
                "snapshot_date": snapshot_date,
                "ticker": frame["Ticker"].astype(str).str.upper().str.strip(),
                "market_name": (
                    frame[layer_cfg.foreign_market_name_column].fillna("").astype(str).str.strip()
                    if layer_cfg.foreign_market_name_column in frame.columns
                    else ""
                ),
                "foreign_tactical_score": pd.to_numeric(frame[layer_cfg.foreign_tactical_score_column], errors="coerce"),
                "source_state": (
                    frame[layer_cfg.foreign_state_column].fillna("").astype(str).str.strip()
                    if layer_cfg.foreign_state_column in frame.columns
                    else ""
                ),
                "source_snapshot": str(path),
            }
        )
        if layer_cfg.foreign_selected_top_m_column in frame.columns:
            out["selected_top_m"] = _coerce_bool_series(frame[layer_cfg.foreign_selected_top_m_column], default=False).astype(int)
        else:
            out["selected_top_m"] = 0
        frames.append(out.dropna(subset=["ticker", "foreign_tactical_score"]))
    if not frames:
        logger.warning("No foreign tactical snapshots were loaded; Stage 12A foreign ETF inputs will be empty.")
        return pd.DataFrame()
    tactical = pd.concat(frames, ignore_index=True)
    return tactical.sort_values(["snapshot_date", "ticker"]).drop_duplicates(
        subset=["snapshot_date", "ticker"],
        keep="last",
    )


def _expand_foreign_tactical(
    tactical: pd.DataFrame,
    dates: pd.DatetimeIndex,
    *,
    layer_cfg: PortfolioInputConfig,
) -> pd.DataFrame:
    if tactical.empty or len(dates) == 0:
        return pd.DataFrame()
    date_frame = pd.DataFrame({"as_of_date": pd.DatetimeIndex(dates).sort_values().unique()})
    tickers = tactical["ticker"].dropna().astype(str).str.upper().unique().tolist()
    expanded = date_frame.assign(_key=1).merge(pd.DataFrame({"ticker": tickers, "_key": 1}), on="_key").drop(columns=["_key"])
    parts: list[pd.DataFrame] = []
    tolerance = pd.Timedelta(days=layer_cfg.foreign_stale_after_days)
    for ticker, ticker_frame in tactical.groupby("ticker"):
        target = expanded.loc[expanded["ticker"].eq(ticker)].sort_values("as_of_date")
        merged = pd.merge_asof(
            target,
            ticker_frame.sort_values("snapshot_date"),
            left_on="as_of_date",
            right_on="snapshot_date",
            direction="backward",
            tolerance=tolerance,
        )
        merged["ticker"] = ticker
        parts.append(merged)
    if not parts:
        return pd.DataFrame()
    out = pd.concat(parts, ignore_index=True)
    return out.dropna(subset=["foreign_tactical_score"]).reset_index(drop=True)


def _load_country_macro(conn: sqlite3.Connection, *, start_date: str, end_date: str) -> pd.DataFrame:
    return pd.read_sql_query(
        """
        SELECT
            r.as_of_date,
            r.ticker,
            r.country_class,
            f.country_name,
            f.region,
            f.market_class,
            r.country_macro_fit,
            r.country_confidence,
            r.confidence_adjusted_fit,
            r.country_rank,
            r.country_percentile,
            r.eligible_flag AS country_eligible_flag,
            r.coverage_flag AS country_macro_coverage_flag
        FROM country_macro_rank_daily r
        LEFT JOIN country_macro_fit_daily f
          ON f.as_of_date = r.as_of_date
         AND f.ticker = r.ticker
        WHERE r.as_of_date BETWEEN ? AND ?
        """,
        conn,
        params=[start_date, end_date],
        parse_dates=["as_of_date"],
    )


def _build_foreign_inputs(
    tactical: pd.DataFrame,
    country: pd.DataFrame,
    *,
    layer_cfg: PortfolioInputConfig,
    updated_at: str,
) -> pd.DataFrame:
    if tactical.empty:
        return pd.DataFrame(columns=PORTFOLIO_COLUMNS)
    out = tactical.merge(country, on=["as_of_date", "ticker"], how="left")
    out["tactical_z"] = _zscore_by_date(out, "foreign_tactical_score")
    out["country_macro_fit_z"] = _zscore_by_date(out, "confidence_adjusted_fit")
    out["country_confidence"] = pd.to_numeric(out["country_confidence"], errors="coerce").fillna(0.0).clip(0.0, 1.0)
    out["agreement_raw"] = out["tactical_z"].fillna(0.0) * out["country_macro_fit_z"].fillna(0.0)
    out["agreement_z"] = _zscore_by_date(out, "agreement_raw")
    weights = layer_cfg.foreign_fused_alpha_weights
    out["foreign_fused_alpha"] = (
        float(weights["tactical_z"]) * out["tactical_z"].fillna(0.0)
        + float(weights["confidence_adjusted_country_macro_fit_z"])
        * out["country_macro_fit_z"].fillna(0.0)
        + float(weights["agreement_z"]) * out["agreement_z"].fillna(0.0)
    )
    if layer_cfg.foreign_score_pct_method == "minmax":
        out["score_pct"] = _minmax_by_date(out, "foreign_fused_alpha")
    else:
        out["score_pct"] = _percentile_by_date(out, "foreign_fused_alpha")
    source_state = out["source_state"].fillna("").astype(str).str.strip()
    eligible = source_state.isin(layer_cfg.foreign_eligible_states)
    if layer_cfg.foreign_require_selected_top_m:
        eligible = eligible & pd.to_numeric(out["selected_top_m"], errors="coerce").fillna(0).astype(int).eq(1)
    eligible = (
        eligible
        & pd.to_numeric(out["country_macro_coverage_flag"], errors="coerce").fillna(0).astype(int).eq(1)
        & pd.to_numeric(out["country_confidence"], errors="coerce").fillna(0.0).ge(layer_cfg.foreign_min_country_confidence)
        & pd.to_numeric(out["foreign_fused_alpha"], errors="coerce").fillna(-np.inf).gt(layer_cfg.foreign_min_fused_alpha)
    )
    final_score = pd.to_numeric(out["foreign_fused_alpha"], errors="coerce")
    result = pd.DataFrame(
        {
            "as_of_date": pd.to_datetime(out["as_of_date"], errors="coerce").dt.normalize(),
            "ticker": out["ticker"].astype(str).str.upper().str.strip(),
            "asset_type": "FOREIGN_ETF",
            "sleeve": "FOREIGN",
            "company": out["market_name"].fillna("").astype(str).str.strip(),
            "market_name": out["market_name"].fillna("").astype(str).str.strip(),
            "sector_name": "FOREIGN",
            "industry_aggregate_name": "FOREIGN_ETF",
            "industry_name": "FOREIGN_ETF",
            "rating": "FOREIGN",
            "region": out["region"].fillna("").astype(str).str.strip() if "region" in out.columns else "",
            "country_class": out["country_class"].fillna("").astype(str).str.strip() if "country_class" in out.columns else "",
            "base_final_score": pd.to_numeric(out["foreign_tactical_score"], errors="coerce"),
            "final_score": final_score,
            "selection_score": final_score,
            "weight_score": final_score,
            "score_pct": pd.to_numeric(out["score_pct"], errors="coerce").clip(0.0, 1.0),
            "state": np.where(eligible, layer_cfg.foreign_state_when_eligible, layer_cfg.foreign_state_when_ineligible),
            "entry_score": final_score,
            "expected_return_score": final_score,
            "base_optimizer_eligible": eligible.astype(int),
            "earnings_blocked_7d": 0,
            "macro_overlay_enabled": int(layer_cfg.macro_overlay_enabled),
            "stock_macro_coverage_flag": 0,
            "country_macro_coverage_flag": pd.to_numeric(out["country_macro_coverage_flag"], errors="coerce").fillna(0).astype(int),
            "macro_stock_fit_z": np.nan,
            "industry_macro_fit": np.nan,
            "industry_aggregate_macro_fit": np.nan,
            "sector_macro_fit": np.nan,
            "sector_tactical_lift_z": np.nan,
            "shock_fit": np.nan,
            "tactical_z": pd.to_numeric(out["tactical_z"], errors="coerce"),
            "country_macro_fit_z": pd.to_numeric(out["country_macro_fit_z"], errors="coerce"),
            "country_confidence": pd.to_numeric(out["country_confidence"], errors="coerce"),
            "foreign_fused_alpha": pd.to_numeric(out["foreign_fused_alpha"], errors="coerce"),
            "agreement_z": pd.to_numeric(out["agreement_z"], errors="coerce"),
            "optimizer_score_source": "foreign_fused_alpha",
            "source_snapshot": out["source_snapshot"].fillna("").astype(str).str.strip(),
            "run_id": "",
            "updated_at_utc": updated_at,
        }
    )
    return result[PORTFOLIO_COLUMNS].dropna(subset=["as_of_date", "ticker", "final_score"]).reset_index(drop=True)


def _build_summary(
    combined: pd.DataFrame,
    *,
    layer_cfg: PortfolioInputConfig,
    stock_csv: Path | None,
    foreign_csv: Path | None,
    combined_csv: Path | None,
    updated_at: str,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for as_of_date, sub in combined.groupby("as_of_date"):
        stock = sub.loc[sub["asset_type"].eq("US_STOCK")]
        foreign = sub.loc[sub["asset_type"].eq("FOREIGN_ETF")]
        stock_selection = pd.to_numeric(stock["selection_score"], errors="coerce")
        stock_weight = pd.to_numeric(stock["weight_score"], errors="coerce")
        foreign_alpha = pd.to_numeric(foreign["foreign_fused_alpha"], errors="coerce")
        rows.append(
            {
                "as_of_date": pd.Timestamp(as_of_date).strftime("%Y-%m-%d"),
                "stock_count": int(len(stock)),
                "stock_eligible_count": int(stock["state"].eq(layer_cfg.stock_state_when_eligible).sum()),
                "foreign_count": int(len(foreign)),
                "foreign_eligible_count": int(foreign["state"].eq(layer_cfg.foreign_state_when_eligible).sum()),
                "foreign_positive_count": int(foreign_alpha.gt(0.0).sum()),
                "macro_overlay_enabled": int(layer_cfg.macro_overlay_enabled),
                "avg_stock_selection_score": float(stock_selection.mean()) if stock_selection.notna().any() else np.nan,
                "avg_stock_weight_score": float(stock_weight.mean()) if stock_weight.notna().any() else np.nan,
                "avg_foreign_fused_alpha": float(foreign_alpha.mean()) if foreign_alpha.notna().any() else np.nan,
                "max_foreign_fused_alpha": float(foreign_alpha.max()) if foreign_alpha.notna().any() else np.nan,
                "stock_output_csv": str(stock_csv) if stock_csv is not None else "",
                "foreign_output_csv": str(foreign_csv) if foreign_csv is not None else "",
                "combined_output_csv": str(combined_csv) if combined_csv is not None else "",
                "updated_at_utc": updated_at,
            }
        )
    return pd.DataFrame(rows, columns=SUMMARY_COLUMNS)


def _write_atomic_csv(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    frame.to_csv(tmp_path, index=False)
    tmp_path.replace(path)


def _load_canonical_universe_for_date(cfg: dict[str, Any], config_path: Path, as_of_key: str) -> pd.DataFrame:
    layer_cfg = dict(cfg_get(cfg, "industry_macro_layer", default={}) or {})
    root = resolve_path(config_path, str(layer_cfg.get("production_output_root", "output")))
    pattern = str(layer_cfg.get("production_file_glob", "tier1_optimizer_universe_*.csv")).strip()
    if root is None:
        return pd.DataFrame()
    frames: list[pd.DataFrame] = []
    for path in _discover_paths(root, pattern):
        try:
            frame = pd.read_csv(path)
        except Exception as exc:
            logger.warning("Failed to read canonical universe file %s: %s", path, exc)
            continue
        if frame.empty or "Ticker" not in frame.columns:
            continue
        if "AsOfDate" in frame.columns:
            dates = pd.to_datetime(frame["AsOfDate"], errors="coerce").dt.strftime("%Y-%m-%d")
            frame = frame.loc[dates.eq(as_of_key)].copy()
        else:
            parsed = _parse_snapshot_date(path, date_source="parent_directory")
            if parsed is None or parsed.strftime("%Y-%m-%d") != as_of_key:
                continue
        if not frame.empty:
            frame["_source_path"] = str(path)
            frames.append(frame)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    out["Ticker"] = out["Ticker"].astype(str).str.upper().str.strip()
    return out.sort_values(["Ticker"]).drop_duplicates(subset=["Ticker"], keep="last").reset_index(drop=True)


def _export_stock_optimizer_csv(
    stock_latest: pd.DataFrame,
    *,
    cfg: dict[str, Any],
    config_path: Path,
    as_of_key: str,
    path: Path,
) -> None:
    canonical = _load_canonical_universe_for_date(cfg, config_path, as_of_key)
    macro = stock_latest.copy()
    macro["Ticker"] = macro["ticker"].astype(str).str.upper().str.strip()
    keep_cols = [
        "Ticker",
        "base_final_score",
        "final_score",
        "selection_score",
        "weight_score",
        "macro_stock_fit_z",
        "industry_macro_fit",
        "industry_aggregate_macro_fit",
        "sector_macro_fit",
        "sector_tactical_lift_z",
        "shock_fit",
        "state",
        "optimizer_score_source",
    ]
    macro = macro[keep_cols].rename(
        columns={
            "base_final_score": "BaseFinalScore",
            "final_score": "MacroFinalScore",
            "selection_score": "SelectionScore",
            "weight_score": "WeightScore",
            "macro_stock_fit_z": "MacroStockFitZ",
            "industry_macro_fit": "IndustryMacroFit",
            "industry_aggregate_macro_fit": "IndustryAggregateMacroFit",
            "sector_macro_fit": "SectorMacroFit",
            "sector_tactical_lift_z": "SectorTacticalLiftZ",
            "shock_fit": "ShockFit",
            "state": "MacroInputState",
            "optimizer_score_source": "OptimizerScoreSource",
        }
    )
    if canonical.empty:
        export = stock_latest.rename(
            columns={
                "as_of_date": "AsOfDate",
                "ticker": "Ticker",
                "company": "Company",
                "sector_name": "sector",
                "industry_aggregate_name": "industry_aggregate",
                "industry_name": "industry",
                "rating": "Rating",
                "final_score": "FinalScore",
            }
        )[
            ["AsOfDate", "Ticker", "Company", "sector", "industry", "industry_aggregate", "Rating", "FinalScore"]
        ].copy()
    else:
        export = canonical.merge(macro, on="Ticker", how="inner")
        if "FinalScore" in export.columns:
            export["BaseFinalScore"] = pd.to_numeric(export["FinalScore"], errors="coerce")
        export["FinalScore"] = pd.to_numeric(export["MacroFinalScore"], errors="coerce")
    export = export.drop(columns=["_source_path"], errors="ignore")
    export = export.sort_values("Ticker").reset_index(drop=True)
    _write_atomic_csv(path, export)


def _export_foreign_optimizer_csv(foreign_latest: pd.DataFrame, *, path: Path) -> None:
    if foreign_latest.empty:
        _write_atomic_csv(
            path,
            pd.DataFrame(columns=["Ticker", "MarketName", "Score", "ScorePct", "Rank", "State"]),
        )
        return
    out = foreign_latest.copy()
    out["Rank"] = pd.to_numeric(out["foreign_fused_alpha"], errors="coerce").rank(method="first", ascending=False).astype("Int64")
    export = pd.DataFrame(
        {
            "Ticker": out["ticker"].astype(str).str.upper().str.strip(),
            "MarketName": out["market_name"].fillna("").astype(str).str.strip(),
            "Score": pd.to_numeric(out["foreign_fused_alpha"], errors="coerce"),
            "ScorePct": pd.to_numeric(out["score_pct"], errors="coerce").clip(0.0, 1.0),
            "Rank": out["Rank"],
            "State": out["state"].fillna("").astype(str).str.strip(),
            "CountryConfidence": pd.to_numeric(out["country_confidence"], errors="coerce"),
            "CountryMacroFitZ": pd.to_numeric(out["country_macro_fit_z"], errors="coerce"),
            "TacticalZ": pd.to_numeric(out["tactical_z"], errors="coerce"),
            "AgreementZ": pd.to_numeric(out["agreement_z"], errors="coerce"),
            "ForeignFusedAlpha": pd.to_numeric(out["foreign_fused_alpha"], errors="coerce"),
            "CountryMacroCoverageFlag": pd.to_numeric(out["country_macro_coverage_flag"], errors="coerce").fillna(0).astype(int),
        }
    )
    _write_atomic_csv(path, export.sort_values("Rank").reset_index(drop=True))


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


def _latest_dependency_run_raw_ingest_id(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        """
        SELECT raw_ingest_run_id
        FROM macro_serving_run
        WHERE build_step IN ('stock_macro_overlay', 'country_macro_layer')
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
        frames: list[pd.DataFrame] = []

        if layer_cfg.stock_enabled:
            stock_overlay = _load_stock_overlay(
                conn,
                start_date=start_date.isoformat(),
                end_date=end_date.isoformat(),
            )
            stock_inputs = _build_stock_inputs(stock_overlay, layer_cfg, updated_at=updated_at)
            frames.append(stock_inputs)
        else:
            stock_inputs = pd.DataFrame(columns=PORTFOLIO_COLUMNS)

        if layer_cfg.foreign_enabled:
            tactical = _load_foreign_tactical(layer_cfg)
            if layer_cfg.stock_enabled:
                build_dates = pd.DatetimeIndex(stock_inputs["as_of_date"].dropna().unique()).sort_values()
            else:
                build_dates = _load_foreign_build_dates(
                    conn,
                    start_date=start_date.isoformat(),
                    end_date=end_date.isoformat(),
                )
            tactical_expanded = _expand_foreign_tactical(tactical, build_dates, layer_cfg=layer_cfg)
            country = _load_country_macro(
                conn,
                start_date=start_date.isoformat(),
                end_date=end_date.isoformat(),
            )
            foreign_inputs = _build_foreign_inputs(
                tactical_expanded,
                country,
                layer_cfg=layer_cfg,
                updated_at=updated_at,
            )
            if not foreign_inputs.empty:
                frames.append(foreign_inputs)
        else:
            foreign_inputs = pd.DataFrame(columns=PORTFOLIO_COLUMNS)

        if not frames:
            raise ValueError("Stage 12A produced no portfolio input rows.")
        combined = pd.concat(frames, ignore_index=True, sort=False)
        combined = combined.drop_duplicates(subset=["as_of_date", "ticker", "asset_type"], keep="last")
        combined = combined.sort_values(["as_of_date", "asset_type", "ticker"]).reset_index(drop=True)
        latest_date = pd.to_datetime(combined["as_of_date"], errors="coerce").max()
        latest_key = latest_date.strftime("%Y-%m-%d")
        stock_csv = layer_cfg.output_dir / layer_cfg.stock_optimizer_csv_name
        foreign_csv = layer_cfg.output_dir / layer_cfg.foreign_optimizer_csv_name
        combined_csv = layer_cfg.output_dir / layer_cfg.combined_optimizer_csv_name
        summary = _build_summary(
            combined,
            layer_cfg=layer_cfg,
            stock_csv=stock_csv,
            foreign_csv=foreign_csv,
            combined_csv=combined_csv,
            updated_at=updated_at,
        )

        start_serving_run(
            conn,
            serving_run_id=serving_run_id,
            build_step="portfolio_input_layer",
            raw_ingest_run_id=_latest_dependency_run_raw_ingest_id(conn),
            as_of_start_date=start_date.isoformat(),
            as_of_end_date=end_date.isoformat(),
            metric_count=int(combined["ticker"].nunique()),
            notes=(
                f"build_scope={layer_cfg.build_scope} macro_overlay_enabled={layer_cfg.macro_overlay_enabled} "
                f"foreign_enabled={layer_cfg.foreign_enabled}"
            ),
        )
        run_started = True
        for table_name in ("portfolio_inputs_daily", "portfolio_allocation_summary"):
            clear_portfolio_input_range(
                conn,
                table_name=table_name,
                start_date=start_date.isoformat(),
                end_date=end_date.isoformat(),
            )
        rows_written += insert_many(
            conn,
            """
            INSERT OR REPLACE INTO portfolio_inputs_daily (
                as_of_date, ticker, asset_type, sleeve, company, market_name, sector_name,
                industry_aggregate_name, industry_name, rating, region, country_class,
                base_final_score, final_score, selection_score, weight_score, score_pct, state,
                entry_score, expected_return_score, base_optimizer_eligible, earnings_blocked_7d,
                macro_overlay_enabled, stock_macro_coverage_flag, country_macro_coverage_flag,
                macro_stock_fit_z, industry_macro_fit, industry_aggregate_macro_fit, sector_macro_fit,
                sector_tactical_lift_z, shock_fit, tactical_z, country_macro_fit_z,
                country_confidence, foreign_fused_alpha, agreement_z, optimizer_score_source,
                source_snapshot, run_id, updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _frame_rows(combined, PORTFOLIO_COLUMNS),
            chunk_size=50_000,
        )
        rows_written += insert_many(
            conn,
            """
            INSERT OR REPLACE INTO portfolio_allocation_summary (
                as_of_date, stock_count, stock_eligible_count, foreign_count, foreign_eligible_count,
                foreign_positive_count, macro_overlay_enabled, avg_stock_selection_score,
                avg_stock_weight_score, avg_foreign_fused_alpha, max_foreign_fused_alpha,
                stock_output_csv, foreign_output_csv, combined_output_csv, updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _frame_rows(summary, SUMMARY_COLUMNS),
            chunk_size=50_000,
        )

        latest = combined.loc[pd.to_datetime(combined["as_of_date"]).dt.strftime("%Y-%m-%d").eq(latest_key)].copy()
        stock_latest = latest.loc[latest["asset_type"].eq("US_STOCK")].copy()
        foreign_latest = latest.loc[latest["asset_type"].eq("FOREIGN_ETF")].copy()
        _export_stock_optimizer_csv(stock_latest, cfg=cfg, config_path=config_path, as_of_key=latest_key, path=stock_csv)
        _export_foreign_optimizer_csv(foreign_latest, path=foreign_csv)
        _write_atomic_csv(combined_csv, latest.sort_values(["asset_type", "final_score"], ascending=[True, False]).reset_index(drop=True))
        _write_atomic_csv(layer_cfg.output_dir / "portfolio_allocation_summary_latest.csv", summary.tail(1).reset_index(drop=True))

        finish_serving_run(conn, serving_run_id=serving_run_id, status="completed", rows_written=rows_written)
        logger.info(
            "Stage 12A portfolio inputs complete: rows_written=%d range=%s..%s output_dir=%s",
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
                logger.exception("Failed to mark Stage 12A serving run as failed.")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
