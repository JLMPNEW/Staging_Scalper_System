#!/usr/bin/env python3
"""Sync FDA Advisory Committee (AdCom) meeting calendar to fda_adcom_events table.

Fetches the FDA AdCom calendar from FDA.gov, parses meeting entries, attempts
best-effort ticker matching using company aliases, and upserts to the DB.
Falls back gracefully if the network fetch fails (shadow-only signal; never
a pipeline blocker).
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
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
from biotech_index.core.db import connect, finish_run, init_db, start_run, utc_now  # noqa: E402
from biotech_index.core.logging_utils import configure_utc_logging  # noqa: E402
from biotech_index.core.text_norm import normalize_ticker  # noqa: E402

LOGGER = logging.getLogger("sync_fda_adcom_calendar")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"

# FDA AdCom calendar source — JSON feed published by FDA.gov
FDA_ADCOM_CALENDAR_URL = "https://www.fda.gov/media/advisory-committees/AdvisoryCommitteeCalendar/data.json"

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
_DATE_MDY_RE = re.compile(
    r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\.?\s+(\d{1,2})(?:st|nd|rd|th)?[,\s]+\s*(20\d{2})\b",
    re.IGNORECASE,
)


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
        default=True,
        help="Do not raise if FDA network fetch fails (default: True).",
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


def utc_today() -> date:
    return datetime.now(timezone.utc).date()


def fetch_fda_adcom_json(url: str, *, timeout_sec: int = 30) -> list[dict[str, Any]]:
    """Fetch FDA AdCom calendar JSON. Returns list of raw meeting dicts."""
    req = urllib.request.Request(url, headers={"User-Agent": "biotech-research-pipeline/1.0 (internal)"})
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    data = json.loads(raw)
    if isinstance(data, list):
        return data
    # FDA sometimes wraps in {"data": [...]} or {"meetings": [...]}
    for key in ("data", "meetings", "items", "results"):
        if isinstance(data.get(key), list):
            return data[key]
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
    candidates: list[str] = []
    if company_name:
        candidates.append(_normalize_for_match(company_name))
    if drug_name:
        candidates.append(_normalize_for_match(drug_name))
    for candidate in candidates:
        if candidate in alias_map:
            return alias_map[candidate]
        # Try word-level prefix match (≥3 chars) as fallback
        for alias_key, cid in alias_map.items():
            if len(alias_key) >= 5 and len(candidate) >= 5:
                if alias_key.startswith(candidate[:8]) or candidate.startswith(alias_key[:8]):
                    return cid
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
) -> list[dict[str, Any]]:
    """Parse raw FDA JSON entries into normalized AdCom event dicts."""
    results: list[dict[str, Any]] = []
    for entry in raw_entries:
        # FDA calendar JSON field names vary; try multiple keys
        meeting_date_raw = (
            entry.get("meetingDate") or entry.get("meeting_date") or entry.get("date")
            or entry.get("startDate") or entry.get("start_date") or ""
        )
        meeting_date = parse_date(meeting_date_raw)
        if meeting_date is None:
            continue
        days_until = (meeting_date - asof_date).days
        if days_until < -30 or days_until > lookahead_days:
            # Keep up to 30 days in the past for vote_result capture
            continue
        committee = str(
            entry.get("committee") or entry.get("committeeName") or entry.get("committee_name") or ""
        ).strip()
        drug_name = str(
            entry.get("drugName") or entry.get("drug_name") or entry.get("product") or entry.get("topic") or ""
        ).strip()
        company_name = str(
            entry.get("company") or entry.get("companyName") or entry.get("sponsor") or entry.get("applicant") or ""
        ).strip()
        indication = str(entry.get("indication") or entry.get("topic") or "").strip()
        vote_result = str(entry.get("vote") or entry.get("voteResult") or entry.get("vote_result") or "").strip()
        source_url = str(entry.get("url") or entry.get("sourceUrl") or entry.get("source_url") or "").strip()
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
) -> tuple[int, int]:
    inserted = 0
    updated = 0
    for ev in events:
        if ev["company_id"] is None:
            # Store unmatched entries with company_id=-1 for audit
            ev = {**ev, "company_id": -1}
        existing = conn.execute(
            """
            SELECT event_id FROM fda_adcom_events
            WHERE company_id = ? AND meeting_date = ? AND (drug_name = ? OR (drug_name IS NULL AND ? IS NULL))
            """,
            (ev["company_id"], ev["meeting_date"], ev["drug_name"] or None, ev["drug_name"] or None),
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE fda_adcom_events
                SET ticker=?, committee=?, indication=?, vote_result=?, source=?, source_url=?, updated_at=?
                WHERE event_id=?
                """,
                (
                    ev["ticker"], ev["committee"], ev["indication"],
                    ev["vote_result"], ev["source"], ev["source_url"],
                    now, existing[0],
                ),
            )
            updated += 1
        else:
            conn.execute(
                """
                INSERT INTO fda_adcom_events
                (company_id, ticker, meeting_date, committee, drug_name, indication, vote_result, source, source_url, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    ev["company_id"], ev["ticker"], ev["meeting_date"],
                    ev["committee"], ev["drug_name"], ev["indication"],
                    ev["vote_result"], ev["source"], ev["source_url"],
                    now, now,
                ),
            )
            inserted += 1
    return inserted, updated


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
    calendar_url = str(fda_cfg.get("calendar_url") or FDA_ADCOM_CALENDAR_URL)
    fetch_timeout_sec = int(fda_cfg.get("fetch_timeout_sec") or 30)
    run_id: int | None = None
    with connect(db_path, timeout_sec=sqlite_timeout_sec) as conn:
        init_db(conn)
        try:
            run_id = start_run(conn, run_type="sync_fda_adcom_calendar", input_path=None)
            now = utc_now()
            alias_map = load_company_aliases(conn)
            company_tickers = load_company_tickers(conn)
            raw_entries: list[dict[str, Any]] = []
            if not args.offline:
                try:
                    raw_entries = fetch_fda_adcom_json(calendar_url, timeout_sec=fetch_timeout_sec)
                    LOGGER.info("Fetched %d raw AdCom entries from FDA calendar", len(raw_entries))
                except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError) as exc:
                    if args.allow_fetch_failure:
                        LOGGER.warning("FDA AdCom calendar fetch failed (non-blocking): %s", exc)
                    else:
                        raise
            else:
                LOGGER.info("Offline mode: skipping FDA network fetch")
            events = parse_adcom_entries(
                raw_entries,
                asof_date=asof_date,
                lookahead_days=lookahead_days,
                alias_map=alias_map,
                company_tickers=company_tickers,
            )
            matched = sum(1 for e in events if e["company_id"] is not None)
            LOGGER.info("Parsed %d AdCom entries, %d matched to universe tickers", len(events), matched)
            inserted, updated = upsert_adcom_events(conn, events, now=now)
            conn.commit()
            LOGGER.info("AdCom sync: inserted=%d updated=%d total_events=%d", inserted, updated, len(events))
            finish_run(
                conn,
                run_id=run_id,
                status="success",
                row_count=len(events),
                message=f"inserted={inserted} updated={updated} matched={matched}",
            )
        except BaseException as exc:
            if run_id is not None and not (isinstance(exc, SystemExit) and exc.code in (0, None)):
                finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()
