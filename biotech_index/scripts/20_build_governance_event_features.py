#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, normalize_string_list, resolve_path
from biotech_index.core.db import connect, finish_run, init_db, start_run, utc_now
from biotech_index.core.pipeline_guards import (
    read_final_scoring_tickers,
    subset_mode_enabled,
    subset_output_path,
    validate_full_universe_coverage,
    validate_nonempty_selection,
    validate_output_coverage,
    validate_requested_tickers,
)


LOGGER = logging.getLogger("build_governance_event_features")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


GOVERNANCE_FIELDS = [
    "asof_date",
    "company_id",
    "ticker",
    "company_name",
    "form4_source_db",
    "form4_snapshot_date",
    "insider_buy_count_90d",
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
    "payload_json",
]


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build governance and ownership event features for the biotech index.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", type=str, default="", help="Feature date in YYYY-MM-DD. Defaults to UTC today.")
    parser.add_argument("--max-companies", type=int, default=0, help="Smoke-test limit. 0 means all.")
    parser.add_argument("--tickers", type=str, default="", help="Optional comma-separated ticker subset.")
    parser.add_argument("--allow-missing-form4", action="store_true", help="Build SEC-only governance features if the Form 4 database is unavailable.")
    return parser.parse_args()


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    for handler in logging.getLogger().handlers:
        if handler.formatter is not None:
            handler.formatter.converter = time.gmtime


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
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
    return max(low, min(high, value))


def as_bool(raw: object) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "y"}


def normalize_ticker(raw: object) -> str:
    return str(raw or "").strip().upper().replace(".", "-")


def normalize_cik(raw: object) -> str:
    text = re.sub(r"\D", "", str(raw or ""))
    if not text:
        return ""
    try:
        return str(int(text))
    except ValueError:
        return text.lstrip("0")


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
        ORDER BY ticker
        """
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
    try:
        if snapshot_table:
            row = conn.execute(f"SELECT MAX(last_index_date) AS snapshot_date FROM {snapshot_table}").fetchone()
            if row and row["snapshot_date"]:
                return str(row["snapshot_date"])
    except sqlite3.Error:
        pass
    for table, field in [("form4_events_tier1", "filing_date"), ("form4_buy_events_v1", "filing_date")]:
        try:
            row = conn.execute(f"SELECT MAX({field}) AS snapshot_date FROM {table}").fetchone()
            if row and row["snapshot_date"]:
                return str(row["snapshot_date"])
        except sqlite3.Error:
            continue
    return ""


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
        rows = form4_conn.execute(
            f"""
            SELECT *
            FROM {table}
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
    except sqlite3.Error as exc:
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
    clauses: list[str] = []
    params: list[Any] = [start_date.isoformat(), asof_date.isoformat()]
    if tickers:
        clauses.append(f"UPPER(issuer_trading_symbol) IN ({','.join('?' for _ in tickers)})")
        params.extend(tickers)
    if ciks:
        clauses.append(f"LTRIM(issuer_cik, '0') IN ({','.join('?' for _ in ciks)})")
        params.extend(ciks)
    if not clauses:
        return {int(company["company_id"]): [] for company in companies}, ""
    try:
        rows = form4_conn.execute(
            f"""
            SELECT *
            FROM {table}
            WHERE is_current_truth = 1
              AND COALESCE(trans_date, filing_date) >= ?
              AND COALESCE(trans_date, filing_date) <= ?
              AND ({' OR '.join(clauses)})
            ORDER BY COALESCE(trans_date, filing_date) DESC
            """,
            tuple(params),
        ).fetchall()
    except sqlite3.Error as exc:
        LOGGER.warning("Bulk Form 4 query failed: %s", exc)
        return {}, f"form4_query_error:{type(exc).__name__}"
    grouped: dict[int, list[dict[str, Any]]] = {int(company["company_id"]): [] for company in companies}
    for row in rows:
        item = dict(row)
        matched_ids: set[int] = set()
        ticker = normalize_ticker(item.get("issuer_trading_symbol"))
        cik_int = normalize_cik(item.get("issuer_cik"))
        matched_ids.update(ticker_to_company_ids.get(ticker, []))
        matched_ids.update(cik_to_company_ids.get(cik_int, []))
        for company_id in matched_ids:
            grouped.setdefault(company_id, []).append(item)
    return grouped, ""


def score_governance(row: dict[str, Any]) -> tuple[float, float]:
    buy_count = to_int(row.get("insider_buy_count_90d"))
    buy_value = to_float(row.get("insider_buy_value_90d"), 0.0) or 0.0
    clusters = to_int(row.get("insider_buy_cluster_count_90d"))
    exec_buys = to_int(row.get("ceo_cfo_buy_count_180d"))
    director_buys = to_int(row.get("director_buy_count_180d"))
    planned_buys = to_int(row.get("planned_10b5_1_buy_count"))
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
    event_score += min(18.0, buy_count * 4.0)
    event_score += buy_value_score
    event_score += min(20.0, clusters * 10.0)
    event_score += min(18.0, exec_buys * 9.0)
    event_score += min(12.0, director_buys * 3.0)
    event_score += min(18.0, buybacks * 7.0 + asr * 14.0)
    event_score += min(12.0, activism * 10.0)
    event_score += min(5.0, leadership * 2.0)
    event_score -= min(8.0, planned_buys * 2.0)

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
    exec_terms = normalize_string_list(cfg_get(config, "governance_events.executive_title_keywords", []))
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

    ratio = sell_value_180 / max(buy_value_180, 1.0) if sell_value_180 > 0 else 0.0
    return {
        "insider_buy_count_90d": buy_count,
        "insider_buy_value_90d": round(buy_value, 2),
        "insider_buy_cluster_count_90d": len(cluster_dates),
        "ceo_cfo_buy_count_180d": exec_buy_count,
        "director_buy_count_180d": director_buy_count,
        "insider_sell_value_90d": round(sell_value, 2),
        "sell_to_buy_value_ratio_180d": round(ratio, 4),
        "planned_10b5_1_buy_count": planned_buy_count,
        "sample_form4_events": sample_events,
    }


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
        text = str(row["text_content"] or "")
        if not text:
            continue
        head = text[:350_000]
        matched = []
        if any(pattern.search(head) for pattern in BUYBACK_PATTERNS):
            buyback_accessions.add(accession)
            matched.append("buyback")
        if any(pattern.search(head) for pattern in ASR_PATTERNS):
            asr_accessions.add(accession)
            matched.append("asr")
        if any(pattern.search(head) for pattern in LEADERSHIP_PATTERNS):
            leadership_accessions.add(accession)
            matched.append("leadership")
        if any(pattern.search(head) for pattern in CFO_DEPARTURE_PATTERNS):
            cfo_departure = True
            matched.append("cfo_departure")
        if any(pattern.search(head) for pattern in REGULATORY_SETBACK_PATTERNS):
            regulatory_accessions.add(accession)
            matched.append("regulatory_setback")
        if any(pattern.search(head) for pattern in ADVERSE_LEGAL_PATTERNS):
            adverse_legal_accessions.add(accession)
            matched.append("adverse_legal")
        if any(pattern.search(head) for pattern in GENERIC_COMPETITION_PATTERNS):
            generic_accessions.add(accession)
            matched.append("generic_competition")
        if any(pattern.search(head) for pattern in PRODUCT_CONCENTRATION_PATTERNS):
            concentration_accessions.add(accession)
            matched.append("product_concentration")
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


def load_sec_governance_inputs_bulk(
    conn: sqlite3.Connection,
    *,
    company_ids: list[int],
    asof_date: date,
    config: dict[str, Any],
) -> tuple[dict[int, list[dict[str, Any]]], dict[int, int]]:
    if not company_ids:
        return {}, {}
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
            SELECT accession_nodash, document_url, text_content
            FROM (
                SELECT
                    d.accession_nodash,
                    d.document_url,
                    d.text_content,
                    d.document_type,
                    ROW_NUMBER() OVER (
                        PARTITION BY d.accession_nodash
                        ORDER BY
                            CASE WHEN d.document_type = 'complete_submission_text' THEN 0 ELSE 1 END,
                            COALESCE(d.fetched_at, d.updated_at, d.created_at) DESC,
                            d.document_id DESC
                    ) AS rn
                FROM sec_filing_documents d
                JOIN target_filings tf ON tf.accession_nodash = d.accession_nodash
                WHERE d.text_content IS NOT NULL
                  AND length(d.text_content) > 0
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
                d.text_content,
                ROW_NUMBER() OVER (
                    PARTITION BY tf.company_id
                    ORDER BY tf.filing_date DESC, tf.accession_nodash DESC
                ) AS rn
            FROM target_filings tf
            JOIN latest_docs d ON d.accession_nodash = tf.accession_nodash
        )
        SELECT company_id, accession_nodash, form, filing_date, document_url, text_content
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
        "sample_form4_events": form4.get("sample_form4_events", []),
        "sample_sec_events": sec_events.get("sample_sec_events", []),
        "method": "form4_current_truth_plus_sec_text_governance_scan",
    }
    return {
        **{field: base.get(field, "") for field in GOVERNANCE_FIELDS if field not in {"governance_event_score", "governance_risk_score", "data_quality", "missing_fields", "payload_json"}},
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
    update_cols = [field for field in GOVERNANCE_FIELDS if field not in {"asof_date", "company_id"}]
    with conn:
        if target_company_ids is None:
            conn.execute("DELETE FROM governance_event_features_daily WHERE asof_date = ?", (asof_date,))
        elif target_company_ids:
            company_placeholders = ",".join("?" for _ in target_company_ids)
            conn.execute(
                f"DELETE FROM governance_event_features_daily WHERE asof_date = ? AND company_id IN ({company_placeholders})",
                (asof_date, *sorted(target_company_ids)),
            )
        else:
            return
        conn.executemany(
            f"""
            INSERT INTO governance_event_features_daily({", ".join(GOVERNANCE_FIELDS)}, created_at, updated_at)
            VALUES ({placeholders}, ?, ?)
            ON CONFLICT(asof_date, company_id) DO UPDATE SET
                {", ".join(f"{field} = excluded.{field}" for field in update_cols)},
                updated_at = excluded.updated_at
            """,
            [tuple(row.get(field) for field in GOVERNANCE_FIELDS) + (now, now) for row in rows],
        )


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
    universe_csv = resolve_path(cfg_get(config, "governance_events.final_scoring_universe_csv"), base_dir=base_dir)
    form4_db_path = resolve_path(cfg_get(config, "governance_events.form4_db_path"), base_dir=base_dir)
    asof_date = parse_date(args.asof) if args.asof else datetime.now(timezone.utc).date()
    if asof_date is None:
        raise ValueError(f"Invalid --asof date: {args.asof}")
    ticker_filter = {normalize_ticker(value) for value in args.tickers.split(",") if value.strip()}
    sqlite_timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))
    form4_required = as_bool(cfg_get(config, "governance_events.form4_required", True)) and not bool(args.allow_missing_form4)

    form4_conn: sqlite3.Connection | None = None
    snapshot_date = ""
    try:
        form4_conn = connect_form4_readonly(form4_db_path)
        snapshot_date = form4_snapshot_date(form4_conn, str(cfg_get(config, "governance_events.form4_snapshot_table", "sec_form4_daily_state")))
    except Exception as exc:
        if form4_required:
            raise RuntimeError(f"Form 4 database is required for governance features but is unavailable: {form4_db_path}") from exc
        LOGGER.warning("Form 4 database unavailable; governance rows will use SEC-only evidence: %s", exc)

    with connect(db_path, timeout_sec=sqlite_timeout_sec) as conn:
        init_db(conn)
        run_id = start_run(conn, run_type="build_governance_event_features", input_path=form4_db_path)
        try:
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
            form4_rows_by_company, form4_preload_error = load_form4_rows_bulk(
                form4_conn,
                table=table,
                companies=companies,
                start_date=asof_date - timedelta(days=lookback_days),
                asof_date=asof_date,
            )
            sec_docs_by_company, activist_counts_by_company = load_sec_governance_inputs_bulk(
                conn,
                company_ids=company_ids,
                asof_date=asof_date,
                config=config,
            )
            rows = []
            for idx, company in enumerate(companies, start=1):
                company_id = int(company["company_id"])
                row = build_row(
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
                        sec_docs_by_company.get(company_id, []),
                        activist_counts_by_company.get(company_id, 0),
                    ),
                )
                rows.append(row)
                if idx % 25 == 0 or idx == len(companies):
                    LOGGER.info("[%d/%d] governance features built", idx, len(companies))
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
            finish_run(conn, run_id=run_id, status="success", row_count=len(rows), message=f"asof={asof_date.isoformat()} output={output_csv}")
        except Exception as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            if form4_conn is not None:
                form4_conn.close()
    LOGGER.info("Governance feature build complete: rows=%d output=%s", len(rows), output_csv)


if __name__ == "__main__":
    main()
