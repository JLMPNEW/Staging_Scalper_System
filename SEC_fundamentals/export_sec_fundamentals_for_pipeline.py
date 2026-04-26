#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from sec_fundamentals_config import (
    SAFE_DIVIDE_MIN_ABS_DENOMINATOR,
    cfg_get,
    configure_pipeline_logging,
    load_sec_fundamentals_config,
    safe_div_series,
    sql_normalized_cik_expr,
)

DEFAULT_DB_PATH = Path(r"C:\Users\josel\Documents\PROD\DB\sec_fundamentals.sqlite")
DEFAULT_CONFIG_PATH = Path(__file__).resolve().with_name("config_sec_fundamentals.yaml")
DEFAULT_SNAPSHOT_TABLES = (
    "sec_fundamental_snapshot_filled_security_t1_resolved",
    "sec_fundamental_snapshot_filled_security_t1",
)
NET_DEBT_TO_EBITDA_CAP = 30.0
SQLITE_BUSY_TIMEOUT_MS = 30000
logger = logging.getLogger(__name__)


def default_db_path() -> Path:
    return Path(os.getenv("SEC_FUNDAMENTALS_DB_PATH", str(DEFAULT_DB_PATH)))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export SEC fundamentals snapshot into Yahoo-compatible field aliases (CSV + JSON)."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Path to fundamentals config.")
    parser.add_argument("--db-path", type=Path, default=None, help="Override fundamentals SQLite DB path.")
    parser.add_argument("--as-of-date", type=str, default=None, help="Optional as_of_date YYYY-MM-DD.")
    parser.add_argument("--output-csv", type=Path, default=None, help="Override output CSV path.")
    parser.add_argument("--output-json", type=Path, default=None, help="Override output JSON path.")
    return parser.parse_args()


def _coalesce_numeric(df: pd.DataFrame, cols: list[str]) -> pd.Series:
    out = pd.Series(np.nan, index=df.index, dtype="float64")
    for col in cols:
        if col in df.columns:
            out = out.fillna(pd.to_numeric(df[col], errors="coerce"))
    return out


def _to_num(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df.columns:
        return pd.Series(np.nan, index=df.index, dtype="float64")
    return pd.to_numeric(df[col], errors="coerce")


def _normalize_cik_key(series: pd.Series) -> pd.Series:
    return (
        series.astype("string")
        .str.extract(r"(\d+)", expand=False)
        .str.zfill(10)
    )


def _cap_abs(series: pd.Series, limit: float) -> pd.Series:
    lim = abs(float(limit))
    out = pd.to_numeric(series, errors="coerce")
    out = out.where(np.isfinite(out), np.nan)
    if lim <= 0:
        return out
    return out.clip(lower=-lim, upper=lim)


def _safe_divide(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    return safe_div_series(numerator, denominator, eps=SAFE_DIVIDE_MIN_ABS_DENOMINATOR)


def _sum_present_components(df: pd.DataFrame, cols: list[str]) -> pd.Series:
    comp_df = pd.concat([_to_num(df, col) for col in cols], axis=1)
    return comp_df.fillna(0.0).sum(axis=1).where(comp_df.notna().any(axis=1), np.nan)


def _validate_row_count(
    pre: int,
    post: int,
    *,
    threshold: float = 0.90,
    label: str,
    exact: bool = False,
) -> None:
    if pre < 0 or post < 0:
        raise ValueError(f"{label}: row counts must be non-negative (pre={pre}, post={post})")
    if exact:
        if post != pre:
            raise RuntimeError(f"{label}: expected {pre} rows, got {post}")
        return
    if pre == 0:
        return
    if post < int(np.ceil(pre * float(threshold))):
        raise RuntimeError(
            f"{label}: {post} rows out of {pre} ({post / pre:.1%}) below {float(threshold):.0%} floor"
        )


def _validate_required_columns(df: pd.DataFrame, required: list[str], *, label: str) -> None:
    missing = [col for col in required if col not in df.columns]
    if missing:
        raise RuntimeError(f"{label}: missing required columns: {missing}")


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table_name,),
    ).fetchone()
    return row is not None


def _choose_existing_table(conn: sqlite3.Connection, candidates: tuple[str, ...]) -> str:
    for table_name in candidates:
        if _table_exists(conn, table_name):
            return table_name
    return ""


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    if not _table_exists(conn, table_name):
        return set()
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table_name})")}


def _connect_sqlite(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def main() -> None:
    configure_pipeline_logging()
    args = parse_args()
    cfg_path, cfg = load_sec_fundamentals_config(args.config)
    raw_db_path = args.db_path if args.db_path is not None else cfg_get(cfg, "db_path", default=str(default_db_path()))
    db_path = Path(raw_db_path).expanduser()
    if not db_path.is_absolute():
        if args.db_path is not None:
            db_path = (Path.cwd() / db_path).resolve()
        else:
            db_path = (cfg_path.parent.parent / db_path).resolve()
    features_cfg = cfg_get(cfg, "features", default={})
    net_debt_ebitda_cap = float(cfg_get(features_cfg, "net_debt_ebitda_cap", default=NET_DEBT_TO_EBITDA_CAP))

    conn = _connect_sqlite(db_path)
    try:
        snapshot_table = _choose_existing_table(conn, DEFAULT_SNAPSHOT_TABLES)
        if not snapshot_table:
            raise RuntimeError(
                "No enhanced SEC snapshot table found. "
                "Build sec_fundamental_snapshot_filled_security_t1_resolved or "
                "sec_fundamental_snapshot_filled_security_t1 first."
            )

        as_of_date = args.as_of_date
        if as_of_date is None or not str(as_of_date).strip():
            as_of_date = conn.execute(
                f"SELECT MAX(as_of_date) FROM {snapshot_table}"
            ).fetchone()[0]
        if as_of_date is None or not str(as_of_date).strip():
            raise RuntimeError(
                f"No rows in {snapshot_table} to export. "
                "Run build_sec_tier1_snapshot_enhanced.py first."
            )
        snapshot_cols = _table_columns(conn, snapshot_table)
        if not snapshot_cols:
            raise RuntimeError(f"Unable to introspect columns for snapshot table: {snapshot_table}")

        def _scol(name: str, default_sql: str = "NULL", alias: str | None = None) -> str:
            out_alias = alias or name
            if name in snapshot_cols:
                return f"{name} AS {out_alias}"
            return f"{default_sql} AS {out_alias}"

        if "report_period_end" in snapshot_cols and "anchor_period_end" in snapshot_cols:
            snapshot_period_expr = "COALESCE(report_period_end, anchor_period_end) AS report_period_end"
        elif "report_period_end" in snapshot_cols:
            snapshot_period_expr = "report_period_end AS report_period_end"
        elif "anchor_period_end" in snapshot_cols:
            snapshot_period_expr = "anchor_period_end AS report_period_end"
        else:
            snapshot_period_expr = "NULL AS report_period_end"

        snapshot = pd.read_sql_query(
            f"""
            SELECT
                as_of_date,
                cik,
                ticker,
                accession_number,
                filing_date,
                {snapshot_period_expr},
                {_scol("revenue", "NULL")},
                {_scol("net_income", "NULL")},
                {_scol("operating_cash_flow", "NULL")},
                {_scol("total_assets", "NULL")},
                {_scol("total_equity", "NULL")}
            FROM {snapshot_table}
            WHERE as_of_date = ?
            """,
            conn,
            params=[as_of_date],
        )
        _validate_required_columns(
            snapshot,
            ["as_of_date", "cik", "ticker", "accession_number", "filing_date", "report_period_end"],
            label="snapshot query",
        )
        if snapshot.empty:
            raise RuntimeError(
                f"No enhanced snapshot rows found for as_of_date={as_of_date}. "
                "Run build_sec_tier1_snapshot_enhanced.py for that date."
            )

        period_cols = _table_columns(conn, "sec_fundamental_period_t1")
        if not period_cols:
            raise RuntimeError("Missing sec_fundamental_period_t1 table.")
        period_cik_expr = sql_normalized_cik_expr("p.cik")
        snapshot_cik_expr = sql_normalized_cik_expr("s.cik")

        def _pcol(name: str, default_sql: str = "NULL", alias: str | None = None) -> str:
            out_alias = alias or name
            if name in period_cols:
                return f"p.{name} AS {out_alias}"
            return f"{default_sql} AS {out_alias}"

        def _porder(name: str, fallback_sql: str) -> str:
            if name in period_cols:
                return f"COALESCE(p.{name}, {fallback_sql})"
            return fallback_sql

        period = pd.read_sql_query(
            f"""
            WITH ranked_period AS (
                SELECT
                    {period_cik_expr} AS cik,
                    UPPER(COALESCE(p.ticker, '')) AS ticker,
                    COALESCE(p.accession_number, '') AS accession_number,
                    {_pcol("as_of_date", "''", "period_as_of_date")},
                    {_pcol("company_name", "''")},
                    {_pcol("revenue", "NULL", "period_revenue")},
                    {_pcol("net_income", "NULL", "period_net_income")},
                    {_pcol("total_assets", "NULL", "period_total_assets")},
                    {_pcol("total_equity", "NULL", "period_total_equity")},
                    {_pcol("ebitda", "NULL")},
                    {_pcol("free_cash_flow", "NULL")},
                    {_pcol("market_cap_proxy", "NULL")},
                    {_pcol("cash_and_equivalents", "NULL")},
                    {_pcol("short_term_investments", "NULL")},
                    {_pcol("short_term_borrowings", "NULL")},
                    {_pcol("current_portion_long_term_debt", "NULL")},
                    {_pcol("long_term_debt", "NULL")},
                    {_pcol("lease_liabilities", "NULL")},
                    {_pcol("revenue_yoy_growth", "NULL")},
                    {_pcol("eps_yoy_growth", "NULL")},
                    {_pcol("net_debt", "NULL")},
                    {_pcol("net_debt_to_assets", "NULL")},
                    {_pcol("accruals_ratio", "NULL")},
                    {_pcol("consensus_proxy_score", "NULL")},
                    {_pcol("recommendation_proxy", "''")},
                    {_pcol("earnings_release_8k_item202_30d", "NULL")},
                    {_pcol("insider_net_score", "NULL")},
                    {_pcol("r_and_d", "NULL")},
                    {_pcol("is_scoring_eligible", "1")},
                    {_pcol("is_metadata_only", "0")},
                    {_pcol("core_nonnull_count", "0")},
                    {_pcol("effective_missing_feature_count", "999")},
                    {_pcol("effective_any_feature_missing", "1")},
                    {_pcol("feature_status_json", "''")},
                    {_pcol("feature_applicability_json", "''")},
                    ROW_NUMBER() OVER (
                        PARTITION BY {period_cik_expr}, UPPER(COALESCE(p.ticker, '')), COALESCE(p.accession_number, '')
                        ORDER BY
                            {_porder("is_scoring_eligible", "1")} DESC,
                            {_porder("core_nonnull_count", "0")} DESC,
                            {_porder("is_metadata_only", "0")} ASC,
                            {_porder("effective_missing_feature_count", "999")} ASC,
                            {_porder("acceptance_datetime", "''")} DESC,
                            {_porder("filing_date", "''")} DESC,
                            {_porder("as_of_date", "''")} DESC
                    ) AS rn
                FROM sec_fundamental_period_t1 p
                JOIN {snapshot_table} s
                  ON {period_cik_expr} = {snapshot_cik_expr}
                 AND UPPER(COALESCE(p.ticker, '')) = UPPER(COALESCE(s.ticker, ''))
                 AND COALESCE(p.accession_number, '') = COALESCE(s.accession_number, '')
                 AND p.as_of_date = s.as_of_date
                WHERE s.as_of_date = ?
                  AND {period_cik_expr} IS NOT NULL
                  AND {snapshot_cik_expr} IS NOT NULL
            )
            SELECT * FROM ranked_period WHERE rn = 1
            """,
            conn,
            params=[as_of_date],
        )
        _validate_required_columns(
            period,
            [
                "cik",
                "ticker",
                "accession_number",
                "period_as_of_date",
                "company_name",
                "is_scoring_eligible",
                "core_nonnull_count",
                "effective_missing_feature_count",
                "effective_any_feature_missing",
                "feature_status_json",
                "feature_applicability_json",
            ],
            label="period query",
        )
    finally:
        conn.close()

    snapshot["ticker"] = snapshot["ticker"].fillna("").astype(str).str.upper()
    snapshot["cik"] = _normalize_cik_key(snapshot["cik"])
    period["ticker"] = period["ticker"].fillna("").astype(str).str.upper()
    period["cik"] = _normalize_cik_key(period["cik"])
    missing_snapshot_cik = int(snapshot["cik"].isna().sum())
    missing_period_cik = int(period["cik"].isna().sum())
    if missing_snapshot_cik > 0:
        logger.warning(
            "Snapshot export frame contains %d rows with missing CIK; period merge will remain incomplete for those rows.",
            missing_snapshot_cik,
        )
    if missing_period_cik > 0:
        logger.warning(
            "Period export frame contains %d rows with missing CIK; those rows cannot participate in CIK-keyed joins.",
            missing_period_cik,
        )
    period["is_scoring_eligible"] = pd.to_numeric(period["is_scoring_eligible"], errors="coerce").fillna(1)
    period["core_nonnull_count"] = pd.to_numeric(period["core_nonnull_count"], errors="coerce").fillna(0)
    period["is_metadata_only"] = pd.to_numeric(period["is_metadata_only"], errors="coerce").fillna(0)
    period["effective_missing_feature_count"] = pd.to_numeric(
        period["effective_missing_feature_count"], errors="coerce"
    ).fillna(999)
    period = period.drop(columns=["rn"], errors="ignore")

    merged = snapshot.merge(
        period,
        on=["cik", "ticker", "accession_number"],
        how="left",
    )
    _validate_row_count(len(snapshot), len(merged), label="snapshot-period merge", exact=True)
    _validate_required_columns(
        merged,
        [
            "as_of_date",
            "cik",
            "ticker",
            "accession_number",
            "period_as_of_date",
            "company_name",
            "is_scoring_eligible",
            "core_nonnull_count",
            "effective_missing_feature_count",
            "effective_any_feature_missing",
            "feature_status_json",
            "feature_applicability_json",
        ],
        label="merged export frame",
    )

    merged["company_name"] = merged.get("company_name", pd.Series("", index=merged.index)).fillna("").astype(str)

    merged["total_revenue"] = _coalesce_numeric(merged, ["period_revenue", "revenue"])
    merged["net_income_use"] = _coalesce_numeric(merged, ["net_income", "period_net_income"])
    merged["total_assets_use"] = _coalesce_numeric(merged, ["total_assets", "period_total_assets"])
    merged["total_equity_use"] = _coalesce_numeric(merged, ["total_equity", "period_total_equity"])

    merged["totalCash"] = _sum_present_components(merged, ["cash_and_equivalents", "short_term_investments"])
    merged["totalDebt"] = _sum_present_components(
        merged,
        [
            "short_term_borrowings",
            "current_portion_long_term_debt",
            "long_term_debt",
            "lease_liabilities",
        ],
    )
    merged["net_debt_to_ebitda"] = _cap_abs(
        _safe_divide(_to_num(merged, "net_debt"), _to_num(merged, "ebitda")),
        net_debt_ebitda_cap,
    )
    merged["fcf_yield"] = _safe_divide(_to_num(merged, "free_cash_flow"), _to_num(merged, "market_cap_proxy"))
    merged["fcf_margin"] = _safe_divide(_to_num(merged, "free_cash_flow"), _to_num(merged, "total_revenue"))

    reco_to_mean = {
        "STRONG_BUY": 1.0,
        "BUY": 2.0,
        "HOLD": 3.0,
        "HOLD_NO_DATA": np.nan,
        "REDUCE": 4.0,
        "SELL": 5.0,
    }
    merged["recommendation_proxy"] = (
        merged.get("recommendation_proxy", pd.Series("", index=merged.index))
        .fillna("HOLD_NO_DATA")
        .astype(str)
        .str.strip()
        .str.upper()
        .replace("", "HOLD_NO_DATA")
    )
    unknown_recommendations = sorted(
        {
            value
            for value in merged["recommendation_proxy"].dropna().astype(str).unique().tolist()
            if value and value not in reco_to_mean
        }
    )
    if unknown_recommendations:
        logger.warning(
            "Unmapped recommendation_proxy values encountered; recommendationMean will be NaN for: %s",
            unknown_recommendations,
        )
    merged["recommendationMean"] = merged["recommendation_proxy"].map(reco_to_mean)
    merged["recommendationKey"] = merged["recommendation_proxy"].str.lower()

    out = pd.DataFrame(
        {
            "ticker": merged["ticker"].fillna("").astype(str).str.upper(),
            "cik": merged["cik"],
            "company_name": merged["company_name"],
            "as_of_date": merged["as_of_date"],
            "totalRevenue": merged["total_revenue"],
            "ebitda": _to_num(merged, "ebitda"),
            "freeCashflow": _to_num(merged, "free_cash_flow"),
            "marketCap": _to_num(merged, "market_cap_proxy"),
            "totalCash": merged["totalCash"],
            "totalDebt": merged["totalDebt"],
            "revenueGrowth": _to_num(merged, "revenue_yoy_growth"),
            "earningsGrowth": _to_num(merged, "eps_yoy_growth"),
            "net_debt_to_ebitda": merged["net_debt_to_ebitda"],
            "accruals_ratio": _to_num(merged, "accruals_ratio"),
            "consensus_proxy_score": _to_num(merged, "consensus_proxy_score"),
            "recommendation_proxy": merged["recommendation_proxy"],
            "recommendationMean": merged["recommendationMean"],
            "recommendationKey": merged["recommendationKey"],
            "earnings_release_8k_item202_30d": _to_num(merged, "earnings_release_8k_item202_30d"),
            "insider_net_score": _to_num(merged, "insider_net_score"),
            "net_debt_to_assets": _to_num(merged, "net_debt_to_assets"),
            "fcf_yield": merged["fcf_yield"],
            "fcf_margin": merged["fcf_margin"],
            "net_income": merged["net_income_use"],
            "total_assets": merged["total_assets_use"],
            "total_equity": merged["total_equity_use"],
            "r_and_d": _to_num(merged, "r_and_d"),
            "research_and_development": _to_num(merged, "r_and_d"),
            "is_scoring_eligible": _to_num(merged, "is_scoring_eligible"),
            "is_metadata_only": _to_num(merged, "is_metadata_only"),
            "core_nonnull_count": _to_num(merged, "core_nonnull_count"),
            "effective_missing_feature_count": _to_num(merged, "effective_missing_feature_count"),
            "effective_any_feature_missing": _to_num(merged, "effective_any_feature_missing"),
            "feature_status_json": merged.get("feature_status_json", pd.Series("", index=merged.index)).fillna("").astype(str),
            "feature_applicability_json": merged.get("feature_applicability_json", pd.Series("", index=merged.index)).fillna("").astype(str),
        }
    ).sort_values(["consensus_proxy_score", "ticker"], ascending=[False, True])

    outputs_cfg = cfg_get(cfg, "outputs", default={})
    output_dir = Path(cfg_get(outputs_cfg, "report_output_dir", default="output")).expanduser()
    if not output_dir.is_absolute():
        output_dir = (Path(__file__).resolve().parent.parent / output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    out_csv = args.output_csv or (
        output_dir / str(cfg_get(outputs_cfg, "yahoo_compatible_csv", default="sec_fundamentals_yahoo_compatible.csv"))
    )
    out_json = args.output_json or (
        output_dir / str(cfg_get(outputs_cfg, "yahoo_compatible_json", default="sec_fundamentals_yahoo_compatible.json"))
    )
    out.to_csv(out_csv, index=False)

    payload: dict[str, dict[str, Any]] = {}
    for _, row in out.iterrows():
        ticker = str(row["ticker"]).strip().upper()
        if not ticker:
            continue
        row_payload: dict[str, Any] = {}
        for key, val in row.drop(labels=["ticker"]).to_dict().items():
            row_payload[str(key)] = None if pd.isna(val) else val
        payload[ticker] = row_payload
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)

    logger.info("Exported CSV: %s", out_csv)
    logger.info("Exported JSON: %s", out_json)
    logger.info("Rows exported: %d", len(out))


if __name__ == "__main__":
    main()
