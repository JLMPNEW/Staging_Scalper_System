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

from build_macro_industry_fit import _load_weekly_score_panel, _resolve_layer_config as _resolve_industry_layer_config  # noqa: E402
from macro_raw_config import (  # noqa: E402
    cfg_get,
    configure_pipeline_logging,
    connect_sqlite,
    load_macro_raw_config,
    parse_boolish,
    parse_iso_date,
    resolve_path,
    utc_now_iso,
)
from macro_serving_common import resolve_serving_db_path  # noqa: E402
from macro_serving_storage import (  # noqa: E402
    clear_stock_macro_range,
    finish_serving_run,
    init_db,
    insert_many,
    start_serving_run,
)
from staging_portfolio_adapter import (  # noqa: E402
    MAX_STAGE2_PRICE_STALE_DAYS,
    load_staging_prices,
    staleness_gated_weekly,
)

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class StockOverlayConfig:
    output_dir: Path
    cadence: str
    include_base_optimizer_ineligible: bool
    base_score_column: str
    rating_column: str
    company_column: str
    base_optimizer_eligible_column: str
    earnings_blocked_column: str
    macro_fit_weights: dict[str, float]
    shock_fit_weights: dict[str, float]
    shock_clip_min: float
    shock_clip_max: float
    selection_score_weights: dict[str, float]
    weight_score_weights: dict[str, float]
    validation_enabled: bool
    validation_signal_column: str
    validation_lookback_periods: int
    validation_min_periods: int
    validation_min_observations: int
    validation_min_spearman: float
    validation_fallback_multiplier: float
    validation_apply_to_selection: bool
    validation_apply_to_weight: bool
    zscore_min_std: float
    zscore_component_clip: float
    final_score_clip: float
    sector_tactical_enabled: bool
    sector_tactical_output_root: Path
    sector_tactical_file_glob: str
    sector_tactical_date_source: str
    sector_tactical_sector_name_column: str
    sector_tactical_score_column: str
    sector_tactical_state_column: str
    sector_tactical_stale_after_days: int
    sector_tactical_missing_policy: str
    sector_tactical_neutral_value: float
    stock_sector_to_rotation_sector: dict[str, str]
    macro_favored_z_threshold: float
    macro_adverse_z_threshold: float
    acceptance: dict[str, float]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build Stage 11 stock-level macro overlay scores.")
    parser.add_argument("--config", type=Path, default=None, help="Path to macro raw YAML config.")
    parser.add_argument("--serving-db-path", type=Path, default=None, help="Optional serving SQLite path override.")
    parser.add_argument("--start-date", type=str, default=None, help="Optional Stage 11 start YYYY-MM-DD override.")
    parser.add_argument("--end-date", type=str, default=None, help="Optional Stage 11 end YYYY-MM-DD override.")
    return parser.parse_args()


def _normalize_weight_dict(raw: dict[str, Any], keys: tuple[str, ...], *, label: str) -> dict[str, float]:
    weights = {key: float(raw.get(key, 0.0)) for key in keys}
    if any(value < 0.0 for value in weights.values()):
        raise ValueError(f"{label} weights must be non-negative.")
    total = sum(weights.values())
    if total <= 0.0:
        raise ValueError(f"{label} weights must sum to a positive value.")
    return {key: value / total for key, value in weights.items()}


def _resolve_layer_config(cfg: dict[str, Any], config_path: Path) -> StockOverlayConfig:
    raw_cfg = dict(cfg_get(cfg, "stock_macro_overlay", default={}) or {})
    output_dir = resolve_path(config_path, str(raw_cfg.get("output_dir", "MacroLayer/out/stock_macro_overlay")))
    if output_dir is None:
        raise ValueError("stock_macro_overlay.output_dir could not be resolved.")
    score_source = dict(raw_cfg.get("score_source", {}) or {})
    tactical = dict(raw_cfg.get("sector_tactical", {}) or {})
    tactical_missing_policy = str(tactical.get("missing_policy", "neutral")).strip().lower() or "neutral"
    if tactical_missing_policy not in {"neutral", "strict"}:
        raise ValueError(
            "stock_macro_overlay.sector_tactical.missing_policy must be one of: neutral, strict."
        )
    validation_cfg = dict(raw_cfg.get("selection_validation", {}) or {})
    validation_signal_column = str(validation_cfg.get("signal_column", "macro_stock_fit_z")).strip() or "macro_stock_fit_z"
    if validation_signal_column not in {"macro_stock_fit_z", "macro_stock_fit_raw", "industry_macro_fit", "sector_macro_fit"}:
        raise ValueError(
            "stock_macro_overlay.selection_validation.signal_column must be one of: "
            "macro_stock_fit_z, macro_stock_fit_raw, industry_macro_fit, sector_macro_fit."
        )
    tactical_root = resolve_path(config_path, str(tactical.get("output_root", "output")))
    if tactical_root is None:
        raise ValueError("stock_macro_overlay.sector_tactical.output_root could not be resolved.")
    return StockOverlayConfig(
        output_dir=output_dir,
        cadence=str(raw_cfg.get("cadence", "W-FRI")).strip() or "W-FRI",
        include_base_optimizer_ineligible=parse_boolish(
            score_source.get("include_base_optimizer_ineligible"),
            default=True,
        ),
        base_score_column=str(score_source.get("base_score_column", "Score")).strip() or "Score",
        rating_column=str(score_source.get("rating_column", "Rating")).strip() or "Rating",
        company_column=str(score_source.get("company_column", "Company")).strip() or "Company",
        base_optimizer_eligible_column=str(score_source.get("base_optimizer_eligible_column", "BaseOptimizerEligible")).strip()
        or "BaseOptimizerEligible",
        earnings_blocked_column=str(score_source.get("earnings_blocked_column", "EarningsBlocked_7D")).strip()
        or "EarningsBlocked_7D",
        macro_fit_weights=_normalize_weight_dict(
            dict(raw_cfg.get("macro_fit_weights", {}) or {}),
            ("industry_macro_fit", "sector_macro_fit", "shock_fit"),
            label="stock_macro_overlay.macro_fit_weights",
        ),
        shock_fit_weights=_normalize_weight_dict(
            dict(raw_cfg.get("shock_fit", {}) or {}),
            ("industry_shock_prior_weight", "sector_shock_prior_weight"),
            label="stock_macro_overlay.shock_fit",
        ),
        shock_clip_min=float(dict(raw_cfg.get("shock_fit", {}) or {}).get("clip_min", -3.0)),
        shock_clip_max=float(dict(raw_cfg.get("shock_fit", {}) or {}).get("clip_max", 3.0)),
        selection_score_weights=_normalize_weight_dict(
            dict(raw_cfg.get("selection_score_weights", {}) or {}),
            ("base_stock_z", "macro_stock_fit_z", "sector_tactical_lift_z"),
            label="stock_macro_overlay.selection_score_weights",
        ),
        weight_score_weights=_normalize_weight_dict(
            dict(raw_cfg.get("weight_score_weights", {}) or {}),
            ("base_stock_z", "macro_stock_fit_z", "sector_tactical_lift_z"),
            label="stock_macro_overlay.weight_score_weights",
        ),
        validation_enabled=parse_boolish(validation_cfg.get("enabled"), default=False),
        validation_signal_column=validation_signal_column,
        validation_lookback_periods=max(1, int(validation_cfg.get("lookback_periods", 52))),
        validation_min_periods=max(1, int(validation_cfg.get("min_periods", 26))),
        validation_min_observations=max(1, int(validation_cfg.get("min_observations", 500))),
        validation_min_spearman=float(validation_cfg.get("min_spearman", 0.0)),
        validation_fallback_multiplier=min(1.0, max(0.0, float(validation_cfg.get("fallback_multiplier", 0.0)))),
        validation_apply_to_selection=parse_boolish(validation_cfg.get("apply_to_selection"), default=True),
        validation_apply_to_weight=parse_boolish(validation_cfg.get("apply_to_weight"), default=False),
        zscore_min_std=max(0.0, float(dict(raw_cfg.get("zscore", {}) or {}).get("min_std", 1e-6))),
        zscore_component_clip=max(0.0, float(dict(raw_cfg.get("zscore", {}) or {}).get("component_clip", 3.0))),
        final_score_clip=max(0.0, float(dict(raw_cfg.get("zscore", {}) or {}).get("final_score_clip", 5.0))),
        sector_tactical_enabled=parse_boolish(tactical.get("enabled"), default=True),
        sector_tactical_output_root=tactical_root,
        sector_tactical_file_glob=str(tactical.get("file_glob", "sector_rotation_latest_*.csv")).strip()
        or "sector_rotation_latest_*.csv",
        sector_tactical_date_source=str(tactical.get("date_source", "parent_directory")).strip() or "parent_directory",
        sector_tactical_sector_name_column=str(tactical.get("sector_name_column", "SectorName")).strip() or "SectorName",
        sector_tactical_score_column=str(tactical.get("score_column", "ScorePct")).strip() or "ScorePct",
        sector_tactical_state_column=str(tactical.get("state_column", "State")).strip() or "State",
        sector_tactical_stale_after_days=max(0, int(tactical.get("stale_after_days", 14))),
        sector_tactical_missing_policy=tactical_missing_policy,
        sector_tactical_neutral_value=float(tactical.get("neutral_value", 0.0)),
        stock_sector_to_rotation_sector={
            str(k).strip(): str(v).strip()
            for k, v in dict(tactical.get("stock_sector_to_rotation_sector", {}) or {}).items()
            if str(k).strip() and str(v).strip()
        },
        macro_favored_z_threshold=float(dict(raw_cfg.get("flags", {}) or {}).get("macro_favored_z_threshold", 0.50)),
        macro_adverse_z_threshold=float(dict(raw_cfg.get("flags", {}) or {}).get("macro_adverse_z_threshold", -0.50)),
        acceptance={str(k): float(v) for k, v in dict(raw_cfg.get("acceptance", {}) or {}).items()},
    )


def _coerce_bool_series(series: pd.Series, *, default: bool) -> pd.Series:
    if series is None:
        return pd.Series(dtype=bool)
    text = series.astype("string").str.strip().str.lower()
    true_values = {"1", "true", "t", "yes", "y"}
    false_values = {"0", "false", "f", "no", "n"}
    out = pd.Series(default, index=series.index, dtype=bool)
    out = out.mask(text.isin(true_values), True)
    out = out.mask(text.isin(false_values), False)
    numeric = pd.to_numeric(series, errors="coerce")
    out = out.mask(numeric.eq(1), True)
    out = out.mask(numeric.eq(0), False)
    return out.fillna(default).astype(bool)


def _zscore_by_date(frame: pd.DataFrame, value_col: str, *, min_std: float, clip_value: float) -> pd.Series:
    values = pd.to_numeric(frame[value_col], errors="coerce")

    def transform(group: pd.Series) -> pd.Series:
        finite = group.replace([np.inf, -np.inf], np.nan)
        mean_value = float(finite.mean()) if finite.notna().any() else 0.0
        std_value = float(finite.std(ddof=1)) if finite.notna().sum() > 1 else 0.0
        if not np.isfinite(std_value) or std_value <= min_std:
            return pd.Series(0.0, index=group.index, dtype="float64")
        return ((finite - mean_value) / std_value).fillna(0.0)

    out = values.groupby(frame["as_of_date"], group_keys=False).apply(transform)
    if clip_value > 0.0:
        out = out.clip(-clip_value, clip_value)
    return out.astype("float64")


def _rank_by_date(frame: pd.DataFrame, value_col: str) -> tuple[pd.Series, pd.Series]:
    values = pd.to_numeric(frame[value_col], errors="coerce")
    ranks = values.groupby(frame["as_of_date"], group_keys=False).rank(method="first", ascending=False, na_option="bottom")
    counts = frame.groupby("as_of_date")["ticker"].transform("count").astype(float)
    percentiles = np.where(counts > 1.0, 1.0 - (ranks.astype(float) - 1.0) / (counts - 1.0), 1.0)
    return ranks.astype("Int64"), pd.Series(percentiles, index=frame.index, dtype="float64")


def _parse_snapshot_date(path: Path, *, date_source: str) -> pd.Timestamp | None:
    candidates: list[str] = []
    if date_source == "parent_directory":
        candidates.append(path.parent.name)
    elif date_source == "path":
        candidates.extend(reversed(path.parts))
    candidates.append(path.stem)
    for candidate in candidates:
        matches = re.findall(r"(20\d{2}[-_]?\d{2}[-_]?\d{2})", str(candidate))
        for match in reversed(matches):
            fmt = "%Y-%m-%d" if "-" in match else "%Y_%m_%d" if "_" in match else "%Y%m%d"
            parsed = pd.to_datetime(match, format=fmt, errors="coerce")
            if pd.notna(parsed):
                return pd.Timestamp(parsed).normalize()
    return None


def _discover_sector_rotation_paths(layer_cfg: StockOverlayConfig) -> list[Path]:
    root = layer_cfg.sector_tactical_output_root
    pattern = layer_cfg.sector_tactical_file_glob
    paths = {p.resolve() for p in root.glob(pattern)}
    paths.update({p.resolve() for p in root.glob(f"*/{pattern}")})
    paths.update({p.resolve() for p in root.glob(f"*/*/{pattern}")})
    return sorted(paths)


def _neutral_sector_tactical_frame(layer_cfg: StockOverlayConfig, dates: pd.DatetimeIndex) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "as_of_date": dates,
            "rotation_sector_name": "",
            "sector_tactical_lift": layer_cfg.sector_tactical_neutral_value,
            "sector_tactical_lift_z": 0.0,
        }
    )


def _load_sector_tactical_frame(layer_cfg: StockOverlayConfig, dates: pd.DatetimeIndex) -> pd.DataFrame:
    if not layer_cfg.sector_tactical_enabled:
        return _neutral_sector_tactical_frame(layer_cfg, dates)
    paths = _discover_sector_rotation_paths(layer_cfg)
    frames: list[pd.DataFrame] = []
    for path in paths:
        snapshot_date = _parse_snapshot_date(path, date_source=layer_cfg.sector_tactical_date_source)
        if snapshot_date is None:
            logger.warning("Skipping sector tactical file with no parseable date: %s", path)
            continue
        frame = pd.read_csv(path)
        required = {layer_cfg.sector_tactical_sector_name_column, layer_cfg.sector_tactical_score_column}
        missing = required - set(frame.columns)
        if missing:
            logger.warning("Skipping sector tactical file %s because required columns are missing: %s", path, sorted(missing))
            continue
        out = pd.DataFrame(
            {
                "snapshot_date": snapshot_date,
                "rotation_sector_name": frame[layer_cfg.sector_tactical_sector_name_column].astype(str).str.strip(),
                "sector_tactical_lift": pd.to_numeric(frame[layer_cfg.sector_tactical_score_column], errors="coerce"),
            }
        )
        if layer_cfg.sector_tactical_state_column in frame.columns:
            out["sector_tactical_state"] = frame[layer_cfg.sector_tactical_state_column].astype(str).str.strip()
        else:
            out["sector_tactical_state"] = ""
        frames.append(out.dropna(subset=["sector_tactical_lift"]))
    if not frames:
        if layer_cfg.sector_tactical_missing_policy == "strict":
            raise ValueError("No sector tactical snapshots were loaded and missing_policy=strict.")
        logger.warning("No sector tactical snapshots were loaded; Stage 11 will use neutral tactical lift.")
        return _neutral_sector_tactical_frame(layer_cfg, dates)
    tactical = pd.concat(frames, ignore_index=True)
    tactical = tactical.sort_values(["snapshot_date", "rotation_sector_name"]).drop_duplicates(
        subset=["snapshot_date", "rotation_sector_name"],
        keep="last",
    )
    tactical["sector_tactical_lift_z"] = _zscore_by_date(
        tactical.rename(columns={"snapshot_date": "as_of_date"}),
        "sector_tactical_lift",
        min_std=layer_cfg.zscore_min_std,
        clip_value=layer_cfg.zscore_component_clip,
    ).to_numpy()
    date_frame = pd.DataFrame({"as_of_date": dates})
    sectors = tactical["rotation_sector_name"].dropna().astype(str).unique().tolist()
    expanded = date_frame.assign(_key=1).merge(pd.DataFrame({"rotation_sector_name": sectors, "_key": 1}), on="_key").drop(columns=["_key"])
    merged_parts: list[pd.DataFrame] = []
    tolerance = pd.Timedelta(days=layer_cfg.sector_tactical_stale_after_days)
    for sector_name, sector_frame in tactical.groupby("rotation_sector_name"):
        target = expanded.loc[expanded["rotation_sector_name"].eq(sector_name)].sort_values("as_of_date")
        merged = pd.merge_asof(
            target,
            sector_frame.sort_values("snapshot_date"),
            left_on="as_of_date",
            right_on="snapshot_date",
            direction="backward",
            tolerance=tolerance,
        )
        merged["rotation_sector_name"] = sector_name
        merged_parts.append(merged)
    if not merged_parts:
        if layer_cfg.sector_tactical_missing_policy == "strict":
            raise ValueError("No sector tactical rows matched the requested dates and missing_policy=strict.")
        return _neutral_sector_tactical_frame(layer_cfg, dates)
    out = pd.concat(merged_parts, ignore_index=True)
    out["sector_tactical_lift"] = pd.to_numeric(out["sector_tactical_lift"], errors="coerce")
    out["sector_tactical_lift_z"] = pd.to_numeric(out["sector_tactical_lift_z"], errors="coerce")
    if layer_cfg.sector_tactical_missing_policy == "neutral":
        out["sector_tactical_lift"] = out["sector_tactical_lift"].fillna(layer_cfg.sector_tactical_neutral_value)
        out["sector_tactical_lift_z"] = out["sector_tactical_lift_z"].fillna(0.0)
    else:
        missing_count = int(out[["sector_tactical_lift", "sector_tactical_lift_z"]].isna().any(axis=1).sum())
        if missing_count:
            raise ValueError(
                f"Sector tactical data has {missing_count} missing stale/unmatched rows and missing_policy=strict."
            )
    return out[["as_of_date", "rotation_sector_name", "sector_tactical_lift", "sector_tactical_lift_z"]]


def _resolve_build_bounds(
    conn: sqlite3.Connection,
    *,
    score_dates: pd.DatetimeIndex,
    start_override: str | None,
    end_override: str | None,
) -> tuple[date, date]:
    valid_score_dates = pd.DatetimeIndex(pd.to_datetime(pd.Index(score_dates), errors="coerce"))
    valid_score_dates = valid_score_dates[valid_score_dates.notna()]
    if len(valid_score_dates) == 0:
        raise ValueError("Stage 11 score panel has no valid dates after source loading and cadence filtering.")
    score_min = valid_score_dates.min().date()
    score_max = valid_score_dates.max().date()
    row = conn.execute(
        """
        SELECT MIN(as_of_date) AS min_date, MAX(as_of_date) AS max_date
        FROM industry_macro_fit_daily
        WHERE coverage_flag = 1
        """
    ).fetchone()
    macro_min = parse_iso_date(row["min_date"]) if row is not None else None
    macro_max = parse_iso_date(row["max_date"]) if row is not None else None
    if macro_min is None or macro_max is None:
        raise ValueError("industry_macro_fit_daily has no covered rows. Build Stage 9 before Stage 11.")
    start_date = parse_iso_date(start_override) or max(score_min, macro_min)
    end_date = parse_iso_date(end_override) or min(score_max, macro_max)
    if end_date < start_date:
        raise ValueError(f"Stage 11 end date {end_date.isoformat()} is before start date {start_date.isoformat()}.")
    if start_date < max(score_min, macro_min) or end_date > min(score_max, macro_max):
        raise ValueError(
            f"Stage 11 requested range {start_date.isoformat()}..{end_date.isoformat()} is outside "
            f"available overlap {max(score_min, macro_min).isoformat()}..{min(score_max, macro_max).isoformat()}."
        )
    return start_date, end_date


def _load_stage9_macro_frames(conn: sqlite3.Connection, *, start_date: str, end_date: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    industry = pd.read_sql_query(
        """
        SELECT
            as_of_date,
            sector_name,
            industry_aggregate_name,
            industry_name,
            final_score AS industry_macro_fit,
            shock_prior_score AS industry_shock_prior_score,
            coverage_flag AS industry_macro_coverage_flag
        FROM industry_macro_fit_daily
        WHERE as_of_date BETWEEN ? AND ?
        """,
        conn,
        params=[start_date, end_date],
        parse_dates=["as_of_date"],
    )
    aggregate = pd.read_sql_query(
        """
        SELECT
            as_of_date,
            sector_name,
            industry_aggregate_name,
            final_score AS industry_aggregate_macro_fit,
            coverage_flag AS aggregate_macro_coverage_flag
        FROM industry_aggregate_macro_fit_daily
        WHERE as_of_date BETWEEN ? AND ?
        """,
        conn,
        params=[start_date, end_date],
        parse_dates=["as_of_date"],
    )
    sector = pd.read_sql_query(
        """
        SELECT
            as_of_date,
            sector_name,
            final_score AS sector_macro_fit,
            shock_prior_score AS sector_shock_prior_score,
            coverage_flag AS sector_macro_coverage_flag
        FROM sector_macro_fit_daily
        WHERE as_of_date BETWEEN ? AND ?
        """,
        conn,
        params=[start_date, end_date],
        parse_dates=["as_of_date"],
    )
    for frame in (industry, aggregate, sector):
        if frame.empty:
            raise ValueError("Stage 11 requires populated Stage 9 macro fit tables for the requested range.")
        for col in frame.columns:
            if col.endswith("_name"):
                frame[col] = frame[col].astype(str).str.strip()
    return industry, aggregate, sector


def _load_validation_forward_returns(
    score_panel: pd.DataFrame,
    *,
    weekly_dates: pd.DatetimeIndex,
    backtest_cfg: dict[str, Any],
    repo_root: Path,
    layer_cfg: StockOverlayConfig,
) -> pd.DataFrame:
    if not layer_cfg.validation_enabled:
        return pd.DataFrame(columns=["as_of_date", "ticker", "validation_forward_return"])
    tickers = sorted(item for item in score_panel["ticker"].dropna().astype(str).str.upper().unique().tolist() if item)
    dates = pd.DatetimeIndex(pd.to_datetime(weekly_dates, errors="coerce")).dropna().sort_values().unique()
    if not tickers or len(dates) < 2:
        return pd.DataFrame(columns=["as_of_date", "ticker", "validation_forward_return"])
    del backtest_cfg, repo_root
    min_dt = pd.Timestamp(dates.min()).normalize() - pd.Timedelta(days=10)
    max_dt = pd.Timestamp(dates.max()).normalize() + pd.Timedelta(days=10)
    prices = load_staging_prices(
        tickers=tickers,
        start_date=min_dt,
        end_date=max_dt,
        freshness_as_of=pd.Timestamp(dates.max()).normalize(),
    )
    weekly_prices = staleness_gated_weekly(prices, dates, max_stale_days=MAX_STAGE2_PRICE_STALE_DAYS)
    forward = weekly_prices.shift(-1) / weekly_prices - 1.0
    out = (
        forward.rename_axis(index="as_of_date", columns="ticker")
        .stack()
        .rename("validation_forward_return")
        .reset_index()
    )
    out["as_of_date"] = pd.to_datetime(out["as_of_date"], errors="coerce").dt.normalize()
    out["ticker"] = out["ticker"].astype(str).str.upper().str.strip()
    out["validation_forward_return"] = pd.to_numeric(out["validation_forward_return"], errors="coerce")
    return out.dropna(subset=["as_of_date", "ticker", "validation_forward_return"]).reset_index(drop=True)


def _macro_validation_diagnostics(out: pd.DataFrame, layer_cfg: StockOverlayConfig) -> pd.DataFrame:
    columns = [
        "as_of_date",
        "validation_spearman",
        "validation_observation_count",
        "validation_period_count",
        "macro_validation_multiplier",
        "validation_ready_flag",
    ]
    if not layer_cfg.validation_enabled:
        dates = pd.DatetimeIndex(pd.to_datetime(out["as_of_date"], errors="coerce").dropna().unique()).sort_values()
        return pd.DataFrame(
            {
                "as_of_date": dates,
                "validation_spearman": np.nan,
                "validation_observation_count": 0,
                "validation_period_count": 0,
                "macro_validation_multiplier": 1.0,
                "validation_ready_flag": 0,
            },
            columns=columns,
        )

    signal_col = layer_cfg.validation_signal_column
    if signal_col not in out.columns:
        raise ValueError(f"Stage 11 validation signal column not found: {signal_col}")
    if "validation_forward_return" not in out.columns:
        raise ValueError("Stage 11 validation requires validation_forward_return.")

    work = out[["as_of_date", signal_col, "validation_forward_return"]].copy()
    work["as_of_date"] = pd.to_datetime(work["as_of_date"], errors="coerce").dt.normalize()
    work[signal_col] = pd.to_numeric(work[signal_col], errors="coerce")
    work["validation_forward_return"] = pd.to_numeric(work["validation_forward_return"], errors="coerce")
    work = work.dropna(subset=["as_of_date", signal_col, "validation_forward_return"])
    grouped = {dt: sub[[signal_col, "validation_forward_return"]].copy() for dt, sub in work.groupby("as_of_date", sort=True)}
    dates = pd.DatetimeIndex(pd.to_datetime(out["as_of_date"], errors="coerce").dropna().unique()).sort_values()
    rows: list[dict[str, Any]] = []
    for idx, current_date in enumerate(dates):
        history_dates = [d for d in dates[max(0, idx - layer_cfg.validation_lookback_periods):idx] if d in grouped]
        if history_dates:
            hist = pd.concat([grouped[d] for d in history_dates], ignore_index=True)
        else:
            hist = pd.DataFrame(columns=[signal_col, "validation_forward_return"])
        observation_count = int(len(hist))
        period_count = int(len(history_dates))
        corr = np.nan
        ready = (
            period_count >= layer_cfg.validation_min_periods
            and observation_count >= layer_cfg.validation_min_observations
        )
        if ready:
            x = pd.to_numeric(hist[signal_col], errors="coerce")
            y = pd.to_numeric(hist["validation_forward_return"], errors="coerce")
            valid = x.notna() & y.notna()
            if int(valid.sum()) >= layer_cfg.validation_min_observations:
                corr = float(x.loc[valid].corr(y.loc[valid], method="spearman"))
            ready = bool(np.isfinite(corr))
        multiplier = (
            1.0
            if ready and float(corr) >= layer_cfg.validation_min_spearman
            else float(layer_cfg.validation_fallback_multiplier)
        )
        rows.append(
            {
                "as_of_date": current_date,
                "validation_spearman": corr,
                "validation_observation_count": observation_count,
                "validation_period_count": period_count,
                "macro_validation_multiplier": multiplier,
                "validation_ready_flag": int(bool(ready)),
            }
        )
    return pd.DataFrame(rows, columns=columns)


def _prepare_score_panel(score_panel: pd.DataFrame, *, layer_cfg: StockOverlayConfig) -> pd.DataFrame:
    rename_map = {
        "Date": "as_of_date",
        "Ticker": "ticker",
        "sector": "sector_name",
        "industry_aggregate": "industry_aggregate_name",
        "industry": "industry_name",
    }
    configured_columns = {
        str(layer_cfg.base_score_column or "").strip(): "base_score",
        str(layer_cfg.rating_column or "").strip(): "rating",
        str(layer_cfg.company_column or "").strip(): "company",
        str(layer_cfg.base_optimizer_eligible_column or "").strip(): "base_optimizer_eligible_raw",
        str(layer_cfg.earnings_blocked_column or "").strip(): "earnings_blocked_7d_raw",
    }
    rename_map.update({source: target for source, target in configured_columns.items() if source})

    out = score_panel.rename(columns=rename_map).copy()
    required = {"as_of_date", "ticker", "base_score", "sector_name", "industry_aggregate_name", "industry_name"}
    missing = sorted(required - set(out.columns))
    if missing:
        raise ValueError(
            "Stage 11 score panel is missing required columns after applying stock_macro_overlay.score_source "
            f"configuration: {missing}"
        )
    out["as_of_date"] = pd.to_datetime(out["as_of_date"], errors="coerce").dt.normalize()
    out["ticker"] = out["ticker"].astype(str).str.upper().str.strip()
    out["sector_name"] = out["sector_name"].astype(str).str.strip()
    out["industry_aggregate_name"] = out["industry_aggregate_name"].astype(str).str.strip()
    out["industry_name"] = out["industry_name"].astype(str).str.strip()
    out["company"] = (
        out["company"].fillna("").astype(str).str.strip()
        if "company" in out.columns
        else pd.Series("", index=out.index, dtype="object")
    )
    out["rating"] = (
        out["rating"].fillna("").astype(str).str.strip()
        if "rating" in out.columns
        else pd.Series("", index=out.index, dtype="object")
    )
    out["base_score"] = pd.to_numeric(out["base_score"], errors="coerce")
    if "base_optimizer_eligible_raw" in out.columns:
        out["base_optimizer_eligible"] = _coerce_bool_series(out["base_optimizer_eligible_raw"], default=True).astype(int)
    else:
        out["base_optimizer_eligible"] = 1
    if "earnings_blocked_7d_raw" in out.columns:
        out["earnings_blocked_7d"] = _coerce_bool_series(out["earnings_blocked_7d_raw"], default=False).astype(int)
    else:
        out["earnings_blocked_7d"] = 0
    if not layer_cfg.include_base_optimizer_ineligible:
        out = out.loc[out["base_optimizer_eligible"].astype(int).eq(1)].copy()
    for col in ("SnapshotSource", "ScoreApproach", "RunId"):
        if col not in out.columns:
            out[col] = ""
        out[col] = out[col].fillna("").astype(str).str.strip()
    if "source_pipeline" in out.columns:
        out["source_pipeline"] = out["source_pipeline"].fillna("").astype(str).str.strip()
    else:
        out["source_pipeline"] = ""
    return out.dropna(subset=["as_of_date", "ticker", "base_score"]).reset_index(drop=True)


def _build_overlay_frames(
    *,
    score_panel: pd.DataFrame,
    industry_macro: pd.DataFrame,
    aggregate_macro: pd.DataFrame,
    sector_macro: pd.DataFrame,
    tactical: pd.DataFrame,
    validation_returns: pd.DataFrame,
    layer_cfg: StockOverlayConfig,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    out = score_panel.merge(
        industry_macro,
        on=["as_of_date", "sector_name", "industry_aggregate_name", "industry_name"],
        how="left",
    )
    out = out.merge(
        aggregate_macro,
        on=["as_of_date", "sector_name", "industry_aggregate_name"],
        how="left",
    )
    out = out.merge(sector_macro, on=["as_of_date", "sector_name"], how="left")
    if layer_cfg.validation_enabled:
        out = out.merge(validation_returns, on=["as_of_date", "ticker"], how="left")
    else:
        out["validation_forward_return"] = np.nan
    out["rotation_sector_name"] = out["source_pipeline"].where(
        out["source_pipeline"].astype(str).str.strip().ne(""),
        out["sector_name"].map(layer_cfg.stock_sector_to_rotation_sector).fillna(out["sector_name"]),
    )
    out = out.merge(tactical, on=["as_of_date", "rotation_sector_name"], how="left")
    out["sector_tactical_lift"] = pd.to_numeric(out["sector_tactical_lift"], errors="coerce")
    out["sector_tactical_lift_z"] = pd.to_numeric(out["sector_tactical_lift_z"], errors="coerce")
    if layer_cfg.sector_tactical_missing_policy == "neutral":
        out["sector_tactical_lift"] = out["sector_tactical_lift"].fillna(layer_cfg.sector_tactical_neutral_value)
        out["sector_tactical_lift_z"] = out["sector_tactical_lift_z"].fillna(0.0)
    else:
        missing_count = int(out[["sector_tactical_lift", "sector_tactical_lift_z"]].isna().any(axis=1).sum())
        if missing_count:
            sample = (
                out.loc[
                    out[["sector_tactical_lift", "sector_tactical_lift_z"]].isna().any(axis=1),
                    ["as_of_date", "sector_name", "rotation_sector_name"],
                ]
                .drop_duplicates()
                .head(10)
                .to_dict("records")
            )
            raise ValueError(
                "Stage 11 sector tactical merge produced "
                f"{missing_count} missing rows and missing_policy=strict. Sample: {sample}"
            )

    out["base_stock_z"] = _zscore_by_date(
        out,
        "base_score",
        min_std=layer_cfg.zscore_min_std,
        clip_value=layer_cfg.zscore_component_clip,
    )
    for col in (
        "industry_macro_fit",
        "industry_aggregate_macro_fit",
        "sector_macro_fit",
        "industry_shock_prior_score",
        "sector_shock_prior_score",
    ):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out["shock_fit"] = (
        float(layer_cfg.shock_fit_weights["industry_shock_prior_weight"]) * out["industry_shock_prior_score"].fillna(0.0)
        + float(layer_cfg.shock_fit_weights["sector_shock_prior_weight"]) * out["sector_shock_prior_score"].fillna(0.0)
    ).clip(layer_cfg.shock_clip_min, layer_cfg.shock_clip_max)
    out["macro_stock_fit_raw"] = (
        float(layer_cfg.macro_fit_weights["industry_macro_fit"]) * out["industry_macro_fit"].fillna(0.0)
        + float(layer_cfg.macro_fit_weights["sector_macro_fit"]) * out["sector_macro_fit"].fillna(0.0)
        + float(layer_cfg.macro_fit_weights["shock_fit"]) * out["shock_fit"].fillna(0.0)
    )
    out["macro_stock_fit_z"] = _zscore_by_date(
        out,
        "macro_stock_fit_raw",
        min_std=layer_cfg.zscore_min_std,
        clip_value=layer_cfg.zscore_component_clip,
    )
    out["coverage_flag"] = (
        out["industry_macro_coverage_flag"].fillna(0).astype(int)
        & out["aggregate_macro_coverage_flag"].fillna(0).astype(int)
        & out["sector_macro_coverage_flag"].fillna(0).astype(int)
        & out["base_score"].notna().astype(int)
    ).astype(int)
    validation = _macro_validation_diagnostics(out, layer_cfg)
    out = out.merge(
        validation[["as_of_date", "macro_validation_multiplier"]],
        on="as_of_date",
        how="left",
    )
    out["macro_validation_multiplier"] = pd.to_numeric(
        out["macro_validation_multiplier"],
        errors="coerce",
    ).fillna(1.0 if not layer_cfg.validation_enabled else layer_cfg.validation_fallback_multiplier)
    out["macro_stock_fit_z_selection"] = out["macro_stock_fit_z"]
    out["macro_stock_fit_z_weight"] = out["macro_stock_fit_z"]
    if layer_cfg.validation_apply_to_selection:
        out["macro_stock_fit_z_selection"] = out["macro_stock_fit_z"] * out["macro_validation_multiplier"]
    if layer_cfg.validation_apply_to_weight:
        out["macro_stock_fit_z_weight"] = out["macro_stock_fit_z"] * out["macro_validation_multiplier"]
    uncovered = ~out["coverage_flag"].astype(int).eq(1)
    out.loc[uncovered, "macro_stock_fit_z_selection"] = 0.0
    out.loc[uncovered, "macro_stock_fit_z_weight"] = 0.0
    out["selection_score"] = (
        float(layer_cfg.selection_score_weights["base_stock_z"]) * out["base_stock_z"]
        + float(layer_cfg.selection_score_weights["macro_stock_fit_z"]) * out["macro_stock_fit_z_selection"]
        + float(layer_cfg.selection_score_weights["sector_tactical_lift_z"]) * out["sector_tactical_lift_z"]
    ).clip(-layer_cfg.final_score_clip, layer_cfg.final_score_clip)
    out["weight_score"] = (
        float(layer_cfg.weight_score_weights["base_stock_z"]) * out["base_stock_z"]
        + float(layer_cfg.weight_score_weights["macro_stock_fit_z"]) * out["macro_stock_fit_z_weight"]
        + float(layer_cfg.weight_score_weights["sector_tactical_lift_z"]) * out["sector_tactical_lift_z"]
    ).clip(-layer_cfg.final_score_clip, layer_cfg.final_score_clip)
    out["macro_favored_flag"] = (out["macro_stock_fit_z_selection"] >= float(layer_cfg.macro_favored_z_threshold)).astype(int)
    out["macro_adverse_flag"] = (out["macro_stock_fit_z_selection"] <= float(layer_cfg.macro_adverse_z_threshold)).astype(int)
    selection_rank, selection_pct = _rank_by_date(out, "selection_score")
    weight_rank, weight_pct = _rank_by_date(out, "weight_score")
    out["selection_rank"] = selection_rank
    out["selection_percentile"] = selection_pct
    out["weight_rank"] = weight_rank
    out["weight_percentile"] = weight_pct
    out["updated_at_utc"] = utc_now_iso()

    fit_cols = [
        "as_of_date",
        "ticker",
        "company",
        "sector_name",
        "industry_aggregate_name",
        "industry_name",
        "rating",
        "base_score",
        "base_stock_z",
        "industry_macro_fit",
        "industry_aggregate_macro_fit",
        "sector_macro_fit",
        "sector_tactical_lift",
        "sector_tactical_lift_z",
        "shock_fit",
        "macro_stock_fit_raw",
        "macro_stock_fit_z",
        "macro_favored_flag",
        "macro_adverse_flag",
        "base_optimizer_eligible",
        "earnings_blocked_7d",
        "SnapshotSource",
        "ScoreApproach",
        "RunId",
        "coverage_flag",
        "updated_at_utc",
    ]
    fit = out[fit_cols].rename(
        columns={"SnapshotSource": "snapshot_source", "ScoreApproach": "score_approach", "RunId": "run_id"}
    )
    selection_cols = [
        "as_of_date",
        "ticker",
        "sector_name",
        "industry_aggregate_name",
        "industry_name",
        "base_stock_z",
        "macro_stock_fit_z",
        "sector_tactical_lift_z",
        "selection_score",
        "selection_rank",
        "selection_percentile",
        "macro_favored_flag",
        "macro_adverse_flag",
        "base_optimizer_eligible",
        "coverage_flag",
        "updated_at_utc",
    ]
    weight_cols = [
        "as_of_date",
        "ticker",
        "sector_name",
        "industry_aggregate_name",
        "industry_name",
        "base_stock_z",
        "macro_stock_fit_z",
        "sector_tactical_lift_z",
        "weight_score",
        "weight_rank",
        "weight_percentile",
        "macro_favored_flag",
        "macro_adverse_flag",
        "base_optimizer_eligible",
        "coverage_flag",
        "updated_at_utc",
    ]
    return fit, out[selection_cols].copy(), out[weight_cols].copy(), validation


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


def main() -> None:
    configure_pipeline_logging()
    args = parse_args()
    config_path, cfg = load_macro_raw_config(args.config)
    layer_cfg = _resolve_layer_config(cfg, config_path)
    layer_cfg.output_dir.mkdir(parents=True, exist_ok=True)
    industry_layer_cfg = _resolve_industry_layer_config(cfg, config_path)
    score_panel_raw, weekly_dates, backtest_cfg, repo_root = _load_weekly_score_panel(
        industry_layer_cfg,
        start_date=None,
        end_date=args.end_date,
    )
    score_panel = _prepare_score_panel(score_panel_raw, layer_cfg=layer_cfg)
    serving_db_path = resolve_serving_db_path(cfg, config_path, override=args.serving_db_path)
    conn = connect_sqlite(serving_db_path, row_factory=sqlite3.Row)
    serving_run_id = uuid.uuid4().hex
    rows_written = 0
    run_started = False
    try:
        init_db(conn)
        write_start_date, write_end_date = _resolve_build_bounds(
            conn,
            score_dates=weekly_dates,
            start_override=args.start_date,
            end_override=args.end_date,
        )
        score_panel = score_panel.loc[
            (score_panel["as_of_date"].dt.date >= write_start_date)
            & (score_panel["as_of_date"].dt.date <= write_end_date)
        ].copy()
        if score_panel.empty:
            raise ValueError("Stage 11 score panel is empty for the requested write range.")
        industry_macro, aggregate_macro, sector_macro = _load_stage9_macro_frames(
            conn,
            start_date=write_start_date.isoformat(),
            end_date=write_end_date.isoformat(),
        )
        tactical = _load_sector_tactical_frame(
            layer_cfg,
            pd.DatetimeIndex(score_panel["as_of_date"].dropna().unique()).sort_values(),
        )
        validation_returns = _load_validation_forward_returns(
            score_panel,
            weekly_dates=pd.DatetimeIndex(score_panel["as_of_date"].dropna().unique()).sort_values(),
            backtest_cfg=backtest_cfg,
            repo_root=repo_root,
            layer_cfg=layer_cfg,
        )
        fit_frame, selection_frame, weight_frame, validation_frame = _build_overlay_frames(
            score_panel=score_panel,
            industry_macro=industry_macro,
            aggregate_macro=aggregate_macro,
            sector_macro=sector_macro,
            tactical=tactical,
            validation_returns=validation_returns,
            layer_cfg=layer_cfg,
        )
        _write_atomic_csv(layer_cfg.output_dir / "stock_macro_validation_diagnostics.csv", validation_frame)

        start_serving_run(
            conn,
            serving_run_id=serving_run_id,
            build_step="stock_macro_overlay",
            raw_ingest_run_id=_latest_regime_decision_run_raw_ingest_id(conn),
            as_of_start_date=write_start_date.isoformat(),
            as_of_end_date=write_end_date.isoformat(),
            metric_count=int(fit_frame["ticker"].nunique()),
            notes=(
                f"cadence={layer_cfg.cadence} "
                f"score_source=inherit_industry_macro_layer tactical_enabled={layer_cfg.sector_tactical_enabled}"
            ),
        )
        run_started = True
        for table_name in ("stock_macro_fit_daily", "stock_selection_score_daily", "stock_weight_score_daily"):
            clear_stock_macro_range(
                conn,
                table_name=table_name,
                start_date=write_start_date.isoformat(),
                end_date=write_end_date.isoformat(),
            )

        fit_columns = [
            "as_of_date",
            "ticker",
            "company",
            "sector_name",
            "industry_aggregate_name",
            "industry_name",
            "rating",
            "base_score",
            "base_stock_z",
            "industry_macro_fit",
            "industry_aggregate_macro_fit",
            "sector_macro_fit",
            "sector_tactical_lift",
            "sector_tactical_lift_z",
            "shock_fit",
            "macro_stock_fit_raw",
            "macro_stock_fit_z",
            "macro_favored_flag",
            "macro_adverse_flag",
            "base_optimizer_eligible",
            "earnings_blocked_7d",
            "snapshot_source",
            "score_approach",
            "run_id",
            "coverage_flag",
            "updated_at_utc",
        ]
        rows_written += insert_many(
            conn,
            """
            INSERT OR REPLACE INTO stock_macro_fit_daily (
                as_of_date, ticker, company, sector_name, industry_aggregate_name, industry_name,
                rating, base_score, base_stock_z, industry_macro_fit, industry_aggregate_macro_fit,
                sector_macro_fit, sector_tactical_lift, sector_tactical_lift_z, shock_fit,
                macro_stock_fit_raw, macro_stock_fit_z, macro_favored_flag, macro_adverse_flag,
                base_optimizer_eligible, earnings_blocked_7d, snapshot_source, score_approach,
                run_id, coverage_flag, updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _frame_rows(fit_frame, fit_columns),
            chunk_size=50_000,
        )

        selection_columns = [
            "as_of_date",
            "ticker",
            "sector_name",
            "industry_aggregate_name",
            "industry_name",
            "base_stock_z",
            "macro_stock_fit_z",
            "sector_tactical_lift_z",
            "selection_score",
            "selection_rank",
            "selection_percentile",
            "macro_favored_flag",
            "macro_adverse_flag",
            "base_optimizer_eligible",
            "coverage_flag",
            "updated_at_utc",
        ]
        rows_written += insert_many(
            conn,
            """
            INSERT OR REPLACE INTO stock_selection_score_daily (
                as_of_date, ticker, sector_name, industry_aggregate_name, industry_name,
                base_stock_z, macro_stock_fit_z, sector_tactical_lift_z, selection_score,
                selection_rank, selection_percentile, macro_favored_flag, macro_adverse_flag,
                base_optimizer_eligible, coverage_flag, updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _frame_rows(selection_frame, selection_columns),
            chunk_size=50_000,
        )

        weight_columns = [
            "as_of_date",
            "ticker",
            "sector_name",
            "industry_aggregate_name",
            "industry_name",
            "base_stock_z",
            "macro_stock_fit_z",
            "sector_tactical_lift_z",
            "weight_score",
            "weight_rank",
            "weight_percentile",
            "macro_favored_flag",
            "macro_adverse_flag",
            "base_optimizer_eligible",
            "coverage_flag",
            "updated_at_utc",
        ]
        rows_written += insert_many(
            conn,
            """
            INSERT OR REPLACE INTO stock_weight_score_daily (
                as_of_date, ticker, sector_name, industry_aggregate_name, industry_name,
                base_stock_z, macro_stock_fit_z, sector_tactical_lift_z, weight_score,
                weight_rank, weight_percentile, macro_favored_flag, macro_adverse_flag,
                base_optimizer_eligible, coverage_flag, updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _frame_rows(weight_frame, weight_columns),
            chunk_size=50_000,
        )

        latest_key = pd.to_datetime(fit_frame["as_of_date"], errors="coerce").max().strftime("%Y-%m-%d")
        _write_atomic_csv(
            layer_cfg.output_dir / "stock_macro_fit_latest.csv",
            fit_frame.loc[pd.to_datetime(fit_frame["as_of_date"]).dt.strftime("%Y-%m-%d").eq(latest_key)]
            .sort_values("macro_stock_fit_z", ascending=False)
            .reset_index(drop=True),
        )
        _write_atomic_csv(
            layer_cfg.output_dir / "stock_selection_score_latest.csv",
            selection_frame.loc[pd.to_datetime(selection_frame["as_of_date"]).dt.strftime("%Y-%m-%d").eq(latest_key)]
            .sort_values("selection_rank")
            .reset_index(drop=True),
        )
        _write_atomic_csv(
            layer_cfg.output_dir / "stock_weight_score_latest.csv",
            weight_frame.loc[pd.to_datetime(weight_frame["as_of_date"]).dt.strftime("%Y-%m-%d").eq(latest_key)]
            .sort_values("weight_rank")
            .reset_index(drop=True),
        )
        finish_serving_run(conn, serving_run_id=serving_run_id, status="completed", rows_written=rows_written)
    except BaseException as exc:
        if run_started:
            try:
                finish_serving_run(
                    conn,
                    serving_run_id=serving_run_id,
                    status="failed",
                    rows_written=rows_written,
                    notes=f"Stock macro overlay failed: {type(exc).__name__}: {exc}",
                )
            except Exception:
                logger.exception("Failed to record failed stock macro overlay run for serving_run_id=%s", serving_run_id)
        raise
    finally:
        conn.close()

    logger.info(
        "Stage 11 stock macro overlay complete: rows_written=%d range=%s..%s output_dir=%s",
        rows_written,
        write_start_date.isoformat(),
        write_end_date.isoformat(),
        layer_cfg.output_dir,
    )


if __name__ == "__main__":
    main()
