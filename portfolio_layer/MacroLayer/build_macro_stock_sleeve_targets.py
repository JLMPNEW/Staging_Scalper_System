#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
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
    clear_stock_sleeve_target_range,
    finish_serving_run,
    init_db,
    insert_many,
    start_serving_run,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StockSleeveTargetConfig:
    output_dir: Path
    build_scope: str
    eligible_state: str
    require_stock_macro_coverage: bool
    opportunity_score_column: str
    opportunity_top_n: int
    opportunity_min_eligible_members: int
    opportunity_transform: str
    opportunity_softplus_beta: float
    macro_score_column: str
    macro_transform: str
    macro_softplus_beta: float
    macro_fallback_value: float
    equal_weight_blend: float
    max_industry_weight: float
    min_target_weight: float
    renormalize_after_caps: bool
    industry_abs_width: float
    industry_rel_width: float
    industry_min_width: float
    sector_abs_width: float
    sector_rel_width: float
    sector_min_width: float
    max_sector_weight: float
    industry_csv_name: str
    sector_csv_name: str
    summary_csv_name: str
    acceptance: dict[str, Any]


INDUSTRY_COLUMNS = [
    "as_of_date",
    "sector_name",
    "industry_aggregate_name",
    "industry_name",
    "member_count",
    "eligible_member_count",
    "top_member_count",
    "industry_macro_fit",
    "opportunity_score",
    "macro_component",
    "opportunity_component",
    "raw_target_score",
    "target_weight",
    "min_weight",
    "max_weight",
    "target_rank",
    "target_percentile",
    "coverage_flag",
    "updated_at_utc",
]


SECTOR_COLUMNS = [
    "as_of_date",
    "sector_name",
    "industry_count",
    "targetable_industry_count",
    "eligible_member_count",
    "avg_industry_macro_fit",
    "avg_opportunity_score",
    "target_weight",
    "min_weight",
    "max_weight",
    "target_rank",
    "target_percentile",
    "coverage_flag",
    "updated_at_utc",
]


SUMMARY_COLUMNS = [
    "as_of_date",
    "industry_count",
    "targetable_industry_count",
    "sector_count",
    "eligible_stock_count",
    "target_weight_sum",
    "max_industry_target_weight",
    "max_sector_target_weight",
    "effective_industry_count",
    "industry_output_csv",
    "sector_output_csv",
    "updated_at_utc",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build Stage 12B stock sleeve industry target weights and bands.")
    parser.add_argument("--config", type=Path, default=None, help="Path to macro raw YAML config.")
    parser.add_argument("--serving-db-path", type=Path, default=None, help="Optional serving SQLite path override.")
    parser.add_argument("--start-date", type=str, default=None, help="Optional Stage 12B start YYYY-MM-DD override.")
    parser.add_argument("--end-date", type=str, default=None, help="Optional Stage 12B end YYYY-MM-DD override.")
    return parser.parse_args()


def _resolve_layer_config(cfg: dict[str, Any], config_path: Path) -> StockSleeveTargetConfig:
    raw_cfg = dict(cfg_get(cfg, "stock_sleeve_target_layer", default={}) or {})
    output_dir = resolve_path(config_path, str(raw_cfg.get("output_dir", "MacroLayer/out/stock_sleeve_targets")))
    if output_dir is None:
        raise ValueError("stock_sleeve_target_layer.output_dir could not be resolved.")
    build_scope = str(raw_cfg.get("build_scope", "all")).strip().lower() or "all"
    if build_scope not in {"all", "latest"}:
        raise ValueError("stock_sleeve_target_layer.build_scope must be one of: all, latest.")
    opportunity_cfg = dict(raw_cfg.get("opportunity", {}) or {})
    macro_cfg = dict(raw_cfg.get("macro", {}) or {})
    target_cfg = dict(raw_cfg.get("target", {}) or {})
    bands_cfg = dict(raw_cfg.get("bands", {}) or {})
    exports_cfg = dict(raw_cfg.get("exports", {}) or {})
    opportunity_transform = str(opportunity_cfg.get("transform", "softplus")).strip().lower() or "softplus"
    macro_transform = str(macro_cfg.get("transform", "softplus")).strip().lower() or "softplus"
    if opportunity_transform not in {"softplus", "positive"}:
        raise ValueError("stock_sleeve_target_layer.opportunity.transform must be one of: softplus, positive.")
    if macro_transform not in {"softplus", "positive"}:
        raise ValueError("stock_sleeve_target_layer.macro.transform must be one of: softplus, positive.")
    return StockSleeveTargetConfig(
        output_dir=output_dir,
        build_scope=build_scope,
        eligible_state=str(raw_cfg.get("eligible_state", "Eligible")).strip() or "Eligible",
        require_stock_macro_coverage=parse_boolish(raw_cfg.get("require_stock_macro_coverage"), default=True),
        opportunity_score_column=str(opportunity_cfg.get("score_column", "weight_score")).strip() or "weight_score",
        opportunity_top_n=max(1, int(opportunity_cfg.get("top_n", 5))),
        opportunity_min_eligible_members=max(1, int(opportunity_cfg.get("min_eligible_members", 1))),
        opportunity_transform=opportunity_transform,
        opportunity_softplus_beta=max(1e-9, float(opportunity_cfg.get("softplus_beta", 1.0))),
        macro_score_column=str(macro_cfg.get("score_column", "industry_macro_fit")).strip() or "industry_macro_fit",
        macro_transform=macro_transform,
        macro_softplus_beta=max(1e-9, float(macro_cfg.get("softplus_beta", 1.0))),
        macro_fallback_value=float(macro_cfg.get("fallback_value", 0.0)),
        equal_weight_blend=min(1.0, max(0.0, float(target_cfg.get("equal_weight_blend", 0.15)))),
        max_industry_weight=max(0.0, float(target_cfg.get("max_industry_weight", 0.08))),
        min_target_weight=max(0.0, float(target_cfg.get("min_target_weight", 0.0))),
        renormalize_after_caps=parse_boolish(target_cfg.get("renormalize_after_caps"), default=True),
        industry_abs_width=max(0.0, float(bands_cfg.get("industry_abs_width", 0.010))),
        industry_rel_width=max(0.0, float(bands_cfg.get("industry_rel_width", 0.35))),
        industry_min_width=max(0.0, float(bands_cfg.get("industry_min_width", 0.005))),
        sector_abs_width=max(0.0, float(bands_cfg.get("sector_abs_width", 0.030))),
        sector_rel_width=max(0.0, float(bands_cfg.get("sector_rel_width", 0.30))),
        sector_min_width=max(0.0, float(bands_cfg.get("sector_min_width", 0.010))),
        max_sector_weight=max(0.0, float(bands_cfg.get("max_sector_weight", 0.35))),
        industry_csv_name=str(exports_cfg.get("industry_csv_name", "stock_industry_targets_latest.csv")).strip()
        or "stock_industry_targets_latest.csv",
        sector_csv_name=str(exports_cfg.get("sector_csv_name", "stock_sector_targets_latest.csv")).strip()
        or "stock_sector_targets_latest.csv",
        summary_csv_name=str(exports_cfg.get("summary_csv_name", "stock_sleeve_target_summary_latest.csv")).strip()
        or "stock_sleeve_target_summary_latest.csv",
        acceptance=dict(raw_cfg.get("acceptance", {}) or {}),
    )


def _softplus(values: pd.Series | np.ndarray, beta: float) -> np.ndarray:
    arr = np.asarray(values, dtype=float)
    return np.logaddexp(0.0, beta * arr) / beta


def _positive_component(values: pd.Series, *, transform: str, beta: float) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce").fillna(0.0)
    if transform == "positive":
        component = numeric.clip(lower=0.0)
    else:
        component = pd.Series(_softplus(numeric.to_numpy(dtype=float), beta), index=numeric.index)
    component = pd.to_numeric(component, errors="coerce").replace([np.inf, -np.inf], np.nan).fillna(0.0)
    return component.clip(lower=0.0)


def _resolve_build_bounds(
    conn: sqlite3.Connection,
    *,
    layer_cfg: StockSleeveTargetConfig,
    start_override: str | None,
    end_override: str | None,
) -> tuple[date, date]:
    row = conn.execute(
        """
        SELECT MIN(as_of_date) AS min_date, MAX(as_of_date) AS max_date
        FROM portfolio_inputs_daily
        WHERE asset_type = 'US_STOCK'
        """
    ).fetchone()
    min_date = parse_iso_date(row["min_date"]) if row is not None else None
    max_date = parse_iso_date(row["max_date"]) if row is not None else None
    if min_date is None or max_date is None:
        raise ValueError("portfolio_inputs_daily has no US_STOCK rows. Build Stage 12A before Stage 12B.")
    if layer_cfg.build_scope == "latest" and not start_override and not end_override:
        return max_date, max_date
    start_date = parse_iso_date(start_override) or min_date
    end_date = parse_iso_date(end_override) or max_date
    if end_date < start_date:
        raise ValueError(f"Stage 12B end date {end_date.isoformat()} is before start date {start_date.isoformat()}.")
    if start_date < min_date or end_date > max_date:
        raise ValueError(
            f"Stage 12B requested range {start_date.isoformat()}..{end_date.isoformat()} is outside "
            f"available Stage 12A stock range {min_date.isoformat()}..{max_date.isoformat()}."
        )
    return start_date, end_date


def _load_stock_inputs(conn: sqlite3.Connection, *, start_date: str, end_date: str) -> pd.DataFrame:
    frame = pd.read_sql_query(
        """
        SELECT
            as_of_date,
            ticker,
            sector_name,
            industry_aggregate_name,
            industry_name,
            state,
            base_optimizer_eligible,
            stock_macro_coverage_flag,
            industry_macro_fit,
            selection_score,
            weight_score,
            expected_return_score,
            final_score
        FROM portfolio_inputs_daily
        WHERE asset_type = 'US_STOCK'
          AND as_of_date BETWEEN ? AND ?
        """,
        conn,
        params=[start_date, end_date],
        parse_dates=["as_of_date"],
    )
    if frame.empty:
        raise ValueError(f"No Stage 12A US_STOCK rows found for {start_date}..{end_date}.")
    for col in ("sector_name", "industry_aggregate_name", "industry_name", "state", "ticker"):
        frame[col] = frame[col].fillna("").astype(str).str.strip()
    return frame.dropna(subset=["as_of_date"]).reset_index(drop=True)


def _weighted_cap_normalize(raw: pd.Series, *, cap: float, floor: float, renormalize: bool) -> pd.Series:
    raw = pd.to_numeric(raw, errors="coerce").fillna(0.0).clip(lower=0.0)
    active = raw.gt(0.0)
    out = pd.Series(0.0, index=raw.index, dtype="float64")
    if not active.any():
        return out
    values = raw.loc[active].copy()
    values = values / float(values.sum())
    if floor > 0.0:
        values = values.clip(lower=floor)
        values = values / float(values.sum())
    if cap <= 0.0:
        out.loc[active] = values
        return out
    if not renormalize:
        out.loc[active] = values.clip(upper=cap)
        return out

    remaining = values.index.tolist()
    capped = pd.Series(0.0, index=values.index, dtype="float64")
    budget = 1.0
    while remaining:
        current = values.loc[remaining]
        if float(current.sum()) <= 0.0:
            capped.loc[remaining] = budget / len(remaining)
            break
        scaled = current / float(current.sum()) * budget
        over = scaled.gt(cap)
        if not bool(over.any()):
            capped.loc[remaining] = scaled
            break
        over_idx = scaled.loc[over].index.tolist()
        capped.loc[over_idx] = cap
        budget -= cap * len(over_idx)
        remaining = [idx for idx in remaining if idx not in set(over_idx)]
        if budget <= 1e-12:
            break
    total = float(capped.sum())
    if total > 0.0:
        capped = capped / total
    out.loc[capped.index] = capped
    return out


def _rank_targets(frame: pd.DataFrame, weight_col: str) -> tuple[pd.Series, pd.Series]:
    ranks = (
        pd.to_numeric(frame[weight_col], errors="coerce")
        .groupby(frame["as_of_date"])
        .rank(method="first", ascending=False)
        .astype("Int64")
    )
    percentiles = (
        pd.to_numeric(frame[weight_col], errors="coerce")
        .groupby(frame["as_of_date"])
        .rank(method="first", ascending=True, pct=True)
        .fillna(0.0)
        .clip(0.0, 1.0)
    )
    return ranks, percentiles


def _add_bands(
    frame: pd.DataFrame,
    *,
    target_col: str,
    abs_width: float,
    rel_width: float,
    min_width: float,
    max_weight: float,
) -> pd.DataFrame:
    target = pd.to_numeric(frame[target_col], errors="coerce").fillna(0.0).clip(lower=0.0)
    width = np.maximum(min_width, abs_width + rel_width * target)
    lower = (target - width).clip(lower=0.0)
    upper = target + width
    if max_weight > 0.0:
        upper = upper.clip(upper=max_weight)
    upper = np.maximum(upper, target)
    out = frame.copy()
    out["min_weight"] = lower
    out["max_weight"] = upper
    return out


def _build_industry_targets(stock: pd.DataFrame, layer_cfg: StockSleeveTargetConfig, *, updated_at: str) -> pd.DataFrame:
    if layer_cfg.opportunity_score_column not in stock.columns:
        raise ValueError(f"Stage 12B opportunity score column not found: {layer_cfg.opportunity_score_column}")
    if layer_cfg.macro_score_column not in stock.columns:
        raise ValueError(f"Stage 12B macro score column not found: {layer_cfg.macro_score_column}")

    stock = stock.copy()
    stock["_eligible"] = stock["state"].eq(layer_cfg.eligible_state) & pd.to_numeric(
        stock["base_optimizer_eligible"],
        errors="coerce",
    ).fillna(0).astype(int).eq(1)
    if layer_cfg.require_stock_macro_coverage:
        stock["_eligible"] = stock["_eligible"] & pd.to_numeric(
            stock["stock_macro_coverage_flag"],
            errors="coerce",
        ).fillna(0).astype(int).eq(1)
    score_col = layer_cfg.opportunity_score_column
    macro_col = layer_cfg.macro_score_column
    group_cols = ["as_of_date", "sector_name", "industry_aggregate_name", "industry_name"]
    rows: list[dict[str, Any]] = []
    for keys, group in stock.groupby(group_cols, sort=True):
        as_of_date, sector_name, aggregate_name, industry_name = keys
        eligible = group.loc[group["_eligible"]].copy()
        member_count = int(group["ticker"].nunique())
        eligible_count = int(eligible["ticker"].nunique())
        top_member_count = min(layer_cfg.opportunity_top_n, eligible_count)
        if top_member_count > 0:
            top_scores = (
                pd.to_numeric(eligible[score_col], errors="coerce")
                .dropna()
                .sort_values(ascending=False)
                .head(layer_cfg.opportunity_top_n)
            )
            opportunity_score = float(top_scores.mean()) if not top_scores.empty else np.nan
            top_member_count = int(len(top_scores))
        else:
            opportunity_score = np.nan
        macro_values = pd.to_numeric(group[macro_col], errors="coerce").dropna()
        industry_macro_fit = float(macro_values.median()) if not macro_values.empty else layer_cfg.macro_fallback_value
        coverage_flag = int(
            eligible_count >= layer_cfg.opportunity_min_eligible_members
            and top_member_count > 0
            and np.isfinite(float(industry_macro_fit))
            and np.isfinite(float(opportunity_score)) if pd.notna(opportunity_score) else False
        )
        rows.append(
            {
                "as_of_date": pd.Timestamp(as_of_date).normalize(),
                "sector_name": str(sector_name),
                "industry_aggregate_name": str(aggregate_name),
                "industry_name": str(industry_name),
                "member_count": member_count,
                "eligible_member_count": eligible_count,
                "top_member_count": top_member_count,
                "industry_macro_fit": industry_macro_fit,
                "opportunity_score": opportunity_score,
                "coverage_flag": coverage_flag,
            }
        )
    out = pd.DataFrame(rows)
    if out.empty:
        raise ValueError("Stage 12B produced no industry groups.")
    out["macro_component"] = _positive_component(
        out["industry_macro_fit"].fillna(layer_cfg.macro_fallback_value),
        transform=layer_cfg.macro_transform,
        beta=layer_cfg.macro_softplus_beta,
    )
    out["opportunity_component"] = _positive_component(
        out["opportunity_score"].fillna(0.0),
        transform=layer_cfg.opportunity_transform,
        beta=layer_cfg.opportunity_softplus_beta,
    )
    out["raw_target_score"] = (
        out["macro_component"].fillna(0.0)
        * out["opportunity_component"].fillna(0.0)
        * out["coverage_flag"].astype(int)
    )
    target_parts: list[pd.Series] = []
    for _, sub in out.groupby("as_of_date", sort=True):
        raw = sub["raw_target_score"].copy()
        active = raw.gt(0.0)
        if active.any() and layer_cfg.equal_weight_blend > 0.0:
            raw_norm = raw / float(raw.sum())
            equal = pd.Series(0.0, index=sub.index, dtype="float64")
            equal.loc[active] = 1.0 / int(active.sum())
            blended = (1.0 - layer_cfg.equal_weight_blend) * raw_norm + layer_cfg.equal_weight_blend * equal
        else:
            blended = raw
        target_parts.append(
            _weighted_cap_normalize(
                blended,
                cap=layer_cfg.max_industry_weight,
                floor=layer_cfg.min_target_weight,
                renormalize=layer_cfg.renormalize_after_caps,
            )
        )
    out["target_weight"] = pd.concat(target_parts).sort_index()
    out = _add_bands(
        out,
        target_col="target_weight",
        abs_width=layer_cfg.industry_abs_width,
        rel_width=layer_cfg.industry_rel_width,
        min_width=layer_cfg.industry_min_width,
        max_weight=layer_cfg.max_industry_weight,
    )
    out["target_rank"], out["target_percentile"] = _rank_targets(out, "target_weight")
    out["updated_at_utc"] = updated_at
    return out[INDUSTRY_COLUMNS].sort_values(["as_of_date", "target_rank"]).reset_index(drop=True)


def _build_sector_targets(industry: pd.DataFrame, layer_cfg: StockSleeveTargetConfig, *, updated_at: str) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for keys, group in industry.groupby(["as_of_date", "sector_name"], sort=True):
        as_of_date, sector_name = keys
        target = pd.to_numeric(group["target_weight"], errors="coerce").fillna(0.0)
        covered = group.loc[group["coverage_flag"].astype(int).eq(1)]
        rows.append(
            {
                "as_of_date": pd.Timestamp(as_of_date).normalize(),
                "sector_name": str(sector_name),
                "industry_count": int(len(group)),
                "targetable_industry_count": int(len(covered)),
                "eligible_member_count": int(pd.to_numeric(group["eligible_member_count"], errors="coerce").fillna(0).sum()),
                "avg_industry_macro_fit": float(pd.to_numeric(group["industry_macro_fit"], errors="coerce").mean()),
                "avg_opportunity_score": float(pd.to_numeric(group["opportunity_score"], errors="coerce").mean()),
                "target_weight": float(target.sum()),
                "coverage_flag": int(len(covered) > 0 and float(target.sum()) > 0.0),
            }
        )
    out = pd.DataFrame(rows)
    out = _add_bands(
        out,
        target_col="target_weight",
        abs_width=layer_cfg.sector_abs_width,
        rel_width=layer_cfg.sector_rel_width,
        min_width=layer_cfg.sector_min_width,
        max_weight=layer_cfg.max_sector_weight,
    )
    out["target_rank"], out["target_percentile"] = _rank_targets(out, "target_weight")
    out["updated_at_utc"] = updated_at
    return out[SECTOR_COLUMNS].sort_values(["as_of_date", "target_rank"]).reset_index(drop=True)


def _build_summary(
    industry: pd.DataFrame,
    sector: pd.DataFrame,
    *,
    industry_csv: Path,
    sector_csv: Path,
    updated_at: str,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for as_of_date, sub in industry.groupby("as_of_date", sort=True):
        target = pd.to_numeric(sub["target_weight"], errors="coerce").fillna(0.0)
        sector_sub = sector.loc[pd.to_datetime(sector["as_of_date"]).eq(pd.Timestamp(as_of_date))]
        sector_target = pd.to_numeric(sector_sub["target_weight"], errors="coerce").fillna(0.0)
        effective_count = 0.0
        if float((target ** 2).sum()) > 0.0:
            effective_count = 1.0 / float((target ** 2).sum())
        rows.append(
            {
                "as_of_date": pd.Timestamp(as_of_date).strftime("%Y-%m-%d"),
                "industry_count": int(len(sub)),
                "targetable_industry_count": int(sub["coverage_flag"].astype(int).sum()),
                "sector_count": int(len(sector_sub)),
                "eligible_stock_count": int(pd.to_numeric(sub["eligible_member_count"], errors="coerce").fillna(0).sum()),
                "target_weight_sum": float(target.sum()),
                "max_industry_target_weight": float(target.max()) if len(target) else np.nan,
                "max_sector_target_weight": float(sector_target.max()) if len(sector_target) else np.nan,
                "effective_industry_count": effective_count,
                "industry_output_csv": str(industry_csv),
                "sector_output_csv": str(sector_csv),
                "updated_at_utc": updated_at,
            }
        )
    return pd.DataFrame(rows, columns=SUMMARY_COLUMNS)


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
        stock = _load_stock_inputs(conn, start_date=start_date.isoformat(), end_date=end_date.isoformat())
        industry = _build_industry_targets(stock, layer_cfg, updated_at=updated_at)
        sector = _build_sector_targets(industry, layer_cfg, updated_at=updated_at)
        industry_csv = layer_cfg.output_dir / layer_cfg.industry_csv_name
        sector_csv = layer_cfg.output_dir / layer_cfg.sector_csv_name
        summary_csv = layer_cfg.output_dir / layer_cfg.summary_csv_name
        summary = _build_summary(industry, sector, industry_csv=industry_csv, sector_csv=sector_csv, updated_at=updated_at)

        start_serving_run(
            conn,
            serving_run_id=serving_run_id,
            build_step="stock_sleeve_target_layer",
            raw_ingest_run_id=_latest_portfolio_input_run_raw_ingest_id(conn),
            as_of_start_date=start_date.isoformat(),
            as_of_end_date=end_date.isoformat(),
            metric_count=int(industry["industry_name"].nunique()),
            notes=(
                f"build_scope={layer_cfg.build_scope} opportunity={layer_cfg.opportunity_score_column} "
                f"max_industry_weight={layer_cfg.max_industry_weight}"
            ),
        )
        run_started = True
        for table_name in ("stock_industry_target_daily", "stock_sector_target_daily", "stock_sleeve_target_summary"):
            clear_stock_sleeve_target_range(
                conn,
                table_name=table_name,
                start_date=start_date.isoformat(),
                end_date=end_date.isoformat(),
            )
        rows_written += insert_many(
            conn,
            """
            INSERT OR REPLACE INTO stock_industry_target_daily (
                as_of_date, sector_name, industry_aggregate_name, industry_name,
                member_count, eligible_member_count, top_member_count, industry_macro_fit,
                opportunity_score, macro_component, opportunity_component, raw_target_score,
                target_weight, min_weight, max_weight, target_rank, target_percentile,
                coverage_flag, updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _frame_rows(industry, INDUSTRY_COLUMNS),
            chunk_size=50_000,
        )
        rows_written += insert_many(
            conn,
            """
            INSERT OR REPLACE INTO stock_sector_target_daily (
                as_of_date, sector_name, industry_count, targetable_industry_count,
                eligible_member_count, avg_industry_macro_fit, avg_opportunity_score,
                target_weight, min_weight, max_weight, target_rank, target_percentile,
                coverage_flag, updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _frame_rows(sector, SECTOR_COLUMNS),
            chunk_size=50_000,
        )
        rows_written += insert_many(
            conn,
            """
            INSERT OR REPLACE INTO stock_sleeve_target_summary (
                as_of_date, industry_count, targetable_industry_count, sector_count,
                eligible_stock_count, target_weight_sum, max_industry_target_weight,
                max_sector_target_weight, effective_industry_count, industry_output_csv,
                sector_output_csv, updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _frame_rows(summary, SUMMARY_COLUMNS),
            chunk_size=50_000,
        )

        latest_date = pd.to_datetime(industry["as_of_date"], errors="coerce").max()
        latest_key = latest_date.strftime("%Y-%m-%d")
        _write_atomic_csv(
            industry_csv,
            industry.loc[pd.to_datetime(industry["as_of_date"]).dt.strftime("%Y-%m-%d").eq(latest_key)]
            .sort_values("target_rank")
            .reset_index(drop=True),
        )
        _write_atomic_csv(
            sector_csv,
            sector.loc[pd.to_datetime(sector["as_of_date"]).dt.strftime("%Y-%m-%d").eq(latest_key)]
            .sort_values("target_rank")
            .reset_index(drop=True),
        )
        _write_atomic_csv(
            summary_csv,
            summary.loc[pd.to_datetime(summary["as_of_date"]).dt.strftime("%Y-%m-%d").eq(latest_key)].reset_index(drop=True),
        )
        finish_serving_run(conn, serving_run_id=serving_run_id, status="completed", rows_written=rows_written)
        logger.info(
            "Stage 12B stock sleeve targets complete: rows_written=%d range=%s..%s output_dir=%s",
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
                logger.exception("Failed to mark Stage 12B serving run as failed.")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
