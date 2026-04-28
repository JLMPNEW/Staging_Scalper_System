#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, normalize_string_list, resolve_path
from biotech_index.core.db import connect, finish_run, init_db, start_run, utc_now
from biotech_index.core.http_cache import CachedHttpClient, HostThrottle


LOGGER = logging.getLogger("sync_sec_filings")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
OUTPUT_FIELDS = [
    "ticker",
    "company_name",
    "cik",
    "accession_nodash",
    "form",
    "filing_date",
    "primary_document",
    "archive_url",
    "document_url",
    "text_hash",
    "text_length",
]


@dataclass(frozen=True)
class Company:
    company_id: int
    ticker: str
    company_name: str
    cik: str


@dataclass(frozen=True)
class Filing:
    company_id: int
    ticker: str
    company_name: str
    cik: str
    accession_nodash: str
    form: str
    filing_date: str
    primary_document: str
    archive_url: str


@dataclass(frozen=True)
class FilingPayload:
    filing: Filing
    document_url: str
    document_type: str
    text: str
    text_hash: str
    text_length: int


@dataclass(frozen=True)
class CompanySyncResult:
    company: Company
    filings: list[FilingPayload]
    text_errors: int = 0
    error: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync SEC filings and filing text for Tier-1 biotech event parsing.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", type=str, default="", help="Optional YYYY-MM-DD upper bound for SEC filing_date. Defaults to UTC today.")
    parser.add_argument("--max-companies", type=int, default=0, help="Limit companies for smoke tests. 0 means all.")
    parser.add_argument("--tickers", type=str, default="", help="Optional comma-separated ticker subset.")
    parser.add_argument("--skip-text", action="store_true", help="Only sync filing metadata; do not fetch filing text.")
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


def parse_utc_datetime(raw: object) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_cik(raw: object) -> str:
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    return digits.zfill(10) if digits else ""


def cik_int_path(cik10: str) -> str:
    text = str(cik10 or "").strip()
    if not text:
        return ""
    if text.isdigit():
        return str(int(text))
    return text.lstrip("0") or text


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def matches_form(form: str, allowed_forms: set[str]) -> bool:
    form_u = str(form or "").strip().upper()
    if form_u in allowed_forms:
        return True
    for allowed in allowed_forms:
        if allowed.endswith("*") and form_u.startswith(allowed[:-1]):
            return True
    return False


def read_scoring_tickers(path: Path) -> set[str]:
    if not path.exists():
        LOGGER.warning("Final scoring universe CSV not found; SEC sync will run with no scoring-universe filter: %s", path)
        return set()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        tickers = {
            str(row.get("ticker") or "").strip().upper().replace(".", "-")
            for row in csv.DictReader(handle)
            if str(row.get("ticker") or "").strip()
        }
    if not tickers:
        raise ValueError(f"Final scoring universe CSV contains no tickers: {path}")
    return tickers


def load_companies(
    conn,
    *,
    scoring_tickers: set[str],
    ticker_filter: set[str],
    max_companies: int,
) -> list[Company]:
    params: list[Any] = []
    where = ["is_active = 1"]
    if scoring_tickers:
        placeholders = ",".join("?" for _ in scoring_tickers)
        where.append(f"ticker IN ({placeholders})")
        params.extend(sorted(scoring_tickers))
    if ticker_filter:
        placeholders = ",".join("?" for _ in ticker_filter)
        where.append(f"ticker IN ({placeholders})")
        params.extend(sorted(ticker_filter))
    sql = f"""
        SELECT company_id, ticker, company_name, cik
        FROM companies
        WHERE {' AND '.join(where)}
        ORDER BY ticker
    """
    if max_companies > 0:
        sql += " LIMIT ?"
        params.append(max_companies)
    rows = conn.execute(sql, params).fetchall()
    return [
        Company(
            company_id=int(row["company_id"]),
            ticker=str(row["ticker"]).upper(),
            company_name=str(row["company_name"] or ""),
            cik=normalize_cik(row["cik"]),
        )
        for row in rows
    ]


def parse_recent_filings(
    payload: dict[str, Any],
    *,
    company: Company,
    allowed_forms: set[str],
    cutoff: date,
    asof: date,
    archives_base_url: str,
    max_filings: int,
) -> list[Filing]:
    recent = payload.get("filings", {}).get("recent", {}) if isinstance(payload, dict) else {}
    if not isinstance(recent, dict):
        return []
    forms = list(recent.get("form") or [])
    accession_numbers = list(recent.get("accessionNumber") or [])
    filing_dates = list(recent.get("filingDate") or [])
    primary_documents = list(recent.get("primaryDocument") or [])
    count = min(len(forms), len(accession_numbers), len(filing_dates))
    filings: list[Filing] = []
    for idx in range(count):
        form = str(forms[idx] or "").strip().upper()
        filing_dt = parse_date(filing_dates[idx])
        accession = str(accession_numbers[idx] or "").replace("-", "").strip()
        if not form or not filing_dt or filing_dt < cutoff or filing_dt > asof or not accession:
            continue
        if not matches_form(form, allowed_forms):
            continue
        primary_document = str(primary_documents[idx] or "").strip() if idx < len(primary_documents) else ""
        archive_url = f"{archives_base_url.rstrip('/')}/{cik_int_path(company.cik)}/{accession}"
        filings.append(
            Filing(
                company_id=company.company_id,
                ticker=company.ticker,
                company_name=company.company_name,
                cik=company.cik,
                accession_nodash=accession,
                form=form,
                filing_date=filing_dt.isoformat(),
                primary_document=primary_document,
                archive_url=archive_url,
            )
        )
    filings.sort(key=lambda item: (item.filing_date, item.accession_nodash), reverse=True)
    return filings[:max_filings] if max_filings > 0 else filings


def filing_text_urls(filing: Filing) -> list[tuple[str, str]]:
    dashed_accession = f"{filing.accession_nodash[:10]}-{filing.accession_nodash[10:12]}-{filing.accession_nodash[12:]}"
    urls = [(f"{filing.archive_url}/{dashed_accession}.txt", "complete_submission_text")]
    if filing.primary_document:
        urls.append((f"{filing.archive_url}/{filing.primary_document}", "primary_document"))
    return urls


def fetch_filing_text(http: CachedHttpClient, filing: Filing, *, headers: dict[str, str], ttl_hours: float) -> tuple[str, str, str]:
    last_exc: Exception | None = None
    for url, doc_type in filing_text_urls(filing):
        try:
            text = http.fetch_text(namespace="sec_filing_text", url=url, headers=headers, ttl_hours=ttl_hours)
            if text and text.strip():
                return url, doc_type, text
            last_exc = ValueError(f"Empty response body from {url}")
        except Exception as exc:
            last_exc = exc
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"No filing text URL candidates for {filing.accession_nodash}")


def existing_document_payload(existing_doc: dict[str, Any] | None) -> tuple[str, str, str, int]:
    if not existing_doc:
        return "", "", "", 0
    return (
        str(existing_doc.get("document_url") or ""),
        str(existing_doc.get("document_type") or ""),
        str(existing_doc.get("text_hash") or ""),
        int(existing_doc.get("text_length") or 0),
    )


def existing_document_is_reusable(
    existing_doc: dict[str, Any] | None,
    *,
    text_ttl_hours: float,
    now: datetime,
) -> bool:
    doc_url, doc_type, digest, text_length = existing_document_payload(existing_doc)
    if not doc_url or not digest or text_length <= 0:
        return False
    if doc_type != "complete_submission_text":
        return False
    if text_ttl_hours < 0:
        return True
    fetched_at = parse_utc_datetime(existing_doc.get("fetched_at") if existing_doc else "")
    if fetched_at is None:
        return False
    age_seconds = (now - fetched_at).total_seconds()
    return age_seconds <= text_ttl_hours * 3600.0


def upsert_filing(
    conn,
    filing: Filing,
    *,
    doc_url: str = "",
    doc_type: str = "",
    text: str = "",
    text_hash_value: str = "",
) -> None:
    now = utc_now()
    digest = text_hash(text) if text else (text_hash_value or None)
    conn.execute(
        """
        INSERT INTO sec_filings(
            accession_nodash, company_id, form, filing_date, primary_document, archive_url, text_hash, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(accession_nodash) DO UPDATE SET
            company_id = excluded.company_id,
            form = excluded.form,
            filing_date = excluded.filing_date,
            primary_document = excluded.primary_document,
            archive_url = excluded.archive_url,
            text_hash = COALESCE(excluded.text_hash, sec_filings.text_hash),
            updated_at = excluded.updated_at
        """,
        (
            filing.accession_nodash,
            filing.company_id,
            filing.form,
            filing.filing_date,
            filing.primary_document,
            filing.archive_url,
            digest,
            now,
            now,
        ),
    )
    if doc_url and text:
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
            (filing.accession_nodash, doc_url, doc_type, text, digest, now, now, now),
        )


def load_existing_documents(
    conn,
    *,
    company_ids: list[int],
    cutoff: date,
    asof: date,
    allowed_forms: set[str],
) -> dict[str, dict[str, Any]]:
    if not company_ids:
        return {}
    company_placeholders = ",".join("?" for _ in company_ids)
    form_clause = ""
    params: list[Any] = [*company_ids, cutoff.isoformat(), asof.isoformat()]
    if allowed_forms:
        form_clause = f"AND ({' OR '.join('f.form = ?' if not form.endswith('*') else 'f.form LIKE ?' for form in sorted(allowed_forms))})"
        params.extend([form[:-1] + "%" if form.endswith("*") else form for form in sorted(allowed_forms)])
    rows = conn.execute(
        f"""
        WITH target_filings AS (
            SELECT accession_nodash
            FROM sec_filings f
            WHERE f.company_id IN ({company_placeholders})
              AND f.filing_date >= ?
              AND f.filing_date <= ?
              {form_clause}
        ),
        ranked_docs AS (
            SELECT
                d.accession_nodash,
                d.document_url,
                d.document_type,
                d.text_hash,
                d.fetched_at,
                length(d.text_content) AS text_length,
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
              AND length(d.text_content) > 0
        )
        SELECT accession_nodash, document_url, document_type, text_hash, fetched_at, text_length
        FROM ranked_docs
        WHERE doc_rank = 1
        """
        ,
        tuple(params),
    ).fetchall()
    existing: dict[str, dict[str, Any]] = {}
    for row in rows:
        accession = str(row["accession_nodash"] or "")
        if accession and accession not in existing:
            existing[accession] = {
                "document_url": str(row["document_url"] or ""),
                "document_type": str(row["document_type"] or ""),
                "text_hash": str(row["text_hash"] or ""),
                "fetched_at": str(row["fetched_at"] or ""),
                "text_length": int(row["text_length"] or 0),
            }
    return existing


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def sync_company(
    company: Company,
    *,
    submissions_template: str,
    allowed_forms: set[str],
    cutoff: date,
    asof: date,
    archives_base_url: str,
    max_filings: int,
    fetch_text: bool,
    headers: dict[str, str],
    cache_dir: Path,
    ttl_hours: float,
    text_ttl_hours: float,
    sleep_sec: float,
    timeout_sec: float,
    max_retries: int,
    throttle: HostThrottle,
    existing_documents: dict[str, dict[str, Any]],
    existing_documents_lock: Lock | None = None,
) -> CompanySyncResult:
    if not company.cik:
        return CompanySyncResult(company=company, filings=[], error="missing CIK")
    try:
        with CachedHttpClient(
            cache_dir=cache_dir,
            sleep_sec=sleep_sec,
            timeout_sec=timeout_sec,
            max_retries=max_retries,
            throttle=throttle,
        ) as http:
            payload = http.fetch_json(
                namespace="sec_submissions",
                url=submissions_template.format(cik=company.cik),
                headers=headers,
                ttl_hours=ttl_hours,
            )
            filings = parse_recent_filings(
                payload,
                company=company,
                allowed_forms=allowed_forms,
                cutoff=cutoff,
                asof=asof,
                archives_base_url=archives_base_url,
                max_filings=max_filings,
            )
            payloads: list[FilingPayload] = []
            text_errors = 0
            for filing in filings:
                doc_url = ""
                doc_type = ""
                text = ""
                digest = ""
                text_length = 0
                if existing_documents_lock is not None:
                    with existing_documents_lock:
                        existing_doc = existing_documents.get(filing.accession_nodash)
                else:
                    existing_doc = existing_documents.get(filing.accession_nodash)
                if fetch_text:
                    if existing_document_is_reusable(
                        existing_doc,
                        text_ttl_hours=text_ttl_hours,
                        now=datetime.now(timezone.utc),
                    ):
                        doc_url, doc_type, digest, text_length = existing_document_payload(existing_doc)
                    else:
                        try:
                            doc_url, doc_type, text = fetch_filing_text(
                                http,
                                filing,
                                headers=headers,
                                ttl_hours=text_ttl_hours,
                            )
                            digest = text_hash(text) if text else ""
                            text_length = len(text)
                        except Exception as exc:
                            text_errors += 1
                            doc_url, doc_type, digest, text_length = existing_document_payload(existing_doc)
                            LOGGER.warning("SEC filing text failed for %s %s: %s", company.ticker, filing.accession_nodash, exc)
                payloads.append(
                    FilingPayload(
                        filing=filing,
                        document_url=doc_url,
                        document_type=doc_type,
                        text=text,
                        text_hash=digest,
                        text_length=text_length,
                    )
                )
            return CompanySyncResult(company=company, filings=payloads, text_errors=text_errors)
    except Exception as exc:
        return CompanySyncResult(company=company, filings=[], error=f"{type(exc).__name__}: {exc}")


def persist_company_result(
    conn,
    result: CompanySyncResult,
    *,
    rows_out: list[dict[str, Any]],
    existing_documents: dict[str, dict[str, Any]],
    existing_documents_lock: Lock | None = None,
) -> None:
    csv_rows: list[dict[str, Any]] = []
    with conn:
        for payload in result.filings:
            filing = payload.filing
            upsert_filing(
                conn,
                filing,
                doc_url=payload.document_url,
                doc_type=payload.document_type,
                text=payload.text,
                text_hash_value=payload.text_hash,
            )
            if payload.document_url and payload.text_hash:
                if existing_documents_lock is not None:
                    with existing_documents_lock:
                        existing_documents[filing.accession_nodash] = {
                            "document_url": payload.document_url,
                            "document_type": payload.document_type,
                            "text_hash": payload.text_hash,
                            "text_length": payload.text_length,
                        }
                else:
                    existing_documents[filing.accession_nodash] = {
                        "document_url": payload.document_url,
                        "document_type": payload.document_type,
                        "text_hash": payload.text_hash,
                        "text_length": payload.text_length,
                    }
            csv_rows.append(
                {
                    "ticker": filing.ticker,
                    "company_name": filing.company_name,
                    "cik": filing.cik,
                    "accession_nodash": filing.accession_nodash,
                    "form": filing.form,
                    "filing_date": filing.filing_date,
                    "primary_document": filing.primary_document,
                    "archive_url": filing.archive_url,
                    "document_url": payload.document_url,
                    "text_hash": payload.text_hash,
                    "text_length": payload.text_length,
                }
            )
    rows_out.extend(csv_rows)


def main() -> None:
    configure_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent

    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    universe_csv = resolve_path(cfg_get(config, "sec_filings.final_scoring_universe_csv"), base_dir=base_dir)
    output_csv = resolve_path(cfg_get(config, "sec_filings.output_csv"), base_dir=base_dir)
    cache_dir = resolve_path(cfg_get(config, "sec_filings.cache_dir", "../output/biotech_index_cache"), base_dir=base_dir)
    submissions_template = str(cfg_get(config, "sec_filings.submissions_url_template", "https://data.sec.gov/submissions/CIK{cik}.json"))
    archives_base_url = str(cfg_get(config, "sec_filings.archives_base_url", "https://www.sec.gov/Archives/edgar/data"))
    user_agent = str(cfg_get(config, "sec_filings.user_agent", "") or "").strip()
    if not user_agent:
        raise ValueError("sec_filings.user_agent is required for SEC requests.")
    allowed_forms = {str(x).strip().upper() for x in normalize_string_list(cfg_get(config, "sec_filings.forms"), []) if str(x).strip()}
    if not allowed_forms:
        raise ValueError("sec_filings.forms must contain at least one SEC form.")
    lookback_days = int(cfg_get(config, "sec_filings.lookback_days", 730))
    asof = parse_date(args.asof) if args.asof else datetime.now(timezone.utc).date()
    if asof is None:
        raise ValueError("--asof must be a valid YYYY-MM-DD date.")
    cutoff = asof - timedelta(days=max(1, lookback_days))
    max_filings = int(cfg_get(config, "sec_filings.max_filings_per_company", 40))
    fetch_text = bool(cfg_get(config, "sec_filings.fetch_text", True)) and not bool(args.skip_text)
    max_workers = max(1, int(cfg_get(config, "sec_filings.max_workers", 1)))
    ticker_filter = {x.strip().upper().replace(".", "-") for x in args.tickers.split(",") if x.strip()}
    rows_out: list[dict[str, Any]] = []

    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        scoring_tickers = read_scoring_tickers(universe_csv)
        companies = load_companies(
            conn,
            scoring_tickers=scoring_tickers,
            ticker_filter=ticker_filter,
            max_companies=int(args.max_companies),
        )
        existing_documents = load_existing_documents(
            conn,
            company_ids=[company.company_id for company in companies],
            cutoff=cutoff,
            asof=asof,
            allowed_forms=allowed_forms,
        )
        existing_documents_lock = Lock()
        LOGGER.info("Loaded %d existing SEC filing documents for resume", len(existing_documents))
        run_id = start_run(conn, run_type="sync_sec_filings", input_path=universe_csv)
        try:
            headers = {"User-Agent": user_agent, "Accept": "application/json,text/plain,*/*"}
            throttle = HostThrottle()
            sync_kwargs = {
                "submissions_template": submissions_template,
                "allowed_forms": allowed_forms,
                "cutoff": cutoff,
                "asof": asof,
                "archives_base_url": archives_base_url,
                "max_filings": max_filings,
                "fetch_text": fetch_text,
                "headers": headers,
                "cache_dir": cache_dir,
                "ttl_hours": float(cfg_get(config, "sec_filings.ttl_hours", 24.0)),
                "text_ttl_hours": float(cfg_get(config, "sec_filings.text_ttl_hours", 168.0)),
                "sleep_sec": float(cfg_get(config, "sec_filings.sleep_sec", 0.15)),
                "timeout_sec": float(cfg_get(config, "sec_filings.timeout_sec", 45.0)),
                "max_retries": int(cfg_get(config, "sec_filings.max_retries", 3)),
                "throttle": throttle,
                "existing_documents": existing_documents,
                "existing_documents_lock": existing_documents_lock,
            }
            if max_workers == 1:
                for idx, company in enumerate(companies, start=1):
                    result = sync_company(company, **sync_kwargs)
                    if result.error:
                        LOGGER.warning("[%d/%d] %s skipped: %s", idx, len(companies), company.ticker, result.error)
                    persist_company_result(
                        conn,
                        result,
                        rows_out=rows_out,
                        existing_documents=existing_documents,
                        existing_documents_lock=existing_documents_lock,
                    )
                    LOGGER.info(
                        "[%d/%d] %s filings=%d text_errors=%d",
                        idx,
                        len(companies),
                        company.ticker,
                        len(result.filings),
                        result.text_errors,
                    )
            else:
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = {
                        executor.submit(sync_company, company, **sync_kwargs): (idx, company)
                        for idx, company in enumerate(companies, start=1)
                    }
                    for done_count, future in enumerate(as_completed(futures), start=1):
                        idx, company = futures[future]
                        try:
                            result = future.result()
                        except Exception as exc:
                            result = CompanySyncResult(company=company, filings=[], error=f"{type(exc).__name__}: {exc}")
                        if result.error:
                            LOGGER.warning("[%d/%d complete=%d] %s skipped: %s", idx, len(companies), done_count, company.ticker, result.error)
                        persist_company_result(
                            conn,
                            result,
                            rows_out=rows_out,
                            existing_documents=existing_documents,
                            existing_documents_lock=existing_documents_lock,
                        )
                        LOGGER.info(
                            "[%d/%d complete=%d] %s filings=%d text_errors=%d",
                            idx,
                            len(companies),
                            done_count,
                            company.ticker,
                            len(result.filings),
                            result.text_errors,
                        )
            write_csv(output_csv, rows_out)
            finish_run(conn, run_id=run_id, status="success", row_count=len(rows_out), message=f"companies={len(companies)} output={output_csv}")
        except Exception as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise
    LOGGER.info("Synced SEC filings: companies=%d rows=%d output=%s", len(companies), len(rows_out), output_csv)


if __name__ == "__main__":
    main()
