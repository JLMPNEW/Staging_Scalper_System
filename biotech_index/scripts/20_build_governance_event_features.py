#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import math
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, normalize_string_list, resolve_path
from biotech_index.core.db import connect, finish_run, init_db, quote_identifier, start_run, utc_now
from biotech_index.core.logging_utils import configure_utc_logging
from biotech_index.core.pipeline_guards import (
    normalize_ticker,
    read_final_scoring_tickers,
    subset_mode_enabled,
    subset_output_path,
    validate_full_universe_coverage,
    validate_nonempty_selection,
    validate_output_coverage,
    validate_requested_tickers,
)
from biotech_index.core.report_inputs import resolve_dated_report_input_csv


LOGGER = logging.getLogger("build_governance_event_features")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
SQLITE_PARAM_CHUNK_SIZE = 800


def chunked(values: list[Any] | tuple[Any, ...], size: int = SQLITE_PARAM_CHUNK_SIZE) -> list[list[Any]]:
    step = max(1, int(size))
    return [list(values[start : start + step]) for start in range(0, len(values), step)]


GOVERNANCE_FIELDS = [
    "asof_date",
    "company_id",
    "ticker",
    "company_name",
    "form4_source_db",
    "form4_snapshot_date",
    "insider_buy_count_90d",
    "open_market_buy_count_90d",
    "insider_buy_value_90d",
    "insider_buy_cluster_count_90d",
    "ceo_cfo_buy_count_180d",
    "director_buy_count_180d",
    "insider_sell_value_90d",
    "sell_to_buy_value_ratio_180d",
    "planned_10b5_1_buy_count",
    "activist_13d_count_365d",
    "buyback_event_count_365d",
    "asr_event_count_365d",
    "leadership_change_count_365d",
    "cfo_departure_flag_365d",
    "regulatory_setback_count_365d",
    "adverse_legal_event_count_365d",
    "generic_competition_risk_count_365d",
    "product_concentration_risk_count_365d",
    "commercial_fragility_risk_score",
    "governance_event_score",
    "governance_risk_score",
    "data_quality",
    "missing_fields",
    "proxy_fields_used",
    "payload_json",
]

GOVERNANCE_NUMERIC_DEFAULT_FIELDS = {
    "company_id",
    "insider_buy_count_90d",
    "open_market_buy_count_90d",
    "insider_buy_value_90d",
    "insider_buy_cluster_count_90d",
    "ceo_cfo_buy_count_180d",
    "director_buy_count_180d",
    "insider_sell_value_90d",
    "sell_to_buy_value_ratio_180d",
    "planned_10b5_1_buy_count",
    "activist_13d_count_365d",
    "buyback_event_count_365d",
    "asr_event_count_365d",
    "leadership_change_count_365d",
    "cfo_departure_flag_365d",
    "regulatory_setback_count_365d",
    "adverse_legal_event_count_365d",
    "generic_competition_risk_count_365d",
    "product_concentration_risk_count_365d",
    "commercial_fragility_risk_score",
}


BUYBACK_PATTERNS = [
    re.compile(r"\bshare repurchase\b", re.IGNORECASE),
    re.compile(r"\bstock repurchase\b", re.IGNORECASE),
    re.compile(r"\brepurchase program\b", re.IGNORECASE),
    re.compile(r"\brepurchase authorization\b", re.IGNORECASE),
    re.compile(r"\brepurchase plan\b", re.IGNORECASE),
]
ASR_PATTERNS = [
    re.compile(r"\baccelerated share repurchase\b", re.IGNORECASE),
    re.compile(r"\bASR\b.{0,80}\b(repurchased|repurchase|agreement|program)\b", re.IGNORECASE),
]
LEADERSHIP_PATTERNS = [
    re.compile(r"\bItem\s+5\.02\b", re.IGNORECASE),
    re.compile(r"\b(appointed|resigned|retired|terminated|departed|departure)\b.{0,120}\b(chief executive|chief financial|president|director|officer|ceo|cfo)\b", re.IGNORECASE),
]
CFO_DEPARTURE_PATTERNS = [
    re.compile(r"\b(resigned|retired|terminated|departed|departure)\b.{0,140}\b(chief financial officer|cfo)\b", re.IGNORECASE),
    re.compile(r"\b(chief financial officer|cfo)\b.{0,140}\b(resigned|retired|terminated|departed|departure)\b", re.IGNORECASE),
]
REGULATORY_SETBACK_PATTERNS = [
    re.compile(r"\b(received|issued|sent|provided)\b.{0,120}\bcomplete response letter\b", re.IGNORECASE),
    re.compile(r"\bcomplete response letter\b.{0,180}\b(additional|further|more)\b.{0,80}\b(efficacy|clinical|data|study|trial)\b", re.IGNORECASE),
    re.compile(r"\bFDA\b.{0,120}\b(requested|requires|required)\b.{0,120}\b(additional|more|further)\b.{0,80}\b(efficacy|clinical|data|study|trial)\b", re.IGNORECASE),
    re.compile(r"\bFDA\b.{0,100}\b(did not approve|refused to approve|declined to approve|rejected)\b", re.IGNORECASE),
]
ADVERSE_LEGAL_PATTERNS = [
    re.compile(r"\b(court|federal circuit|appeals court|district court)\b.{0,220}\b(ruled|held|found|affirmed|determined)\b.{0,180}\b(does not infringe|did not infringe|non[- ]infringement|invalid|unpatentable)\b", re.IGNORECASE),
    re.compile(r"\b(does not infringe|did not infringe|non[- ]infringement)\b.{0,180}\b(patent|patents|claims)\b", re.IGNORECASE),
    re.compile(r"\b(teva|generic)\b.{0,240}\b(does not infringe|did not infringe|non[- ]infringement|invalid|unpatentable)\b", re.IGNORECASE),
]
GENERIC_COMPETITION_PATTERNS = [
    re.compile(r"\bgeneric (version|product|competition|entry|launch)\b", re.IGNORECASE),
    re.compile(r"\bANDA\b|\bParagraph IV\b", re.IGNORECASE),
    re.compile(r"\b(teva|generic)\b.{0,140}\b(korlym|patent|launch|approval|competition)\b", re.IGNORECASE),
]
PRODUCT_CONCENTRATION_PATTERNS = [
    re.compile(r"\bsubstantially all of (our )?(revenue|revenues|net product revenue|net sales)\b", re.IGNORECASE),
    re.compile(r"\b(depend|depends|dependent|rely|relies|reliant)\b.{0,160}\b(sales|revenue|commercialization)\b.{0,120}\b(single|only|primary|principal|one)\b", re.IGNORECASE),
    re.compile(r"\b(currently|primarily)\b.{0,100}\b(rely|depend)\b.{0,120}\b(sales|revenue)\b", re.IGNORECASE),
]
SEC_SCAN_FORMS = ("8-K", "8-K/A", "10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "40-F", "40-F/A", "6-K", "6-K/A", "S-3", "S-3/A", "424B5", "424B3", "424B4")
SEC_GOVERNANCE_CACHE_VERSION = "2026-05-08-sec-governance-signal-cache-v3"
GOVERNANCE_FEATURE_SIGNATURE_VERSION = "2026-05-08-governance-feature-signature-v1"
SEC_GOVERNANCE_HEAD_SCAN_CHARS = 350_000
SEC_GOVERNANCE_CONTEXT_CHARS = 35_000
SEC_GOVERNANCE_DEFAULT_MAX_SCAN_CHARS = 1_250_000
SEC_GOVERNANCE_FORM_MAX_SCAN_CHARS = {
    "10-K": 1_500_000,
    "10-K/A": 1_500_000,
    "20-F": 1_500_000,
    "20-F/A": 1_500_000,
    "40-F": 1_500_000,
    "40-F/A": 1_500_000,
    "10-Q": 800_000,
    "10-Q/A": 800_000,
}
SEC_GOVERNANCE_ANCHOR_RE = re.compile(
    r"\b("
    r"share repurchase|stock repurchase|repurchase program|accelerated share repurchase|"
    r"Item\s+5\.02|appointed|resigned|retired|terminated|departed|departure|"
    r"complete response letter|FDA|regulatory|clinical hold|"
    r"court|federal circuit|appeals court|district court|litigation|lawsuit|patent|"
    r"generic|ANDA|Paragraph IV|"
    r"substantially all of|depend|depends|dependent|rely|relies|reliant"
    r")\b",
    re.IGNORECASE,
)
SEC_GOVERNANCE_SIGNAL_FIELDS = [
    "buyback_flag",
    "asr_flag",
    "leadership_flag",
    "cfo_departure_flag",
    "regulatory_setback_flag",
    "adverse_legal_flag",
    "generic_competition_flag",
    "product_concentration_flag",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build governance and ownership event features for the biotech index.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", type=str, default="", help="Feature date in YYYY-MM-DD. Defaults to UTC today.")
    parser.add_argument("--max-companies", type=int, default=0, help="Smoke-test limit. 0 means all.")
    parser.add_argument("--tickers", type=str, default="", help="Optional comma-separated ticker subset.")
    parser.add_argument("--allow-missing-form4", action="store_true", help="Build SEC-only governance features if the Form 4 database is unavailable.")
    parser.add_argument("--reuse-unchanged-historical", action="store_true", help="Reuse prior governance rows when historical input signatures match exactly.")
    return parser.parse_args()


def configure_logging() -> None:
    configure_utc_logging()


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        LOGGER.debug("Invalid governance date ignored: %r", raw)
        return None


def to_float(raw: object, default: float | None = None) -> float | None:
    if raw is None:
        return default
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def to_int(raw: object, default: int = 0) -> int:
    value = to_float(raw)
    return default if value is None else int(round(value))


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return low
    if not math.isfinite(parsed):
        return low
    return max(low, min(high, parsed))


def as_bool(raw: object, default: bool = False) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in {"1", "true", "t", "yes", "y", "enabled", "on"}:
        return True
    if text in {"0", "false", "f", "no", "n", "disabled", "off"}:
        return False
    return default


def safe_json_loads(raw: object) -> dict[str, Any]:
    try:
        payload = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def normalize_cik(raw: object) -> str:
    text = re.sub(r"\D", "", str(raw or ""))
    if not text:
        return ""
    normalized = text.lstrip("0") or "0"
    return normalized.zfill(10) if len(normalized) <= 10 else normalized


def read_scoring_tickers(path: Path) -> set[str]:
    return read_final_scoring_tickers(path)


def load_companies(
    conn: sqlite3.Connection,
    *,
    scoring_tickers: set[str],
    ticker_filter: set[str],
    max_companies: int,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT company_id, ticker, cik, company_name
        FROM companies
        WHERE is_active = 1
           OR (universe_status = 'delisted_calibration' AND ticker IN (
                SELECT value FROM json_each(?)
           ))
        ORDER BY ticker
        """
        ,
        (json.dumps(sorted(scoring_tickers)),),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        ticker = normalize_ticker(row["ticker"])
        if scoring_tickers and ticker not in scoring_tickers:
            continue
        if ticker_filter and ticker not in ticker_filter:
            continue
        company = dict(row)
        company["ticker"] = ticker
        out.append(company)
        if max_companies > 0 and len(out) >= max_companies:
            break
    return out


def connect_form4_readonly(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def form4_snapshot_date(conn: sqlite3.Connection, snapshot_table: str) -> str:
    sources: list[tuple[str, str]] = []
    if snapshot_table:
        sources.extend([(snapshot_table, "last_index_date"), (snapshot_table, "as_of_date")])
    sources.extend(
        [
            ("stock_signal_snapshot_tier1", "as_of_date"),
            ("sec_form4_daily_state", "last_index_date"),
            ("form4_events_tier1", "filing_date"),
            ("form4_buy_events_v1", "filing_date"),
        ]
    )
    best = ""
    best_key = ""
    for table, field in sources:
        try:
            row = conn.execute(f"SELECT MAX({quote_identifier(field)}) AS snapshot_date FROM {quote_identifier(table)}").fetchone()
        except (sqlite3.Error, ValueError):
            continue
        if row and row["snapshot_date"]:
            raw = str(row["snapshot_date"])
            key = raw[:10] if len(raw) >= 10 and raw[4:5] == "-" and raw[7:8] == "-" else ""
            if key and key > best_key:
                best = raw
                best_key = key
    return best


def contains_any(text: str, terms: list[str]) -> bool:
    lower = text.lower()
    return any(term.lower() in lower for term in terms)


def load_form4_rows(
    form4_conn: sqlite3.Connection | None,
    *,
    table: str,
    ticker: str,
    cik_int: str,
    start_date: date,
    asof_date: date,
) -> tuple[list[dict[str, Any]], str]:
    if form4_conn is None:
        return [], "form4_db_unavailable"
    try:
        table_sql = quote_identifier(table)
        rows = form4_conn.execute(
            f"""
            SELECT *
            FROM {table_sql}
            WHERE is_current_truth = 1
              AND COALESCE(trans_date, filing_date) >= ?
              AND COALESCE(trans_date, filing_date) <= ?
              AND (
                    UPPER(issuer_trading_symbol) = ?
                    OR (? <> '' AND LTRIM(issuer_cik, '0') = ?)
                  )
            ORDER BY COALESCE(trans_date, filing_date) DESC
            """,
            (start_date.isoformat(), asof_date.isoformat(), ticker, cik_int, cik_int),
        ).fetchall()
    except (sqlite3.Error, ValueError) as exc:
        LOGGER.warning("Form 4 query failed for %s: %s", ticker, exc)
        return [], f"form4_query_error:{type(exc).__name__}"
    return [dict(row) for row in rows], ""


def load_form4_rows_bulk(
    form4_conn: sqlite3.Connection | None,
    *,
    table: str,
    companies: list[dict[str, Any]],
    start_date: date,
    asof_date: date,
) -> tuple[dict[int, list[dict[str, Any]]], str]:
    if form4_conn is None:
        return {}, "form4_db_unavailable"
    tickers = sorted({normalize_ticker(company.get("ticker")) for company in companies if normalize_ticker(company.get("ticker"))})
    ciks = sorted({normalize_cik(company.get("cik")) for company in companies if normalize_cik(company.get("cik"))})
    ticker_to_company_ids: dict[str, list[int]] = {}
    cik_to_company_ids: dict[str, list[int]] = {}
    for company in companies:
        company_id = int(company["company_id"])
        ticker = normalize_ticker(company.get("ticker"))
        cik_int = normalize_cik(company.get("cik"))
        if ticker:
            ticker_to_company_ids.setdefault(ticker, []).append(company_id)
        if cik_int:
            cik_to_company_ids.setdefault(cik_int, []).append(company_id)
    if not tickers and not ciks:
        return {int(company["company_id"]): [] for company in companies}, ""
    rows_by_key: dict[str, dict[str, Any]] = {}
    try:
        table_sql = quote_identifier(table)
        for field_expr, values in [
            ("UPPER(issuer_trading_symbol)", tickers),
            ("LTRIM(issuer_cik, '0')", ciks),
        ]:
            for value_chunk in chunked(values):
                placeholders = ",".join("?" for _ in value_chunk)
                rows = form4_conn.execute(
                    f"""
                    SELECT rowid AS __rowid__, *
                    FROM {table_sql}
                    WHERE is_current_truth = 1
                      AND COALESCE(trans_date, filing_date) >= ?
                      AND COALESCE(trans_date, filing_date) <= ?
                      AND {field_expr} IN ({placeholders})
                    ORDER BY COALESCE(trans_date, filing_date) DESC
                    """,
                    (start_date.isoformat(), asof_date.isoformat(), *value_chunk),
                ).fetchall()
                for row in rows:
                    item = dict(row)
                    row_key = str(item.pop("__rowid__", "")) or json.dumps(item, ensure_ascii=True, sort_keys=True, default=str)
                    rows_by_key[row_key] = item
    except (sqlite3.Error, ValueError) as exc:
        LOGGER.warning("Bulk Form 4 query failed: %s", exc)
        return {}, f"form4_query_error:{type(exc).__name__}"
    grouped: dict[int, list[dict[str, Any]]] = {int(company["company_id"]): [] for company in companies}
    rows = sorted(rows_by_key.values(), key=lambda item: str(item.get("trans_date") or item.get("filing_date") or ""), reverse=True)
    for item in rows:
        matched_ids: set[int] = set()
        ticker = normalize_ticker(item.get("issuer_trading_symbol"))
        cik_int = normalize_cik(item.get("issuer_cik"))
        matched_ids.update(ticker_to_company_ids.get(ticker, []))
        matched_ids.update(cik_to_company_ids.get(cik_int, []))
        for company_id in matched_ids:
            grouped.setdefault(company_id, []).append(item)
    return grouped, ""


def load_delisted_form4_rows_bulk(
    conn: sqlite3.Connection,
    *,
    companies: list[dict[str, Any]],
    start_date: date,
    asof_date: date,
) -> dict[int, list[dict[str, Any]]]:
    table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'delisted_calibration_form4_filings'"
    ).fetchone()
    grouped: dict[int, list[dict[str, Any]]] = {int(company["company_id"]): [] for company in companies}
    if table is None or not companies:
        return grouped
    company_ids = [int(company["company_id"]) for company in companies]
    for company_chunk in chunked(company_ids):
        placeholders = ",".join("?" for _ in company_chunk)
        rows = conn.execute(
            f"""
            SELECT *
            FROM delisted_calibration_form4_filings
            WHERE company_id IN ({placeholders})
              AND COALESCE(report_date, filing_date) >= ?
              AND COALESCE(report_date, filing_date) <= ?
              AND (valid_window_start IS NULL OR valid_window_start = '' OR date(COALESCE(report_date, filing_date)) >= date(valid_window_start))
              AND (valid_window_end IS NULL OR valid_window_end = '' OR date(COALESCE(report_date, filing_date)) <= date(valid_window_end))
            ORDER BY COALESCE(report_date, filing_date) DESC
            """,
            (*company_chunk, start_date.isoformat(), asof_date.isoformat()),
        ).fetchall()
        for row in rows:
            item = dict(row)
            company_id = int(item["company_id"])
            codes = [
                code.strip().upper()
                for code in str(item.get("transaction_codes") or "").replace(";", ",").replace("|", ",").split(",")
                if code.strip()
            ]
            purchase_count = to_int(item.get("purchase_transaction_count"))
            trans_code = codes[0] if codes else ("P" if purchase_count > 0 else "")
            grouped.setdefault(company_id, []).append(
                {
                    "issuer_trading_symbol": item.get("calibration_company_ticker") or item.get("ticker") or "",
                    "issuer_cik": item.get("issuer_cik") or "",
                    "trans_date": item.get("report_date") or item.get("filing_date") or "",
                    "filing_date": item.get("filing_date") or "",
                    "trans_code": trans_code,
                    "signal_side": "buy" if purchase_count > 0 or trans_code == "P" else "",
                    "trade_value_usd": 0.0,
                    "rptowner_name": "",
                    "rptowner_title": "",
                    "rptowner_relationship": "",
                    "aff10b5one_flag": 0,
                    "cluster_insiders_5bd": 0,
                    "cluster_insiders_10bd": 0,
                    "cluster_insiders_20bd": 0,
                    "source": "delisted_calibration_form4_filings",
                    "accession_nodash": item.get("accession_nodash") or "",
                    "document_parse_status": item.get("document_parse_status") or "",
                }
            )
    return grouped


def score_governance(row: dict[str, Any]) -> tuple[float, float]:
    buy_count = to_int(row.get("insider_buy_count_90d"))
    planned_buys = to_int(row.get("planned_10b5_1_buy_count"))
    open_market_buys = to_int(row.get("open_market_buy_count_90d"), max(0, buy_count - planned_buys))
    buy_value = to_float(row.get("insider_buy_value_90d"), 0.0) or 0.0
    clusters = to_int(row.get("insider_buy_cluster_count_90d"))
    exec_buys = to_int(row.get("ceo_cfo_buy_count_180d"))
    director_buys = to_int(row.get("director_buy_count_180d"))
    buybacks = to_int(row.get("buyback_event_count_365d"))
    asr = to_int(row.get("asr_event_count_365d"))
    activism = to_int(row.get("activist_13d_count_365d"))
    leadership = to_int(row.get("leadership_change_count_365d"))
    fragility = to_float(row.get("commercial_fragility_risk_score"), 0.0) or 0.0
    sell_value = to_float(row.get("insider_sell_value_90d"), 0.0) or 0.0
    sell_ratio = to_float(row.get("sell_to_buy_value_ratio_180d"), 0.0) or 0.0
    cfo_departure = to_int(row.get("cfo_departure_flag_365d"))

    buy_value_score = 0.0
    if buy_value > 0:
        buy_value_score = min(18.0, math.log10(max(buy_value, 1.0)) * 2.5)
    event_score = 15.0
    event_score += min(18.0, open_market_buys * 4.0 + planned_buys * 1.0)
    event_score += buy_value_score
    event_score += min(20.0, clusters * 10.0)
    event_score += min(18.0, exec_buys * 9.0)
    event_score += min(12.0, director_buys * 3.0)
    event_score += min(18.0, buybacks * 7.0 + asr * 14.0)
    event_score += min(12.0, activism * 10.0)
    event_score += min(5.0, leadership * 2.0)
    event_score -= min(5.0, planned_buys * 1.0)

    risk_score = 0.0
    if sell_value > 0 and buy_value <= 0:
        risk_score += 18.0
    if sell_ratio > 4.0:
        risk_score += 18.0
    elif sell_ratio > 2.0:
        risk_score += 10.0
    if cfo_departure:
        risk_score += 15.0
    if fragility > 0:
        risk_score += min(35.0, fragility * 0.65)

    return round(clamp(event_score), 4), round(clamp(risk_score), 4)


def score_commercial_fragility(row: dict[str, Any]) -> float:
    regulatory = to_int(row.get("regulatory_setback_count_365d"))
    legal = to_int(row.get("adverse_legal_event_count_365d"))
    generic = to_int(row.get("generic_competition_risk_count_365d"))
    concentration = to_int(row.get("product_concentration_risk_count_365d"))
    score = 0.0
    score += min(35.0, regulatory * 25.0)
    score += min(35.0, legal * 25.0)
    if legal > 0:
        score += min(15.0, generic * 7.0)
    else:
        score += min(10.0, generic * 3.0)
    if concentration > 0 and (legal > 0 or regulatory > 0 or generic > 0):
        score += min(15.0, concentration * 8.0)
    elif concentration > 0:
        score += min(8.0, concentration * 4.0)
    return round(clamp(score, 0.0, 75.0), 4)


def form4_metrics(
    rows: list[dict[str, Any]],
    *,
    asof_date: date,
    config: dict[str, Any],
) -> dict[str, Any]:
    buy_codes = {code.upper() for code in normalize_string_list(cfg_get(config, "governance_events.buy_codes", ["P"]))}
    sell_codes = {code.upper() for code in normalize_string_list(cfg_get(config, "governance_events.sell_codes", ["S"]))}
    exec_terms = normalize_string_list(
        cfg_get(
            config,
            "governance_events.executive_title_keywords",
            ["chief executive", "ceo", "chief financial", "cfo", "president"],
        )
    )
    director_terms = normalize_string_list(cfg_get(config, "governance_events.director_title_keywords", ["director"]))
    buy_days = int(cfg_get(config, "governance_events.insider_buy_lookback_days", 90))
    sell_days = int(cfg_get(config, "governance_events.insider_sell_lookback_days", 90))
    exec_days = int(cfg_get(config, "governance_events.executive_buy_lookback_days", 180))
    cluster_days = int(cfg_get(config, "governance_events.cluster_lookback_days", 90))
    min_cluster = int(cfg_get(config, "governance_events.buy_cluster_min_insiders", 2))

    buy_start = asof_date - timedelta(days=buy_days)
    sell_start = asof_date - timedelta(days=sell_days)
    exec_start = asof_date - timedelta(days=exec_days)
    cluster_start = asof_date - timedelta(days=cluster_days)

    buy_count = 0
    buy_value = 0.0
    sell_value = 0.0
    buy_value_180 = 0.0
    sell_value_180 = 0.0
    exec_buy_count = 0
    director_buy_count = 0
    planned_buy_count = 0
    cluster_dates: set[str] = set()
    sample_events: list[dict[str, Any]] = []

    for item in rows:
        event_date = parse_date(item.get("trans_date")) or parse_date(item.get("filing_date"))
        if event_date is None or event_date > asof_date:
            continue
        code = str(item.get("trans_code") or "").strip().upper()
        side = str(item.get("signal_side") or item.get("trans_direction") or "").strip().lower()
        value = abs(to_float(item.get("trade_value_usd"), 0.0) or 0.0)
        title = " ".join(
            [
                str(item.get("rptowner_relationship") or ""),
                str(item.get("rptowner_title") or ""),
            ]
        )
        is_buy = code in buy_codes or side == "buy"
        is_sell = code in sell_codes or side == "sell"
        if is_buy and event_date >= buy_start:
            buy_count += 1
            buy_value += value
            if to_int(item.get("aff10b5one_flag")):
                planned_buy_count += 1
            if len(sample_events) < 6:
                sample_events.append(
                    {
                        "date": event_date.isoformat(),
                        "owner": str(item.get("rptowner_name") or ""),
                        "title": str(item.get("rptowner_title") or item.get("rptowner_relationship") or ""),
                        "code": code,
                        "value": round(value, 2),
                    }
                )
        if is_sell and event_date >= sell_start:
            sell_value += value
        if is_buy and event_date >= exec_start:
            buy_value_180 += value
            if contains_any(title, exec_terms):
                exec_buy_count += 1
            if contains_any(title, director_terms):
                director_buy_count += 1
        if is_sell and event_date >= exec_start:
            sell_value_180 += value
        if is_buy and event_date >= cluster_start:
            cluster_max = max(
                to_int(item.get("cluster_insiders_5bd")),
                to_int(item.get("cluster_insiders_10bd")),
                to_int(item.get("cluster_insiders_20bd")),
            )
            if cluster_max >= min_cluster:
                cluster_dates.add(event_date.isoformat())

    ratio = sell_value_180 / buy_value_180 if sell_value_180 > 0 and buy_value_180 > 0 else None if sell_value_180 > 0 else 0.0
    open_market_buy_count = max(0, buy_count - planned_buy_count)
    return {
        "insider_buy_count_90d": buy_count,
        "open_market_buy_count_90d": open_market_buy_count,
        "insider_buy_value_90d": round(buy_value, 2),
        "insider_buy_cluster_count_90d": len(cluster_dates),
        "ceo_cfo_buy_count_180d": exec_buy_count,
        "director_buy_count_180d": director_buy_count,
        "insider_sell_value_90d": round(sell_value, 2),
        "sell_to_buy_value_ratio_180d": round(ratio, 4) if ratio is not None else None,
        "planned_10b5_1_buy_count": planned_buy_count,
        "sample_form4_events": sample_events,
    }


def stable_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def governance_input_signature(
    *,
    sec_docs: list[dict[str, Any]],
    activist_count: int,
    form4_rows: list[dict[str, Any]],
    form4_error: str,
    asof_date: date,
    snapshot_date: str,
    config: dict[str, Any],
    form4_table: str,
) -> str:
    sec_items = [
        {
            "accession_nodash": str(row.get("accession_nodash") or ""),
            "filing_date": str(row.get("filing_date") or ""),
            "form": str(row.get("form") or ""),
            "text_hash": str(row.get("text_hash") or ""),
        }
        for row in sorted(sec_docs, key=lambda item: (str(item.get("accession_nodash") or ""), str(item.get("text_hash") or "")))
    ]
    form4_items = [
        {
            "trans_date": str(row.get("trans_date") or ""),
            "filing_date": str(row.get("filing_date") or ""),
            "issuer_trading_symbol": normalize_ticker(row.get("issuer_trading_symbol")),
            "issuer_cik": normalize_cik(row.get("issuer_cik")),
            "trans_code": str(row.get("trans_code") or "").strip().upper(),
            "signal_side": str(row.get("signal_side") or row.get("trans_direction") or "").strip().lower(),
            "trade_value_usd": round(to_float(row.get("trade_value_usd"), 0.0) or 0.0, 4),
            "rptowner_name": str(row.get("rptowner_name") or ""),
            "rptowner_title": str(row.get("rptowner_title") or ""),
            "rptowner_relationship": str(row.get("rptowner_relationship") or ""),
            "aff10b5one_flag": to_int(row.get("aff10b5one_flag")),
            "cluster_insiders_5bd": to_int(row.get("cluster_insiders_5bd")),
            "cluster_insiders_10bd": to_int(row.get("cluster_insiders_10bd")),
            "cluster_insiders_20bd": to_int(row.get("cluster_insiders_20bd")),
        }
        for row in sorted(
            form4_rows,
            key=lambda item: (
                str(item.get("trans_date") or item.get("filing_date") or ""),
                normalize_ticker(item.get("issuer_trading_symbol")),
                normalize_cik(item.get("issuer_cik")),
                str(item.get("rptowner_name") or ""),
                str(item.get("trans_code") or ""),
            ),
        )
    ]
    governance_cfg = cfg_get(config, "governance_events", {}) or {}
    form4_metric_state = {
        key: value
        for key, value in form4_metrics(form4_rows, asof_date=asof_date, config=config).items()
        if key != "sample_form4_events"
    }
    return stable_hash(
        {
            "version": GOVERNANCE_FEATURE_SIGNATURE_VERSION,
            "parser_signature": governance_parser_signature(),
            "snapshot_date": snapshot_date,
            "form4_table": form4_table,
            "form4_error": form4_error,
            "config": governance_cfg,
            "activist_count": int(activist_count),
            "sec_docs": sec_items,
            "form4_rows": form4_items,
            "form4_metric_state": form4_metric_state,
        }
    )


def governance_parser_signature() -> str:
    payload = {
        "version": SEC_GOVERNANCE_CACHE_VERSION,
        "forms": SEC_SCAN_FORMS,
        "patterns": {
            "buyback": [pattern.pattern for pattern in BUYBACK_PATTERNS],
            "asr": [pattern.pattern for pattern in ASR_PATTERNS],
            "leadership": [pattern.pattern for pattern in LEADERSHIP_PATTERNS],
            "cfo_departure": [pattern.pattern for pattern in CFO_DEPARTURE_PATTERNS],
            "regulatory_setback": [pattern.pattern for pattern in REGULATORY_SETBACK_PATTERNS],
            "adverse_legal": [pattern.pattern for pattern in ADVERSE_LEGAL_PATTERNS],
            "generic_competition": [pattern.pattern for pattern in GENERIC_COMPETITION_PATTERNS],
            "product_concentration": [pattern.pattern for pattern in PRODUCT_CONCENTRATION_PATTERNS],
        },
        "scan_limits": {
            "head_chars": SEC_GOVERNANCE_HEAD_SCAN_CHARS,
            "context_chars": SEC_GOVERNANCE_CONTEXT_CHARS,
            "default_max_chars": SEC_GOVERNANCE_DEFAULT_MAX_SCAN_CHARS,
            "form_max_chars": SEC_GOVERNANCE_FORM_MAX_SCAN_CHARS,
        },
    }
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def governance_scan_text(text: str, *, form: str = "") -> str:
    """Scan the filing head plus targeted deep-context windows from large filings."""
    if len(text) <= SEC_GOVERNANCE_HEAD_SCAN_CHARS:
        return text
    max_scan_chars = int(
        SEC_GOVERNANCE_FORM_MAX_SCAN_CHARS.get(
            str(form or "").strip().upper(),
            SEC_GOVERNANCE_DEFAULT_MAX_SCAN_CHARS,
        )
    )
    segments: list[tuple[int, int]] = [(0, SEC_GOVERNANCE_HEAD_SCAN_CHARS)]
    scanned_chars = SEC_GOVERNANCE_HEAD_SCAN_CHARS
    for match in SEC_GOVERNANCE_ANCHOR_RE.finditer(text, SEC_GOVERNANCE_HEAD_SCAN_CHARS):
        start = max(SEC_GOVERNANCE_HEAD_SCAN_CHARS, match.start() - SEC_GOVERNANCE_CONTEXT_CHARS)
        end = min(len(text), match.end() + SEC_GOVERNANCE_CONTEXT_CHARS)
        if segments and start <= segments[-1][1]:
            old_start, old_end = segments[-1]
            new_end = max(old_end, end)
            scanned_chars += max(0, new_end - old_end)
            segments[-1] = (old_start, new_end)
        else:
            scanned_chars += end - start
            segments.append((start, end))
        if scanned_chars >= max_scan_chars:
            break
    return "\n".join(text[start:end] for start, end in segments)


def empty_sec_governance_signal(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "accession_nodash": str(row.get("accession_nodash") or ""),
        "filing_date": str(row.get("filing_date") or ""),
        "form": str(row.get("form") or ""),
        "text_hash": str(row.get("text_hash") or ""),
        "matches": "",
        **{field: 0 for field in SEC_GOVERNANCE_SIGNAL_FIELDS},
    }


def classify_sec_governance_text(row: dict[str, Any]) -> dict[str, Any]:
    signal = empty_sec_governance_signal(row)
    text = str(row.get("text_content") or "")
    if not text:
        return signal
    signal["text_hash"] = str(row.get("text_hash") or "") or text_hash(text)
    scan_text = governance_scan_text(text, form=str(row.get("form") or ""))
    matches: list[str] = []
    if any(pattern.search(scan_text) for pattern in BUYBACK_PATTERNS):
        signal["buyback_flag"] = 1
        matches.append("buyback")
    if any(pattern.search(scan_text) for pattern in ASR_PATTERNS):
        signal["asr_flag"] = 1
        matches.append("asr")
    if any(pattern.search(scan_text) for pattern in LEADERSHIP_PATTERNS):
        signal["leadership_flag"] = 1
        matches.append("leadership")
    if any(pattern.search(scan_text) for pattern in CFO_DEPARTURE_PATTERNS):
        signal["cfo_departure_flag"] = 1
        matches.append("cfo_departure")
    if any(pattern.search(scan_text) for pattern in REGULATORY_SETBACK_PATTERNS):
        signal["regulatory_setback_flag"] = 1
        matches.append("regulatory_setback")
    if any(pattern.search(scan_text) for pattern in ADVERSE_LEGAL_PATTERNS):
        signal["adverse_legal_flag"] = 1
        matches.append("adverse_legal")
    if any(pattern.search(scan_text) for pattern in GENERIC_COMPETITION_PATTERNS):
        signal["generic_competition_flag"] = 1
        matches.append("generic_competition")
    if any(pattern.search(scan_text) for pattern in PRODUCT_CONCENTRATION_PATTERNS):
        signal["product_concentration_flag"] = 1
        matches.append("product_concentration")
    signal["matches"] = ",".join(dict.fromkeys(matches))
    return signal


def scan_sec_governance_rows(rows: list[dict[str, Any]], activist_count: int) -> dict[str, Any]:
    buyback_accessions: set[str] = set()
    asr_accessions: set[str] = set()
    leadership_accessions: set[str] = set()
    regulatory_accessions: set[str] = set()
    adverse_legal_accessions: set[str] = set()
    generic_accessions: set[str] = set()
    concentration_accessions: set[str] = set()
    cfo_departure = False
    examples: list[dict[str, str]] = []
    for row in rows:
        accession = str(row["accession_nodash"] or "")
        signal = row if all(field in row for field in SEC_GOVERNANCE_SIGNAL_FIELDS) else classify_sec_governance_text(row)
        matched = [part for part in str(signal.get("matches") or "").split(",") if part]
        if int(signal.get("buyback_flag") or 0):
            buyback_accessions.add(accession)
        if int(signal.get("asr_flag") or 0):
            asr_accessions.add(accession)
        if int(signal.get("leadership_flag") or 0):
            leadership_accessions.add(accession)
        if int(signal.get("cfo_departure_flag") or 0):
            cfo_departure = True
        if int(signal.get("regulatory_setback_flag") or 0):
            regulatory_accessions.add(accession)
        if int(signal.get("adverse_legal_flag") or 0):
            adverse_legal_accessions.add(accession)
        if int(signal.get("generic_competition_flag") or 0):
            generic_accessions.add(accession)
        if int(signal.get("product_concentration_flag") or 0):
            concentration_accessions.add(accession)
        if matched and len(examples) < 6:
            examples.append(
                {
                    "filing_date": str(row["filing_date"] or ""),
                    "form": str(row["form"] or ""),
                    "accession": accession,
                    "matches": ",".join(dict.fromkeys(matched)),
                }
            )

    return {
        "activist_13d_count_365d": int(activist_count),
        "buyback_event_count_365d": len(buyback_accessions),
        "asr_event_count_365d": len(asr_accessions),
        "leadership_change_count_365d": len(leadership_accessions),
        "cfo_departure_flag_365d": int(cfo_departure),
        "regulatory_setback_count_365d": len(regulatory_accessions),
        "adverse_legal_event_count_365d": len(adverse_legal_accessions),
        "generic_competition_risk_count_365d": len(generic_accessions),
        "product_concentration_risk_count_365d": len(concentration_accessions),
        "sample_sec_events": examples,
    }


def load_cached_sec_governance_signals(
    conn: sqlite3.Connection,
    docs: list[dict[str, Any]],
    *,
    parser_signature: str,
) -> dict[tuple[str, str], dict[str, Any]]:
    accessions = sorted({str(row.get("accession_nodash") or "") for row in docs if str(row.get("accession_nodash") or "")})
    wanted = {
        (str(row.get("accession_nodash") or ""), str(row.get("text_hash") or ""))
        for row in docs
        if str(row.get("accession_nodash") or "") and str(row.get("text_hash") or "")
    }
    if not accessions or not wanted:
        return {}
    out: dict[tuple[str, str], dict[str, Any]] = {}
    chunk_size = 900
    for start in range(0, len(accessions), chunk_size):
        chunk = accessions[start : start + chunk_size]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            SELECT accession_nodash, text_hash, buyback_flag, asr_flag, leadership_flag,
                   cfo_departure_flag, regulatory_setback_flag, adverse_legal_flag,
                   generic_competition_flag, product_concentration_flag, matches
            FROM sec_governance_signal_cache
            WHERE parser_signature = ?
              AND accession_nodash IN ({placeholders})
            """,
            (parser_signature, *chunk),
        ).fetchall()
        for row in rows:
            key = (str(row["accession_nodash"] or ""), str(row["text_hash"] or ""))
            if key in wanted:
                out[key] = dict(row)
    return out


def load_sec_governance_document_texts(
    conn: sqlite3.Connection,
    docs: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    accessions = sorted({str(row.get("accession_nodash") or "") for row in docs if str(row.get("accession_nodash") or "")})
    if not accessions:
        return {}
    out: dict[str, dict[str, Any]] = {}
    chunk_size = 900
    for start in range(0, len(accessions), chunk_size):
        chunk = accessions[start : start + chunk_size]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            SELECT accession_nodash, document_url, document_type, text_content, text_hash
            FROM (
                SELECT d.accession_nodash, d.document_url, d.document_type, d.text_content, d.text_hash,
                       ROW_NUMBER() OVER (
                           PARTITION BY d.accession_nodash
                           ORDER BY
                               CASE WHEN d.document_type = 'complete_submission_text' THEN 0 ELSE 1 END,
                               COALESCE(d.fetched_at, d.updated_at, d.created_at) DESC,
                               d.document_id DESC
                       ) AS rn
                FROM sec_filing_documents d
                WHERE d.accession_nodash IN ({placeholders})
                  AND d.text_content IS NOT NULL
                  AND LENGTH(d.text_content) > 0
            )
            WHERE rn = 1
            """,
            tuple(chunk),
        ).fetchall()
        for row in rows:
            out[str(row["accession_nodash"] or "")] = dict(row)
    return out


def upsert_sec_governance_signal_cache(
    conn: sqlite3.Connection,
    signals: list[dict[str, Any]],
    *,
    parser_signature: str,
) -> None:
    if not signals:
        return
    now = utc_now()
    rows_to_upsert: list[tuple[Any, ...]] = []
    for signal in signals:
        accession = str(signal.get("accession_nodash") or "")
        digest = str(signal.get("text_hash") or "")
        if not accession or not digest:
            continue
        rows_to_upsert.append(
            (
                accession,
                digest,
                parser_signature,
                *[int(signal.get(field) or 0) for field in SEC_GOVERNANCE_SIGNAL_FIELDS],
                str(signal.get("matches") or ""),
                now,
                now,
            )
        )
    if not rows_to_upsert:
        return
    with conn:
        conn.executemany(
            """
            INSERT INTO sec_governance_signal_cache(
                accession_nodash, text_hash, parser_signature,
                buyback_flag, asr_flag, leadership_flag, cfo_departure_flag,
                regulatory_setback_flag, adverse_legal_flag, generic_competition_flag,
                product_concentration_flag, matches, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(accession_nodash, text_hash, parser_signature) DO UPDATE SET
                buyback_flag = excluded.buyback_flag,
                asr_flag = excluded.asr_flag,
                leadership_flag = excluded.leadership_flag,
                cfo_departure_flag = excluded.cfo_departure_flag,
                regulatory_setback_flag = excluded.regulatory_setback_flag,
                adverse_legal_flag = excluded.adverse_legal_flag,
                generic_competition_flag = excluded.generic_competition_flag,
                product_concentration_flag = excluded.product_concentration_flag,
                matches = excluded.matches,
                updated_at = excluded.updated_at
            """,
            rows_to_upsert,
        )


def hydrate_sec_governance_signals(
    conn: sqlite3.Connection,
    docs_by_company: dict[int, list[dict[str, Any]]],
    *,
    parser_signature: str,
) -> dict[int, list[dict[str, Any]]]:
    all_docs = [row for rows in docs_by_company.values() for row in rows]
    if not all_docs:
        return docs_by_company
    cached = load_cached_sec_governance_signals(conn, all_docs, parser_signature=parser_signature)
    missing_docs = [
        row
        for row in all_docs
        if not str(row.get("text_hash") or "")
        or (str(row.get("accession_nodash") or ""), str(row.get("text_hash") or "")) not in cached
    ]
    text_rows = load_sec_governance_document_texts(conn, missing_docs)
    classified: dict[tuple[str, str], dict[str, Any]] = {}
    classified_by_accession: dict[str, dict[str, Any]] = {}
    cache_updates: list[dict[str, Any]] = []
    for row in missing_docs:
        accession = str(row.get("accession_nodash") or "")
        text_row = text_rows.get(accession)
        if not text_row:
            continue
        text = str(text_row.get("text_content") or "")
        digest = str(text_row.get("text_hash") or "") or text_hash(text)
        signal = classify_sec_governance_text(
            {
                **row,
                "document_url": text_row.get("document_url"),
                "document_type": text_row.get("document_type"),
                "text_content": text,
                "text_hash": digest,
            }
        )
        key = (accession, str(signal.get("text_hash") or ""))
        classified[key] = signal
        classified_by_accession[accession] = signal
        cache_updates.append(signal)
    upsert_sec_governance_signal_cache(conn, cache_updates, parser_signature=parser_signature)

    out: dict[int, list[dict[str, Any]]] = {company_id: [] for company_id in docs_by_company}
    for company_id, rows in docs_by_company.items():
        for row in rows:
            key = (str(row.get("accession_nodash") or ""), str(row.get("text_hash") or ""))
            signal = cached.get(key) or classified.get(key)
            if signal is None and key[0]:
                signal = classified_by_accession.get(key[0])
            out.setdefault(company_id, []).append({**row, **(signal or empty_sec_governance_signal(row))})
    cache_hit_count = len(all_docs) - len(missing_docs)
    cache_hit_rate = (100.0 * cache_hit_count / float(len(all_docs))) if all_docs else 0.0
    LOGGER.info(
        "SEC governance signal cache docs=%d hits=%d misses=%d hit_rate=%.1f%% text_loaded=%d classified=%d fallback_empty=%d",
        len(all_docs),
        cache_hit_count,
        len(missing_docs),
        cache_hit_rate,
        len(text_rows),
        len(cache_updates),
        max(0, len(missing_docs) - len(cache_updates)),
    )
    return out


def load_sec_governance_inputs_bulk(
    conn: sqlite3.Connection,
    *,
    company_ids: list[int],
    asof_date: date,
    config: dict[str, Any],
) -> tuple[dict[int, list[dict[str, Any]]], dict[int, int]]:
    if not company_ids:
        return {}, {}
    if len(company_ids) > SQLITE_PARAM_CHUNK_SIZE:
        docs_by_company: dict[int, list[dict[str, Any]]] = {int(company_id): [] for company_id in company_ids}
        activist_counts: dict[int, int] = {int(company_id): 0 for company_id in company_ids}
        for company_chunk in chunked(company_ids):
            chunk_docs, chunk_activists = load_sec_governance_inputs_bulk(
                conn,
                company_ids=[int(value) for value in company_chunk],
                asof_date=asof_date,
                config=config,
            )
            docs_by_company.update(chunk_docs)
            activist_counts.update(chunk_activists)
        return docs_by_company, activist_counts
    lookback_days = int(cfg_get(config, "governance_events.sec_event_lookback_days", 365))
    max_docs = int(cfg_get(config, "governance_events.sec_document_max_per_company", 40))
    start_date = (asof_date - timedelta(days=lookback_days)).isoformat()
    company_placeholders = ",".join("?" for _ in company_ids)
    form_placeholders = ",".join("?" for _ in SEC_SCAN_FORMS)
    rows = conn.execute(
        f"""
        WITH target_filings AS (
            SELECT company_id, accession_nodash, form, filing_date
            FROM sec_filings
            WHERE company_id IN ({company_placeholders})
              AND filing_date >= ?
              AND filing_date <= ?
              AND form IN ({form_placeholders})
        ),
        latest_docs AS (
            SELECT tf.accession_nodash, l.document_url, l.document_type, l.text_hash
            FROM target_filings tf
            JOIN sec_filing_latest_document l ON l.accession_nodash = tf.accession_nodash
            UNION ALL
            SELECT accession_nodash, document_url, document_type, text_hash
            FROM (
                SELECT
                    d.accession_nodash,
                    d.document_url,
                    d.document_type,
                    d.text_hash,
                    ROW_NUMBER() OVER (
                        PARTITION BY d.accession_nodash
                        ORDER BY
                            CASE WHEN d.document_type = 'complete_submission_text' THEN 0 ELSE 1 END,
                            COALESCE(d.fetched_at, d.updated_at, d.created_at) DESC,
                            d.document_id DESC
                    ) AS rn
                FROM sec_filing_documents d
                JOIN target_filings tf ON tf.accession_nodash = d.accession_nodash
                LEFT JOIN sec_filing_latest_document l ON l.accession_nodash = d.accession_nodash
                WHERE l.accession_nodash IS NULL
                  AND (
                    COALESCE(d.text_length, 0) > 0
                    OR d.text_hash IS NOT NULL
                    OR d.text_content IS NOT NULL
                  )
            )
            WHERE rn = 1
        ),
        ranked_docs AS (
            SELECT
                tf.company_id,
                tf.accession_nodash,
                tf.form,
                tf.filing_date,
                d.document_url,
                d.document_type,
                COALESCE(d.text_hash, '') AS text_hash,
                ROW_NUMBER() OVER (
                    PARTITION BY tf.company_id
                    ORDER BY tf.filing_date DESC, tf.accession_nodash DESC
                ) AS rn
            FROM target_filings tf
            JOIN latest_docs d ON d.accession_nodash = tf.accession_nodash
        )
        SELECT company_id, accession_nodash, form, filing_date, document_url, document_type, text_hash
        FROM ranked_docs
        WHERE rn <= ?
        ORDER BY company_id, filing_date DESC, accession_nodash DESC
        """,
        tuple(company_ids) + (start_date, asof_date.isoformat()) + SEC_SCAN_FORMS + (max_docs,),
    ).fetchall()
    docs_by_company: dict[int, list[dict[str, Any]]] = {company_id: [] for company_id in company_ids}
    for row in rows:
        docs_by_company.setdefault(int(row["company_id"]), []).append(dict(row))
    activist_rows = conn.execute(
        f"""
        SELECT company_id, COUNT(*) AS n
        FROM sec_filings
        WHERE company_id IN ({company_placeholders})
          AND filing_date >= ?
          AND filing_date <= ?
          AND form IN ('SC 13D','SC 13D/A')
        GROUP BY company_id
        """,
        tuple(company_ids) + (start_date, asof_date.isoformat()),
    ).fetchall()
    activist_counts = {company_id: 0 for company_id in company_ids}
    for row in activist_rows:
        activist_counts[int(row["company_id"])] = int(row["n"] or 0)
    return docs_by_company, activist_counts


def scan_sec_governance_events(
    conn: sqlite3.Connection,
    *,
    company_id: int,
    asof_date: date,
    config: dict[str, Any],
) -> dict[str, Any]:
    docs_by_company, activist_counts = load_sec_governance_inputs_bulk(
        conn,
        company_ids=[company_id],
        asof_date=asof_date,
        config=config,
    )
    return scan_sec_governance_rows(docs_by_company.get(company_id, []), activist_counts.get(company_id, 0))


def build_row(
    conn: sqlite3.Connection,
    form4_conn: sqlite3.Connection | None,
    *,
    company: dict[str, Any],
    asof_date: date,
    config: dict[str, Any],
    form4_db_path: Path,
    snapshot_date: str,
    preloaded_form4_rows: list[dict[str, Any]] | None = None,
    preloaded_form4_error: str = "",
    preloaded_sec_events: dict[str, Any] | None = None,
    input_signature: str = "",
) -> dict[str, Any]:
    ticker = normalize_ticker(company.get("ticker"))
    cik_int = normalize_cik(company.get("cik"))
    lookback_days = int(cfg_get(config, "governance_events.lookback_days", 365))
    table = str(cfg_get(config, "governance_events.form4_table", "form4_events_tier1"))
    if preloaded_form4_rows is not None or preloaded_form4_error:
        form4_rows = preloaded_form4_rows or []
        form4_error = preloaded_form4_error
    else:
        form4_rows, form4_error = load_form4_rows(
            form4_conn,
            table=table,
            ticker=ticker,
            cik_int=cik_int,
            start_date=asof_date - timedelta(days=lookback_days),
            asof_date=asof_date,
        )
    form4 = form4_metrics(form4_rows, asof_date=asof_date, config=config) if form4_rows else {
        "insider_buy_count_90d": 0,
        "open_market_buy_count_90d": 0,
        "insider_buy_value_90d": 0.0,
        "insider_buy_cluster_count_90d": 0,
        "ceo_cfo_buy_count_180d": 0,
        "director_buy_count_180d": 0,
        "insider_sell_value_90d": 0.0,
        "sell_to_buy_value_ratio_180d": 0.0,
        "planned_10b5_1_buy_count": 0,
        "sample_form4_events": [],
    }
    sec_events = preloaded_sec_events or scan_sec_governance_events(
        conn,
        company_id=int(company["company_id"]),
        asof_date=asof_date,
        config=config,
    )
    base = {
        "asof_date": asof_date.isoformat(),
        "company_id": int(company["company_id"]),
        "ticker": ticker,
        "company_name": str(company.get("company_name") or ""),
        "form4_source_db": str(form4_db_path),
        "form4_snapshot_date": snapshot_date,
        **{key: form4[key] for key in form4 if key != "sample_form4_events"},
        **sec_events,
    }
    base["commercial_fragility_risk_score"] = score_commercial_fragility(base)
    event_score, risk_score = score_governance(base)
    missing: list[str] = []
    if form4_error:
        missing.append(form4_error)
    if not snapshot_date:
        missing.append("form4_snapshot_date")
    data_quality = "high" if not missing else "medium" if form4_error != "form4_db_unavailable" else "low"
    payload = {
        "form4_row_count_365d": len(form4_rows),
        "form4_lookup": {"ticker": ticker, "cik_int": cik_int},
        "input_signature": input_signature,
        "sample_form4_events": form4.get("sample_form4_events", []),
        "sample_sec_events": sec_events.get("sample_sec_events", []),
        "method": "form4_current_truth_plus_sec_text_governance_scan",
    }
    copied_fields: dict[str, Any] = {}
    excluded_fields = {"governance_event_score", "governance_risk_score", "data_quality", "missing_fields", "payload_json"}
    for field in GOVERNANCE_FIELDS:
        if field in excluded_fields:
            continue
        value = base.get(field)
        if (value is None or value == "") and field in GOVERNANCE_NUMERIC_DEFAULT_FIELDS:
            value = 0.0
        copied_fields[field] = "" if value is None else value
    return {
        **copied_fields,
        "governance_event_score": event_score,
        "governance_risk_score": risk_score,
        "data_quality": data_quality,
        "missing_fields": ";".join(missing),
        "payload_json": json.dumps(payload, ensure_ascii=True, sort_keys=True),
    }


def upsert_rows(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    asof_date: str,
    *,
    target_company_ids: set[int] | None = None,
) -> None:
    now = utc_now()
    placeholders = ", ".join("?" for _ in GOVERNANCE_FIELDS)
    with conn:
        if target_company_ids is None:
            conn.execute("DELETE FROM governance_event_features_daily WHERE asof_date = ?", (asof_date,))
        elif target_company_ids:
            for company_chunk in chunked(sorted(target_company_ids)):
                company_placeholders = ",".join("?" for _ in company_chunk)
                conn.execute(
                    f"DELETE FROM governance_event_features_daily WHERE asof_date = ? AND company_id IN ({company_placeholders})",
                    (asof_date, *company_chunk),
                )
        else:
            return
        conn.executemany(
            f"""
            INSERT INTO governance_event_features_daily({", ".join(GOVERNANCE_FIELDS)}, created_at, updated_at)
            VALUES ({placeholders}, ?, ?)
            """,
            [tuple(row.get(field) for field in GOVERNANCE_FIELDS) + (now, now) for row in rows],
        )


def load_previous_governance_rows(
    conn: sqlite3.Connection,
    *,
    asof_date: date,
    company_ids: list[int],
) -> dict[int, dict[str, Any]]:
    if not company_ids:
        return {}
    out: dict[int, dict[str, Any]] = {}
    for company_chunk in chunked(company_ids):
        placeholders = ",".join("?" for _ in company_chunk)
        rows = conn.execute(
            f"""
            WITH latest AS (
                SELECT company_id, MAX(asof_date) AS max_asof
                FROM governance_event_features_daily
                WHERE asof_date < ?
                  AND company_id IN ({placeholders})
                GROUP BY company_id
            )
            SELECT g.*
            FROM governance_event_features_daily g
            JOIN latest l
              ON l.company_id = g.company_id
             AND l.max_asof = g.asof_date
            """,
            (asof_date.isoformat(), *company_chunk),
        ).fetchall()
        out.update({int(row["company_id"]): dict(row) for row in rows})
    return out


def row_input_signature(row: dict[str, Any]) -> str:
    return str(safe_json_loads(row.get("payload_json")).get("input_signature") or "")


def copy_governance_row_for_asof(row: dict[str, Any], company: dict[str, Any], asof_date: date) -> dict[str, Any]:
    copied = {field: row.get(field) for field in GOVERNANCE_FIELDS}
    copied["asof_date"] = asof_date.isoformat()
    copied["company_id"] = int(company["company_id"])
    copied["ticker"] = normalize_ticker(company.get("ticker"))
    copied["company_name"] = str(company.get("company_name") or "")
    return copied


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=GOVERNANCE_FIELDS, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    configure_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_csv = resolve_path(cfg_get(config, "governance_events.output_csv"), base_dir=base_dir)
    configured_universe_csv = resolve_path(cfg_get(config, "governance_events.final_scoring_universe_csv"), base_dir=base_dir)
    form4_db_path = resolve_path(cfg_get(config, "governance_events.form4_db_path"), base_dir=base_dir)
    asof_date = parse_date(args.asof) if args.asof else datetime.now(timezone.utc).date()
    if asof_date is None:
        raise ValueError(f"Invalid --asof date: {args.asof}")
    universe_csv = resolve_dated_report_input_csv(
        configured_universe_csv,
        base_output_dir=resolve_path(cfg_get(config, "biotech_scoring.output_dir", "../output/biotech_index_reports"), base_dir=base_dir),
        asof_date=asof_date.isoformat(),
        logger=LOGGER,
    )
    ticker_filter = {normalize_ticker(value) for value in args.tickers.split(",") if value.strip()}
    sqlite_timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))
    form4_required = as_bool(cfg_get(config, "governance_events.form4_required", True)) and not bool(args.allow_missing_form4)
    reuse_unchanged = bool(args.reuse_unchanged_historical or as_bool(cfg_get(config, "governance_events.reuse_unchanged_historical", False)))

    form4_conn: sqlite3.Connection | None = None
    snapshot_date = ""
    try:
        form4_conn = connect_form4_readonly(form4_db_path)
        snapshot_date = form4_snapshot_date(form4_conn, str(cfg_get(config, "governance_events.form4_snapshot_table", "sec_form4_daily_state")))
    except BaseException as exc:
        if isinstance(exc, (SystemExit, KeyboardInterrupt, GeneratorExit)):
            raise
        if form4_conn is not None:
            form4_conn.close()
            form4_conn = None
        if form4_required:
            raise RuntimeError(f"Form 4 database is required for governance features but is unavailable: {form4_db_path}") from exc
        LOGGER.warning("Form 4 database unavailable; governance rows will use SEC-only evidence: %s", exc)

    with connect(db_path, timeout_sec=sqlite_timeout_sec) as conn:
        run_id: int | None = None
        try:
            overall_start = time.perf_counter()
            init_db(conn)
            run_id = start_run(conn, run_type="build_governance_event_features", input_path=form4_db_path)
            scoring_tickers = read_scoring_tickers(universe_csv)
            companies = load_companies(conn, scoring_tickers=scoring_tickers, ticker_filter=ticker_filter, max_companies=args.max_companies)
            subset_mode = subset_mode_enabled(ticker_filter=ticker_filter, max_count=int(args.max_companies))
            output_csv = subset_output_path(output_csv, subset_mode=subset_mode)
            validate_nonempty_selection(count=len(companies), context="governance event feature build", subset_mode=subset_mode)
            loaded_tickers = [str(company["ticker"]) for company in companies]
            validate_requested_tickers(
                requested_tickers=ticker_filter,
                loaded_tickers=loaded_tickers,
                context="governance event feature build",
            )
            validate_full_universe_coverage(
                expected_tickers=scoring_tickers,
                observed_tickers=loaded_tickers,
                context="governance event feature build",
                subset_mode=subset_mode,
            )
            company_ids = [int(company["company_id"]) for company in companies]
            table = str(cfg_get(config, "governance_events.form4_table", "form4_events_tier1"))
            lookback_days = int(cfg_get(config, "governance_events.lookback_days", 365))
            phase_start = time.perf_counter()
            form4_rows_by_company, form4_preload_error = load_form4_rows_bulk(
                form4_conn,
                table=table,
                companies=companies,
                start_date=asof_date - timedelta(days=lookback_days),
                asof_date=asof_date,
            )
            delisted_form4_rows_by_company = load_delisted_form4_rows_bulk(
                conn,
                companies=companies,
                start_date=asof_date - timedelta(days=lookback_days),
                asof_date=asof_date,
            )
            delisted_form4_count = 0
            for company_id, rows in delisted_form4_rows_by_company.items():
                if rows:
                    form4_rows_by_company.setdefault(company_id, []).extend(rows)
                    delisted_form4_count += len(rows)
            LOGGER.info(
                "Governance Form 4 preload complete: companies=%d rows=%d delisted_cache_rows=%d elapsed=%.3fs error=%s",
                len(companies),
                sum(len(rows) for rows in form4_rows_by_company.values()),
                delisted_form4_count,
                time.perf_counter() - phase_start,
                form4_preload_error or "",
            )
            phase_start = time.perf_counter()
            sec_docs_by_company, activist_counts_by_company = load_sec_governance_inputs_bulk(
                conn,
                company_ids=company_ids,
                asof_date=asof_date,
                config=config,
            )
            LOGGER.info(
                "Governance SEC metadata preload complete: companies=%d docs=%d elapsed=%.3fs",
                len(companies),
                sum(len(rows) for rows in sec_docs_by_company.values()),
                time.perf_counter() - phase_start,
            )
            signature_by_company = {
                int(company["company_id"]): governance_input_signature(
                    sec_docs=sec_docs_by_company.get(int(company["company_id"]), []),
                    activist_count=activist_counts_by_company.get(int(company["company_id"]), 0),
                    form4_rows=form4_rows_by_company.get(int(company["company_id"]), []),
                    form4_error=form4_preload_error,
                    asof_date=asof_date,
                    snapshot_date=snapshot_date,
                    config=config,
                    form4_table=table,
                )
                for company in companies
            }
            previous_rows = load_previous_governance_rows(conn, asof_date=asof_date, company_ids=company_ids) if reuse_unchanged else {}
            reused_rows_by_company: dict[int, dict[str, Any]] = {}
            dirty_companies: list[dict[str, Any]] = []
            for company in companies:
                company_id = int(company["company_id"])
                previous = previous_rows.get(company_id)
                if previous and row_input_signature(previous) == signature_by_company.get(company_id, ""):
                    reused_rows_by_company[company_id] = copy_governance_row_for_asof(previous, company, asof_date)
                else:
                    dirty_companies.append(company)
            LOGGER.info(
                "Governance historical reuse: enabled=%s reused=%d dirty=%d",
                reuse_unchanged,
                len(reused_rows_by_company),
                len(dirty_companies),
            )
            dirty_company_ids = {int(company["company_id"]) for company in dirty_companies}
            phase_start = time.perf_counter()
            hydrated_dirty_docs = hydrate_sec_governance_signals(
                conn,
                {company_id: rows for company_id, rows in sec_docs_by_company.items() if company_id in dirty_company_ids},
                parser_signature=governance_parser_signature(),
            )
            LOGGER.info(
                "Governance SEC signal hydration complete: dirty_companies=%d dirty_docs=%d elapsed=%.3fs",
                len(dirty_companies),
                sum(len(rows) for rows in hydrated_dirty_docs.values()),
                time.perf_counter() - phase_start,
            )
            phase_start = time.perf_counter()
            row_workers = max(1, int(cfg_get(config, "governance_events.max_workers", 1)))

            def build_company_output(company: dict[str, Any]) -> dict[str, Any]:
                company_id = int(company["company_id"])
                if company_id in reused_rows_by_company:
                    return reused_rows_by_company[company_id]
                return build_row(
                    conn,
                    form4_conn,
                    company=company,
                    asof_date=asof_date,
                    config=config,
                    form4_db_path=form4_db_path,
                    snapshot_date=snapshot_date,
                    preloaded_form4_rows=form4_rows_by_company.get(company_id, []),
                    preloaded_form4_error=form4_preload_error,
                    preloaded_sec_events=scan_sec_governance_rows(
                        hydrated_dirty_docs.get(company_id, []),
                        activist_counts_by_company.get(company_id, 0),
                    ),
                    input_signature=signature_by_company.get(company_id, ""),
                )

            rows_by_company: dict[int, dict[str, Any]] = {}
            if row_workers <= 1 or len(companies) <= 1:
                for idx, company in enumerate(companies, start=1):
                    row = build_company_output(company)
                    rows_by_company[int(company["company_id"])] = row
                    if idx % 25 == 0 or idx == len(companies):
                        LOGGER.info("[%d/%d] governance features built/reused", idx, len(companies))
            else:
                with ThreadPoolExecutor(max_workers=min(row_workers, len(companies))) as executor:
                    futures = {executor.submit(build_company_output, company): company for company in companies}
                    pending_raise: BaseException | None = None
                    for idx, future in enumerate(as_completed(futures), start=1):
                        company = futures[future]
                        try:
                            rows_by_company[int(company["company_id"])] = future.result()
                        except BaseException as exc:
                            pending_raise = exc
                            if isinstance(exc, (SystemExit, KeyboardInterrupt, GeneratorExit)):
                                LOGGER.warning("Governance row worker interrupted for ticker=%s", company.get("ticker"))
                            else:
                                LOGGER.exception("Governance row worker failed for ticker=%s", company.get("ticker"))
                            for other in futures:
                                if other is not future:
                                    other.cancel()
                            break
                        if idx % 25 == 0 or idx == len(companies):
                            LOGGER.info("[%d/%d] governance features built/reused", idx, len(companies))
                    if pending_raise is not None:
                        raise pending_raise
            rows = [rows_by_company[int(company["company_id"])] for company in companies]
            LOGGER.info("Governance row assembly complete: rows=%d reused=%d elapsed=%.3fs", len(rows), len(reused_rows_by_company), time.perf_counter() - phase_start)
            partial_run = bool(ticker_filter) or int(args.max_companies) > 0
            validate_output_coverage(
                expected_tickers=scoring_tickers,
                output_tickers=[row["ticker"] for row in rows],
                context="governance event feature build",
                subset_mode=subset_mode,
            )
            upsert_rows(
                conn,
                rows,
                asof_date.isoformat(),
                target_company_ids=set(company_ids) if partial_run else None,
            )
            write_csv(output_csv, rows)
            LOGGER.info("Governance feature build complete: rows=%d elapsed=%.3fs output=%s", len(rows), time.perf_counter() - overall_start, output_csv)
            finish_run(conn, run_id=run_id, status="success", row_count=len(rows), message=f"asof={asof_date.isoformat()} output={output_csv}")
        except BaseException as exc:
            if isinstance(exc, KeyboardInterrupt) or (isinstance(exc, SystemExit) and exc.code in (0, None)):
                raise
            if run_id is not None:
                finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            if form4_conn is not None:
                form4_conn.close()


if __name__ == "__main__":
    main()
