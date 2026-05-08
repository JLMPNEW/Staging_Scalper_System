#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import logging
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.clients.ctgov_client import CtgovClient, parse_sponsors, parse_study
from biotech_index.core.config import cfg_get, load_yaml, normalize_string_list, resolve_optional_path, resolve_path
from biotech_index.core.db import connect, finish_run, init_db, start_run, utc_now
from biotech_index.core.http_cache import CachedHttpClient, HostThrottle
from biotech_index.core.logging_utils import configure_utc_logging
from biotech_index.core.pipeline_guards import validate_nonempty_selection, validate_requested_tickers


LOGGER = logging.getLogger("sync_ctgov_trials")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
SQLITE_PARAM_CHUNK_SIZE = 800


def chunked(values: list[Any] | tuple[Any, ...], size: int = SQLITE_PARAM_CHUNK_SIZE) -> list[list[Any]]:
    step = max(1, int(size))
    return [list(values[start : start + step]) for start in range(0, len(values), step)]


@dataclass(frozen=True)
class CompanyJob:
    company_id: int
    ticker: str
    company_name: str
    aliases: tuple[str, ...]
    searches: tuple["SearchTerm", ...]


@dataclass(frozen=True)
class SearchTerm:
    search_term: str
    query_fields: tuple[str, ...]
    source: str
    confidence: float
    link_from_search: bool = False


@dataclass(frozen=True)
class QueryHit:
    company_id: int
    nct_id: str
    search_term: str
    query_field: str
    source: str
    confidence: float


@dataclass(frozen=True)
class SyncResult:
    company_id: int
    ticker: str
    alias_count: int
    search_count: int
    study_count: int
    studies: dict[str, dict[str, Any]]
    query_hits: tuple[QueryHit, ...] = ()
    error: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync ClinicalTrials.gov interventional studies for active biotech companies.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None, help="Override SQLite database path.")
    parser.add_argument("--asof", type=str, default="", help="Snapshot date in YYYY-MM-DD. Defaults to UTC today.")
    parser.add_argument("--max-workers", type=int, default=None)
    parser.add_argument("--max-companies", type=int, default=0, help="Limit companies for smoke tests. 0 means all.")
    parser.add_argument("--tickers", type=str, default="", help="Optional comma-separated ticker subset.")
    parser.add_argument("--allow-partial", action="store_true", help="Return success even if one or more company syncs fail.")
    return parser.parse_args()


def configure_logging() -> None:
    configure_utc_logging()


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def load_company_jobs(
    conn: sqlite3.Connection,
    *,
    status_filter: set[str],
    min_alias_length: int,
    max_aliases_per_company: int,
    query_fields: list[str],
    search_overrides: dict[str, list[SearchTerm]],
    ticker_filter: set[str],
    max_companies: int,
) -> list[CompanyJob]:
    sql = """
        SELECT company_id, ticker, company_name, universe_status
        FROM companies
        WHERE is_active = 1
        ORDER BY ticker
    """
    jobs: list[CompanyJob] = []
    for row in conn.execute(sql):
        status = str(row["universe_status"] or "").lower()
        ticker = str(row["ticker"] or "").upper()
        if status_filter and status not in status_filter:
            continue
        if ticker_filter and ticker not in ticker_filter:
            continue
        aliases = load_company_aliases(
            conn,
            company_id=int(row["company_id"]),
            fallback_name=str(row["company_name"] or ""),
            min_alias_length=min_alias_length,
            max_aliases=max_aliases_per_company,
        )
        searches = build_company_searches(
            aliases=aliases,
            query_fields=query_fields,
            overrides=search_overrides.get(ticker, []),
        )
        jobs.append(
            CompanyJob(
                company_id=int(row["company_id"]),
                ticker=ticker,
                company_name=str(row["company_name"] or ""),
                aliases=tuple(aliases),
                searches=tuple(searches),
            )
        )
        if max_companies > 0 and len(jobs) >= max_companies:
            break
    return jobs


def load_company_aliases(
    conn: sqlite3.Connection,
    *,
    company_id: int,
    fallback_name: str,
    min_alias_length: int,
    max_aliases: int,
) -> list[str]:
    rows = conn.execute(
        """
        SELECT alias_raw, source, confidence, is_manual
        FROM company_aliases
        WHERE company_id = ?
        ORDER BY is_manual DESC, confidence DESC, source ASC, LENGTH(alias_raw) DESC
        """,
        (company_id,),
    ).fetchall()
    aliases: list[str] = []
    seen: set[str] = set()
    for row in rows:
        source = str(row["source"] or "").strip().lower()
        alias = str(row["alias_raw"] or "").strip()
        if source == "ticker":
            continue
        if source == "core_tokens" and len(alias.split()) == 1:
            continue
        if len(alias) < min_alias_length:
            continue
        key = alias.upper()
        if key in seen:
            continue
        seen.add(key)
        aliases.append(alias)
        if max_aliases > 0 and len(aliases) >= max_aliases:
            break
    if not aliases and len(fallback_name.strip()) >= min_alias_length:
        aliases.append(fallback_name.strip())
    return aliases


def split_query_fields(raw: str, default_fields: list[str]) -> tuple[str, ...]:
    fields = [part.strip() for part in str(raw or "").replace(";", ",").split(",") if part.strip()]
    return tuple(fields or default_fields)


def parse_bool(raw: object, *, default: bool = False) -> bool:
    text = str(raw or "").strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "y"}


def row_get(row: dict[str, str], *keys: str) -> str:
    lowered = {str(k).strip().lower(): str(v or "") for k, v in row.items()}
    for key in keys:
        raw = row.get(key)
        if raw is not None and str(raw).strip():
            return str(raw).strip()
        raw = lowered.get(key.lower())
        if raw is not None and str(raw).strip():
            return str(raw).strip()
    return ""


def load_ctgov_search_overrides(path: Path | None, *, default_query_fields: list[str]) -> dict[str, list[SearchTerm]]:
    if path is None or not path.exists():
        return {}
    out: dict[str, list[SearchTerm]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"CTGov search overrides CSV has no header: {path}")
        for row_raw in reader:
            row = {str(k): str(v or "") for k, v in row_raw.items()}
            ticker = row_get(row, "ticker", "Ticker", "Tickers").upper().replace(".", "-")
            search_term = row_get(row, "search_term", "SearchTerm", "alias", "Alias")
            if not ticker or not search_term:
                continue
            try:
                confidence = float(row_get(row, "confidence", "Confidence") or "0.75")
            except ValueError:
                confidence = 0.75
            query_fields = split_query_fields(row_get(row, "query_field", "query_fields", "QueryField"), default_query_fields)
            out.setdefault(ticker, []).append(
                SearchTerm(
                    search_term=search_term,
                    query_fields=query_fields,
                    source=row_get(row, "source", "Source") or "manual_ctgov_search",
                    confidence=confidence,
                    link_from_search=parse_bool(row_get(row, "link_from_search", "LinkFromSearch"), default=True),
                )
            )
    return out


def build_company_searches(
    *,
    aliases: list[str],
    query_fields: list[str],
    overrides: list[SearchTerm],
) -> list[SearchTerm]:
    searches_by_key: dict[tuple[str, tuple[str, ...]], SearchTerm] = {}
    for alias in aliases:
        term = str(alias or "").strip()
        if not term:
            continue
        key = (term.upper(), tuple(query_fields))
        if key in searches_by_key:
            continue
        searches_by_key[key] = SearchTerm(
            search_term=term,
            query_fields=tuple(query_fields),
            source="company_alias",
            confidence=0.0,
            link_from_search=False,
        )
    for override in overrides:
        term = str(override.search_term or "").strip()
        fields = tuple(field for field in override.query_fields if str(field).strip())
        if not term or not fields:
            continue
        key = (term.upper(), fields)
        existing = searches_by_key.get(key)
        if existing is None:
            searches_by_key[key] = override
            continue
        if override.link_from_search and not existing.link_from_search:
            searches_by_key[key] = SearchTerm(
                search_term=term,
                query_fields=fields,
                source=override.source,
                confidence=override.confidence,
                link_from_search=True,
            )
    return list(searches_by_key.values())


def sync_one_company(
    job: CompanyJob,
    *,
    cache_dir: Path,
    studies_url: str,
    query_fields: list[str],
    page_size: int,
    max_pages: int,
    ttl_hours: float,
    sleep_sec: float,
    timeout_sec: float,
    max_retries: int,
    throttle: HostThrottle,
) -> SyncResult:
    try:
        studies: dict[str, dict[str, Any]] = {}
        query_hits: list[QueryHit] = []
        with CachedHttpClient(
            cache_dir=cache_dir,
            sleep_sec=sleep_sec,
            timeout_sec=timeout_sec,
            max_retries=max_retries,
            throttle=throttle,
        ) as http:
            client = CtgovClient(
                http=http,
                studies_url=studies_url,
                page_size=page_size,
                max_pages=max_pages,
                ttl_hours=ttl_hours,
            )
            for search in job.searches:
                for query_field in search.query_fields:
                    found = client.search_studies(
                        alias=search.search_term,
                        query_fields=[query_field],
                        interventional_only=True,
                    )
                    studies.update(found)
                    if search.link_from_search:
                        query_hits.extend(
                            QueryHit(
                                company_id=job.company_id,
                                nct_id=nct_id,
                                search_term=search.search_term,
                                query_field=query_field,
                                source=search.source,
                                confidence=search.confidence,
                            )
                            for nct_id in found
                        )
        return SyncResult(
            company_id=job.company_id,
            ticker=job.ticker,
            alias_count=len(job.aliases),
            search_count=len(job.searches),
            study_count=len(studies),
            studies=studies,
            query_hits=tuple(query_hits),
        )
    except Exception as exc:
        LOGGER.exception("CTGov sync failed for %s (%s): %s", job.ticker, job.company_name, exc)
        return SyncResult(
            company_id=job.company_id,
            ticker=job.ticker,
            alias_count=len(job.aliases),
            search_count=len(job.searches),
            study_count=0,
            studies={},
            query_hits=(),
            error=f"{type(exc).__name__}: {exc}",
        )


def dedupe_sponsors(sponsors: Iterable[Any]) -> list[Any]:
    seen: set[tuple[str, str, str]] = set()
    out: list[Any] = []
    for sponsor in sponsors:
        key = (str(sponsor.nct_id), str(sponsor.sponsor_name_norm), str(sponsor.sponsor_role))
        if key in seen:
            continue
        seen.add(key)
        out.append(sponsor)
    return out


def dedupe_query_hits(hits: Iterable[QueryHit]) -> list[QueryHit]:
    best: dict[tuple[int, str, str, str], QueryHit] = {}
    for hit in hits:
        key = (int(hit.company_id), str(hit.nct_id), str(hit.search_term), str(hit.query_field))
        old = best.get(key)
        if old is None or float(hit.confidence) > float(old.confidence):
            best[key] = hit
    return list(best.values())


def upsert_trial(conn: sqlite3.Connection, study: dict[str, Any], *, asof_date: str | None = None) -> bool:
    parsed = parse_study(study)
    if parsed is None:
        return False
    now = utc_now()
    conn.execute(
        """
        INSERT INTO trials(
            nct_id, brief_title, study_type, phase_text, overall_status,
            lead_sponsor, last_update_post_date, has_results, raw_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(nct_id) DO UPDATE SET
            brief_title = excluded.brief_title,
            study_type = excluded.study_type,
            phase_text = excluded.phase_text,
            overall_status = excluded.overall_status,
            lead_sponsor = excluded.lead_sponsor,
            last_update_post_date = excluded.last_update_post_date,
            has_results = excluded.has_results,
            raw_json = excluded.raw_json,
            updated_at = excluded.updated_at
        """,
        (
            parsed.nct_id,
            parsed.brief_title,
            parsed.study_type,
            parsed.phase_text,
            parsed.overall_status,
            parsed.lead_sponsor,
            parsed.last_update_post_date,
            1 if parsed.has_results else 0,
            parsed.raw_json,
            now,
            now,
        ),
    )
    sponsors = dedupe_sponsors(parse_sponsors(study))
    conn.execute("DELETE FROM trial_sponsors WHERE nct_id = ?", (parsed.nct_id,))
    for sponsor in sponsors:
        conn.execute(
            """
            INSERT INTO trial_sponsors(
                nct_id, sponsor_name, sponsor_name_norm, sponsor_role, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                sponsor.nct_id,
                sponsor.sponsor_name,
                sponsor.sponsor_name_norm,
                sponsor.sponsor_role,
                now,
                now,
            ),
        )
    snapshot_date = asof_date or datetime.now(timezone.utc).date().isoformat()
    conn.execute(
        """
        INSERT INTO trial_snapshot_daily(
            asof_date, nct_id, overall_status, phase_text, has_results,
            primary_completion_date, enrollment_count, raw_hash, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(asof_date, nct_id) DO UPDATE SET
            overall_status = excluded.overall_status,
            phase_text = excluded.phase_text,
            has_results = excluded.has_results,
            primary_completion_date = excluded.primary_completion_date,
            enrollment_count = excluded.enrollment_count,
            raw_hash = excluded.raw_hash
        """,
        (
            snapshot_date,
            parsed.nct_id,
            parsed.overall_status,
            parsed.phase_text,
            1 if parsed.has_results else 0,
            parsed.primary_completion_date,
            parsed.enrollment_count,
            parsed.raw_hash,
            now,
        ),
    )
    return True


def replace_query_hits(conn: sqlite3.Connection, results: list[SyncResult]) -> int:
    successful_results = [result for result in results if not result.error]
    company_ids = sorted({result.company_id for result in successful_results})
    if not company_ids:
        return 0
    for company_chunk in chunked(company_ids):
        placeholders = ",".join("?" for _ in company_chunk)
        conn.execute(f"DELETE FROM ctgov_query_hits WHERE company_id IN ({placeholders})", tuple(company_chunk))
    written = 0
    now = utc_now()
    hits = dedupe_query_hits(hit for result in successful_results for hit in result.query_hits)
    for hit in hits:
        conn.execute(
            """
            INSERT INTO ctgov_query_hits(
                company_id, nct_id, search_term, query_field, source, confidence, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                hit.company_id,
                hit.nct_id,
                hit.search_term,
                hit.query_field,
                hit.source,
                float(hit.confidence),
                now,
                now,
            ),
        )
        written += 1
    return written


def main() -> None:
    configure_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent

    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    cache_dir = resolve_path(cfg_get(config, "ctgov.cache_dir", "../output/biotech_index_cache"), base_dir=base_dir)
    overrides_path = resolve_optional_path(cfg_get(config, "ctgov.search_overrides_csv"), base_dir=base_dir)
    studies_url = str(cfg_get(config, "ctgov.studies_url", "https://clinicaltrials.gov/api/v2/studies"))
    query_fields = normalize_string_list(cfg_get(config, "ctgov.query_fields"), ["query.spons", "query.lead"])
    override_default_query_fields = normalize_string_list(cfg_get(config, "ctgov.override_default_query_fields"), ["query.intr"])
    status_filter = {value.lower() for value in normalize_string_list(cfg_get(config, "ctgov.status_filter"), ["keep", "review"])}
    min_alias_length = int(cfg_get(config, "ctgov.min_alias_length", 4))
    max_aliases_per_company = int(cfg_get(config, "ctgov.max_aliases_per_company", 4))
    page_size = int(cfg_get(config, "ctgov.page_size", 100))
    max_pages = int(cfg_get(config, "ctgov.max_pages", 25))
    ttl_hours = float(cfg_get(config, "ctgov.json_ttl_hours", 168.0))
    sleep_sec = float(cfg_get(config, "ctgov.sleep_sec", 0.2))
    timeout_sec = float(cfg_get(config, "ctgov.timeout_sec", 45.0))
    max_retries = int(cfg_get(config, "ctgov.max_retries", 3))
    max_workers = int(args.max_workers if args.max_workers is not None else cfg_get(config, "ctgov.max_workers", 4))
    ticker_filter = {value.strip().upper().replace(".", "-") for value in args.tickers.split(",") if value.strip()}
    asof_obj = parse_date(args.asof) if args.asof else datetime.now(timezone.utc).date()
    if asof_obj is None:
        raise ValueError(f"Invalid --asof date: {args.asof}")
    asof_date = asof_obj.isoformat()

    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        search_overrides = load_ctgov_search_overrides(overrides_path, default_query_fields=override_default_query_fields)
        jobs = load_company_jobs(
            conn,
            status_filter=status_filter,
            min_alias_length=min_alias_length,
            max_aliases_per_company=max_aliases_per_company,
            query_fields=query_fields,
            search_overrides=search_overrides,
            ticker_filter=ticker_filter,
            max_companies=int(args.max_companies),
        )
        run_id = start_run(conn, run_type="sync_ctgov_trials", input_path=db_path)
        LOGGER.info("Loaded %d active company job(s) from %s", len(jobs), db_path)

        throttle = HostThrottle()
        results: list[SyncResult] = []
        try:
            validate_nonempty_selection(
                count=len(jobs),
                context="CTGov sync",
                subset_mode=bool(ticker_filter) or int(args.max_companies) > 0,
            )
            validate_requested_tickers(
                requested_tickers=ticker_filter,
                loaded_tickers=[job.ticker for job in jobs],
                context="CTGov sync",
            )
            if max_workers <= 1:
                for idx, job in enumerate(jobs, start=1):
                    LOGGER.info("[%d/%d] CTGov %s aliases=%d searches=%d", idx, len(jobs), job.ticker, len(job.aliases), len(job.searches))
                    result = sync_one_company(
                        job,
                        cache_dir=cache_dir,
                        studies_url=studies_url,
                        query_fields=query_fields,
                        page_size=page_size,
                        max_pages=max_pages,
                        ttl_hours=ttl_hours,
                        sleep_sec=sleep_sec,
                        timeout_sec=timeout_sec,
                        max_retries=max_retries,
                        throttle=throttle,
                    )
                    results.append(result)
                    LOGGER.info(
                        "[%d/%d] CTGov complete %s aliases=%d searches=%d studies=%d query_hits=%d error=%s",
                        idx,
                        len(jobs),
                        result.ticker,
                        result.alias_count,
                        result.search_count,
                        result.study_count,
                        len(result.query_hits),
                        result.error,
                    )
            else:
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = {
                        executor.submit(
                            sync_one_company,
                            job,
                            cache_dir=cache_dir,
                            studies_url=studies_url,
                            query_fields=query_fields,
                            page_size=page_size,
                            max_pages=max_pages,
                            ttl_hours=ttl_hours,
                            sleep_sec=sleep_sec,
                            timeout_sec=timeout_sec,
                            max_retries=max_retries,
                            throttle=throttle,
                        ): job
                        for job in jobs
                    }
                    for idx, future in enumerate(as_completed(futures), start=1):
                        job = futures[future]
                        try:
                            result = future.result()
                        except Exception as exc:
                            LOGGER.exception("Unexpected CTGov worker failure for %s: %s", job.ticker, exc)
                            result = SyncResult(
                                company_id=job.company_id,
                                ticker=job.ticker,
                                alias_count=len(job.aliases),
                                search_count=len(job.searches),
                                study_count=0,
                                studies={},
                                query_hits=(),
                                error=f"{type(exc).__name__}: {exc}",
                            )
                        results.append(result)
                        LOGGER.info(
                            "[%d/%d] CTGov complete %s aliases=%d searches=%d studies=%d query_hits=%d error=%s",
                            idx,
                            len(jobs),
                            result.ticker,
                            result.alias_count,
                            result.search_count,
                            result.study_count,
                            len(result.query_hits),
                            result.error,
                        )

            unique_studies: dict[str, dict[str, Any]] = {}
            error_count = sum(1 for result in results if result.error)
            for result in results:
                unique_studies.update(result.studies)
            written = 0
            query_hit_count = 0
            with conn:
                for study in unique_studies.values():
                    if upsert_trial(conn, study, asof_date=asof_date):
                        written += 1
                query_hit_count = replace_query_hits(conn, results)
            sponsor_count = int(conn.execute("SELECT COUNT(*) FROM trial_sponsors").fetchone()[0])
            snapshot_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM trial_snapshot_daily WHERE asof_date = ?",
                    (asof_date,),
                ).fetchone()[0]
            )
            message = f"companies={len(jobs)} errors={error_count} unique_trials={written} query_hits={query_hit_count} sponsors={sponsor_count} snapshots_asof={snapshot_count}"
            status = "success" if error_count == 0 else "partial"
            finish_run(conn, run_id=run_id, status=status, row_count=written, message=message)
            LOGGER.info("CTGov sync complete: %s", message)
            if error_count > 0 and not args.allow_partial:
                raise SystemExit(2)
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()
