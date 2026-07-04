#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import html as html_lib
import json
import logging
import re
import sys
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.db import connect, finish_run, init_db, start_run, utc_now  # noqa: E402
from industrials.core.logging_utils import configure_utc_logging  # noqa: E402
from industrials.core.source_registry import load_source_registry, upsert_source_registry  # noqa: E402
from industrials.core.text_norm import normalize_cik, normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("sync_industrials_sec_fundamentals")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
RUN_TYPE = "sync_industrials_sec_fundamentals"
RECENT_STUB_PROFILES = {"RECENT_IPO_DEVELOPMENT_STAGE", "RECENT_PUBLIC_STUB"}
FPI_HYBRID_PROFILES = {"FPI_HYBRID_STUB_LOADED", "FPI_HYBRID_LOADED"}
REPORT_FIELDS = [
    "ticker",
    "cik",
    "company_name",
    "country",
    "status",
    "reporting_profile",
    "reporting_standard",
    "latest_filing_date",
    "latest_form_type",
    "filing_count",
    "raw_fact_count",
    "mapped_fact_count",
    "review_reason",
]

XBRL_INSTANCE_NAMESPACE = "http://www.xbrl.org/2003/instance"
INLINE_XBRL_LOCAL_NAMES = {"nonfraction", "nonnumeric"}
ARCHIVE_EXCLUDED_SUFFIXES = (
    "_cal.xml",
    "_def.xml",
    "_lab.xml",
    "_pre.xml",
    ".xsd",
    "filingsummary.xml",
    "metalinks.json",
    "-index-headers.html",
    "-index.html",
)
ARCHIVE_ALLOWED_DOCUMENT_SUFFIXES = (".xml", ".xhtml", ".htm", ".html", ".txt")
TEXT_TABLE_SOURCE_DETAIL = "sec_archive_text_table"
XBRL_ARCHIVE_SOURCE_DETAIL = "sec_archive_xbrl"


class SecRequestError(RuntimeError):
    def __init__(self, *, status_code: int, url: str, body: str) -> None:
        super().__init__(f"SEC request failed status={status_code} url={url} body={body[:200]}")
        self.status_code = status_code
        self.url = url
        self.body = body


@dataclass(frozen=True)
class ReportingOverride:
    ticker: str
    handling_type: str
    parent_ticker: str
    skip_sec_network: bool
    reporting_profile: str
    reporting_standard: str
    fallback_status: str
    financial_confidence: float
    usable_xbrl_flag: int
    review_reason: str
    notes: str


@dataclass(frozen=True)
class ArchiveFact:
    taxonomy: str
    concept_name: str
    unit: str
    value: float
    period_start: str
    period_end: str
    frame: str
    decimals: str
    payload_json: str
    source_detail: str = XBRL_ARCHIVE_SOURCE_DETAIL


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync SEC submissions and companyfacts for an industrials model family.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--model-family", default="", help="Industrials model family to sync, e.g. defense.")
    parser.add_argument("--tickers", default="", help="Optional comma-separated ticker filter.")
    parser.add_argument("--max-tickers", type=int, default=0, help="Optional cap for smoke tests.")
    parser.add_argument("--include-historical", action="store_true", help="Also sync non-current historical/delisted members.")
    parser.add_argument("--force", action="store_true", help="Ignore cached JSON and refetch.")
    parser.add_argument(
        "--incremental",
        action="store_true",
        help=(
            "Daily refresh mode: refetch submissions metadata, then process "
            "companyfacts/archive only for tickers with new filings or no existing SEC financial state."
        ),
    )
    parser.add_argument("--force-submissions", action="store_true", help="Refetch submissions metadata without forcing companyfacts/archive caches.")
    parser.add_argument("--force-companyfacts", action="store_true", help="Refetch companyfacts JSON without forcing archive document caches.")
    parser.add_argument("--force-archive", action="store_true", help="Refetch SEC archive index/document caches.")
    parser.add_argument("--allow-partial", action="store_true", help="Finish with success when individual tickers fail.")
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--skip-source-registry", action="store_true")
    return parser.parse_args()


def parse_ticker_list(raw: object) -> list[str]:
    values = raw if isinstance(raw, list) else str(raw or "").split(",")
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        ticker = normalize_ticker(value)
        if ticker and ticker not in seen:
            out.append(ticker)
            seen.add(ticker)
    return out


def as_bool(raw: object) -> bool:
    text = str(raw or "").strip().lower()
    return text in {"1", "true", "t", "yes", "y"}


def sec_cik(raw: object) -> str:
    cik = normalize_cik(raw)
    if not cik or len(cik) != 10:
        raise ValueError(f"Invalid SEC CIK value: {raw!r}")
    return cik


def row_value(row: dict[str, str], key: str) -> str:
    return str(row.get(key) or "").strip()


def load_reporting_overrides(path: Path | None) -> dict[str, ReportingOverride]:
    if path is None or not path.exists():
        return {}
    overrides: dict[str, ReportingOverride] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for line_number, row in enumerate(reader, start=2):
            ticker = normalize_ticker(row_value(row, "ticker"))
            if not ticker:
                raise ValueError(f"{path}:{line_number} missing or invalid ticker")
            if ticker in overrides:
                raise ValueError(f"{path}:{line_number} duplicate override ticker={ticker}")
            confidence = as_float(row_value(row, "financial_confidence"))
            if confidence is None:
                raise ValueError(f"{path}:{line_number} ticker={ticker} missing financial_confidence")
            overrides[ticker] = ReportingOverride(
                ticker=ticker,
                handling_type=row_value(row, "handling_type"),
                parent_ticker=normalize_ticker(row_value(row, "parent_ticker")),
                skip_sec_network=as_bool(row_value(row, "skip_sec_network")),
                reporting_profile=row_value(row, "reporting_profile"),
                reporting_standard=row_value(row, "reporting_standard"),
                fallback_status=row_value(row, "fallback_status") or "review",
                financial_confidence=confidence,
                usable_xbrl_flag=1 if as_bool(row_value(row, "usable_xbrl_flag")) else 0,
                review_reason=row_value(row, "review_reason"),
                notes=row_value(row, "notes"),
            )
    return overrides


def parse_date(raw: object) -> str:
    text = str(raw or "").strip()[:10]
    if not text:
        return ""
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return ""


def as_float(raw: object) -> float | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value


def as_int(raw: object) -> int | None:
    text = str(raw or "").strip()
    return int(text) if text.isdigit() else None


def compact_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def normalized_name_text(raw: object) -> str:
    text = re.sub(r"[^A-Z0-9 ]+", " ", str(raw or "").upper())
    return " ".join(
        token
        for token in text.split()
        if token not in {"INC", "CORP", "CORPORATION", "CO", "COMPANY", "LTD", "LIMITED", "PLC", "SA", "THE"}
    )


def normalized_name_similarity(left: object, right: object) -> float:
    left_text = normalized_name_text(left)
    right_text = normalized_name_text(right)
    if not left_text or not right_text:
        return 0.0
    return SequenceMatcher(None, left_text, right_text).ratio()


def names_plausibly_match(left: object, right: object) -> bool:
    left_text = normalized_name_text(left)
    right_text = normalized_name_text(right)
    if not left_text or not right_text:
        return True
    if left_text in right_text or right_text in left_text:
        return True
    left_initials = "".join(token[0] for token in left_text.split() if token)
    right_initials = "".join(token[0] for token in right_text.split() if token)
    if len(left_text) <= 5 and left_text == right_initials:
        return True
    if len(right_text) <= 5 and right_text == left_initials:
        return True
    return normalized_name_similarity(left_text, right_text) >= 0.25


def payload_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def cache_path(cache_dir: Path, *, source_id: str, cik: str) -> Path:
    return cache_dir / source_id / f"CIK{cik}.json"


def named_cache_path(cache_dir: Path, *, source_id: str, name: str) -> Path:
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)
    return cache_dir / source_id / safe_name


def request_json(url: str, *, user_agent: str, timeout_sec: float, max_retries: int, sleep_sec: float) -> tuple[int, dict[str, Any], str]:
    try:
        import requests  # type: ignore
    except ImportError as exc:
        raise RuntimeError("Package 'requests' is required for SEC sync.") from exc

    headers = {
        "User-Agent": user_agent,
        "Accept-Encoding": "gzip, deflate",
    }
    last_status = 0
    last_text = ""
    for attempt in range(max(1, max_retries)):
        response = requests.get(url, headers=headers, timeout=timeout_sec)
        last_status = int(response.status_code)
        last_text = response.text
        if response.status_code == 200:
            return last_status, response.json(), last_text
        if response.status_code not in {429, 500, 502, 503, 504}:
            break
        time.sleep(sleep_sec * (attempt + 1))
    raise SecRequestError(status_code=last_status, url=url, body=last_text)


def request_text(url: str, *, user_agent: str, timeout_sec: float, max_retries: int, sleep_sec: float) -> tuple[int, str]:
    try:
        import requests  # type: ignore
    except ImportError as exc:
        raise RuntimeError("Package 'requests' is required for SEC archive sync.") from exc

    headers = {
        "User-Agent": user_agent,
        "Accept-Encoding": "gzip, deflate",
    }
    last_status = 0
    last_text = ""
    for attempt in range(max(1, max_retries)):
        response = requests.get(url, headers=headers, timeout=timeout_sec)
        last_status = int(response.status_code)
        last_text = response.text
        if response.status_code == 200:
            return last_status, last_text
        if response.status_code not in {429, 500, 502, 503, 504}:
            break
        time.sleep(sleep_sec * (attempt + 1))
    raise SecRequestError(status_code=last_status, url=url, body=last_text)


def load_or_fetch_json(
    url: str,
    *,
    cache_file: Path,
    force: bool,
    user_agent: str,
    timeout_sec: float,
    max_retries: int,
    sleep_sec: float,
) -> tuple[int, dict[str, Any], str, str]:
    if cache_file.exists() and not force:
        text = cache_file.read_text(encoding="utf-8")
        return 200, json.loads(text), text, "cache"
    status, payload, text = request_json(url, user_agent=user_agent, timeout_sec=timeout_sec, max_retries=max_retries, sleep_sec=sleep_sec)
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(text, encoding="utf-8")
    return status, payload, text, "network"


def load_or_fetch_text(
    url: str,
    *,
    cache_file: Path,
    force: bool,
    user_agent: str,
    timeout_sec: float,
    max_retries: int,
    sleep_sec: float,
) -> tuple[int, str, str]:
    if cache_file.exists() and not force:
        text = cache_file.read_text(encoding="utf-8", errors="replace")
        return 200, text, "cache"
    status, text = request_text(url, user_agent=user_agent, timeout_sec=timeout_sec, max_retries=max_retries, sleep_sec=sleep_sec)
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(text, encoding="utf-8")
    return status, text, "network"


def add_issue(
    conn: Any,
    *,
    severity: str,
    ticker: str,
    source_id: str,
    issue_type: str,
    detail: str,
) -> None:
    now = utc_now()
    row = conn.execute("SELECT company_id FROM dim_company WHERE ticker = ?", (ticker,)).fetchone()
    company_id = int(row["company_id"]) if row is not None else None
    conn.execute(
        """
        INSERT INTO data_quality_issues(
            detected_at, severity, stage, ticker, company_id, source_id, issue_type,
            issue_detail, resolution_status, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
        """,
        (now, severity, RUN_TYPE, ticker, company_id, source_id, issue_type, detail, now, now),
    )


def resolve_open_issue(
    conn: Any,
    *,
    ticker: str,
    source_id: str,
    issue_type: str,
    detail: str,
    resolution_status: str,
) -> None:
    now = utc_now()
    conn.execute(
        """
        UPDATE data_quality_issues
        SET resolution_status = ?,
            updated_at = ?
        WHERE stage = ?
          AND ticker = ?
          AND source_id = ?
          AND issue_type = ?
          AND issue_detail = ?
          AND resolution_status = 'open'
        """,
        (resolution_status, now, RUN_TYPE, ticker, source_id, issue_type, detail),
    )


def load_universe(conn: Any, *, model_family: str, ticker_filter: list[str], include_historical: bool) -> list[dict[str, Any]]:
    filter_sql = ""
    params: list[Any] = [model_family]
    if ticker_filter:
        filter_sql = f"AND c.ticker IN ({','.join('?' for _ in ticker_filter)})"
        params.extend(ticker_filter)
    if include_historical:
        rows = conn.execute(
            f"""
            SELECT DISTINCT c.company_id, c.ticker, c.cik, c.company_name, c.country, c.currency, c.is_active
            FROM dim_company c
            JOIN dim_universe_membership m
              ON m.company_id = c.company_id
             AND m.model_family = ?
            WHERE 1 = 1
              {filter_sql}
            ORDER BY c.ticker
            """,
            tuple(params),
        ).fetchall()
    else:
        rows = conn.execute(
            f"""
            SELECT DISTINCT c.company_id, c.ticker, c.cik, c.company_name, c.country, c.currency, c.is_active
            FROM dim_company c
            JOIN dim_industrials_taxonomy t
              ON t.company_id = c.company_id
             AND t.model_family = ?
            WHERE c.is_active = 1
              {filter_sql}
            ORDER BY c.ticker
            """,
            tuple(params),
        ).fetchall()
    return [dict(row) for row in rows]


def filing_keys(conn: Any, *, ticker: str, source_id: str) -> set[tuple[str, str, str]]:
    rows = conn.execute(
        """
        SELECT accession_number, filing_date, form_type
        FROM fact_sec_filing
        WHERE ticker = ?
          AND source_id = ?
        """,
        (ticker, source_id),
    ).fetchall()
    return {
        (
            str(row["accession_number"] or ""),
            str(row["filing_date"] or ""),
            str(row["form_type"] or ""),
        )
        for row in rows
    }


def has_filing_metadata(conn: Any, *, ticker: str, source_id: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM fact_sec_filing
        WHERE ticker = ?
          AND source_id = ?
        LIMIT 1
        """,
        (ticker, source_id),
    ).fetchone()
    return row is not None


def has_existing_sec_financial_state(
    conn: Any,
    *,
    ticker: str,
    source_id: str,
    override: ReportingOverride | None,
) -> bool:
    if override is not None and override.skip_sec_network:
        return True
    row = conn.execute(
        """
        SELECT 1
        FROM fact_sec_xbrl_fact
        WHERE ticker = ?
          AND source_id = ?
        LIMIT 1
        """,
        (ticker, source_id),
    ).fetchone()
    if row is not None:
        return True
    if override is None:
        return False
    # These profiles can be intentionally low/partial coverage. Daily refreshes
    # should not repeatedly grind the archive fallback unless a new filing appears
    # or the caller explicitly forces companyfacts/archive processing.
    return override.reporting_profile in {
        "SEC_RAW_ARCHIVE_REQUIRED",
        "RECENT_IPO_DEVELOPMENT_STAGE",
        "RECENT_PUBLIC_STUB",
        "SEC_20F_METADATA_ONLY",
        "FOREIGN_PRIVATE_ISSUER_ARCHIVE_REQUIRED",
        "FPI_HYBRID_STUB_LOADED",
        "FPI_HYBRID_LOADED",
    }


def sec_fact_counts(conn: Any, *, ticker: str, source_id: str) -> tuple[int, int]:
    raw_row = conn.execute(
        """
        SELECT COUNT(*)
        FROM fact_sec_xbrl_fact_raw
        WHERE ticker = ?
          AND source_id = ?
        """,
        (ticker, source_id),
    ).fetchone()
    mapped_row = conn.execute(
        """
        SELECT COUNT(*)
        FROM fact_sec_xbrl_fact
        WHERE ticker = ?
          AND source_id = ?
        """,
        (ticker, source_id),
    ).fetchone()
    return int(raw_row[0] or 0), int(mapped_row[0] or 0)


def clear_stage_issues(conn: Any, *, ticker_filter: list[str] | None = None) -> None:
    if ticker_filter:
        placeholders = ",".join("?" for _ in ticker_filter)
        conn.execute(f"DELETE FROM data_quality_issues WHERE stage = ? AND ticker IN ({placeholders})", (RUN_TYPE, *ticker_filter))
    else:
        conn.execute("DELETE FROM data_quality_issues WHERE stage = ?", (RUN_TYPE,))


def record_raw_response(
    conn: Any,
    *,
    source_id: str,
    endpoint: str,
    status: int,
    payload_text: str,
    asof_date: str,
    ingestion_run_id: int,
) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO raw_api_responses(
            source_id, endpoint, query_params_json, request_time_utc, response_status,
            response_hash, asof_date, payload_text, ingestion_run_id, created_at
        )
        VALUES (?, ?, '{}', ?, ?, ?, ?, ?, ?, ?)
        """,
        (source_id, endpoint, now, status, payload_hash(payload_text), asof_date, payload_text, ingestion_run_id, now),
    )


def upsert_filings(
    conn: Any,
    *,
    ticker: str,
    cik: str,
    source_id: str,
    payload: dict[str, Any],
    allowed_forms: set[str],
    start_date: str,
) -> int:
    recent_payload = (payload.get("filings") or {}).get("recent") if isinstance(payload.get("filings"), dict) else None
    rows_payload = recent_payload if isinstance(recent_payload, dict) else payload
    if not isinstance(rows_payload, dict):
        return 0
    forms = rows_payload.get("form") or []
    count = 0
    now = utc_now()
    keys = [
        "accessionNumber",
        "filingDate",
        "acceptanceDateTime",
        "reportDate",
        "form",
        "primaryDocument",
        "fy",
        "fp",
    ]
    for idx, form in enumerate(forms):
        form_type = str(form or "").strip().upper()
        if allowed_forms and form_type not in allowed_forms:
            continue
        values = {key: (rows_payload.get(key) or []) for key in keys}
        accession = str(values["accessionNumber"][idx] or "").strip() if idx < len(values["accessionNumber"]) else ""
        filing_date = parse_date(values["filingDate"][idx] if idx < len(values["filingDate"]) else "")
        if not accession or not filing_date or (start_date and filing_date < start_date):
            continue
        accepted_at = str(values["acceptanceDateTime"][idx] or "").strip() if idx < len(values["acceptanceDateTime"]) else ""
        report_date = parse_date(values["reportDate"][idx] if idx < len(values["reportDate"]) else "")
        primary_document = str(values["primaryDocument"][idx] or "").strip() if idx < len(values["primaryDocument"]) else ""
        fiscal_year_raw = values["fy"][idx] if idx < len(values["fy"]) else None
        fiscal_year = as_int(fiscal_year_raw)
        fiscal_period = str(values["fp"][idx] or "").strip() if idx < len(values["fp"]) else ""
        accession_nodash = accession.replace("-", "")
        filing_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession_nodash}/{primary_document}" if primary_document else ""
        conn.execute(
            """
            INSERT INTO fact_sec_filing(
                ticker, cik, source_id, accession_number, form_type, filing_date,
                accepted_at, report_date, fiscal_year, fiscal_period, primary_document,
                filing_url, source_detail, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'sec_submissions_recent', ?, ?)
            ON CONFLICT(ticker, accession_number, source_id) DO UPDATE SET
                cik = excluded.cik,
                form_type = excluded.form_type,
                filing_date = excluded.filing_date,
                accepted_at = excluded.accepted_at,
                report_date = excluded.report_date,
                fiscal_year = excluded.fiscal_year,
                fiscal_period = excluded.fiscal_period,
                primary_document = excluded.primary_document,
                filing_url = excluded.filing_url,
                updated_at = excluded.updated_at
            """,
            (
                ticker,
                cik,
                source_id,
                accession,
                form_type,
                filing_date,
                accepted_at,
                report_date,
                fiscal_year,
                fiscal_period,
                primary_document,
                filing_url,
                now,
                now,
            ),
        )
        count += 1
    return count


def submission_history_files(payload: dict[str, Any], *, max_files: int) -> list[str]:
    files = (payload.get("filings") or {}).get("files") if isinstance(payload.get("filings"), dict) else []
    if not isinstance(files, list):
        return []
    names: list[str] = []
    seen: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name or name in seen:
            continue
        names.append(name)
        seen.add(name)
        if max_files > 0 and len(names) >= max_files:
            break
    return names


def sync_submission_history_files(
    conn: Any,
    *,
    ticker: str,
    cik: str,
    source_id: str,
    root_payload: dict[str, Any],
    cache_dir: Path,
    force: bool,
    user_agent: str,
    timeout_sec: float,
    max_retries: int,
    sleep_sec: float,
    allowed_forms: set[str],
    start_date: str,
    url_template: str,
    max_files: int,
    ingestion_run_id: int,
) -> tuple[int, int]:
    filing_count = 0
    request_count = 0
    for file_name in submission_history_files(root_payload, max_files=max_files):
        url = url_template.format(file_name=file_name)
        request_count += 1
        try:
            status, payload, text, _ = load_or_fetch_json(
                url,
                cache_file=named_cache_path(cache_dir, source_id=source_id, name=file_name),
                force=force,
                user_agent=user_agent,
                timeout_sec=timeout_sec,
                max_retries=max_retries,
                sleep_sec=sleep_sec,
            )
        except SecRequestError as exc:
            record_raw_response(
                conn,
                source_id=source_id,
                endpoint=exc.url,
                status=exc.status_code,
                payload_text=exc.body,
                asof_date=datetime.now(timezone.utc).date().isoformat(),
                ingestion_run_id=ingestion_run_id,
            )
            LOGGER.warning("Skipping unavailable SEC submission history file ticker=%s url=%s status=%s", ticker, exc.url, exc.status_code)
            continue
        record_raw_response(
            conn,
            source_id=source_id,
            endpoint=url,
            status=status,
            payload_text=text,
            asof_date=datetime.now(timezone.utc).date().isoformat(),
            ingestion_run_id=ingestion_run_id,
        )
        filing_count += upsert_filings(
            conn,
            ticker=ticker,
            cik=cik,
            source_id=source_id,
            payload=payload,
            allowed_forms=allowed_forms,
            start_date=start_date,
        )
        time.sleep(sleep_sec)
    return filing_count, request_count


def parse_browse_atom_filings(atom_text: str, *, fallback_form_type: str) -> list[dict[str, str]]:
    try:
        root = ET.fromstring(atom_text.encode("utf-8"))
    except ET.ParseError:
        return []
    filings: list[dict[str, str]] = []
    for entry in root.iter():
        if local_name(entry.tag).lower() != "entry":
            continue
        values: dict[str, str] = {"form_type": fallback_form_type}
        link_href = ""
        for child in entry.iter():
            child_name = local_name(child.tag).lower()
            text = str(child.text or "").strip()
            if child_name == "link" and not link_href:
                link_href = str(child.attrib.get("href") or "").strip()
            elif child_name in {"accession-number", "accessionnumber"} and text:
                values["accession_number"] = text
            elif child_name in {"filing-date", "filingdate", "updated"} and text and "filing_date" not in values:
                values["filing_date"] = parse_date(text)
            elif child_name in {"filing-type", "filingtype", "category"} and text:
                values["form_type"] = text.upper()
            elif child_name == "title" and text and values.get("form_type") == fallback_form_type:
                values["form_type"] = text.split()[0].upper()
        if "accession_number" not in values and link_href:
            match = re.search(r"(\d{10}-\d{2}-\d{6})", link_href)
            if match:
                values["accession_number"] = match.group(1)
        if "filing_date" not in values and link_href:
            values["filing_date"] = ""
        if values.get("accession_number"):
            values["filing_url"] = link_href
            filings.append(values)
    return filings


def upsert_filing_stub(
    conn: Any,
    *,
    ticker: str,
    cik: str,
    source_id: str,
    accession: str,
    form_type: str,
    filing_date: str,
    filing_url: str,
) -> None:
    now = utc_now()
    accession_nodash = accession.replace("-", "")
    url = filing_url or f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession_nodash}/"
    conn.execute(
        """
        INSERT INTO fact_sec_filing(
            ticker, cik, source_id, accession_number, form_type, filing_date,
            accepted_at, report_date, fiscal_year, fiscal_period, primary_document,
            filing_url, source_detail, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, '', '', NULL, '', '', ?, 'sec_browse_edgar_atom', ?, ?)
        ON CONFLICT(ticker, accession_number, source_id) DO UPDATE SET
            cik = excluded.cik,
            form_type = excluded.form_type,
            filing_date = excluded.filing_date,
            filing_url = excluded.filing_url,
            source_detail = excluded.source_detail,
            updated_at = excluded.updated_at
        """,
        (ticker, cik, source_id, accession, form_type, filing_date, url, now, now),
    )


def sync_browse_edgar_filings(
    conn: Any,
    *,
    ticker: str,
    cik: str,
    source_id: str,
    cache_dir: Path,
    force: bool,
    user_agent: str,
    timeout_sec: float,
    max_retries: int,
    sleep_sec: float,
    allowed_forms: set[str],
    start_date: str,
    url_template: str,
    ingestion_run_id: int,
) -> tuple[int, int]:
    forms = sorted(allowed_forms or {"10-K", "10-Q"})
    filing_count = 0
    request_count = 0
    for form_type in forms:
        url = url_template.format(cik=cik, form_type=form_type)
        cache = named_cache_path(cache_dir, source_id=source_id, name=f"CIK{cik}_browse_{form_type}.atom")
        request_count += 1
        try:
            status, text, _ = load_or_fetch_text(
                url,
                cache_file=cache,
                force=force,
                user_agent=user_agent,
                timeout_sec=timeout_sec,
                max_retries=max_retries,
                sleep_sec=sleep_sec,
            )
        except SecRequestError as exc:
            record_raw_response(
                conn,
                source_id=source_id,
                endpoint=exc.url,
                status=exc.status_code,
                payload_text=exc.body,
                asof_date=datetime.now(timezone.utc).date().isoformat(),
                ingestion_run_id=ingestion_run_id,
            )
            LOGGER.warning("Skipping unavailable SEC browse feed ticker=%s form=%s status=%s", ticker, form_type, exc.status_code)
            continue
        record_raw_response(
            conn,
            source_id=source_id,
            endpoint=url,
            status=status,
            payload_text=text,
            asof_date=datetime.now(timezone.utc).date().isoformat(),
            ingestion_run_id=ingestion_run_id,
        )
        for filing in parse_browse_atom_filings(text, fallback_form_type=form_type):
            filing_date = parse_date(filing.get("filing_date"))
            if start_date and filing_date and filing_date < start_date:
                continue
            accession = str(filing.get("accession_number") or "").strip()
            if not accession:
                continue
            upsert_filing_stub(
                conn,
                ticker=ticker,
                cik=cik,
                source_id=source_id,
                accession=accession,
                form_type=str(filing.get("form_type") or form_type).upper(),
                filing_date=filing_date,
                filing_url=str(filing.get("filing_url") or ""),
            )
            filing_count += 1
        time.sleep(sleep_sec)
    return filing_count, request_count


def load_concept_map(conn: Any) -> dict[tuple[str, str], list[dict[str, Any]]]:
    rows = conn.execute(
        """
        SELECT taxonomy, concept_name, canonical_metric, financial_statement,
               period_type, sign_policy, priority
        FROM dim_xbrl_concept_map
        WHERE active_flag = 1
        ORDER BY priority, canonical_metric
        """
    ).fetchall()
    out: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        out.setdefault((str(row["taxonomy"]), str(row["concept_name"])), []).append(dict(row))
    return out


def make_fact_key(*parts: object) -> str:
    text = "|".join(str(part or "") for part in parts)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def apply_sign(value: float | None, sign_policy: str) -> float | None:
    if value is None:
        return None
    if sign_policy in {"positive_abs", "abs"}:
        return abs(value)
    if sign_policy == "negative_abs":
        return -abs(value)
    return value


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def namespace_uri(tag: str) -> str:
    if tag.startswith("{") and "}" in tag:
        return tag[1:].split("}", 1)[0]
    return ""


def taxonomy_from_namespace(raw: str) -> str:
    text = str(raw or "").lower()
    if "us-gaap" in text:
        return "us-gaap"
    if "ifrs-full" in text:
        return "ifrs-full"
    return ""


def parse_float_text(raw: object) -> float | None:
    text = str(raw or "").strip()
    if not text:
        return None
    text = text.replace(",", "").replace("$", "").replace("\u00a0", "")
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    return as_float(text)


def parse_inline_numeric_value(element: ET.Element) -> float | None:
    text = "".join(element.itertext()).strip()
    value = parse_float_text(text)
    if value is None:
        return None
    scale = as_int(element.attrib.get("scale"))
    if scale is not None:
        value *= 10.0**scale
    if str(element.attrib.get("sign") or "").strip() == "-":
        value = -abs(value)
    return value


def parse_namespace_prefixes(document_text: str) -> dict[str, str]:
    prefixes: dict[str, str] = {}
    for match in re.finditer(r"xmlns:([A-Za-z0-9_\-]+)\s*=\s*['\"]([^'\"]+)['\"]", document_text[:200000]):
        prefixes[match.group(1)] = match.group(2)
    return prefixes


def read_context_periods(root: ET.Element) -> dict[str, tuple[str, str, str]]:
    contexts: dict[str, tuple[str, str, str]] = {}
    for element in root.iter():
        if local_name(element.tag).lower() != "context":
            continue
        if context_has_dimensional_qualifier(element):
            continue
        context_id = str(element.attrib.get("id") or "").strip()
        if not context_id:
            continue
        start = ""
        end = ""
        instant = ""
        for child in element.iter():
            child_name = local_name(child.tag).lower()
            child_text = parse_date(child.text)
            if child_name == "startdate":
                start = child_text
            elif child_name == "enddate":
                end = child_text
            elif child_name == "instant":
                instant = child_text
        period_end = end or instant
        if period_end:
            contexts[context_id] = (start, period_end, context_id)
    return contexts


def element_has_dimension_attribute(element: ET.Element) -> bool:
    return any(local_name(key).lower() == "dimension" for key in element.attrib)


def context_has_dimensional_qualifier(context: ET.Element) -> bool:
    """Return True when a context is segment/scenario-specific rather than consolidated.

    Archive inline-XBRL fallback must ingest only consolidated facts. Segment or
    scenario contexts often carry business-unit, geographic, product, or legal
    entity dimensions that share concept/period/unit values with consolidated
    facts and can otherwise collide during canonical projection.
    """
    for element in context.iter():
        if element is context:
            continue
        element_name = local_name(element.tag).lower()
        if element_name in {"explicitmember", "typedmember"}:
            return True
        if element_has_dimension_attribute(element):
            return True
        if element_name in {"segment", "scenario"}:
            if str(element.text or "").strip() or element.attrib:
                return True
            if any(descendant is not element for descendant in element.iter()):
                return True
    return False


def read_units(root: ET.Element) -> dict[str, str]:
    units: dict[str, str] = {}
    for element in root.iter():
        if local_name(element.tag).lower() != "unit":
            continue
        unit_id = str(element.attrib.get("id") or "").strip()
        if not unit_id:
            continue
        measures: list[str] = []
        for child in element.iter():
            if local_name(child.tag).lower() == "measure" and child.text:
                measures.append(child.text.strip().split(":")[-1].upper())
        if measures:
            units[unit_id] = "*".join(measures)
    return units


def parse_archive_facts(document_text: str, *, document_name: str, concept_map: dict[tuple[str, str], list[dict[str, Any]]]) -> list[ArchiveFact]:
    try:
        root = ET.fromstring(document_text.encode("utf-8"))
    except ET.ParseError:
        cleaned = re.sub(r"<\?xml[^>]*\?>", "", document_text, count=1).strip()
        try:
            root = ET.fromstring(cleaned.encode("utf-8"))
        except ET.ParseError:
            return []
    context_periods = read_context_periods(root)
    units = read_units(root)
    prefix_map = parse_namespace_prefixes(document_text)
    facts: list[ArchiveFact] = []
    seen: set[tuple[str, str, str, str, str, float]] = set()

    for element in root.iter():
        element_local_name = local_name(element.tag)
        element_local_lower = element_local_name.lower()
        context_ref = str(element.attrib.get("contextRef") or element.attrib.get("contextref") or "").strip()
        if not context_ref or context_ref not in context_periods:
            continue
        unit_ref = str(element.attrib.get("unitRef") or element.attrib.get("unitref") or "").strip()
        decimals = str(element.attrib.get("decimals") or "").strip()
        taxonomy = ""
        concept_name = ""
        value: float | None = None

        if element_local_lower in INLINE_XBRL_LOCAL_NAMES and "name" in element.attrib:
            raw_name = str(element.attrib.get("name") or "").strip()
            if ":" in raw_name:
                prefix, concept_name = raw_name.split(":", 1)
                taxonomy = taxonomy_from_namespace(prefix_map.get(prefix, prefix))
            else:
                concept_name = raw_name
            value = parse_inline_numeric_value(element)
        else:
            taxonomy = taxonomy_from_namespace(namespace_uri(element.tag))
            concept_name = element_local_name
            value = parse_float_text(element.text)

        if not taxonomy or not concept_name or value is None:
            continue
        if (taxonomy, concept_name) not in concept_map:
            continue
        period_start, period_end, frame = context_periods[context_ref]
        if not period_end:
            continue
        unit = units.get(unit_ref, unit_ref or "")
        key = (taxonomy, concept_name, unit, period_start, period_end, value)
        if key in seen:
            continue
        seen.add(key)
        facts.append(
            ArchiveFact(
                taxonomy=taxonomy,
                concept_name=concept_name,
                unit=unit,
                value=value,
                period_start=period_start,
                period_end=period_end,
                frame=frame,
                decimals=decimals,
                payload_json=compact_json({"document": document_name, "contextRef": context_ref, "unitRef": unit_ref}),
            )
        )
    return facts


TEXT_TABLE_LABELS: list[tuple[str, str, tuple[str, ...], tuple[str, ...]]] = [
    ("Revenue", "duration", (r"^(?:total\s+)?(?:net\s+)?(?:sales|revenues?|revenue)\b",), (r"backlog", r"deferred", r"remaining", r"per\s+share", r"%")),
    ("CostOfRevenue", "duration", (r"^cost\s+of\s+(?:sales|revenues?|revenue|goods\s+sold)\b",), (r"%",)),
    ("GrossProfit", "duration", (r"^gross\s+profit\b",), (r"%",)),
    ("OperatingIncomeLoss", "duration", (r"^(?:income|loss)\s+from\s+operations\b", r"^operating\s+(?:income|loss)\b"), (r"%",)),
    ("NetIncomeLoss", "duration", (r"^net\s+(?:income|loss|earnings)\b",), (r"per\s+share", r"attributable", r"%")),
    ("Assets", "instant", (r"^total\s+assets\b",), ()),
    ("Liabilities", "instant", (r"^total\s+liabilities\b",), (r"and\s+(?:stockholders|shareholders|equity)",)),
    ("Equity", "instant", (r"^total\s+(?:stockholders|shareholders|members|owners).{0,30}equity\b", r"^total\s+equity\b"), (r"liabilities",)),
    ("CashAndCashEquivalents", "instant", (r"^cash\s+and\s+cash\s+equivalents\b",), (r"restricted", r"cash\s+flows?")),
    ("Inventory", "instant", (r"^inventor(?:y|ies)\b",), ()),
    ("AccountsReceivable", "instant", (r"^accounts\s+receivable\b", r"^trade\s+receivables\b"), ()),
    ("AccountsPayable", "instant", (r"^accounts\s+payable\b", r"^trade\s+payables\b"), ()),
    ("OperatingCashFlow", "duration", (r"^net\s+cash\s+(?:provided\s+by|used\s+in|provided\s+by\s+\(used\s+in\))\s+operating\s+activities\b",), ()),
    ("Capex", "duration", (r"^(?:purchases?|payments?)\s+(?:of|to\s+acquire)\s+(?:property|plant|equipment)", r"^capital\s+expenditures\b"), ()),
    ("ResearchAndDevelopment", "duration", (r"^(?:research\s+and\s+development|r\s*&\s*d)\b",), (r"%",)),
    ("DilutedShares", "duration", (r"^weighted\s+average.{0,35}diluted\s+shares\b",), (r"per\s+share",)),
    ("DebtTotal", "instant", (r"^total\s+(?:debt|borrowings)\b",), ()),
]


def strip_html_cell(raw: str) -> str:
    text = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", " ", raw)
    text = re.sub(r"(?is)<br\s*/?>", " ", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = html_lib.unescape(text).replace("\xa0", " ").replace("\u200b", " ").replace("\ufeff", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def html_table_rows(table_html: str) -> list[list[str]]:
    rows: list[list[str]] = []
    row_matches = list(re.finditer(r"(?is)<tr\b[^>]*>(.*?)</tr>", table_html))
    if not row_matches:
        return rows
    for row_match in row_matches:
        cells = [strip_html_cell(match.group(1)) for match in re.finditer(r"(?is)<t[dh]\b[^>]*>(.*?)</t[dh]>", row_match.group(1))]
        cells = [cell for cell in cells if cell]
        if cells:
            rows.append(cells)
    return rows


def table_scale(text: str) -> float:
    return table_scale_info(text)[0]


def table_scale_info(text: str) -> tuple[float, str, str]:
    normalized = re.sub(r"\s+", " ", text.lower())
    scale_patterns: tuple[tuple[float, str], ...] = (
        (1_000_000_000.0, r"(?:in|amounts in|presented in|expressed in|stated in|\$\s*in)\s+billions|\bbillions\b|\$000000000\b"),
        (1_000_000.0, r"(?:in|amounts in|presented in|expressed in|stated in|\$\s*in)\s+millions|\bmillions\b|\$000000\b"),
        (1_000.0, r"(?:in|amounts in|presented in|expressed in|stated in|\$\s*in)\s+thousands|\bthousands\b|\$000\b"),
    )
    for scale, pattern in scale_patterns:
        match = re.search(pattern, normalized)
        if match:
            return scale, match.group(0), "high"
    return 1.0, "not_detected_default_units", "low"


def detect_text_currency(text: str, *, allow_symbol_only: bool) -> str:
    normalized = re.sub(r"\s+", " ", strip_html_cell(text).lower())
    currency_patterns: tuple[tuple[str, tuple[str, ...]], ...] = (
        (
            "CAD",
            (
                r"\b(?:in|expressed in|presented in|stated in|reported in)\s+(?:thousands|millions)?(?:\s+of)?\s+(?:canadian dollars|cad|cdn dollars)\b",
                r"\b(?:cad|cdn)\s?\$",
                r"\bc\$\s?(?:000|millions?|thousands?)?\b",
            ),
        ),
        (
            "GBP",
            (
                r"\b(?:in|expressed in|presented in|stated in|reported in)\s+(?:thousands|millions)?(?:\s+of)?\s+(?:pounds sterling|gbp|pounds)\b",
                r"\bpounds sterling\b",
                r"\bgbp\b",
                r"£\s?(?:000|millions?|thousands?)?\b",
            ),
        ),
        (
            "EUR",
            (
                r"\b(?:in|expressed in|presented in|stated in|reported in)\s+(?:thousands|millions)?(?:\s+of)?\s+(?:euros?|eur)\b",
                r"\b(?:eur)\s?(?:000|millions?|thousands?)\b",
                r"€\s?(?:000|millions?|thousands?)?\b",
            ),
        ),
    )
    for currency, patterns in currency_patterns:
        for pattern in patterns:
            if re.search(pattern, normalized) and (allow_symbol_only or not pattern.startswith(("£", "€"))):
                return currency
    return "USD"


def text_table_unit(document_text: str, context_text: str = "") -> str:
    context_currency = detect_text_currency(context_text, allow_symbol_only=True)
    if context_currency != "USD":
        return context_currency
    intro_currency = detect_text_currency(document_text[:50000], allow_symbol_only=False)
    if intro_currency != "USD":
        return intro_currency
    return "USD"


def parse_table_number(raw: str) -> float | None:
    text = html_lib.unescape(str(raw or "")).replace("\xa0", " ").strip()
    if not text or "%" in text:
        return None
    text = text.replace("$", "").replace("£", "").replace("€", "").replace(",", "").replace("*", "").replace("\u200b", "")
    text = re.sub(r"\[[^\]]*\]|\([a-zA-Z]\)|\b[a-zA-Z]\b", "", text).strip()
    if text in {"-", "--", "---", "N/A", "n/a"}:
        return None
    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1].strip()
    match = re.fullmatch(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    value = float(text)
    return -abs(value) if negative else value


def normalize_table_label(raw: str) -> str:
    text = html_lib.unescape(str(raw or "")).replace("\xa0", " ")
    text = re.sub(r"\[[^\]]*\]|\([a-zA-Z]\)|\b[a-zA-Z]\b", " ", text)
    text = re.sub(r"[^A-Za-z0-9%&()'/ -]+", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def text_table_label_concept(label: str) -> tuple[str, str] | None:
    normalized = normalize_table_label(label)
    if not normalized or len(normalized) > 180:
        return None
    for concept_name, period_type, allow_patterns, reject_patterns in TEXT_TABLE_LABELS:
        if any(re.search(pattern, normalized) for pattern in reject_patterns):
            continue
        if any(re.search(pattern, normalized) for pattern in allow_patterns):
            return concept_name, period_type
    return None


def row_values(cells: list[str]) -> list[float]:
    values: list[float] = []
    for cell in cells[1:]:
        value = parse_table_number(cell)
        if value is not None:
            values.append(value)
    return values


MONTH_LOOKUP = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


def month_number(raw: str) -> int | None:
    token = re.sub(r"[^A-Za-z]", "", raw).lower()
    return MONTH_LOOKUP.get(token)


def period_iso(year: int, month: int, day: int) -> str:
    try:
        return datetime(year, month, day).date().isoformat()
    except ValueError:
        return ""


def month_day_pairs(text: str) -> list[tuple[int, int, str]]:
    pairs: list[tuple[int, int, str]] = []
    month_names = "|".join(sorted(MONTH_LOOKUP, key=len, reverse=True))
    for match in re.finditer(rf"\b({month_names})\.?\s+(\d{{1,2}})(?:,|\b)", text, flags=re.IGNORECASE):
        month = month_number(match.group(1))
        day = int(match.group(2))
        if month is not None:
            pairs.append((month, day, match.group(0)))
    return pairs


def full_text_dates(text: str) -> list[tuple[str, str]]:
    dates: list[tuple[str, str]] = []
    month_names = "|".join(sorted(MONTH_LOOKUP, key=len, reverse=True))
    for match in re.finditer(rf"\b({month_names})\.?\s+(\d{{1,2}}),?\s+((?:19|20)\d{{2}})\b", text, flags=re.IGNORECASE):
        month = month_number(match.group(1))
        if month is None:
            continue
        value = period_iso(int(match.group(3)), month, int(match.group(2)))
        if value:
            dates.append((value, match.group(0)))
    return dates


def standalone_years(text: str) -> list[int]:
    return [int(match.group(1)) for match in re.finditer(r"(?<![\d.])((?:19|20)\d{2})(?![\d.])", text)]


def duration_days_from_table_context(text: str) -> int:
    normalized = re.sub(r"\s+", " ", text.lower())
    if "three months ended" in normalized or "quarter ended" in normalized:
        return 90
    if "six months ended" in normalized:
        return 181
    if "nine months ended" in normalized:
        return 273
    return 364


def duration_days_for_period_evidence(*, month: int | None, day: int | None, combined_text: str, default_days: int, form_type: str = "") -> int:
    normalized = re.sub(r"\s+", " ", combined_text.lower())
    form = str(form_type or "").upper()
    if month == 12 and day in {30, 31} and re.search(r"\byears?\s+ended\b|\bfiscal\s+years?\b", normalized):
        return 364
    if re.search(r"\bthree\s+months?\s+ended\b|\bquarter\s+ended\b", normalized) and not (month == 12 and day in {30, 31}):
        return 90
    if re.search(r"\bsix\s+months?\s+ended\b", normalized):
        return 181
    if re.search(r"\bnine\s+months?\s+ended\b", normalized):
        return 273
    if form == "6-K" and not re.search(r"\byears?\s+ended\b|\bfiscal\s+years?\b", normalized):
        if month == 3 and day in {30, 31}:
            return 90
        if month == 6 and day in {29, 30}:
            return 181
        if month == 9 and day in {29, 30}:
            return 273
    return default_days


def infer_text_table_periods(
    header_rows: list[list[str]],
    *,
    value_count: int,
    fallback_period_end: str,
    context_text: str,
    form_type: str = "",
) -> list[tuple[str, int, str, str]]:
    header_text = " ".join(cell for row in header_rows for cell in row if cell)
    combined = re.sub(r"\s+", " ", f"{header_text} {strip_html_cell(context_text[:1200])}").strip()
    duration_days = duration_days_from_table_context(combined)
    full_dates = full_text_dates(header_text)
    if len(full_dates) >= value_count:
        inferred_full: list[tuple[str, int, str, str]] = []
        for period_end, evidence in full_dates[:value_count]:
            parsed = datetime.strptime(period_end, "%Y-%m-%d").date()
            inferred_full.append(
                (
                    period_end,
                    duration_days_for_period_evidence(
                        month=parsed.month,
                        day=parsed.day,
                        combined_text=combined,
                        default_days=duration_days,
                        form_type=form_type,
                    ),
                    "table_column_date",
                    evidence,
                )
            )
        return inferred_full

    pairs = month_day_pairs(header_text)
    years = standalone_years(header_text)
    if years and pairs:
        inferred: list[tuple[str, int, str, str]] = []
        if len(pairs) == 1:
            month, day, pair_evidence = pairs[0]
            for year in years[:value_count]:
                period_end = period_iso(year, month, day)
                if period_end:
                    inferred.append(
                        (
                            period_end,
                            duration_days_for_period_evidence(
                                month=month,
                                day=day,
                                combined_text=combined,
                                default_days=duration_days,
                                form_type=form_type,
                            ),
                            "table_column_month_day_year",
                            f"{pair_evidence} {year}",
                        )
                    )
        else:
            for idx, year in enumerate(years[:value_count]):
                month, day, pair_evidence = pairs[min(idx, len(pairs) - 1)]
                period_end = period_iso(year, month, day)
                if period_end:
                    inferred.append(
                        (
                            period_end,
                            duration_days_for_period_evidence(
                                month=month,
                                day=day,
                                combined_text=combined,
                                default_days=duration_days,
                                form_type=form_type,
                            ),
                            "table_column_month_day_year",
                            f"{pair_evidence} {year}",
                        )
                    )
        if len(inferred) == value_count:
            return inferred

    if years and re.search(r"\byears?\s+ended\b|\bfiscal\s+years?\b", combined, re.IGNORECASE):
        inferred = [(period_iso(year, 12, 31), 364, "table_column_year_default_dec31", str(year)) for year in years[:value_count]]
        inferred = [item for item in inferred if item[0]]
        if len(inferred) == value_count:
            return inferred

    return [
        (fallback_period_end, duration_days, "fallback_filing_or_report_date", fallback_period_end)
        for _ in range(value_count)
    ]


def period_start_for_text_fact(period_end: str, period_type: str, duration_days: int) -> str:
    if period_type == "instant":
        return ""
    parsed = parse_date(period_end)
    if not parsed:
        return ""
    end = datetime.strptime(parsed, "%Y-%m-%d").date()
    return (end - timedelta(days=duration_days)).isoformat()


def parse_archive_text_table_facts(
    document_text: str,
    *,
    document_name: str,
    filing: dict[str, Any],
) -> list[ArchiveFact]:
    lower_document_name = document_name.lower()
    if lower_document_name.endswith(("-index.html", "-index-headers.html")):
        return []
    period_end = parse_date(filing.get("report_date")) or parse_date(filing.get("filing_date"))
    form_type = str(filing.get("form_type") or "").strip().upper()
    if not period_end:
        return []
    facts: list[ArchiveFact] = []
    seen: set[tuple[str, str, str, float]] = set()
    for table_index, match in enumerate(re.finditer(r"(?is)<table\b[^>]*>.*?</table>", document_text), start=1):
        table_html = match.group(0)
        rows = html_table_rows(table_html)
        concept_row_flags = [bool(row and text_table_label_concept(row[0])) for row in rows]
        if sum(1 for flag in concept_row_flags if flag) < 2:
            continue
        table_text = strip_html_cell(table_html)
        scale_context = document_text[max(0, match.start() - 2500) : min(len(document_text), match.end() + 500)]
        unit = text_table_unit(document_text, scale_context)
        normalized_table_text = f"{strip_html_cell(scale_context)} {table_text}".lower()
        if not any(
            marker in normalized_table_text
            for marker in (
                "consolidated",
                "summary financial",
                "statement of operations",
                "statement of income",
                "balance sheet",
                "cash flows",
                "financial position",
            )
        ):
            continue
        scale, scale_source, scale_confidence = table_scale_info(f"{scale_context} {table_text[:1000]}")
        first_concept_idx = next((idx for idx, flag in enumerate(concept_row_flags) if flag), 0)
        table_header_rows = rows[:first_concept_idx]
        for row_index, cells in enumerate(rows):
            if len(cells) < 2:
                continue
            label_result = text_table_label_concept(cells[0])
            if label_result is None:
                continue
            concept_name, period_type = label_result
            values = row_values(cells)
            if not values:
                continue
            local_header_rows = [
                row
                for row in rows[max(0, row_index - 8) : row_index]
                if not (row and text_table_label_concept(row[0]))
            ] or table_header_rows
            periods = infer_text_table_periods(
                local_header_rows,
                value_count=len(values),
                fallback_period_end=period_end,
                context_text=scale_context,
                form_type=form_type,
            )
            if table_header_rows and all(item[2] == "fallback_filing_or_report_date" for item in periods):
                periods = infer_text_table_periods(
                    table_header_rows,
                    value_count=len(values),
                    fallback_period_end=period_end,
                    context_text=scale_context,
                    form_type=form_type,
                )
            for value_index, raw_value in enumerate(values):
                fact_period_end, duration_days, period_confidence, period_evidence = periods[min(value_index, len(periods) - 1)]
                if period_type == "duration" and period_confidence == "fallback_filing_or_report_date":
                    continue
                value = raw_value * scale
                key = (concept_name, fact_period_end, unit, value)
                if key in seen:
                    continue
                seen.add(key)
                facts.append(
                    ArchiveFact(
                        taxonomy="sec-text",
                        concept_name=concept_name,
                        unit=unit,
                        value=value,
                        period_start=period_start_for_text_fact(fact_period_end, period_type, duration_days),
                        period_end=fact_period_end,
                        frame=f"text_table:{document_name}:{table_index}:{row_index}:{value_index}",
                        decimals="",
                        payload_json=compact_json(
                            {
                                "document": document_name,
                                "label": cells[0],
                                "source": "sec_archive_text_table",
                                "scale": scale,
                                "scale_source": scale_source,
                                "scale_confidence": scale_confidence,
                                "period_confidence": period_confidence,
                                "period_evidence": period_evidence,
                                "column_index": value_index,
                                "column_count": len(values),
                            }
                        ),
                        source_detail=TEXT_TABLE_SOURCE_DETAIL,
                    )
                )
    return facts


def archive_document_candidates(index_payload: dict[str, Any], *, primary_document: str, max_documents: int) -> list[str]:
    raw_items = ((index_payload.get("directory") or {}).get("item") or [])
    candidates: list[str] = []
    primary = str(primary_document or "").strip()
    if primary and primary.lower().endswith(ARCHIVE_ALLOWED_DOCUMENT_SUFFIXES):
        candidates.append(primary)
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        lower = name.lower()
        if not name or name in candidates:
            continue
        if not lower.endswith(ARCHIVE_ALLOWED_DOCUMENT_SUFFIXES):
            continue
        if any(lower.endswith(suffix) for suffix in ARCHIVE_EXCLUDED_SUFFIXES):
            continue
        candidates.append(name)
    candidates.sort(
        key=lambda name: (
            0 if name == primary else 1 if re.search(r"ex(?:hibit)?99|ex99", name, re.IGNORECASE) else 2,
            1 if name.lower().endswith(".txt") else 0,
            name.lower(),
        )
    )
    if max_documents > 0:
        return candidates[:max_documents]
    return candidates


def archive_cache_file(cache_dir: Path, *, cik: str, accession: str, document_name: str) -> Path:
    safe_document = re.sub(r"[^A-Za-z0-9_.-]+", "_", document_name)
    return cache_dir / "sec_archive_xbrl" / f"CIK{cik}" / accession.replace("-", "") / safe_document


def upsert_archive_facts(
    conn: Any,
    *,
    ticker: str,
    cik: str,
    source_id: str,
    filing: dict[str, Any],
    document_name: str,
    facts: list[ArchiveFact],
    concept_map: dict[tuple[str, str], list[dict[str, Any]]],
    start_date: str,
) -> tuple[int, int]:
    now = utc_now()
    raw_count = 0
    mapped_count = 0
    accession = str(filing.get("accession_number") or "").strip()
    form_type = str(filing.get("form_type") or "").strip().upper()
    filing_date = parse_date(filing.get("filing_date"))
    accepted_at = str(filing.get("accepted_at") or "").strip()
    fiscal_year = as_int(filing.get("fiscal_year"))
    fiscal_period = str(filing.get("fiscal_period") or "").strip()
    for fact in facts:
        if start_date and filing_date and filing_date < start_date:
            continue
        fact_key = make_fact_key(ticker, source_id, accession, fact.taxonomy, fact.concept_name, fact.unit, fact.period_start, fact.period_end, fact.frame, document_name)
        conn.execute(
            """
            INSERT INTO fact_sec_xbrl_fact_raw(
                fact_key, ticker, cik, source_id, accession_number, form_type,
                filing_date, accepted_at, fiscal_year, fiscal_period, period_start,
                period_end, frame, taxonomy, concept_name, unit, raw_value, decimals,
                source_detail, payload_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(fact_key) DO UPDATE SET
                filing_date = excluded.filing_date,
                accepted_at = excluded.accepted_at,
                fiscal_year = excluded.fiscal_year,
                fiscal_period = excluded.fiscal_period,
                raw_value = excluded.raw_value,
                decimals = excluded.decimals,
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            (
                fact_key,
                ticker,
                cik,
                source_id,
                accession,
                form_type,
                filing_date,
                accepted_at,
                fiscal_year,
                fiscal_period,
                fact.period_start,
                fact.period_end,
                fact.frame,
                fact.taxonomy,
                fact.concept_name,
                fact.unit,
                fact.value,
                fact.decimals,
                fact.source_detail,
                fact.payload_json,
                now,
                now,
            ),
        )
        raw_row = conn.execute("SELECT raw_fact_id FROM fact_sec_xbrl_fact_raw WHERE fact_key = ?", (fact_key,)).fetchone()
        raw_fact_id = int(raw_row["raw_fact_id"]) if raw_row is not None else None
        raw_count += 1
        for mapping in concept_map.get((fact.taxonomy, fact.concept_name), []):
            mapped_value = apply_sign(fact.value, str(mapping["sign_policy"]))
            conn.execute(
                """
                INSERT INTO fact_sec_xbrl_fact(
                    raw_fact_id, ticker, cik, source_id, accession_number,
                    form_type, filing_date, accepted_at, fiscal_year, fiscal_period,
                    period_start, period_end, frame, taxonomy, concept_name,
                    canonical_metric, financial_statement, period_type, unit,
                    value, sign_policy, source_priority, source_detail,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker, source_id, accession_number, taxonomy, concept_name, canonical_metric, unit, period_start, period_end, frame)
                DO UPDATE SET
                    raw_fact_id = excluded.raw_fact_id,
                    filing_date = excluded.filing_date,
                    accepted_at = excluded.accepted_at,
                    fiscal_year = excluded.fiscal_year,
                    fiscal_period = excluded.fiscal_period,
                    value = excluded.value,
                    sign_policy = excluded.sign_policy,
                    source_priority = excluded.source_priority,
                    source_detail = excluded.source_detail,
                    updated_at = excluded.updated_at
                """,
                (
                    raw_fact_id,
                    ticker,
                    cik,
                    source_id,
                    accession,
                    form_type,
                    filing_date,
                    accepted_at,
                    fiscal_year,
                    fiscal_period,
                    fact.period_start,
                    fact.period_end,
                    fact.frame,
                    fact.taxonomy,
                    fact.concept_name,
                    str(mapping["canonical_metric"]),
                    str(mapping["financial_statement"]),
                    str(mapping["period_type"]),
                    fact.unit,
                    mapped_value,
                    str(mapping["sign_policy"]),
                    int(mapping["priority"]),
                    f"{fact.source_detail}_mapped",
                    now,
                    now,
                ),
            )
            mapped_count += 1
    return raw_count, mapped_count


def purge_archive_xbrl_facts(conn: Any, *, ticker: str, source_id: str, model_family: str) -> None:
    conn.execute(
        """
        DELETE FROM fact_financial_statement_canonical
        WHERE ticker = ?
          AND source_id = ?
          AND model_family = ?
          AND EXISTS (
                SELECT 1
                FROM fact_sec_xbrl_fact f
                WHERE f.ticker = fact_financial_statement_canonical.ticker
                  AND f.source_id = fact_financial_statement_canonical.source_id
                  AND f.canonical_metric = fact_financial_statement_canonical.canonical_metric
                  AND f.period_end = fact_financial_statement_canonical.period_end
                  AND COALESCE(f.accession_number, '') = COALESCE(fact_financial_statement_canonical.accession_number, '')
                  AND COALESCE(f.unit, '') = COALESCE(fact_financial_statement_canonical.unit, '')
                  AND (
                        f.source_detail IN ('sec_archive_xbrl_mapped', 'sec_archive_text_table_mapped')
                     OR f.raw_fact_id IN (
                            SELECT raw_fact_id
                            FROM fact_sec_xbrl_fact_raw r
                            WHERE r.ticker = ?
                              AND r.source_id = ?
                              AND r.source_detail IN ('sec_archive_xbrl', 'sec_archive_text_table')
                        )
                  )
          )
        """,
        (ticker, source_id, model_family, ticker, source_id),
    )
    conn.execute(
        """
        DELETE FROM fact_sec_xbrl_fact
        WHERE ticker = ?
          AND source_id = ?
          AND (
                source_detail IN ('sec_archive_xbrl_mapped', 'sec_archive_text_table_mapped')
             OR raw_fact_id IN (
                    SELECT raw_fact_id
                    FROM fact_sec_xbrl_fact_raw
                    WHERE ticker = ?
                      AND source_id = ?
                      AND source_detail IN ('sec_archive_xbrl', 'sec_archive_text_table')
                )
          )
        """,
        (ticker, source_id, ticker, source_id),
    )
    conn.execute(
        """
        DELETE FROM fact_sec_xbrl_fact_raw
        WHERE ticker = ?
          AND source_id = ?
          AND source_detail IN ('sec_archive_xbrl', 'sec_archive_text_table')
        """,
        (ticker, source_id),
    )


def should_attempt_archive(override: ReportingOverride | None) -> bool:
    if override is None:
        return False
    return override.reporting_profile in {
        "SEC_RAW_ARCHIVE_REQUIRED",
        "RECENT_IPO_DEVELOPMENT_STAGE",
        "RECENT_PUBLIC_STUB",
        "SEC_20F_METADATA_ONLY",
        "FOREIGN_PRIVATE_ISSUER_ARCHIVE_REQUIRED",
        "FPI_HYBRID_STUB_LOADED",
        "FPI_HYBRID_LOADED",
    }


def sync_archive_xbrl(
    conn: Any,
    *,
    ticker: str,
    cik: str,
    source_id: str,
    submissions_source_id: str,
    model_family: str,
    cache_dir: Path,
    force: bool,
    user_agent: str,
    timeout_sec: float,
    max_retries: int,
    sleep_sec: float,
    concept_map: dict[tuple[str, str], list[dict[str, Any]]],
    start_date: str,
    index_url_template: str,
    document_url_template: str,
    max_filings: int,
    max_documents: int,
    parse_all_documents: bool = False,
    ingestion_run_id: int,
) -> tuple[int, int, int]:
    purge_archive_xbrl_facts(conn, ticker=ticker, source_id=source_id, model_family=model_family)
    filing_rows = conn.execute(
        """
        SELECT accession_number, form_type, filing_date, accepted_at, report_date, fiscal_year,
               fiscal_period, primary_document
        FROM fact_sec_filing
        WHERE ticker = ?
          AND source_id = ?
        ORDER BY filing_date DESC, accession_number DESC
        """,
        (ticker, submissions_source_id),
    ).fetchall()
    if not filing_rows:
        return 0, 0, 0
    if max_filings > 0:
        filing_rows = filing_rows[:max_filings]
    raw_total = 0
    mapped_total = 0
    requests = 0
    for row in filing_rows:
        filing = dict(row)
        accession = str(filing.get("accession_number") or "")
        accession_nodash = accession.replace("-", "")
        if not accession_nodash:
            continue
        cik_int = str(int(cik))
        index_url = index_url_template.format(cik_int=cik_int, accession_nodash=accession_nodash)
        index_cache = archive_cache_file(cache_dir, cik=cik, accession=accession, document_name="index.json")
        requests += 1
        try:
            status, index_payload, index_text, _ = load_or_fetch_json(
                index_url,
                cache_file=index_cache,
                force=force,
                user_agent=user_agent,
                timeout_sec=timeout_sec,
                max_retries=max_retries,
                sleep_sec=sleep_sec,
            )
        except SecRequestError as exc:
            record_raw_response(
                conn,
                source_id=source_id,
                endpoint=exc.url,
                status=exc.status_code,
                payload_text=exc.body,
                asof_date=datetime.now(timezone.utc).date().isoformat(),
                ingestion_run_id=ingestion_run_id,
            )
            LOGGER.warning("Skipping unavailable SEC archive index ticker=%s accession=%s status=%s", ticker, accession, exc.status_code)
            continue
        record_raw_response(
            conn,
            source_id=source_id,
            endpoint=index_url,
            status=status,
            payload_text=index_text,
            asof_date=datetime.now(timezone.utc).date().isoformat(),
            ingestion_run_id=ingestion_run_id,
        )
        for document_name in archive_document_candidates(index_payload, primary_document=str(filing.get("primary_document") or ""), max_documents=max_documents):
            document_url = document_url_template.format(cik_int=cik_int, accession_nodash=accession_nodash, document_name=document_name)
            document_cache = archive_cache_file(cache_dir, cik=cik, accession=accession, document_name=document_name)
            requests += 1
            try:
                _, document_text, _ = load_or_fetch_text(
                    document_url,
                    cache_file=document_cache,
                    force=force,
                    user_agent=user_agent,
                    timeout_sec=timeout_sec,
                    max_retries=max_retries,
                    sleep_sec=sleep_sec,
                )
            except SecRequestError as exc:
                record_raw_response(
                    conn,
                    source_id=source_id,
                    endpoint=exc.url,
                    status=exc.status_code,
                    payload_text=exc.body,
                    asof_date=datetime.now(timezone.utc).date().isoformat(),
                    ingestion_run_id=ingestion_run_id,
                )
                LOGGER.warning("Skipping unavailable SEC archive document ticker=%s accession=%s document=%s status=%s", ticker, accession, document_name, exc.status_code)
                continue
            facts = [
                *parse_archive_facts(document_text, document_name=document_name, concept_map=concept_map),
                *parse_archive_text_table_facts(document_text, document_name=document_name, filing=filing),
            ]
            raw_count, mapped_count = upsert_archive_facts(
                conn,
                ticker=ticker,
                cik=cik,
                source_id=source_id,
                filing=filing,
                document_name=document_name,
                facts=facts,
                concept_map=concept_map,
                start_date=start_date,
            )
            raw_total += raw_count
            mapped_total += mapped_count
            if mapped_count > 0 and not parse_all_documents:
                break
        time.sleep(sleep_sec)
    return raw_total, mapped_total, requests


def upsert_companyfacts(
    conn: Any,
    *,
    ticker: str,
    cik: str,
    source_id: str,
    payload: dict[str, Any],
    concept_map: dict[tuple[str, str], list[dict[str, Any]]],
    start_date: str,
) -> tuple[int, int]:
    facts = payload.get("facts") or {}
    if not isinstance(facts, dict):
        return 0, 0
    now = utc_now()
    raw_count = 0
    mapped_count = 0
    for taxonomy, concepts in facts.items():
        if not isinstance(concepts, dict):
            continue
        taxonomy_text = str(taxonomy)
        for concept_name, concept_payload in concepts.items():
            if not isinstance(concept_payload, dict):
                continue
            units = concept_payload.get("units") or {}
            if not isinstance(units, dict):
                continue
            mappings = concept_map.get((taxonomy_text, str(concept_name)), [])
            for unit, fact_rows in units.items():
                if not isinstance(fact_rows, list):
                    continue
                for fact in fact_rows:
                    if not isinstance(fact, dict):
                        continue
                    period_end = parse_date(fact.get("end"))
                    filing_date = parse_date(fact.get("filed"))
                    if not period_end or (start_date and filing_date and filing_date < start_date):
                        continue
                    value = as_float(fact.get("val"))
                    accession = str(fact.get("accn") or "").strip()
                    form_type = str(fact.get("form") or "").strip().upper()
                    fiscal_year_raw = fact.get("fy")
                    fiscal_year = as_int(fiscal_year_raw)
                    fiscal_period = str(fact.get("fp") or "").strip()
                    period_start = parse_date(fact.get("start"))
                    frame = str(fact.get("frame") or "").strip()
                    fact_key = make_fact_key(ticker, source_id, accession, taxonomy_text, concept_name, unit, period_start, period_end, frame)
                    conn.execute(
                        """
                        INSERT INTO fact_sec_xbrl_fact_raw(
                            fact_key, ticker, cik, source_id, accession_number, form_type,
                            filing_date, fiscal_year, fiscal_period, period_start, period_end,
                            frame, taxonomy, concept_name, unit, raw_value, decimals,
                            source_detail, payload_json, created_at, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'sec_companyfacts', ?, ?, ?)
                        ON CONFLICT(fact_key) DO UPDATE SET
                            filing_date = excluded.filing_date,
                            fiscal_year = excluded.fiscal_year,
                            fiscal_period = excluded.fiscal_period,
                            raw_value = excluded.raw_value,
                            decimals = excluded.decimals,
                            payload_json = excluded.payload_json,
                            updated_at = excluded.updated_at
                        """,
                        (
                            fact_key,
                            ticker,
                            cik,
                            source_id,
                            accession,
                            form_type,
                            filing_date,
                            fiscal_year,
                            fiscal_period,
                            period_start,
                            period_end,
                            frame,
                            taxonomy_text,
                            str(concept_name),
                            str(unit),
                            value,
                            str(fact.get("decimals") or ""),
                            compact_json(fact),
                            now,
                            now,
                        ),
                    )
                    raw_row = conn.execute("SELECT raw_fact_id FROM fact_sec_xbrl_fact_raw WHERE fact_key = ?", (fact_key,)).fetchone()
                    raw_fact_id = int(raw_row["raw_fact_id"]) if raw_row is not None else None
                    raw_count += 1
                    for mapping in mappings:
                        mapped_value = apply_sign(value, str(mapping["sign_policy"]))
                        conn.execute(
                            """
                            INSERT INTO fact_sec_xbrl_fact(
                                raw_fact_id, ticker, cik, source_id, accession_number,
                                form_type, filing_date, fiscal_year, fiscal_period,
                                period_start, period_end, frame, taxonomy, concept_name,
                                canonical_metric, financial_statement, period_type, unit,
                                value, sign_policy, source_priority, source_detail,
                                created_at, updated_at
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'sec_companyfacts_mapped', ?, ?)
                            ON CONFLICT(ticker, source_id, accession_number, taxonomy, concept_name, canonical_metric, unit, period_start, period_end, frame)
                            DO UPDATE SET
                                raw_fact_id = excluded.raw_fact_id,
                                filing_date = excluded.filing_date,
                                fiscal_year = excluded.fiscal_year,
                                fiscal_period = excluded.fiscal_period,
                                value = excluded.value,
                                sign_policy = excluded.sign_policy,
                                source_priority = excluded.source_priority,
                                updated_at = excluded.updated_at
                            """,
                            (
                                raw_fact_id,
                                ticker,
                                cik,
                                source_id,
                                accession,
                                form_type,
                                filing_date,
                                fiscal_year,
                                fiscal_period,
                                period_start,
                                period_end,
                                frame,
                                taxonomy_text,
                                str(concept_name),
                                str(mapping["canonical_metric"]),
                                str(mapping["financial_statement"]),
                                str(mapping["period_type"]),
                                str(unit),
                                mapped_value,
                                str(mapping["sign_policy"]),
                                int(mapping["priority"]),
                                now,
                                now,
                            ),
                        )
                        mapped_count += 1
    return raw_count, mapped_count


def classify_reporting_profile(
    conn: Any,
    *,
    ticker: str,
    cik: str,
    country: str,
    model_family: str,
    source_id: str,
    override: ReportingOverride | None = None,
) -> dict[str, Any]:
    latest = conn.execute(
        """
        SELECT accession_number, filing_date, form_type
        FROM fact_sec_filing
        WHERE ticker = ?
        ORDER BY filing_date DESC, accession_number DESC
        LIMIT 1
        """,
        (ticker,),
    ).fetchone()
    tax_rows = conn.execute(
        """
        SELECT taxonomy, COUNT(*) AS n
        FROM fact_sec_xbrl_fact
        WHERE ticker = ?
        GROUP BY taxonomy
        """,
        (ticker,),
    ).fetchall()
    metrics = {
        str(row["canonical_metric"])
        for row in conn.execute(
            "SELECT DISTINCT canonical_metric FROM fact_sec_xbrl_fact WHERE ticker = ?",
            (ticker,),
        ).fetchall()
    }
    taxonomies = {str(row["taxonomy"]): int(row["n"] or 0) for row in tax_rows}
    has_core = {"revenue", "assets"} <= metrics
    has_balance_sheet = "assets" in metrics and bool(metrics.intersection({"cash_and_equivalents", "liabilities", "equity"}))
    has_operating_or_income = bool(metrics.intersection({"operating_income", "net_income", "operating_cash_flow", "capex", "research_and_development"}))
    has_partial_xbrl = has_balance_sheet or has_operating_or_income
    latest_form = str(latest["form_type"]) if latest is not None else ""
    latest_filing = str(latest["filing_date"]) if latest is not None else ""
    latest_accession = str(latest["accession_number"]) if latest is not None else ""
    country_text = str(country or "").strip()

    raw_archive_override = override is not None and override.reporting_profile == "SEC_RAW_ARCHIVE_REQUIRED"
    archive_attempt_override = should_attempt_archive(override)
    fallback_only_override = archive_attempt_override
    if override is not None and override.reporting_profile and not fallback_only_override:
        profile = override.reporting_profile
        standard = override.reporting_standard or "override"
        primary_taxonomy = ",".join(sorted(taxonomies))
        fallback = override.fallback_status
        confidence = override.financial_confidence
        usable_xbrl = override.usable_xbrl_flag
        reason = override.review_reason or override.handling_type
    elif override is not None and override.reporting_profile in RECENT_STUB_PROFILES and (has_core or has_partial_xbrl):
        profile = override.reporting_profile
        if taxonomies.get("us-gaap", 0) > 0:
            standard = "US_GAAP_STUB"
            primary_taxonomy = "us-gaap"
        elif taxonomies.get("ifrs-full", 0) > 0:
            standard = "IFRS_STUB"
            primary_taxonomy = "ifrs-full"
        elif taxonomies.get("sec-text", 0) > 0:
            standard = "sec_archive_text_table_stub"
            primary_taxonomy = "sec-text"
        else:
            standard = override.reporting_standard or "recent_public_stub"
            primary_taxonomy = ",".join(sorted(taxonomies))
        fallback = "recent_public_stub_period_limited"
        confidence = max(override.financial_confidence, 0.55 if has_core else 0.35)
        usable_xbrl = 1
        reason = override.review_reason or "recent_public_stub_limited_annual_history"
    elif override is not None and override.reporting_profile in FPI_HYBRID_PROFILES and (has_core or has_partial_xbrl):
        profile = override.reporting_profile
        if taxonomies.get("ifrs-full", 0) > 0:
            standard = "IFRS_FPI_HYBRID"
            primary_taxonomy = "ifrs-full"
        elif taxonomies.get("sec-text", 0) > 0:
            standard = "sec_archive_text_table_fpi_hybrid"
            primary_taxonomy = "sec-text"
        elif taxonomies.get("us-gaap", 0) > 0:
            standard = "US_GAAP_FPI_HYBRID"
            primary_taxonomy = "us-gaap"
        else:
            standard = override.reporting_standard or "fpi_hybrid"
            primary_taxonomy = ",".join(sorted(taxonomies))
        if profile == "FPI_HYBRID_LOADED":
            fallback = "fpi_hybrid_loaded"
            reason = ""
        else:
            fallback = "fpi_hybrid_stub_period_limited"
            reason = override.review_reason or "fpi_hybrid_stub_loaded_not_rank_ready"
        confidence = max(override.financial_confidence, 0.55 if has_core else 0.35)
        usable_xbrl = 1
    elif has_core and taxonomies.get("us-gaap", 0) > 0:
        profile = "SEC_XBRL_US_GAAP"
        standard = "US_GAAP"
        primary_taxonomy = "us-gaap"
        fallback = "none"
        confidence = 0.9
        usable_xbrl = 1
        reason = ""
    elif has_core and taxonomies.get("ifrs-full", 0) > 0:
        profile = "SEC_XBRL_IFRS"
        standard = "IFRS"
        primary_taxonomy = "ifrs-full"
        fallback = "none"
        confidence = 0.75
        usable_xbrl = 1
        reason = ""
    elif has_core and taxonomies.get("sec-text", 0) > 0 and override is not None and override.reporting_profile == "RECENT_IPO_DEVELOPMENT_STAGE":
        profile = "RECENT_IPO_DEVELOPMENT_STAGE"
        standard = "sec_archive_text_table"
        primary_taxonomy = "sec-text"
        fallback = "text_table_extracted_lifecycle_limited"
        confidence = max(override.financial_confidence, 0.45)
        usable_xbrl = 1
        reason = override.review_reason or "recent_ipo_limited_public_filing_history"
    elif has_core and taxonomies.get("sec-text", 0) > 0:
        profile = "SEC_ARCHIVE_TEXT_TABLE"
        standard = "sec_archive_text_table"
        primary_taxonomy = "sec-text"
        fallback = "text_table_extracted"
        confidence = 0.55
        usable_xbrl = 1
        reason = ""
    elif has_partial_xbrl and taxonomies.get("us-gaap", 0) > 0:
        profile = "SEC_XBRL_US_GAAP_PARTIAL"
        standard = "US_GAAP_PARTIAL"
        primary_taxonomy = "us-gaap"
        fallback = "component_limited"
        confidence = 0.55
        usable_xbrl = 1
        reason = "partial_xbrl_missing_core_revenue_or_assets"
    elif has_partial_xbrl and taxonomies.get("ifrs-full", 0) > 0:
        profile = "SEC_XBRL_IFRS_PARTIAL"
        standard = "IFRS_PARTIAL"
        primary_taxonomy = "ifrs-full"
        fallback = "component_limited"
        confidence = 0.45
        usable_xbrl = 1
        reason = "partial_ifrs_xbrl_missing_core_revenue_or_assets"
    elif has_partial_xbrl and taxonomies.get("sec-text", 0) > 0 and override is not None and override.reporting_profile == "RECENT_IPO_DEVELOPMENT_STAGE":
        profile = "RECENT_IPO_DEVELOPMENT_STAGE"
        standard = "sec_archive_text_table_partial"
        primary_taxonomy = "sec-text"
        fallback = "text_table_partial_lifecycle_limited"
        confidence = max(override.financial_confidence, 0.35)
        usable_xbrl = 1
        reason = override.review_reason or "recent_ipo_limited_public_filing_history"
    elif has_partial_xbrl and taxonomies.get("sec-text", 0) > 0:
        profile = "SEC_ARCHIVE_TEXT_TABLE_PARTIAL"
        standard = "sec_archive_text_table_partial"
        primary_taxonomy = "sec-text"
        fallback = "text_table_partial"
        confidence = 0.35
        usable_xbrl = 1
        reason = "text_table_partial_missing_core_revenue_or_assets"
    elif latest_form in {"20-F", "40-F", "6-K"}:
        profile = "SEC_20F_METADATA_ONLY"
        standard = "foreign_private_issuer_metadata"
        primary_taxonomy = ",".join(sorted(taxonomies))
        fallback = "neutral_low_confidence"
        confidence = 0.35
        usable_xbrl = 0
        reason = f"foreign_issuer_without_mapped_core_xbrl form={latest_form}"
    elif country_text and country_text.upper() not in {"UNITED STATES", "USA", "US"}:
        profile = "FOREIGN_NEUTRAL_LOW_CONFIDENCE"
        standard = "foreign_no_sec_xbrl"
        primary_taxonomy = ",".join(sorted(taxonomies))
        fallback = "neutral_low_confidence"
        confidence = 0.25
        usable_xbrl = 0
        reason = "foreign_issuer_no_usable_sec_xbrl"
    elif raw_archive_override or archive_attempt_override:
        profile = override.reporting_profile if override is not None and override.reporting_profile else "SEC_RAW_ARCHIVE_REQUIRED"
        standard = override.reporting_standard if override is not None else "legacy_sec_archive"
        primary_taxonomy = ",".join(sorted(taxonomies))
        fallback = override.fallback_status if override is not None else "raw_archive_required"
        confidence = override.financial_confidence if override is not None else 0.2
        usable_xbrl = 0
        reason = override.review_reason if override is not None else "legacy_or_delisted_sec_archive_required"
    elif latest is None:
        profile = "NO_FINANCIALS_REVIEW"
        standard = "unavailable"
        primary_taxonomy = ""
        fallback = "review"
        confidence = 0.0
        usable_xbrl = 0
        reason = "no_sec_filings_loaded"
    else:
        profile = "NO_FINANCIALS_REVIEW"
        standard = "sec_metadata_no_mapped_core_xbrl"
        primary_taxonomy = ",".join(sorted(taxonomies))
        fallback = "review"
        confidence = 0.2
        usable_xbrl = 0
        reason = "sec_filing_loaded_without_mapped_core_xbrl"

    now = utc_now()
    conn.execute(
        """
        INSERT INTO dim_issuer_reporting_profile(
            ticker, model_family, cik, country, reporting_profile, reporting_standard,
            primary_taxonomy, latest_filing_date, latest_form_type, latest_accession_number,
            fallback_status, financial_confidence, usable_xbrl_flag, source_id,
            review_reason, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker, model_family) DO UPDATE SET
            cik = excluded.cik,
            country = excluded.country,
            reporting_profile = excluded.reporting_profile,
            reporting_standard = excluded.reporting_standard,
            primary_taxonomy = excluded.primary_taxonomy,
            latest_filing_date = excluded.latest_filing_date,
            latest_form_type = excluded.latest_form_type,
            latest_accession_number = excluded.latest_accession_number,
            fallback_status = excluded.fallback_status,
            financial_confidence = excluded.financial_confidence,
            usable_xbrl_flag = excluded.usable_xbrl_flag,
            source_id = excluded.source_id,
            review_reason = excluded.review_reason,
            updated_at = excluded.updated_at
        """,
        (
            ticker,
            model_family,
            cik,
            country_text,
            profile,
            standard,
            primary_taxonomy,
            latest_filing,
            latest_form,
            latest_accession,
            fallback,
            confidence,
            usable_xbrl,
            source_id,
            reason,
            now,
            now,
        ),
    )
    return {
        "reporting_profile": profile,
        "reporting_standard": standard,
        "latest_filing_date": latest_filing,
        "latest_form_type": latest_form,
        "financial_confidence": confidence,
        "review_reason": reason,
    }


def write_report(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REPORT_FIELDS, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def source_status(conn: Any, source_id: str) -> str:
    row = conn.execute("SELECT status FROM source_registry WHERE source_id = ?", (source_id,)).fetchone()
    return str(row["status"]) if row is not None else ""


def start_ingestion_run(conn: Any, *, source_id: str) -> int:
    now = utc_now()
    with conn:
        cur = conn.execute(
            """
            INSERT INTO ingestion_runs(source_id, started_at, status, created_at)
            VALUES (?, ?, 'running', ?)
            """,
            (source_id, now, now),
        )
    if cur.lastrowid is None:
        raise RuntimeError(f"Failed to create ingestion run for {source_id}")
    return int(cur.lastrowid)


def finish_ingestion_run(conn: Any, *, ingestion_run_id: int, status: str, request_count: int, row_count: int, message: str) -> None:
    with conn:
        conn.execute(
            """
            UPDATE ingestion_runs
            SET completed_at = ?, status = ?, request_count = ?, row_count = ?, message = ?
            WHERE ingestion_run_id = ?
            """,
            (utc_now(), status, int(request_count), int(row_count), str(message or ""), int(ingestion_run_id)),
        )


def purge_stale_cik_artifacts(
    conn: Any,
    *,
    ticker: str,
    cik: str,
    submissions_source_id: str,
    companyfacts_source_id: str,
    model_family: str,
) -> None:
    if not cik:
        return
    conn.execute(
        """
        DELETE FROM fact_sec_xbrl_fact
        WHERE ticker = ?
          AND source_id = ?
          AND COALESCE(cik, '') <> ?
        """,
        (ticker, companyfacts_source_id, cik),
    )
    conn.execute(
        """
        DELETE FROM fact_sec_xbrl_fact_raw
        WHERE ticker = ?
          AND source_id = ?
          AND COALESCE(cik, '') <> ?
        """,
        (ticker, companyfacts_source_id, cik),
    )
    conn.execute(
        """
        DELETE FROM fact_sec_filing
        WHERE ticker = ?
          AND source_id IN (?, ?)
          AND COALESCE(cik, '') <> ?
        """,
        (ticker, submissions_source_id, companyfacts_source_id, cik),
    )
    conn.execute(
        """
        DELETE FROM dim_issuer_reporting_profile
        WHERE ticker = ?
          AND model_family = ?
          AND COALESCE(cik, '') NOT IN ('', ?)
        """,
        (ticker, model_family, cik),
    )


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    model_family = str(args.model_family or cfg_get(config, "industrials_universe.initial_subsector", "defense") or "defense").strip()
    submissions_source_id = str(cfg_get(config, "sec_fundamentals.submissions_source_id", "sec_submissions") or "sec_submissions")
    companyfacts_source_id = str(cfg_get(config, "sec_fundamentals.companyfacts_source_id", "sec_companyfacts") or "sec_companyfacts")
    submissions_template = str(cfg_get(config, "sec_fundamentals.submissions_url_template") or "https://data.sec.gov/submissions/CIK{cik}.json")
    companyfacts_template = str(cfg_get(config, "sec_fundamentals.companyfacts_url_template") or "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")
    user_agent = str(cfg_get(config, "sec_fundamentals.user_agent", "") or "")
    if "@" not in user_agent:
        LOGGER.warning("SEC user agent should include contact information; current value=%r", user_agent)
    timeout_sec = float(cfg_get(config, "sec_fundamentals.timeout_sec", 30.0))
    max_retries = int(cfg_get(config, "sec_fundamentals.max_retries", 3))
    sleep_sec = float(cfg_get(config, "sec_fundamentals.request_sleep_sec", 0.12))
    start_date = parse_date(cfg_get(config, "sec_fundamentals.start_date", "2015-01-01"))
    allowed_forms = {str(form).upper() for form in (cfg_get(config, "sec_fundamentals.forms", []) or [])}
    cache_dir = resolve_path(cfg_get(config, "sec_fundamentals.cache_dir"), base_dir=base_dir)
    archive_enabled = as_bool(cfg_get(config, "sec_archive.enabled", True))
    archive_index_template = str(
        cfg_get(config, "sec_archive.index_url_template")
        or "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_nodash}/index.json"
    )
    archive_document_template = str(
        cfg_get(config, "sec_archive.document_url_template")
        or "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_nodash}/{document_name}"
    )
    archive_submission_file_template = str(
        cfg_get(config, "sec_archive.submission_file_url_template")
        or "https://data.sec.gov/submissions/{file_name}"
    )
    archive_browse_edgar_template = str(
        cfg_get(config, "sec_archive.browse_edgar_atom_url_template")
        or "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type={form_type}&owner=exclude&count=100&output=atom"
    )
    archive_max_submission_files = int(cfg_get(config, "sec_archive.max_submission_history_files", 0) or 0)
    archive_max_filings = int(cfg_get(config, "sec_archive.max_filings_per_ticker", 0) or 0)
    archive_max_documents_raw = cfg_get(config, "sec_archive.max_documents_per_filing", 5)
    archive_max_documents = int(archive_max_documents_raw) if archive_max_documents_raw is not None else 5
    output_csv = args.output_csv.expanduser().resolve() if args.output_csv else resolve_path(cfg_get(config, "sec_fundamentals.sync_output_csv"), base_dir=base_dir)
    include_historical = bool(args.include_historical or cfg_get(config, "sec_fundamentals.include_historical_members", False))
    reporting_overrides_path_raw = str(cfg_get(config, "sec_fundamentals.reporting_overrides_csv", "") or "").strip()
    reporting_overrides_path = resolve_path(reporting_overrides_path_raw, base_dir=base_dir) if reporting_overrides_path_raw else None
    reporting_overrides = load_reporting_overrides(reporting_overrides_path)
    ticker_filter = parse_ticker_list(args.tickers)
    if args.incremental and args.force:
        raise ValueError("--incremental cannot be combined with --force; use --force-submissions, --force-companyfacts, or --force-archive.")
    force_submissions = bool(args.force or args.force_submissions or args.incremental)
    force_submission_history = bool(args.force or args.force_submissions)
    force_companyfacts = bool(args.force or args.force_companyfacts)
    force_archive = bool(args.force or args.force_archive)

    with closing(connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0)))) as conn:
        init_db(conn)
        if not args.skip_source_registry:
            registry_path = resolve_path(cfg_get(config, "source_registry.path"), base_dir=base_dir)
            with conn:
                upsert_source_registry(conn, load_source_registry(registry_path))
        if source_status(conn, submissions_source_id) != "active":
            raise ValueError(f"Source {submissions_source_id} must be active in source_registry.")
        if source_status(conn, companyfacts_source_id) != "active":
            raise ValueError(f"Source {companyfacts_source_id} must be active in source_registry.")

        tickers = load_universe(conn, model_family=model_family, ticker_filter=ticker_filter, include_historical=include_historical)
        if args.max_tickers > 0:
            tickers = tickers[: args.max_tickers]
        if not tickers:
            raise ValueError(f"No tickers found for model_family={model_family}")
        LOGGER.info(
            "SEC fundamentals sync mode: tickers=%d incremental=%s include_historical=%s force_submissions=%s force_companyfacts=%s force_archive=%s",
            len(tickers),
            bool(args.incremental),
            include_historical,
            force_submissions,
            force_companyfacts,
            force_archive,
        )

        concept_map = load_concept_map(conn)
        run_id = start_run(conn, run_type=RUN_TYPE, input_path=config_path)
        submissions_run_id = start_ingestion_run(conn, source_id=submissions_source_id)
        companyfacts_run_id = start_ingestion_run(conn, source_id=companyfacts_source_id)
        report_rows: list[dict[str, Any]] = []
        failures: list[str] = []
        submissions_requests = 0
        companyfacts_requests = 0
        try:
            with conn:
                if not args.incremental:
                    clear_stage_issues(conn, ticker_filter=ticker_filter or None)

            for item in tickers:
                ticker = normalize_ticker(item.get("ticker"))
                cik = sec_cik(item.get("cik")) if normalize_cik(item.get("cik")) else ""
                company_name = str(item.get("company_name") or "")
                country = str(item.get("country") or "")
                reporting_override = reporting_overrides.get(ticker)
                filing_count = 0
                raw_count = 0
                mapped_count = 0
                status = "success"
                review_reason = ""
                if not ticker:
                    continue
                try:
                    with conn:
                        purge_stale_cik_artifacts(
                            conn,
                            ticker=ticker,
                            cik=cik,
                            submissions_source_id=submissions_source_id,
                            companyfacts_source_id=companyfacts_source_id,
                            model_family=model_family,
                        )
                    if reporting_override is not None and reporting_override.skip_sec_network:
                        status = "review"
                        review_reason = reporting_override.review_reason or reporting_override.handling_type
                        with conn:
                            add_issue(
                                conn,
                                severity="warning",
                                ticker=ticker,
                                source_id=companyfacts_source_id,
                                issue_type="sec_reporting_override",
                                detail=f"{reporting_override.handling_type}; parent={reporting_override.parent_ticker}",
                            )
                            profile = classify_reporting_profile(
                                conn,
                                ticker=ticker,
                                cik=cik,
                                country=country,
                                model_family=model_family,
                                source_id=companyfacts_source_id,
                                override=reporting_override,
                            )
                    elif not cik:
                        status = "review"
                        review_reason = "missing_cik"
                        with conn:
                            add_issue(conn, severity="error", ticker=ticker, source_id=submissions_source_id, issue_type="missing_cik", detail="Ticker has no CIK; SEC financial sync skipped.")
                            profile = classify_reporting_profile(conn, ticker=ticker, cik="", country=country, model_family=model_family, source_id=submissions_source_id, override=reporting_override)
                    else:
                        existing_filing_keys = filing_keys(conn, ticker=ticker, source_id=submissions_source_id) if args.incremental else set()
                        existing_archive_metadata = has_filing_metadata(conn, ticker=ticker, source_id=submissions_source_id)
                        submissions_url = submissions_template.format(cik=cik)
                        submissions_cache = cache_path(cache_dir, source_id=submissions_source_id, cik=cik)
                        status_code, submissions_payload, submissions_text, _ = load_or_fetch_json(
                            submissions_url,
                            cache_file=submissions_cache,
                            force=force_submissions,
                            user_agent=user_agent,
                            timeout_sec=timeout_sec,
                            max_retries=max_retries,
                            sleep_sec=sleep_sec,
                        )
                        with conn:
                            submissions_requests += 1
                            record_raw_response(
                                conn,
                                source_id=submissions_source_id,
                                endpoint=submissions_url,
                                status=status_code,
                                payload_text=submissions_text,
                                asof_date=datetime.now(timezone.utc).date().isoformat(),
                                ingestion_run_id=submissions_run_id,
                            )
                            sec_conformed_name = str(submissions_payload.get("name") or "").strip()
                            name_score = normalized_name_similarity(company_name, sec_conformed_name)
                            if company_name and sec_conformed_name and not names_plausibly_match(company_name, sec_conformed_name):
                                status = "review"
                                review_reason = f"sec_cik_company_name_mismatch:{sec_conformed_name}"
                                add_issue(
                                    conn,
                                    severity="error",
                                    ticker=ticker,
                                    source_id=submissions_source_id,
                                    issue_type="sec_cik_company_name_mismatch",
                                    detail=f"ticker_company={company_name}; sec_name={sec_conformed_name}; score={name_score:.3f}; cik={cik}",
                                )
                                profile = classify_reporting_profile(
                                    conn,
                                    ticker=ticker,
                                    cik=cik,
                                    country=country,
                                    model_family=model_family,
                                    source_id=submissions_source_id,
                                    override=reporting_override,
                                )
                                report_rows.append(
                                    {
                                        "ticker": ticker,
                                        "cik": cik,
                                        "company_name": company_name,
                                        "country": country,
                                        "status": status,
                                        "reporting_profile": profile.get("reporting_profile", ""),
                                        "reporting_standard": profile.get("reporting_standard", ""),
                                        "latest_filing_date": profile.get("latest_filing_date", ""),
                                        "latest_form_type": profile.get("latest_form_type", ""),
                                        "filing_count": 0,
                                        "raw_fact_count": 0,
                                        "mapped_fact_count": 0,
                                        "review_reason": review_reason,
                                    }
                                )
                                continue
                            filing_count = upsert_filings(
                                conn,
                                ticker=ticker,
                                cik=cik,
                                source_id=submissions_source_id,
                                payload=submissions_payload,
                                allowed_forms=allowed_forms,
                                start_date=start_date,
                            )
                            if archive_enabled and should_attempt_archive(reporting_override) and (
                                not args.incremental or not existing_archive_metadata or force_submission_history
                            ):
                                extra_filing_count, extra_submission_requests = sync_submission_history_files(
                                    conn,
                                    ticker=ticker,
                                    cik=cik,
                                    source_id=submissions_source_id,
                                    root_payload=submissions_payload,
                                    cache_dir=cache_dir,
                                    force=force_submission_history,
                                    user_agent=user_agent,
                                    timeout_sec=timeout_sec,
                                    max_retries=max_retries,
                                    sleep_sec=sleep_sec,
                                    allowed_forms=allowed_forms,
                                    start_date=start_date,
                                    url_template=archive_submission_file_template,
                                    max_files=archive_max_submission_files,
                                    ingestion_run_id=submissions_run_id,
                                )
                                filing_count += extra_filing_count
                                submissions_requests += extra_submission_requests
                                if filing_count == 0:
                                    browse_filing_count, browse_requests = sync_browse_edgar_filings(
                                        conn,
                                        ticker=ticker,
                                        cik=cik,
                                        source_id=submissions_source_id,
                                        cache_dir=cache_dir,
                                        force=force_submission_history,
                                        user_agent=user_agent,
                                        timeout_sec=timeout_sec,
                                        max_retries=max_retries,
                                        sleep_sec=sleep_sec,
                                        allowed_forms=allowed_forms,
                                        start_date=start_date,
                                        url_template=archive_browse_edgar_template,
                                        ingestion_run_id=submissions_run_id,
                                    )
                                    filing_count += browse_filing_count
                                    submissions_requests += browse_requests

                        new_filing_keys = (
                            filing_keys(conn, ticker=ticker, source_id=submissions_source_id) - existing_filing_keys
                            if args.incremental
                            else set()
                        )
                        if (
                            args.incremental
                            and not new_filing_keys
                            and not force_companyfacts
                            and not force_archive
                            and has_existing_sec_financial_state(
                                conn,
                                ticker=ticker,
                                source_id=companyfacts_source_id,
                                override=reporting_override,
                            )
                        ):
                            with conn:
                                profile = classify_reporting_profile(
                                    conn,
                                    ticker=ticker,
                                    cik=cik,
                                    country=country,
                                    model_family=model_family,
                                    source_id=companyfacts_source_id,
                                    override=reporting_override,
                                )
                                raw_count, mapped_count = sec_fact_counts(
                                    conn,
                                    ticker=ticker,
                                    source_id=companyfacts_source_id,
                                )
                            status = "skipped_current"
                            review_reason = str(profile.get("review_reason", "") or "")
                            report_rows.append(
                                {
                                    "ticker": ticker,
                                    "cik": cik,
                                    "company_name": company_name,
                                    "country": country,
                                    "status": status,
                                    "reporting_profile": profile.get("reporting_profile", ""),
                                    "reporting_standard": profile.get("reporting_standard", ""),
                                    "latest_filing_date": profile.get("latest_filing_date", ""),
                                    "latest_form_type": profile.get("latest_form_type", ""),
                                    "filing_count": filing_count,
                                    "raw_fact_count": raw_count,
                                    "mapped_fact_count": mapped_count,
                                    "review_reason": review_reason,
                                }
                            )
                            continue

                        if args.incremental:
                            with conn:
                                clear_stage_issues(conn, ticker_filter=[ticker])

                        companyfacts_url = companyfacts_template.format(cik=cik)
                        companyfacts_cache = cache_path(cache_dir, source_id=companyfacts_source_id, cik=cik)
                        status_code, companyfacts_payload, companyfacts_text, _ = load_or_fetch_json(
                            companyfacts_url,
                            cache_file=companyfacts_cache,
                            force=force_companyfacts,
                            user_agent=user_agent,
                            timeout_sec=timeout_sec,
                            max_retries=max_retries,
                            sleep_sec=sleep_sec,
                        )
                        with conn:
                            companyfacts_requests += 1
                            record_raw_response(
                                conn,
                                source_id=companyfacts_source_id,
                                endpoint=companyfacts_url,
                                status=status_code,
                                payload_text=companyfacts_text,
                                asof_date=datetime.now(timezone.utc).date().isoformat(),
                                ingestion_run_id=companyfacts_run_id,
                            )
                            raw_count, mapped_count = upsert_companyfacts(
                                conn,
                                ticker=ticker,
                                cik=cik,
                                source_id=companyfacts_source_id,
                                payload=companyfacts_payload,
                                concept_map=concept_map,
                                start_date=start_date,
                            )
                            if archive_enabled and should_attempt_archive(reporting_override):
                                archive_raw_count, archive_mapped_count, archive_requests = sync_archive_xbrl(
                                    conn,
                                    ticker=ticker,
                                    cik=cik,
                                    source_id=companyfacts_source_id,
                                    submissions_source_id=submissions_source_id,
                                    model_family=model_family,
                                    cache_dir=cache_dir,
                                    force=force_archive,
                                    user_agent=user_agent,
                                    timeout_sec=timeout_sec,
                                    max_retries=max_retries,
                                    sleep_sec=sleep_sec,
                                    concept_map=concept_map,
                                    start_date=start_date,
                                    index_url_template=archive_index_template,
                                    document_url_template=archive_document_template,
                                    max_filings=archive_max_filings,
                                    max_documents=archive_max_documents,
                                    parse_all_documents=(
                                        reporting_override is not None
                                        and reporting_override.reporting_profile in FPI_HYBRID_PROFILES
                                    ),
                                    ingestion_run_id=companyfacts_run_id,
                                )
                                raw_count += archive_raw_count
                                mapped_count += archive_mapped_count
                                companyfacts_requests += archive_requests
                                if archive_requests == 0:
                                    add_issue(
                                        conn,
                                        severity="warning",
                                        ticker=ticker,
                                        source_id=companyfacts_source_id,
                                        issue_type="sec_archive_xbrl_no_filing_metadata",
                                        detail="Archive fallback could not run because SEC submissions metadata had no filing rows.",
                                    )
                                elif archive_mapped_count == 0:
                                    add_issue(
                                        conn,
                                        severity="warning",
                                        ticker=ticker,
                                        source_id=companyfacts_source_id,
                                        issue_type="sec_archive_xbrl_no_mapped_facts",
                                        detail="Archive index/documents fetched but no mapped XBRL facts were extracted.",
                                    )
                            profile = classify_reporting_profile(conn, ticker=ticker, cik=cik, country=country, model_family=model_family, source_id=companyfacts_source_id, override=reporting_override)
                            if profile["review_reason"]:
                                add_issue(
                                    conn,
                                    severity="warning",
                                    ticker=ticker,
                                    source_id=companyfacts_source_id,
                                    issue_type="financial_reporting_profile_review",
                                    detail=str(profile["review_reason"]),
                                )
                                status = "review"
                                review_reason = str(profile["review_reason"])
                        time.sleep(sleep_sec)
                except SecRequestError as exc:
                    if exc.status_code == 404:
                        status = "review"
                        endpoint_source_id = companyfacts_source_id if "/companyfacts/" in exc.url else submissions_source_id
                        endpoint_run_id = companyfacts_run_id if endpoint_source_id == companyfacts_source_id else submissions_run_id
                        review_reason = f"sec_endpoint_404:{endpoint_source_id}"
                        with conn:
                            record_raw_response(
                                conn,
                                source_id=endpoint_source_id,
                                endpoint=exc.url,
                                status=exc.status_code,
                                payload_text=exc.body,
                                asof_date=datetime.now(timezone.utc).date().isoformat(),
                                ingestion_run_id=endpoint_run_id,
                            )
                            add_issue(
                                conn,
                                severity="warning",
                                ticker=ticker,
                                source_id=endpoint_source_id,
                                issue_type="sec_endpoint_not_available",
                                detail=review_reason,
                            )
                            if (
                                archive_enabled
                                and endpoint_source_id == companyfacts_source_id
                                and cik
                                and should_attempt_archive(reporting_override)
                            ):
                                try:
                                    archive_raw_count, archive_mapped_count, archive_requests = sync_archive_xbrl(
                                        conn,
                                        ticker=ticker,
                                        cik=cik,
                                        source_id=companyfacts_source_id,
                                        submissions_source_id=submissions_source_id,
                                        model_family=model_family,
                                        cache_dir=cache_dir,
                                        force=force_archive,
                                        user_agent=user_agent,
                                        timeout_sec=timeout_sec,
                                        max_retries=max_retries,
                                        sleep_sec=sleep_sec,
                                        concept_map=concept_map,
                                        start_date=start_date,
                                        index_url_template=archive_index_template,
                                        document_url_template=archive_document_template,
                                        max_filings=archive_max_filings,
                                        max_documents=archive_max_documents,
                                        parse_all_documents=(
                                            reporting_override is not None
                                            and reporting_override.reporting_profile in FPI_HYBRID_PROFILES
                                        ),
                                        ingestion_run_id=companyfacts_run_id,
                                    )
                                    raw_count += archive_raw_count
                                    mapped_count += archive_mapped_count
                                    companyfacts_requests += archive_requests
                                    if archive_mapped_count > 0:
                                        resolve_open_issue(
                                            conn,
                                            ticker=ticker,
                                            source_id=endpoint_source_id,
                                            issue_type="sec_endpoint_not_available",
                                            detail=review_reason,
                                            resolution_status="resolved_by_archive_fallback",
                                        )
                                        review_reason = ""
                                    elif archive_requests == 0:
                                        add_issue(
                                            conn,
                                            severity="warning",
                                            ticker=ticker,
                                            source_id=companyfacts_source_id,
                                            issue_type="sec_archive_xbrl_no_filing_metadata",
                                            detail="CompanyFacts 404 and archive fallback could not run because SEC submissions metadata had no filing rows.",
                                        )
                                    else:
                                        add_issue(
                                            conn,
                                            severity="warning",
                                            ticker=ticker,
                                            source_id=companyfacts_source_id,
                                            issue_type="sec_archive_xbrl_no_mapped_facts",
                                            detail="CompanyFacts 404; archive documents did not produce mapped facts.",
                                        )
                                except SecRequestError as archive_exc:
                                    add_issue(
                                        conn,
                                        severity="warning",
                                        ticker=ticker,
                                        source_id=companyfacts_source_id,
                                        issue_type="sec_archive_xbrl_unavailable",
                                        detail=f"CompanyFacts 404 and archive fallback failed: status={archive_exc.status_code} url={archive_exc.url}",
                                    )
                            profile = classify_reporting_profile(
                                conn,
                                ticker=ticker,
                                cik=cik,
                                country=country,
                                model_family=model_family,
                                source_id=endpoint_source_id,
                                override=reporting_override,
                            )
                    else:
                        status = "failed"
                        review_reason = f"{type(exc).__name__}: {exc}"
                        failures.append(f"{ticker}: {review_reason}")
                        with conn:
                            add_issue(conn, severity="error", ticker=ticker, source_id=companyfacts_source_id, issue_type="sec_sync_failed", detail=review_reason)
                            profile = classify_reporting_profile(conn, ticker=ticker, cik=cik, country=country, model_family=model_family, source_id=companyfacts_source_id, override=reporting_override)
                        if not args.allow_partial:
                            raise
                except Exception as exc:
                    status = "failed"
                    review_reason = f"{type(exc).__name__}: {exc}"
                    failures.append(f"{ticker}: {review_reason}")
                    with conn:
                        add_issue(conn, severity="error", ticker=ticker, source_id=companyfacts_source_id, issue_type="sec_sync_failed", detail=review_reason)
                        profile = classify_reporting_profile(conn, ticker=ticker, cik=cik, country=country, model_family=model_family, source_id=companyfacts_source_id, override=reporting_override)
                    if not args.allow_partial:
                        raise

                report_rows.append(
                    {
                        "ticker": ticker,
                        "cik": cik,
                        "company_name": company_name,
                        "country": country,
                        "status": status,
                        "reporting_profile": profile.get("reporting_profile", ""),
                        "reporting_standard": profile.get("reporting_standard", ""),
                        "latest_filing_date": profile.get("latest_filing_date", ""),
                        "latest_form_type": profile.get("latest_form_type", ""),
                        "filing_count": filing_count,
                        "raw_fact_count": raw_count,
                        "mapped_fact_count": mapped_count,
                        "review_reason": review_reason or profile.get("review_reason", ""),
                    }
                )

            write_report(output_csv, report_rows)
            status = "success_with_failures" if failures else "success"
            if failures and not args.allow_partial:
                status = "failed"
            finish_run(conn, run_id=run_id, status=status, row_count=len(report_rows), message=f"rows={len(report_rows)} failures={len(failures)} output={output_csv}")
            finish_ingestion_run(
                conn,
                ingestion_run_id=submissions_run_id,
                status=status,
                request_count=submissions_requests,
                row_count=sum(int(row.get("filing_count") or 0) for row in report_rows),
                message=f"tickers={len(report_rows)}",
            )
            finish_ingestion_run(
                conn,
                ingestion_run_id=companyfacts_run_id,
                status=status,
                request_count=companyfacts_requests,
                row_count=sum(int(row.get("mapped_fact_count") or 0) for row in report_rows),
                message=f"tickers={len(report_rows)}",
            )
            LOGGER.info("Wrote SEC fundamentals coverage report: %s", output_csv)
            LOGGER.info("SEC fundamentals sync complete: rows=%d failures=%d", len(report_rows), len(failures))
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=len(report_rows), message=f"{type(exc).__name__}: {exc}")
            finish_ingestion_run(
                conn,
                ingestion_run_id=submissions_run_id,
                status="failed",
                request_count=submissions_requests,
                row_count=0,
                message=f"{type(exc).__name__}: {exc}",
            )
            finish_ingestion_run(
                conn,
                ingestion_run_id=companyfacts_run_id,
                status="failed",
                request_count=companyfacts_requests,
                row_count=0,
                message=f"{type(exc).__name__}: {exc}",
            )
            raise


if __name__ == "__main__":
    main()
