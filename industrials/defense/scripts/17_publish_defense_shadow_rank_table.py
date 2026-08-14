#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from industrials.core.financial_filing_lineage import (  # noqa: E402
    apply_financial_lineage_gate,
    build_financial_filing_lineage,
    validate_financial_lineage_rank_rows,
    write_financial_lineage_report,
)

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.logging_utils import configure_utc_logging  # noqa: E402
from industrials.core.policy_loader import load_eligibility_policy, resolve_policy  # noqa: E402
from industrials.core.rank_table_contracts import defense_final_rank_header  # noqa: E402
from industrials.core.reports import write_csv_atomic  # noqa: E402
from industrials.core.text_norm import normalize_ticker  # noqa: E402
from industrials.defense.metric_contract import (  # noqa: E402
    BASE_FEATURE_ALIASES,
    HIGHER_IS_BETTER_SCORE_FIELDS,
    LOWER_IS_BETTER_SCORE_FIELDS,
    PILLAR_INPUT_FIELDS,
    SPECIALIZED_PILLAR_FIELDS,
)
from industrials.defense.research_artifacts import (  # noqa: E402
    PRODUCTION_PROMOTION_STATUS,
    PRODUCTION_SCORING_CONTRACT_VERSION,
    load_production_lock,
    lock_mode_for_asof,
    weighted_score,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
MODEL_FAMILY = "defense"
SCORE_MODEL_VERSION = "defense_shadow_v0.1.0"
MODEL_VERSION = "defense_shadow_2026_07"
SCORING_CONTRACT_VERSION = "tech_family_final_rank_table_v1_shadow"
NEUTRAL_SCORE = 50.0
PANEL_SOURCE_CURRENT_UNIVERSE_REPLAY = "dashboard_rank_snapshot_current_universe_replay"
PANEL_SOURCE_SURVIVORSHIP_CORRECTED = "survivorship_corrected_pit_membership_score_recompute"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish the shadow defense final rank table.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--asof", required=True, help="Market/PIT as-of date, YYYY-MM-DD.")
    parser.add_argument(
        "--policy-asof",
        default="",
        help="Eligibility-policy lock date. Defaults to --asof; historical research replays may pin this to a model lock date.",
    )
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument(
        "--membership-mode",
        choices=["current", "pit"],
        default="current",
        help="current publishes the live dashboard universe; pit publishes members effective at --asof for research snapshots.",
    )
    parser.add_argument(
        "--allow-overwrite",
        action="store_true",
        help="Explicitly replace an existing dated shadow artifact. Daily/PIT runs should not use this.",
    )
    parser.add_argument(
        "--scoring-mode",
        choices=["auto", "baseline", "specialized_v1"],
        default="auto",
        help=(
            "Scoring treatment. Auto selects the effective production lock; "
            "research candidates must choose explicitly."
        ),
    )
    parser.add_argument(
        "--score-model-version",
        default="",
        help="Explicit score-model build id. Required for research candidates.",
    )
    parser.add_argument(
        "--research-candidate",
        action="store_true",
        help="Seal this artifact as research-only and keep every portfolio/OOS eligibility gate closed.",
    )
    return parser.parse_args()


def parse_asof(raw: str) -> str:
    return datetime.strptime(raw.strip(), "%Y-%m-%d").date().isoformat()


def date_on_or_before(raw: object, asof: str) -> bool:
    text = str(raw or "").strip()[:10]
    if not text:
        return False
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d").date()
        limit = datetime.strptime(asof, "%Y-%m-%d").date()
    except ValueError:
        return False
    return parsed <= limit


def finite(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def fmt(value: Any, digits: int = 6) -> str:
    number = finite(value)
    if number is None:
        return ""
    return f"{number:.{digits}f}".rstrip("0").rstrip(".")


def flag(value: bool) -> str:
    return "1" if value else "0"


def percentile_map(rows: list[dict[str, Any]], field: str, *, higher_is_better: bool = True) -> dict[str, float]:
    values: list[tuple[str, float]] = []
    for row in rows:
        value = finite(row.get(field))
        ticker = str(row.get("ticker") or "")
        if ticker and value is not None:
            values.append((ticker, value))
    if not values:
        return {}
    values.sort(key=lambda item: item[1])
    if len(values) == 1:
        return {values[0][0]: NEUTRAL_SCORE}
    out: dict[str, float] = {}
    index = 0
    while index < len(values):
        end = index + 1
        while end < len(values) and values[end][1] == values[index][1]:
            end += 1
        average_rank = (index + end - 1) / 2.0
        pct = 100.0 * average_rank / (len(values) - 1)
        score = pct if higher_is_better else 100.0 - pct
        for ticker, _ in values[index:end]:
            out[ticker] = score
        index = end
    return out


@dataclass(frozen=True)
class Component:
    score: float
    quality: float
    status: str
    available: int
    missing: int


def component(
    ticker: str,
    fields: list[str],
    score_maps: dict[str, dict[str, float]],
    *,
    extra_scores: list[float] | None = None,
) -> Component:
    scores: list[float] = []
    missing = 0
    for field in fields:
        value = score_maps.get(field, {}).get(ticker)
        if value is None:
            missing += 1
        else:
            scores.append(value)
    for score in extra_scores or []:
        if math.isfinite(score):
            scores.append(score)
    available = len(scores)
    total = available + missing
    if not scores:
        return Component(NEUTRAL_SCORE, 0.0, "missing_neutralized", 0, total)
    quality = available / total if total else 0.0
    status = "complete" if missing == 0 else "partial"
    return Component(sum(scores) / len(scores), quality, status, available, missing)


def quality_value(status: str, *, kind: str) -> float:
    text = str(status or "").strip().lower()
    if kind == "market":
        return 1.0 if text == "complete" else 0.65 if text == "review" else 0.5
    if kind == "financial":
        value = finite(status)
        if value is not None:
            return max(0.0, min(1.0, value))
        return 1.0 if text == "complete" else 0.5
    if kind == "positioning":
        return 1.0 if text == "complete" else 0.75 if text == "policy_exempt" else 0.4
    return 0.5


def select_effective_policies(
    policies: dict[tuple[str, str], dict[str, str]],
    *,
    asof: str,
) -> dict[tuple[str, str], dict[str, str]]:
    """Keep only policy rows effective at the evaluation asof (valid_from <= asof, same-day-inclusive).

    Rows without a valid_from are treated as always effective (legacy rows predating the PIT
    columns). A malformed non-empty valid_from is a contract violation and fails loudly.
    """
    effective: dict[tuple[str, str], dict[str, str]] = {}
    for key, row in policies.items():
        valid_from = str(row.get("valid_from") or "").strip()
        if valid_from:
            try:
                valid_from = datetime.strptime(valid_from[:10], "%Y-%m-%d").date().isoformat()
            except ValueError as exc:
                raise ValueError(f"Policy row {key} has malformed valid_from={valid_from!r}") from exc
            if valid_from > asof:
                continue
        effective[key] = row
    return effective


def resolve_eligibility_policy_path(config: dict[str, Any], *, base_dir: Path) -> Path:
    """Resolve the eligibility policy CSV: per-family key first, legacy flat key as fallback (NEW-3).

    Mirrors scripts/10's resolve_eligibility_policy_path so the validator and the
    publisher can never read different policy CSVs from the same config.
    """
    family_key = f"scoring_policy.families.{MODEL_FAMILY}.eligibility_policy_csv"
    policy_path_raw = str(cfg_get(config, family_key, "") or "").strip()
    if not policy_path_raw:
        policy_path_raw = str(cfg_get(config, "scoring_policy.defense_eligibility_policy_csv", "") or "").strip()
    if not policy_path_raw:
        raise ValueError(
            f"Missing eligibility policy CSV config for model_family={MODEL_FAMILY}: set {family_key} "
            "(legacy scoring_policy.defense_eligibility_policy_csv is also absent)"
        )
    policy_path = resolve_path(policy_path_raw, base_dir=base_dir)
    if not policy_path.exists():
        raise FileNotFoundError(f"Eligibility policy CSV not found for model_family={MODEL_FAMILY}: {policy_path}")
    return policy_path


def header(project_root: Path) -> list[str]:
    return defense_final_rank_header(project_root)


def fetch_map(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> dict[str, dict[str, Any]]:
    rows = conn.execute(sql, params).fetchall()
    return {normalize_ticker(row["ticker"]): dict(row) for row in rows if normalize_ticker(row["ticker"])}


def parse_source_list(raw: Any) -> list[str]:
    if isinstance(raw, str):
        values = [part.strip() for part in raw.split(",")]
    else:
        values = [str(part).strip() for part in (raw or [])]
    return [value for value in values if value]


def source_priority_list(primary_source: str, fallback_sources: list[str]) -> list[str]:
    out: list[str] = []
    for source_id in [primary_source, *fallback_sources]:
        if source_id and source_id not in out:
            out.append(source_id)
    if not out:
        raise ValueError("At least one market source_id is required")
    return out


def source_rank_case(source_ids: list[str], column: str = "source_id") -> str:
    whens = " ".join(f"WHEN ? THEN {rank}" for rank in range(len(source_ids)))
    return f"CASE {column} {whens} ELSE 99 END"


def placeholders(values: list[str]) -> str:
    if not values:
        raise ValueError("values cannot be empty")
    return ",".join("?" for _ in values)


def select_effective_capacity_overrides(
    path: Path,
    *,
    asof: str,
) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    selected: dict[str, dict[str, str]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            ticker = normalize_ticker(row.get("ticker"))
            if not ticker:
                continue
            valid_from = str(row.get("valid_from") or "").strip()
            if valid_from:
                valid_from = datetime.strptime(valid_from[:10], "%Y-%m-%d").date().isoformat()
                if valid_from > asof:
                    continue
            current = selected.get(ticker)
            current_valid_from = str(current.get("valid_from") or "") if current else ""
            if current is None or valid_from >= current_valid_from:
                selected[ticker] = {str(k): str(v or "") for k, v in row.items()}
    return selected


def load_rows(
    conn: sqlite3.Connection,
    *,
    asof: str,
    config: dict[str, Any],
    base_dir: Path,
    membership_mode: str,
) -> list[dict[str, Any]]:
    market_source = str(cfg_get(config, "market_feature_build.source_id", "yahoo_finance_adjusted"))
    market_sources = source_priority_list(
        market_source,
        parse_source_list(cfg_get(config, "market_data_policy.scoring_fallback_sources", [])),
    )
    financial_source = str(cfg_get(config, "sec_fundamentals.companyfacts_source_id", "sec_companyfacts"))
    positioning_source = str(cfg_get(config, "positioning_import.source_id", "industrials_positioning_composite"))
    submissions_source = str(cfg_get(config, "sec_fundamentals.submissions_source_id", "sec_submissions"))
    capacity_override_path = resolve_path(
        str(cfg_get(config, "rank_export.defense_capacity_overrides_csv", "defense/system_csvs/defense_capacity_overrides.csv")),
        base_dir=base_dir,
    )
    capacity_overrides = select_effective_capacity_overrides(capacity_override_path, asof=asof)
    if membership_mode == "current":
        active = fetch_map(
            conn,
            """
            SELECT c.ticker, c.company_name, c.sector, c.industry, c.subsector, c.country, c.currency,
                   c.universe_status, t.calibration_cohort_id, t.calibration_cohort, t.development_stage,
                   m.membership_source_id AS historical_universe_source, m.start_date AS price_start_date,
                   m.end_date AS terminal_date
            FROM dim_company c
            JOIN dim_industrials_taxonomy t ON t.company_id = c.company_id AND t.model_family = ?
            LEFT JOIN dim_universe_membership m
              ON m.company_id = c.company_id AND m.model_family = t.model_family AND m.is_current_member = 1
            WHERE c.is_active = 1
            ORDER BY c.ticker
            """,
            (MODEL_FAMILY,),
        )
    else:
        active = fetch_map(
            conn,
            """
            SELECT c.ticker, c.company_name, c.sector, c.industry, c.subsector, c.country, c.currency,
                   COALESCE(NULLIF(m.membership_status, ''), c.universe_status) AS universe_status,
                   t.calibration_cohort_id, t.calibration_cohort, t.development_stage,
                   m.membership_source_id AS historical_universe_source, m.start_date AS price_start_date,
                   m.end_date AS terminal_date
            FROM dim_universe_membership m
            JOIN dim_company c ON c.company_id = m.company_id
            JOIN dim_industrials_taxonomy t ON t.company_id = c.company_id AND t.model_family = m.model_family
            WHERE m.model_family = ?
              AND m.point_in_time_flag = 1
              AND m.start_date <= ?
              AND COALESCE(m.end_date, '9999-12-31') >= ?
            ORDER BY c.ticker
            """,
            (MODEL_FAMILY, asof, asof),
        )
    market = fetch_map(
        conn,
        f"""
        WITH ranked AS (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY ticker
                       ORDER BY {source_rank_case(market_sources)} ASC
                   ) AS rn
            FROM feature_market_technical
            WHERE model_family = ?
              AND source_id IN ({placeholders(market_sources)})
              AND asof_date = ?
        )
        SELECT * FROM ranked WHERE rn = 1
        """,
        (*market_sources, MODEL_FAMILY, *market_sources, asof),
    )
    snapshots = fetch_map(
        conn,
        f"""
        WITH ranked AS (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY ticker
                       ORDER BY {source_rank_case(market_sources)} ASC
                   ) AS rn
            FROM fact_market_snapshot
            WHERE source_id IN ({placeholders(market_sources)})
              AND asof_date = ?
        )
        SELECT * FROM ranked WHERE rn = 1
        """,
        (*market_sources, *market_sources, asof),
    )
    financial = fetch_map(
        conn,
        "SELECT * FROM feature_financial_statement WHERE model_family = ? AND source_id = ? AND asof_date = ?",
        (MODEL_FAMILY, financial_source, asof),
    )
    positioning = fetch_map(
        conn,
        "SELECT * FROM feature_positioning WHERE model_family = ? AND source_id = ? AND asof_date = ?",
        (MODEL_FAMILY, positioning_source, asof),
    )
    profiles = fetch_map(
        conn,
        """
        SELECT *
        FROM dim_issuer_reporting_profile
        WHERE model_family = ?
          AND source_id IN (?, ?)
        ORDER BY ticker
        """,
        (MODEL_FAMILY, financial_source, submissions_source),
    )
    active_tickers = sorted(active)
    active_ticker_placeholders = placeholders(active_tickers)
    price_dates = fetch_map(
        conn,
        f"""
        SELECT ticker, MIN(bar_date) AS price_start_date, MAX(bar_date) AS price_end_date
        FROM fact_price_ohlcv
        WHERE source_id IN ({placeholders(market_sources)})
          AND ticker IN ({active_ticker_placeholders})
        GROUP BY ticker
        """,
        (*market_sources, *active_tickers),
    )
    fallback_volume_tickers = sorted(
        ticker
        for ticker in active_tickers
        if finite(market.get(ticker, {}).get("avg_dollar_volume_60d")) is None
    )
    available_dollar_volume: dict[str, dict[str, Any]] = {}
    if fallback_volume_tickers:
        available_dollar_volume = fetch_map(
            conn,
            f"""
            WITH dedup AS (
                SELECT ticker, bar_date, close, volume,
                       ROW_NUMBER() OVER (
                           PARTITION BY ticker, bar_date
                           ORDER BY {source_rank_case(market_sources)} ASC
                       ) AS source_rn
                FROM fact_price_ohlcv
                WHERE source_id IN ({placeholders(market_sources)})
                  AND ticker IN ({placeholders(fallback_volume_tickers)})
                  AND bar_date <= ?
                  AND close IS NOT NULL
                  AND volume IS NOT NULL
                  AND close > 0
                  AND volume >= 0
            ),
            ranked AS (
                SELECT ticker, close, volume,
                       ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY bar_date DESC) AS rn
                FROM dedup
                WHERE source_rn = 1
            )
            SELECT ticker,
                   COUNT(*) AS available_dollar_volume_days,
                   AVG(close * volume) AS avg_dollar_volume_available_window
            FROM ranked
            WHERE rn <= 60
            GROUP BY ticker
            """,
            (
                *market_sources,
                *market_sources,
                *fallback_volume_tickers,
                asof,
            ),
        )
    filing_urls = {
        (str(row["ticker"]), str(row["accession_number"])): dict(row)
        for row in conn.execute(
            f"""
            SELECT ticker, accession_number, form_type, filing_date, filing_url
            FROM fact_sec_filing
            WHERE source_id = ?
              AND ticker IN ({active_ticker_placeholders})
            """,
            (submissions_source, *active_tickers),
        )
    }
    rows: list[dict[str, Any]] = []
    for ticker, base in active.items():
        merged: dict[str, Any] = dict(base)
        merged.update({f"market_{k}": v for k, v in market.get(ticker, {}).items()})
        merged.update({f"snapshot_{k}": v for k, v in snapshots.get(ticker, {}).items()})
        merged.update({f"financial_{k}": v for k, v in financial.get(ticker, {}).items()})
        merged.update({f"positioning_{k}": v for k, v in positioning.get(ticker, {}).items()})
        merged.update({f"profile_{k}": v for k, v in profiles.get(ticker, {}).items()})
        merged.update({f"capacity_{k}": v for k, v in capacity_overrides.get(ticker, {}).items()})
        merged.update({f"market_window_{k}": v for k, v in available_dollar_volume.get(ticker, {}).items()})
        merged["price_start_date"] = price_dates.get(ticker, {}).get("price_start_date") or merged.get("price_start_date") or ""
        merged["price_end_date"] = price_dates.get(ticker, {}).get("price_end_date") or ""
        accession = str(merged.get("financial_accession_number") or "")
        filing = filing_urls.get((ticker, accession), {})
        filing_date = str(filing.get("filing_date") or "").strip()
        profile_date = str(merged.get("profile_latest_filing_date") or "").strip()
        if filing_date:
            merged["latest_sec_form"] = merged.get("financial_form_type") or filing.get("form_type") or ""
            merged["latest_sec_filing_date"] = filing_date
            merged["latest_sec_url"] = filing.get("filing_url") or ""
        elif date_on_or_before(profile_date, asof):
            merged["latest_sec_form"] = merged.get("financial_form_type") or merged.get("profile_latest_form_type") or ""
            merged["latest_sec_filing_date"] = profile_date
            merged["latest_sec_url"] = ""
        else:
            # The reporting profile stores the latest known SEC filing as of
            # ingestion time. For historical PIT snapshots it can be years in
            # the future, so do not export it as row-level PIT evidence.
            merged["latest_sec_form"] = merged.get("financial_form_type") or ""
            merged["latest_sec_filing_date"] = ""
            merged["latest_sec_url"] = ""
        rows.append(merged)
    return rows


def first_finite(*values: Any) -> float | None:
    for value in values:
        number = finite(value)
        if number is not None:
            return number
    return None


def market_cap_export_value(row: dict[str, Any]) -> tuple[float | None, str, str]:
    snapshot_market_cap = finite(row.get("snapshot_market_cap"))
    if snapshot_market_cap is not None:
        return snapshot_market_cap, "fact_market_snapshot.market_cap", ""
    financial_market_cap = finite(row.get("financial_market_cap"))
    if financial_market_cap is not None:
        return financial_market_cap, "feature_financial_statement.market_cap", ""

    price = first_finite(
        row.get("snapshot_regular_market_price"),
        row.get("financial_latest_price"),
        row.get("market_latest_close"),
        row.get("market_latest_adj_close"),
    )
    diluted_shares = finite(row.get("financial_diluted_shares"))
    if price is not None and diluted_shares is not None and diluted_shares > 0:
        return price * diluted_shares, "computed_pit_price_x_diluted_shares", ""

    short_shares = finite(row.get("positioning_latest_short_interest_shares"))
    short_pct_float = finite(row.get("positioning_latest_short_interest_pct_float"))
    if price is not None and short_shares is not None and short_pct_float is not None and short_pct_float > 0:
        implied_float_shares = short_shares / short_pct_float
        if implied_float_shares > 0:
            return (
                price * implied_float_shares,
                "computed_pit_price_x_short_interest_float_proxy",
                "market_cap_share_count_proxy_from_short_interest_pct_float",
            )

    override_market_cap = finite(row.get("capacity_market_cap_value"))
    override_source = str(row.get("capacity_market_cap_source") or "").strip()
    override_reason = str(row.get("capacity_market_cap_reason") or "market_cap_from_capacity_override").strip()
    if override_market_cap is not None and override_market_cap > 0:
        return override_market_cap, override_source or "capacity_override_market_cap_value", override_reason
    override_shares = finite(row.get("capacity_market_cap_share_count"))
    if price is not None and override_shares is not None and override_shares > 0:
        return (
            price * override_shares,
            override_source or "computed_pit_price_x_capacity_override_shares",
            override_reason or "market_cap_share_count_from_capacity_override",
        )

    reasons: list[str] = []
    if price is None:
        reasons.append("market_cap_unavailable_missing_pit_price")
    if diluted_shares is None or diluted_shares <= 0:
        reasons.append("market_cap_unavailable_missing_share_count")
    reasons.append("market_cap_unavailable_missing_capacity_override")
    return None, "", ";".join(reasons)


def liquidity_capacity_reason(row: dict[str, Any], *, market_cap_reason: str, avg_dollar_volume_reason: str = "") -> str:
    reasons: list[str] = []
    if market_cap_reason:
        reasons.append(market_cap_reason)
    if avg_dollar_volume_reason:
        reasons.append(avg_dollar_volume_reason)
    elif finite(row.get("avg_dollar_volume_60d")) is None:
        days = finite(row.get("market_trading_days_available"))
        if days is not None and days < 60:
            reasons.append(f"avg_dollar_volume_60d_unavailable_insufficient_history_{int(days)}_of_60")
        else:
            reasons.append("avg_dollar_volume_60d_unavailable_missing_price_or_volume_window")
    return ";".join(reason for reason in reasons if reason)


def add_feature_aliases(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        row["ticker"] = normalize_ticker(row.get("ticker"))
        row["low_liquidity_flag"] = int(finite(row.get("market_low_liquidity_flag")) or 0)
        row["share_count_yoy_growth"] = ""
        row["latest_price"] = first_finite(
            row.get("snapshot_regular_market_price"),
            row.get("financial_latest_price"),
            row.get("market_latest_close"),
            row.get("market_latest_adj_close"),
        )
        market_cap, market_cap_source, market_cap_reason = market_cap_export_value(row)
        row["market_cap"] = market_cap
        row["market_cap_source"] = market_cap_source
        row["_market_cap_reason"] = market_cap_reason
        for out_name, source_name in BASE_FEATURE_ALIASES.items():
            if out_name != "latest_price":
                row[out_name] = row.get(source_name)
        row["liquidity_capacity_reason"] = liquidity_capacity_reason(row, market_cap_reason=market_cap_reason)


def add_specialized_feature_aliases(rows: list[dict[str, Any]]) -> None:
    """Resolve overlapping disclosure families into four independent demand signals.

    The reviewed parser can populate funded backlog, reported backlog, RPO, or a
    contract-load proxy depending on issuer disclosure. Choosing one value per
    economic dimension avoids rewarding issuers merely for reporting the same
    backlog through several labels.
    """
    for row in rows:
        row["defense_orders_growth"] = finite(row.get("financial_orders_yoy_growth"))
        row["defense_backlog_growth"] = first_finite(
            row.get("financial_backlog_yoy_growth"),
            row.get("financial_reported_backlog_yoy_growth"),
            row.get("financial_rpo_yoy_growth"),
            row.get("financial_contract_load_proxy_yoy_growth"),
        )
        row["defense_backlog_coverage"] = first_finite(
            row.get("financial_backlog_to_revenue"),
            row.get("financial_reported_backlog_to_revenue"),
            row.get("financial_rpo_to_revenue"),
            row.get("financial_contract_load_proxy_to_revenue"),
        )
        row["defense_book_to_bill"] = first_finite(
            row.get("financial_book_to_bill"),
            row.get("financial_rpo_implied_book_to_bill"),
        )


def apply_export_capacity_fallbacks(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        avg_reason = ""
        if finite(row.get("avg_dollar_volume_60d")) is None:
            available_avg = finite(row.get("market_window_avg_dollar_volume_available_window"))
            available_days = finite(row.get("market_window_available_dollar_volume_days"))
            if available_avg is not None and available_days is not None and available_days > 0:
                row["avg_dollar_volume_60d"] = available_avg
                avg_reason = f"avg_dollar_volume_60d_available_history_proxy_{int(available_days)}_of_60"
        row["liquidity_capacity_reason"] = liquidity_capacity_reason(
            row,
            market_cap_reason=str(row.get("_market_cap_reason") or ""),
            avg_dollar_volume_reason=avg_reason,
        )


def build_scores(
    rows: list[dict[str, Any]],
    *,
    scoring_mode: str = "baseline",
) -> dict[str, dict[str, Any]]:
    if scoring_mode not in {"baseline", "specialized_v1"}:
        raise ValueError(f"Unsupported defense scoring_mode={scoring_mode!r}")
    add_feature_aliases(rows)
    if scoring_mode == "specialized_v1":
        add_specialized_feature_aliases(rows)
    score_maps: dict[str, dict[str, float]] = {}
    for field in HIGHER_IS_BETTER_SCORE_FIELDS:
        score_maps[field] = percentile_map(rows, field, higher_is_better=True)
    for field in LOWER_IS_BETTER_SCORE_FIELDS:
        score_maps[field] = percentile_map(rows, field, higher_is_better=False)
    score_maps["max_drawdown_12m"] = percentile_map(rows, "max_drawdown_12m", higher_is_better=True)
    if scoring_mode == "specialized_v1":
        for field in SPECIALIZED_PILLAR_FIELDS:
            score_maps[field] = percentile_map(rows, field, higher_is_better=True)
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        ticker = str(row["ticker"])
        low_liquidity_score = 0.0 if int(row.get("low_liquidity_flag") or 0) else 100.0
        valuation = component(ticker, list(PILLAR_INPUT_FIELDS["valuation"]), score_maps)
        quality = component(
            ticker,
            list(PILLAR_INPUT_FIELDS["quality"]),
            score_maps,
        )
        risk = component(
            ticker,
            list(PILLAR_INPUT_FIELDS["risk_control"]),
            score_maps,
            extra_scores=[low_liquidity_score],
        )
        positioning = component(
            ticker,
            list(PILLAR_INPUT_FIELDS["positioning"]),
            score_maps,
        )
        market_behavior = component(
            ticker,
            list(PILLAR_INPUT_FIELDS["market_behavior"]),
            score_maps,
        )
        growth = component(
            ticker,
            list(PILLAR_INPUT_FIELDS["growth"]),
            score_maps,
        )
        defense_budget_backlog = (
            component(ticker, list(SPECIALIZED_PILLAR_FIELDS), score_maps)
            if scoring_mode == "specialized_v1"
            else Component(NEUTRAL_SCORE, 0.0, "neutralized_not_loaded", 0, 1)
        )
        if scoring_mode == "specialized_v1":
            if defense_budget_backlog.available == 0:
                defense_budget_backlog = Component(
                    NEUTRAL_SCORE,
                    0.0,
                    "candidate_specialized_missing_neutralized",
                    0,
                    len(SPECIALIZED_PILLAR_FIELDS),
                )
            else:
                defense_budget_backlog = Component(
                    defense_budget_backlog.score,
                    defense_budget_backlog.quality,
                    (
                        "candidate_specialized_complete"
                        if defense_budget_backlog.missing == 0
                        else "candidate_specialized_partial"
                    ),
                    defense_budget_backlog.available,
                    defense_budget_backlog.missing,
                )
        components = [valuation, quality, risk, positioning, market_behavior, growth]
        weighted = [
            (valuation.score, 0.16), (quality.score, 0.18), (risk.score, 0.18),
            (positioning.score, 0.12), (market_behavior.score, 0.20), (growth.score, 0.16),
        ]
        total_weight = sum(weight for comp, weight in zip(components, [w for _, w in weighted]) if comp.available > 0)
        core = (
            sum(score * weight for (score, weight), comp in zip(weighted, components) if comp.available > 0) / total_weight
            if total_weight > 0
            else NEUTRAL_SCORE
        )
        overlay = NEUTRAL_SCORE
        final_score = max(0.0, min(100.0, 0.9 * core + 0.1 * overlay))
        out[ticker] = {
            "valuation": valuation,
            "quality": quality,
            "risk_control": risk,
            "positioning": positioning,
            "market_behavior": market_behavior,
            "growth": growth,
            "sector_cycle": Component(NEUTRAL_SCORE, 0.0, "neutralized_not_loaded", 0, 1),
            "defense_budget_backlog": defense_budget_backlog,
            "core_score": core,
            "sector_overlay_score": overlay,
            "final_score": final_score,
            "available": sum(comp.available for comp in components),
            "missing": sum(comp.missing for comp in components),
        }
    return out


def rank_percentiles(scores: dict[str, dict[str, Any]]) -> dict[str, tuple[int, float]]:
    ordered = sorted(scores.items(), key=lambda item: (-float(item[1]["final_score"]), item[0]))
    out: dict[str, tuple[int, float]] = {}
    n = len(ordered)
    for idx, (ticker, _) in enumerate(ordered, start=1):
        pct = 100.0 if n == 1 else 100.0 * (n - idx) / (n - 1)
        out[ticker] = (idx, pct)
    return out


def compose_rows(
    rows: list[dict[str, Any]],
    scores: dict[str, dict[str, Any]],
    policies: dict[tuple[str, str], dict[str, str]],
    asof: str,
    *,
    provenance_version: str,
    score_model_version: str,
    membership_mode: str,
    borrow_asof_by_ticker: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    ranks = rank_percentiles(scores)
    out: list[dict[str, str]] = []
    unmatched: list[tuple[str, str, str]] = []
    is_pit_membership = membership_mode == "pit"
    stage11_source = (
        PANEL_SOURCE_SURVIVORSHIP_CORRECTED if is_pit_membership else PANEL_SOURCE_CURRENT_UNIVERSE_REPLAY
    )
    for row in rows:
        ticker = str(row["ticker"])
        score = scores[ticker]
        rank, pct = ranks[ticker]
        market_quality = quality_value(str(row.get("market_market_data_quality") or ""), kind="market")
        positioning_quality = quality_value(str(row.get("positioning_positioning_quality") or ""), kind="positioning")
        # NEW-4 / EL-9 parity with scripts/10: the issuer-profile (overrides) row wins for
        # BOTH profile and confidence — never a profile from one source graded against the
        # other source's confidence — and the stage comes from the taxonomy, not the feature row.
        profile_row_profile = str(row.get("profile_reporting_profile") or "").strip()
        feature_row_profile = str(row.get("financial_reporting_profile") or "").strip()
        if profile_row_profile:
            profile = profile_row_profile
            financial_confidence = quality_value(str(row.get("profile_financial_confidence") or ""), kind="financial")
        elif feature_row_profile:
            profile = feature_row_profile
            financial_confidence = quality_value(str(row.get("financial_financial_confidence") or ""), kind="financial")
        else:
            profile = "NO_FINANCIALS_REVIEW"
            financial_confidence = quality_value("", kind="financial")
        confidence = (market_quality + financial_confidence + positioning_quality) / 3.0
        development_stage = str(row.get("development_stage") or "operating")
        policy = resolve_policy(policies, profile, development_stage)
        if policy is None:
            unmatched.append((ticker, profile, development_stage))
            continue
        rank_policy = str(policy.get("rank_ready_policy") or "")
        min_conf = finite(policy.get("minimum_financial_confidence")) or 0.0
        rank_ready = (
            rank_policy.startswith("eligible")
            and financial_confidence >= min_conf
            and str(row.get("financial_data_quality_status") or "") == "complete"
            and str(row.get("positioning_positioning_quality") or "") in {"complete", "policy_exempt"}
        )
        review_reasons = []
        if not rank_ready:
            review_reasons.append(str(policy.get("review_reason") or rank_policy or "not_rank_ready"))
        if str(row.get("market_market_data_quality") or "") == "review":
            review_reasons.append("market_data_review")
        if str(row.get("positioning_positioning_quality") or "") == "policy_exempt":
            review_reasons.append("positioning_policy_exempt")
        model_status = "complete" if rank_ready else "review"
        calibration_reason = "shadow_only_oos_calibration_not_available"
        record: dict[str, str] = {
            "ticker": ticker,
            "asof_date": asof,
            "score_model_version": score_model_version,
            "model_family": MODEL_FAMILY,
            "model_version": MODEL_VERSION,
            "scoring_contract_version": SCORING_CONTRACT_VERSION,
            "company_name": str(row.get("company_name") or ""),
            "sector": "Industrials",
            "industry": "Aerospace & Defense",
            "subsector": "Defense",
            "country": str(row.get("country") or "United States"),
            "currency": str(row.get("currency") or "USD"),
            "universe_status": str(row.get("universe_status") or "active"),
            "historical_universe_source": str(row.get("historical_universe_source") or ""),
            "price_start_date": str(row.get("price_start_date") or ""),
            "price_end_date": str(row.get("price_end_date") or ""),
            "terminal_date": str(row.get("terminal_date") or ""),
            "historical_price_ticker": ticker,
            "latest_price_date": str(row.get("market_latest_bar_date") or ""),
            "market_feature_asof_date": str(row.get("market_asof_date") or asof),
            "financial_feature_asof_date": str(row.get("financial_asof_date") or asof),
            "positioning_feature_asof_date": str(row.get("positioning_asof_date") or asof),
            "final_rank": str(rank),
            "final_percentile": fmt(pct, 4),
            "final_score": fmt(score["final_score"]),
            "core_score": fmt(score["core_score"]),
            "sector_overlay_score": fmt(score["sector_overlay_score"]),
            "data_quality_confidence": fmt(confidence, 4),
            "rank_ready_flag": flag(rank_ready),
            "calibration_eligible_flag": "0",
            "model_status": model_status,
            "review_reason": ";".join(reason for reason in review_reasons if reason),
            "calibration_cohort_id": str(row.get("calibration_cohort_id") or ""),
            "calibration_cohort": str(row.get("calibration_cohort") or ""),
            "market_quality": fmt(market_quality, 4),
            "financial_quality": fmt(financial_confidence, 4),
            "positioning_quality": fmt(positioning_quality, 4),
            "core_available_component_count": str(score["available"]),
            "core_missing_component_count": str(score["missing"]),
            "core_data_quality_confidence": fmt(confidence, 4),
            "latest_sec_form": str(row.get("latest_sec_form") or ""),
            "latest_sec_filing_date": str(row.get("latest_sec_filing_date") or ""),
            "latest_sec_url": str(row.get("latest_sec_url") or ""),
            "calibration_usage": "shadow_only",
            "calibration_input_valid_flag": "0",
            "oos_score_valid_flag": "0",
            "oos_score_asof_date": "",
            "oos_invalid_reason": calibration_reason,
            "feature_point_in_time_flag": "1",
            "future_return_excluded_flag": "1",
            "non_point_in_time_sections_omitted_flag": "1",
            "scoring_weights_frozen_flag": "1",
            "calibration_train_start_date": "",
            "calibration_train_end_date": "",
            "calibration_lock_date": "",
            "calibration_production_start_date": "",
            "calibration_validation_method": "not_available_shadow_only",
            "calibration_provenance_version": provenance_version,
            "oos_assertion_basis": "not_available_shadow_only",
            "portfolio_candidate_gate": "0",
            "portfolio_candidate_score": fmt(score["final_score"]),
            "portfolio_candidate_status": "shadow_only",
            "portfolio_candidate_reason": calibration_reason,
            "research_calibration_input_eligible_flag": "0",
            "research_calibration_eligible_flag": "0",
            "research_calibration_status": "shadow_only",
            "research_calibration_reason": calibration_reason,
            "calibration_sample_role": "excluded",
            "calibration_status": "shadow_only",
            "calibration_status_reason": calibration_reason,
            "survivorship_corrected_panel_flag": flag(is_pit_membership),
            "stage11_calibration_panel_source": stage11_source,
            "stage11_calibration_input_eligible_flag": "0",
            "stage11_calibration_input_reason": calibration_reason,
            "score_scale_min": "0",
            "score_scale_max": "100",
            "score_neutral_value": "50",
            "score_confidence": fmt(confidence, 4),
            "eligibility_reason": "shadow_only_oos_pending" if rank_ready else ";".join(review_reasons) or "not_rank_ready",
            "native_score_field": "final_score",
            "native_score_value": fmt(score["final_score"]),
            "score_zero_is_missing_flag": "0",
            "calibration_only": "0",
            "recovery_type": "",
            "equity_recovery": "",
            "drop_otc_tape": "0",
            "source_snapshot_asof_date": asof,
            "price_data_asof_date": str(row.get("market_latest_bar_date") or ""),
            "feature_data_asof_date": asof,
            "financial_data_asof_date": str(row.get("financial_asof_date") or asof),
            "short_interest_asof_date": asof if row.get("positioning_latest_short_interest_shares") is not None else "",
            "institutional_data_asof_date": asof if row.get("positioning_latest_institutional_shares") is not None else "",
            "insider_data_asof_date": asof,
            # The borrow OBSERVATION date, not the feature asof: within the
            # staleness tolerance the feature build carries the newest borrow
            # row at-or-before asof, so a carried value must be dated by that
            # observation instead of masquerading as same-day data.
            "borrow_data_asof_date": (
                (borrow_asof_by_ticker or {}).get(ticker, "")
                if row.get("positioning_latest_borrow_fee_rate") is not None
                else ""
            ),
            "forward_catalyst_event_date": "",
            "forward_catalyst_event_type": "",
            "forward_catalyst_nearest_days": "",
            "forward_catalyst_source": "",
            "forward_catalyst_confidence": "",
            "forward_catalyst_asof_date": "",
            "market_cap_source": str(row.get("market_cap_source") or ""),
            "liquidity_capacity_reason": str(row.get("liquidity_capacity_reason") or ""),
        }
        for field in [
            "market_cap", "latest_price", "revenue_yoy_growth", "gross_profit_yoy_growth",
            "operating_income_yoy_growth", "free_cash_flow_yoy_growth", "revenue_acceleration",
            "gross_margin", "operating_margin", "fcf_margin", "fcf_to_net_income",
            "net_cash_to_assets", "sbc_pct_revenue", "r_and_d_pct_revenue",
            "share_count_yoy_growth", "inventory_days", "fcf_yield", "ev_gross_profit",
            "ev_operating_income", "ret_3m", "ret_12m_ex_1m", "rel_strength_bench_3m",
            "realized_vol_60d", "max_drawdown_12m", "distance_from_52w_high",
            "avg_dollar_volume_60d", "low_liquidity_flag", "insider_net_value_90d",
            "insider_cluster_buyers_90d", "institutional_ownership_delta_pct",
            "latest_short_interest_pct_float", "short_interest_change_3m", "latest_days_to_cover",
            "latest_borrow_fee_rate",
        ]:
            record[field] = fmt(row.get(field)) if field != "low_liquidity_flag" else str(row.get(field) or 0)
        for name in [
            "valuation", "quality", "risk_control", "positioning", "market_behavior",
            "growth", "sector_cycle", "defense_budget_backlog",
        ]:
            comp = score[name]
            record[f"{name}_score"] = fmt(comp.score)
            if name != "positioning":
                record[f"{name}_quality"] = fmt(comp.quality, 4)
            record[f"{name}_status"] = comp.status
        out.append(record)
    if unmatched:
        raise ValueError(
            "Missing scoring eligibility policy rows for (profile, stage) combos effective at "
            f"asof={asof}: {sorted(set((profile, stage) for _, profile, stage in unmatched))} "
            f"(tickers: {sorted(ticker for ticker, _, _ in unmatched)[:20]})"
        )
    out.sort(key=lambda record: (int(record["final_rank"]), record["ticker"]))
    return out


def production_candidate(row: dict[str, str]) -> bool:
    return (
        str(row.get("rank_ready_flag") or "") == "1"
        and str(row.get("model_status") or "").strip().lower() == "complete"
        and finite(row.get("final_score")) is not None
    )


def production_noncandidate_reason(row: dict[str, str]) -> str:
    reason = str(row.get("review_reason") or row.get("eligibility_reason") or "").strip()
    if reason and reason.lower() not in {"ok", "shadow_only_oos_pending"}:
        return reason[:240]
    if str(row.get("rank_ready_flag") or "") != "1":
        return "not_rank_ready"
    if str(row.get("model_status") or "").strip().lower() != "complete":
        return "model_incomplete"
    if finite(row.get("final_score")) is None:
        return "missing_score"
    return "not_portfolio_candidate"


def apply_lock_stamps(
    rows: list[dict[str, str]],
    *,
    lock: dict[str, Any],
    asof: str,
    mode: str,
    is_pit_membership: bool,
) -> list[dict[str, str]]:
    """Restamp composed rows under the sealed production calibration.

    Score definition is IDENTICAL to scripts/27 (weighted_score over the pillar
    columns with the sealed promotion weights), so backfilled history and the
    promoted production file share one score definition. Stamps by mode:
      production (asof >= production start): scripts/27-parity production_oos stamps.
      pre_lock  (asof <  production start): every production gate stays closed
        (oos=0, gate=0, calibration_eligible=0); rows are exposed to research only
        as pre_lock_research, and only when the snapshot is survivorship-corrected
        PIT membership — a current-universe replay of a historical date must never
        feed calibration (fail closed at the source AND in the portfolio adapter).
    """
    scored: list[tuple[dict[str, str], float]] = []
    for row in rows:
        score = weighted_score(row, lock["weights"])
        if score is None:
            fallback = finite(row.get("final_score"))
            if fallback is None:
                raise ValueError(f"{row.get('ticker')}: no production score and no shadow fallback score")
            score = max(0.0, min(100.0, fallback))
        row["final_score"] = fmt(score)
        row["native_score_field"] = "final_score"
        row["native_score_value"] = fmt(score)
        row["portfolio_candidate_score"] = fmt(score)
        scored.append((row, score))
    scored.sort(key=lambda item: (-item[1], str(item[0].get("ticker") or "")))
    total = len(scored)
    for rank, (row, _) in enumerate(scored, start=1):
        percentile = 100.0 if total == 1 else 100.0 * (total - rank) / (total - 1)
        row["final_rank"] = str(rank)
        row["final_percentile"] = fmt(percentile, 4)
        row["scoring_contract_version"] = PRODUCTION_SCORING_CONTRACT_VERSION
        row["scoring_weights_frozen_flag"] = "1"
        row["calibration_train_start_date"] = lock["train_start_date"]
        row["calibration_train_end_date"] = lock["train_end_date"]
        row["calibration_lock_date"] = lock["lock_date"]
        row["calibration_production_start_date"] = lock["production_start_date"]
        row["calibration_validation_method"] = lock["validation_method"]
        row["calibration_provenance_version"] = PRODUCTION_PROMOTION_STATUS
        if mode == "production":
            candidate = production_candidate(row)
            reason = "ok" if candidate else production_noncandidate_reason(row)
            oos_valid = finite(row.get("final_score")) is not None
            row["calibration_usage"] = "production_oos"
            row["calibration_input_valid_flag"] = "1" if candidate else "0"
            row["calibration_eligible_flag"] = "1" if candidate else "0"
            row["oos_score_valid_flag"] = "1" if oos_valid else "0"
            row["oos_score_asof_date"] = asof if oos_valid else ""
            row["oos_invalid_reason"] = "" if oos_valid else "missing_production_score"
            row["oos_assertion_basis"] = lock["validation_method"]
            row["portfolio_candidate_gate"] = "1" if candidate else "0"
            row["portfolio_candidate_status"] = "eligible" if candidate else "not_eligible"
            row["portfolio_candidate_reason"] = reason
            row["research_calibration_input_eligible_flag"] = "1" if candidate else "0"
            row["research_calibration_eligible_flag"] = row["research_calibration_input_eligible_flag"]
            row["research_calibration_status"] = PRODUCTION_PROMOTION_STATUS if candidate else "not_eligible"
            row["research_calibration_reason"] = reason
            row["calibration_sample_role"] = "strict_oos" if oos_valid else "excluded"
            row["calibration_status"] = PRODUCTION_PROMOTION_STATUS if candidate else "not_eligible"
            row["calibration_status_reason"] = reason
            row["stage11_calibration_input_eligible_flag"] = "1" if candidate else "0"
            row["stage11_calibration_input_reason"] = reason
            row["eligibility_reason"] = reason
        else:
            components_available = int(finite(row.get("core_available_component_count")) or 0)
            if not is_pit_membership:
                research_ok = False
                reason = "pre_lock_not_survivorship_corrected_use_pit_membership_replay"
            elif components_available <= 0:
                research_ok = False
                reason = "all_score_components_missing"
            else:
                research_ok = True
                reason = "ok"
            row["calibration_usage"] = "pre_lock_research"
            row["calibration_input_valid_flag"] = "1" if research_ok else "0"
            row["calibration_eligible_flag"] = "0"
            row["oos_score_valid_flag"] = "0"
            row["oos_score_asof_date"] = ""
            row["oos_invalid_reason"] = "pre_lock_research_window"
            row["oos_assertion_basis"] = "pre_lock_no_oos_assertion"
            row["portfolio_candidate_gate"] = "0"
            row["portfolio_candidate_status"] = "pre_lock_research"
            row["portfolio_candidate_reason"] = "pre_lock_research_window"
            row["research_calibration_input_eligible_flag"] = "1" if research_ok else "0"
            row["research_calibration_eligible_flag"] = row["research_calibration_input_eligible_flag"]
            row["research_calibration_status"] = "pre_lock_research" if research_ok else "not_eligible"
            row["research_calibration_reason"] = reason
            row["calibration_sample_role"] = "pre_lock_research" if research_ok else "excluded"
            row["calibration_status"] = "pre_lock_research" if research_ok else "not_eligible"
            row["calibration_status_reason"] = reason
            row["stage11_calibration_input_eligible_flag"] = "1" if research_ok else "0"
            row["stage11_calibration_input_reason"] = reason
            # eligibility_reason keeps the live-gate documentation compose_rows wrote
    return [row for row, _ in scored]


def apply_research_candidate_stamps(rows: list[dict[str, str]], *, scoring_mode: str) -> list[dict[str, str]]:
    """Fail closed when a candidate is evaluated after a production lock exists."""
    reason = f"research_candidate_{scoring_mode}_not_production_approved"
    for row in rows:
        row["calibration_usage"] = "research_candidate_only"
        row["calibration_input_valid_flag"] = "0"
        row["calibration_eligible_flag"] = "0"
        row["oos_score_valid_flag"] = "0"
        row["oos_score_asof_date"] = ""
        row["oos_invalid_reason"] = reason
        row["calibration_train_start_date"] = ""
        row["calibration_train_end_date"] = ""
        row["calibration_lock_date"] = ""
        row["calibration_production_start_date"] = ""
        row["calibration_validation_method"] = "candidate_research_not_production"
        row["oos_assertion_basis"] = "candidate_research_not_production"
        row["portfolio_candidate_gate"] = "0"
        row["portfolio_candidate_status"] = "research_candidate"
        row["portfolio_candidate_reason"] = reason
        row["research_calibration_input_eligible_flag"] = "0"
        row["research_calibration_eligible_flag"] = "0"
        row["research_calibration_status"] = "research_candidate"
        row["research_calibration_reason"] = reason
        row["calibration_sample_role"] = "excluded"
        row["calibration_status"] = "research_candidate"
        row["calibration_status_reason"] = reason
        row["stage11_calibration_input_eligible_flag"] = "0"
        row["stage11_calibration_input_reason"] = reason
    return rows


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = ""
    try:
        with NamedTemporaryFile("w", encoding="utf-8", newline="", dir=path.parent, delete=False) as handle:
            tmp_name = handle.name
            handle.write(text)
        os.replace(tmp_name, path)
    finally:
        if tmp_name and Path(tmp_name).exists():
            Path(tmp_name).unlink()


def sealed_artifact_valid(
    path: Path,
    *,
    asof: str,
    membership_mode: str,
    calibration_mode: str,
    scoring_mode: str,
    score_model_version: str,
    research_candidate: bool,
) -> bool:
    manifest_path = path.with_name("defense_final_rank_table_manifest.json")
    if not path.exists() or not manifest_path.exists():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    lineage_manifest = manifest.get("financial_filing_lineage")
    if not isinstance(lineage_manifest, dict):
        return False
    lineage_path_text = str(lineage_manifest.get("path") or "").strip()
    if not lineage_path_text:
        return False
    lineage_path = Path(lineage_path_text)
    if (
        not lineage_path.is_file()
        or lineage_manifest.get("sha256") != hashlib.sha256(lineage_path.read_bytes()).hexdigest()
        or lineage_manifest.get("acceptance") != "PASS"
        or int(lineage_manifest.get("blocking_issue_count", -1)) != 0
    ):
        return False
    expected_shadow_only = calibration_mode != "production"
    return (
        manifest.get("acceptance") == "PASS"
        and manifest.get("sha256") == digest
        and manifest.get("asof_date") == asof
        and manifest.get("model_family") == MODEL_FAMILY
        and manifest.get("membership_mode", "current") == membership_mode
        and manifest.get("shadow_only") is expected_shadow_only
        and manifest.get("calibration_mode", "shadow" if expected_shadow_only else "production") == calibration_mode
        and manifest.get("scoring_mode", "baseline") == scoring_mode
        and manifest.get("score_model_version") == score_model_version
        and bool(manifest.get("research_candidate", False)) is research_candidate
    )


def seal_manifest(
    path: Path,
    *,
    rows: int,
    asof: str,
    policy_asof: str,
    membership_mode: str,
    calibration_mode: str,
    scoring_mode: str,
    score_model_version: str,
    research_candidate: bool,
    financial_filing_lineage: dict[str, Any],
    lock: dict[str, Any] | None,
) -> Path:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {
        "acceptance": str(financial_filing_lineage["acceptance"]),
        "artifact": str(path),
        "asof_date": asof,
        "rows": rows,
        "sha256": digest,
        "score_model_version": score_model_version,
        "model_family": MODEL_FAMILY,
        "policy_asof_date": policy_asof,
        "membership_mode": membership_mode,
        "calibration_mode": calibration_mode,
        "scoring_mode": scoring_mode,
        "research_candidate": research_candidate,
        "shadow_only": research_candidate or calibration_mode != "production",
        "financial_filing_lineage": financial_filing_lineage,
        "sealed_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    if lock is not None:
        manifest.update(
            {
                "calibration_lock_date": lock["lock_date"],
                "calibration_production_start_date": lock["production_start_date"],
                "production_promotion_decision_manifest": lock["decision_manifest_path"],
                "production_promotion_decision_sha256": lock["decision_manifest_sha256"],
            }
        )
        if calibration_mode == "production" and not research_candidate:
            manifest["production_promoted"] = True
    manifest_path = path.with_name("defense_final_rank_table_manifest.json")
    write_text_atomic(manifest_path, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest_path


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    asof = parse_asof(args.asof)
    policy_asof = parse_asof(args.policy_asof) if str(args.policy_asof or "").strip() else asof
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    policy_path = resolve_eligibility_policy_path(config, base_dir=base_dir)
    lock = load_production_lock(config, base_dir=base_dir, asof=asof)
    requested_scoring_mode = str(args.scoring_mode)
    if args.research_candidate:
        if requested_scoring_mode == "auto":
            raise ValueError(
                "--research-candidate requires an explicit --scoring-mode"
            )
        if not args.score_model_version.strip():
            raise ValueError(
                "--research-candidate requires an explicit --score-model-version"
            )
        scoring_mode = requested_scoring_mode
        score_model_version = args.score_model_version.strip()
        if score_model_version == SCORE_MODEL_VERSION:
            raise ValueError(
                "Research candidates must use a score-model version distinct "
                "from the production baseline"
            )
        calibration_mode = "research_candidate"
    else:
        effective_scoring_mode = (
            str(lock["scoring_mode"]) if lock is not None else "baseline"
        )
        if (
            requested_scoring_mode != "auto"
            and requested_scoring_mode != effective_scoring_mode
        ):
            raise ValueError(
                f"Requested scoring_mode={requested_scoring_mode!r} conflicts "
                f"with effective production lock mode={effective_scoring_mode!r}"
            )
        scoring_mode = effective_scoring_mode
        effective_score_model_version = (
            str(lock["score_model_version"])
            if lock is not None
            else str(
                os.environ.get("DEFENSE_SCORE_MODEL_VERSION", "").strip()
                or SCORE_MODEL_VERSION
            )
        )
        if (
            args.score_model_version.strip()
            and args.score_model_version.strip() != effective_score_model_version
        ):
            raise ValueError(
                f"Requested score_model_version={args.score_model_version!r} "
                "conflicts with the effective production lock"
            )
        score_model_version = effective_score_model_version
        calibration_mode = lock_mode_for_asof(lock, asof)
    provenance_version = (
        score_model_version
        if args.research_candidate or lock is not None
        else str(
            cfg_get(
                config,
                "oos_calibration_standards.families.defense.calibration_provenance_version",
                SCORE_MODEL_VERSION,
            )
            or SCORE_MODEL_VERSION
        )
    )
    snapshot_root = resolve_path(
        str(
            cfg_get(
                config,
                "oos_calibration_standards.families.defense.snapshot_history_root",
                "../output/industrials/defense/dashboard",
            )
        ),
        base_dir=base_dir,
    )
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else snapshot_root / asof
    output_path = output_dir / "defense_final_rank_table.csv"
    manifest_path = output_path.with_name("defense_final_rank_table_manifest.json")
    if output_path.exists() or manifest_path.exists():
        existing_manifest: dict[str, Any] = {}
        if manifest_path.exists():
            try:
                existing_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                existing_manifest = {}
        if not args.allow_overwrite and sealed_artifact_valid(
            output_path,
            asof=asof,
            membership_mode=args.membership_mode,
            calibration_mode=calibration_mode,
            scoring_mode=scoring_mode,
            score_model_version=score_model_version,
            research_candidate=bool(args.research_candidate),
        ):
            print(f"Existing sealed artifact is valid; keeping {output_path}")
            print(f"Existing sealed manifest is valid; keeping {manifest_path}")
            return 0
        if existing_manifest.get("promotion_payload"):
            raise FileExistsError(
                f"Existing artifact for {asof} was sealed by the scripts/27 production promotion "
                f"(manifest carries promotion_payload): {manifest_path}. The publisher never "
                "overwrites a promotion seal, even with --allow-overwrite; remove it manually "
                "only with a protocol note."
            )
        if not args.allow_overwrite:
            raise FileExistsError(
                f"Refusing to overwrite existing dated defense artifact for {asof}: {output_path}. "
                "Use --allow-overwrite only for an explicit manual rebuild."
            )
    fields = header(PROJECT_ROOT)
    # NEW-2: pass the evaluation asof so versioned (profile, stage) keys select the
    # row effective at that asof instead of raising on the first second version.
    policies = select_effective_policies(load_eligibility_policy(policy_path, asof=policy_asof), asof=policy_asof)
    with closing(sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        rows = load_rows(
            conn,
            asof=asof,
            config=config,
            base_dir=base_dir,
            membership_mode=args.membership_mode,
        )
        lineage = build_financial_filing_lineage(
            conn,
            model_family=MODEL_FAMILY,
            asof=asof,
            tickers=(str(row.get("ticker") or "") for row in rows),
        )
        # Newest borrow observation at-or-before asof per ticker: the truthful
        # date for borrow_data_asof_date (the feature build consumes exactly
        # this row via its latest<=asof + staleness rule; one registered borrow
        # source exists, so an unscoped MAX matches what features consumed).
        borrow_asof_by_ticker = {
            str(r["ticker"]): str(r["max_asof"] or "")
            for r in conn.execute(
                "SELECT ticker, MAX(asof_date) AS max_asof FROM fact_ibkr_borrow_snapshot"
                " WHERE asof_date <= ? GROUP BY ticker",
                (asof,),
            )
        }
    if not rows:
        raise ValueError(f"No defense rows found for asof={asof} membership_mode={args.membership_mode}")
    missing_feature = [
        row["ticker"]
        for row in rows
        if not row.get("market_ticker") or not row.get("financial_ticker") or not row.get("positioning_ticker")
    ]
    if missing_feature:
        raise ValueError(
            f"Missing Stage 3/4/5 feature rows for membership_mode={args.membership_mode}: {missing_feature[:20]}"
        )
    scores = build_scores(rows, scoring_mode=scoring_mode)
    apply_export_capacity_fallbacks(rows)
    out_rows = compose_rows(
        rows,
        scores,
        policies,
        asof,
        provenance_version=provenance_version,
        score_model_version=score_model_version,
        membership_mode=args.membership_mode,
        borrow_asof_by_ticker=borrow_asof_by_ticker,
    )
    if args.research_candidate:
        out_rows = apply_research_candidate_stamps(out_rows, scoring_mode=scoring_mode)
    elif calibration_mode != "shadow":
        assert lock is not None
        out_rows = apply_lock_stamps(
            out_rows,
            lock=lock,
            asof=asof,
            mode=calibration_mode,
            is_pit_membership=args.membership_mode == "pit",
        )
    out_rows = apply_financial_lineage_gate(out_rows, lineage)
    lineage_errors = validate_financial_lineage_rank_rows(out_rows)
    if lineage_errors:
        raise ValueError("; ".join(lineage_errors[:20]))
    lineage_manifest = write_financial_lineage_report(
        output_dir / "defense_financial_filing_lineage.csv",
        out_rows,
        model_family=MODEL_FAMILY,
        asof=asof,
        policy_context="research" if args.research_candidate else "production",
    )
    write_csv_atomic(output_path, fields, [{field: row.get(field, "") for field in fields} for row in out_rows])
    manifest_path = seal_manifest(
        output_path,
        rows=len(out_rows),
        asof=asof,
        policy_asof=policy_asof,
        membership_mode=args.membership_mode,
        calibration_mode=calibration_mode,
        scoring_mode=scoring_mode,
        score_model_version=score_model_version,
        research_candidate=bool(args.research_candidate),
        lock=None if args.research_candidate else lock,
        financial_filing_lineage=lineage_manifest,
    )
    print(f"Wrote {output_path} (calibration_mode={calibration_mode})")
    print(f"Wrote {manifest_path}")
    return 0 if lineage_manifest.get("acceptance") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
