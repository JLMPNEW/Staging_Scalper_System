from __future__ import annotations

import csv
import json
import os
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from dedicated_parser.catalog import accession_directory, relevant_document_names
from dedicated_parser.contracts import FilingRef, file_sha256


HYDRATION_VERSION = "software_infrastructure_sec_hydration_v1"
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
TEXT_DOCUMENT_SUFFIXES = frozenset(
    {".htm", ".html", ".xhtml", ".xml", ".xsd", ".txt", ".pdf"}
)
EXCLUDED_NAME_SUFFIXES = (
    "-index.html",
    "-index-headers.html",
)
EXCLUDED_NAMES = frozenset({"report.css", "show.js"})


@dataclass(frozen=True)
class HydrationFiling:
    ticker: str
    cik: str
    accession_number: str
    form_type: str
    filing_date: str
    accepted_at: str
    report_date: str
    primary_document: str
    source_id: str

    def filing_ref(self) -> FilingRef:
        return FilingRef(
            ticker=self.ticker,
            cik=self.cik,
            accession_number=self.accession_number,
            form_type=self.form_type,
            filing_date=self.filing_date,
            accepted_at=self.accepted_at,
            report_date=self.report_date,
            primary_document=self.primary_document,
            source_id=self.source_id,
        )


class RequestThrottle:
    def __init__(self, spacing_sec: float) -> None:
        self.spacing_sec = max(0.1, spacing_sec)
        self.last_request = 0.0

    def wait(self) -> None:
        wait_sec = self.last_request + self.spacing_sec - time.monotonic()
        if wait_sec > 0:
            time.sleep(wait_sec)
        self.last_request = time.monotonic()


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(payload)
        temp_path = Path(handle.name)
    os.replace(temp_path, path)


def atomic_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        raise ValueError(f"Refusing to write empty CSV: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), extrasaction="raise")
        writer.writeheader()
        writer.writerows(rows)
        temp_path = Path(handle.name)
    os.replace(temp_path, path)


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_bytes(
        path,
        (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )


def select_filings(
    conn: sqlite3.Connection,
    *,
    forms: tuple[str, ...],
    asof_date: str,
    start_date: str,
    tickers: tuple[str, ...] = (),
    accessions: tuple[str, ...] = (),
    max_filings_per_ticker: int = 8,
    max_tickers: int = 0,
) -> list[HydrationFiling]:
    params: list[Any] = ["software_infrastructure", asof_date, start_date]
    ticker_filter = ""
    if tickers:
        ticker_filter = f"AND f.ticker IN ({','.join('?' for _ in tickers)})"
        params.extend(tickers)
    accession_filter = ""
    if accessions:
        accession_filter = (
            f"AND f.accession_number IN "
            f"({','.join('?' for _ in accessions)})"
        )
        params.extend(accessions)
    params.extend(forms)
    params.extend((start_date, asof_date))
    rows = conn.execute(
        f"""
        WITH scoped AS (
            SELECT DISTINCT
                f.ticker,
                f.cik,
                f.accession_number,
                UPPER(f.form_type) AS form_type,
                COALESCE(f.filing_date, '') AS filing_date,
                COALESCE(f.acceptance_datetime, '') AS accepted_at,
                COALESCE(f.report_date, '') AS report_date,
                COALESCE(f.primary_document, '') AS primary_document,
                f.source_id,
                ROW_NUMBER() OVER (
                    PARTITION BY f.ticker
                    ORDER BY COALESCE(f.acceptance_datetime, f.filing_date) DESC,
                             f.accession_number DESC
                ) AS ticker_sequence
            FROM fact_sec_filing AS f
            WHERE EXISTS (
                SELECT 1
                FROM dim_universe_membership AS m
                WHERE m.model_family = ?
                  AND m.ticker = f.ticker
                  AND m.start_date <= ?
                  AND COALESCE(NULLIF(m.end_date, ''), '9999-12-31') >= ?
            )
              {ticker_filter}
              {accession_filter}
              AND UPPER(f.form_type) IN ({','.join('?' for _ in forms)})
              AND SUBSTR(
                    COALESCE(f.acceptance_datetime, f.filing_date), 1, 10
                  ) >= ?
              AND SUBSTR(COALESCE(f.acceptance_datetime, f.filing_date), 1, 10) <= ?
        )
        SELECT *
        FROM scoped
        WHERE (? = 0 OR ticker_sequence <= ?)
        ORDER BY ticker, ticker_sequence
        """,
        (
            *params,
            max_filings_per_ticker,
            max_filings_per_ticker,
        ),
    ).fetchall()
    selected_tickers = sorted({str(row["ticker"]) for row in rows})
    if max_tickers > 0:
        selected = set(selected_tickers[:max_tickers])
        rows = [row for row in rows if str(row["ticker"]) in selected]
    return [
        HydrationFiling(
            ticker=str(row["ticker"]).upper(),
            cik=str(row["cik"] or "").strip().zfill(10),
            accession_number=str(row["accession_number"]),
            form_type=str(row["form_type"]),
            filing_date=str(row["filing_date"]),
            accepted_at=str(row["accepted_at"]),
            report_date=str(row["report_date"]),
            primary_document=str(row["primary_document"]),
            source_id=str(row["source_id"]),
        )
        for row in rows
        if str(row["cik"] or "").strip() and str(row["accession_number"] or "").strip()
    ]


def _valid_cache(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size <= 0:
        return False
    if path.suffix.lower() == ".json":
        try:
            return isinstance(json.loads(path.read_text(encoding="utf-8")), dict)
        except (OSError, json.JSONDecodeError):
            return False
    return True


def _request(
    *,
    url: str,
    path: Path,
    user_agent: str,
    timeout_sec: float,
    max_retries: int,
    throttle: RequestThrottle,
    execute: bool,
) -> dict[str, Any]:
    if _valid_cache(path):
        return {
            "status": "CACHE_HIT_VALID",
            "http_status": 200,
            "attempt_count": 0,
            "content_bytes": path.stat().st_size,
            "content_sha256": file_sha256(path),
            "error": "",
        }
    if not execute:
        return {
            "status": "PLANNED",
            "http_status": 0,
            "attempt_count": 0,
            "content_bytes": 0,
            "content_sha256": "",
            "error": "",
        }
    last_error = ""
    for attempt in range(1, max(1, max_retries) + 1):
        try:
            throttle.wait()
            response = requests.get(
                url,
                headers={
                    "User-Agent": user_agent,
                    "Accept-Encoding": "gzip, deflate",
                    "Host": "www.sec.gov",
                },
                timeout=timeout_sec,
            )
            if response.status_code == 200 and response.content:
                _atomic_bytes(path, response.content)
                return {
                    "status": "HYDRATED",
                    "http_status": 200,
                    "attempt_count": attempt,
                    "content_bytes": len(response.content),
                    "content_sha256": file_sha256(path),
                    "error": "",
                }
            last_error = f"HTTP {response.status_code}"
            if response.status_code not in RETRYABLE_STATUS:
                break
        except requests.RequestException as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt < max_retries:
            time.sleep(min(30.0, float(2**attempt)))
    return {
        "status": "FAILED",
        "http_status": 0,
        "attempt_count": max(1, max_retries),
        "content_bytes": 0,
        "content_sha256": "",
        "error": last_error,
    }


def _archive_base(filing: HydrationFiling) -> str:
    return (
        "https://www.sec.gov/Archives/edgar/data/"
        f"{int(filing.cik)}/{filing.accession_number.replace('-', '')}"
    )


def _index_items(index_path: Path) -> list[dict[str, str]]:
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    items = ((payload.get("directory") or {}).get("item") or [])
    output: list[dict[str, str]] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        if not name or Path(name).name != name:
            continue
        output.append(
            {
                "name": name,
                "type": str(raw.get("type") or "").strip(),
                "description": str(raw.get("description") or "").strip(),
            }
        )
    return output


def _selected_documents(
    filing: HydrationFiling,
    items: list[dict[str, str]],
) -> list[str]:
    selected = {
        item["name"]
        for item in items
        if Path(item["name"]).suffix.lower() in TEXT_DOCUMENT_SUFFIXES
        and item["name"].lower() not in EXCLUDED_NAMES
        and not item["name"].lower().endswith(EXCLUDED_NAME_SUFFIXES)
    }
    if filing.primary_document:
        selected.add(filing.primary_document)
    return sorted(selected, key=lambda name: (name != filing.primary_document, name.lower()))


def hydrate_filings(
    filings: list[HydrationFiling],
    *,
    cache_dir: Path,
    output_dir: Path,
    user_agent: str,
    timeout_sec: float,
    max_retries: int,
    request_spacing_sec: float,
    execute: bool,
    max_documents_per_filing: int = 0,
) -> dict[str, Any]:
    if not filings:
        raise ValueError("No filings selected for software parser hydration")
    output_dir.mkdir(parents=True, exist_ok=True)
    lock_path = output_dir / ".software_parser_hydration.lock"
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise RuntimeError(f"Hydration is already running: {lock_path}") from exc
    os.close(descriptor)
    throttle = RequestThrottle(request_spacing_sec)
    results: list[dict[str, Any]] = []
    filing_status: dict[tuple[str, str], str] = {}
    try:
        for filing in filings:
            filing_ref = filing.filing_ref()
            directory = accession_directory(cache_dir, filing_ref)
            index_path = directory / "index.json"
            index_result = _request(
                url=f"{_archive_base(filing)}/index.json",
                path=index_path,
                user_agent=user_agent,
                timeout_sec=timeout_sec,
                max_retries=max_retries,
                throttle=throttle,
                execute=execute,
            )
            results.append(
                {
                    "hydration_version": HYDRATION_VERSION,
                    "ticker": filing.ticker,
                    "cik": filing.cik,
                    "accession_number": filing.accession_number,
                    "form_type": filing.form_type,
                    "filing_date": filing.filing_date,
                    "document_name": "index.json",
                    "document_role": "accession_index",
                    "url": f"{_archive_base(filing)}/index.json",
                    "cache_path": str(index_path.resolve()),
                    **index_result,
                }
            )
            if index_result["status"] not in {"HYDRATED", "CACHE_HIT_VALID"}:
                filing_status[(filing.ticker, filing.accession_number)] = "INDEX_INCOMPLETE"
                continue
            items = _index_items(index_path)
            names = _selected_documents(filing, items)
            if max_documents_per_filing > 0:
                names = names[:max_documents_per_filing]
            full_submission = f"{filing.accession_number}.txt"
            if full_submission not in names:
                names.append(full_submission)
            complete = True
            for name in names:
                role = (
                    "primary_document"
                    if name == filing.primary_document
                    else "full_submission"
                    if name == full_submission
                    else "accession_support_or_exhibit"
                )
                path = directory / name
                result = _request(
                    url=f"{_archive_base(filing)}/{quote(name)}",
                    path=path,
                    user_agent=user_agent,
                    timeout_sec=timeout_sec,
                    max_retries=max_retries,
                    throttle=throttle,
                    execute=execute,
                )
                complete = complete and result["status"] in {
                    "HYDRATED",
                    "CACHE_HIT_VALID",
                }
                results.append(
                    {
                        "hydration_version": HYDRATION_VERSION,
                        "ticker": filing.ticker,
                        "cik": filing.cik,
                        "accession_number": filing.accession_number,
                        "form_type": filing.form_type,
                        "filing_date": filing.filing_date,
                        "document_name": name,
                        "document_role": role,
                        "url": f"{_archive_base(filing)}/{quote(name)}",
                        "cache_path": str(path.resolve()),
                        **result,
                    }
                )
            filing_status[(filing.ticker, filing.accession_number)] = (
                "COMPLETE" if complete else "DOCUMENT_INCOMPLETE"
            )
            atomic_csv(output_dir / "software_parser_hydration_results.csv", results)
        keywords = (
            "annual recurring revenue",
            "arr",
            "billings",
            "customer",
            "deferred revenue",
            "net retention",
            "performance obligation",
            "remaining performance",
            "subscription revenue",
        )
        sealed: list[dict[str, Any]] = []
        for filing in filings:
            if filing_status.get((filing.ticker, filing.accession_number)) != "COMPLETE":
                continue
            directory = accession_directory(cache_dir, filing.filing_ref())
            for name in relevant_document_names(
                directory,
                filing=filing.filing_ref(),
                keywords=keywords,
            ):
                path = directory / name
                sealed.append(
                    {
                        "ticker": filing.ticker,
                        "accession_number": filing.accession_number,
                        "document_name": name,
                        "content_sha256": file_sha256(path),
                        "cache_status": "CACHED_HASHED",
                        "cik": filing.cik,
                        "form_type": filing.form_type,
                        "filing_date": filing.filing_date,
                        "source_path": str(path.resolve()),
                    }
                )
        results_path = output_dir / "software_parser_hydration_results.csv"
        if results and not results_path.exists():
            atomic_csv(results_path, results)
        sealed_path = output_dir / "software_parser_hydrated_source_manifest.csv"
        if sealed:
            atomic_csv(sealed_path, sealed)
        complete_count = sum(status == "COMPLETE" for status in filing_status.values())
        manifest = {
            "manifest_version": "software_parser_hydration_manifest_v1",
            "hydration_version": HYDRATION_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "execution_mode": "execute" if execute else "dry_run",
            "model_family": "software_infrastructure",
            "selected_filing_count": len(filings),
            "complete_filing_count": complete_count,
            "incomplete_filing_count": len(filings) - complete_count,
            "request_row_count": len(results),
            "sealed_document_count": len(sealed),
            "results_path": str(results_path.resolve()),
            "results_sha256": file_sha256(results_path) if results_path.exists() else "",
            "sealed_source_manifest_path": str(sealed_path.resolve()) if sealed else "",
            "sealed_source_manifest_sha256": file_sha256(sealed_path) if sealed else "",
            "parser_execution_allowed_flag": int(
                complete_count == len(filings) and bool(sealed)
            ),
            "production_facts_modified_flag": 0,
            "production_scores_modified_flag": 0,
        }
        atomic_json(output_dir / "software_parser_hydration_manifest.json", manifest)
        return manifest
    finally:
        lock_path.unlink(missing_ok=True)
