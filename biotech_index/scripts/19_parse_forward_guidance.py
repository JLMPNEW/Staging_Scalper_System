#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import logging
import math
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, resolve_optional_path, resolve_path
from biotech_index.core.db import connect, finish_run, init_db, start_run, utc_now
from biotech_index.core.http_cache import CachedHttpClient, HostThrottle
from biotech_index.core.logging_utils import configure_utc_logging
from biotech_index.core.pipeline_guards import (
    normalize_ticker,
    read_final_scoring_tickers,
    subset_mode_enabled,
    subset_output_path,
    validate_full_universe_coverage,
    validate_layer_freshness,
    validate_nonempty_selection,
    validate_output_coverage,
    validate_requested_tickers,
)


LOGGER = logging.getLogger("parse_forward_guidance")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
PARSER_LOGIC_VERSION = "2026-04-27-forward-guidance-parser-v2"
SQLITE_PARAM_CHUNK_SIZE = 800

GUIDANCE_FIELDS = [
    "asof_date",
    "ticker",
    "company_name",
    "accession_nodash",
    "filing_date",
    "form",
    "metric",
    "guidance_year",
    "period_label",
    "low_value",
    "high_value",
    "midpoint_value",
    "unit",
    "currency",
    "confidence",
    "source_excerpt",
]

FEATURE_FIELDS = [
    "asof_date",
    "ticker",
    "company_name",
    "latest_guidance_filing_date",
    "forward_revenue_midpoint",
    "forward_revenue_low",
    "forward_revenue_high",
    "forward_revenue_year",
    "forward_revenue_growth_pct",
    "forward_ebitda_midpoint",
    "forward_ebitda_margin_pct",
    "forward_eps_midpoint",
    "guidance_confidence",
    "guidance_recency_days",
    "forward_profitability_flag",
    "guidance_score",
    "forward_growth_score",
    "forward_profitability_score",
    "forward_valuation_score",
    "data_quality",
    "missing_fields",
    "payload_json",
]


def chunked(values: list[Any] | tuple[Any, ...], size: int = SQLITE_PARAM_CHUNK_SIZE) -> list[list[Any]]:
    step = max(1, int(size))
    return [list(values[start : start + step]) for start in range(0, len(values), step)]


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


def sortable_date_int(raw: object) -> int:
    parsed = parse_date(raw)
    if parsed is not None:
        return int(parsed.strftime("%Y%m%d"))
    return int(re.sub(r"\D", "", str(raw or "")) or "0")


def filing_sort_key(filing: "FilingText") -> tuple[str, int, str]:
    date_key = sortable_date_int(filing.filing_date)
    return filing.ticker, -date_key, filing.accession_nodash

OVERRIDE_FIELDS = [
    "enabled",
    "ticker",
    "metric",
    "guidance_year",
    "period_label",
    "low_value",
    "high_value",
    "midpoint_value",
    "unit",
    "currency",
    "filing_date",
    "form",
    "confidence",
    "source_name",
    "source_url",
    "source_excerpt",
    "override_reason",
]

METRIC_PATTERNS: dict[str, tuple[str, ...]] = {
    "revenue": (
        r"net revenue",
        r"total revenue",
        r"product revenue",
        r"revenues?",
        r"net sales",
        r"sales",
    ),
    "adjusted_ebitda": (
        r"adjusted ebitda",
        r"adjusted earnings before interest, taxes, depreciation and amortization",
    ),
    "ebitda": (r"\bebitda\b",),
    "adjusted_eps": (r"adjusted (?:diluted )?eps", r"adjusted earnings per share"),
    "eps": (r"(?:diluted )?eps", r"earnings per share"),
}

GUIDANCE_TERMS = re.compile(
    r"\b(guidance|outlook|expects?|anticipates?|projects?|forecasts?|guides?|reiterates?|reaffirms?|raises?|lowers?|full year|fiscal year|fy\s*20\d{2})\b",
    re.IGNORECASE,
)
FORWARD_LOOKING_ONLY = re.compile(
    r"\b(forward-looking statements?|safe harbor|actual results may differ|risk factors|there can be no assurance)\b",
    re.IGNORECASE,
)
YEAR_RE = re.compile(r"\b(20\d{2})\b")
TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")
AMOUNT_RE = r"(?:\$\s*(\d+(?:,\d{3})*(?:\.\d+)?)\s*(billion|million|bn|mm|m|b)?|(\d+(?:,\d{3})*(?:\.\d+)?)\s*(billion|million|bn|mm|m|b))"
RANGE_CONNECTOR_RE = r"(?:\s*(?:-|–|—|to|and|through)\s*)"
GUIDANCE_CUE_RE = re.compile(
    r"\b(guidance|outlook|expects?|expected|anticipates?|anticipated|projects?|projected|forecasts?|forecasted|guides?|range|reaffirms?|reiterates?|provides|provided|raise|raises|lower|lowers|target)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class FilingText:
    company_id: int
    ticker: str
    company_name: str
    accession_nodash: str
    filing_date: str
    form: str
    archive_url: str
    document_type: str
    text_content: str
    text_hash: str


@dataclass(frozen=True)
class CompleteSubmissionFetch:
    filing: FilingText
    url: str
    text: str
    error: str


@dataclass(frozen=True)
class GuidanceRecord:
    asof_date: str
    company_id: int
    ticker: str
    company_name: str
    accession_nodash: str
    filing_date: str
    form: str
    metric: str
    guidance_year: int | None
    period_label: str
    low_value: float | None
    high_value: float | None
    midpoint_value: float | None
    unit: str
    currency: str
    confidence: float
    source_excerpt: str
    source_payload: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Parse SEC filings for forward revenue/EBITDA/EPS guidance and build daily guidance features.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", type=str, default="", help="As-of date in YYYY-MM-DD. Defaults to UTC today.")
    parser.add_argument("--tickers", type=str, default="", help="Optional comma-separated ticker subset.")
    parser.add_argument("--max-companies", type=int, default=0, help="Smoke-test company limit. 0 means all.")
    parser.add_argument(
        "--run-mode",
        choices=["daily_delta", "weekly_reconcile", "full_backfill"],
        default="",
        help="Refresh mode. Defaults to forward_guidance.run_mode from config.",
    )
    parser.add_argument("--full-rescan", action="store_true", help="Alias for --run-mode full_backfill.")
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
        return None


def to_float(raw: object) -> float | None:
    if raw is None:
        return None
    try:
        value = float(str(raw).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    return value


def to_int(raw: object) -> int | None:
    value = to_float(raw)
    if value is None:
        return None
    return int(value)


def enabled_flag(raw: object) -> bool:
    text = str(raw or "").strip().lower()
    if text in {"1", "true", "yes", "y", "enabled", "include"}:
        return True
    if text == "":
        return False
    if text in {"0", "false", "no", "n", "disabled", "exclude"}:
        return False
    raise ValueError(f"Invalid enabled value: {raw}")


def config_bool(raw: object, default: bool = False) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "y", "enabled", "on"}:
        return True
    if text in {"0", "false", "no", "n", "disabled", "off"}:
        return False
    return default


def safe_json_loads(raw: object) -> dict[str, Any]:
    try:
        payload = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def clean_text(raw: str) -> str:
    text = html.unescape(str(raw or ""))
    text = TAG_RE.sub(" ", text)
    text = text.replace("\xa0", " ")
    return WHITESPACE_RE.sub(" ", text).strip()


def money_value(raw_number: str, raw_unit: str | None, default_unit: str | None, *, per_share: bool = False) -> tuple[float | None, str]:
    value = to_float(raw_number)
    if value is None:
        return None, ""
    if per_share:
        return value, "per_share"
    unit = (raw_unit or default_unit or "").strip().lower()
    if unit in {"billion", "bn", "b"}:
        return value * 1_000_000_000.0, "usd"
    if unit in {"million", "mm", "m"}:
        return value * 1_000_000.0, "usd"
    # Guidance in biotech filings normally uses dollars; if no unit is present,
    # treat values above 1,000 as absolute dollars and smaller values as millions.
    if value >= 1_000:
        return value, "usd"
    return value * 1_000_000.0, "usd"


def amount_from_groups(groups: tuple[str | None, ...], start: int, default_unit: str | None, *, per_share: bool = False) -> tuple[float | None, str, str | None]:
    dollar_number = groups[start]
    dollar_unit = groups[start + 1]
    bare_number = groups[start + 2]
    bare_unit = groups[start + 3]
    number = dollar_number or bare_number
    unit = dollar_unit or bare_unit or default_unit
    value, normalized_unit = money_value(str(number or ""), unit, default_unit, per_share=per_share)
    return value, normalized_unit, unit


def pct_change(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None or previous == 0:
        return None
    return (current - previous) / abs(previous)


def metric_regex(metric: str) -> str:
    return r"(?:" + "|".join(METRIC_PATTERNS[metric]) + r")"


def extract_year(window: str, asof_date: date) -> int | None:
    years = [int(match) for match in YEAR_RE.findall(window)]
    future_years = [year for year in years if asof_date.year <= year <= asof_date.year + 5]
    if future_years:
        return min(future_years)
    return years[0] if years else None


def extract_period_label(window: str, guidance_year: int | None) -> str:
    lower = window.lower()
    if guidance_year:
        if "full year" in lower or "fiscal year" in lower or f"fy {guidance_year}" in lower or f"fy{guidance_year}" in lower:
            return f"FY{guidance_year}"
        return str(guidance_year)
    if "full year" in lower:
        return "FY"
    if "quarter" in lower:
        return "quarter"
    return ""


def parse_metric_values(metric: str, window: str) -> tuple[float | None, float | None, float | None, str] | None:
    metric_pat = metric_regex(metric)
    per_share = metric in {"eps", "adjusted_eps"}
    range_after = re.compile(
        metric_pat + r".{0,160}?" + AMOUNT_RE + RANGE_CONNECTOR_RE + AMOUNT_RE,
        re.IGNORECASE | re.DOTALL,
    )
    range_before = re.compile(
        AMOUNT_RE + RANGE_CONNECTOR_RE + AMOUNT_RE + r".{0,160}?" + metric_pat,
        re.IGNORECASE | re.DOTALL,
    )
    single_after = re.compile(metric_pat + r".{0,140}?" + AMOUNT_RE, re.IGNORECASE | re.DOTALL)

    def has_metric_guidance_cue(snippet: str) -> bool:
        text = " ".join(str(snippet or "").lower().split())
        if any(term in text for term in ("not expected to reoccur", "not expected to recur", "compared to", "year-over-year to")):
            return False
        if "guidance" in text or "outlook" in text:
            return True
        if metric == "revenue":
            return bool(
                re.search(r"\b(expect|expects|expected|anticipate|anticipates|project|projects|forecast|forecasts|guide|guides|target|targets)\b.{0,80}\b(revenue|revenues|sales)\b", text)
                or re.search(r"\b(revenue|revenues|sales)\b.{0,80}\b(expected|anticipated|projected|forecast|targeted|range)\b", text)
            )
        if metric in {"adjusted_ebitda", "ebitda"}:
            return bool(re.search(r"\b(expect|expected|guidance|outlook|project|forecast|target|range)\b.{0,100}\bebitda\b", text) or re.search(r"\bebitda\b.{0,100}\b(expected|guidance|outlook|projected|forecast|target|range)\b", text))
        if metric in {"adjusted_eps", "eps"}:
            return bool(re.search(r"\b(expect|expected|guidance|outlook|project|forecast|target|range)\b.{0,100}\b(eps|earnings per share)\b", text) or re.search(r"\b(eps|earnings per share)\b.{0,100}\b(expected|guidance|outlook|projected|forecast|target|range)\b", text))
        return bool(GUIDANCE_CUE_RE.search(snippet))

    for pattern in (range_after, range_before):
        matches = list(pattern.finditer(window))
        for match in matches:
            snippet = match.group(0)
            if not has_metric_guidance_cue(snippet):
                continue
            groups = match.groups()
            first_unit = groups[1] or groups[3]
            second_unit = groups[5] or groups[7]
            default_unit = second_unit or first_unit
            low, unit, _ = amount_from_groups(groups, 0, default_unit, per_share=per_share)
            high, _, _ = amount_from_groups(groups, 4, default_unit, per_share=per_share)
            if low is None or high is None:
                continue
            if low > high:
                low, high = high, low
            return low, high, (low + high) / 2.0, unit

    for match in single_after.finditer(window):
        snippet = match.group(0)
        if not has_metric_guidance_cue(snippet):
            continue
        groups = match.groups()
        value, normalized_unit, _ = amount_from_groups(groups, 0, None, per_share=per_share)
        if value is not None:
            return value, value, value, normalized_unit
    return None


def confidence_for(*, form: str, window: str, has_range: bool, filing_date: date | None, asof_date: date) -> float:
    lower = window.lower()
    score = 0.62
    if form.upper() in {"8-K", "6-K"}:
        score += 0.14
    elif form.upper() in {"10-K", "10-Q", "20-F", "40-F"}:
        score += 0.08
    if any(term in lower for term in ("guidance", "outlook", "expects", "anticipates", "projects", "reaffirms", "reiterates")):
        score += 0.10
    if has_range:
        score += 0.06
    if filing_date is not None:
        age = max(0, (asof_date - filing_date).days)
        if age <= 120:
            score += 0.08
        elif age <= 365:
            score += 0.04
    if FORWARD_LOOKING_ONLY.search(window):
        score -= 0.18
    return round(clamp(score, 0.0, 0.98), 4)


def guidance_windows(text: str, *, max_windows: int) -> list[str]:
    windows: list[str] = []
    for match in GUIDANCE_TERMS.finditer(text):
        start = max(0, match.start() - 280)
        end = min(len(text), match.end() + 520)
        window = text[start:end].strip()
        if window and window not in windows:
            windows.append(window)
        if len(windows) >= max_windows:
            break
    return windows


def detect_guidance(filing: FilingText, *, asof_date: date, min_confidence: float, max_windows: int) -> list[GuidanceRecord]:
    text = clean_text(filing.text_content)
    if not text:
        return []
    filing_dt = parse_date(filing.filing_date)
    records: list[GuidanceRecord] = []
    seen: set[tuple[str, int | None, str, str]] = set()
    for window in guidance_windows(text, max_windows=max_windows):
        lower_window = window.lower()
        if (
            ("preliminary " in lower_window or "preliminary unaudited" in lower_window or "results of operations" in lower_window or "fiscal year ended" in lower_window)
            and "guidance" not in lower_window
            and "outlook" not in lower_window
        ):
            continue
        if not YEAR_RE.search(window):
            continue
        guidance_year = extract_year(window, asof_date)
        if guidance_year is not None and guidance_year < asof_date.year:
            continue
        period_label = extract_period_label(window, guidance_year)
        for metric in METRIC_PATTERNS:
            if not re.search(metric_regex(metric), window, re.IGNORECASE):
                continue
            parsed = parse_metric_values(metric, window)
            if parsed is None:
                continue
            low_value, high_value, midpoint_value, unit = parsed
            if midpoint_value is None:
                continue
            has_range = low_value is not None and high_value is not None and abs(high_value - low_value) > 1e-9
            confidence = confidence_for(form=filing.form, window=window, has_range=has_range, filing_date=filing_dt, asof_date=asof_date)
            if confidence < min_confidence:
                continue
            key = (
                metric,
                guidance_year,
                normalize_guidance_number(low_value, null_token="<NULL>"),
                normalize_guidance_number(high_value, null_token="<NULL>"),
            )
            if key in seen:
                continue
            seen.add(key)
            payload = {
                "detector": "regex_guidance_v1",
                "window_length": len(window),
                "form": filing.form,
            }
            records.append(
                GuidanceRecord(
                    asof_date=asof_date.isoformat(),
                    company_id=filing.company_id,
                    ticker=filing.ticker,
                    company_name=filing.company_name,
                    accession_nodash=filing.accession_nodash,
                    filing_date=filing.filing_date,
                    form=filing.form,
                    metric=metric,
                    guidance_year=guidance_year,
                    period_label=period_label,
                    low_value=low_value,
                    high_value=high_value,
                    midpoint_value=midpoint_value,
                    unit=unit,
                    currency="USD" if unit == "usd" else "",
                    confidence=confidence,
                    source_excerpt=window[:900],
                    source_payload=json.dumps(payload, ensure_ascii=True, sort_keys=True),
                )
            )
    return records


def read_scoring_tickers(path: Path) -> set[str]:
    return read_final_scoring_tickers(path)


def load_companies(conn: sqlite3.Connection, *, scoring_tickers: set[str], ticker_filter: set[str], max_companies: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT company_id, ticker, company_name
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


def guidance_record_id(*, ticker: str, metric: str, guidance_year: int | None, midpoint_value: float | None, source_name: str) -> str:
    raw = "|".join(
        [
            ticker.upper(),
            metric,
            "" if guidance_year is None else str(guidance_year),
            f"{midpoint_value:.4f}" if midpoint_value is not None else "",
            source_name,
        ]
    )
    return "MANUAL_OVERRIDE_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20].upper()


def normalize_override_metric(raw: object) -> str:
    metric = str(raw or "").strip().lower()
    aliases = {
        "net_revenue": "revenue",
        "total_revenue": "revenue",
        "sales": "revenue",
        "revenue_guidance": "revenue",
        "adj_ebitda": "adjusted_ebitda",
        "adjusted ebitda": "adjusted_ebitda",
        "adj_eps": "adjusted_eps",
        "adjusted eps": "adjusted_eps",
    }
    metric = aliases.get(metric, metric)
    if metric not in METRIC_PATTERNS:
        raise ValueError(f"Unsupported guidance metric: {raw}")
    return metric


def load_guidance_overrides(path: Path | None, companies_by_ticker: dict[str, dict[str, Any]], *, asof_date: date) -> list[GuidanceRecord]:
    if path is None:
        return []
    if not path.exists():
        LOGGER.warning("Forward guidance overrides file not found; continuing without overrides: %s", path)
        return []

    records: list[GuidanceRecord] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing_headers = [field for field in OVERRIDE_FIELDS if field not in (reader.fieldnames or [])]
        if missing_headers:
            raise ValueError(f"Forward guidance overrides file is missing required columns: {', '.join(missing_headers)}")
        for line_no, row in enumerate(reader, start=2):
            try:
                if not enabled_flag(row.get("enabled")):
                    continue
                ticker = str(row.get("ticker") or "").strip().upper()
                if not ticker:
                    raise ValueError("ticker is required")
                company = companies_by_ticker.get(ticker)
                if company is None:
                    LOGGER.warning("Ignoring forward guidance override for ticker outside scoring universe at %s:%d: %s", path, line_no, ticker)
                    continue
                metric = normalize_override_metric(row.get("metric"))
                low_value = to_float(row.get("low_value"))
                high_value = to_float(row.get("high_value"))
                midpoint_value = to_float(row.get("midpoint_value"))
                if midpoint_value is None and low_value is not None and high_value is not None:
                    midpoint_value = (low_value + high_value) / 2.0
                elif midpoint_value is None:
                    midpoint_value = low_value if low_value is not None else high_value
                if midpoint_value is None:
                    raise ValueError("one of low_value, high_value, midpoint_value is required")
                guidance_year = to_int(row.get("guidance_year"))
                filing_dt = parse_date(row.get("filing_date")) or asof_date
                source_name = str(row.get("source_name") or "manual_forward_guidance_override").strip()
                source_url = str(row.get("source_url") or "").strip()
                source_excerpt = str(row.get("source_excerpt") or "").strip()
                override_reason = str(row.get("override_reason") or "").strip()
                if not source_url and not source_excerpt:
                    raise ValueError("source_url or source_excerpt is required")
                confidence_raw = to_float(row.get("confidence"))
                confidence = clamp(confidence_raw if confidence_raw is not None else 0.75, 0.0, 1.0)
                unit = str(row.get("unit") or ("per_share" if metric in {"adjusted_eps", "eps"} else "usd")).strip().lower()
                currency = str(row.get("currency") or ("USD" if unit == "usd" else "")).strip().upper()
                period_label = str(row.get("period_label") or (f"FY{guidance_year}" if guidance_year else "FY")).strip()
                payload = {
                    "detector": "manual_forward_guidance_override_v1",
                    "source_name": source_name,
                    "source_url": source_url,
                    "override_reason": override_reason,
                    "line_no": line_no,
                }
                records.append(
                    GuidanceRecord(
                        asof_date=asof_date.isoformat(),
                        company_id=int(company["company_id"]),
                        ticker=ticker,
                        company_name=str(company.get("company_name") or ""),
                        accession_nodash=guidance_record_id(
                            ticker=ticker,
                            metric=metric,
                            guidance_year=guidance_year,
                            midpoint_value=midpoint_value,
                            source_name=source_name,
                        ),
                        filing_date=filing_dt.isoformat(),
                        form=str(row.get("form") or "MANUAL").strip().upper(),
                        metric=metric,
                        guidance_year=guidance_year,
                        period_label=period_label,
                        low_value=low_value,
                        high_value=high_value,
                        midpoint_value=midpoint_value,
                        unit=unit,
                        currency=currency,
                        confidence=confidence,
                        source_excerpt=source_excerpt[:900],
                        source_payload=json.dumps(payload, ensure_ascii=True, sort_keys=True),
                    )
                )
            except Exception as exc:
                LOGGER.warning("Ignoring invalid forward guidance override at %s:%d: %s", path, line_no, exc)
    return records


def load_filing_texts(
    conn: sqlite3.Connection,
    *,
    company_id: int,
    asof_date: date,
    lookback_days: int,
    forms: set[str],
    max_filings: int,
) -> list[FilingText]:
    cutoff = (asof_date - timedelta(days=max(1, lookback_days))).isoformat()
    placeholders = ",".join("?" for _ in forms)
    form_clause = f"AND f.form IN ({placeholders})" if forms else ""
    params: list[Any] = [company_id, cutoff, asof_date.isoformat()]
    if forms:
        params.extend(sorted(forms))
    params.append(max(1, max_filings))
    rows = conn.execute(
        f"""
        SELECT
            f.company_id, c.ticker, c.company_name, f.accession_nodash,
            f.filing_date, f.form, f.archive_url, d.document_type, d.text_content,
            COALESCE(d.text_hash, '') AS source_text_hash
        FROM sec_filings f
        JOIN companies c ON c.company_id = f.company_id
        JOIN sec_filing_documents d ON d.accession_nodash = f.accession_nodash
        WHERE f.company_id = ?
          AND f.filing_date >= ?
          AND f.filing_date <= ?
          {form_clause}
          AND d.text_content IS NOT NULL
          AND LENGTH(d.text_content) > 0
        ORDER BY f.filing_date DESC, f.accession_nodash DESC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    return [
        FilingText(
            company_id=int(row["company_id"]),
            ticker=str(row["ticker"] or "").upper(),
            company_name=str(row["company_name"] or ""),
            accession_nodash=str(row["accession_nodash"] or ""),
            filing_date=str(row["filing_date"] or ""),
            form=str(row["form"] or ""),
            archive_url=str(row["archive_url"] or ""),
            document_type=str(row["document_type"] or ""),
            text_content=str(row["text_content"] or ""),
            text_hash=str(row["source_text_hash"] or "") or text_hash(str(row["text_content"] or "")),
        )
        for row in rows
    ]


def load_filing_texts_bulk(
    conn: sqlite3.Connection,
    *,
    company_ids: list[int],
    asof_date: date,
    lookback_days: int,
    forms: set[str],
    max_filings_per_company: int,
) -> list[FilingText]:
    if not company_ids:
        return []
    if len(company_ids) > SQLITE_PARAM_CHUNK_SIZE:
        out: list[FilingText] = []
        for company_chunk in chunked(company_ids):
            out.extend(
                load_filing_texts_bulk(
                    conn,
                    company_ids=[int(value) for value in company_chunk],
                    asof_date=asof_date,
                    lookback_days=lookback_days,
                    forms=forms,
                    max_filings_per_company=max_filings_per_company,
                )
            )
        out.sort(key=filing_sort_key)
        return out
    cutoff = (asof_date - timedelta(days=max(1, lookback_days))).isoformat()
    company_placeholders = ",".join("?" for _ in company_ids)
    form_clause = ""
    params: list[Any] = [*company_ids, cutoff, asof_date.isoformat()]
    if forms:
        form_clause = f"AND f.form IN ({','.join('?' for _ in forms)})"
        params.extend(sorted(forms))
    params.append(max(1, max_filings_per_company))
    rows = conn.execute(
        f"""
        WITH target_filings AS (
            SELECT f.company_id, c.ticker, c.company_name, f.accession_nodash,
                   f.filing_date, f.form, f.archive_url, f.text_hash AS filing_text_hash
            FROM sec_filings f
            JOIN companies c ON c.company_id = f.company_id
            WHERE f.company_id IN ({company_placeholders})
              AND f.filing_date >= ?
              AND f.filing_date <= ?
              {form_clause}
        ),
        ranked_docs AS (
            SELECT d.accession_nodash, d.document_type, d.text_content, d.text_hash,
                   ROW_NUMBER() OVER (
                       PARTITION BY d.accession_nodash
                       ORDER BY
                           CASE WHEN d.document_type = 'complete_submission_text' THEN 0 ELSE 1 END,
                           d.fetched_at DESC,
                           d.document_url DESC
                   ) AS doc_rank
            FROM sec_filing_documents d
            JOIN target_filings f ON f.accession_nodash = d.accession_nodash
            WHERE d.text_content IS NOT NULL
              AND LENGTH(d.text_content) > 0
        ),
        latest_docs AS (
            SELECT accession_nodash, document_type, text_content, text_hash
            FROM ranked_docs
            WHERE doc_rank = 1
        ),
        ranked_filings AS (
            SELECT f.*, d.document_type, d.text_content,
                   COALESCE(d.text_hash, '') AS source_text_hash,
                   ROW_NUMBER() OVER (
                       PARTITION BY f.company_id
                       ORDER BY f.filing_date DESC, f.accession_nodash DESC
                   ) AS filing_rank
            FROM target_filings f
            JOIN latest_docs d ON d.accession_nodash = f.accession_nodash
        )
        SELECT *
        FROM ranked_filings
        WHERE filing_rank <= ?
        ORDER BY ticker, filing_date DESC, accession_nodash DESC
        """,
        tuple(params),
    ).fetchall()
    return [
        FilingText(
            company_id=int(row["company_id"]),
            ticker=str(row["ticker"] or "").upper(),
            company_name=str(row["company_name"] or ""),
            accession_nodash=str(row["accession_nodash"] or ""),
            filing_date=str(row["filing_date"] or ""),
            form=str(row["form"] or ""),
            archive_url=str(row["archive_url"] or ""),
            document_type=str(row["document_type"] or ""),
            text_content=str(row["text_content"] or ""),
            text_hash=str(row["source_text_hash"] or "") or text_hash(str(row["text_content"] or "")),
        )
        for row in rows
    ]


def load_filing_metadata_bulk(
    conn: sqlite3.Connection,
    *,
    company_ids: list[int],
    asof_date: date,
    lookback_days: int,
    forms: set[str],
    max_filings_per_company: int,
) -> list[FilingText]:
    """Select candidate filings without loading large SEC document text bodies."""
    if not company_ids:
        return []
    if len(company_ids) > SQLITE_PARAM_CHUNK_SIZE:
        out: list[FilingText] = []
        for company_chunk in chunked(company_ids):
            out.extend(
                load_filing_metadata_bulk(
                    conn,
                    company_ids=[int(value) for value in company_chunk],
                    asof_date=asof_date,
                    lookback_days=lookback_days,
                    forms=forms,
                    max_filings_per_company=max_filings_per_company,
                )
            )
        out.sort(key=filing_sort_key)
        return out
    cutoff = (asof_date - timedelta(days=max(1, lookback_days))).isoformat()
    company_placeholders = ",".join("?" for _ in company_ids)
    form_clause = ""
    params: list[Any] = [*company_ids, cutoff, asof_date.isoformat()]
    if forms:
        form_clause = f"AND f.form IN ({','.join('?' for _ in forms)})"
        params.extend(sorted(forms))
    params.append(max(1, max_filings_per_company))
    rows = conn.execute(
        f"""
        WITH target_filings AS (
            SELECT f.company_id, c.ticker, c.company_name, f.accession_nodash,
                   f.filing_date, f.form, f.archive_url
            FROM sec_filings f
            JOIN companies c ON c.company_id = f.company_id
            WHERE f.company_id IN ({company_placeholders})
              AND f.filing_date >= ?
              AND f.filing_date <= ?
              {form_clause}
        ),
        latest_docs AS (
            SELECT f.accession_nodash, l.document_type, l.text_hash
            FROM target_filings f
            JOIN sec_filing_latest_document l ON l.accession_nodash = f.accession_nodash
            UNION ALL
            SELECT accession_nodash, document_type, text_hash
            FROM (
                SELECT d.accession_nodash, d.document_type, d.text_hash,
                       ROW_NUMBER() OVER (
                           PARTITION BY d.accession_nodash
                           ORDER BY
                               CASE WHEN d.document_type = 'complete_submission_text' THEN 0 ELSE 1 END,
                               d.fetched_at DESC,
                               d.document_url DESC
                       ) AS doc_rank
                FROM sec_filing_documents d
                JOIN target_filings f ON f.accession_nodash = d.accession_nodash
                LEFT JOIN sec_filing_latest_document l ON l.accession_nodash = d.accession_nodash
                WHERE l.accession_nodash IS NULL
                  AND (
                    COALESCE(d.text_length, 0) > 0
                    OR d.text_hash IS NOT NULL
                    OR d.text_content IS NOT NULL
                  )
            )
            WHERE doc_rank = 1
        ),
        ranked_filings AS (
            SELECT f.*, d.document_type,
                   COALESCE(d.text_hash, '') AS source_text_hash,
                   ROW_NUMBER() OVER (
                       PARTITION BY f.company_id
                       ORDER BY f.filing_date DESC, f.accession_nodash DESC
                   ) AS filing_rank
            FROM target_filings f
            JOIN latest_docs d ON d.accession_nodash = f.accession_nodash
        )
        SELECT *
        FROM ranked_filings
        WHERE filing_rank <= ?
        ORDER BY ticker, filing_date DESC, accession_nodash DESC
        """,
        tuple(params),
    ).fetchall()
    return [
        FilingText(
            company_id=int(row["company_id"]),
            ticker=str(row["ticker"] or "").upper(),
            company_name=str(row["company_name"] or ""),
            accession_nodash=str(row["accession_nodash"] or ""),
            filing_date=str(row["filing_date"] or ""),
            form=str(row["form"] or ""),
            archive_url=str(row["archive_url"] or ""),
            document_type=str(row["document_type"] or ""),
            text_content="",
            text_hash=str(row["source_text_hash"] or ""),
        )
        for row in rows
    ]


def load_filing_text_content_bulk(conn: sqlite3.Connection, filings: list[FilingText]) -> list[FilingText]:
    """Load full document text only for filings that must be parsed."""
    if not filings:
        return []
    filing_by_accession = {filing.accession_nodash: filing for filing in filings}
    text_by_accession: dict[str, dict[str, Any]] = {}
    accessions = list(filing_by_accession)
    chunk_size = SQLITE_PARAM_CHUNK_SIZE
    for start in range(0, len(accessions), chunk_size):
        chunk = accessions[start : start + chunk_size]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            SELECT accession_nodash, document_type, text_content, text_hash
            FROM (
                SELECT d.accession_nodash, d.document_type, d.text_content, d.text_hash,
                       ROW_NUMBER() OVER (
                           PARTITION BY d.accession_nodash
                           ORDER BY
                               CASE WHEN d.document_type = 'complete_submission_text' THEN 0 ELSE 1 END,
                               d.fetched_at DESC,
                               d.document_url DESC
                       ) AS doc_rank
                FROM sec_filing_documents d
                WHERE d.accession_nodash IN ({placeholders})
                  AND d.text_content IS NOT NULL
                  AND LENGTH(d.text_content) > 0
            )
            WHERE doc_rank = 1
            """,
            tuple(chunk),
        ).fetchall()
        for row in rows:
            text_by_accession[str(row["accession_nodash"] or "")] = dict(row)

    out: list[FilingText] = []
    for filing in filings:
        row = text_by_accession.get(filing.accession_nodash)
        if not row:
            out.append(filing)
            continue
        text = str(row.get("text_content") or "")
        out.append(
            FilingText(
                **{
                    **filing.__dict__,
                    "document_type": str(row.get("document_type") or filing.document_type),
                    "text_content": text,
                    "text_hash": str(row.get("text_hash") or "") or text_hash(text),
                }
            )
        )
    return out


def dashed_accession(accession_nodash: str) -> str:
    text = str(accession_nodash or "")
    if len(text) != 18 or not text.isdigit():
        raise ValueError(f"Malformed SEC accession_nodash for complete submission fetch: {text!r}")
    return f"{text[:10]}-{text[10:12]}-{text[12:]}"


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def build_parser_signature(
    *,
    forms: set[str],
    lookback_days: int,
    max_filings_per_company: int,
    max_windows_per_filing: int,
    min_confidence: float,
    fetch_complete: bool,
    fetch_forms: set[str],
) -> str:
    payload = {
        "logic_version": PARSER_LOGIC_VERSION,
        "forms": sorted(forms),
        "lookback_days": int(lookback_days),
        "max_filings_per_company": int(max_filings_per_company),
        "max_windows_per_filing": int(max_windows_per_filing),
        "min_confidence": round(float(min_confidence), 6),
        "fetch_complete": bool(fetch_complete),
        "fetch_forms": sorted(fetch_forms),
        "metric_patterns": METRIC_PATTERNS,
    }
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def load_complete_submission_document(conn: sqlite3.Connection, accession_nodash: str) -> str:
    row = conn.execute(
        """
        SELECT text_content
        FROM sec_filing_documents
        WHERE accession_nodash = ?
          AND document_type = 'complete_submission_text'
          AND text_content IS NOT NULL
          AND LENGTH(text_content) > 0
        ORDER BY fetched_at DESC
        LIMIT 1
        """,
        (accession_nodash,),
    ).fetchone()
    return str(row["text_content"] or "") if row else ""


def upsert_complete_submission_document(conn: sqlite3.Connection, filing: FilingText, url: str, text: str) -> None:
    now = utc_now()
    digest = text_hash(text)
    with conn:
        conn.execute(
            """
            INSERT INTO sec_filing_documents(
                accession_nodash, document_url, document_type, text_content, text_hash, fetched_at, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(accession_nodash, document_url) DO UPDATE SET
                document_type = excluded.document_type,
                text_content = excluded.text_content,
                text_hash = excluded.text_hash,
                fetched_at = excluded.fetched_at,
                updated_at = excluded.updated_at
            """,
            (filing.accession_nodash, url, "complete_submission_text", text, digest, now, now, now),
        )


def ensure_complete_submission_text(
    conn: sqlite3.Connection,
    http: CachedHttpClient | None,
    filing: FilingText,
    *,
    headers: dict[str, str],
    ttl_hours: float,
    fetch_enabled: bool,
    fetch_forms: set[str],
) -> FilingText:
    if filing.document_type == "complete_submission_text" or filing.text_content.lstrip().startswith("<SEC-DOCUMENT>"):
        return filing
    cached = load_complete_submission_document(conn, filing.accession_nodash)
    if cached:
        return FilingText(**{**filing.__dict__, "document_type": "complete_submission_text", "text_content": cached, "text_hash": text_hash(cached)})
    if not fetch_enabled or http is None or not filing.archive_url or filing.form.upper() not in fetch_forms:
        return filing
    try:
        url = f"{filing.archive_url}/{dashed_accession(filing.accession_nodash)}.txt"
        text = http.fetch_text(namespace="sec_complete_submission_text", url=url, headers=headers, ttl_hours=ttl_hours)
        if text.strip():
            upsert_complete_submission_document(conn, filing, url, text)
            return FilingText(**{**filing.__dict__, "document_type": "complete_submission_text", "text_content": text, "text_hash": text_hash(text)})
    except Exception as exc:
        LOGGER.debug("Complete SEC submission fetch failed for %s %s: %s", filing.ticker, filing.accession_nodash, exc)
    return filing


def should_fetch_complete_submission(filing: FilingText, *, fetch_enabled: bool, fetch_forms: set[str]) -> bool:
    if filing.document_type == "complete_submission_text" or filing.text_content.lstrip().startswith("<SEC-DOCUMENT>"):
        return False
    return bool(fetch_enabled and filing.archive_url and filing.form.upper() in fetch_forms)


def fetch_complete_submission_worker(
    filing: FilingText,
    *,
    cache_dir: Path,
    headers: dict[str, str],
    ttl_hours: float,
    sleep_sec: float,
    timeout_sec: float,
    max_retries: int,
    throttle: HostThrottle,
) -> CompleteSubmissionFetch:
    url = ""
    try:
        url = f"{filing.archive_url}/{dashed_accession(filing.accession_nodash)}.txt"
        with CachedHttpClient(
            cache_dir=cache_dir,
            sleep_sec=sleep_sec,
            timeout_sec=timeout_sec,
            max_retries=max_retries,
            throttle=throttle,
        ) as http:
            text = http.fetch_text(namespace="sec_complete_submission_text", url=url, headers=headers, ttl_hours=ttl_hours)
        return CompleteSubmissionFetch(filing=filing, url=url, text=text if text.strip() else "", error="")
    except Exception as exc:
        return CompleteSubmissionFetch(filing=filing, url=url, text="", error=f"{type(exc).__name__}: {exc}")


def prepare_filing_texts(
    conn: sqlite3.Connection,
    filings: list[FilingText],
    *,
    headers: dict[str, str],
    ttl_hours: float,
    fetch_enabled: bool,
    fetch_forms: set[str],
    cache_dir: Path,
    sleep_sec: float,
    timeout_sec: float,
    max_retries: int,
    max_workers: int,
) -> list[FilingText]:
    if not filings:
        return []
    if max_workers <= 1:
        throttle = HostThrottle()
        with CachedHttpClient(
            cache_dir=cache_dir,
            sleep_sec=sleep_sec,
            timeout_sec=timeout_sec,
            max_retries=max_retries,
            throttle=throttle,
        ) as http:
            return [
                ensure_complete_submission_text(
                    conn,
                    http,
                    filing,
                    headers=headers,
                    ttl_hours=ttl_hours,
                    fetch_enabled=fetch_enabled,
                    fetch_forms=fetch_forms,
                )
                for filing in filings
            ]

    fetch_targets = [filing for filing in filings if should_fetch_complete_submission(filing, fetch_enabled=fetch_enabled, fetch_forms=fetch_forms)]
    if not fetch_targets:
        return filings

    throttle = HostThrottle()
    fetched_by_accession: dict[str, CompleteSubmissionFetch] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                fetch_complete_submission_worker,
                filing,
                cache_dir=cache_dir,
                headers=headers,
                ttl_hours=ttl_hours,
                sleep_sec=sleep_sec,
                timeout_sec=timeout_sec,
                max_retries=max_retries,
                throttle=throttle,
            ): filing
            for filing in fetch_targets
        }
        for future in as_completed(futures):
            result = future.result()
            fetched_by_accession[result.filing.accession_nodash] = result
            if result.error:
                LOGGER.debug("Complete SEC submission fetch failed for %s %s: %s", result.filing.ticker, result.filing.accession_nodash, result.error)

    for result in fetched_by_accession.values():
        if result.text:
            upsert_complete_submission_document(conn, result.filing, result.url, result.text)

    out: list[FilingText] = []
    for filing in filings:
        result = fetched_by_accession.get(filing.accession_nodash)
        if result and result.text:
            out.append(
                FilingText(
                    **{
                        **filing.__dict__,
                        "document_type": "complete_submission_text",
                        "text_content": result.text,
                        "text_hash": text_hash(result.text),
                    }
                )
            )
        else:
            out.append(filing)
    return out


def latest_commercial_rows(conn: sqlite3.Connection, asof_date: date) -> dict[int, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT c.*
        FROM commercial_value_features_daily c
        JOIN (
            SELECT company_id, MAX(asof_date) AS max_asof
            FROM commercial_value_features_daily
            WHERE asof_date <= ?
            GROUP BY company_id
        ) latest
          ON latest.company_id = c.company_id AND latest.max_asof = c.asof_date
        """,
        (asof_date.isoformat(),),
    ).fetchall()
    return {int(row["company_id"]): dict(row) for row in rows}


def load_forward_guidance_parse_state(conn: sqlite3.Connection, accessions: list[str]) -> dict[str, dict[str, Any]]:
    if not accessions:
        return {}
    out: dict[str, dict[str, Any]] = {}
    chunk_size = SQLITE_PARAM_CHUNK_SIZE
    for start in range(0, len(accessions), chunk_size):
        chunk = accessions[start : start + chunk_size]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            SELECT accession_nodash, text_hash, asof_year, parser_signature, parsed_at, guidance_count
            FROM forward_guidance_parse_state
            WHERE accession_nodash IN ({placeholders})
            """,
            tuple(chunk),
        ).fetchall()
        out.update({str(row["accession_nodash"]): dict(row) for row in rows})
    return out


def is_unchanged_guidance_parse(
    filing: FilingText,
    state: dict[str, Any] | None,
    *,
    asof_date: date,
    parser_signature: str,
) -> bool:
    return guidance_parse_reuse_miss_reason(
        filing,
        state,
        asof_date=asof_date,
        parser_signature=parser_signature,
    ) == ""


def guidance_parse_reuse_miss_reason(
    filing: FilingText,
    state: dict[str, Any] | None,
    *,
    asof_date: date,
    parser_signature: str,
) -> str:
    if not str(filing.text_hash or ""):
        return "missing_current_text_hash"
    if not state:
        return "missing_parse_state"
    if not str(state.get("text_hash") or ""):
        return "missing_state_text_hash"
    if int(state.get("asof_year") or 0) != asof_date.year:
        return "asof_year_mismatch"
    if str(state.get("parser_signature") or "") != parser_signature:
        return "parser_signature_mismatch"
    if str(state.get("text_hash") or "") != str(filing.text_hash or ""):
        return "text_hash_mismatch"
    return ""


def load_previous_guidance_records(
    conn: sqlite3.Connection,
    *,
    filings: list[FilingText],
    asof_date: date,
) -> list[GuidanceRecord]:
    accessions = [filing.accession_nodash for filing in filings]
    if not accessions:
        return []
    filing_by_accession = {filing.accession_nodash: filing for filing in filings}
    records: list[GuidanceRecord] = []
    chunk_size = SQLITE_PARAM_CHUNK_SIZE
    for start in range(0, len(accessions), chunk_size):
        chunk = accessions[start : start + chunk_size]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            WITH latest AS (
                SELECT accession_nodash, MAX(asof_date) AS max_asof
                FROM company_forward_guidance
                WHERE accession_nodash IN ({placeholders})
                  AND asof_date <= ?
                GROUP BY accession_nodash
            )
            SELECT g.*
            FROM company_forward_guidance g
            JOIN latest l
              ON l.accession_nodash = g.accession_nodash
             AND l.max_asof = g.asof_date
            ORDER BY g.company_id, g.filing_date DESC, g.metric
            """,
            (*chunk, asof_date.isoformat()),
        ).fetchall()
        for row in rows:
            filing = filing_by_accession.get(str(row["accession_nodash"] or ""))
            records.append(
                GuidanceRecord(
                    asof_date=asof_date.isoformat(),
                    company_id=int(row["company_id"]),
                    ticker=str(row["ticker"] or (filing.ticker if filing else "")).upper(),
                    company_name=str(row["company_name"] or (filing.company_name if filing else "")),
                    accession_nodash=str(row["accession_nodash"] or ""),
                    filing_date=str(row["filing_date"] or ""),
                    form=str(row["form"] or ""),
                    metric=str(row["metric"] or ""),
                    guidance_year=to_int(row["guidance_year"]),
                    period_label=str(row["period_label"] or ""),
                    low_value=to_float(row["low_value"]),
                    high_value=to_float(row["high_value"]),
                    midpoint_value=to_float(row["midpoint_value"]),
                    unit=str(row["unit"] or ""),
                    currency=str(row["currency"] or ""),
                    confidence=to_float(row["confidence"]) or 0.0,
                    source_excerpt=str(row["source_excerpt"] or ""),
                    source_payload=str(row["source_payload"] or "{}"),
                )
            )
    return records


def upsert_forward_guidance_parse_state(
    conn: sqlite3.Connection,
    *,
    filings: list[FilingText],
    guidance_counts: dict[str, int],
    asof_date: date,
    parser_signature: str,
) -> None:
    if not filings:
        return
    now = utc_now()
    with conn:
        conn.executemany(
            """
            INSERT INTO forward_guidance_parse_state(
                accession_nodash, text_hash, asof_year, parser_signature, parsed_at, guidance_count, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(accession_nodash) DO UPDATE SET
                text_hash = excluded.text_hash,
                asof_year = excluded.asof_year,
                parser_signature = excluded.parser_signature,
                parsed_at = excluded.parsed_at,
                guidance_count = excluded.guidance_count,
                updated_at = excluded.updated_at
            """,
            [
                (
                    filing.accession_nodash,
                    filing.text_hash,
                    asof_date.year,
                    parser_signature,
                    now,
                    int(guidance_counts.get(filing.accession_nodash, 0)),
                    now,
                    now,
                )
                for filing in filings
            ],
        )


def parse_guidance_records(
    filings: list[FilingText],
    *,
    asof_date: date,
    min_confidence: float,
    max_windows_per_filing: int,
    max_workers: int,
) -> tuple[list[GuidanceRecord], dict[str, int]]:
    if not filings:
        return [], {}
    records: list[GuidanceRecord] = []
    counts: dict[str, int] = {}
    if max_workers <= 1:
        for filing in filings:
            filing_records = detect_guidance(
                filing,
                asof_date=asof_date,
                min_confidence=min_confidence,
                max_windows=max_windows_per_filing,
            )
            records.extend(filing_records)
            counts[filing.accession_nodash] = len(filing_records)
        return records, counts

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                detect_guidance,
                filing,
                asof_date=asof_date,
                min_confidence=min_confidence,
                max_windows=max_windows_per_filing,
            ): filing
            for filing in filings
        }
        pending_raise: BaseException | None = None
        for future in as_completed(futures):
            filing = futures[future]
            try:
                filing_records = future.result()
            except BaseException as exc:
                pending_raise = exc
                if isinstance(exc, (SystemExit, KeyboardInterrupt)):
                    LOGGER.warning(
                        "Forward guidance worker interrupted for accession=%s ticker=%s",
                        filing.accession_nodash,
                        filing.ticker,
                    )
                else:
                    LOGGER.exception(
                        "Forward guidance worker failed for accession=%s ticker=%s",
                        filing.accession_nodash,
                        filing.ticker,
                    )
                for other in futures:
                    if other is not future:
                        other.cancel()
                break
            records.extend(filing_records)
            counts[filing.accession_nodash] = len(filing_records)
        if pending_raise is not None:
            raise pending_raise
    records.sort(key=lambda record: (record.ticker, sortable_date_int(record.filing_date), record.accession_nodash, record.metric))
    return records, counts


def latest_guidance_by_metric(records: list[GuidanceRecord], asof_date: date) -> dict[str, GuidanceRecord]:
    out: dict[str, GuidanceRecord] = {}
    def sort_key(record: GuidanceRecord) -> tuple[int, int, float, float]:
        year = record.guidance_year or 0
        if year == asof_date.year:
            year_priority = 3
        elif year == asof_date.year + 1:
            year_priority = 2
        elif year > asof_date.year + 1:
            year_priority = 1
        else:
            year_priority = 0
        return year_priority, sortable_date_int(record.filing_date), record.confidence, record.midpoint_value or 0.0

    for record in records:
        existing = out.get(record.metric)
        if existing is None:
            out[record.metric] = record
            continue
        if sort_key(record) > sort_key(existing):
            out[record.metric] = record
    return out


def score_growth(growth: float | None) -> float:
    if growth is None:
        return 45.0
    if growth < -0.20:
        return 15.0
    if growth < 0.0:
        return 35.0 + growth * 75.0
    if growth < 0.10:
        return 55.0 + growth * 150.0
    if growth < 0.30:
        return 70.0 + (growth - 0.10) * 75.0
    if growth < 0.75:
        return 85.0 + (growth - 0.30) * 25.0
    return 100.0


def score_profitability(ebitda_margin: float | None, eps: float | None) -> float:
    if ebitda_margin is not None:
        if ebitda_margin >= 0.30:
            return 95.0
        if ebitda_margin >= 0.15:
            return 82.0
        if ebitda_margin > 0:
            return 68.0
        return 35.0
    if eps is not None:
        return 75.0 if eps > 0 else 35.0
    return 45.0


def score_forward_valuation(market_cap: float | None, revenue_midpoint: float | None) -> float:
    if market_cap is None or market_cap <= 0 or revenue_midpoint is None or revenue_midpoint <= 0:
        return 50.0
    multiple = market_cap / revenue_midpoint
    if multiple < 1.0:
        return 95.0
    if multiple < 2.0:
        return 88.0
    if multiple < 4.0:
        return 75.0
    if multiple < 7.0:
        return 60.0
    if multiple < 12.0:
        return 42.0
    return 20.0


def build_feature_row(
    *,
    company: dict[str, Any],
    asof_date: date,
    records: list[GuidanceRecord],
    commercial: dict[str, Any] | None,
) -> dict[str, Any]:
    by_metric = latest_guidance_by_metric(records, asof_date)
    revenue = by_metric.get("revenue")
    ebitda = by_metric.get("adjusted_ebitda") or by_metric.get("ebitda")
    eps = by_metric.get("adjusted_eps") or by_metric.get("eps")
    ttm_revenue = to_float((commercial or {}).get("ttm_revenue"))
    market_cap = to_float((commercial or {}).get("market_cap"))
    revenue_mid = revenue.midpoint_value if revenue else None
    revenue_growth = pct_change(revenue_mid, ttm_revenue)
    ebitda_mid = ebitda.midpoint_value if ebitda else None
    ebitda_margin = ebitda_mid / revenue_mid if ebitda_mid is not None and revenue_mid is not None and revenue_mid != 0 else None
    eps_mid = eps.midpoint_value if eps else None
    confidence_values = [record.confidence for record in by_metric.values()]
    confidence = max(confidence_values) if confidence_values else 0.0
    filing_dates = [parse_date(record.filing_date) for record in by_metric.values()]
    valid_dates = [item for item in filing_dates if item is not None]
    latest_filing = max(valid_dates).isoformat() if valid_dates else ""
    recency_days = min((asof_date - item).days for item in valid_dates) if valid_dates else None
    growth_score = score_growth(revenue_growth)
    profitability_score = score_profitability(ebitda_margin, eps_mid)
    valuation_score = score_forward_valuation(market_cap, revenue_mid)
    confidence_score = confidence * 100.0
    forward_profitability_flag = int((ebitda_mid is not None and ebitda_mid > 0) or (eps_mid is not None and eps_mid > 0))
    missing: list[str] = []
    if revenue_mid is None:
        missing.append("forward_revenue_guidance")
    if ebitda_mid is None and eps_mid is None:
        missing.append("forward_profitability_guidance")
    if not records:
        data_quality = "low"
    elif revenue_mid is not None and confidence >= 0.75:
        data_quality = "high"
    else:
        data_quality = "medium"
    if not records:
        guidance_score = 50.0
    else:
        guidance_score = clamp(0.35 * growth_score + 0.25 * profitability_score + 0.20 * valuation_score + 0.20 * confidence_score)
    payload = {
        "guidance_records": [
            {
                "metric": record.metric,
                "guidance_year": record.guidance_year,
                "midpoint_value": record.midpoint_value,
                "filing_date": record.filing_date,
                "form": record.form,
                "confidence": record.confidence,
                "source_type": safe_json_loads(record.source_payload).get("detector", ""),
                "source_name": safe_json_loads(record.source_payload).get("source_name", ""),
                "source_url": safe_json_loads(record.source_payload).get("source_url", ""),
                "override_reason": safe_json_loads(record.source_payload).get("override_reason", ""),
                "excerpt": record.source_excerpt[:420],
            }
            for record in by_metric.values()
        ],
        "ttm_revenue": ttm_revenue,
        "market_cap": market_cap,
    }
    return {
        "asof_date": asof_date.isoformat(),
        "company_id": int(company["company_id"]),
        "ticker": str(company["ticker"] or "").upper(),
        "company_name": str(company["company_name"] or ""),
        "latest_guidance_filing_date": latest_filing,
        "forward_revenue_midpoint": revenue_mid,
        "forward_revenue_low": revenue.low_value if revenue else None,
        "forward_revenue_high": revenue.high_value if revenue else None,
        "forward_revenue_year": revenue.guidance_year if revenue else None,
        "forward_revenue_growth_pct": revenue_growth,
        "forward_ebitda_midpoint": ebitda_mid,
        "forward_ebitda_margin_pct": ebitda_margin,
        "forward_eps_midpoint": eps_mid,
        "guidance_confidence": confidence,
        "guidance_recency_days": recency_days,
        "forward_profitability_flag": forward_profitability_flag,
        "guidance_score": round(guidance_score, 4),
        "forward_growth_score": round(growth_score, 4),
        "forward_profitability_score": round(profitability_score, 4),
        "forward_valuation_score": round(valuation_score, 4),
        "data_quality": data_quality,
        "missing_fields": ";".join(missing),
        "payload_json": json.dumps(payload, ensure_ascii=True, sort_keys=True),
    }


def normalize_guidance_number(raw: object, *, null_token: str = "<NULL>") -> str:
    if raw is None:
        return null_token
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return str(raw)
    if not math.isfinite(value):
        return null_token
    return f"{value:.12g}"


def guidance_unique_key_from_values(
    asof_date: object,
    company_id: object,
    accession_nodash: object,
    metric: object,
    guidance_year: object,
    low_value: object,
    high_value: object,
) -> str:
    parsed_company_id = to_int(company_id)
    if parsed_company_id is None:
        raise ValueError(f"Invalid company_id for guidance_unique_key: {company_id!r}")
    parts = [
        str(asof_date or ""),
        parsed_company_id,
        str(accession_nodash or ""),
        str(metric or ""),
        "<NULL>" if guidance_year is None else str(guidance_year),
        normalize_guidance_number(low_value, null_token="<NULL>"),
        normalize_guidance_number(high_value, null_token="<NULL>"),
    ]
    return json.dumps(parts, ensure_ascii=True, separators=(",", ":"))


def guidance_unique_key(record: GuidanceRecord) -> str:
    return guidance_unique_key_from_values(
        record.asof_date,
        record.company_id,
        record.accession_nodash,
        record.metric,
        record.guidance_year,
        record.low_value,
        record.high_value,
    )


def load_existing_guidance_created_at(
    conn: sqlite3.Connection,
    asof_date: str,
    target_company_ids: set[int] | None,
) -> dict[str, str]:
    params: list[object] = [asof_date]
    company_clause = ""
    if target_company_ids is not None:
        if not target_company_ids:
            return {}
        if len(target_company_ids) > SQLITE_PARAM_CHUNK_SIZE:
            out: dict[str, str] = {}
            for company_chunk in chunked(sorted(target_company_ids)):
                out.update(load_existing_guidance_created_at(conn, asof_date, {int(value) for value in company_chunk}))
            return out
        company_placeholders = ",".join("?" for _ in target_company_ids)
        company_clause = f" AND company_id IN ({company_placeholders})"
        params.extend(sorted(target_company_ids))
    rows = conn.execute(
        f"""
        SELECT asof_date, company_id, accession_nodash, metric, guidance_year,
               COALESCE(guidance_unique_key, '') AS guidance_unique_key,
               low_value, high_value, created_at
        FROM company_forward_guidance
        WHERE asof_date = ?{company_clause}
        """,
        params,
    ).fetchall()
    return {
        (
            str(row["guidance_unique_key"] or "")
            or guidance_unique_key_from_values(
                row["asof_date"],
                row["company_id"],
                row["accession_nodash"],
                row["metric"],
                row["guidance_year"],
                row["low_value"],
                row["high_value"],
            )
        ): str(row["created_at"] or "")
        for row in rows
    }


def replace_guidance(
    conn: sqlite3.Connection,
    records: list[GuidanceRecord],
    override_records: list[GuidanceRecord],
    features: list[dict[str, Any]],
    asof_date: str,
    *,
    target_company_ids: set[int] | None = None,
) -> None:
    now = utc_now()
    with conn:
        existing_guidance_created_at = load_existing_guidance_created_at(conn, asof_date, target_company_ids)
        if target_company_ids is None:
            conn.execute("DELETE FROM company_forward_guidance WHERE asof_date = ?", (asof_date,))
            conn.execute("DELETE FROM company_forward_guidance_overrides WHERE asof_date = ?", (asof_date,))
            conn.execute("DELETE FROM forward_guidance_features_daily WHERE asof_date = ?", (asof_date,))
        elif target_company_ids:
            for company_chunk in chunked(sorted(target_company_ids)):
                company_placeholders = ",".join("?" for _ in company_chunk)
                params = (asof_date, *company_chunk)
                conn.execute(
                    f"DELETE FROM company_forward_guidance WHERE asof_date = ? AND company_id IN ({company_placeholders})",
                    params,
                )
                conn.execute(
                    f"DELETE FROM company_forward_guidance_overrides WHERE asof_date = ? AND company_id IN ({company_placeholders})",
                    params,
                )
                conn.execute(
                    f"DELETE FROM forward_guidance_features_daily WHERE asof_date = ? AND company_id IN ({company_placeholders})",
                    params,
                )
        else:
            return
        guidance_params: list[tuple[Any, ...]] = []
        for record in records:
            unique_key = guidance_unique_key(record)
            if not unique_key:
                raise ValueError(
                    f"Could not compute guidance_unique_key for company_id={record.company_id} "
                    f"accession={record.accession_nodash} metric={record.metric}"
                )
            guidance_params.append(
                (
                    record.asof_date,
                    record.company_id,
                    record.ticker,
                    record.company_name,
                    record.accession_nodash,
                    record.filing_date,
                    record.form,
                    record.metric,
                    record.guidance_year,
                    record.period_label,
                    record.low_value,
                    record.high_value,
                    unique_key,
                    record.midpoint_value,
                    record.unit,
                    record.currency,
                    record.confidence,
                    record.source_excerpt,
                    record.source_payload,
                    existing_guidance_created_at.get(unique_key, now),
                    now,
                ),
            )
        if guidance_params:
            conn.executemany(
                """
                INSERT INTO company_forward_guidance(
                    asof_date, company_id, ticker, company_name, accession_nodash, filing_date, form,
                    metric, guidance_year, period_label, low_value, high_value, guidance_unique_key, midpoint_value,
                    unit, currency, confidence, source_excerpt, source_payload, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guidance_unique_key)
                DO UPDATE SET
                    ticker = excluded.ticker,
                    company_name = excluded.company_name,
                    filing_date = excluded.filing_date,
                    form = excluded.form,
                    period_label = excluded.period_label,
                    midpoint_value = excluded.midpoint_value,
                    unit = excluded.unit,
                    currency = excluded.currency,
                    confidence = excluded.confidence,
                    source_excerpt = excluded.source_excerpt,
                    source_payload = excluded.source_payload,
                    updated_at = excluded.updated_at
                """,
                guidance_params,
            )
        override_params: list[tuple[Any, ...]] = []
        for record in override_records:
            payload = safe_json_loads(record.source_payload)
            override_params.append(
                (
                    record.asof_date,
                    record.company_id,
                    record.ticker,
                    record.company_name,
                    record.accession_nodash,
                    record.metric,
                    record.guidance_year,
                    record.period_label,
                    record.low_value,
                    record.high_value,
                    record.midpoint_value,
                    record.unit,
                    record.currency,
                    record.filing_date,
                    record.form,
                    record.confidence,
                    payload.get("source_name", ""),
                    payload.get("source_url", ""),
                    record.source_excerpt,
                    payload.get("override_reason", ""),
                    1,
                    now,
                    now,
                )
            )
        if override_params:
            conn.executemany(
                """
                INSERT INTO company_forward_guidance_overrides(
                    asof_date, company_id, ticker, company_name, unique_key, metric, guidance_year, period_label,
                    low_value, high_value, midpoint_value, unit, currency, filing_date, form,
                    confidence, source_name, source_url, source_excerpt, override_reason, enabled,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(unique_key) DO UPDATE SET
                    company_name = excluded.company_name,
                    period_label = excluded.period_label,
                    midpoint_value = excluded.midpoint_value,
                    unit = excluded.unit,
                    currency = excluded.currency,
                    filing_date = excluded.filing_date,
                    form = excluded.form,
                    confidence = excluded.confidence,
                    source_url = excluded.source_url,
                    source_excerpt = excluded.source_excerpt,
                    override_reason = excluded.override_reason,
                    enabled = excluded.enabled,
                    updated_at = excluded.updated_at
                """,
                override_params,
            )
        feature_params = [
            (
                row["asof_date"],
                row["company_id"],
                row["ticker"],
                row["company_name"],
                row["latest_guidance_filing_date"],
                row["forward_revenue_midpoint"],
                row["forward_revenue_low"],
                row["forward_revenue_high"],
                row["forward_revenue_year"],
                row["forward_revenue_growth_pct"],
                row["forward_ebitda_midpoint"],
                row["forward_ebitda_margin_pct"],
                row["forward_eps_midpoint"],
                row["guidance_confidence"],
                row["guidance_recency_days"],
                row["forward_profitability_flag"],
                row["guidance_score"],
                row["forward_growth_score"],
                row["forward_profitability_score"],
                row["forward_valuation_score"],
                row["data_quality"],
                row["missing_fields"],
                row["payload_json"],
                now,
                now,
            )
            for row in features
        ]
        if feature_params:
            conn.executemany(
                """
                INSERT INTO forward_guidance_features_daily(
                    asof_date, company_id, ticker, company_name, latest_guidance_filing_date,
                    forward_revenue_midpoint, forward_revenue_low, forward_revenue_high,
                    forward_revenue_year, forward_revenue_growth_pct, forward_ebitda_midpoint,
                    forward_ebitda_margin_pct, forward_eps_midpoint, guidance_confidence,
                    guidance_recency_days, forward_profitability_flag, guidance_score,
                    forward_growth_score, forward_profitability_score, forward_valuation_score,
                    data_quality, missing_fields, payload_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(asof_date, company_id) DO UPDATE SET
                    latest_guidance_filing_date = excluded.latest_guidance_filing_date,
                    forward_revenue_midpoint = excluded.forward_revenue_midpoint,
                    forward_revenue_low = excluded.forward_revenue_low,
                    forward_revenue_high = excluded.forward_revenue_high,
                    forward_revenue_year = excluded.forward_revenue_year,
                    forward_revenue_growth_pct = excluded.forward_revenue_growth_pct,
                    forward_ebitda_midpoint = excluded.forward_ebitda_midpoint,
                    forward_ebitda_margin_pct = excluded.forward_ebitda_margin_pct,
                    forward_eps_midpoint = excluded.forward_eps_midpoint,
                    guidance_confidence = excluded.guidance_confidence,
                    guidance_recency_days = excluded.guidance_recency_days,
                    forward_profitability_flag = excluded.forward_profitability_flag,
                    guidance_score = excluded.guidance_score,
                    forward_growth_score = excluded.forward_growth_score,
                    forward_profitability_score = excluded.forward_profitability_score,
                    forward_valuation_score = excluded.forward_valuation_score,
                    data_quality = excluded.data_quality,
                    missing_fields = excluded.missing_fields,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                feature_params,
            )


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    configure_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    universe_csv = resolve_path(cfg_get(config, "forward_guidance.final_scoring_universe_csv"), base_dir=base_dir)
    guidance_csv = resolve_path(cfg_get(config, "forward_guidance.guidance_output_csv"), base_dir=base_dir)
    features_csv = resolve_path(cfg_get(config, "forward_guidance.features_output_csv"), base_dir=base_dir)
    overrides_csv = resolve_optional_path(cfg_get(config, "forward_guidance.overrides_csv"), base_dir=base_dir)
    asof_date = parse_date(args.asof) if args.asof else datetime.now(timezone.utc).date()
    if asof_date is None:
        raise ValueError(f"Invalid --asof date: {args.asof}")
    ticker_filter = {normalize_ticker(part) for part in args.tickers.split(",") if normalize_ticker(part)}
    forms = {str(item).upper() for item in (cfg_get(config, "forward_guidance.forms", []) or [])}
    lookback_days = int(cfg_get(config, "forward_guidance.lookback_days", 540))
    max_filings_per_company = int(cfg_get(config, "forward_guidance.max_filings_per_company", 14))
    max_windows_per_filing = int(cfg_get(config, "forward_guidance.max_windows_per_filing", 40))
    min_confidence = float(cfg_get(config, "forward_guidance.min_confidence", 0.68))
    fetch_complete = as_bool(cfg_get(config, "forward_guidance.fetch_complete_submission_text", True), True)
    fetch_forms = {str(item).upper() for item in (cfg_get(config, "forward_guidance.fetch_complete_submission_forms", ["8-K", "8-K/A", "6-K", "6-K/A"]) or [])}
    cache_dir = resolve_path(cfg_get(config, "forward_guidance.cache_dir", "../output/biotech_index_cache"), base_dir=base_dir)
    user_agent = str(cfg_get(config, "forward_guidance.user_agent", cfg_get(config, "sec_filings.user_agent", "")) or "").strip()
    ttl_hours = float(cfg_get(config, "forward_guidance.text_ttl_hours", 168.0))
    sleep_sec = float(cfg_get(config, "forward_guidance.sleep_sec", 0.15))
    timeout_sec = float(cfg_get(config, "forward_guidance.timeout_sec", 45.0))
    max_retries = int(cfg_get(config, "forward_guidance.max_retries", 3))
    max_workers = max(1, int(cfg_get(config, "forward_guidance.max_workers", 4)))
    run_mode = str(args.run_mode or ("full_backfill" if args.full_rescan else cfg_get(config, "forward_guidance.run_mode", "daily_delta"))).strip().lower()
    if run_mode not in {"daily_delta", "weekly_reconcile", "full_backfill"}:
        raise ValueError(f"Invalid forward guidance run mode: {run_mode}")
    parser_signature = build_parser_signature(
        forms=forms,
        lookback_days=lookback_days,
        max_filings_per_company=max_filings_per_company,
        max_windows_per_filing=max_windows_per_filing,
        min_confidence=min_confidence,
        fetch_complete=fetch_complete,
        fetch_forms=fetch_forms,
    )
    headers = {"User-Agent": user_agent} if user_agent else {}
    sqlite_timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))

    with connect(db_path, timeout_sec=sqlite_timeout_sec) as conn:
        init_db(conn)
        scoring_tickers = read_scoring_tickers(universe_csv)
        companies = load_companies(conn, scoring_tickers=scoring_tickers, ticker_filter=ticker_filter, max_companies=int(args.max_companies))
        subset_mode = subset_mode_enabled(ticker_filter=ticker_filter, max_count=int(args.max_companies))
        guidance_csv = subset_output_path(guidance_csv, subset_mode=subset_mode)
        features_csv = subset_output_path(features_csv, subset_mode=subset_mode)
        validate_nonempty_selection(count=len(companies), context="forward guidance parse", subset_mode=subset_mode)
        loaded_tickers = [str(company["ticker"]) for company in companies]
        validate_requested_tickers(requested_tickers=ticker_filter, loaded_tickers=loaded_tickers, context="forward guidance parse")
        validate_full_universe_coverage(
            expected_tickers=scoring_tickers,
            observed_tickers=loaded_tickers,
            context="forward guidance parse",
            subset_mode=subset_mode,
        )
        companies_by_ticker = {normalize_ticker(company["ticker"]): company for company in companies}
        override_records = load_guidance_overrides(overrides_csv, companies_by_ticker, asof_date=asof_date)
        commercial_by_company = latest_commercial_rows(conn, asof_date)
        validate_layer_freshness(
            base_rows=companies,
            layer_rows_by_company=commercial_by_company,
            asof_date=asof_date,
            context="forward guidance parse commercial_value_features_daily",
            max_staleness_days=int(cfg_get(config, "biotech_refresh.max_upstream_staleness_days", 2)),
        )
        run_id: int | None = start_run(conn, run_type="parse_forward_guidance", input_path=universe_csv)
        try:
            overall_start = time.perf_counter()
            all_records: list[GuidanceRecord] = []
            records_by_company: dict[int, list[GuidanceRecord]] = {}
            company_ids = [int(company["company_id"]) for company in companies]
            phase_start = time.perf_counter()
            selected_filings = load_filing_metadata_bulk(
                conn,
                company_ids=company_ids,
                asof_date=asof_date,
                lookback_days=lookback_days,
                forms=forms,
                max_filings_per_company=max_filings_per_company,
            )
            LOGGER.info("Forward guidance metadata selected: companies=%d filings=%d elapsed=%.3fs", len(companies), len(selected_filings), time.perf_counter() - phase_start)
            phase_start = time.perf_counter()
            parse_state = load_forward_guidance_parse_state(conn, [filing.accession_nodash for filing in selected_filings])
            reuse_parse_state = run_mode in {"daily_delta", "weekly_reconcile"} and config_bool(
                cfg_get(config, "forward_guidance.reuse_unchanged_parse_state", True),
                True,
            )
            reuse_miss_reasons: dict[str, int] = {}
            if reuse_parse_state:
                unchanged_filings: list[FilingText] = []
                filings_to_parse: list[FilingText] = []
                for filing in selected_filings:
                    reason = guidance_parse_reuse_miss_reason(
                        filing,
                        parse_state.get(filing.accession_nodash),
                        asof_date=asof_date,
                        parser_signature=parser_signature,
                    )
                    if reason:
                        reuse_miss_reasons[reason] = reuse_miss_reasons.get(reason, 0) + 1
                        filings_to_parse.append(filing)
                    else:
                        unchanged_filings.append(filing)
            else:
                unchanged_filings = []
                filings_to_parse = selected_filings
                if selected_filings:
                    reuse_miss_reasons["reuse_disabled"] = len(selected_filings)
            reuse_hit_rate = (100.0 * len(unchanged_filings) / float(len(selected_filings))) if selected_filings else 0.0
            LOGGER.info(
                "Forward guidance parse-state split: enabled=%s hits=%d misses=%d hit_rate=%.1f%% miss_reasons=%s elapsed=%.3fs",
                reuse_parse_state,
                len(unchanged_filings),
                len(filings_to_parse),
                reuse_hit_rate,
                ",".join(f"{key}:{value}" for key, value in sorted(reuse_miss_reasons.items())) or "none",
                time.perf_counter() - phase_start,
            )

            phase_start = time.perf_counter()
            reused_records = load_previous_guidance_records(conn, filings=unchanged_filings, asof_date=asof_date)
            reused_accessions = {record.accession_nodash for record in reused_records}
            missing_reuse = [
                filing
                for filing in unchanged_filings
                if int((parse_state.get(filing.accession_nodash) or {}).get("guidance_count") or 0) > 0
                and filing.accession_nodash not in reused_accessions
            ]
            if missing_reuse:
                missing_reuse_accessions = {filing.accession_nodash for filing in missing_reuse}
                filings_to_parse.extend(missing_reuse)
                unchanged_filings = [filing for filing in unchanged_filings if filing.accession_nodash not in missing_reuse_accessions]
                LOGGER.info("Forward guidance reparsing %d unchanged filings with missing prior parsed rows", len(missing_reuse))
            LOGGER.info("Forward guidance prior-record reuse loaded: records=%d elapsed=%.3fs", len(reused_records), time.perf_counter() - phase_start)
            phase_start = time.perf_counter()
            filings_to_parse = load_filing_text_content_bulk(conn, filings_to_parse)
            prepared_filings = prepare_filing_texts(
                conn,
                filings_to_parse,
                headers=headers,
                ttl_hours=ttl_hours,
                fetch_enabled=fetch_complete,
                fetch_forms=fetch_forms,
                cache_dir=cache_dir,
                sleep_sec=sleep_sec,
                timeout_sec=timeout_sec,
                max_retries=max_retries,
                max_workers=max_workers,
            )
            LOGGER.info("Forward guidance text preparation complete: filings=%d elapsed=%.3fs", len(prepared_filings), time.perf_counter() - phase_start)
            phase_start = time.perf_counter()
            parsed_records, guidance_counts = parse_guidance_records(
                prepared_filings,
                asof_date=asof_date,
                min_confidence=min_confidence,
                max_windows_per_filing=max_windows_per_filing,
                max_workers=max_workers,
            )
            LOGGER.info("Forward guidance parsing complete: filings=%d records=%d elapsed=%.3fs", len(prepared_filings), len(parsed_records), time.perf_counter() - phase_start)
            upsert_forward_guidance_parse_state(
                conn,
                filings=prepared_filings,
                guidance_counts=guidance_counts,
                asof_date=asof_date,
                parser_signature=parser_signature,
            )
            all_records = [*reused_records, *parsed_records]
            for record in all_records:
                records_by_company.setdefault(record.company_id, []).append(record)
            LOGGER.info(
                "Forward guidance mode=%s companies=%d filings=%d parsed=%d reused=%d records=%d max_workers=%d",
                run_mode,
                len(companies),
                len(selected_filings),
                len(prepared_filings),
                len(unchanged_filings),
                len(all_records),
                max_workers,
            )
            for record in override_records:
                records_by_company.setdefault(record.company_id, []).append(record)
            combined_records = [*all_records, *override_records]
            phase_start = time.perf_counter()
            feature_rows = [
                build_feature_row(
                    company=company,
                    asof_date=asof_date,
                    records=records_by_company.get(int(company["company_id"]), []),
                    commercial=commercial_by_company.get(int(company["company_id"])),
                )
                for company in companies
            ]
            LOGGER.info("Forward guidance feature rows built: rows=%d elapsed=%.3fs", len(feature_rows), time.perf_counter() - phase_start)
            partial_run = bool(ticker_filter) or int(args.max_companies) > 0
            validate_output_coverage(
                expected_tickers=scoring_tickers,
                output_tickers=[row["ticker"] for row in feature_rows],
                context="forward guidance parse",
                subset_mode=subset_mode,
            )
            replace_guidance(
                conn,
                all_records,
                override_records,
                feature_rows,
                asof_date.isoformat(),
                target_company_ids=set(company_ids) if partial_run else None,
            )
            write_csv(guidance_csv, [record.__dict__ for record in combined_records], ["company_id", *GUIDANCE_FIELDS, "source_payload"])
            write_csv(features_csv, feature_rows, ["company_id", *FEATURE_FIELDS])
            LOGGER.info("Built forward guidance features: companies=%d records=%d overrides=%d elapsed=%.3fs output=%s", len(companies), len(combined_records), len(override_records), time.perf_counter() - overall_start, features_csv)
            finish_run(
                conn,
                run_id=run_id,
                status="success",
                row_count=len(feature_rows),
                message=(
                    f"asof={asof_date.isoformat()} mode={run_mode} filings={len(selected_filings)} "
                    f"parsed={len(prepared_filings)} reused={len(unchanged_filings)} "
                    f"guidance_records={len(combined_records)} overrides={len(override_records)} output={features_csv}"
                ),
            )
        except BaseException as exc:
            if run_id is not None and not (isinstance(exc, SystemExit) and exc.code in (0, None)):
                finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()
