#!/usr/bin/env python3
"""Sync FDA Advisory Committee (AdCom) meeting calendar to fda_adcom_events table.

Fetches the FDA AdCom calendar from FDA.gov, parses meeting entries, attempts
best-effort ticker matching using company aliases, and upserts to the DB.
Falls back gracefully if the network fetch fails (shadow-only signal; never
a pipeline blocker).
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import re
import sqlite3
import ssl
import sys
import http.cookiejar
import urllib.parse
import urllib.request
import urllib.error
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from biotech_index.core.db import (  # noqa: E402
    connect,
    ensure_table_optional_columns,
    finish_run,
    init_db,
    start_run,
    utc_now,
)
from biotech_index.core.logging_utils import configure_utc_logging  # noqa: E402

LOGGER = logging.getLogger("sync_fda_adcom_calendar")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"

# FDA AdCom calendar source published by FDA.gov.
FDA_ADCOM_CALENDAR_URL = "https://www.fda.gov/datatables-json/advisory-committee-calendar-json"
FDA_ADCOM_LEGACY_CALENDAR_URL = "https://www.fda.gov/media/advisory-committees/AdvisoryCommitteeCalendar/data.json"
FDA_ADCOM_WARMUP_URL = "https://www.fda.gov/advisory-committees/advisory-committee-calendar"

# Oncology-related committee names (case-insensitive substring match)
ONCOLOGY_COMMITTEE_KEYWORDS = frozenset({
    "oncologic",
    "oncology",
    "odac",
    "hematology",
    "hematologic",
})

MONTH_LOOKUP = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

_DATE_ISO_RE = re.compile(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b")
_DATE_NUMERIC_MDY_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(20\d{2})\b")
_DATE_MDY_RE = re.compile(
    r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\.?\s+(\d{1,2})(?:st|nd|rd|th)?[,\s]+\s*(20\d{2})\b",
    re.IGNORECASE,
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HREF_RE = re.compile(r"""href=["']([^"']+)["']""", re.IGNORECASE)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync FDA AdCom calendar to fda_adcom_events table.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", type=str, default="", help="As-of date YYYY-MM-DD. Defaults to UTC today.")
    parser.add_argument("--lookahead-days", type=int, default=365, help="Only import meetings within this many days.")
    parser.add_argument("--offline", action="store_true", help="Skip network fetch; only validates existing DB rows.")
    parser.add_argument(
        "--allow-fetch-failure",
        action="store_true",
        default=None,
        help="Do not raise if FDA network fetch fails. Defaults to fda_adcom_calendar.allow_fetch_failure or true.",
    )
    parser.add_argument(
        "--fail-on-fetch-failure",
        dest="allow_fetch_failure",
        action="store_false",
        help="Raise when all FDA calendar fetch attempts fail.",
    )
    parser.add_argument(
        "--store-vote-results",
        action="store_true",
        help="Persist vote_result values. Defaults to current/future live runs only to avoid historical look-ahead.",
    )
    return parser.parse_args()


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    m = _DATE_ISO_RE.search(text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    m = _DATE_NUMERIC_MDY_RE.search(text)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except ValueError:
            pass
    m = _DATE_MDY_RE.search(text)
    if m:
        month_name = m.group(1).lower()[:3]
        month = MONTH_LOOKUP.get(month_name)
        if month:
            try:
                return date(int(m.group(3)), month, int(m.group(2)))
            except ValueError:
                pass
    return None


def strip_html(raw: object) -> str:
    text = html.unescape(str(raw or ""))
    text = _HTML_TAG_RE.sub(" ", text)
    return " ".join(text.split()).strip()


def extract_href(raw: object) -> str:
    m = _HREF_RE.search(str(raw or ""))
    if not m:
        return ""
    return urllib.parse.urljoin("https://www.fda.gov", html.unescape(m.group(1)))


def utc_today() -> date:
    return datetime.now(timezone.utc).date()


def as_bool(raw: object, default: bool = False) -> bool:
    if raw is None:
        return default
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def build_ssl_context(mode: str = "auto", ca_bundle_path: str = "") -> ssl.SSLContext | None:
    """Build the TLS context used for FDA.gov fetches.

    `auto` prefers the OS trust store via truststore when available, then falls
    back to certifi.  This avoids the common Windows/conda failure where urllib
    cannot validate a chain that the OS trusts, while still keeping certificate
    verification enabled.
    """
    clean_mode = str(mode or "auto").strip().lower()
    ca_path = str(ca_bundle_path or "").strip()
    if clean_mode in {"", "auto"}:
        if ca_path:
            return ssl.create_default_context(cafile=ca_path)
        try:
            import truststore  # type: ignore

            return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        except Exception as exc:
            LOGGER.debug("truststore SSL context unavailable for FDA AdCom fetch: %s", exc)
        try:
            import certifi  # type: ignore

            return ssl.create_default_context(cafile=certifi.where())
        except Exception as exc:
            LOGGER.debug("certifi SSL context unavailable for FDA AdCom fetch: %s", exc)
        return ssl.create_default_context()
    if clean_mode == "truststore":
        import truststore  # type: ignore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    if clean_mode == "certifi":
        import certifi  # type: ignore

        return ssl.create_default_context(cafile=ca_path or certifi.where())
    if clean_mode == "system":
        return ssl.create_default_context(cafile=ca_path or None)
    if clean_mode == "none":
        return None
    if clean_mode == "insecure_no_verify":
        LOGGER.warning("FDA AdCom fetch is using insecure_no_verify SSL mode; use only for local diagnostics.")
        return ssl._create_unverified_context()
    raise ValueError(
        "fda_adcom_calendar.ssl_context must be one of auto, truststore, certifi, system, none, insecure_no_verify; "
        f"got {mode!r}"
    )


def fetch_fda_adcom_json(
    url: str,
    *,
    timeout_sec: int = 30,
    ssl_context: ssl.SSLContext | None = None,
    warmup_url: str = "",
) -> list[dict[str, Any]]:
    """Fetch FDA AdCom calendar JSON. Returns list of raw meeting dicts."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36",
        "Accept": "application/json,text/plain,*/*",
        "Accept-Encoding": "identity",
        "Connection": "close",
        "Referer": warmup_url or "https://www.fda.gov/advisory-committees",
    }
    opener: urllib.request.OpenerDirector | None = None
    if warmup_url:
        cookie_jar = http.cookiejar.CookieJar()
        handlers: list[urllib.request.BaseHandler] = [urllib.request.HTTPCookieProcessor(cookie_jar)]
        if ssl_context is not None:
            handlers.append(urllib.request.HTTPSHandler(context=ssl_context))
        opener = urllib.request.build_opener(*handlers)
        warmup_req = urllib.request.Request(
            warmup_url,
            headers={**headers, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
        )
        with opener.open(warmup_req, timeout=timeout_sec) as warmup_resp:
            warmup_resp.read(2048)
    req = urllib.request.Request(url, headers=headers)
    if opener is not None:
        with opener.open(req, timeout=timeout_sec) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    else:
        with urllib.request.urlopen(req, timeout=timeout_sec, context=ssl_context) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    data = json.loads(raw)
    if isinstance(data, list):
        return data
    # FDA sometimes wraps in {"data": [...]} or {"meetings": [...]}
    for key in ("data", "meetings", "items", "results"):
        if isinstance(data.get(key), list):
            return data[key]
    return []


def fetch_fda_adcom_json_with_ssl_fallback(
    url: str,
    *,
    timeout_sec: int,
    ssl_mode: str,
    ca_bundle_path: str,
    warmup_url: str,
) -> list[dict[str, Any]]:
    clean_mode = str(ssl_mode or "auto").strip().lower()
    modes = [clean_mode]
    if clean_mode == "auto":
        # `auto` first uses truststore when available. If that fails due a local
        # trust-store problem, certifi gives a second secure attempt before the
        # caller treats the fetch as non-blocking.
        modes.extend(["certifi", "system"])
    last_exc: BaseException | None = None
    seen: set[str] = set()
    for mode in modes:
        if mode in seen:
            continue
        seen.add(mode)
        try:
            context = build_ssl_context(mode, ca_bundle_path=ca_bundle_path)
            entries = fetch_fda_adcom_json(
                url,
                timeout_sec=timeout_sec,
                ssl_context=context,
                warmup_url=warmup_url,
            )
            LOGGER.info("FDA AdCom calendar fetch succeeded with ssl_context=%s", mode)
            return entries
        except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError) as exc:
            last_exc = exc
            LOGGER.warning("FDA AdCom calendar fetch failed with ssl_context=%s: %s", mode, exc)
    if last_exc is not None:
        raise last_exc
    return []


def load_company_aliases(conn: sqlite3.Connection) -> dict[str, int]:
    """Return lowercase alias_norm → company_id mapping."""
    rows = conn.execute("SELECT alias_norm, company_id FROM company_aliases").fetchall()
    return {str(r[0] or "").lower(): int(r[1]) for r in rows if r[0]}


def load_company_tickers(conn: sqlite3.Connection) -> dict[int, str]:
    """Return company_id → ticker mapping."""
    rows = conn.execute("SELECT company_id, ticker FROM companies WHERE is_active = 1").fetchall()
    return {int(r[0]): str(r[1] or "").upper() for r in rows if r[0] and r[1]}


def _normalize_for_match(text: str) -> str:
    text = text.lower()
    # Strip common noise tokens
    for noise in (" inc", " inc.", " corp", " corp.", " ltd", " llc", " plc", " sa", " ag", " nv", " bv"):
        text = text.replace(noise, "")
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    return " ".join(text.split())


def match_company_id(
    company_name: str,
    drug_name: str,
    *,
    alias_map: dict[str, int],
) -> int | None:
    """Best-effort match of an AdCom entry to a company_id via alias lookup."""
    candidates: list[tuple[str, str]] = []
    if company_name:
        candidates.append((_normalize_for_match(company_name), "company"))
    if drug_name:
        candidates.append((_normalize_for_match(drug_name), "drug"))
    for candidate, candidate_type in candidates:
        if candidate in alias_map:
            return alias_map[candidate]
        if candidate_type != "company":
            continue
        # Try 8-char prefix match as fallback, but require at least two shared
        # meaningful tokens so a single overlapping word cannot mislink two
        # different companies with similar leading names.
        matched_company_ids: set[int] = set()
        candidate_tokens = {token for token in candidate.split() if len(token) >= 3}
        for alias_key, cid in alias_map.items():
            if len(alias_key) >= 8 and len(candidate) >= 8:
                if alias_key.startswith(candidate[:8]) or candidate.startswith(alias_key[:8]):
                    alias_tokens = {token for token in alias_key.split() if len(token) >= 3}
                    shared_tokens = candidate_tokens & alias_tokens
                    if len(shared_tokens) >= 2:
                        matched_company_ids.add(cid)
                    elif shared_tokens:
                        LOGGER.debug(
                            "Skipping near-match AdCom alias candidate=%r alias=%r shared_tokens=%s",
                            candidate,
                            alias_key,
                            ",".join(sorted(shared_tokens)),
                        )
        if len(matched_company_ids) == 1:
            return next(iter(matched_company_ids))
    return None


def is_oncology_committee(committee: str) -> bool:
    lower = committee.lower()
    return any(kw in lower for kw in ONCOLOGY_COMMITTEE_KEYWORDS)


def parse_adcom_entries(
    raw_entries: list[dict[str, Any]],
    *,
    asof_date: date,
    lookahead_days: int,
    alias_map: dict[str, int],
    company_tickers: dict[int, str],
    include_vote_results: bool = False,
) -> list[dict[str, Any]]:
    """Parse raw FDA JSON entries into normalized AdCom event dicts."""
    results: list[dict[str, Any]] = []
    for entry in raw_entries:
        # FDA calendar JSON field names vary; try multiple keys
        meeting_date_raw = (
            entry.get("meetingDate") or entry.get("meeting_date") or entry.get("date")
            or entry.get("startDate") or entry.get("start_date") or entry.get("field_start_date") or ""
        )
        meeting_date = parse_date(meeting_date_raw)
        if meeting_date is None:
            continue
        days_until = (meeting_date - asof_date).days
        if days_until < -30 or days_until > lookahead_days:
            # Keep up to 30 days in the past for vote_result capture
            continue
        title_html = str(entry.get("title") or "")
        clean_title = strip_html(title_html)
        committee = str(
            entry.get("committee")
            or entry.get("committeeName")
            or entry.get("committee_name")
            or entry.get("field_contributing_office")
            or clean_title
            or ""
        ).strip()
        drug_name = str(
            entry.get("drugName")
            or entry.get("drug_name")
            or entry.get("product")
            or entry.get("topic")
            or clean_title
            or ""
        ).strip()
        company_name = str(
            entry.get("company") or entry.get("companyName") or entry.get("sponsor") or entry.get("applicant") or ""
        ).strip()
        indication = str(entry.get("indication") or entry.get("topic") or clean_title or "").strip()
        # Vote outcomes only become public after the meeting occurs, so even
        # when vote storage is enabled, only persist them for meetings strictly
        # before the asof date to avoid look-ahead in historical runs.
        vote_result = (
            str(entry.get("vote") or entry.get("voteResult") or entry.get("vote_result") or "").strip()
            if include_vote_results and meeting_date < asof_date
            else ""
        )
        source_url = str(entry.get("url") or entry.get("sourceUrl") or entry.get("source_url") or "").strip()
        if not source_url and title_html:
            source_url = extract_href(title_html)
        if source_url:
            source_url = urllib.parse.urljoin("https://www.fda.gov", source_url)
        company_id = match_company_id(company_name, drug_name, alias_map=alias_map)
        ticker = company_tickers.get(company_id, "") if company_id else ""
        results.append({
            "company_id": company_id,
            "ticker": ticker,
            "meeting_date": meeting_date.isoformat(),
            "committee": committee,
            "drug_name": drug_name,
            "indication": indication,
            "vote_result": vote_result,
            "source": "fda_calendar",
            "source_url": source_url,
        })
    return results


def upsert_adcom_events(
    conn: sqlite3.Connection,
    events: list[dict[str, Any]],
    *,
    now: str,
    announced_date: str,
) -> tuple[int, int, int]:
    inserted = 0
    updated = 0
    skipped_unmatched = 0
    with conn:
        conn.execute("UPDATE fda_adcom_events SET drug_name = NULL WHERE drug_name = ''")
        for ev in events:
            if ev["company_id"] is None or int(ev["company_id"]) <= 0:
                # fda_adcom_events is company-linked with a foreign key.  FDA
                # calendar rows that cannot be matched to the biotech universe are
                # intentionally skipped instead of inserting a synthetic company id.
                skipped_unmatched += 1
                continue
            drug_name = str(ev.get("drug_name") or "").strip() or None
            existing = conn.execute(
                """
                SELECT event_id FROM fda_adcom_events
                WHERE company_id = ? AND meeting_date = ? AND (drug_name = ? OR (drug_name IS NULL AND ? IS NULL))
                """,
                (ev["company_id"], ev["meeting_date"], drug_name, drug_name),
            ).fetchone()
            if existing:
                # Preserve previously captured non-empty committee/indication/
                # vote_result values when a rerun (e.g. with votes disabled)
                # supplies empty strings; keep the first-seen announced_date.
                conn.execute(
                    """
                    UPDATE fda_adcom_events
                    SET ticker=?,
                        committee=COALESCE(NULLIF(?, ''), committee),
                        indication=COALESCE(NULLIF(?, ''), indication),
                        vote_result=COALESCE(NULLIF(?, ''), vote_result),
                        source=?, source_url=?,
                        announced_date=COALESCE(announced_date, ?),
                        updated_at=?
                    WHERE event_id=?
                    """,
                    (
                        ev["ticker"], ev["committee"], ev["indication"],
                        ev["vote_result"], ev["source"], ev["source_url"],
                        announced_date, now, existing[0],
                    ),
                )
                updated += 1
            else:
                conn.execute(
                    """
                    INSERT INTO fda_adcom_events
                    (company_id, ticker, meeting_date, committee, drug_name, indication, vote_result, source, source_url, announced_date, created_at, updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        ev["company_id"], ev["ticker"], ev["meeting_date"],
                        ev["committee"], drug_name, ev["indication"],
                        ev["vote_result"], ev["source"], ev["source_url"],
                        announced_date, now, now,
                    ),
                )
                inserted += 1
    return inserted, updated, skipped_unmatched


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    asof_date = parse_date(args.asof) if args.asof else utc_today()
    if asof_date is None:
        raise ValueError(f"Invalid --asof date: {args.asof}")
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    lookahead_days = args.lookahead_days
    sqlite_timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))
    fda_cfg = cfg_get(config, "fda_adcom_calendar", {}) or {}
    allow_fetch_failure = (
        as_bool(fda_cfg.get("allow_fetch_failure"), True)
        if args.allow_fetch_failure is None
        else bool(args.allow_fetch_failure)
    )
    include_vote_results = bool(
        args.store_vote_results
        or (
            asof_date >= utc_today()
            and as_bool(fda_cfg.get("store_vote_results_for_live_runs", True), True)
        )
    )
    calendar_url = str(fda_cfg.get("calendar_url") or FDA_ADCOM_CALENDAR_URL)
    fallback_calendar_urls = [
        str(item).strip()
        for item in (fda_cfg.get("fallback_calendar_urls") or [FDA_ADCOM_LEGACY_CALENDAR_URL])
        if str(item).strip()
    ]
    calendar_urls = []
    for candidate_url in [calendar_url, *fallback_calendar_urls]:
        if candidate_url and candidate_url not in calendar_urls:
            calendar_urls.append(candidate_url)
    fetch_timeout_sec = int(fda_cfg.get("fetch_timeout_sec") or 30)
    ssl_mode = str(fda_cfg.get("ssl_context") or "auto")
    ca_bundle_path = str(fda_cfg.get("ca_bundle_path") or "")
    warmup_url = str(fda_cfg.get("warmup_url") or FDA_ADCOM_WARMUP_URL)
    run_id: int | None = None
    with connect(db_path, timeout_sec=sqlite_timeout_sec) as conn:
        init_db(conn)
        # announced_date records first public visibility of each AdCom event for
        # point-in-time filtering downstream; legacy rows stay NULL until re-seen.
        ensure_table_optional_columns(conn, "fda_adcom_events", {"announced_date": "TEXT"})
        try:
            run_id = start_run(conn, run_type="sync_fda_adcom_calendar", input_path=None)
            now = utc_now()
            alias_map = load_company_aliases(conn)
            company_tickers = load_company_tickers(conn)
            raw_entries: list[dict[str, Any]] = []
            if not args.offline:
                last_fetch_error: BaseException | None = None
                for source_url in calendar_urls:
                    try:
                        raw_entries = fetch_fda_adcom_json_with_ssl_fallback(
                            source_url,
                            timeout_sec=fetch_timeout_sec,
                            ssl_mode=ssl_mode,
                            ca_bundle_path=ca_bundle_path,
                            warmup_url=warmup_url,
                        )
                        LOGGER.info("Fetched %d raw AdCom entries from FDA calendar source=%s", len(raw_entries), source_url)
                        break
                    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError) as exc:
                        last_fetch_error = exc
                        LOGGER.warning("FDA AdCom calendar source failed: source=%s error=%s", source_url, exc)
                if not raw_entries and last_fetch_error is not None:
                    if allow_fetch_failure:
                        LOGGER.warning("FDA AdCom calendar fetch failed (non-blocking): %s", last_fetch_error)
                    else:
                        raise last_fetch_error
            else:
                LOGGER.info("Offline mode: skipping FDA network fetch")
            events = parse_adcom_entries(
                raw_entries,
                asof_date=asof_date,
                lookahead_days=lookahead_days,
                alias_map=alias_map,
                company_tickers=company_tickers,
                include_vote_results=include_vote_results,
            )
            matched = sum(1 for e in events if e["company_id"] is not None)
            LOGGER.info("Parsed %d AdCom entries, %d matched to universe tickers", len(events), matched)
            inserted, updated, skipped_unmatched = upsert_adcom_events(
                conn, events, now=now, announced_date=utc_today().isoformat()
            )
            LOGGER.info(
                "AdCom sync: inserted=%d updated=%d skipped_unmatched=%d total_events=%d",
                inserted,
                updated,
                skipped_unmatched,
                len(events),
            )
            finish_run(
                conn,
                run_id=run_id,
                status="success",
                row_count=len(events),
                message=f"inserted={inserted} updated={updated} skipped_unmatched={skipped_unmatched} matched={matched}",
            )
        except BaseException as exc:
            if run_id is not None and not (isinstance(exc, SystemExit) and exc.code in (0, None)):
                finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()
