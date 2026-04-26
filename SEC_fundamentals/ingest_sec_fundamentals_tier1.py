#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import random
import re
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import requests
import yaml

from sec_fundamentals_config import (
    FOREIGN_ANNUAL_FORMS,
    cfg_get,
    configure_pipeline_logging,
    load_sec_fundamentals_config,
    normalize_cik_10d,
)

DEFAULT_DB_PATH = Path(r"C:\Users\josel\Documents\PROD\DB\sec_fundamentals.sqlite")
DEFAULT_CONFIG_PATH = Path(__file__).resolve().with_name("config_sec_fundamentals.yaml")
DEFAULT_UNIVERSE_CSV = Path("index_constituents_out") / "cik_ticker_mapping.csv"
DEFAULT_USER_AGENT = "JL, Independent Research, jm.357@hotmail.com"
PLACEHOLDER_USER_AGENT = "Your Name your_email@example.com"
EMAIL_PATTERN = re.compile(r"([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})")
PLACEHOLDER_EMAIL_TOKENS = ("example", "placeholder", "test", "your")

SEC_SUBMISSIONS_URL_TMPL = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_SUBMISSIONS_PAGE_URL_TMPL = "https://data.sec.gov/submissions/{name}"
SEC_COMPANYFACTS_URL_TMPL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

DEI_TAGS = (
    "TradingSymbol",
    "SecurityExchangeName",
    "EntityCommonStockSharesOutstanding",
    "EntityPublicFloat",
    "EntityFilerCategory",
    "EntityWellKnownSeasonedIssuer",
    "EntitySmallBusinessIssuer",
)

RETRYABLE_HTTP_CODES = {408, 425, 429, 500, 502, 503, 504}
logger = logging.getLogger(__name__)


def _ensure_required_forms(configured_forms: set[str], *, label: str) -> set[str]:
    out = {str(x).strip().upper() for x in configured_forms if str(x).strip()}
    missing = sorted(FOREIGN_ANNUAL_FORMS - out)
    if missing:
        logger.warning(
            "SEC ingestion %s missing required foreign annual forms; auto-adding: %s",
            label,
            ", ".join(missing),
        )
        out.update(missing)
    return out


@dataclass(frozen=True)
class RunWindow:
    start_date: date
    end_date: date
    mode: str
    fetch_companyfacts_on_new_filings_only: bool
    companyfacts_refresh_days: int
    max_ciks: int


@dataclass(frozen=True)
class RequestConfig:
    timeout_seconds: int
    max_retries: int
    backoff_base_seconds: float
    backoff_cap_seconds: float
    sleep_seconds: float


@dataclass(frozen=True)
class HttpCacheConfig:
    enabled: bool
    cache_dir: Path
    submissions_ttl_hours: float
    companyfacts_ttl_hours: float
    cache_use_for_backfill: bool


@dataclass
class IngestFetchResult:
    cik: str
    ticker: str
    base_payload_for_profile: dict[str, Any] | None
    collected_rows: list[tuple[Any, ...]]
    newest_acceptance: str
    newest_filing_date: str
    companyfacts_fetched: bool
    fact_rows: list[tuple[Any, ...]]
    dei_rows: list[tuple[Any, ...]]
    dropped_unmapped: int = 0
    dropped_bad_units: int = 0
    error_text: str | None = None


class SecRequestRateLimiter:
    def __init__(self, min_interval_seconds: float) -> None:
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self._next_allowed = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        if self.min_interval_seconds <= 0:
            return
        while True:
            with self._lock:
                now = time.monotonic()
                wait_for = self._next_allowed - now
                if wait_for <= 0:
                    self._next_allowed = max(self._next_allowed, now) + self.min_interval_seconds
                    return
            time.sleep(min(wait_for, 0.05))


class SecHttpCache:
    def __init__(self, cfg: HttpCacheConfig) -> None:
        self.cfg = cfg
        self._write_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._stats = {
            "hits": 0,
            "negative_hits": 0,
            "misses": 0,
            "stale": 0,
            "writes": 0,
            "errors": 0,
        }
        self.cfg.cache_dir.mkdir(parents=True, exist_ok=True)

    def _ttl_seconds(self, namespace: str) -> float:
        if namespace == "companyfacts":
            return max(0.0, float(self.cfg.companyfacts_ttl_hours)) * 3600.0
        return max(0.0, float(self.cfg.submissions_ttl_hours)) * 3600.0

    def _cache_path(self, namespace: str, url: str) -> Path:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self.cfg.cache_dir / namespace / digest[:2] / f"{digest}.json"

    def _bump(self, key: str) -> None:
        with self._stats_lock:
            self._stats[key] = int(self._stats.get(key, 0)) + 1

    def get(self, namespace: str, url: str) -> tuple[bool, dict[str, Any] | None]:
        ttl_seconds = self._ttl_seconds(namespace)
        if ttl_seconds <= 0:
            return False, None
        path = self._cache_path(namespace, url)
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except FileNotFoundError:
            self._bump("misses")
            return False, None
        except Exception as exc:
            logger.warning("Cache read failed for namespace=%s url=%s: %s", namespace, url, exc)
            self._bump("errors")
            return False, None
        if not isinstance(raw, dict):
            self._bump("errors")
            return False, None
        fetched_at = parse_iso_datetime(str(raw.get("fetched_at_utc", "") or ""))
        if fetched_at is None:
            self._bump("errors")
            return False, None
        age_seconds = (datetime.now(timezone.utc) - fetched_at).total_seconds()
        if age_seconds > ttl_seconds:
            self._bump("stale")
            self._bump("misses")
            return False, None
        status = str(raw.get("status", "") or "").strip().lower()
        if status == "missing":
            self._bump("negative_hits")
            return True, None
        payload = raw.get("payload")
        if status == "ok" and isinstance(payload, dict):
            self._bump("hits")
            return True, payload
        self._bump("errors")
        return False, None

    def put(self, namespace: str, url: str, payload: dict[str, Any] | None, *, status: str) -> None:
        ttl_seconds = self._ttl_seconds(namespace)
        if ttl_seconds <= 0:
            return
        path = self._cache_path(namespace, url)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        record: dict[str, Any] = {
            "url": url,
            "status": status,
            "fetched_at_utc": utc_now_iso(),
        }
        if status == "ok":
            record["payload"] = payload or {}
        try:
            with self._write_lock:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(record, f, separators=(",", ":"), ensure_ascii=True)
                os.replace(tmp_path, path)
            self._bump("writes")
        except Exception as exc:
            logger.warning("Cache write failed for namespace=%s url=%s: %s", namespace, url, exc)
            self._bump("errors")
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass

    def summary(self) -> dict[str, Any]:
        with self._stats_lock:
            out = dict(self._stats)
        out["cache_dir"] = str(self.cfg.cache_dir)
        return out


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_date(value: str | None) -> date | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%d-%b-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def parse_iso_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def normalize_cik(raw: str | int | None) -> str | None:
    return normalize_cik_10d(raw)


def resolve_user_agent(raw_user_agent: str) -> str:
    user_agent = (raw_user_agent or "").strip()
    lower_ua = user_agent.lower()
    if (
        not user_agent
        or user_agent == PLACEHOLDER_USER_AGENT
        or "your name" in lower_ua
    ):
        raise SystemExit(
            "Missing SEC User-Agent. Set it in fundamental_data/config_sec_fundamentals.yaml "
            "as sec_fundamentals.user_agent."
        )
    m = EMAIL_PATTERN.search(user_agent)
    if not m:
        raise SystemExit(
            "Invalid SEC User-Agent. Include a real contact email address, e.g. "
            "'Jane Doe, Research, jane@example.org'."
        )
    email = m.group(1).lower()
    domain = email.split("@", 1)[1] if "@" in email else ""
    if any(tok in domain for tok in PLACEHOLDER_EMAIL_TOKENS):
        raise SystemExit(
            "SEC User-Agent email appears to be placeholder/test. "
            "Set a real monitored contact email in sec_fundamentals.user_agent."
        )
    return user_agent


def _sleep_backoff(
    attempt: int,
    cfg: RequestConfig,
    retry_after: str | None = None,
) -> None:
    if retry_after and retry_after.strip().isdigit():
        delay = max(1.0, float(retry_after.strip()))
    else:
        delay = min(cfg.backoff_cap_seconds, cfg.backoff_base_seconds * (2**attempt))
        delay += random.uniform(0.0, min(1.0, delay / 5.0))
    time.sleep(delay)


def fetch_json(
    session: requests.Session,
    url: str,
    request_cfg: RequestConfig,
    missing_statuses: set[int] | None = None,
    rate_limiter: SecRequestRateLimiter | None = None,
    http_cache: SecHttpCache | None = None,
    cache_namespace: str | None = None,
) -> dict[str, Any] | None:
    missing = missing_statuses or {404}
    attempt_limit = max(1, int(request_cfg.max_retries))
    if http_cache is not None and cache_namespace:
        hit, cached_payload = http_cache.get(cache_namespace, url)
        if hit:
            return cached_payload
    for attempt in range(attempt_limit):
        if rate_limiter is not None:
            rate_limiter.wait()
        try:
            resp = session.get(url, timeout=request_cfg.timeout_seconds)
        except (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
            requests.exceptions.SSLError,
        ) as net_exc:
            if attempt < attempt_limit - 1:
                _sleep_backoff(attempt=attempt, cfg=request_cfg)
                continue
            raise RuntimeError(
                f"Network error after {attempt_limit} attempts for {url}"
            ) from net_exc
        if resp.status_code in missing:
            if http_cache is not None and cache_namespace:
                http_cache.put(cache_namespace, url, None, status="missing")
            return None
        if resp.status_code in RETRYABLE_HTTP_CODES:
            if attempt < attempt_limit - 1:
                _sleep_backoff(
                    attempt=attempt,
                    cfg=request_cfg,
                    retry_after=resp.headers.get("Retry-After"),
                )
                continue
            raise RuntimeError(f"Retry-exhausted HTTP {resp.status_code} after {attempt_limit} attempts for {url}")
        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, dict):
            raise RuntimeError(f"Expected JSON object from {url}, got {type(payload).__name__}")
        if http_cache is not None and cache_namespace:
            http_cache.put(cache_namespace, url, payload, status="ok")
        if rate_limiter is None and request_cfg.sleep_seconds > 0:
            time.sleep(request_cfg.sleep_seconds)
        return payload
    raise RuntimeError(f"fetch_json exhausted retries without a terminal outcome for {url}")


def load_tag_map(tag_map_path: Path) -> dict[str, Any]:
    if not tag_map_path.exists():
        return {}
    with open(tag_map_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return raw if isinstance(raw, dict) else {}


def mapped_tag_set(tag_map: dict[str, Any]) -> set[tuple[str, str]]:
    metrics = cfg_get(tag_map, "canonical_metrics", default={})
    if not isinstance(metrics, dict):
        return set()
    out: set[tuple[str, str]] = set()
    for spec in metrics.values():
        if not isinstance(spec, dict):
            continue
        cands = spec.get("candidate_tags", [])
        if not isinstance(cands, list):
            continue
        for cand in cands:
            if not (isinstance(cand, list) and len(cand) == 2):
                continue
            taxonomy = str(cand[0]).strip()
            tag = str(cand[1]).strip()
            if taxonomy and tag:
                out.add((taxonomy, tag))
    return out


def normalize_unit_text(unit: str | None) -> str:
    return re.sub(r"\s+", "", str(unit or "").strip().lower())


def mapped_tag_unit_map(tag_map: dict[str, Any]) -> dict[tuple[str, str], set[str]]:
    metrics = cfg_get(tag_map, "canonical_metrics", default={})
    if not isinstance(metrics, dict):
        return {}
    out: dict[tuple[str, str], set[str]] = {}
    for spec in metrics.values():
        if not isinstance(spec, dict):
            continue
        cands = spec.get("candidate_tags", [])
        if not isinstance(cands, list):
            continue
        preferred_units = spec.get("preferred_units", [])
        preferred_norm = {
            normalize_unit_text(str(u))
            for u in preferred_units
            if str(u).strip()
        } if isinstance(preferred_units, list) else set()
        for cand in cands:
            if not (isinstance(cand, list) and len(cand) == 2):
                continue
            taxonomy = str(cand[0]).strip()
            tag = str(cand[1]).strip()
            if taxonomy and tag:
                out[(taxonomy, tag)] = set(preferred_norm)
    return out


def read_universe_rows(universe_csv: Path, max_ciks: int) -> list[dict[str, str]]:
    if not universe_csv.exists():
        raise FileNotFoundError(f"Universe CSV not found: {universe_csv}")

    with open(universe_csv, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise RuntimeError(f"Universe CSV has no header: {universe_csv}")
        cols = {name.lower().strip(): name for name in reader.fieldnames}
        cols_norm = {
            re.sub(r"[^a-z0-9]+", "", name.lower().strip()): name
            for name in reader.fieldnames
        }

        def resolve_col(candidates: tuple[str, ...]) -> str | None:
            for candidate in candidates:
                if candidate in cols:
                    return cols[candidate]
                norm = re.sub(r"[^a-z0-9]+", "", candidate)
                if norm in cols_norm:
                    return cols_norm[norm]
            return None

        cik_col = resolve_col(("cik", "issuer_cik", "cik_str", "ciknumber"))
        if cik_col is None:
            raise RuntimeError("Universe CSV must include a CIK column.")

        # Prefer raw input ticker, then SEC-matched ticker.
        ticker_col = resolve_col(
            (
                "ticker",
                "matched_ticker",
                "matchedticker",
                "symbol",
                "issuer_trading_symbol",
            )
        )
        name_col = resolve_col(("company_name", "companyname", "name", "issuer_name", "company"))

        rows: list[dict[str, str]] = []
        seen_ciks: set[str] = set()
        for raw in reader:
            cik = normalize_cik(raw.get(cik_col))
            if not cik or cik in seen_ciks:
                continue
            seen_ciks.add(cik)
            rows.append(
                {
                    "cik": cik,
                    "ticker": (raw.get(ticker_col, "") if ticker_col else "").strip().upper(),
                    "company_name": (raw.get(name_col, "") if name_col else "").strip(),
                }
            )
            if max_ciks > 0 and len(rows) >= max_ciks:
                break
        return rows


def default_db_path() -> Path:
    return Path(os.getenv("SEC_FUNDAMENTALS_DB_PATH", str(DEFAULT_DB_PATH)))


def ensure_required_tables(conn: sqlite3.Connection) -> None:
    required = {
        "sec_entity_universe",
        "sec_entity_profile",
        "sec_entity_ticker_history",
        "sec_filing_index",
        "sec_dei_facts",
        "sec_xbrl_facts_raw",
        "sec_entity_sync_state",
        "sec_ingest_run_log",
    }
    existing = {
        row[0].lower()
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    missing = sorted(name for name in required if name not in existing)
    if missing:
        raise RuntimeError(
            "Fundamentals DB schema is missing required tables: "
            + ", ".join(missing)
            + ". Run fundamental_data/init_sec_fundamentals_db.py first."
        )


def upsert_universe_rows(conn: sqlite3.Connection, rows: Iterable[dict[str, str]]) -> None:
    now = utc_now_iso()
    data = [
        (
            row["cik"],
            row.get("ticker", ""),
            row.get("company_name", ""),
            "csv",
            1,
            now,
            now,
        )
        for row in rows
    ]
    conn.executemany(
        """
        INSERT INTO sec_entity_universe(
            cik, ticker, company_name, universe_source, active, added_at_utc, updated_at_utc
        )
        VALUES(?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(cik) DO UPDATE SET
            ticker = excluded.ticker,
            company_name = excluded.company_name,
            universe_source = excluded.universe_source,
            active = excluded.active,
            updated_at_utc = excluded.updated_at_utc
        """,
        data,
    )


def upsert_run_log_start(conn: sqlite3.Connection, run_id: str, mode: str, cik_total: int) -> None:
    conn.execute(
        """
        INSERT INTO sec_ingest_run_log(
            run_id, mode, started_utc, status, cik_total
        )
        VALUES(?, ?, ?, 'running', ?)
        """,
        (run_id, mode, utc_now_iso(), cik_total),
    )


def upsert_run_log_finish(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    status: str,
    cik_processed: int,
    filing_rows_added: int,
    fact_rows_added: int,
    error_text: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE sec_ingest_run_log
        SET
            finished_utc = ?,
            status = ?,
            cik_processed = ?,
            filing_rows_added = ?,
            fact_rows_added = ?,
            error_text = ?
        WHERE run_id = ?
        """,
        (
            utc_now_iso(),
            status,
            cik_processed,
            filing_rows_added,
            fact_rows_added,
            error_text,
            run_id,
        ),
    )


def get_sync_state(conn: sqlite3.Connection, cik: str) -> dict[str, str]:
    row = conn.execute(
        """
        SELECT
            COALESCE(last_submission_acceptance_datetime, ''),
            COALESCE(last_submissions_fetch_utc, ''),
            COALESCE(last_companyfacts_fetch_utc, ''),
            COALESCE(last_filing_date_seen, '')
        FROM sec_entity_sync_state
        WHERE cik = ?
        """,
        (cik,),
    ).fetchone()
    if not row:
        return {
            "last_submission_acceptance_datetime": "",
            "last_submissions_fetch_utc": "",
            "last_companyfacts_fetch_utc": "",
            "last_filing_date_seen": "",
        }
    return {
        "last_submission_acceptance_datetime": row[0] or "",
        "last_submissions_fetch_utc": row[1] or "",
        "last_companyfacts_fetch_utc": row[2] or "",
        "last_filing_date_seen": row[3] or "",
    }


def upsert_sync_state(
    conn: sqlite3.Connection,
    cik: str,
    *,
    last_submission_acceptance_datetime: str | None = None,
    last_submissions_fetch_utc: str | None = None,
    last_companyfacts_fetch_utc: str | None = None,
    last_filing_date_seen: str | None = None,
    last_error_utc: str | None = None,
    last_error_text: str | None = None,
    run_mode: str,
) -> None:
    row = conn.execute(
        """
        SELECT
            COALESCE(last_submission_acceptance_datetime, ''),
            COALESCE(last_submissions_fetch_utc, ''),
            COALESCE(last_companyfacts_fetch_utc, ''),
            COALESCE(last_filing_date_seen, ''),
            COALESCE(last_success_utc, ''),
            COALESCE(last_error_utc, ''),
            COALESCE(last_error_text, '')
        FROM sec_entity_sync_state
        WHERE cik = ?
        """,
        (cik,),
    ).fetchone()
    existing = {
        "last_submission_acceptance_datetime": row[0] if row else "",
        "last_submissions_fetch_utc": row[1] if row else "",
        "last_companyfacts_fetch_utc": row[2] if row else "",
        "last_filing_date_seen": row[3] if row else "",
        "last_success_utc": row[4] if row else "",
        "last_error_utc": row[5] if row else "",
        "last_error_text": row[6] if row else "",
    }
    now = utc_now_iso()
    is_error = bool(last_error_utc or last_error_text)
    conn.execute(
        """
        INSERT INTO sec_entity_sync_state(
            cik,
            last_submission_acceptance_datetime,
            last_submissions_fetch_utc,
            last_companyfacts_fetch_utc,
            last_filing_date_seen,
            last_success_utc,
            last_error_utc,
            last_error_text,
            last_run_mode
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(cik) DO UPDATE SET
            last_submission_acceptance_datetime = excluded.last_submission_acceptance_datetime,
            last_submissions_fetch_utc = excluded.last_submissions_fetch_utc,
            last_companyfacts_fetch_utc = excluded.last_companyfacts_fetch_utc,
            last_filing_date_seen = excluded.last_filing_date_seen,
            last_success_utc = excluded.last_success_utc,
            last_error_utc = excluded.last_error_utc,
            last_error_text = excluded.last_error_text,
            last_run_mode = excluded.last_run_mode
        """,
        (
            cik,
            last_submission_acceptance_datetime or existing["last_submission_acceptance_datetime"],
            last_submissions_fetch_utc or existing["last_submissions_fetch_utc"],
            last_companyfacts_fetch_utc or existing["last_companyfacts_fetch_utc"],
            last_filing_date_seen or existing["last_filing_date_seen"],
            existing["last_success_utc"] if is_error else now,
            last_error_utc or "",
            last_error_text or "",
            run_mode,
        ),
    )


def build_run_window(cfg: dict[str, Any], mode: str, as_of: date) -> RunWindow:
    mode = mode.lower().strip()
    if mode not in {"daily", "weekly", "quarterly", "backfill"}:
        raise ValueError("run_mode must be one of: daily, weekly, quarterly, backfill")

    mode_cfg = cfg_get(cfg, mode, default={})
    ingestion_cfg = cfg_get(cfg, "ingestion", default={})
    start = parse_date(cfg_get(cfg, "start_date", default=None))
    end = parse_date(cfg_get(cfg, "end_date", default=None)) or as_of

    if mode == "backfill":
        if start is None:
            backfill_years = int(cfg_get(cfg, "backfill_years", default=7))
            start = as_of - timedelta(days=365 * backfill_years)
    else:
        default_lookback = 30 if mode == "daily" else 120 if mode == "weekly" else 420
        lookback_days = int(cfg_get(mode_cfg, "lookback_days", default=default_lookback))
        if start is None:
            start = as_of - timedelta(days=lookback_days)

    if start > end:
        raise ValueError("Computed start_date is after end_date.")

    return RunWindow(
        start_date=start,
        end_date=end,
        mode=mode,
        fetch_companyfacts_on_new_filings_only=bool(
            cfg_get(
                mode_cfg,
                "fetch_companyfacts_on_new_filings_only",
                default=(mode == "daily"),
            )
        ),
        companyfacts_refresh_days=int(cfg_get(mode_cfg, "companyfacts_refresh_days", default=7)),
        max_ciks=int(cfg_get(mode_cfg, "max_ciks", default=cfg_get(ingestion_cfg, "max_ciks", default=0))),
    )


def build_request_cfg(cfg: dict[str, Any]) -> RequestConfig:
    req = cfg_get(cfg, "request", default={})
    return RequestConfig(
        timeout_seconds=int(cfg_get(req, "timeout_seconds", default=60)),
        max_retries=int(cfg_get(req, "max_retries", default=4)),
        backoff_base_seconds=float(cfg_get(req, "backoff_base_seconds", default=1.5)),
        backoff_cap_seconds=float(cfg_get(req, "backoff_cap_seconds", default=60.0)),
        sleep_seconds=float(cfg_get(req, "sleep_seconds", default=0.2)),
    )


def build_http_cache_cfg(cfg: dict[str, Any]) -> HttpCacheConfig:
    req = cfg_get(cfg, "request", default={})
    raw_cache_dir = Path(
        cfg_get(req, "cache_dir", default=str(Path("fundamental_data") / "cache" / "sec_http"))
    )
    if not raw_cache_dir.is_absolute():
        raw_cache_dir = (Path(__file__).resolve().parent.parent / raw_cache_dir).resolve()
    return HttpCacheConfig(
        enabled=bool(cfg_get(req, "cache_enabled", default=True)),
        cache_dir=raw_cache_dir,
        submissions_ttl_hours=float(cfg_get(req, "submissions_cache_ttl_hours", default=4.0)),
        companyfacts_ttl_hours=float(cfg_get(req, "companyfacts_cache_ttl_hours", default=8.0)),
        cache_use_for_backfill=bool(cfg_get(req, "cache_use_for_backfill", default=True)),
    )


def build_sec_session(user_agent: str) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
        }
    )
    return session


class SecSessionPool:
    def __init__(self, user_agent: str) -> None:
        self.user_agent = user_agent
        self._local = threading.local()
        self._lock = threading.Lock()
        self._sessions: list[requests.Session] = []

    def get(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = build_sec_session(self.user_agent)
            self._local.session = session
            with self._lock:
                self._sessions.append(session)
        return session

    def close_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions)
            self._sessions.clear()
        for session in sessions:
            try:
                session.close()
            except Exception:
                logger.debug("Failed to close SEC HTTP session cleanly.", exc_info=True)


def connect_sqlite(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def str_or_empty(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def int_or_none(value: Any) -> int | None:
    text = str_or_empty(value)
    if text == "":
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if text == "":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def should_keep_date(
    dt: date | None,
    *,
    start_date: date,
    end_date: date,
) -> bool:
    if dt is None:
        return False
    return start_date <= dt <= end_date


def upsert_entity_profile(
    conn: sqlite3.Connection,
    cik: str,
    payload: dict[str, Any],
) -> None:
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO sec_entity_profile(
            cik,
            entity_name,
            sic,
            sic_description,
            category,
            fiscal_year_end,
            state_of_incorporation,
            state_of_incorporation_description,
            phone,
            website,
            investor_website,
            description,
            insider_transaction_for_owner_exists,
            insider_transaction_for_issuer_exists,
            former_names_json,
            last_submissions_fetched_utc,
            updated_at_utc
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(cik) DO UPDATE SET
            entity_name = excluded.entity_name,
            sic = excluded.sic,
            sic_description = excluded.sic_description,
            category = excluded.category,
            fiscal_year_end = excluded.fiscal_year_end,
            state_of_incorporation = excluded.state_of_incorporation,
            state_of_incorporation_description = excluded.state_of_incorporation_description,
            phone = excluded.phone,
            website = excluded.website,
            investor_website = excluded.investor_website,
            description = excluded.description,
            insider_transaction_for_owner_exists = excluded.insider_transaction_for_owner_exists,
            insider_transaction_for_issuer_exists = excluded.insider_transaction_for_issuer_exists,
            former_names_json = excluded.former_names_json,
            last_submissions_fetched_utc = excluded.last_submissions_fetched_utc,
            updated_at_utc = excluded.updated_at_utc
        """,
        (
            cik,
            str_or_empty(payload.get("name")),
            str_or_empty(payload.get("sic")),
            str_or_empty(payload.get("sicDescription")),
            str_or_empty(payload.get("category")),
            str_or_empty(payload.get("fiscalYearEnd")),
            str_or_empty(payload.get("stateOfIncorporation")),
            str_or_empty(payload.get("stateOfIncorporationDescription")),
            str_or_empty(payload.get("phone")),
            str_or_empty(payload.get("website")),
            str_or_empty(payload.get("investorWebsite")),
            str_or_empty(payload.get("description")),
            int_or_none(payload.get("insiderTransactionForOwnerExists")),
            int_or_none(payload.get("insiderTransactionForIssuerExists")),
            json.dumps(payload.get("formerNames", []), separators=(",", ":"), ensure_ascii=True),
            now,
            now,
        ),
    )

    tickers = payload.get("tickers", [])
    exchanges = payload.get("exchanges", [])
    if isinstance(tickers, list):
        for idx, raw_ticker in enumerate(tickers):
            ticker = str_or_empty(raw_ticker).upper()
            if not ticker:
                continue
            exchange = ""
            if isinstance(exchanges, list) and idx < len(exchanges):
                exchange = str_or_empty(exchanges[idx])
            conn.execute(
                """
                INSERT INTO sec_entity_ticker_history(
                    cik, ticker, exchange, is_current, as_of_date, source, updated_at_utc
                )
                VALUES(?, ?, ?, 1, '', 'submissions', ?)
                ON CONFLICT(cik, ticker, exchange, as_of_date) DO UPDATE SET
                    is_current = excluded.is_current,
                    source = excluded.source,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (cik, ticker, exchange, now),
            )


def extract_submission_rows(
    cik: str,
    payload: dict[str, Any],
    *,
    source_json_page: str,
    forms_of_interest: set[str],
    start_date: date,
    end_date: date,
) -> tuple[list[tuple[Any, ...]], str, str]:
    filings = cfg_get(payload, "filings", default={})
    recent = cfg_get(filings, "recent", default={})
    if (
        not isinstance(recent, dict)
        or ("accessionNumber" not in recent and isinstance(payload, dict) and "accessionNumber" in payload)
    ):
        # Older SEC submissions page files (e.g. CIK##########-submissions-001.json)
        # expose the filing arrays at the top level rather than under filings.recent.
        recent = payload if isinstance(payload, dict) else {}
    if not isinstance(recent, dict):
        return [], "", ""

    arrays = {
        "accessionNumber": recent.get("accessionNumber", []),
        "form": recent.get("form", []),
        "filingDate": recent.get("filingDate", []),
        "acceptanceDateTime": recent.get("acceptanceDateTime", []),
        "reportDate": recent.get("reportDate", []),
        "act": recent.get("act", []),
        "fileNumber": recent.get("fileNumber", []),
        "filmNumber": recent.get("filmNumber", []),
        "items": recent.get("items", []),
        "size": recent.get("size", []),
        "isXBRL": recent.get("isXBRL", []),
        "isInlineXBRL": recent.get("isInlineXBRL", []),
        "primaryDocument": recent.get("primaryDocument", []),
        "primaryDocDescription": recent.get("primaryDocDescription", []),
    }
    lengths = [len(v) for v in arrays.values() if isinstance(v, list)]
    if not lengths:
        return [], "", ""
    min_len = min(lengths)
    max_len = max(lengths)
    if min_len != max_len:
        logger.warning(
            "CIK %s submissions payload has mismatched recent-array lengths on %s; truncating to %d rows (min=%d max=%d).",
            cik,
            source_json_page,
            min_len,
            min_len,
            max_len,
        )

    n = min_len
    now = utc_now_iso()
    out: list[tuple[Any, ...]] = []
    newest_acceptance = ""
    newest_filing_date = ""

    for i in range(n):
        form = str_or_empty(arrays["form"][i]).upper()
        if forms_of_interest and form not in forms_of_interest:
            continue

        filing_date = parse_date(str_or_empty(arrays["filingDate"][i]))
        if not should_keep_date(filing_date, start_date=start_date, end_date=end_date):
            continue

        accession = str_or_empty(arrays["accessionNumber"][i])
        if not accession:
            continue

        filing_date_text = filing_date.isoformat() if filing_date else ""
        acceptance_text = str_or_empty(arrays["acceptanceDateTime"][i])
        report_end = parse_date(str_or_empty(arrays["reportDate"][i]))
        report_end_text = report_end.isoformat() if report_end else ""
        is_amendment = 1 if form.endswith("/A") else 0

        source_url = SEC_SUBMISSIONS_PAGE_URL_TMPL.format(name=source_json_page) if source_json_page else ""
        out.append(
            (
                accession,
                cik,
                "",
                form,
                filing_date_text,
                acceptance_text,
                report_end_text,
                None,
                None,
                is_amendment,
                "",
                str_or_empty(arrays["primaryDocument"][i]),
                str_or_empty(arrays["primaryDocDescription"][i]),
                str_or_empty(arrays["items"][i]),
                str_or_empty(arrays["filmNumber"][i]),
                str_or_empty(arrays["fileNumber"][i]),
                int_or_none(arrays["size"][i]),
                int_or_none(arrays["isXBRL"][i]),
                int_or_none(arrays["isInlineXBRL"][i]),
                source_json_page,
                source_url,
                now,
                now,
            )
        )

        if acceptance_text and acceptance_text > newest_acceptance:
            newest_acceptance = acceptance_text
        if filing_date_text and filing_date_text > newest_filing_date:
            newest_filing_date = filing_date_text

    return out, newest_acceptance, newest_filing_date


def upsert_submission_rows(conn: sqlite3.Connection, rows: Iterable[tuple[Any, ...]]) -> int:
    changes_before = conn.total_changes
    conn.executemany(
        """
        INSERT INTO sec_filing_index(
            accession_number,
            cik,
            company_name,
            form_type,
            filing_date,
            acceptance_datetime,
            report_period_end,
            fiscal_year_focus,
            fiscal_period_focus,
            is_amendment,
            amendment_description,
            primary_document,
            primary_doc_description,
            items,
            film_number,
            file_number,
            size_bytes,
            is_xbrl,
            is_inline_xbrl,
            source_json_page,
            source_url,
            created_at_utc,
            updated_at_utc
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(accession_number) DO UPDATE SET
            cik = excluded.cik,
            form_type = excluded.form_type,
            filing_date = excluded.filing_date,
            acceptance_datetime = excluded.acceptance_datetime,
            report_period_end = excluded.report_period_end,
            is_amendment = excluded.is_amendment,
            primary_document = excluded.primary_document,
            primary_doc_description = excluded.primary_doc_description,
            items = excluded.items,
            film_number = excluded.film_number,
            file_number = excluded.file_number,
            size_bytes = excluded.size_bytes,
            is_xbrl = excluded.is_xbrl,
            is_inline_xbrl = excluded.is_inline_xbrl,
            source_json_page = excluded.source_json_page,
            source_url = excluded.source_url,
            updated_at_utc = excluded.updated_at_utc
        """,
        list(rows),
    )
    return conn.total_changes - changes_before


def parse_companyfacts_payload(
    cik: str,
    payload: dict[str, Any],
    *,
    forms_filter: set[str],
    start_date: date,
    end_date: date,
    mapped_only: bool,
    allowed_tags: set[tuple[str, str]],
    preferred_units_by_tag: dict[tuple[str, str], set[str]],
) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]], int, int]:
    facts = cfg_get(payload, "facts", default={})
    if not isinstance(facts, dict):
        return [], [], 0, 0

    now = utc_now_iso()
    fact_rows: list[tuple[Any, ...]] = []
    dei_by_accession: dict[str, dict[str, Any]] = {}

    dropped_unmapped = 0
    dropped_bad_units = 0

    for taxonomy, tag_map in facts.items():
        if not isinstance(tag_map, dict):
            continue
        taxonomy_text = str_or_empty(taxonomy)
        for tag, tag_obj in tag_map.items():
            if not isinstance(tag_obj, dict):
                continue
            tag_text = str_or_empty(tag)
            if not taxonomy_text or not tag_text:
                continue
            if mapped_only and (taxonomy_text, tag_text) not in allowed_tags:
                units_obj = tag_obj.get("units", {})
                if isinstance(units_obj, dict):
                    dropped_unmapped += sum(
                        len(v) for v in units_obj.values() if isinstance(v, list)
                    )
                else:
                    dropped_unmapped += 1
                continue
            preferred_units = preferred_units_by_tag.get((taxonomy_text, tag_text), set())

            label = str_or_empty(tag_obj.get("label"))
            units = tag_obj.get("units", {})
            if not isinstance(units, dict):
                continue

            for unit, entries in units.items():
                if not isinstance(entries, list):
                    continue
                unit_text = str_or_empty(unit)
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    form = str_or_empty(entry.get("form")).upper()
                    if forms_filter and (not form or form not in forms_filter):
                        continue

                    filed_dt = parse_date(str_or_empty(entry.get("filed")))
                    if filed_dt is not None and not should_keep_date(
                        filed_dt,
                        start_date=start_date,
                        end_date=end_date,
                    ):
                        continue

                    period_end = parse_date(str_or_empty(entry.get("end")))
                    if period_end is not None and period_end > end_date:
                        continue
                    if preferred_units:
                        normalized_unit = normalize_unit_text(unit_text)
                        if normalized_unit not in preferred_units:
                            dropped_bad_units += 1
                            continue

                    val = entry.get("val")
                    value_text = "" if val is None else str(val)
                    value_num = float_or_none(val)
                    accession = str_or_empty(entry.get("accn"))

                    fact_rows.append(
                        (
                            cik,
                            accession,
                            taxonomy_text,
                            tag_text,
                            label,
                            unit_text,
                            value_text,
                            value_num,
                            str_or_empty(entry.get("frame")),
                            form,
                            int_or_none(entry.get("fy")),
                            str_or_empty(entry.get("fp")),
                            str_or_empty(entry.get("start")),
                            str_or_empty(entry.get("end")),
                            str_or_empty(entry.get("filed")),
                            # Companyfacts exposes one period end date, not a distinct report date.
                            str_or_empty(entry.get("end")),
                            1 if form.endswith("/A") else 0,
                            "companyfacts",
                            now,
                        )
                    )

                    if taxonomy_text != "dei" or tag_text not in DEI_TAGS or not accession:
                        continue
                    existing = dei_by_accession.setdefault(
                        accession,
                        {
                            "accession_number": accession,
                            "cik": cik,
                            "trading_symbol": "",
                            "security_exchange_name": "",
                            "entity_common_stock_shares_outstanding": None,
                            "public_float": None,
                            "filer_category": "",
                            "well_known_seasoned_issuer": "",
                            "small_business_issuer": "",
                            "period_end_date": str_or_empty(entry.get("end")),
                            "filed_date": str_or_empty(entry.get("filed")),
                            "acceptance_datetime": "",
                            "source_tags": set(),
                        },
                    )
                    existing["source_tags"].add(tag_text)
                    existing["period_end_date"] = str_or_empty(entry.get("end")) or existing["period_end_date"]
                    existing["filed_date"] = str_or_empty(entry.get("filed")) or existing["filed_date"]

                    if tag_text == "TradingSymbol":
                        existing["trading_symbol"] = value_text.strip().upper()
                    elif tag_text == "SecurityExchangeName":
                        existing["security_exchange_name"] = value_text.strip()
                    elif tag_text == "EntityCommonStockSharesOutstanding":
                        existing["entity_common_stock_shares_outstanding"] = value_num
                    elif tag_text == "EntityPublicFloat":
                        existing["public_float"] = value_num
                    elif tag_text == "EntityFilerCategory":
                        existing["filer_category"] = value_text.strip()
                    elif tag_text == "EntityWellKnownSeasonedIssuer":
                        existing["well_known_seasoned_issuer"] = value_text.strip()
                    elif tag_text == "EntitySmallBusinessIssuer":
                        existing["small_business_issuer"] = value_text.strip()

    dei_rows = []
    for row in dei_by_accession.values():
        dei_rows.append(
            (
                row["accession_number"],
                row["cik"],
                row["trading_symbol"],
                row["security_exchange_name"],
                row["entity_common_stock_shares_outstanding"],
                row["public_float"],
                row["filer_category"],
                row["well_known_seasoned_issuer"],
                row["small_business_issuer"],
                row["period_end_date"],
                row["filed_date"],
                row["acceptance_datetime"],
                json.dumps(sorted(row["source_tags"]), separators=(",", ":"), ensure_ascii=True),
                now,
            )
        )
    return fact_rows, dei_rows, dropped_unmapped, dropped_bad_units


def insert_fact_rows(
    conn: sqlite3.Connection,
    rows: list[tuple[Any, ...]],
    *,
    chunk_size: int,
) -> int:
    if not rows:
        return 0
    def _natural_key(row: tuple[Any, ...]) -> tuple[Any, ...]:
        return (
            row[0],   # cik
            row[1],   # accession_number
            row[2],   # taxonomy
            row[3],   # tag
            row[5],   # unit
            row[12],  # period_start
            row[13],  # period_end
            row[14],  # filed_date
            row[8],   # frame
            row[9],   # form_type
        )

    def _unique_key(row: tuple[Any, ...]) -> tuple[Any, ...]:
        return _natural_key(row) + (row[6],)  # value_text

    grouped_rows: dict[tuple[Any, ...], dict[tuple[Any, ...], tuple[Any, ...]]] = {}
    for row in rows:
        natural_key = _natural_key(row)
        grouped_rows.setdefault(natural_key, {})[_unique_key(row)] = row

    temp_table = "_tmp_fact_upsert_keys"
    conn.execute(f"DROP TABLE IF EXISTS {temp_table}")
    conn.execute(
        f"""
        CREATE TEMP TABLE {temp_table} (
            cik TEXT NOT NULL,
            accession_number TEXT NOT NULL,
            taxonomy TEXT NOT NULL,
            tag TEXT NOT NULL,
            unit TEXT NOT NULL,
            period_start TEXT NOT NULL,
            period_end TEXT NOT NULL,
            filed_date TEXT NOT NULL,
            frame TEXT NOT NULL,
            form_type TEXT NOT NULL,
            PRIMARY KEY (
                cik,
                accession_number,
                taxonomy,
                tag,
                unit,
                period_start,
                period_end,
                filed_date,
                frame,
                form_type
            )
        )
        """
    )
    try:
        candidate_keys = list(grouped_rows.keys())
        existing_counts: dict[tuple[Any, ...], int] = {}
        for i in range(0, len(candidate_keys), max(1, chunk_size)):
            key_chunk = candidate_keys[i : i + chunk_size]
            conn.execute(f"DELETE FROM {temp_table}")
            conn.executemany(
                f"""
                INSERT OR REPLACE INTO {temp_table}(
                    cik,
                    accession_number,
                    taxonomy,
                    tag,
                    unit,
                    period_start,
                    period_end,
                    filed_date,
                    frame,
                    form_type
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                key_chunk,
            )
            rows_existing = conn.execute(
                f"""
                SELECT
                    k.cik,
                    k.accession_number,
                    k.taxonomy,
                    k.tag,
                    k.unit,
                    k.period_start,
                    k.period_end,
                    k.filed_date,
                    k.frame,
                    k.form_type,
                    COUNT(f.fact_id) AS existing_count
                FROM {temp_table} k
                LEFT JOIN sec_xbrl_facts_raw f
                  ON f.cik = k.cik
                 AND f.accession_number = k.accession_number
                 AND f.taxonomy = k.taxonomy
                 AND f.tag = k.tag
                 AND f.unit = k.unit
                 AND f.period_start = k.period_start
                 AND f.period_end = k.period_end
                 AND f.filed_date = k.filed_date
                 AND f.frame = k.frame
                 AND f.form_type = k.form_type
                GROUP BY
                    k.cik,
                    k.accession_number,
                    k.taxonomy,
                    k.tag,
                    k.unit,
                    k.period_start,
                    k.period_end,
                    k.filed_date,
                    k.frame,
                    k.form_type
                """
            ).fetchall()
            for existing_row in rows_existing:
                existing_counts[tuple(existing_row[:10])] = int(existing_row[10])

        replace_rows: list[tuple[Any, ...]] = []
        preserve_rows: list[tuple[Any, ...]] = []
        net_new_rows = 0
        for natural_key, unique_rows in grouped_rows.items():
            deduped_rows = list(unique_rows.values())
            existing_count = existing_counts.get(natural_key, 0)
            if len(deduped_rows) == 1 and existing_count <= 1:
                replace_rows.append(deduped_rows[0])
                if existing_count == 0:
                    net_new_rows += 1
            else:
                preserve_rows.extend(deduped_rows)

        delete_sql = """
            DELETE FROM sec_xbrl_facts_raw
            WHERE cik = ?
              AND accession_number = ?
              AND taxonomy = ?
              AND tag = ?
              AND unit = ?
              AND period_start = ?
              AND period_end = ?
              AND filed_date = ?
              AND frame = ?
              AND form_type = ?
        """
        insert_sql = """
            INSERT INTO sec_xbrl_facts_raw(
                cik,
                accession_number,
                taxonomy,
                tag,
                label,
                unit,
                value_text,
                value_num,
                frame,
                form_type,
                fiscal_year,
                fiscal_period,
                period_start,
                period_end,
                filed_date,
                report_period_end,
                is_amendment,
                source,
                loaded_at_utc
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        for i in range(0, len(replace_rows), max(1, chunk_size)):
            chunk = replace_rows[i : i + chunk_size]
            delete_keys = [_natural_key(row) for row in chunk]
            conn.executemany(delete_sql, delete_keys)
            conn.executemany(insert_sql, chunk)

        if preserve_rows:
            changes_before = conn.total_changes
            for i in range(0, len(preserve_rows), max(1, chunk_size)):
                conn.executemany(insert_sql.replace("INSERT INTO", "INSERT OR IGNORE INTO", 1), preserve_rows[i : i + chunk_size])
            net_new_rows += conn.total_changes - changes_before
        return net_new_rows
    finally:
        conn.execute(f"DROP TABLE IF EXISTS {temp_table}")


def upsert_dei_rows(conn: sqlite3.Connection, rows: list[tuple[Any, ...]]) -> int:
    if not rows:
        return 0
    changes_before = conn.total_changes
    conn.executemany(
        """
        INSERT INTO sec_dei_facts(
            accession_number,
            cik,
            trading_symbol,
            security_exchange_name,
            entity_common_stock_shares_outstanding,
            public_float,
            filer_category,
            well_known_seasoned_issuer,
            small_business_issuer,
            period_end_date,
            filed_date,
            acceptance_datetime,
            source_tags_json,
            updated_at_utc
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(accession_number) DO UPDATE SET
            cik = excluded.cik,
            trading_symbol = excluded.trading_symbol,
            security_exchange_name = excluded.security_exchange_name,
            entity_common_stock_shares_outstanding = excluded.entity_common_stock_shares_outstanding,
            public_float = excluded.public_float,
            filer_category = excluded.filer_category,
            well_known_seasoned_issuer = excluded.well_known_seasoned_issuer,
            small_business_issuer = excluded.small_business_issuer,
            period_end_date = excluded.period_end_date,
            filed_date = excluded.filed_date,
            acceptance_datetime = excluded.acceptance_datetime,
            source_tags_json = excluded.source_tags_json,
            updated_at_utc = excluded.updated_at_utc
        """,
        rows,
    )
    return conn.total_changes - changes_before


def iter_submission_payloads(
    session: requests.Session,
    cik: str,
    *,
    include_pages: bool,
    max_pages: int,
    request_cfg: RequestConfig,
    rate_limiter: SecRequestRateLimiter | None = None,
    http_cache: SecHttpCache | None = None,
) -> Iterable[tuple[str, dict[str, Any]]]:
    base_url = SEC_SUBMISSIONS_URL_TMPL.format(cik=cik)
    base = fetch_json(
        session,
        base_url,
        request_cfg=request_cfg,
        rate_limiter=rate_limiter,
        http_cache=http_cache,
        cache_namespace="submissions",
    )
    if base is None:
        return
    yield "CIK" + cik + ".json", base

    if not include_pages:
        return

    files = cfg_get(base, "filings", "files", default=[])
    if not isinstance(files, list):
        return
    page_count = 0
    for file_obj in files:
        if not isinstance(file_obj, dict):
            continue
        name = str_or_empty(file_obj.get("name"))
        if not name:
            continue
        page_url = SEC_SUBMISSIONS_PAGE_URL_TMPL.format(name=name)
        payload = fetch_json(
            session,
            page_url,
            request_cfg=request_cfg,
            missing_statuses={404},
            rate_limiter=rate_limiter,
            http_cache=http_cache,
            cache_namespace="submissions",
        )
        if payload is None:
            continue
        yield name, payload
        page_count += 1
        if max_pages > 0 and page_count >= max_pages:
            logger.warning(
                "CIK %s: reached max_submissions_pages=%d; older filings not fetched",
                cik,
                max_pages,
            )
            break


def should_refresh_companyfacts(
    *,
    window: RunWindow,
    new_filing_count: int,
    last_companyfacts_fetch_utc: str,
) -> bool:
    if not window.fetch_companyfacts_on_new_filings_only:
        return True
    if new_filing_count > 0:
        return True
    if window.companyfacts_refresh_days <= 0:
        return False
    last_dt = parse_iso_datetime(last_companyfacts_fetch_utc)
    if last_dt is None:
        return True
    threshold = datetime.now(timezone.utc) - timedelta(days=window.companyfacts_refresh_days)
    return last_dt < threshold


def load_companyfacts(
    session: requests.Session,
    *,
    cik: str,
    forms_filter: set[str],
    start_date: date,
    end_date: date,
    mapped_only: bool,
    allowed_tags: set[tuple[str, str]],
    preferred_units_by_tag: dict[tuple[str, str], set[str]],
    request_cfg: RequestConfig,
    rate_limiter: SecRequestRateLimiter | None = None,
    http_cache: SecHttpCache | None = None,
) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]], int, int]:
    payload = fetch_json(
        session,
        SEC_COMPANYFACTS_URL_TMPL.format(cik=cik),
        request_cfg=request_cfg,
        missing_statuses={404},
        rate_limiter=rate_limiter,
        http_cache=http_cache,
        cache_namespace="companyfacts",
    )
    if payload is None:
        return [], [], 0, 0
    return parse_companyfacts_payload(
        cik=cik,
        payload=payload,
        forms_filter=forms_filter,
        start_date=start_date,
        end_date=end_date,
        mapped_only=mapped_only,
        allowed_tags=allowed_tags,
        preferred_units_by_tag=preferred_units_by_tag,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Tier-1 SEC fundamentals ingestion (submissions + companyfacts) into sec_fundamentals.sqlite."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Path to fundamentals YAML config.",
    )
    parser.add_argument("--mode", choices=["daily", "weekly", "quarterly", "backfill"], default=None)
    parser.add_argument("--start-date", type=str, default=None, help="Override start date (YYYY-MM-DD).")
    parser.add_argument("--end-date", type=str, default=None, help="Override end date (YYYY-MM-DD).")
    parser.add_argument("--max-ciks", type=int, default=None, help="Override max CIK count for this run.")
    parser.add_argument(
        "--fetch-workers",
        type=int,
        default=None,
        help="Parallel fetch worker count for SEC HTTP requests (DB writes remain single-threaded).",
    )
    parser.add_argument("--db-path", type=Path, default=None, help="Override SQLite DB path.")
    parser.add_argument(
        "--user-agent",
        type=str,
        default=None,
        help="SEC identifying User-Agent.",
    )
    return parser.parse_args()


def _fetch_cik_payload(
    *,
    item: dict[str, str],
    state: dict[str, str],
    session_pool: SecSessionPool,
    include_submissions_pages: bool,
    max_submissions_pages: int,
    request_cfg: RequestConfig,
    forms_of_interest: set[str],
    window: RunWindow,
    facts_forms: set[str],
    mapped_only: bool,
    allowed_tags: set[tuple[str, str]],
    preferred_units_by_tag: dict[tuple[str, str], set[str]],
    rate_limiter: SecRequestRateLimiter | None,
    http_cache: SecHttpCache | None,
) -> IngestFetchResult:
    cik = item["cik"]
    ticker = item.get("ticker", "")
    session = session_pool.get()
    try:
        newest_acceptance = state["last_submission_acceptance_datetime"]
        newest_filing_date = state["last_filing_date_seen"]
        collected_rows: dict[str, tuple[Any, ...]] = {}
        base_payload_for_profile: dict[str, Any] | None = None

        for page_name, payload in iter_submission_payloads(
            session,
            cik,
            include_pages=include_submissions_pages,
            max_pages=max_submissions_pages,
            request_cfg=request_cfg,
            rate_limiter=rate_limiter,
            http_cache=http_cache,
        ):
            if base_payload_for_profile is None:
                base_payload_for_profile = payload
            rows, page_acceptance, page_filing_date = extract_submission_rows(
                cik=cik,
                payload=payload,
                source_json_page=page_name,
                forms_of_interest=forms_of_interest,
                start_date=window.start_date,
                end_date=window.end_date,
            )
            for row in rows:
                collected_rows[row[0]] = row
            if page_acceptance > newest_acceptance:
                newest_acceptance = page_acceptance
            if page_filing_date > newest_filing_date:
                newest_filing_date = page_filing_date

        companyfacts_fetched = False
        fact_rows: list[tuple[Any, ...]] = []
        dei_rows: list[tuple[Any, ...]] = []
        dropped_unmapped = 0
        dropped_bad_units = 0

        do_companyfacts = should_refresh_companyfacts(
            window=window,
            new_filing_count=len(collected_rows),
            last_companyfacts_fetch_utc=state["last_companyfacts_fetch_utc"],
        )
        if do_companyfacts:
            fact_rows, dei_rows, dropped_unmapped, dropped_bad_units = load_companyfacts(
                session,
                cik=cik,
                forms_filter=facts_forms,
                start_date=window.start_date,
                end_date=window.end_date,
                mapped_only=mapped_only,
                allowed_tags=allowed_tags,
                preferred_units_by_tag=preferred_units_by_tag,
                request_cfg=request_cfg,
                rate_limiter=rate_limiter,
                http_cache=http_cache,
            )
            companyfacts_fetched = True

        return IngestFetchResult(
            cik=cik,
            ticker=ticker,
            base_payload_for_profile=base_payload_for_profile,
            collected_rows=list(collected_rows.values()),
            newest_acceptance=newest_acceptance,
            newest_filing_date=newest_filing_date,
            companyfacts_fetched=companyfacts_fetched,
            fact_rows=fact_rows,
            dei_rows=dei_rows,
            dropped_unmapped=dropped_unmapped,
            dropped_bad_units=dropped_bad_units,
            error_text=None,
        )
    except Exception as exc:
        return IngestFetchResult(
            cik=cik,
            ticker=ticker,
            base_payload_for_profile=None,
            collected_rows=[],
            newest_acceptance=state["last_submission_acceptance_datetime"],
            newest_filing_date=state["last_filing_date_seen"],
            companyfacts_fetched=False,
            fact_rows=[],
            dei_rows=[],
            error_text=f"{type(exc).__name__}: {exc}",
        )


def run_ingest(args: argparse.Namespace) -> None:
    _, cfg = load_sec_fundamentals_config(args.config)
    db_path = Path(
        args.db_path
        if args.db_path is not None
        else cfg_get(cfg, "db_path", default=str(default_db_path()))
    ).expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    run_mode = (args.mode or str(cfg_get(cfg, "run_mode", default="daily"))).lower()
    as_of = datetime.now(timezone.utc).date()
    window = build_run_window(cfg, run_mode, as_of=as_of)
    if args.start_date:
        window = RunWindow(
            start_date=parse_date(args.start_date) or window.start_date,
            end_date=window.end_date,
            mode=window.mode,
            fetch_companyfacts_on_new_filings_only=window.fetch_companyfacts_on_new_filings_only,
            companyfacts_refresh_days=window.companyfacts_refresh_days,
            max_ciks=window.max_ciks,
        )
    if args.end_date:
        window = RunWindow(
            start_date=window.start_date,
            end_date=parse_date(args.end_date) or window.end_date,
            mode=window.mode,
            fetch_companyfacts_on_new_filings_only=window.fetch_companyfacts_on_new_filings_only,
            companyfacts_refresh_days=window.companyfacts_refresh_days,
            max_ciks=window.max_ciks,
        )
    if args.max_ciks is not None:
        window = RunWindow(
            start_date=window.start_date,
            end_date=window.end_date,
            mode=window.mode,
            fetch_companyfacts_on_new_filings_only=window.fetch_companyfacts_on_new_filings_only,
            companyfacts_refresh_days=window.companyfacts_refresh_days,
            max_ciks=args.max_ciks,
        )
    if window.start_date > window.end_date:
        raise ValueError("start_date cannot be after end_date.")

    request_cfg = build_request_cfg(cfg)
    http_cache_cfg = build_http_cache_cfg(cfg)
    ingestion_cfg = cfg_get(cfg, "ingestion", default={})
    fetch_workers = max(
        1,
        int(args.fetch_workers if args.fetch_workers is not None else cfg_get(ingestion_cfg, "fetch_workers", default=1)),
    )
    include_submissions_pages = bool(cfg_get(ingestion_cfg, "include_submissions_pages", default=True))
    max_submissions_pages = int(cfg_get(ingestion_cfg, "max_submissions_pages_per_cik", default=200))
    chunk_size = int(cfg_get(ingestion_cfg, "batch_commit_size", default=2500))
    mapped_only = bool(cfg_get(ingestion_cfg, "only_tag_mapped", default=False))

    forms_of_interest = {
        str(x).strip().upper()
        for x in cfg_get(ingestion_cfg, "forms_of_interest", default=[])
        if str(x).strip()
    }
    forms_of_interest = _ensure_required_forms(forms_of_interest, label="forms_of_interest")
    facts_forms = {
        str(x).strip().upper()
        for x in cfg_get(ingestion_cfg, "facts_forms", default=[])
        if str(x).strip()
    }
    facts_forms = _ensure_required_forms(facts_forms, label="facts_forms")

    tag_map_path = Path(
        cfg_get(cfg, "tag_map_path", default=str(Path(__file__).resolve().with_name("tier1_tag_map.yaml")))
    )
    if not tag_map_path.is_absolute():
        tag_map_path = (Path(__file__).resolve().parent.parent / tag_map_path).resolve()
    tag_map = load_tag_map(tag_map_path)
    allowed_tags = mapped_tag_set(tag_map)
    preferred_units_by_tag = mapped_tag_unit_map(tag_map)

    max_ciks = window.max_ciks if window.max_ciks > 0 else int(cfg_get(ingestion_cfg, "max_ciks", default=0))
    universe_csv = Path(cfg_get(cfg, "universe_csv", default=str(DEFAULT_UNIVERSE_CSV)))
    if not universe_csv.is_absolute():
        universe_csv = (Path(__file__).resolve().parent.parent / universe_csv).resolve()
    universe_rows = read_universe_rows(universe_csv, max_ciks=max_ciks if max_ciks > 0 else 0)

    user_agent = resolve_user_agent(
        args.user_agent or str(cfg_get(cfg, "user_agent", default=DEFAULT_USER_AGENT))
    )
    session_pool = SecSessionPool(user_agent)
    http_cache = (
        SecHttpCache(http_cache_cfg)
        if http_cache_cfg.enabled and (window.mode != "backfill" or http_cache_cfg.cache_use_for_backfill)
        else None
    )
    rate_limiter = (
        SecRequestRateLimiter(request_cfg.sleep_seconds)
        if fetch_workers > 1 and request_cfg.sleep_seconds > 0
        else None
    )

    conn = connect_sqlite(db_path)
    ensure_required_tables(conn)

    run_id = str(uuid.uuid4())
    upsert_universe_rows(conn, universe_rows)
    upsert_run_log_start(conn, run_id=run_id, mode=window.mode, cik_total=len(universe_rows))
    conn.commit()

    processed = 0
    filing_changes = 0
    fact_changes = 0
    dropped_unmapped_total = 0
    dropped_bad_units_total = 0

    try:
        state_by_cik = {row["cik"]: get_sync_state(conn, row["cik"]) for row in universe_rows}
        if fetch_workers > 1:
            logger.info(
                "Parallel SEC fetch mode enabled: fetch_workers=%d. HTTP fetches run in parallel; SQLite writes remain single-threaded.",
                fetch_workers,
            )
        if http_cache is not None:
            logger.info(
                "SEC HTTP cache enabled: dir=%s submissions_ttl_h=%s companyfacts_ttl_h=%s",
                http_cache_cfg.cache_dir,
                f"{http_cache_cfg.submissions_ttl_hours:g}",
                f"{http_cache_cfg.companyfacts_ttl_hours:g}",
            )
        with ThreadPoolExecutor(max_workers=fetch_workers) as executor:
            future_map = {
                executor.submit(
                    _fetch_cik_payload,
                    item=item,
                    state=state_by_cik[item["cik"]],
                    session_pool=session_pool,
                    include_submissions_pages=include_submissions_pages,
                    max_submissions_pages=max_submissions_pages,
                    request_cfg=request_cfg,
                    forms_of_interest=forms_of_interest,
                    window=window,
                    facts_forms=facts_forms,
                    mapped_only=mapped_only,
                    allowed_tags=allowed_tags,
                    preferred_units_by_tag=preferred_units_by_tag,
                    rate_limiter=rate_limiter,
                    http_cache=http_cache,
                ): item
                for item in universe_rows
            }

            for fut in as_completed(future_map):
                item = future_map[fut]
                cik = item["cik"]
                ticker = item.get("ticker", "")
                processed += 1
                logger.info("[%d/%d] CIK %s %s", processed, len(universe_rows), cik, ticker)
                try:
                    result = fut.result()
                except Exception as exc:
                    err_text = f"{type(exc).__name__}: {exc}"
                    logger.warning("ERROR %s: %s", cik, err_text)
                    upsert_sync_state(
                        conn,
                        cik,
                        last_error_utc=utc_now_iso(),
                        last_error_text=err_text[:1200],
                        run_mode=window.mode,
                    )
                    conn.commit()
                    continue

                if result.error_text:
                    logger.warning("ERROR %s: %s", cik, result.error_text)
                    upsert_sync_state(
                        conn,
                        cik,
                        last_error_utc=utc_now_iso(),
                        last_error_text=result.error_text[:1200],
                        run_mode=window.mode,
                    )
                    conn.commit()
                    continue

                try:
                    if result.base_payload_for_profile is not None:
                        upsert_entity_profile(conn, cik, result.base_payload_for_profile)

                    filing_delta = upsert_submission_rows(conn, result.collected_rows)
                    filing_changes += filing_delta
                    now = utc_now_iso()
                    sync_kwargs: dict[str, Any] = {
                        "last_submission_acceptance_datetime": result.newest_acceptance,
                        "last_submissions_fetch_utc": now,
                        "last_filing_date_seen": result.newest_filing_date,
                        "run_mode": window.mode,
                    }

                    if result.companyfacts_fetched:
                        dropped_unmapped_total += int(result.dropped_unmapped)
                        dropped_bad_units_total += int(result.dropped_bad_units)
                        fact_delta = insert_fact_rows(conn, result.fact_rows, chunk_size=chunk_size)
                        dei_delta = upsert_dei_rows(conn, result.dei_rows)
                        fact_changes += fact_delta + dei_delta
                        sync_kwargs["last_companyfacts_fetch_utc"] = now

                    upsert_sync_state(conn, cik, **sync_kwargs)

                    conn.commit()
                except Exception as exc:
                    conn.rollback()
                    err_text = f"{type(exc).__name__}: {exc}"
                    logger.warning("WRITE ERROR %s: %s", cik, err_text)
                    upsert_sync_state(
                        conn,
                        cik,
                        last_error_utc=utc_now_iso(),
                        last_error_text=err_text[:1200],
                        run_mode=window.mode,
                    )
                    conn.commit()
                    continue

        upsert_run_log_finish(
            conn,
            run_id,
            status="success",
            cik_processed=processed,
            filing_rows_added=filing_changes,
            fact_rows_added=fact_changes,
        )
        conn.commit()
    except Exception as exc:
        logger.exception("Unhandled fatal error in SEC fundamentals ingestion run.")
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        try:
            upsert_run_log_finish(
                conn,
                run_id,
                status="failed",
                cik_processed=processed,
                filing_rows_added=filing_changes,
                fact_rows_added=fact_changes,
                error_text=f"{type(exc).__name__}: {exc}"[:1200],
            )
            conn.commit()
        except Exception as finish_exc:
            logger.warning("Failed to finalize SEC ingestion run log for run_id=%s: %s", run_id, finish_exc)
        raise
    finally:
        session_pool.close_all()
        conn.close()

    logger.info(
        "SEC fundamentals ingest completed: mode=%s window=%s..%s cik_processed=%d filing_changes=%d fact_changes=%d dropped_unmapped=%d dropped_unit_mismatch=%d",
        window.mode,
        window.start_date.isoformat(),
        window.end_date.isoformat(),
        processed,
        filing_changes,
        fact_changes,
        dropped_unmapped_total,
        dropped_bad_units_total,
    )
    if http_cache is not None:
        cache_stats = http_cache.summary()
        logger.info(
            "SEC HTTP cache stats: hits=%d negative_hits=%d misses=%d stale=%d writes=%d errors=%d",
            int(cache_stats.get("hits", 0)),
            int(cache_stats.get("negative_hits", 0)),
            int(cache_stats.get("misses", 0)),
            int(cache_stats.get("stale", 0)),
            int(cache_stats.get("writes", 0)),
            int(cache_stats.get("errors", 0)),
        )


def main() -> None:
    configure_pipeline_logging()
    args = parse_args()
    run_ingest(args)


if __name__ == "__main__":
    main()
