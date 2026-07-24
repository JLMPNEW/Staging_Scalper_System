#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import (  # noqa: E402
    cfg_get,
    expand_env_vars,
    family_config,
    load_yaml,
    resolve_path,
)
from industrials.core.db import (  # noqa: E402
    connect,
    finish_run,
    init_db,
    start_run,
    utc_now,
)
from industrials.core.reports import write_csv_atomic  # noqa: E402
from industrials.transportation.disclosure_candidates import (  # noqa: E402
    ANNUAL_FORMS,
    EXTRACTION_METHOD,
    INTERIM_FORMS,
    extract_transportation_disclosure_candidates,
    upsert_transportation_disclosure_candidates,
)
from industrials.transportation.contracts import write_manifest  # noqa: E402
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
)


RUN_TYPE = "sync_transportation_specialized_disclosures"
FIELDS = [
    "ticker",
    "company_name",
    "calibration_cohort",
    "industry",
    "universe_role",
    "cik",
    "scan_mode",
    "eligible_filing_count",
    "selected_filing_count",
    "skipped_existing_filing_count",
    "fetched_document_count",
    "annual_document_count",
    "interim_document_count",
    "candidate_count",
    "accepted_candidate_count",
    "review_candidate_count",
    "accepted_metric_count",
    "candidate_metrics",
    "status",
    "status_reason",
]


class DocumentFetchError(RuntimeError):
    def __init__(self, *, url: str, status_code: int, detail: str) -> None:
        super().__init__(f"{status_code} {url}: {detail[:250]}")
        self.url = url
        self.status_code = status_code
        self.detail = detail


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch a bounded current disclosure sample and extract transportation "
            "cohort-specific SEC filing metric candidates."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", default="")
    parser.add_argument("--tickers", default="")
    parser.add_argument("--active-only", action="store_true")
    parser.add_argument(
        "--historical-backfill",
        action="store_true",
        help=(
            "Parse every annual/interim filing inside the configured historical "
            "window instead of the bounded latest-filing coverage sample."
        ),
    )
    parser.add_argument(
        "--start-date",
        default="",
        help="Historical filing-date lower bound; valid only with --historical-backfill.",
    )
    parser.add_argument(
        "--max-filings-per-ticker",
        type=int,
        default=0,
        help="Optional fail-safe cap in historical mode; 0 uses the configured cap.",
    )
    parser.add_argument(
        "--reparse-existing",
        action="store_true",
        help="Reparse documents already checkpointed for the current extraction method.",
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--no-network", action="store_true")
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--output-json", type=Path, default=None)
    return parser.parse_args()


def _ticker_filter(raw: object) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in str(raw or "").split(","):
        ticker = re.sub(r"[^A-Z0-9.-]", "", value.strip().upper())
        if ticker and ticker not in seen:
            output.append(ticker)
            seen.add(ticker)
    return output


def _cache_file(
    cache_dir: Path,
    *,
    cik: str,
    accession: str,
    document_name: str,
) -> Path:
    safe_document = re.sub(r"[^A-Za-z0-9_.-]+", "_", document_name)
    return (
        cache_dir
        / "sec_archive_xbrl"
        / f"CIK{cik}"
        / accession.replace("-", "")
        / safe_document
    )


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _fetch_document(
    *,
    url: str,
    cache_file: Path,
    force: bool,
    no_network: bool,
    user_agent: str,
    timeout_sec: float,
    max_retries: int,
    sleep_sec: float,
) -> tuple[str, str]:
    if cache_file.exists() and not force:
        return cache_file.read_text(encoding="utf-8", errors="replace"), "cache"
    if no_network:
        raise DocumentFetchError(
            url=url,
            status_code=0,
            detail=f"cache miss with --no-network: {cache_file}",
        )
    try:
        import requests  # type: ignore
    except ImportError as exc:
        raise RuntimeError("Package 'requests' is required for SEC document sync.") from exc
    last_status = 0
    last_text = ""
    for attempt in range(max(1, max_retries)):
        try:
            response = requests.get(
                url,
                headers={
                    "User-Agent": user_agent,
                    "Accept-Encoding": "gzip, deflate",
                },
                timeout=timeout_sec,
            )
        except requests.RequestException as exc:
            last_status = -1
            last_text = f"{type(exc).__name__}: {exc}"
            if attempt + 1 < max(1, max_retries):
                time.sleep(sleep_sec * (attempt + 1))
            continue
        last_status = int(response.status_code)
        last_text = response.text
        if last_status == 200:
            _write_text_atomic(cache_file, last_text)
            return last_text, "network"
        if last_status not in {429, 500, 502, 503, 504}:
            break
        time.sleep(sleep_sec * (attempt + 1))
    raise DocumentFetchError(
        url=url,
        status_code=last_status,
        detail=last_text,
    )


def _members(
    conn: Any,
    *,
    asof: str,
    active_source_id: str,
    delisted_source_id: str,
    include_historical: bool,
    ticker_filter: list[str],
) -> list[dict[str, Any]]:
    params: list[Any] = [MODEL_FAMILY]
    filter_sql = ""
    if ticker_filter:
        placeholders = ",".join("?" for _ in ticker_filter)
        filter_sql = f" AND t.ticker IN ({placeholders})"
        params.extend(ticker_filter)
    rows = [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT t.ticker, c.company_name, c.cik, t.industry,
                   t.calibration_cohort_id,
                   CASE
                     WHEN EXISTS (
                       SELECT 1 FROM dim_universe_membership AS active
                       WHERE active.ticker=t.ticker
                         AND active.model_family=t.model_family
                         AND active.membership_source_id=?
                         AND active.membership_status='active'
                         AND active.start_date<=?
                         AND COALESCE(active.end_date, '9999-12-31')>=?
                     ) THEN 'active'
                     WHEN EXISTS (
                       SELECT 1 FROM dim_universe_membership AS historical
                       WHERE historical.ticker=t.ticker
                         AND historical.model_family=t.model_family
                         AND historical.membership_source_id=?
                     ) THEN 'delisted_usable'
                     ELSE 'delisted_excluded'
                   END AS universe_role
            FROM dim_industrials_taxonomy AS t
            JOIN dim_company AS c ON c.company_id=t.company_id
            WHERE t.model_family=? {filter_sql}
            ORDER BY t.ticker
            """,
            (
                active_source_id,
                asof,
                asof,
                delisted_source_id,
                *params,
            ),
        ).fetchall()
    ]
    if include_historical:
        return rows
    return [row for row in rows if row["universe_role"] == "active"]


def _selected_filings(
    conn: Any,
    *,
    ticker: str,
    source_id: str,
    asof: str,
    annual_limit: int,
    interim_limit: int,
    start_date: str = "",
    max_total: int = 0,
    financial_6k_only: bool = False,
) -> list[dict[str, Any]]:
    start_sql = "AND filing_date>=?" if start_date else ""
    six_k_sql = (
        """
        AND (
          UPPER(form_type) NOT IN ('6-K', '6-K/A')
          OR EXISTS (
            SELECT 1
            FROM fact_sec_xbrl_fact_raw AS raw
            WHERE raw.ticker=fact_sec_filing.ticker
              AND raw.accession_number=fact_sec_filing.accession_number
          )
        )
        """
        if financial_6k_only
        else ""
    )
    params: tuple[Any, ...] = (
        (ticker, source_id, asof, start_date)
        if start_date
        else (ticker, source_id, asof)
    )
    rows = [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT accession_number, form_type, filing_date, accepted_at,
                   report_date, fiscal_year, fiscal_period, primary_document
            FROM fact_sec_filing
            WHERE ticker=? AND source_id=? AND filing_date<=?
              {start_sql}
              {six_k_sql}
              AND COALESCE(primary_document, '')<>''
            ORDER BY filing_date DESC, accession_number DESC
            """,
            params,
        ).fetchall()
    ]
    annual_rows = [
        row
        for row in rows
        if str(row.get("form_type") or "").strip().upper() in ANNUAL_FORMS
    ]
    interim_rows = [
        row
        for row in rows
        if str(row.get("form_type") or "").strip().upper() in INTERIM_FORMS
    ]
    annual = annual_rows[:annual_limit] if annual_limit > 0 else annual_rows
    interim = interim_rows[:interim_limit] if interim_limit > 0 else interim_rows
    selected = [*annual, *interim]
    if not selected and rows:
        selected = rows[:1]
    unique: dict[str, dict[str, Any]] = {}
    for row in selected:
        unique[str(row["accession_number"])] = row
    ordered = sorted(
        unique.values(),
        key=lambda row: (
            str(row.get("filing_date") or ""),
            str(row.get("accession_number") or ""),
        ),
        reverse=True,
    )
    return ordered[:max_total] if max_total > 0 else ordered


def _raw_submission_name(accession: str) -> str:
    return f"{accession}.txt" if re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession) else ""


def _fetch_member_documents(
    *,
    member: dict[str, Any],
    filings: list[dict[str, Any]],
    cache_dir: Path,
    document_template: str,
    force: bool,
    no_network: bool,
    user_agent: str,
    timeout_sec: float,
    max_retries: int,
    sleep_sec: float,
) -> dict[str, Any]:
    cik = str(member.get("cik") or "").zfill(10)
    staged: list[tuple[dict[str, Any], str, str, str, list[Any]]] = []
    reasons: list[str] = []
    fetched = 0
    annual_documents = 0
    interim_documents = 0
    for filing in filings:
        accession = str(filing["accession_number"])
        primary_document = str(filing["primary_document"])
        cik_int = str(int(cik)) if cik.strip("0") else ""
        if not cik_int:
            reasons.append("missing_cik")
            continue
        document_name = primary_document
        url = document_template.format(
            cik_int=cik_int,
            accession_nodash=accession.replace("-", ""),
            document_name=document_name,
        )
        cache_file = _cache_file(
            cache_dir,
            cik=cik,
            accession=accession,
            document_name=document_name,
        )
        try:
            text, fetch_mode = _fetch_document(
                url=url,
                cache_file=cache_file,
                force=force,
                no_network=no_network,
                user_agent=user_agent,
                timeout_sec=timeout_sec,
                max_retries=max_retries,
                sleep_sec=sleep_sec,
            )
        except DocumentFetchError as primary_error:
            raw_name = _raw_submission_name(accession)
            if not raw_name or raw_name == document_name:
                reasons.append(f"fetch_failed:{accession}:{primary_error.status_code}")
                continue
            document_name = raw_name
            url = document_template.format(
                cik_int=cik_int,
                accession_nodash=accession.replace("-", ""),
                document_name=document_name,
            )
            cache_file = _cache_file(
                cache_dir,
                cik=cik,
                accession=accession,
                document_name=document_name,
            )
            try:
                text, fetch_mode = _fetch_document(
                    url=url,
                    cache_file=cache_file,
                    force=force,
                    no_network=no_network,
                    user_agent=user_agent,
                    timeout_sec=timeout_sec,
                    max_retries=max_retries,
                    sleep_sec=sleep_sec,
                )
            except DocumentFetchError as raw_error:
                reasons.append(
                    f"fetch_failed:{accession}:{primary_error.status_code}/"
                    f"{raw_error.status_code}"
                )
                continue
        candidates = extract_transportation_disclosure_candidates(
            text,
            filing=filing,
            cohort=str(member["calibration_cohort_id"]),
            industry=str(member["industry"]),
        )
        content_hash = hashlib.sha256(
            text.encode("utf-8", errors="replace")
        ).hexdigest()
        staged.append((filing, document_name, url, content_hash, candidates))
        fetched += 1
        form = str(filing.get("form_type") or "").upper()
        annual_documents += int(form in ANNUAL_FORMS)
        interim_documents += int(form in INTERIM_FORMS)
        if fetch_mode == "network":
            # Four workers with a 0.5-second per-worker spacing remain below
            # the SEC's 10 requests/second fair-access ceiling.
            time.sleep(sleep_sec)
    return {
        "member": member,
        "cik": cik,
        "filings": filings,
        "staged": staged,
        "reasons": reasons,
        "fetched": fetched,
        "annual_documents": annual_documents,
        "interim_documents": interim_documents,
    }


def _scanned_accessions(
    conn: Any,
    *,
    ticker: str,
    source_id: str,
) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            """
            SELECT accession_number
            FROM fact_sec_metric_disclosure_document_scan
            WHERE model_family=? AND ticker=? AND source_id=?
              AND extraction_method=? AND scan_status='PARSED'
            """,
            (MODEL_FAMILY, ticker, source_id, EXTRACTION_METHOD),
        ).fetchall()
    }


def _replace_document_candidates(
    conn: Any,
    *,
    ticker: str,
    cik: str,
    source_id: str,
    filing: dict[str, Any],
    document_name: str,
    source_url: str,
    content_sha256: str,
    candidates: list[Any],
    now: str,
) -> None:
    accession = str(filing.get("accession_number") or "")
    conn.execute(
        """
        DELETE FROM fact_sec_metric_disclosure_candidate
        WHERE ticker=? AND model_family=? AND source_id=?
          AND accession_number=?
          AND extraction_method LIKE 'transportation_sec_filing_prose_v%'
        """,
        (ticker, MODEL_FAMILY, source_id, accession),
    )
    conn.execute(
        """
        DELETE FROM fact_sec_metric_disclosure_document_scan
        WHERE ticker=? AND model_family=? AND source_id=?
          AND accession_number=? AND extraction_method=?
        """,
        (ticker, MODEL_FAMILY, source_id, accession, EXTRACTION_METHOD),
    )
    upsert_transportation_disclosure_candidates(
        conn,
        ticker=ticker,
        cik=cik,
        source_id=source_id,
        filing=filing,
        document_name=document_name,
        source_url=source_url,
        content_sha256=content_sha256,
        candidates=candidates,
        now=now,
    )
    accepted = sum(
        candidate.candidate_status == "ACCEPTED" for candidate in candidates
    )
    conn.execute(
        """
        INSERT INTO fact_sec_metric_disclosure_document_scan(
            model_family, ticker, source_id, accession_number, document_name,
            form_type, filing_date, accepted_at, source_url, content_sha256,
            extraction_method, scan_status, candidate_count,
            accepted_candidate_count, review_candidate_count, scanned_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PARSED', ?, ?, ?, ?)
        ON CONFLICT(
            model_family, ticker, source_id, accession_number,
            document_name, extraction_method
        ) DO UPDATE SET
            form_type=excluded.form_type,
            filing_date=excluded.filing_date,
            accepted_at=excluded.accepted_at,
            source_url=excluded.source_url,
            content_sha256=excluded.content_sha256,
            scan_status=excluded.scan_status,
            candidate_count=excluded.candidate_count,
            accepted_candidate_count=excluded.accepted_candidate_count,
            review_candidate_count=excluded.review_candidate_count,
            scanned_at=excluded.scanned_at
        """,
        (
            MODEL_FAMILY,
            ticker,
            source_id,
            accession,
            document_name,
            str(filing.get("form_type") or ""),
            str(filing.get("filing_date") or ""),
            str(filing.get("accepted_at") or ""),
            source_url,
            content_sha256,
            EXTRACTION_METHOD,
            len(candidates),
            accepted,
            len(candidates) - accepted,
            now,
        ),
    )


def _persist_fetch_result(
    conn: Any,
    *,
    fetch_result: dict[str, Any],
    source_id: str,
    historical_mode: bool,
    dry_run: bool,
) -> tuple[dict[str, Any], str]:
    member = fetch_result["member"]
    ticker = str(member["ticker"])
    cik = str(fetch_result["cik"])
    filings = fetch_result["filings"]
    staged = fetch_result["staged"]
    reasons = list(fetch_result["reasons"])
    fetched = int(fetch_result["fetched"])
    annual_documents = int(fetch_result["annual_documents"])
    interim_documents = int(fetch_result["interim_documents"])
    eligible_count = int(fetch_result["eligible_filing_count"])
    skipped_count = int(fetch_result["skipped_existing_filing_count"])
    candidate_count = sum(len(item[4]) for item in staged)
    accepted = sum(
        candidate.candidate_status == "ACCEPTED"
        for item in staged
        for candidate in item[4]
    )
    review = candidate_count - accepted
    metrics = sorted(
        {
            candidate.metric_name
            for item in staged
            for candidate in item[4]
        }
    )
    accepted_metrics = {
        candidate.metric_name
        for item in staged
        for candidate in item[4]
        if candidate.candidate_status == "ACCEPTED"
    }
    if not filings and eligible_count == 0:
        status = "NO_FILING_METADATA"
        reasons.append("no_filing_metadata_in_selected_window")
    elif not filings and skipped_count == eligible_count:
        status = "ALREADY_SCANNED"
        reasons.append("all_eligible_filings_checkpointed")
    elif fetched < len(filings):
        status = "PARTIAL_DOCUMENT_FETCH"
        reasons.append(f"fetched_{fetched}_of_{len(filings)}_selected_filings")
    elif not staged:
        status = "DOCUMENT_FETCH_FAILED"
    elif candidate_count:
        status = "CANDIDATES_FOUND"
    else:
        status = "NO_CANDIDATE"
        reasons.append("scanned_documents_contained_no_supported_disclosure")
    failed_statuses = {"DOCUMENT_FETCH_FAILED", "PARTIAL_DOCUMENT_FETCH"}
    if not historical_mode:
        failed_statuses.add("NO_FILING_METADATA")
    failure = f"{ticker}:{status}" if status in failed_statuses else ""
    if staged and not dry_run:
        with conn:
            for filing, document_name, url, content_hash, candidates in staged:
                _replace_document_candidates(
                    conn,
                    ticker=ticker,
                    cik=cik,
                    source_id=source_id,
                    filing=filing,
                    document_name=document_name,
                    source_url=url,
                    content_sha256=content_hash,
                    candidates=candidates,
                    now=utc_now(),
                )
    return (
        {
            "ticker": ticker,
            "company_name": member["company_name"],
            "calibration_cohort": member["calibration_cohort_id"],
            "industry": member["industry"],
            "universe_role": member["universe_role"],
            "cik": cik,
            "scan_mode": (
                "historical_backfill" if historical_mode else "bounded_current"
            ),
            "eligible_filing_count": eligible_count,
            "selected_filing_count": len(filings),
            "skipped_existing_filing_count": skipped_count,
            "fetched_document_count": fetched,
            "annual_document_count": annual_documents,
            "interim_document_count": interim_documents,
            "candidate_count": candidate_count,
            "accepted_candidate_count": accepted,
            "review_candidate_count": review,
            "accepted_metric_count": len(accepted_metrics),
            "candidate_metrics": ";".join(metrics),
            "status": status,
            "status_reason": ";".join(dict.fromkeys(reasons)),
        },
        failure,
    )


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    family = family_config(config, MODEL_FAMILY)
    universe = family["universe"]
    specialized = family.get("specialized_disclosures")
    if not isinstance(specialized, dict):
        raise KeyError("model_families.transportation.specialized_disclosures is required")
    historical_mode = bool(args.historical_backfill)
    if args.start_date and not historical_mode:
        raise ValueError("--start-date requires --historical-backfill")
    if args.max_filings_per_ticker < 0:
        raise ValueError("--max-filings-per-ticker cannot be negative")
    base_dir = config_path.parent
    db_path = (
        args.db.expanduser().resolve()
        if args.db
        else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    )
    output_path = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(
            specialized[
                "historical_sync_output_csv" if historical_mode else "sync_output_csv"
            ],
            base_dir=base_dir,
        )
    )
    output_json = (
        args.output_json.expanduser().resolve()
        if args.output_json
        else resolve_path(
            specialized[
                "historical_sync_output_json"
                if historical_mode
                else "sync_output_json"
            ],
            base_dir=base_dir,
        )
    )
    cache_dir = resolve_path(
        cfg_get(config, "sec_fundamentals.cache_dir"), base_dir=base_dir
    )
    source_id = str(
        cfg_get(config, "sec_fundamentals.companyfacts_source_id", "sec_companyfacts")
    )
    submissions_source_id = str(
        cfg_get(config, "sec_fundamentals.submissions_source_id", "sec_submissions")
    )
    document_template = str(cfg_get(config, "sec_archive.document_url_template"))
    user_agent = expand_env_vars(cfg_get(config, "sec_fundamentals.user_agent", ""))
    if not args.no_network and ("@" not in user_agent or "${" in user_agent):
        raise ValueError("SEC User-Agent must resolve to a contact-bearing value")
    timeout_sec = float(cfg_get(config, "sec_fundamentals.timeout_sec", 30.0))
    max_retries = int(cfg_get(config, "sec_fundamentals.max_retries", 3))
    sleep_sec = float(cfg_get(config, "sec_fundamentals.request_sleep_sec", 0.12))
    worker_count = int(specialized.get("download_workers", 4) or 1)
    request_spacing = max(
        sleep_sec,
        float(specialized.get("parallel_request_spacing_sec", 0.5) or 0.5),
    )
    annual_limit = (
        0
        if historical_mode
        else int(specialized.get("annual_filings_per_ticker", 1) or 1)
    )
    interim_limit = (
        0
        if historical_mode
        else int(specialized.get("interim_filings_per_ticker", 1) or 1)
    )
    max_filings_per_ticker = (
        int(
            args.max_filings_per_ticker
            or specialized.get("historical_max_filings_per_ticker", 0)
            or 0
        )
        if historical_mode
        else 0
    )
    include_historical = bool(
        specialized.get("include_historical", True) and not args.active_only
    )
    asof = str(args.asof or datetime.now(timezone.utc).date().isoformat())[:10]
    try:
        date.fromisoformat(asof)
    except ValueError as exc:
        raise ValueError(f"Invalid --asof={asof!r}") from exc
    start_date = (
        str(
            args.start_date
            or specialized.get("historical_backfill_start_date")
            or universe.get("optimization_start_date")
            or ""
        )[:10]
        if historical_mode
        else ""
    )
    if historical_mode:
        try:
            parsed_start = date.fromisoformat(start_date)
        except ValueError as exc:
            raise ValueError(f"Invalid historical start date={start_date!r}") from exc
        if parsed_start > date.fromisoformat(asof):
            raise ValueError("Historical start date cannot be after --asof")
    selected_tickers = _ticker_filter(args.tickers)
    report: list[dict[str, Any]] = []
    failures: list[str] = []
    run_type = f"{RUN_TYPE}_historical_backfill" if historical_mode else RUN_TYPE

    with connect(
        db_path,
        timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 120.0)),
    ) as conn:
        init_db(conn)
        members = _members(
            conn,
            asof=asof,
            active_source_id=str(universe["seed_source_id"]),
            delisted_source_id=str(universe["historical_membership_source_id"]),
            include_historical=include_historical,
            ticker_filter=selected_tickers,
        )
        if not members:
            raise ValueError("No transportation issuers selected")
        run_id = 0 if args.dry_run else start_run(
            conn,
            run_type=run_type,
            input_path=config_path,
        )
        if not args.dry_run:
            with conn:
                conn.execute(
                    """
                    UPDATE runs
                    SET status='failed', completed_at=?,
                        message=?
                    WHERE run_type=? AND status='running' AND run_id<>?
                    """,
                    (
                        utc_now(),
                        (
                            "superseded stale run after interrupted historical "
                            "disclosure backfill"
                            if historical_mode
                            else "superseded stale run after interrupted bounded disclosure scan"
                        ),
                        run_type,
                        run_id,
                    ),
                )
        try:
            member_jobs: list[
                tuple[dict[str, Any], list[dict[str, Any]], int, int]
            ] = []
            for member in members:
                ticker = str(member["ticker"])
                eligible_filings = _selected_filings(
                    conn,
                    ticker=ticker,
                    source_id=submissions_source_id,
                    asof=asof,
                    annual_limit=annual_limit,
                    interim_limit=interim_limit,
                    start_date=start_date,
                    max_total=max_filings_per_ticker,
                    financial_6k_only=historical_mode,
                )
                filings = eligible_filings
                if historical_mode and not args.reparse_existing:
                    scanned = _scanned_accessions(
                        conn,
                        ticker=ticker,
                        source_id=source_id,
                    )
                    filings = [
                        filing
                        for filing in eligible_filings
                        if str(filing["accession_number"]) not in scanned
                    ]
                member_jobs.append(
                    (
                        member,
                        filings,
                        len(eligible_filings),
                        len(eligible_filings) - len(filings),
                    )
                )
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=max(1, min(worker_count, 4))
            ) as executor:
                futures = {
                    executor.submit(
                        _fetch_member_documents,
                        member=member,
                        filings=filings,
                        cache_dir=cache_dir,
                        document_template=document_template,
                        force=args.force,
                        no_network=args.no_network,
                        user_agent=user_agent,
                        timeout_sec=timeout_sec,
                        max_retries=max_retries,
                        sleep_sec=request_spacing,
                    ): (eligible_count, skipped_count)
                    for member, filings, eligible_count, skipped_count in member_jobs
                }
                for future in concurrent.futures.as_completed(futures):
                    eligible_count, skipped_count = futures[future]
                    result = future.result()
                    result["eligible_filing_count"] = eligible_count
                    result["skipped_existing_filing_count"] = skipped_count
                    row, failure = _persist_fetch_result(
                        conn,
                        fetch_result=result,
                        source_id=source_id,
                        historical_mode=historical_mode,
                        dry_run=bool(args.dry_run),
                    )
                    report.append(row)
                    if failure:
                        failures.append(failure)
                    if historical_mode and (
                        len(report) % 10 == 0 or len(report) == len(member_jobs)
                    ):
                        print(
                            (
                                f"historical_disclosure_progress "
                                f"issuers={len(report)}/{len(member_jobs)} "
                                f"documents={sum(int(item['fetched_document_count']) for item in report)} "
                                f"failures={len(failures)}"
                            ),
                            flush=True,
                        )
            if not args.dry_run:
                finish_run(
                    conn,
                    run_id=run_id,
                    status="success" if not failures else "partial",
                    row_count=len(report),
                    message=(
                        f"mode={'historical' if historical_mode else 'bounded'}; "
                        f"start={start_date}; asof={asof}; "
                        f"candidates={sum(int(row['candidate_count']) for row in report)}; "
                        f"failures={len(failures)}"
                    ),
                )
        except BaseException as exc:
            if not args.dry_run:
                finish_run(
                    conn,
                    run_id=run_id,
                    status="failed",
                    row_count=len(report),
                    message=f"{type(exc).__name__}: {exc}",
                )
            raise
    report.sort(key=lambda row: str(row["ticker"]))
    write_csv_atomic(output_path, FIELDS, report)
    summary = {
        "acceptance": (
            "PASS"
            if not failures
            else "PASS_WITH_REVIEW"
            if args.allow_partial
            else "FAIL"
        ),
        "scan_mode": "historical_backfill" if historical_mode else "bounded_current",
        "extraction_method": EXTRACTION_METHOD,
        "start_date": start_date,
        "asof_date": asof,
        "issuer_count": len(report),
        "eligible_filing_count": sum(
            int(row["eligible_filing_count"]) for row in report
        ),
        "processed_document_count": sum(
            int(row["fetched_document_count"]) for row in report
        ),
        "skipped_existing_filing_count": sum(
            int(row["skipped_existing_filing_count"]) for row in report
        ),
        "document_ticker_count": sum(int(row["fetched_document_count"]) > 0 for row in report),
        "candidate_ticker_count": sum(int(row["candidate_count"]) > 0 for row in report),
        "candidate_count": sum(int(row["candidate_count"]) for row in report),
        "accepted_candidate_count": sum(
            int(row["accepted_candidate_count"]) for row in report
        ),
        "review_candidate_count": sum(
            int(row["review_candidate_count"]) for row in report
        ),
        "failures": failures,
        "output_csv": str(output_path),
        "output_json": str(output_json),
        "dry_run": bool(args.dry_run),
    }
    write_manifest(output_json, summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 1 if failures and not args.allow_partial else 0


if __name__ == "__main__":
    raise SystemExit(main())
