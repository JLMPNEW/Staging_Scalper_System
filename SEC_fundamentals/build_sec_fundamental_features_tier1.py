#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import numpy as np
import pandas as pd
import yaml

from sec_fundamentals_config import (
    ANNUAL_FORMS,
    PERIODIC_FORMS,
    QUARTERLY_FORMS,
    SAFE_DIVIDE_MIN_ABS_DENOMINATOR,
    cfg_get,
    configure_pipeline_logging,
    load_sec_fundamentals_config,
    previous_or_same_business_day,
    safe_div_series,
    sql_normalized_cik_expr,
)

DEFAULT_DB_PATH = Path(r"C:\Users\josel\Documents\PROD\DB\sec_fundamentals.sqlite")
DEFAULT_CONFIG_PATH = Path(__file__).resolve().with_name("config_sec_fundamentals.yaml")
DEFAULT_RESOLVED_SNAPSHOT_TABLES = (
    "sec_fundamental_snapshot_filled_security_t1_resolved",
    "sec_fundamental_snapshot_filled_security_t1",
)
SQLITE_BUSY_TIMEOUT_MS = 30000
logger = logging.getLogger(__name__)

REQUIRED_CONSENSUS_WEIGHT_KEYS = (
    "sue",
    "earnings_acceleration",
    "revenue_acceleration",
    "insider_net_score",
    "eight_k_item202",
    "accruals_quality_bonus",
    "accruals_penalty",
    "leverage_penalty",
)
OPTIONAL_CONSENSUS_WEIGHT_KEYS = ("sur",)

DURATION_METRICS = {
    "revenue",
    "cogs",
    "gross_profit",
    "sga",
    "research_and_development",
    "depreciation_and_amortization",
    "operating_income",
    "interest_expense",
    "pretax_income",
    "tax_expense",
    "net_income",
    "ebitda",
    "eps_basic",
    "eps_diluted",
    "stock_based_compensation",
    "operating_cash_flow",
    "capex",
    "acquisitions",
    "cash_from_investing",
    "cash_from_financing",
    "dividends_paid",
    "share_repurchases",
    "share_issuance",
    "debt_issuance",
    "debt_repayment",
}
CORE_METRICS = ("revenue", "net_income", "operating_cash_flow", "total_assets", "total_equity")
METADATA_FACT_FIELDS = ("shares_outstanding_period_end", "public_float")


@dataclass(frozen=True)
class MetricRule:
    metric_name: str
    taxonomy: str
    tag: str
    priority: int
    period_type: str
    industry_aggregate: str = ""
    cik: str = ""
    ticker: str = ""


def cfg_section(cfg: dict[str, Any], key: str) -> dict[str, Any]:
    raw = cfg_get(cfg, key, default={})
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"Expected config section '{key}' to be a mapping, got {type(raw).__name__}.")
    return raw


def _connect_sqlite(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


METRIC_TO_PERIOD_COLUMN = {
    "revenue": "revenue",
    "cogs": "cogs",
    "gross_profit": "gross_profit",
    "sga": "sga",
    "research_and_development": "r_and_d",
    "depreciation_and_amortization": "depreciation_and_amortization",
    "operating_income": "operating_income",
    "interest_expense": "interest_expense",
    "pretax_income": "pretax_income",
    "tax_expense": "tax_expense",
    "net_income": "net_income",
    "ebitda": "ebitda",
    "eps_basic": "eps_basic",
    "eps_diluted": "eps_diluted",
    "weighted_avg_shares_basic": "weighted_avg_shares_basic",
    "weighted_avg_shares_diluted": "weighted_avg_shares_diluted",
    "stock_based_compensation": "stock_based_compensation",
    "impairment_charges": "impairment_charges",
    "restructuring_charges": "restructuring_charges",
    "cash_and_equivalents": "cash_and_equivalents",
    "short_term_investments": "short_term_investments",
    "accounts_receivable": "accounts_receivable",
    "inventory": "inventory",
    "prepaid_other_current_assets": "prepaid_other_current_assets",
    "total_current_assets": "total_current_assets",
    "ppe_net": "ppe_net",
    "goodwill": "goodwill",
    "intangibles": "intangibles",
    "total_assets": "total_assets",
    "accounts_payable": "accounts_payable",
    "accrued_liabilities": "accrued_liabilities",
    "contract_liabilities_current": "contract_liabilities_current",
    "contract_liabilities_noncurrent": "contract_liabilities_noncurrent",
    "short_term_borrowings": "short_term_borrowings",
    "current_portion_long_term_debt": "current_portion_long_term_debt",
    "long_term_debt": "long_term_debt",
    "lease_liabilities": "lease_liabilities",
    "total_liabilities": "total_liabilities",
    "total_equity": "total_equity",
    "shares_outstanding_period_end": "shares_outstanding_period_end",
    "public_float": "public_float",
    "operating_cash_flow": "operating_cash_flow",
    "capex": "capex",
    "acquisitions": "acquisitions",
    "cash_from_investing": "cash_from_investing",
    "cash_from_financing": "cash_from_financing",
    "dividends_paid": "dividends_paid",
    "share_repurchases": "share_repurchases",
    "share_issuance": "share_issuance",
    "debt_issuance": "debt_issuance",
    "debt_repayment": "debt_repayment",
    "taxes_payable": "taxes_payable",
    "taxes_receivable": "taxes_receivable",
    "allowance_credit_losses": "allowance_credit_losses",
}


def default_db_path() -> Path:
    return Path(os.getenv("SEC_FUNDAMENTALS_DB_PATH", str(DEFAULT_DB_PATH)))


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso_date(text: str | None) -> date | None:
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_iso_datetime(text: str | None) -> datetime | None:
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def resolve_as_of_timestamp(
    *,
    as_of_date: date,
    cli_value: str | None,
    cfg: dict[str, Any],
) -> datetime:
    if cli_value:
        dt = parse_iso_datetime(cli_value)
        if dt is None:
            raise ValueError(f"Invalid --as-of-timestamp: {cli_value}")
        return dt

    features_cfg = cfg_section(cfg, "features")
    cfg_value = cfg_get(features_cfg, "as_of_timestamp", default=None)
    if cfg_value:
        dt = parse_iso_datetime(str(cfg_value))
        if dt is None:
            raise ValueError(f"Invalid features.as_of_timestamp: {cfg_value}")
        return dt
    snap_cfg = cfg_section(cfg, "snapshot_enhanced")
    legacy_cutoff_text = cfg_get(features_cfg, "signal_cutoff_time_utc", default=None)
    if legacy_cutoff_text is not None and str(legacy_cutoff_text).strip():
        logger.warning(
            "features.signal_cutoff_time_utc is deprecated; use snapshot_enhanced.cutoff_time instead."
        )
    cutoff_text = str(cfg_get(snap_cfg, "cutoff_time", default=legacy_cutoff_text or "23:59:59")).strip()
    cutoff_timezone = str(cfg_get(snap_cfg, "cutoff_timezone", default="UTC")).strip() or "UTC"
    try:
        hh, mm, ss = [int(x) for x in cutoff_text.split(":")]
        local_cutoff = time(hh, mm, ss)
    except Exception as exc:
        raise ValueError(f"Invalid cutoff time '{cutoff_text}'. Expected HH:MM:SS.") from exc
    try:
        tzinfo = ZoneInfo(cutoff_timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Invalid snapshot_enhanced.cutoff_timezone: {cutoff_timezone}") from exc
    local_dt = datetime.combine(as_of_date, local_cutoff, tzinfo=tzinfo)
    return local_dt.astimezone(timezone.utc)


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table_name,),
    ).fetchone()
    return row is not None


def table_columns(conn: sqlite3.Connection, table_name: str) -> list[str]:
    if not table_exists(conn, table_name):
        return []
    return [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table_name})")]


def choose_existing_table(conn: sqlite3.Connection, candidates: Iterable[str]) -> str:
    for name in candidates:
        if table_exists(conn, name):
            return str(name)
    return ""


def load_tag_map(tag_map_path: Path) -> dict[str, Any]:
    if not tag_map_path.exists():
        raise FileNotFoundError(f"Tag map file not found: {tag_map_path}")
    with open(tag_map_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise RuntimeError("Tag map YAML root must be a dict.")
    return data


def candidate_priority(tag_map: dict[str, Any]) -> list[MetricRule]:
    metrics = cfg_get(tag_map, "canonical_metrics", default={})
    if not isinstance(metrics, dict):
        return []
    out: list[MetricRule] = []
    for metric, spec in metrics.items():
        if not isinstance(spec, dict):
            continue
        cands = spec.get("candidate_tags", [])
        if not isinstance(cands, list):
            continue
        period_type_raw = str(spec.get("period_type", "")).strip().lower()
        default_period_type = period_type_raw if period_type_raw in {"duration", "instant"} else (
            "duration" if str(metric) in DURATION_METRICS else "instant"
        )
        for idx, cand in enumerate(cands):
            if not (isinstance(cand, list) and len(cand) == 2):
                continue
            taxonomy = str(cand[0]).strip().lower()
            tag = str(cand[1]).strip()
            if not taxonomy or not tag:
                continue
            out.append(
                MetricRule(
                    metric_name=str(metric),
                    taxonomy=taxonomy,
                    tag=tag,
                    priority=idx,
                    period_type=default_period_type,
                )
            )
    return out


def load_metric_registry(
    tag_map: dict[str, Any],
    mapping_csv: Path | None,
) -> pd.DataFrame:
    base_rules = candidate_priority(tag_map)
    base = pd.DataFrame(
        [
            {
                "metric_name": r.metric_name,
                "taxonomy": r.taxonomy,
                "tag": r.tag,
                "priority": int(r.priority),
                "period_type": r.period_type,
                "industry_aggregate": r.industry_aggregate,
                "cik": r.cik,
                "ticker": r.ticker,
            }
            for r in base_rules
        ]
    )

    if mapping_csv is None or not mapping_csv.exists():
        if base.empty:
            raise RuntimeError("No tag mappings found in tier1_tag_map.")
        logger.info("Metric registry loaded from tier1_tag_map only; candidate-tag order defines fallback priority.")
        return base

    ext = pd.read_csv(mapping_csv)
    cols = {str(c).strip().lower(): c for c in ext.columns}
    metric_col = cols.get("metric_name") or cols.get("metric")
    tax_col = cols.get("taxonomy")
    tag_col = cols.get("concept_name") or cols.get("tag")
    if metric_col is None or tax_col is None or tag_col is None:
        raise RuntimeError(
            "metric mapping CSV must include metric_name/metric, taxonomy, and concept_name/tag."
        )

    priority_raw = (
        ext[cols.get("priority")] if cols.get("priority") is not None else pd.Series(999, index=ext.index)
    )
    ext_df = pd.DataFrame(
        {
            "metric_name": ext[metric_col].fillna("").astype(str).str.strip(),
            "taxonomy": ext[tax_col].fillna("").astype(str).str.strip().str.lower(),
            "tag": ext[tag_col].fillna("").astype(str).str.strip(),
            "priority": pd.to_numeric(priority_raw, errors="coerce").fillna(999).astype(int),
            "period_type": (
                ext[cols.get("period_type")].fillna("").astype(str).str.strip().str.lower()
                if cols.get("period_type") is not None
                else ""
            ),
            "industry_aggregate": (
                ext[cols.get("industry_aggregate")].fillna("").astype(str).str.strip()
                if cols.get("industry_aggregate") is not None
                else ""
            ),
            "cik": (
                ext[cols.get("cik")].fillna("").astype(str).str.replace(".0", "", regex=False).str.zfill(10)
                if cols.get("cik") is not None
                else ""
            ),
            "ticker": (
                ext[cols.get("ticker")].fillna("").astype(str).str.strip().str.upper()
                if cols.get("ticker") is not None
                else ""
            ),
        }
    )
    ext_df = ext_df[
        (ext_df["metric_name"] != "") & (ext_df["taxonomy"] != "") & (ext_df["tag"] != "")
    ].copy()
    merged = pd.concat([base, ext_df], ignore_index=True)
    if merged.empty:
        raise RuntimeError("Metric registry is empty after loading mappings.")

    merged["period_type"] = merged["period_type"].replace("", pd.NA)
    merged["period_type"] = merged.apply(
        lambda r: r["period_type"]
        if pd.notna(r["period_type"])
        else ("duration" if str(r["metric_name"]) in DURATION_METRICS else "instant"),
        axis=1,
    )
    merged = merged.sort_values(
        ["metric_name", "industry_aggregate", "cik", "ticker", "priority", "taxonomy", "tag"],
        ascending=[True, True, True, True, True, True, True],
    )
    merged = merged.drop_duplicates(
        subset=["metric_name", "taxonomy", "tag", "industry_aggregate", "cik", "ticker"],
        keep="first",
    ).reset_index(drop=True)
    logger.info(
        "Metric registry merged tier1_tag_map with mapping CSV %s; explicit CSV priority values take precedence on overlapping rules.",
        mapping_csv,
    )
    return merged


def build_metric_sql_filters(metric_registry: pd.DataFrame) -> tuple[str, list[Any]]:
    if metric_registry.empty:
        return "", []
    params: list[Any] = []
    chunks: list[str] = []
    for _, row in metric_registry[["taxonomy", "tag"]].drop_duplicates().iterrows():
        chunks.append("(lower(fr.taxonomy) = ? AND fr.tag = ?)")
        params.extend([str(row["taxonomy"]).lower(), str(row["tag"])])
    return " OR ".join(chunks), params


def rank_centered(series: pd.Series) -> pd.Series:
    s = pd.to_numeric(series, errors="coerce")
    mask = s.notna()
    out = pd.Series(0.0, index=series.index, dtype=float)
    if mask.sum() == 0:
        return out
    ranked = s[mask].rank(pct=True, method="average")
    out.loc[mask] = (ranked - 0.5) * 2.0
    return out


def num_series(df: pd.DataFrame, col: str) -> pd.Series:
    if col in df.columns:
        return pd.to_numeric(df[col], errors="coerce")
    return pd.Series(float("nan"), index=df.index, dtype="float64")


def sum_present_components(df: pd.DataFrame, cols: Iterable[str]) -> pd.Series:
    components = [num_series(df, col) for col in cols]
    if not components:
        return pd.Series(np.nan, index=df.index, dtype="float64")
    comp_df = pd.concat(components, axis=1)
    return comp_df.fillna(0.0).sum(axis=1).where(comp_df.notna().any(axis=1), np.nan)


def text_series(df: pd.DataFrame, col: str) -> pd.Series:
    if col in df.columns:
        return df[col].astype(str)
    return pd.Series("", index=df.index, dtype="object")


def valid_quarterly_ttm_mask(period_end_series: pd.Series) -> pd.Series:
    """
    Mark rows where trailing-4 observations represent contiguous quarterly cadence.
    """
    dates = pd.to_datetime(period_end_series, errors="coerce")
    if len(dates) < 4:
        return pd.Series(False, index=period_end_series.index, dtype=bool)
    gaps = dates.diff().dt.days
    valid_gap = gaps.between(50, 130, inclusive="both").fillna(False)
    rolling_valid = valid_gap.rolling(3, min_periods=3).min().fillna(0.0).astype(bool)
    return pd.Series(rolling_valid.to_numpy(), index=period_end_series.index, dtype=bool)


def rank_centered_by_group(
    values: pd.Series,
    group_primary: pd.Series | None,
    group_secondary: pd.Series | None = None,
    min_group_size: int = 8,
) -> pd.Series:
    """
    Rank-center within primary/secondary groups when coverage is sufficient.
    Falls back to global rank-centering for small/unknown groups.
    """
    base = rank_centered(values)
    out = pd.Series(float("nan"), index=values.index, dtype="float64")

    def _fill_from_group(group: pd.Series | None) -> None:
        nonlocal out
        if group is None:
            return
        g = group.fillna("").astype(str).str.strip()
        valid_group = (g != "") & (g.str.lower() != "nan")
        counts = g[valid_group].value_counts()
        eligible = set(counts[counts >= max(1, int(min_group_size))].index.tolist())
        if not eligible:
            return
        for key in eligible:
            idx = (g == key) & valid_group & out.isna()
            if not idx.any():
                continue
            out.loc[idx] = rank_centered(values.loc[idx])

    _fill_from_group(group_primary)
    _fill_from_group(group_secondary)
    return out.fillna(base)


def load_issuer_profile_mapping(
    cfg: dict[str, Any],
    cfg_path: Path,
) -> pd.DataFrame:
    snap_cfg = cfg_get(cfg, "snapshot_enhanced", default={})
    raw = cfg_get(snap_cfg, "issuer_profile_csv", default=None)
    if not raw:
        return pd.DataFrame(columns=["ticker", "sector", "industry", "industry_aggregate"])
    path = Path(str(raw))
    if not path.is_absolute():
        path = (cfg_path.parent.parent / path).resolve()
    if not path.exists():
        return pd.DataFrame(columns=["ticker", "sector", "industry", "industry_aggregate"])

    df = pd.read_csv(path)
    cols = {str(c).strip().lower(): c for c in df.columns}
    ticker_col = cols.get("ticker") or cols.get("matchedticker") or cols.get("symbol")
    sector_col = cols.get("sector")
    industry_col = cols.get("industry")
    indagg_col = cols.get("industry_aggregate")
    if ticker_col is None:
        return pd.DataFrame(columns=["ticker", "sector", "industry", "industry_aggregate"])
    if sector_col is None and industry_col is None and indagg_col is None:
        return pd.DataFrame(columns=["ticker", "sector", "industry", "industry_aggregate"])

    out = pd.DataFrame({"ticker": df[ticker_col].fillna("").astype(str).str.strip().str.upper()})
    out["sector"] = (
        df[sector_col].fillna("").astype(str).str.strip()
        if sector_col is not None
        else ""
    )
    out["industry"] = (
        df[industry_col].fillna("").astype(str).str.strip()
        if industry_col is not None
        else ""
    )
    if indagg_col is not None:
        out["industry_aggregate"] = df[indagg_col].fillna("").astype(str).str.strip()
    else:
        out["industry_aggregate"] = out["industry"]
    out = out[out["ticker"] != ""].drop_duplicates(subset=["ticker"], keep="last")
    return out.reset_index(drop=True)


def resolve_consensus_weights(weights_raw: dict[str, Any]) -> dict[str, float]:
    weights_raw = weights_raw if isinstance(weights_raw, dict) else {}
    missing = [k for k in REQUIRED_CONSENSUS_WEIGHT_KEYS if k not in weights_raw]
    allowed = set(REQUIRED_CONSENSUS_WEIGHT_KEYS) | set(OPTIONAL_CONSENSUS_WEIGHT_KEYS)
    extras = [k for k in weights_raw.keys() if k not in allowed]
    if missing or extras:
        raise ValueError(
            "features.consensus_proxy_weights must contain exactly these keys: "
            f"{', '.join(REQUIRED_CONSENSUS_WEIGHT_KEYS)} "
            f"(optional: {', '.join(OPTIONAL_CONSENSUS_WEIGHT_KEYS)}). "
            f"Missing={missing or '[]'} Extra={extras or '[]'}"
        )
    weights = {key: float(weights_raw[key]) for key in REQUIRED_CONSENSUS_WEIGHT_KEYS}
    for key in OPTIONAL_CONSENSUS_WEIGHT_KEYS:
        weights[key] = float(weights_raw[key]) if key in weights_raw else 0.0
    total = sum(weights.values())
    if abs(total - 1.0) > 0.01:
        raise ValueError(
            "features.consensus_proxy_weights must sum to 1.0 "
            f"(tolerance 0.01). Got {total:.4f}"
        )
    return weights


def ensure_required_tables(conn: sqlite3.Connection) -> None:
    required = {
        "sec_xbrl_facts_raw",
        "sec_filing_index",
        "sec_entity_universe",
        "sec_fundamental_period_t1",
        "sec_cutover_tolerance_result",
    }
    existing = {
        row[0].lower()
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    missing = sorted(name for name in required if name not in existing)
    if missing:
        raise RuntimeError(
            "Fundamentals DB schema missing required tables: "
            + ", ".join(missing)
            + ". Run fundamental_data/init_sec_fundamentals_db.py first."
        )


def ensure_period_output_columns(conn: sqlite3.Connection) -> None:
    existing = set(table_columns(conn, "sec_fundamental_period_t1"))
    desired_sql = {
        "insider_data_present": "INTEGER",
        "feature_status_json": "TEXT",
        "feature_applicability_json": "TEXT",
        "effective_missing_feature_count": "INTEGER",
        "effective_any_feature_missing": "INTEGER",
        "core_nonnull_count": "INTEGER",
        "metadata_marker_count": "INTEGER",
        "is_metadata_only": "INTEGER",
        "is_scoring_eligible": "INTEGER",
        "metadata_only_reason": "TEXT",
    }
    for col, sql_type in desired_sql.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE sec_fundamental_period_t1 ADD COLUMN {col} {sql_type}")


def ensure_metadata_period_table(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, "sec_fundamental_period_metadata_t1"):
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sec_fundamental_period_metadata_t1 (
                period_sk                            INTEGER PRIMARY KEY AUTOINCREMENT,
                cik                                  TEXT NOT NULL,
                ticker                               TEXT,
                company_name                         TEXT,
                accession_number                     TEXT,
                form_type                            TEXT,
                report_period_end                    TEXT NOT NULL,
                fiscal_year                          INTEGER,
                fiscal_period                        TEXT,
                filing_date                          TEXT,
                acceptance_datetime                  TEXT,
                revenue                              REAL,
                cogs                                 REAL,
                gross_profit                         REAL,
                sga                                  REAL,
                r_and_d                              REAL,
                depreciation_and_amortization        REAL,
                operating_income                     REAL,
                interest_expense                     REAL,
                pretax_income                        REAL,
                tax_expense                          REAL,
                net_income                           REAL,
                ebitda                               REAL,
                eps_basic                            REAL,
                eps_diluted                          REAL,
                weighted_avg_shares_basic            REAL,
                weighted_avg_shares_diluted          REAL,
                stock_based_compensation             REAL,
                impairment_charges                   REAL,
                restructuring_charges                REAL,
                cash_and_equivalents                 REAL,
                short_term_investments               REAL,
                accounts_receivable                  REAL,
                inventory                            REAL,
                prepaid_other_current_assets         REAL,
                total_current_assets                 REAL,
                ppe_net                              REAL,
                goodwill                             REAL,
                intangibles                          REAL,
                total_assets                         REAL,
                accounts_payable                     REAL,
                accrued_liabilities                  REAL,
                contract_liabilities_current         REAL,
                contract_liabilities_noncurrent      REAL,
                short_term_borrowings                REAL,
                current_portion_long_term_debt       REAL,
                long_term_debt                       REAL,
                lease_liabilities                    REAL,
                total_liabilities                    REAL,
                total_equity                         REAL,
                shares_outstanding_period_end        REAL,
                public_float                         REAL,
                operating_cash_flow                  REAL,
                capex                                REAL,
                acquisitions                         REAL,
                cash_from_investing                  REAL,
                cash_from_financing                  REAL,
                dividends_paid                       REAL,
                share_repurchases                    REAL,
                share_issuance                       REAL,
                debt_issuance                        REAL,
                debt_repayment                       REAL,
                free_cash_flow                       REAL,
                taxes_payable                        REAL,
                taxes_receivable                     REAL,
                allowance_credit_losses              REAL,
                accruals_ratio                       REAL,
                gross_margin                         REAL,
                operating_margin                     REAL,
                cfo_to_net_income                    REAL,
                net_debt                             REAL,
                net_debt_to_assets                   REAL,
                sbc_to_revenue                       REAL,
                dilution_rate                        REAL,
                market_cap_proxy                     REAL,
                revenue_yoy_growth                   REAL,
                eps_yoy_growth                       REAL,
                revenue_acceleration                 REAL,
                earnings_acceleration                REAL,
                sue                                  REAL,
                earnings_release_8k_item202_30d      INTEGER,
                insider_buy_score_20bd               REAL,
                insider_sell_score_20bd              REAL,
                insider_net_score                    REAL,
                insider_data_present                 INTEGER,
                consensus_proxy_score                REAL,
                recommendation_proxy                 TEXT,
                data_quality_flags_json              TEXT,
                feature_status_json                  TEXT,
                feature_applicability_json           TEXT,
                effective_missing_feature_count      INTEGER,
                effective_any_feature_missing        INTEGER,
                core_nonnull_count                   INTEGER,
                metadata_marker_count                INTEGER,
                is_metadata_only                     INTEGER,
                is_scoring_eligible                  INTEGER,
                metadata_only_reason                 TEXT,
                as_of_date                           TEXT NOT NULL,
                updated_at_utc                       TEXT NOT NULL,
                UNIQUE (cik, report_period_end, form_type, accession_number, as_of_date)
            )
            """
        )
    existing = set(table_columns(conn, "sec_fundamental_period_metadata_t1"))
    desired_sql = {
        "insider_data_present": "INTEGER",
        "feature_status_json": "TEXT",
        "feature_applicability_json": "TEXT",
        "effective_missing_feature_count": "INTEGER",
        "effective_any_feature_missing": "INTEGER",
        "core_nonnull_count": "INTEGER",
        "metadata_marker_count": "INTEGER",
        "is_metadata_only": "INTEGER",
        "is_scoring_eligible": "INTEGER",
        "metadata_only_reason": "TEXT",
    }
    for col, sql_type in desired_sql.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE sec_fundamental_period_metadata_t1 ADD COLUMN {col} {sql_type}")


def annotate_period_rows(
    period: pd.DataFrame,
    *,
    min_core_metrics: int = 1,
) -> pd.DataFrame:
    if period.empty:
        return period
    out = period.copy()
    present_core = [c for c in CORE_METRICS if c in out.columns]
    out["core_nonnull_count"] = (
        out[present_core].notna().sum(axis=1).astype("int64") if present_core else 0
    )
    present_meta = [c for c in METADATA_FACT_FIELDS if c in out.columns]
    out["metadata_marker_count"] = (
        out[present_meta].notna().sum(axis=1).astype("int64") if present_meta else 0
    )
    out["is_metadata_only"] = (out["core_nonnull_count"] == 0).astype("int64")
    out["is_scoring_eligible"] = (
        out["core_nonnull_count"] >= max(1, int(min_core_metrics))
    ).astype("int64")
    out["metadata_only_reason"] = ""
    out.loc[(out["is_metadata_only"] == 1) & (out["metadata_marker_count"] > 0), "metadata_only_reason"] = (
        "metadata_fact_only"
    )
    out.loc[(out["is_metadata_only"] == 1) & (out["metadata_marker_count"] == 0), "metadata_only_reason"] = (
        "no_core_metrics"
    )
    return out


def attach_metadata_facts_to_scoring_rows(
    scoring: pd.DataFrame,
    metadata_only: pd.DataFrame,
) -> pd.DataFrame:
    if scoring.empty or metadata_only.empty:
        return scoring
    carry_cols = [c for c in METADATA_FACT_FIELDS if c in metadata_only.columns]
    if not carry_cols:
        return scoring
    join_keys = ["cik", "accession_number"]
    meta_best = (
        metadata_only.sort_values(
            ["cik", "accession_number", "metadata_marker_count", "acceptance_datetime_dt", "filing_date_dt"],
            ascending=[True, True, False, False, False],
        )
        .drop_duplicates(subset=join_keys, keep="first")[join_keys + carry_cols]
    )
    out = scoring.merge(meta_best, on=join_keys, how="left", suffixes=("", "_meta"))
    for col in carry_cols:
        meta_col = f"{col}_meta"
        if meta_col in out.columns:
            out[col] = pd.to_numeric(out.get(col), errors="coerce").combine_first(
                pd.to_numeric(out[meta_col], errors="coerce")
            )
            out = out.drop(columns=[meta_col], errors="ignore")
    return out


def _legacy_proxy_table_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sec_signal_proxy_snapshot_t1' LIMIT 1"
    ).fetchone()
    return row is not None


def load_resolved_security_snapshot(
    conn: sqlite3.Connection,
    *,
    as_of_date: date,
    preferred_tables: tuple[str, ...],
) -> tuple[str, pd.DataFrame]:
    table_name = choose_existing_table(conn, preferred_tables)
    if not table_name:
        return "", pd.DataFrame()
    cols = set(table_columns(conn, table_name))
    needed = {
        "as_of_date",
        "ticker",
        "cik",
        "accession_number",
        "report_period_end",
        "filing_date",
        "form_type",
        "revenue",
        "net_income",
        "operating_cash_flow",
        "total_assets",
        "total_equity",
    }
    if not needed.issubset(cols):
        return "", pd.DataFrame()

    base = pd.read_sql_query(
        f"SELECT * FROM {table_name} WHERE as_of_date = ?",
        conn,
        params=[as_of_date.isoformat()],
    )
    if base.empty:
        return table_name, base
    base["ticker"] = base["ticker"].fillna("").astype(str).str.upper().str.strip()
    base["cik"] = base["cik"].fillna("").astype(str).str.replace(".0", "", regex=False).str.zfill(10)
    for col in ("sector", "industry", "industry_aggregate"):
        if col not in base.columns:
            base[col] = ""
        base[col] = base[col].fillna("").astype(str).str.strip()
    return table_name, base


def merge_base_snapshot(
    *,
    latest_entity: pd.DataFrame,
    security_base: pd.DataFrame,
    issuer_profile: pd.DataFrame,
) -> pd.DataFrame:
    if latest_entity.empty:
        return pd.DataFrame()
    if security_base.empty:
        out = latest_entity.copy()
        if "ticker" not in out.columns:
            out["ticker"] = ""
        return out

    base = security_base.copy()
    base = base.drop_duplicates(subset=["ticker", "cik", "accession_number"], keep="last")
    for col in ("revenue", "net_income", "operating_cash_flow", "total_assets", "total_equity"):
        if col in base.columns:
            base[col] = pd.to_numeric(base[col], errors="coerce")

    # Primary join on cik+accession to avoid stale cross-accession propagation.
    entity = latest_entity.copy()
    entity = entity.drop_duplicates(subset=["cik"], keep="first")
    entity_cols = [c for c in entity.columns if c not in {"ticker", "company_name", "sector", "industry", "industry_aggregate"}]
    merged = base.merge(
        entity[entity_cols],
        on=["cik", "accession_number"],
        how="left",
        suffixes=("", "_entity"),
    )

    # Fallback by cik if accession-level merge missed.
    fallback = entity.drop(columns=["accession_number"], errors="ignore")
    merged = merged.merge(
        fallback.add_suffix("_cik"),
        left_on="cik",
        right_on="cik_cik",
        how="left",
    )
    for col in ["revenue_yoy_growth", "eps_yoy_growth", "sue", "sur", "earnings_acceleration", "revenue_acceleration", "net_debt_to_assets", "accruals_ratio", "cfo_to_net_income", "gross_margin", "operating_margin", "insider_buy_score_20bd", "insider_sell_score_20bd", "insider_net_score", "insider_data_present", "earnings_release_8k_item202_30d", "earnings_release_8k_item202_days_since", "consensus_proxy_score", "recommendation_proxy", "report_period_end", "filing_date", "form_type", "fiscal_year", "fiscal_period", "ttm_revenue", "ttm_net_income", "ttm_operating_cash_flow", "ttm_ebitda", "ebitda", "market_cap_proxy", "free_cash_flow"]:
        alt_col = f"{col}_cik"
        if col not in merged.columns:
            merged[col] = pd.NA
        if alt_col in merged.columns:
            merged[col] = merged[col].combine_first(merged[alt_col])
    drop_cik_cols = [c for c in merged.columns if c.endswith("_cik")]
    merged = merged.drop(columns=drop_cik_cols, errors="ignore")

    if not issuer_profile.empty:
        prof = issuer_profile.drop_duplicates(subset=["ticker"], keep="last")
        merged = merged.merge(prof, on="ticker", how="left", suffixes=("", "_profile"))
        for col in ("sector", "industry", "industry_aggregate"):
            pcol = f"{col}_profile"
            if pcol in merged.columns:
                merged[col] = merged[col].replace("", pd.NA).fillna(merged[pcol])
                merged = merged.drop(columns=[pcol], errors="ignore")

    for col in ("sector", "industry", "industry_aggregate"):
        if col not in merged.columns:
            merged[col] = ""
        merged[col] = merged[col].fillna("").astype(str)
    return merged


def load_universe(conn: sqlite3.Connection) -> pd.DataFrame:
    cik_expr = sql_normalized_cik_expr("cik")
    return pd.read_sql_query(
        f"""
        SELECT
            {cik_expr} AS cik,
            UPPER(COALESCE(ticker, '')) AS ticker,
            COALESCE(company_name, '') AS company_name
        FROM sec_entity_universe
        WHERE active = 1
          AND {cik_expr} IS NOT NULL
        """,
        conn,
    )


def load_selected_facts(
    conn: sqlite3.Connection,
    *,
    include_forms: list[str],
    metric_registry: pd.DataFrame,
    as_of_timestamp: datetime,
    publication_buffer_minutes: int,
    cik_profile_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    if metric_registry.empty:
        return pd.DataFrame()

    registry = metric_registry.copy()
    registry["taxonomy"] = registry["taxonomy"].fillna("").astype(str).str.strip().str.lower()
    registry["tag"] = registry["tag"].fillna("").astype(str).str.strip()
    registry["metric_name"] = registry["metric_name"].fillna("").astype(str).str.strip()
    registry["priority"] = pd.to_numeric(registry["priority"], errors="coerce").fillna(999999).astype(int)
    registry["period_type"] = registry["period_type"].fillna("").astype(str).str.strip().str.lower()
    registry["period_type"] = registry["period_type"].where(
        registry["period_type"].isin({"duration", "instant"}),
        registry["metric_name"].map(lambda m: "duration" if str(m) in DURATION_METRICS else "instant"),
    )
    registry["industry_aggregate"] = registry["industry_aggregate"].fillna("").astype(str).str.strip()
    raw_cik = (
        registry["cik"]
        .fillna("")
        .astype(str)
        .str.strip()
        .str.replace(".0", "", regex=False)
    )
    cik_digits = raw_cik.str.replace(r"\D", "", regex=True)
    cik_blank = cik_digits.eq("")
    cik_all_zero = cik_digits.str.fullmatch(r"0+").fillna(False)
    registry["cik"] = cik_digits.where(~cik_blank, "").str.zfill(10)
    registry.loc[cik_blank, "cik"] = ""
    # Drop explicit all-zero CIK rows; they are invalid and should not become global rules.
    registry = registry[~(cik_all_zero & ~cik_blank)].copy()
    registry = registry[(registry["taxonomy"] != "") & (registry["tag"] != "") & (registry["metric_name"] != "")]
    if registry.empty:
        return pd.DataFrame()
    registry = registry.sort_values(
        ["taxonomy", "tag", "industry_aggregate", "cik", "priority"],
        ascending=[True, True, True, True, True],
    ).drop_duplicates(
        subset=["taxonomy", "tag", "metric_name", "industry_aggregate", "cik"],
        keep="first",
    )

    temp_rules = "_tmp_metric_rules_t1"
    temp_profile = "_tmp_cik_profile_t1"
    conn.execute(f"DROP TABLE IF EXISTS temp.{temp_rules}")
    conn.execute(f"DROP TABLE IF EXISTS temp.{temp_profile}")
    conn.execute(
        f"""
        CREATE TEMP TABLE {temp_rules}(
            taxonomy TEXT NOT NULL,
            tag TEXT NOT NULL,
            metric_name TEXT NOT NULL,
            priority INTEGER NOT NULL,
            period_type TEXT NOT NULL,
            industry_aggregate TEXT NOT NULL,
            cik TEXT NOT NULL
        )
        """
    )
    conn.executemany(
        f"INSERT INTO {temp_rules}(taxonomy, tag, metric_name, priority, period_type, industry_aggregate, cik) VALUES (?, ?, ?, ?, ?, ?, ?)",
        registry[["taxonomy", "tag", "metric_name", "priority", "period_type", "industry_aggregate", "cik"]].itertuples(index=False, name=None),
    )
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{temp_rules}_tt ON {temp_rules}(taxonomy, tag)")
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{temp_rules}_scope ON {temp_rules}(cik, industry_aggregate)")

    if cik_profile_df is not None and not cik_profile_df.empty:
        prof = cik_profile_df.copy()
        prof["cik"] = prof["cik"].fillna("").astype(str).str.replace(".0", "", regex=False).str.zfill(10)
        if "industry_aggregate" not in prof.columns:
            prof["industry_aggregate"] = ""
        prof["industry_aggregate"] = prof["industry_aggregate"].fillna("").astype(str).str.strip()
        prof = prof[prof["cik"] != ""].drop_duplicates(subset=["cik"], keep="last")
        conn.execute(f"CREATE TEMP TABLE {temp_profile}(cik TEXT PRIMARY KEY, industry_aggregate TEXT NOT NULL)")
        conn.executemany(
            f"INSERT INTO {temp_profile}(cik, industry_aggregate) VALUES (?, ?)",
            prof[["cik", "industry_aggregate"]].itertuples(index=False, name=None),
        )
    else:
        conn.execute(f"CREATE TEMP TABLE {temp_profile}(cik TEXT PRIMARY KEY, industry_aggregate TEXT)")
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{temp_profile}_cik ON {temp_profile}(cik)")

    form_placeholders = ",".join("?" for _ in include_forms) if include_forms else "''"
    eff_ts = as_of_timestamp - timedelta(minutes=max(0, int(publication_buffer_minutes)))
    eff_ts_text = eff_ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    eff_date_text = eff_ts.date().isoformat()
    params: list[Any] = []
    params.extend(include_forms)
    params.extend([eff_ts_text, eff_date_text])

    periodic_forms_sql = ", ".join(f"'{f.upper()}'" for f in sorted(PERIODIC_FORMS))
    fr_cik_expr = sql_normalized_cik_expr("fr.cik")
    fi_cik_expr = sql_normalized_cik_expr("fi.cik")
    sql = f"""
        WITH base AS (
            SELECT
                {fr_cik_expr} AS cik,
                COALESCE(fr.accession_number, fi.accession_number, '') AS accession_number,
                fr.taxonomy AS taxonomy,
                fr.tag AS tag,
                fr.value_num,
                fr.value_text,
                COALESCE(fr.form_type, fi.form_type, '') AS form_type,
                fr.fiscal_year,
                COALESCE(fr.fiscal_period, '') AS fiscal_period,
                COALESCE(fr.period_start, '') AS period_start,
                COALESCE(fr.period_end, '') AS period_end,
                COALESCE(fi.filing_date, fr.filed_date, '') AS filing_date,
                COALESCE(fi.acceptance_datetime, '') AS acceptance_datetime,
                CASE
                    WHEN COALESCE(fi.report_period_end, fr.report_period_end, '') <> '' THEN
                        COALESCE(fi.report_period_end, fr.report_period_end, '')
                    WHEN UPPER(COALESCE(fr.form_type, fi.form_type, '')) IN ({periodic_forms_sql}) THEN
                        COALESCE(fr.period_end, '')
                    ELSE ''
                END AS report_period_end,
                COALESCE(fr.is_amendment, fi.is_amendment, 0) AS is_amendment,
                mr.metric_name,
                mr.priority,
                mr.period_type,
                (CASE WHEN COALESCE(mr.cik, '') <> '' THEN 2 ELSE 0 END
                 + CASE WHEN COALESCE(mr.industry_aggregate, '') <> '' THEN 1 ELSE 0 END) AS rule_specificity,
                COALESCE(cp.industry_aggregate, '') AS industry_aggregate,
                CASE
                    WHEN COALESCE(fr.period_start, '') <> '' AND COALESCE(fr.period_end, '') <> ''
                    THEN CAST(julianday(fr.period_end) - julianday(fr.period_start) + 1 AS REAL)
                    ELSE NULL
                END AS duration_days
            FROM sec_xbrl_facts_raw fr
            LEFT JOIN sec_filing_index fi
              ON fr.accession_number = fi.accession_number
             AND {fr_cik_expr} = {fi_cik_expr}
            INNER JOIN {temp_rules} mr
              ON fr.taxonomy = mr.taxonomy
             AND fr.tag = mr.tag
            LEFT JOIN {temp_profile} cp
              ON cp.cik = {fr_cik_expr}
            WHERE COALESCE(fr.form_type, fi.form_type, '') IN ({form_placeholders})
              AND {fr_cik_expr} IS NOT NULL
              AND (
                    CASE
                        WHEN COALESCE(fi.report_period_end, fr.report_period_end, '') <> '' THEN
                            COALESCE(fi.report_period_end, fr.report_period_end, '')
                        WHEN UPPER(COALESCE(fr.form_type, fi.form_type, '')) IN ({periodic_forms_sql}) THEN
                            COALESCE(fr.period_end, '')
                        ELSE ''
                    END
                  ) <> ''
              AND (
                    (COALESCE(fi.acceptance_datetime, '') <> '' AND COALESCE(fi.acceptance_datetime, '') <= ?)
                    OR
                    (COALESCE(fi.acceptance_datetime, '') = '' AND COALESCE(fi.filing_date, fr.filed_date, '') <> '' AND COALESCE(fi.filing_date, fr.filed_date, '') <= ?)
                  )
              AND (COALESCE(mr.cik, '') = '' OR mr.cik = {fr_cik_expr})
              AND (
                    COALESCE(mr.industry_aggregate, '') = ''
                    OR mr.industry_aggregate = COALESCE(cp.industry_aggregate, '')
                  )
        ),
        scored AS (
            SELECT
                b.*,
                CASE
                    WHEN b.period_type = 'instant' THEN 0
                    WHEN UPPER(b.form_type) IN ('10-Q', '10-Q/A', '10-QT', '10-QT/A')
                         AND UPPER(b.fiscal_period) = 'Q1'
                         AND b.duration_days BETWEEN 75 AND 105 THEN 0
                    WHEN UPPER(b.form_type) IN ('10-Q', '10-Q/A', '10-QT', '10-QT/A')
                         AND UPPER(b.fiscal_period) = 'Q1' THEN 1
                    WHEN UPPER(b.form_type) IN ('10-Q', '10-Q/A', '10-QT', '10-QT/A')
                         AND UPPER(b.fiscal_period) IN ('Q2', 'Q3')
                         AND b.duration_days BETWEEN 75 AND 105 THEN 0
                    WHEN UPPER(b.form_type) IN ('10-Q', '10-Q/A', '10-QT', '10-QT/A')
                         AND UPPER(b.fiscal_period) IN ('Q2', 'Q3')
                         AND b.duration_days BETWEEN 150 AND 310 THEN 1
                    WHEN UPPER(b.form_type) IN ('10-Q', '10-Q/A', '10-QT', '10-QT/A')
                         AND UPPER(b.fiscal_period) IN ('Q2', 'Q3') THEN 2
                    WHEN UPPER(b.form_type) IN ('10-K', '10-K/A', '20-F', '20-F/A', '40-F', '40-F/A')
                         AND b.duration_days BETWEEN 300 AND 380 THEN 0
                    WHEN UPPER(b.form_type) IN ('10-K', '10-K/A', '20-F', '20-F/A', '40-F', '40-F/A') THEN 1
                    ELSE 3
                END AS duration_pref
            FROM base b
        ),
        ranked AS (
            SELECT
                s.*,
                ROW_NUMBER() OVER (
                    PARTITION BY s.cik, s.report_period_end, s.metric_name
                    ORDER BY
                        s.rule_specificity DESC,
                        s.priority ASC,
                        s.duration_pref ASC,
                        s.acceptance_datetime DESC,
                        s.filing_date DESC,
                        s.is_amendment DESC
                ) AS rn
            FROM scored s
        )
        SELECT
            cik,
            accession_number,
            value_num,
            value_text,
            form_type,
            fiscal_year,
            fiscal_period,
            period_start,
            period_end,
            filing_date,
            acceptance_datetime,
            report_period_end,
            is_amendment,
            metric_name,
            priority,
            period_type,
            rule_specificity,
            duration_days
        FROM ranked
        WHERE rn = 1
    """
    facts = pd.read_sql_query(sql, conn, params=params)
    conn.execute(f"DROP TABLE IF EXISTS temp.{temp_rules}")
    conn.execute(f"DROP TABLE IF EXISTS temp.{temp_profile}")
    if facts.empty:
        return facts

    facts["value_num"] = pd.to_numeric(facts["value_num"], errors="coerce")
    missing_num = facts["value_num"].isna() & facts["value_text"].notna()
    if missing_num.any():
        facts.loc[missing_num, "value_num"] = pd.to_numeric(
            facts.loc[missing_num, "value_text"].str.replace(",", "", regex=False),
            errors="coerce",
        )
    facts["duration_days"] = pd.to_numeric(facts["duration_days"], errors="coerce")
    facts["report_period_end_dt"] = pd.to_datetime(facts["report_period_end"], errors="coerce")
    facts["filing_date_dt"] = pd.to_datetime(facts["filing_date"], errors="coerce")
    facts["acceptance_datetime_dt"] = pd.to_datetime(facts["acceptance_datetime"], errors="coerce", utc=True)
    facts["period_end_dt"] = pd.to_datetime(facts["period_end"], errors="coerce")
    facts = facts[facts["report_period_end_dt"].notna()].copy()
    facts["form_type"] = facts["form_type"].fillna("").astype(str).str.upper()
    facts["fiscal_period"] = facts["fiscal_period"].fillna("").astype(str).str.upper()
    facts["period_type"] = facts["period_type"].fillna("").astype(str).str.lower()
    default_period = facts["metric_name"].map(lambda m: "duration" if str(m) in DURATION_METRICS else "instant")
    facts["period_type"] = facts["period_type"].where(facts["period_type"].isin({"duration", "instant"}), default_period)
    facts["priority"] = pd.to_numeric(facts["priority"], errors="coerce").fillna(999999).astype(int)
    facts["rule_specificity"] = pd.to_numeric(facts["rule_specificity"], errors="coerce").fillna(0).astype(int)
    facts = facts.drop(columns=["value_text"], errors="ignore")
    return facts.reset_index(drop=True)


def choose_fact_context(group: pd.DataFrame) -> pd.DataFrame:
    g = group.copy()
    if g.empty:
        return g

    period_type = str(g["period_type"].iloc[0]).lower()
    form_type = str(g["form_type"].iloc[0]).upper()
    fiscal_period = str(g["fiscal_period"].iloc[0]).upper()
    duration = pd.to_numeric(g.get("duration_days", pd.Series(float("nan"), index=g.index)), errors="coerce")
    g["duration_pref"] = 9999

    if period_type == "instant":
        g["duration_pref"] = 0
    elif form_type in QUARTERLY_FORMS:
        q_current = duration.between(75, 105, inclusive="both")
        q_ytd = duration.between(150, 310, inclusive="both")
        if fiscal_period == "Q1":
            g.loc[q_current, "duration_pref"] = 0
            g.loc[g["duration_pref"] == 9999, "duration_pref"] = 1
        elif fiscal_period in {"Q2", "Q3"}:
            g.loc[q_current, "duration_pref"] = 0
            g.loc[q_ytd, "duration_pref"] = g.loc[q_ytd, "duration_pref"].clip(upper=1)
            g.loc[g["duration_pref"] == 9999, "duration_pref"] = 2
        else:
            g["duration_pref"] = 2
    elif form_type in ANNUAL_FORMS:
        annual = duration.between(300, 380, inclusive="both")
        g.loc[annual, "duration_pref"] = 0
        g.loc[g["duration_pref"] == 9999, "duration_pref"] = 1

    g = g.sort_values(
        [
            "rule_specificity",
            "priority",
            "duration_pref",
            "acceptance_datetime_dt",
            "filing_date_dt",
            "is_amendment",
        ],
        ascending=[False, True, True, False, False, False],
    )
    return g.head(1).drop(columns=["duration_pref"], errors="ignore")


def build_metric_period_frame(facts: pd.DataFrame) -> pd.DataFrame:
    if facts.empty:
        return pd.DataFrame()
    # load_selected_facts already enforces one best row per (cik, report_period_end, metric_name).
    selected = facts.copy()

    pivot = (
        selected.pivot_table(
            index=[
                "cik",
                "report_period_end",
            ],
            columns="metric_name",
            values="value_num",
            aggfunc="first",
        )
        .reset_index()
        .rename_axis(None, axis=1)
    )

    meta = (
        selected.sort_values(
            ["cik", "report_period_end_dt", "acceptance_datetime_dt", "filing_date_dt", "rule_specificity", "priority"],
            ascending=[True, True, False, False, False, True],
        )
        .drop_duplicates(subset=["cik", "report_period_end"], keep="first")[
            [
                "cik",
                "accession_number",
                "report_period_end",
                "period_start",
                "period_end",
                "form_type",
                "fiscal_year",
                "fiscal_period",
                "filing_date",
                "acceptance_datetime",
            ]
        ]
    )
    out = pivot.merge(
        meta,
        on=[
            "cik",
            "report_period_end",
        ],
        how="left",
    )
    out["report_period_end_dt"] = pd.to_datetime(out["report_period_end"], errors="coerce")
    out["filing_date_dt"] = pd.to_datetime(out["filing_date"], errors="coerce")
    out["acceptance_datetime_dt"] = pd.to_datetime(out["acceptance_datetime"], errors="coerce", utc=True)
    out["period_end_dt"] = pd.to_datetime(out["period_end"], errors="coerce")
    out["period_start_dt"] = pd.to_datetime(out["period_start"], errors="coerce")
    out = out.sort_values(
        ["cik", "report_period_end_dt", "acceptance_datetime_dt", "filing_date_dt", "accession_number"]
    ).reset_index(drop=True)
    return out


def load_filing_acceptance(conn: sqlite3.Connection, include_forms: list[str]) -> pd.DataFrame:
    placeholders = ",".join("?" for _ in include_forms) if include_forms else "''"
    query_params = tuple(include_forms)
    cik_expr = sql_normalized_cik_expr("cik")
    return pd.read_sql_query(
        f"""
        SELECT
            accession_number,
            {cik_expr} AS cik,
            COALESCE(form_type, '') AS form_type,
            COALESCE(report_period_end, '') AS report_period_end,
            COALESCE(filing_date, '') AS filing_date,
            COALESCE(acceptance_datetime, '') AS acceptance_datetime
        FROM sec_filing_index
        WHERE COALESCE(form_type, '') IN ({placeholders})
          AND {cik_expr} IS NOT NULL
        """,
        conn,
        params=query_params,
    )


def load_recent_8k_item202_flags(conn: sqlite3.Connection, as_of_date: date) -> pd.DataFrame:
    start = (as_of_date - timedelta(days=30)).isoformat()
    end = as_of_date.isoformat()
    if "items" not in set(table_columns(conn, "sec_filing_index")):
        return pd.DataFrame(
            columns=["cik", "earnings_release_8k_item202_30d", "earnings_release_8k_item202_days_since"]
        )
    cik_expr = sql_normalized_cik_expr("cik")
    return pd.read_sql_query(
        f"""
        SELECT
            {cik_expr} AS cik,
            MAX(
                CASE
                    WHEN COALESCE(items, '') LIKE '%2.02%' THEN 1
                    ELSE 0
                END
            ) AS earnings_release_8k_item202_30d,
            MIN(
                CASE
                    WHEN COALESCE(items, '') LIKE '%2.02%'
                    THEN (julianday(?) - julianday(COALESCE(filing_date, '')))
                    ELSE NULL
                END
            ) AS earnings_release_8k_item202_days_since
        FROM sec_filing_index
        WHERE COALESCE(form_type, '') IN ('8-K', '8-K/A')
          AND COALESCE(filing_date, '') BETWEEN ? AND ?
          AND {cik_expr} IS NOT NULL
        GROUP BY {cik_expr}
        """,
        conn,
        params=[end, start, end],
    )


def load_insider_scores(insider_db_path: Path, as_of_date: date | None = None) -> pd.DataFrame:
    empty = pd.DataFrame(
        columns=[
            "cik",
            "ticker",
            "insider_buy_score_20bd",
            "insider_sell_score_20bd",
            "insider_data_present",
        ]
    )
    if not insider_db_path.exists():
        return empty
    conn = sqlite3.connect(insider_db_path)
    try:
        tables = {
            row[0].lower()
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        table = ""
        if "stock_signal_snapshot_tier1" in tables:
            table = "stock_signal_snapshot_tier1"
        elif "stock_signal_snapshot_t1" in tables:
            table = "stock_signal_snapshot_t1"
        if not table:
            return empty

        cols = {
            row[1].lower()
            for row in conn.execute(f"PRAGMA table_info({table})")
        }
        cik_col = "issuer_cik" if "issuer_cik" in cols else "cik" if "cik" in cols else ""
        ticker_col = (
            "issuer_trading_symbol"
            if "issuer_trading_symbol" in cols
            else "ticker"
            if "ticker" in cols
            else ""
        )
        buy_col = "buy_score_20bd" if "buy_score_20bd" in cols else ""
        sell_col = (
            "sell_score_20bd"
            if "sell_score_20bd" in cols
            else "sell_risk_score"
            if "sell_risk_score" in cols
            else "sell_risk_score_20bd"
            if "sell_risk_score_20bd" in cols
            else ""
        )
        if not cik_col or not buy_col or not sell_col:
            return empty

        as_of_col = "as_of_date" if "as_of_date" in cols else ""
        selected_asof = None
        query_params: list[str] = []
        where_clauses: list[str] = []
        if as_of_col:
            if as_of_date is not None:
                row = conn.execute(
                    f"SELECT MAX({as_of_col}) FROM {table} WHERE {as_of_col} <= ?",
                    (as_of_date.isoformat(),),
                ).fetchone()
            else:
                row = conn.execute(f"SELECT MAX({as_of_col}) FROM {table}").fetchone()
            selected_asof = row[0] if row else None
            if selected_asof is None:
                return empty
            where_clauses.append(f"{as_of_col} = ?")
            query_params = [str(selected_asof)]
        cik_expr = sql_normalized_cik_expr(cik_col)
        where_clauses.append(f"{cik_expr} IS NOT NULL")
        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        ticker_expr = f"UPPER(COALESCE({ticker_col}, ''))" if ticker_col else "''"

        logger.info(
            "Insider DB schema selected: table=%s cik_col=%s ticker_col=%s buy_col=%s sell_col=%s selected_asof=%s",
            table,
            cik_col,
            ticker_col or "<none>",
            buy_col,
            sell_col,
            selected_asof if selected_asof is not None else "<none>",
        )

        sql = f"""
            SELECT
                {cik_expr} AS cik,
                {ticker_expr} AS ticker,
                {buy_col} AS insider_buy_score_20bd,
                {sell_col} AS insider_sell_score_20bd,
                CASE
                    WHEN {buy_col} IS NOT NULL OR {sell_col} IS NOT NULL THEN 1
                    ELSE 0
                END AS insider_data_present
            FROM {table}
            {where_sql}
        """
        if query_params:
            return pd.read_sql_query(sql, conn, params=query_params)
        return pd.read_sql_query(sql, conn)
    finally:
        conn.close()


def compute_period_features(
    df: pd.DataFrame,
    *,
    ttm_interpolation_enabled: bool = False,
    sue_std_window: int = 8,
    sue_std_min_periods: int = 4,
    revenue_yoy_min_abs_prior: float = SAFE_DIVIDE_MIN_ABS_DENOMINATOR,
    eps_yoy_min_abs_prior: float = 0.01,
) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    sue_std_window = max(int(sue_std_window), 1)
    sue_std_min_periods = max(1, min(int(sue_std_min_periods), sue_std_window))
    revenue_yoy_min_abs_prior = max(float(revenue_yoy_min_abs_prior), SAFE_DIVIDE_MIN_ABS_DENOMINATOR)
    eps_yoy_min_abs_prior = max(float(eps_yoy_min_abs_prior), SAFE_DIVIDE_MIN_ABS_DENOMINATOR)
    sort_cols = ["cik", "report_period_end_dt"]
    if "acceptance_datetime_dt" in out.columns:
        sort_cols.append("acceptance_datetime_dt")
    sort_cols.append("filing_date_dt")
    out = out.sort_values(sort_cols).reset_index(drop=True)

    if "gross_profit" in out.columns and "revenue" in out.columns and "cogs" in out.columns:
        gp_missing = out["gross_profit"].isna() & out["revenue"].notna() & out["cogs"].notna()
        out.loc[gp_missing, "gross_profit"] = out.loc[gp_missing, "revenue"] - out.loc[gp_missing, "cogs"]

    if "ebitda" not in out.columns:
        out["ebitda"] = pd.NA
    eb_missing = out["ebitda"].isna()
    if "operating_income" in out.columns and "depreciation_and_amortization" in out.columns:
        out.loc[eb_missing, "ebitda"] = (
            out.loc[eb_missing, "operating_income"] + out.loc[eb_missing, "depreciation_and_amortization"]
        )
    eb_missing = out["ebitda"].isna()
    if "operating_income" in out.columns:
        out.loc[eb_missing, "ebitda"] = pd.to_numeric(out.loc[eb_missing, "operating_income"], errors="coerce")

    out["capex_abs"] = num_series(out, "capex").abs()

    debt = sum_present_components(
        out,
        (
            "short_term_borrowings",
            "current_portion_long_term_debt",
            "long_term_debt",
            "lease_liabilities",
        ),
    )
    cash_like = sum_present_components(out, ("cash_and_equivalents", "short_term_investments"))
    out["net_debt"] = debt - cash_like
    out["net_debt_to_assets"] = safe_div_series(out["net_debt"], num_series(out, "total_assets"))
    flow_cols = (
        "revenue",
        "cogs",
        "gross_profit",
        "sga",
        "research_and_development",
        "depreciation_and_amortization",
        "operating_income",
        "interest_expense",
        "pretax_income",
        "tax_expense",
        "net_income",
        "ebitda",
        "operating_cash_flow",
        "capex_abs",
        "stock_based_compensation",
    )
    for col in flow_cols:
        out[f"ttm_{col}"] = pd.Series(float("nan"), index=out.index, dtype="float64")
    out["ttm_is_interpolated"] = pd.Series(0, index=out.index, dtype="int64")

    for _, g in out.groupby("cik", sort=False):
        forms = text_series(g, "form_type").str.upper()
        annual_mask = forms.isin(ANNUAL_FORMS)
        quarterly_mask = forms.isin(QUARTERLY_FORMS)
        annual_idx = g.index[annual_mask]
        quarterly_idx = g.index[quarterly_mask]
        quarterly_ttm_valid = valid_quarterly_ttm_mask(g.loc[quarterly_idx, "report_period_end_dt"])

        for col in flow_cols:
            s = pd.to_numeric(g[col], errors="coerce") if col in g.columns else pd.Series(float("nan"), index=g.index)
            ttm_col = f"ttm_{col}"
            if len(annual_idx) > 0:
                out.loc[annual_idx, ttm_col] = s.loc[annual_idx]
            if len(quarterly_idx) > 0:
                rolling_sum = s.loc[quarterly_idx].rolling(4, min_periods=4).sum()
                rolling_sum = rolling_sum.where(quarterly_ttm_valid, float("nan"))
                if ttm_interpolation_enabled:
                    q_vals = s.loc[quarterly_idx]
                    q_count = q_vals.notna().rolling(4, min_periods=3).sum()
                    interp = q_vals.rolling(4, min_periods=3).sum() * (4.0 / q_count)
                    q_dates = pd.to_datetime(g.loc[quarterly_idx, "report_period_end_dt"], errors="coerce")
                    q_date_ord = q_dates.map(lambda x: x.toordinal() if pd.notna(x) else float("nan"))
                    span_days = q_date_ord.rolling(4, min_periods=3).max() - q_date_ord.rolling(4, min_periods=3).min()
                    interp_ok = (q_count >= 3) & (span_days <= 430)
                    interp = interp.where(interp_ok & ~quarterly_ttm_valid)
                    filled = rolling_sum.isna() & interp.notna()
                    if filled.any():
                        out.loc[filled[filled].index, "ttm_is_interpolated"] = 1
                    rolling_sum = rolling_sum.combine_first(interp)
                out.loc[quarterly_idx, ttm_col] = rolling_sum

    out["free_cash_flow"] = num_series(out, "ttm_operating_cash_flow") - num_series(out, "ttm_capex_abs")

    assets_now = num_series(out, "total_assets")
    assets_prev = assets_now.groupby(out["cik"]).shift(1)
    avg_assets = (assets_now + assets_prev) / 2.0
    out["accruals_ratio"] = safe_div_series(
        num_series(out, "ttm_net_income") - num_series(out, "ttm_operating_cash_flow"),
        avg_assets,
    )
    out["gross_margin"] = safe_div_series(num_series(out, "ttm_gross_profit"), num_series(out, "ttm_revenue"))
    out["operating_margin"] = safe_div_series(num_series(out, "ttm_operating_income"), num_series(out, "ttm_revenue"))
    out["cfo_to_net_income"] = safe_div_series(num_series(out, "ttm_operating_cash_flow"), num_series(out, "ttm_net_income"))
    out["sbc_to_revenue"] = safe_div_series(num_series(out, "ttm_stock_based_compensation"), num_series(out, "ttm_revenue"))
    # Legacy schema name retained for compatibility; this is option dilution vs. basic shares, not YoY share-count growth.
    out["dilution_rate"] = safe_div_series(
        (num_series(out, "weighted_avg_shares_diluted") - num_series(out, "weighted_avg_shares_basic")),
        num_series(out, "weighted_avg_shares_basic"),
    )
    out["market_cap_proxy"] = num_series(out, "public_float")

    out["eps_base"] = num_series(out, "eps_diluted")
    eps_missing = out["eps_base"].isna()
    out.loc[eps_missing, "eps_base"] = pd.to_numeric(out.loc[eps_missing, "eps_basic"], errors="coerce")

    out["revenue_yoy_growth"] = pd.Series(float("nan"), index=out.index, dtype="float64")
    out["eps_yoy_growth"] = pd.Series(float("nan"), index=out.index, dtype="float64")
    out["unexpected_eps"] = pd.Series(float("nan"), index=out.index, dtype="float64")
    out["unexpected_revenue"] = pd.Series(float("nan"), index=out.index, dtype="float64")
    out["unexpected_revenue_std"] = pd.Series(float("nan"), index=out.index, dtype="float64")
    out["sur"] = pd.Series(float("nan"), index=out.index, dtype="float64")
    out["revenue_acceleration"] = pd.Series(float("nan"), index=out.index, dtype="float64")
    out["earnings_acceleration"] = pd.Series(float("nan"), index=out.index, dtype="float64")
    out["unexpected_eps_std"] = pd.Series(float("nan"), index=out.index, dtype="float64")

    for _, g in out.groupby("cik", sort=False):
        forms = text_series(g, "form_type").str.upper()
        annual_mask = forms.isin(ANNUAL_FORMS)
        quarterly_mask = forms.isin(QUARTERLY_FORMS)
        revenue = pd.to_numeric(g["revenue"], errors="coerce")
        eps = pd.to_numeric(g["eps_base"], errors="coerce")

        for mask, cadence in ((annual_mask, 1), (quarterly_mask, 4)):
            idx = g.index[mask]
            if len(idx) == 0:
                continue

            rev_slice = revenue.loc[idx]
            rev_prev = rev_slice.shift(cadence)
            revenue_yoy = safe_div_series(rev_slice, rev_prev, eps=revenue_yoy_min_abs_prior) - 1.0
            out.loc[idx, "revenue_yoy_growth"] = revenue_yoy

            eps_slice = eps.loc[idx]
            eps_prev = eps_slice.shift(cadence)
            eps_yoy = safe_div_series(eps_slice, eps_prev, eps=eps_yoy_min_abs_prior) - 1.0
            out.loc[idx, "eps_yoy_growth"] = eps_yoy

            unexpected_revenue = rev_slice - rev_prev
            unexpected_eps = eps_slice - eps_prev
            out.loc[idx, "unexpected_revenue"] = unexpected_revenue
            out.loc[idx, "unexpected_eps"] = unexpected_eps
            out.loc[idx, "revenue_acceleration"] = revenue_yoy.diff()
            out.loc[idx, "earnings_acceleration"] = eps_yoy.diff()
            out.loc[idx, "unexpected_revenue_std"] = unexpected_revenue.shift(1).rolling(
                sue_std_window,
                min_periods=sue_std_min_periods,
            ).std()
            out.loc[idx, "unexpected_eps_std"] = unexpected_eps.shift(1).rolling(
                sue_std_window,
                min_periods=sue_std_min_periods,
            ).std()

    out["sue"] = safe_div_series(
        pd.to_numeric(out["unexpected_eps"], errors="coerce"),
        pd.to_numeric(out["unexpected_eps_std"], errors="coerce"),
    )
    out["sur"] = safe_div_series(
        pd.to_numeric(out["unexpected_revenue"], errors="coerce"),
        pd.to_numeric(out["unexpected_revenue_std"], errors="coerce"),
    )

    feature_cols = [
        "gross_margin",
        "operating_margin",
        "accruals_ratio",
        "cfo_to_net_income",
        "sue",
        "sur",
        "revenue_acceleration",
        "earnings_acceleration",
    ]
    present = [c for c in feature_cols if c in out.columns]
    if present:
        miss = out[present].isna()
        out["effective_missing_feature_count"] = miss.sum(axis=1).astype("int64")
        out["effective_any_feature_missing"] = (out["effective_missing_feature_count"] > 0).astype("int64")
    else:
        out["effective_missing_feature_count"] = 0
        out["effective_any_feature_missing"] = 0
    out["feature_status_json"] = "{}"
    out["feature_applicability_json"] = "{}"
    return out


def score_snapshot(
    snapshot: pd.DataFrame,
    *,
    weights: dict[str, float],
    thresholds: dict[str, float],
    enhancements: dict[str, Any] | None = None,
) -> pd.DataFrame:
    out = snapshot.copy()
    enhancements = enhancements if isinstance(enhancements, dict) else {}

    sue_group_enabled = bool(enhancements.get("sue_group_enabled", True))
    sue_primary_group = str(enhancements.get("sue_primary_group", "industry_aggregate")).strip()
    sue_secondary_group = str(enhancements.get("sue_secondary_group", "sector")).strip()
    sue_min_group_size = int(enhancements.get("sue_min_group_size", 8))

    if sue_group_enabled and (
        (sue_primary_group and sue_primary_group in out.columns)
        or (sue_secondary_group and sue_secondary_group in out.columns)
    ):
        out["z_sue"] = rank_centered_by_group(
            out["sue"],
            out[sue_primary_group] if sue_primary_group and sue_primary_group in out.columns else None,
            out[sue_secondary_group] if sue_secondary_group and sue_secondary_group in out.columns else None,
            min_group_size=sue_min_group_size,
        )
    else:
        out["z_sue"] = rank_centered(out["sue"])

    out["z_sur"] = rank_centered(out["sur"])
    out["z_earnings_acceleration"] = rank_centered(out["earnings_acceleration"])
    out["z_revenue_acceleration"] = rank_centered(out["revenue_acceleration"])
    out["z_insider_net_score"] = rank_centered(out["insider_net_score"])
    out["bonus_accrual_quality"] = rank_centered(
        (-pd.to_numeric(out["accruals_ratio"], errors="coerce")).clip(lower=0.0)
    )
    accrual_penalty_growth_threshold = float(enhancements.get("accrual_penalty_growth_threshold", 0.15))
    accrual_raw = pd.to_numeric(out["accruals_ratio"], errors="coerce").clip(lower=0.0)
    yoy_growth = pd.to_numeric(out.get("revenue_yoy_growth", pd.Series(float("nan"), index=out.index)), errors="coerce")
    accrual_apply_mask = yoy_growth.isna() | (yoy_growth < accrual_penalty_growth_threshold)
    out["pen_accruals"] = rank_centered(accrual_raw.where(accrual_apply_mask, 0.0))
    out["pen_leverage"] = rank_centered(pd.to_numeric(out["net_debt_to_assets"], errors="coerce").clip(lower=0.0))
    item202_decay_enabled = bool(enhancements.get("item202_decay_enabled", True))
    item202_decay_window_days = max(1.0, float(enhancements.get("item202_decay_window_days", 30.0)))
    item202_binary = pd.to_numeric(
        out.get("earnings_release_8k_item202_30d", pd.Series(0.0, index=out.index)),
        errors="coerce",
    ).fillna(0.0)
    item202_days = pd.to_numeric(
        out.get("earnings_release_8k_item202_days_since", pd.Series(float("nan"), index=out.index)),
        errors="coerce",
    )
    if item202_decay_enabled:
        item202_decay = (1.0 - (item202_days / item202_decay_window_days)).clip(lower=0.0, upper=1.0)
        out["f_8k_item202"] = item202_decay.where(item202_days.notna(), item202_binary).fillna(0.0)
    else:
        out["f_8k_item202"] = item202_binary

    out["consensus_proxy_score"] = (
        weights["sue"] * out["z_sue"].fillna(0.0)
        + weights.get("sur", 0.0) * out["z_sur"].fillna(0.0)
        + weights["earnings_acceleration"] * out["z_earnings_acceleration"].fillna(0.0)
        + weights["revenue_acceleration"] * out["z_revenue_acceleration"].fillna(0.0)
        + weights["insider_net_score"] * out["z_insider_net_score"].fillna(0.0)
        + weights["eight_k_item202"] * out["f_8k_item202"].fillna(0.0)
        + weights["accruals_quality_bonus"] * out["bonus_accrual_quality"].fillna(0.0)
        - weights["accruals_penalty"] * out["pen_accruals"].fillna(0.0)
        - weights["leverage_penalty"] * out["pen_leverage"].fillna(0.0)
    )

    strong_buy = float(thresholds.get("strong_buy", 1.20))
    buy = float(thresholds.get("buy", 0.40))
    hold_low = float(thresholds.get("hold_low", -0.40))
    reduce_low = float(thresholds.get("reduce_low", -1.20))

    def map_reco(val: Any) -> str:
        if pd.isna(val):
            return "HOLD_NO_DATA"
        x = float(val)
        if x >= strong_buy:
            return "STRONG_BUY"
        if x >= buy:
            return "BUY"
        if x >= hold_low:
            return "HOLD"
        if x >= reduce_low:
            return "REDUCE"
        return "SELL"

    out["recommendation_proxy"] = out["consensus_proxy_score"].map(map_reco)
    return out


def evaluate_cutover(
    snapshot: pd.DataFrame,
    *,
    universe_count: int,
    as_of_date: date,
    tolerances: dict[str, float],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    coverage = (snapshot["ticker"].nunique() / universe_count) if universe_count > 0 else 0.0
    rows.append(
        {
            "metric_name": "universe_coverage",
            "metric_value": coverage,
            "threshold_value": float(tolerances.get("universe_coverage_min", 0.93)),
            "comparator": ">=",
        }
    )

    required_cols = ["revenue", "net_income", "operating_cash_flow", "total_assets", "total_equity"]
    present = [c for c in required_cols if c in snapshot.columns]
    required_field_coverage = float(snapshot[present].notna().mean().mean()) if present and not snapshot.empty else 0.0
    rows.append(
        {
            "metric_name": "required_field_coverage",
            "metric_value": required_field_coverage,
            "threshold_value": float(tolerances.get("required_field_coverage_min", 0.90)),
            "comparator": ">=",
        }
    )

    eff_cov = 1.0 - (
        float(snapshot["effective_any_feature_missing"].mean())
        if "effective_any_feature_missing" in snapshot.columns and not snapshot.empty
        else 1.0
    )
    rows.append(
        {
            "metric_name": "effective_feature_coverage",
            "metric_value": eff_cov,
            "threshold_value": float(tolerances.get("effective_feature_coverage_min", 0.80)),
            "comparator": ">=",
        }
    )

    proxy_coverage = float(snapshot["consensus_proxy_score"].notna().mean()) if "consensus_proxy_score" in snapshot.columns and not snapshot.empty else 0.0
    rows.append(
        {
            "metric_name": "proxy_coverage",
            "metric_value": proxy_coverage,
            "threshold_value": float(tolerances.get("proxy_coverage_min", 0.90)),
            "comparator": ">=",
        }
    )

    fresh = pd.to_datetime(text_series(snapshot, "filing_date"), errors="coerce", utc=True)
    if fresh.notna().any():
        delta_days = (pd.Timestamp(as_of_date, tz=timezone.utc) - fresh).dt.days
        freshness_median = float(delta_days.median())
    else:
        freshness_median = 9999.0
    rows.append(
        {
            "metric_name": "freshness_median_days",
            "metric_value": freshness_median,
            "threshold_value": float(tolerances.get("freshness_median_days_max", 130.0)),
            "comparator": "<=",
        }
    )

    for row in rows:
        if row["comparator"] == ">=":
            row["pass_flag"] = int(float(row["metric_value"]) >= float(row["threshold_value"]))
        else:
            row["pass_flag"] = int(float(row["metric_value"]) <= float(row["threshold_value"]))
    return rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build SEC tier-1 period features + proxy snapshot + cutover tolerance report."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Path to fundamentals YAML config.")
    parser.add_argument("--db-path", type=Path, default=None, help="Override fundamentals SQLite DB path.")
    parser.add_argument("--as-of-date", type=str, default=None, help="As-of date (YYYY-MM-DD).")
    parser.add_argument("--as-of-timestamp", type=str, default=None, help="Explicit UTC timestamp (ISO 8601).")
    parser.add_argument("--metric-mapping-csv", type=Path, default=None, help="Optional compiled metric-mapping CSV.")
    return parser.parse_args()


def main() -> None:
    configure_pipeline_logging()
    args = parse_args()
    cfg_path, cfg = load_sec_fundamentals_config(args.config)
    db_path = Path(
        args.db_path
        if args.db_path is not None
        else cfg_get(cfg, "db_path", default=str(default_db_path()))
    ).expanduser()
    as_of_date = parse_iso_date(args.as_of_date) if args.as_of_date else parse_iso_date(
        cfg_get(cfg_get(cfg, "features", default={}), "as_of_date", default=None)
    )
    if as_of_date is None:
        as_of_date = datetime.now(timezone.utc).date()
    normalized_as_of_date = previous_or_same_business_day(as_of_date)
    if normalized_as_of_date != as_of_date:
        logger.info(
            f"Adjusted weekend as_of_date {as_of_date.isoformat()} to business day {normalized_as_of_date.isoformat()}."
        )
        as_of_date = normalized_as_of_date
    as_of_timestamp = resolve_as_of_timestamp(
        as_of_date=as_of_date,
        cli_value=args.as_of_timestamp,
        cfg=cfg,
    )

    tag_map_path = Path(
        cfg_get(cfg, "tag_map_path", default=str(Path(__file__).resolve().with_name("tier1_tag_map.yaml")))
    )
    if not tag_map_path.is_absolute():
        tag_map_path = (Path(__file__).resolve().parent.parent / tag_map_path).resolve()
    tag_map = load_tag_map(tag_map_path)
    features_cfg = cfg_get(cfg, "features", default={})

    metric_mapping_csv = args.metric_mapping_csv
    if metric_mapping_csv is None:
        raw_map = cfg_get(features_cfg, "metric_mapping_csv", default=None)
        if raw_map:
            metric_mapping_csv = Path(str(raw_map))
            if not metric_mapping_csv.is_absolute():
                metric_mapping_csv = (Path(__file__).resolve().parent.parent / metric_mapping_csv).resolve()
    if metric_mapping_csv is None:
        snap_cfg = cfg_get(cfg, "snapshot_enhanced", default={})
        raw_map = cfg_get(snap_cfg, "metric_mapping_csv", default=None)
        if raw_map:
            metric_mapping_csv = Path(str(raw_map))
            if not metric_mapping_csv.is_absolute():
                metric_mapping_csv = (Path(__file__).resolve().parent.parent / metric_mapping_csv).resolve()

    metric_registry = load_metric_registry(tag_map, metric_mapping_csv)

    conn = _connect_sqlite(db_path)
    try:
        ensure_required_tables(conn)
        ensure_period_output_columns(conn)
        ensure_metadata_period_table(conn)
        universe = load_universe(conn)
        if universe.empty:
            raise RuntimeError("sec_entity_universe is empty. Run ingestion first.")
        issuer_profile = load_issuer_profile_mapping(cfg, cfg_path)

        resolved_source_name, security_base = load_resolved_security_snapshot(
            conn,
            as_of_date=as_of_date,
            preferred_tables=DEFAULT_RESOLVED_SNAPSHOT_TABLES,
        )
        cik_profile_df = pd.DataFrame()
        if not security_base.empty and "industry_aggregate" in security_base.columns:
            cik_profile_df = (
                security_base[["cik", "industry_aggregate"]]
                .drop_duplicates(subset=["cik"], keep="last")
                .reset_index(drop=True)
            )

        include_forms = [str(x).strip().upper() for x in cfg_get(features_cfg, "include_forms", default=[]) if str(x).strip()]
        if not include_forms:
            include_forms = sorted(PERIODIC_FORMS)
        publication_buffer_minutes = int(cfg_get(features_cfg, "publication_buffer_minutes", default=5))

        facts = load_selected_facts(
            conn,
            include_forms=include_forms,
            metric_registry=metric_registry,
            as_of_timestamp=as_of_timestamp,
            publication_buffer_minutes=publication_buffer_minutes,
            cik_profile_df=cik_profile_df,
        )
        if facts.empty:
            raise RuntimeError("No mapped SEC facts found in sec_xbrl_facts_raw for selected forms/window.")

        period_all = build_metric_period_frame(facts)
        period_all = period_all.merge(universe, on="cik", how="left")
        period_all["ticker"] = period_all["ticker"].fillna("").str.upper()
        period_all["company_name"] = period_all["company_name"].fillna("")
        if not issuer_profile.empty:
            period_all = period_all.merge(issuer_profile, on="ticker", how="left", suffixes=("", "_profile"))
            for col in ("sector", "industry", "industry_aggregate"):
                pcol = f"{col}_profile"
                if pcol in period_all.columns:
                    if col not in period_all.columns:
                        period_all[col] = ""
                    period_all[col] = period_all[col].replace("", pd.NA).fillna(period_all[pcol])
                    period_all = period_all.drop(columns=[pcol], errors="ignore")
        for col in ("sector", "industry", "industry_aggregate"):
            if col not in period_all.columns:
                period_all[col] = ""
            period_all[col] = period_all[col].fillna("")

        period_all = annotate_period_rows(
            period_all,
            min_core_metrics=int(cfg_get(features_cfg, "period_min_core_metrics", default=1)),
        )
        metadata_period = period_all[period_all["is_scoring_eligible"] == 0].copy()
        period = period_all[period_all["is_scoring_eligible"] == 1].copy()
        period = attach_metadata_facts_to_scoring_rows(period, metadata_period)
        if period.empty:
            raise RuntimeError("No scoring-eligible period rows remain after metadata-only filtering.")

        period = compute_period_features(
            period,
            ttm_interpolation_enabled=bool(cfg_get(features_cfg, "ttm_interpolation_enabled", default=True)),
            sue_std_window=int(cfg_get(features_cfg, "sue_std_window", default=8)),
            sue_std_min_periods=int(cfg_get(features_cfg, "sue_std_min_periods", default=4)),
            revenue_yoy_min_abs_prior=float(
                cfg_get(features_cfg, "revenue_yoy_min_abs_prior", default=SAFE_DIVIDE_MIN_ABS_DENOMINATOR)
            ),
            eps_yoy_min_abs_prior=float(cfg_get(features_cfg, "eps_yoy_min_abs_prior", default=0.01)),
        )
        period["as_of_date"] = as_of_date.isoformat()
        period["updated_at_utc"] = utc_now_iso()
        metadata_period["as_of_date"] = as_of_date.isoformat()
        metadata_period["updated_at_utc"] = utc_now_iso()

        # Attach 8-K and insider proxies.
        item202 = load_recent_8k_item202_flags(conn, as_of_date=as_of_date)
        insider_db_path = Path(cfg_get(cfg, "insider_db_path", default="")).expanduser()
        insider = load_insider_scores(insider_db_path, as_of_date=as_of_date)
        core_cols = ["ttm_revenue", "ttm_net_income", "ttm_operating_cash_flow", "total_assets", "ttm_ebitda"]
        for col in core_cols:
            if col not in period.columns:
                period[col] = pd.NA
        period["_core_metric_count"] = period[core_cols].notna().sum(axis=1)
        latest = (
            period.sort_values(
                ["cik", "_core_metric_count", "report_period_end_dt", "acceptance_datetime_dt", "filing_date_dt"],
                ascending=[True, False, False, False, False],
            )
            .drop_duplicates(subset=["cik"], keep="first")
            .copy()
        )
        latest = latest.merge(item202, on="cik", how="left")
        latest = latest.merge(insider, on=["cik", "ticker"], how="left")
        if "market_cap_proxy" not in latest.columns:
            latest["market_cap_proxy"] = pd.NA
        latest["market_cap_proxy"] = pd.to_numeric(latest["market_cap_proxy"], errors="coerce")
        latest["_latest_public_float"] = pd.to_numeric(latest.get("public_float"), errors="coerce")
        latest["_latest_public_float_dt"] = pd.to_datetime(latest.get("filing_date_dt"), errors="coerce")
        missing_float_dt = latest["_latest_public_float_dt"].isna()
        latest.loc[missing_float_dt, "_latest_public_float_dt"] = pd.to_datetime(
            latest.loc[missing_float_dt, "report_period_end_dt"], errors="coerce"
        )
        max_age_days = int(cfg_get(features_cfg, "market_cap_public_float_max_age_days", default=180))
        stale_days = (pd.Timestamp(as_of_date) - latest["_latest_public_float_dt"]).dt.days
        fresh_float = latest["_latest_public_float"].where(
            stale_days.notna() & (stale_days <= max_age_days),
            float("nan"),
        )
        latest["market_cap_proxy"] = latest["market_cap_proxy"].fillna(fresh_float)
        latest = latest.drop(
            columns=[
                "_latest_public_float",
                "_latest_public_float_dt",
            ],
            errors="ignore",
        )
        for col in ("insider_buy_score_20bd", "insider_sell_score_20bd"):
            if col not in latest.columns:
                latest[col] = pd.NA
            latest[col] = pd.to_numeric(latest[col], errors="coerce")
        if "insider_data_present" not in latest.columns:
            latest["insider_data_present"] = 0
        latest["insider_data_present"] = (
            pd.to_numeric(latest["insider_data_present"], errors="coerce").fillna(0).astype("int64")
        )
        if "earnings_release_8k_item202_30d" not in latest.columns:
            latest["earnings_release_8k_item202_30d"] = 0.0
        latest["earnings_release_8k_item202_30d"] = pd.to_numeric(
            latest["earnings_release_8k_item202_30d"], errors="coerce"
        ).fillna(0.0)
        if "earnings_release_8k_item202_days_since" not in latest.columns:
            latest["earnings_release_8k_item202_days_since"] = pd.NA
        latest["earnings_release_8k_item202_days_since"] = pd.to_numeric(
            latest["earnings_release_8k_item202_days_since"], errors="coerce"
        )
        latest["insider_net_score"] = (
            latest["insider_buy_score_20bd"].fillna(0.0) - latest["insider_sell_score_20bd"].fillna(0.0)
        ).where(latest["insider_data_present"] > 0, np.nan)

        security_snapshot = merge_base_snapshot(
            latest_entity=latest,
            security_base=security_base,
            issuer_profile=issuer_profile,
        )
        if security_snapshot.empty:
            security_snapshot = latest.copy()
        security_snapshot["stale_entity_data"] = ~security_snapshot["cik"].isin(latest["cik"])
        security_snapshot["_core_metric_count"] = pd.to_numeric(
            security_snapshot.get("_core_metric_count", pd.Series(0, index=security_snapshot.index)),
            errors="coerce",
        ).fillna(0)

        enhancements = {
            "sue_group_enabled": bool(cfg_get(features_cfg, "sue_group_enabled", default=True)),
            "sue_primary_group": str(cfg_get(features_cfg, "sue_primary_group", default="industry_aggregate")),
            "sue_secondary_group": str(cfg_get(features_cfg, "sue_secondary_group", default="sector")),
            "sue_min_group_size": int(cfg_get(features_cfg, "sue_min_group_size", default=8)),
            "accrual_penalty_growth_threshold": float(
                cfg_get(features_cfg, "accrual_penalty_growth_threshold", default=0.15)
            ),
            "item202_decay_enabled": bool(cfg_get(features_cfg, "item202_decay_enabled", default=True)),
            "item202_decay_window_days": float(cfg_get(features_cfg, "item202_decay_window_days", default=30.0)),
        }

        raw_weights = dict(cfg_get(features_cfg, "consensus_proxy_weights", default={}))
        weighted_entity = score_snapshot(
            latest,
            weights=resolve_consensus_weights(raw_weights),
            thresholds={k: float(v) for k, v in dict(cfg_get(features_cfg, "recommendation_thresholds", default={})).items()},
            enhancements=enhancements,
        )
        weighted = score_snapshot(
            security_snapshot,
            weights=resolve_consensus_weights(raw_weights),
            thresholds={k: float(v) for k, v in dict(cfg_get(features_cfg, "recommendation_thresholds", default={})).items()},
            enhancements=enhancements,
        )

        # Populate period rows with proxy columns via cik join.
        period = period.merge(
            weighted_entity.drop_duplicates(subset=["cik"], keep="first")[
                [
                    "cik",
                    "insider_buy_score_20bd",
                    "insider_sell_score_20bd",
                    "insider_net_score",
                    "insider_data_present",
                    "earnings_release_8k_item202_30d",
                    "consensus_proxy_score",
                    "recommendation_proxy",
                ]
            ],
            on="cik",
            how="left",
        )
        period = period.drop(columns=["_core_metric_count"], errors="ignore")

        period["data_quality_flags_json"] = period.apply(
            lambda r: json.dumps(
                {
                    "missing_revenue": pd.isna(r.get("revenue")),
                    "missing_total_assets": pd.isna(r.get("total_assets")),
                    "missing_operating_cash_flow": pd.isna(r.get("operating_cash_flow")),
                    "missing_eps": pd.isna(r.get("eps_base")),
                    "insider_data_present": bool(r.get("insider_data_present", 0)),
                    "effective_feature_missing": bool(r.get("effective_any_feature_missing", 0)),
                    "core_nonnull_count": int(r.get("core_nonnull_count", 0) or 0),
                    "is_metadata_only": bool(r.get("is_metadata_only", 0)),
                    "is_scoring_eligible": bool(r.get("is_scoring_eligible", 0)),
                },
                separators=(",", ":"),
                ensure_ascii=True,
            ),
            axis=1,
        )
        if not metadata_period.empty:
            metadata_period["data_quality_flags_json"] = metadata_period.apply(
                lambda r: json.dumps(
                    {
                        "core_nonnull_count": int(r.get("core_nonnull_count", 0) or 0),
                        "is_metadata_only": bool(r.get("is_metadata_only", 0)),
                        "is_scoring_eligible": bool(r.get("is_scoring_eligible", 0)),
                        "metadata_only_reason": str(r.get("metadata_only_reason", "") or ""),
                    },
                    separators=(",", ":"),
                    ensure_ascii=True,
                ),
                axis=1,
            )

        # Rename canonical columns to table schema names where needed.
        for metric, col_name in METRIC_TO_PERIOD_COLUMN.items():
            if metric in period.columns and col_name != metric:
                period[col_name] = period[metric]
        period["total_revenue"] = num_series(period, "ttm_revenue").combine_first(num_series(period, "revenue"))

        period_cols = [row[1] for row in conn.execute("PRAGMA table_info(sec_fundamental_period_t1)")]
        for col in period_cols:
            if col not in period.columns:
                period[col] = pd.NA
        period_out = period[period_cols].copy()
        period_unique_cols = ["cik", "report_period_end", "form_type", "accession_number", "as_of_date"]
        period_out = (
            period_out.sort_values(
                ["cik", "report_period_end", "acceptance_datetime", "filing_date", "updated_at_utc"],
                ascending=[True, True, False, False, False],
            )
            .drop_duplicates(subset=period_unique_cols, keep="last")
            .reset_index(drop=True)
        )
        period_conflicts = (
            period_out.groupby(["as_of_date", "cik", "report_period_end"], dropna=False)
            .size()
            .reset_index(name="row_count")
        )
        period_conflicts = period_conflicts[period_conflicts["row_count"] > 1]
        if not period_conflicts.empty:
            sample = period_conflicts.head(10).to_dict(orient="records")
            logger.warning(
                "Detected %d cik/report_period_end duplicate groups in period output; sample=%s",
                len(period_conflicts),
                sample,
            )

        metadata_cols = table_columns(conn, "sec_fundamental_period_metadata_t1")
        if not metadata_cols:
            raise RuntimeError(
                "Missing sec_fundamental_period_metadata_t1 schema after ensure_metadata_period_table(). "
                "Run init_sec_fundamentals_db.py to create the metadata table."
            )
        for col in metadata_cols:
            if col not in metadata_period.columns:
                metadata_period[col] = pd.NA
        metadata_out = metadata_period[metadata_cols].copy()

        snapshot = weighted.copy()
        snapshot["as_of_date"] = as_of_date.isoformat()
        snapshot["updated_at_utc"] = utc_now_iso()
        if "latest_report_period_end" not in snapshot.columns:
            snapshot["latest_report_period_end"] = snapshot["report_period_end"]
        if "latest_filing_date" not in snapshot.columns:
            snapshot["latest_filing_date"] = snapshot["filing_date"]
        snapshot["ebitda"] = num_series(snapshot, "ttm_ebitda").combine_first(num_series(snapshot, "ebitda"))
        snapshot["net_income"] = num_series(snapshot, "ttm_net_income").combine_first(num_series(snapshot, "net_income"))
        snapshot["operating_cash_flow"] = num_series(snapshot, "ttm_operating_cash_flow").combine_first(
            num_series(snapshot, "operating_cash_flow")
        )
        snapshot["total_revenue"] = num_series(snapshot, "ttm_revenue").combine_first(num_series(snapshot, "revenue"))
        snapshot_out = snapshot.copy()
        snapshot_out = snapshot_out.drop_duplicates(subset=["as_of_date", "ticker"], keep="last").reset_index(drop=True)

        conn.execute("DELETE FROM sec_fundamental_period_t1 WHERE as_of_date = ?", (as_of_date.isoformat(),))
        period_out.to_sql("sec_fundamental_period_t1", conn, if_exists="append", index=False)
        conn.execute("DELETE FROM sec_fundamental_period_metadata_t1 WHERE as_of_date = ?", (as_of_date.isoformat(),))
        if not metadata_out.empty:
            metadata_out.to_sql("sec_fundamental_period_metadata_t1", conn, if_exists="append", index=False)
        if _legacy_proxy_table_exists(conn):
            # Enforce enhanced-only consumption by clearing stale legacy snapshot data.
            conn.execute("DELETE FROM sec_signal_proxy_snapshot_t1")

        # Cutover checks.
        tolerances = {k: float(v) for k, v in dict(cfg_get(features_cfg, "cutover_tolerances", default={})).items()}
        cutover_rows = evaluate_cutover(
            snapshot_out,
            universe_count=max(1, universe["ticker"].str.upper().nunique()),
            as_of_date=as_of_date,
            tolerances=tolerances,
        )
        run_id = str(uuid.uuid4())
        conn.execute("DELETE FROM sec_cutover_tolerance_result WHERE run_id = ?", (run_id,))
        for row in cutover_rows:
            conn.execute(
                """
                INSERT INTO sec_cutover_tolerance_result(
                    run_id,
                    as_of_date,
                    metric_name,
                    metric_value,
                    threshold_value,
                    comparator,
                    pass_flag,
                    details,
                    created_at_utc
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    as_of_date.isoformat(),
                    row["metric_name"],
                    float(row["metric_value"]),
                    float(row["threshold_value"]),
                    row["comparator"],
                    int(row["pass_flag"]),
                    "",
                    utc_now_iso(),
                ),
            )
        conn.commit()

        outputs_cfg = cfg_get(cfg, "outputs", default={})
        output_dir = Path(cfg_get(outputs_cfg, "report_output_dir", default="output")).expanduser()
        if not output_dir.is_absolute():
            output_dir = (Path(__file__).resolve().parent.parent / output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

        cutover_json = output_dir / str(cfg_get(outputs_cfg, "cutover_report_json", default="sec_fund_cutover_report.json"))
        feature_csv = output_dir / str(cfg_get(outputs_cfg, "feature_snapshot_csv", default="sec_fundamental_proxy_snapshot.csv"))

        with open(cutover_json, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "run_id": run_id,
                    "as_of_date": as_of_date.isoformat(),
                    "all_pass": all(int(row["pass_flag"]) == 1 for row in cutover_rows),
                    "checks": cutover_rows,
                },
                f,
                indent=2,
            )
        snapshot_out.sort_values(["consensus_proxy_score", "sue"], ascending=[False, False]).to_csv(
            feature_csv,
            index=False,
        )

        logger.info("Built sec_fundamental_period_t1 rows: %s", f"{len(period_out):,}")
        logger.info("Built sec_fundamental_period_metadata_t1 rows: %s", f"{len(metadata_out):,}")
        logger.info("Built in-memory proxy snapshot rows (not persisted): %s", f"{len(snapshot_out):,}")
        logger.info("As-of timestamp UTC: %s", as_of_timestamp.strftime('%Y-%m-%dT%H:%M:%SZ'))
        if resolved_source_name:
            logger.info("Resolved security source: %s", resolved_source_name)
        if _legacy_proxy_table_exists(conn):
            logger.info("Cleared legacy sec_signal_proxy_snapshot_t1 rows (enhanced-only mode).")
        logger.info("Saved cutover report: %s", cutover_json)
        logger.info("Saved snapshot CSV: %s", feature_csv)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
