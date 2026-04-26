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


LOGGER = logging.getLogger("parse_forward_guidance")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"

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
    if text in {"", "1", "true", "yes", "y", "enabled", "include"}:
        return True
    if text in {"0", "false", "no", "n", "disabled", "exclude"}:
        return False
    raise ValueError(f"Invalid enabled value: {raw}")


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
    future_years = [year for year in years if year >= asof_date.year]
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
    seen: set[tuple[str, int | None, float | None, float | None]] = set()
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
            key = (metric, guidance_year, round(low_value or 0.0, 4), round(high_value or 0.0, 4))
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
    if not path.exists():
        return set()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        out: set[str] = set()
        for row in reader:
            ticker = str(row.get("ticker") or "").strip().upper()
            if ticker and str(row.get("final_status") or "").strip().lower() == "keep" and str(row.get("scoring_include") or "").lower() == "true":
                out.add(ticker)
        return out


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
        ticker = str(row["ticker"] or "").upper()
        if scoring_tickers and ticker not in scoring_tickers:
            continue
        if ticker_filter and ticker not in ticker_filter:
            continue
        out.append(dict(row))
        if max_companies > 0 and len(out) >= max_companies:
            break
    return out


def guidance_record_id(*, ticker: str, metric: str, guidance_year: int | None, midpoint_value: float | None, source_name: str) -> str:
    raw = "|".join(
        [
            ticker.upper(),
            metric,
            str(guidance_year or ""),
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
            COALESCE(NULLIF(f.text_hash, ''), d.text_hash, '') AS source_text_hash
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
                   COALESCE(NULLIF(f.filing_text_hash, ''), d.text_hash, '') AS source_text_hash,
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


def dashed_accession(accession_nodash: str) -> str:
    text = str(accession_nodash or "")
    if len(text) < 18:
        return text
    return f"{text[:10]}-{text[10:12]}-{text[12:]}"


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


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
    url = f"{filing.archive_url}/{dashed_accession(filing.accession_nodash)}.txt"
    try:
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
    url = f"{filing.archive_url}/{dashed_accession(filing.accession_nodash)}.txt"
    try:
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
    chunk_size = 900
    for start in range(0, len(accessions), chunk_size):
        chunk = accessions[start : start + chunk_size]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            SELECT accession_nodash, text_hash, asof_year, parsed_at, guidance_count
            FROM forward_guidance_parse_state
            WHERE accession_nodash IN ({placeholders})
            """,
            tuple(chunk),
        ).fetchall()
        out.update({str(row["accession_nodash"]): dict(row) for row in rows})
    return out


def is_unchanged_guidance_parse(filing: FilingText, state: dict[str, Any] | None, *, asof_date: date) -> bool:
    if not state:
        return False
    if int(state.get("asof_year") or 0) != asof_date.year:
        return False
    return str(state.get("text_hash") or "") == str(filing.text_hash or "")


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
    chunk_size = 600
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
) -> None:
    if not filings:
        return
    now = utc_now()
    with conn:
        conn.executemany(
            """
            INSERT INTO forward_guidance_parse_state(
                accession_nodash, text_hash, asof_year, parsed_at, guidance_count, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(accession_nodash) DO UPDATE SET
                text_hash = excluded.text_hash,
                asof_year = excluded.asof_year,
                parsed_at = excluded.parsed_at,
                guidance_count = excluded.guidance_count,
                updated_at = excluded.updated_at
            """,
            [
                (
                    filing.accession_nodash,
                    filing.text_hash,
                    asof_date.year,
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
        for future in as_completed(futures):
            filing = futures[future]
            filing_records = future.result()
            records.extend(filing_records)
            counts[filing.accession_nodash] = len(filing_records)
    records.sort(key=lambda record: (record.ticker, record.filing_date, record.accession_nodash, record.metric))
    return records, counts


def latest_guidance_by_metric(records: list[GuidanceRecord], asof_date: date) -> dict[str, GuidanceRecord]:
    out: dict[str, GuidanceRecord] = {}
    def sort_key(record: GuidanceRecord) -> tuple[int, str, float, float]:
        year = record.guidance_year or 0
        if year == asof_date.year:
            year_priority = 3
        elif year == asof_date.year + 1:
            year_priority = 2
        elif year > asof_date.year + 1:
            year_priority = 1
        else:
            year_priority = 0
        return year_priority, record.filing_date, record.confidence, record.midpoint_value or 0.0

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
    ebitda_margin = ebitda_mid / revenue_mid if ebitda_mid is not None and revenue_mid not in {None, 0} else None
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


def replace_guidance(
    conn: sqlite3.Connection,
    records: list[GuidanceRecord],
    override_records: list[GuidanceRecord],
    features: list[dict[str, Any]],
    asof_date: str,
) -> None:
    now = utc_now()
    with conn:
        conn.execute("DELETE FROM company_forward_guidance WHERE asof_date = ?", (asof_date,))
        conn.execute("DELETE FROM company_forward_guidance_overrides WHERE asof_date = ?", (asof_date,))
        conn.execute("DELETE FROM forward_guidance_features_daily WHERE asof_date = ?", (asof_date,))
        for record in records:
            conn.execute(
                """
                INSERT OR REPLACE INTO company_forward_guidance(
                    asof_date, company_id, ticker, company_name, accession_nodash, filing_date, form,
                    metric, guidance_year, period_label, low_value, high_value, midpoint_value,
                    unit, currency, confidence, source_excerpt, source_payload, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
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
                    record.midpoint_value,
                    record.unit,
                    record.currency,
                    record.confidence,
                    record.source_excerpt,
                    record.source_payload,
                    now,
                    now,
                ),
            )
        for record in override_records:
            payload = safe_json_loads(record.source_payload)
            conn.execute(
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
                ),
            )
        for row in features:
            conn.execute(
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
                ),
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
    ticker_filter = {part.strip().upper() for part in args.tickers.split(",") if part.strip()}
    forms = {str(item).upper() for item in (cfg_get(config, "forward_guidance.forms", []) or [])}
    lookback_days = int(cfg_get(config, "forward_guidance.lookback_days", 540))
    max_filings_per_company = int(cfg_get(config, "forward_guidance.max_filings_per_company", 14))
    max_windows_per_filing = int(cfg_get(config, "forward_guidance.max_windows_per_filing", 40))
    min_confidence = float(cfg_get(config, "forward_guidance.min_confidence", 0.68))
    fetch_complete = bool(cfg_get(config, "forward_guidance.fetch_complete_submission_text", True))
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
    headers = {"User-Agent": user_agent} if user_agent else {}
    sqlite_timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))

    with connect(db_path, timeout_sec=sqlite_timeout_sec) as conn:
        init_db(conn)
        scoring_tickers = read_scoring_tickers(universe_csv)
        companies = load_companies(conn, scoring_tickers=scoring_tickers, ticker_filter=ticker_filter, max_companies=int(args.max_companies))
        companies_by_ticker = {str(company["ticker"] or "").upper(): company for company in companies}
        override_records = load_guidance_overrides(overrides_csv, companies_by_ticker, asof_date=asof_date)
        commercial_by_company = latest_commercial_rows(conn, asof_date)
        run_id = start_run(conn, run_type="parse_forward_guidance", input_path=universe_csv)
        try:
            all_records: list[GuidanceRecord] = []
            records_by_company: dict[int, list[GuidanceRecord]] = {}
            company_ids = [int(company["company_id"]) for company in companies]
            selected_filings = load_filing_texts_bulk(
                conn,
                company_ids=company_ids,
                asof_date=asof_date,
                lookback_days=lookback_days,
                forms=forms,
                max_filings_per_company=max_filings_per_company,
            )
            parse_state = load_forward_guidance_parse_state(conn, [filing.accession_nodash for filing in selected_filings])
            if run_mode == "daily_delta":
                unchanged_filings = [
                    filing
                    for filing in selected_filings
                    if is_unchanged_guidance_parse(filing, parse_state.get(filing.accession_nodash), asof_date=asof_date)
                ]
                filings_to_parse = [filing for filing in selected_filings if filing.accession_nodash not in {item.accession_nodash for item in unchanged_filings}]
            else:
                unchanged_filings = []
                filings_to_parse = selected_filings

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
            parsed_records, guidance_counts = parse_guidance_records(
                prepared_filings,
                asof_date=asof_date,
                min_confidence=min_confidence,
                max_windows_per_filing=max_windows_per_filing,
                max_workers=max_workers,
            )
            upsert_forward_guidance_parse_state(
                conn,
                filings=prepared_filings,
                guidance_counts=guidance_counts,
                asof_date=asof_date,
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
            feature_rows = [
                build_feature_row(
                    company=company,
                    asof_date=asof_date,
                    records=records_by_company.get(int(company["company_id"]), []),
                    commercial=commercial_by_company.get(int(company["company_id"])),
                )
                for company in companies
            ]
            replace_guidance(conn, all_records, override_records, feature_rows, asof_date.isoformat())
            write_csv(guidance_csv, [record.__dict__ for record in combined_records], ["company_id", *GUIDANCE_FIELDS, "source_payload"])
            write_csv(features_csv, feature_rows, ["company_id", *FEATURE_FIELDS])
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
        except Exception as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise
    LOGGER.info("Built forward guidance features: companies=%d records=%d overrides=%d output=%s", len(companies), len(combined_records), len(override_records), features_csv)


if __name__ == "__main__":
    main()
