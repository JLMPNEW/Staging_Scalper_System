#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, resolve_optional_path, resolve_path  # noqa: E402
from biotech_index.core.db import connect, init_db  # noqa: E402
from biotech_index.core.logging_utils import configure_utc_logging  # noqa: E402


LOGGER = logging.getLogger("build_forward_catalyst_calendar")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"

FORWARD_EVENT_TYPES = {
    "pdufa_date",
    "nda_bla_accepted",
    "regulatory_submission",
    "clinical_update_positive",
    "endpoint_met",
}
EVENT_TYPE_CONFIDENCE_FLOOR = {
    "pdufa_date": 0.85,
    "nda_bla_accepted": 0.78,
    "regulatory_submission": 0.68,
    "endpoint_met": 0.62,
    "clinical_update_positive": 0.58,
}
OUTPUT_FIELDS = [
    "ticker",
    "company_name",
    "company_id",
    "accession_nodash",
    "filing_date",
    "form",
    "event_type",
    "event_date",
    "days_until",
    "event_value",
    "polarity",
    "confidence",
    "source",
    "source_name",
    "source_url",
    "nct_id",
    "trial_phase",
    "overall_status",
    "document_url",
    "extracted_text",
    "notes",
]
DATE_PATTERN = re.compile(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b")
MONTH_DATE_PATTERN = re.compile(
    r"\b("
    r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?"
    r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?[,]?\s+(20\d{2})\b",
    re.IGNORECASE,
)
MONTH_LOOKUP = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build point-in-time forward catalyst calendar from parsed SEC events.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", type=str, default="", help="As-of date in YYYY-MM-DD. Defaults to UTC today.")
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--lookahead-days", type=int, default=None)
    parser.add_argument(
        "--include-all-companies",
        action="store_true",
        help="Do not restrict output to the final scoring universe CSV.",
    )
    return parser.parse_args()


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    match = DATE_PATTERN.search(text)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    month_match = MONTH_DATE_PATTERN.search(text)
    if month_match:
        month_key = month_match.group(1).strip(".").lower()[:4]
        if month_key not in MONTH_LOOKUP:
            month_key = month_key[:3]
        try:
            return date(int(month_match.group(3)), MONTH_LOOKUP[month_key], int(month_match.group(2)))
        except (KeyError, ValueError):
            return None
    return None


def as_bool(raw: object, default: bool = False) -> bool:
    if raw is None:
        return default
    text = str(raw).strip().lower()
    if text in {"1", "true", "t", "yes", "y", "enabled", "on"}:
        return True
    if text in {"0", "false", "f", "no", "n", "disabled", "off"}:
        return False
    return default


def to_float(raw: object, default: float = 0.0) -> float:
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return value if value == value and value not in {float("inf"), float("-inf")} else default


def normalize_ticker(raw: object) -> str:
    return str(raw or "").strip().upper()


def parse_string_list(raw: object, default: list[str] | None = None) -> list[str]:
    if raw is None:
        return list(default or [])
    if isinstance(raw, str):
        parts = raw.replace(";", ",").replace("|", ",").split(",")
    elif isinstance(raw, (list, tuple, set)):
        parts = [str(item) for item in raw]
    else:
        parts = [str(raw)]
    values = [part.strip() for part in parts if part.strip()]
    return values or list(default or [])


def load_scoring_tickers(path: Path | None) -> set[str]:
    if path is None or not path.exists():
        return set()
    tickers: set[str] = set()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if not as_bool(row.get("scoring_include"), True):
                continue
            ticker = normalize_ticker(row.get("ticker") or row.get("symbol"))
            if ticker:
                tickers.add(ticker)
    return tickers


def confidence_for_event(event_type: str, raw_confidence: object) -> float:
    confidence = to_float(raw_confidence, 0.0)
    if confidence > 2.0:
        confidence /= 100.0
    if confidence <= 0.0:
        return 0.0
    floor = EVENT_TYPE_CONFIDENCE_FLOOR.get(event_type, 0.60)
    return round(max(floor, min(1.0, confidence)), 6)


def normalize_confidence(raw_confidence: object, default: float) -> float:
    confidence = to_float(raw_confidence, default)
    if confidence > 2.0:
        confidence /= 100.0
    return round(max(0.0, min(1.0, confidence)), 6)


def source_priority(source: object) -> int:
    text = str(source or "").strip().lower()
    if text == "sec_events":
        return 0
    if text == "manual_override":
        return 1
    if text.startswith("ctgov"):
        return 2
    return 9


def company_lookup(conn) -> dict[str, dict[str, Any]]:
    rows = conn.execute("SELECT company_id, ticker, company_name FROM companies").fetchall()
    return {
        normalize_ticker(row["ticker"]): {
            "company_id": int(row["company_id"]),
            "company_name": str(row["company_name"] or ""),
        }
        for row in rows
        if normalize_ticker(row["ticker"])
    }


def output_csv_path(config: dict[str, Any], *, base_dir: Path, override: Path | None) -> Path:
    if override is not None:
        return override.expanduser().resolve()
    configured = str(cfg_get(config, "biotech_features.forward_catalyst_calendar_csv", "") or "").strip()
    if configured:
        return resolve_path(configured, base_dir=base_dir)
    output_dir = resolve_path(cfg_get(config, "biotech_features.output_dir", "../output/biotech_index_reports"), base_dir=base_dir)
    return output_dir / "forward_catalyst_calendar.csv"


def load_forward_events(
    conn,
    *,
    asof_date: date,
    lookahead_days: int,
    ticker_filter: set[str],
    diagnostics: Counter[str] | None = None,
) -> list[dict[str, Any]]:
    max_event_date = asof_date + timedelta(days=lookahead_days)
    params: list[Any] = [asof_date.isoformat()]
    event_placeholders = ",".join("?" for _ in FORWARD_EVENT_TYPES)
    params.extend(sorted(FORWARD_EVENT_TYPES))
    ticker_clause = ""
    if ticker_filter:
        ticker_clause = f" AND upper(c.ticker) IN ({','.join('?' for _ in ticker_filter)})"
        params.extend(sorted(ticker_filter))
    rows = conn.execute(
        f"""
        SELECT
            c.ticker,
            c.company_name,
            e.company_id,
            e.accession_nodash,
            e.filing_date,
            e.form,
            lower(e.event_type) AS event_type,
            e.event_date,
            e.event_value,
            e.polarity,
            e.confidence,
            e.extracted_text,
            COALESCE(f.archive_url, '') AS document_url
        FROM sec_events e
        JOIN companies c ON c.company_id = e.company_id
        LEFT JOIN sec_filings f ON f.accession_nodash = e.accession_nodash
        WHERE e.filing_date <= ?
          AND lower(e.event_type) IN ({event_placeholders})
          {ticker_clause}
        ORDER BY c.ticker, e.filing_date DESC, e.event_type, e.accession_nodash
        """,
        params,
    ).fetchall()
    output: list[dict[str, Any]] = []
    for row in rows:
        event_type = str(row["event_type"] or "").strip().lower()
        event_date = parse_date(row["event_date"])
        if event_date is None:
            if diagnostics is not None:
                diagnostics["sec_events_unparseable_event_date"] += 1
            continue
        days_until = (event_date - asof_date).days
        if days_until < 0 or event_date > max_event_date:
            continue
        output.append(
            {
                "ticker": normalize_ticker(row["ticker"]),
                "company_name": str(row["company_name"] or ""),
                "company_id": int(row["company_id"]),
                "accession_nodash": str(row["accession_nodash"] or ""),
                "filing_date": str(row["filing_date"] or ""),
                "form": str(row["form"] or ""),
                "event_type": event_type,
                "event_date": event_date.isoformat(),
                "days_until": days_until,
                "event_value": str(row["event_value"] or ""),
                "polarity": str(row["polarity"] or ""),
                "confidence": confidence_for_event(event_type, row["confidence"]),
                "source": "sec_events",
                "source_name": "SEC parsed event",
                "source_url": str(row["document_url"] or ""),
                "nct_id": "",
                "trial_phase": "",
                "overall_status": "",
                "document_url": str(row["document_url"] or ""),
                "extracted_text": str(row["extracted_text"] or ""),
                "notes": "",
            }
        )
    output.sort(
        key=lambda item: (
            str(item["ticker"]),
            int(item["days_until"]),
            source_priority(item.get("source")),
            str(item["event_type"]),
            str(item["accession_nodash"]),
        )
    )
    return output


def load_manual_overrides(
    path: Path | None,
    *,
    asof_date: date,
    lookahead_days: int,
    ticker_filter: set[str],
    companies_by_ticker: dict[str, dict[str, Any]],
    default_confidence: float,
    diagnostics: Counter[str] | None = None,
) -> list[dict[str, Any]]:
    if path is None or not path.exists():
        return []
    max_event_date = asof_date + timedelta(days=lookahead_days)
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for idx, record in enumerate(csv.DictReader(handle), start=2):
            if not as_bool(record.get("active"), True):
                continue
            ticker = normalize_ticker(record.get("ticker") or record.get("symbol"))
            if not ticker or (ticker_filter and ticker not in ticker_filter):
                continue
            asof_start = parse_date(record.get("asof_start") or record.get("start_asof"))
            asof_end = parse_date(record.get("asof_end") or record.get("end_asof"))
            if asof_start is not None and asof_date < asof_start:
                continue
            if asof_end is not None and asof_date > asof_end:
                continue
            event_date = parse_date(record.get("event_date") or record.get("catalyst_date") or record.get("date"))
            if event_date is None:
                if diagnostics is not None:
                    diagnostics["manual_override_unparseable_event_date"] += 1
                LOGGER.warning("Skipping manual catalyst override row=%d ticker=%s with invalid event_date", idx, ticker)
                continue
            days_until = (event_date - asof_date).days
            if days_until < 0 or event_date > max_event_date:
                continue
            company = companies_by_ticker.get(ticker, {})
            source_url = str(record.get("source_url") or record.get("url") or record.get("document_url") or "")
            event_type = str(record.get("event_type") or record.get("catalyst_type") or "manual_catalyst").strip().lower()
            rows.append(
                {
                    "ticker": ticker,
                    "company_name": str(company.get("company_name") or record.get("company_name") or ""),
                    "company_id": company.get("company_id", ""),
                    "accession_nodash": "",
                    "filing_date": str(record.get("filing_date") or ""),
                    "form": "",
                    "event_type": event_type,
                    "event_date": event_date.isoformat(),
                    "days_until": days_until,
                    "event_value": str(record.get("event_value") or record.get("notes") or ""),
                    "polarity": str(record.get("polarity") or "positive"),
                    "confidence": normalize_confidence(record.get("confidence") or record.get("confidence_pct"), default_confidence),
                    "source": "manual_override",
                    "source_name": str(record.get("source_name") or "Manual catalyst override"),
                    "source_url": source_url,
                    "nct_id": str(record.get("nct_id") or ""),
                    "trial_phase": str(record.get("trial_phase") or record.get("phase") or ""),
                    "overall_status": str(record.get("overall_status") or ""),
                    "document_url": source_url,
                    "extracted_text": "",
                    "notes": str(record.get("notes") or ""),
                }
            )
    return rows


def ctgov_event_type(phase_text: object) -> str:
    phase = str(phase_text or "").strip().lower().replace(" ", "")
    if "phase3" in phase or "phase2/phase3" in phase or "phase2|phase3" in phase:
        return "ctgov_primary_completion_phase3"
    if "phase2" in phase:
        return "ctgov_primary_completion_phase2"
    if "phase1" in phase:
        return "ctgov_primary_completion_phase1"
    return "ctgov_primary_completion"


def ctgov_confidence(
    *,
    phase_text: object,
    match_role: object,
    link_confidence: object,
    settings: dict[str, Any],
) -> float:
    event_type = ctgov_event_type(phase_text)
    if event_type.endswith("phase3"):
        default = float(settings.get("phase3_confidence", 0.55))
    elif event_type.endswith("phase2"):
        default = float(settings.get("phase2_confidence", 0.45))
    else:
        default = float(settings.get("default_confidence", 0.38))
    confidence = normalize_confidence(None, default)
    role = str(match_role or "").strip().lower()
    if "collab" in role:
        confidence *= float(settings.get("collaborator_confidence_multiplier", 0.85))
    link = normalize_confidence(link_confidence, 1.0)
    confidence *= max(0.50, min(1.0, link))
    return round(max(0.0, min(1.0, confidence)), 6)


def load_ctgov_forward_events(
    conn,
    *,
    asof_date: date,
    lookahead_days: int,
    ticker_filter: set[str],
    settings: dict[str, Any],
    diagnostics: Counter[str] | None = None,
) -> list[dict[str, Any]]:
    if not as_bool(settings.get("enabled", True), True):
        return []
    max_event_date = asof_date + timedelta(days=lookahead_days)
    active_statuses = {
        value.strip().upper()
        for value in parse_string_list(
            settings.get("active_statuses"),
            ["RECRUITING", "ACTIVE_NOT_RECRUITING", "ENROLLING_BY_INVITATION", "NOT_YET_RECRUITING"],
        )
    }
    min_link_confidence = float(settings.get("min_link_confidence", 0.60))
    params: list[Any] = [asof_date.isoformat()]
    ticker_clause = ""
    if ticker_filter:
        ticker_clause = f" AND upper(c.ticker) IN ({','.join('?' for _ in ticker_filter)})"
        params.extend(sorted(ticker_filter))
    rows = conn.execute(
        f"""
        WITH latest_snapshot AS (
            SELECT nct_id, MAX(asof_date) AS snapshot_asof
            FROM trial_snapshot_daily
            WHERE asof_date <= ?
            GROUP BY nct_id
        )
        SELECT
            c.ticker,
            c.company_name,
            c.company_id,
            l.nct_id,
            l.match_role,
            l.confidence AS link_confidence,
            t.brief_title,
            t.phase_text,
            COALESCE(s.overall_status, t.overall_status, '') AS overall_status,
            s.primary_completion_date,
            s.asof_date AS snapshot_asof
        FROM latest_snapshot ls
        JOIN trial_snapshot_daily s
          ON s.nct_id = ls.nct_id AND s.asof_date = ls.snapshot_asof
        JOIN trials t ON t.nct_id = s.nct_id
        JOIN trial_company_links l ON l.nct_id = s.nct_id
        JOIN companies c ON c.company_id = l.company_id
        WHERE s.primary_completion_date IS NOT NULL
          AND s.primary_completion_date != ''
          AND l.confidence >= ?
          {ticker_clause}
        """,
        [params[0], min_link_confidence, *params[1:]],
    ).fetchall()
    deduped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        status = str(row["overall_status"] or "").strip().upper()
        if active_statuses and status not in active_statuses:
            continue
        event_date = parse_date(row["primary_completion_date"])
        if event_date is None:
            if diagnostics is not None:
                diagnostics["ctgov_unparseable_primary_completion_date"] += 1
            continue
        days_until = (event_date - asof_date).days
        if days_until < 0 or event_date > max_event_date:
            continue
        event_type = ctgov_event_type(row["phase_text"])
        confidence = ctgov_confidence(
            phase_text=row["phase_text"],
            match_role=row["match_role"],
            link_confidence=row["link_confidence"],
            settings=settings,
        )
        nct_id = str(row["nct_id"] or "")
        source_url = f"https://clinicaltrials.gov/study/{nct_id}" if nct_id else ""
        candidate = {
            "ticker": normalize_ticker(row["ticker"]),
            "company_name": str(row["company_name"] or ""),
            "company_id": int(row["company_id"]),
            "accession_nodash": "",
            "filing_date": str(row["snapshot_asof"] or ""),
            "form": "",
            "event_type": event_type,
            "event_date": event_date.isoformat(),
            "days_until": days_until,
            "event_value": str(row["brief_title"] or ""),
            "polarity": "positive",
            "confidence": confidence,
            "source": "ctgov_primary_completion",
            "source_name": "ClinicalTrials.gov primary completion date",
            "source_url": source_url,
            "nct_id": nct_id,
            "trial_phase": str(row["phase_text"] or ""),
            "overall_status": status,
            "document_url": source_url,
            "extracted_text": "",
            "notes": (
                "Lower-confidence timing proxy; primary completion date is not guaranteed readout date."
            ),
        }
        key = (str(candidate["ticker"]), nct_id, str(candidate["event_date"]))
        current = deduped.get(key)
        if current is None or float(candidate["confidence"]) > float(current.get("confidence", 0.0)):
            deduped[key] = candidate
    return sorted(
        deduped.values(),
        key=lambda item: (
            str(item["ticker"]),
            int(item["days_until"]),
            source_priority(item.get("source")),
            str(item["event_type"]),
            str(item["nct_id"]),
        ),
    )


def write_calendar(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True), encoding="utf-8")


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    asof_date = parse_date(args.asof) if args.asof else datetime.now(timezone.utc).date()
    if asof_date is None:
        raise ValueError(f"Invalid --asof date: {args.asof}")
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    lookahead_days = int(args.lookahead_days or cfg_get(config, "biotech_features.forward_catalyst_calendar_lookahead_days", 365))
    if lookahead_days <= 0:
        raise ValueError("--lookahead-days must be positive")
    output_path = output_csv_path(config, base_dir=base_dir, override=args.output_csv)
    overrides_path = resolve_optional_path(
        cfg_get(config, "biotech_features.forward_catalyst_overrides_csv", ""),
        base_dir=base_dir,
    )
    ctgov_settings = cfg_get(config, "biotech_features.forward_catalyst_ctgov", {}) or {}
    if not isinstance(ctgov_settings, dict):
        ctgov_settings = {}
    manual_default_confidence = float(cfg_get(config, "biotech_features.forward_catalyst_manual_default_confidence", 0.80))
    universe_path = resolve_path(
        cfg_get(config, "biotech_features.final_scoring_universe_csv", "../output/biotech_index_reports/ctgov_final_scoring_universe.csv"),
        base_dir=base_dir,
    )
    ticker_filter = set() if args.include_all_companies else load_scoring_tickers(universe_path)
    if not ticker_filter and not args.include_all_companies:
        LOGGER.warning("No scoring universe tickers loaded from %s; exporting all companies with forward SEC catalysts.", universe_path)

    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        companies_by_ticker = company_lookup(conn)
        diagnostics: Counter[str] = Counter()
        sec_rows = load_forward_events(
            conn,
            asof_date=asof_date,
            lookahead_days=lookahead_days,
            ticker_filter=ticker_filter,
            diagnostics=diagnostics,
        )
        manual_rows = load_manual_overrides(
            overrides_path,
            asof_date=asof_date,
            lookahead_days=lookahead_days,
            ticker_filter=ticker_filter,
            companies_by_ticker=companies_by_ticker,
            default_confidence=manual_default_confidence,
            diagnostics=diagnostics,
        )
        ctgov_rows = load_ctgov_forward_events(
            conn,
            asof_date=asof_date,
            lookahead_days=lookahead_days,
            ticker_filter=ticker_filter,
            settings=ctgov_settings,
            diagnostics=diagnostics,
        )
    rows = [*sec_rows, *manual_rows, *ctgov_rows]
    rows.sort(
        key=lambda item: (
            str(item["ticker"]),
            int(item["days_until"]),
            source_priority(item.get("source")),
            str(item["event_type"]),
            str(item.get("accession_nodash") or item.get("nct_id") or ""),
        )
    )
    write_calendar(output_path, rows)
    manifest_path = output_path.with_name(f"{output_path.stem}_manifest.json")
    write_manifest(
        manifest_path,
        {
            "asof_date": asof_date.isoformat(),
            "lookahead_days": lookahead_days,
            "output_csv": str(output_path),
            "event_count": len(rows),
            "ticker_count": len({str(row["ticker"]) for row in rows}),
            "source_event_counts": {
                "sec_events": len(sec_rows),
                "manual_override": len(manual_rows),
                "ctgov_primary_completion": len(ctgov_rows),
            },
            "dropped_event_counts": dict(sorted(diagnostics.items())),
        },
    )
    ticker_count = len({str(row["ticker"]) for row in rows})
    if diagnostics:
        LOGGER.warning("Forward catalyst dropped-event diagnostics: %s", dict(sorted(diagnostics.items())))
    LOGGER.info(
        "Wrote %d forward catalyst events for %d tickers to %s asof=%s lookahead_days=%d sec=%d manual=%d ctgov=%d manifest=%s",
        len(rows),
        ticker_count,
        output_path,
        asof_date.isoformat(),
        lookahead_days,
        len(sec_rows),
        len(manual_rows),
        len(ctgov_rows),
        manifest_path,
    )


if __name__ == "__main__":
    main()
