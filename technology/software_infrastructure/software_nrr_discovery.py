from __future__ import annotations

import html
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from dedicated_parser.contracts import FilingRef, file_sha256
from technology.software_infrastructure.software_parser_hydration import (
    EXCLUDED_NAMES,
    EXCLUDED_NAME_SUFFIXES,
    TEXT_DOCUMENT_SUFFIXES,
)


MODEL_FAMILY = "software_infrastructure"
NRR_METRIC = "net_revenue_retention"
PERIODIC_FORMS = frozenset(
    {
        "10-K",
        "10-K/A",
        "10-Q",
        "10-Q/A",
        "20-F",
        "20-F/A",
        "40-F",
        "40-F/A",
    }
)
EVENT_FORMS = frozenset({"8-K", "8-K/A", "6-K", "6-K/A"})
REGISTRATION_FORMS = frozenset({"S-1", "S-1/A", "F-1", "F-1/A"})
ALL_FORMS = PERIODIC_FORMS | EVENT_FORMS | REGISTRATION_FORMS
TEXT_SUFFIXES = frozenset({".htm", ".html", ".xhtml", ".txt", ".xml"})
EXACT_NRR_PATTERN = re.compile(
    r"\b(?:net\s+revenue|net\s+dollar|dollar[- ]based\s+net)"
    r"\s+retention\b",
    re.IGNORECASE,
)
ALIAS_NRR_PATTERN = re.compile(
    r"\b(?:"
    r"net\s+retention(?:\s+rate)?|"
    r"dollar[- ]based\s+retention(?:\s+rate)?|"
    r"(?:dollar[- ]based\s+)?net\s+expansion(?:\s+rate)?|"
    r"net\s+dollar\s+expansion(?:\s+rate)?"
    r")\b",
    re.IGNORECASE,
)
PERCENT_NEARBY_PATTERN = re.compile(
    r"(?:\d{1,3}(?:\.\d+)?)\s*(?:%|percent)\b",
    re.IGNORECASE,
)


def _iso(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def load_nrr_applicable_cohorts(path: Path) -> tuple[str, ...]:
    import csv

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    cohorts = sorted(
        {
            str(row.get("cohort_id") or "").strip()
            for row in rows
            if str(row.get("metric_name") or "").strip() == NRR_METRIC
            and str(row.get("applicability") or "").strip()
            in {"universal", "conditional"}
            and str(row.get("cohort_id") or "").strip()
        }
    )
    if not cohorts:
        raise ValueError("NRR applicability has no eligible cohorts")
    return tuple(cohorts)


def load_likely_nrr_tickers(
    conn: sqlite3.Connection,
    *,
    max_tickers: int = 30,
    minimum_historical: int = 8,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        WITH evidence AS (
            SELECT
                ticker,
                SUM(
                    CASE metric_name
                      WHEN 'net_revenue_retention' THEN 100
                      WHEN 'annual_recurring_revenue' THEN 4
                      WHEN 'customer_count_threshold' THEN 2
                      WHEN 'subscription_revenue' THEN 1
                      ELSE 0
                    END
                ) AS disclosure_score,
                COUNT(DISTINCT metric_name) AS supporting_metric_count,
                COUNT(DISTINCT accession_number) AS supporting_accession_count
            FROM sec_parser_metric_evidence_shadow
            WHERE model_family = ?
              AND metric_name IN (
                  'net_revenue_retention',
                  'annual_recurring_revenue',
                  'customer_count_threshold',
                  'subscription_revenue'
              )
            GROUP BY ticker
        ), membership AS (
            SELECT ticker,
                   MAX(CASE WHEN membership_status <> 'active' THEN 1 ELSE 0 END)
                       AS historical_member_flag
            FROM dim_universe_membership
            WHERE model_family = ? AND point_in_time_flag = 1
            GROUP BY ticker
        )
        SELECT evidence.*,
               COALESCE(membership.historical_member_flag, 0)
                   AS historical_member_flag
        FROM evidence
        LEFT JOIN membership ON membership.ticker = evidence.ticker
        ORDER BY evidence.disclosure_score DESC,
                 evidence.supporting_metric_count DESC,
                 evidence.supporting_accession_count DESC,
                 evidence.ticker
        """,
        (MODEL_FAMILY, MODEL_FAMILY),
    ).fetchall()
    candidates = [dict(row) for row in rows]
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in candidates:
        if len(selected) >= min(minimum_historical, max_tickers):
            break
        if not int(row["historical_member_flag"]):
            continue
        ticker = str(row["ticker"])
        selected.append(row)
        seen.add(ticker)
    for row in candidates:
        if len(selected) >= max_tickers:
            break
        ticker = str(row["ticker"])
        if ticker in seen:
            continue
        selected.append(row)
        seen.add(ticker)
    return selected


def limit_nrr_hydration_scope(
    selected_filings: list[dict[str, Any]],
    *,
    likely_tickers: Iterable[str],
    max_years_per_ticker: int = 5,
    event_window_days: int = 21,
) -> set[str]:
    scoped = set(likely_tickers)
    by_ticker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in selected_filings:
        ticker = str(row["ticker"])
        if ticker in scoped:
            by_ticker[ticker].append(row)
    accessions: set[str] = set()
    for ticker in sorted(by_ticker):
        rows = by_ticker[ticker]
        periodic = [
            row for row in rows if str(row["form_type"]) in PERIODIC_FORMS
        ]
        years = sorted(
            {
                parsed.year
                for row in periodic
                if (parsed := _iso(row["filing_date"])) is not None
            },
            reverse=True,
        )[:max_years_per_ticker]
        selected_periodic: list[dict[str, Any]] = []
        for year in years:
            candidates = [
                row
                for row in periodic
                if (parsed := _iso(row["filing_date"])) is not None
                and parsed.year == year
            ]
            if not candidates:
                continue
            chosen = max(
                candidates,
                key=lambda row: (
                    int(str(row["form_type"]).startswith(("10-K", "20-F", "40-F"))),
                    str(row["filing_date"]),
                    str(row["accession_number"]),
                ),
            )
            selected_periodic.append(chosen)
            accessions.add(str(chosen["accession_number"]))
        events = [
            row for row in rows if str(row["form_type"]) in EVENT_FORMS
        ]
        for periodic_row in selected_periodic:
            periodic_date = _iso(periodic_row["filing_date"])
            if periodic_date is None:
                continue
            nearby = [
                row
                for row in events
                if (event_date := _iso(row["filing_date"])) is not None
                and abs((event_date - periodic_date).days)
                <= event_window_days
            ]
            if nearby:
                chosen_event = min(
                    nearby,
                    key=lambda row: (
                        abs(
                            (
                                _iso(row["filing_date"]) - periodic_date  # type: ignore[operator]
                            ).days
                        ),
                        str(row["filing_date"]),
                        str(row["accession_number"]),
                    ),
                )
                accessions.add(str(chosen_event["accession_number"]))
        registrations = [
            row
            for row in rows
            if str(row["form_type"]) in REGISTRATION_FORMS
        ]
        if registrations:
            accessions.add(
                str(
                    min(
                        registrations,
                        key=lambda row: (
                            str(row["filing_date"]),
                            str(row["accession_number"]),
                        ),
                    )["accession_number"]
                )
            )
    return accessions


def load_nrr_filings(
    conn: sqlite3.Connection,
    *,
    start_date: str,
    asof_date: str,
    cohorts: tuple[str, ...],
) -> list[dict[str, Any]]:
    cohort_placeholders = ",".join("?" for _ in cohorts)
    form_placeholders = ",".join("?" for _ in ALL_FORMS)
    rows = conn.execute(
        f"""
        SELECT DISTINCT
            f.ticker,
            f.cik,
            f.accession_number,
            UPPER(f.form_type) AS form_type,
            COALESCE(f.filing_date, '') AS filing_date,
            COALESCE(f.acceptance_datetime, '') AS accepted_at,
            COALESCE(f.report_date, '') AS report_date,
            COALESCE(f.primary_document, '') AS primary_document,
            COALESCE(f.source_id, '') AS source_id,
            COALESCE(t.calibration_cohort_id, '') AS cohort_id,
            COALESCE(m.membership_status, '') AS membership_status
        FROM fact_sec_filing AS f
        JOIN dim_universe_membership AS m
          ON m.model_family = ?
         AND m.ticker = f.ticker
         AND m.point_in_time_flag = 1
         AND m.start_date <= SUBSTR(
               COALESCE(f.acceptance_datetime, f.filing_date), 1, 10
             )
         AND COALESCE(NULLIF(m.end_date, ''), '9999-12-31') >=
             SUBSTR(COALESCE(f.acceptance_datetime, f.filing_date), 1, 10)
        JOIN dim_technology_taxonomy AS t
          ON t.model_family = m.model_family
         AND t.ticker = m.ticker
        WHERE t.calibration_cohort_id IN ({cohort_placeholders})
          AND UPPER(f.form_type) IN ({form_placeholders})
          AND SUBSTR(
                COALESCE(f.acceptance_datetime, f.filing_date), 1, 10
              ) BETWEEN ? AND ?
        ORDER BY f.ticker, f.filing_date, f.accession_number
        """,
        (
            MODEL_FAMILY,
            *cohorts,
            *sorted(ALL_FORMS),
            start_date,
            asof_date,
        ),
    ).fetchall()
    return [dict(row) for row in rows]


def select_nrr_accessions(
    filings: list[dict[str, Any]],
    *,
    event_window_days: int = 21,
) -> list[dict[str, Any]]:
    by_ticker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for filing in filings:
        by_ticker[str(filing["ticker"])].append(filing)
    selected: list[dict[str, Any]] = []
    for ticker in sorted(by_ticker):
        issuer_rows = by_ticker[ticker]
        periodic_dates = [
            parsed
            for row in issuer_rows
            if str(row["form_type"]) in PERIODIC_FORMS
            and (parsed := _iso(row["filing_date"])) is not None
        ]
        foreign_event_quarters: set[tuple[int, int]] = set()
        registration_seen = False
        for row in issuer_rows:
            form = str(row["form_type"])
            filing_date = _iso(row["filing_date"])
            tier = ""
            if form in PERIODIC_FORMS:
                tier = "periodic"
            elif form in EVENT_FORMS and filing_date is not None:
                nearest = min(
                    (
                        abs((filing_date - periodic).days)
                        for periodic in periodic_dates
                    ),
                    default=10_000,
                )
                if nearest <= event_window_days:
                    tier = "earnings_adjacent_event"
                elif form.startswith("6-K") and not periodic_dates:
                    quarter = (
                        filing_date.year,
                        (filing_date.month - 1) // 3 + 1,
                    )
                    if quarter not in foreign_event_quarters:
                        foreign_event_quarters.add(quarter)
                        tier = "foreign_quarterly_event_proxy"
            elif form in REGISTRATION_FORMS and not registration_seen:
                registration_seen = True
                tier = "registration_baseline"
            if not tier:
                continue
            selected.append(
                {
                    **row,
                    "selection_tier": tier,
                    "requested_metric_ids": NRR_METRIC,
                }
            )
    unique: dict[str, dict[str, Any]] = {}
    for row in selected:
        unique.setdefault(str(row["accession_number"]), row)
    return list(unique.values())


def _filing_ref(row: dict[str, Any]) -> FilingRef:
    return FilingRef(
        ticker=str(row["ticker"]),
        cik=str(row.get("cik") or "").strip().zfill(10),
        accession_number=str(row["accession_number"]),
        form_type=str(row["form_type"]),
        filing_date=str(row["filing_date"]),
        accepted_at=str(row["accepted_at"]),
        report_date=str(row["report_date"]),
        primary_document=str(row["primary_document"]),
        source_id=str(row.get("source_id") or ""),
    )


def _cache_complete(directory: Path, accession: str) -> bool:
    index_path = directory / "index.json"
    if not index_path.is_file() or index_path.stat().st_size <= 0:
        return False
    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    items = ((payload.get("directory") or {}).get("item") or [])
    required = {
        str(item.get("name") or "")
        for item in items
        if isinstance(item, dict)
        and Path(str(item.get("name") or "")).suffix.lower()
        in TEXT_DOCUMENT_SUFFIXES
        and str(item.get("name") or "").lower() not in EXCLUDED_NAMES
        and not str(item.get("name") or "").lower().endswith(
            EXCLUDED_NAME_SUFFIXES
        )
    }
    required.add(f"{accession}.txt")
    return all(
        name
        and (directory / name).is_file()
        and (directory / name).stat().st_size > 0
        for name in required
    )


def _cache_inventory(cache_dir: Path) -> dict[str, Path]:
    root = cache_dir / "sec_archive_xbrl"
    if not root.is_dir():
        return {}
    inventory: dict[str, Path] = {}
    for cik_dir in root.iterdir():
        if not cik_dir.is_dir():
            continue
        for accession_dir in cik_dir.iterdir():
            if accession_dir.is_dir():
                inventory[accession_dir.name] = accession_dir
    return inventory


def _candidate_document_names(
    directory: Path,
    filing: FilingRef,
) -> set[str]:
    names = {filing.primary_document} if filing.primary_document else set()
    index_path = directory / "index.json"
    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    items = ((payload.get("directory") or {}).get("item") or [])
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        item_type = str(item.get("type") or "").upper()
        description = str(item.get("description") or "").lower()
        lowered = name.lower()
        if (
            item_type.startswith("EX-99")
            or "earnings" in description
            or "press release" in description
            or "ex99" in lowered
            or "earn" in lowered
            or "pressrelease" in lowered
            or re.search(r"(?:^|[-_])ex(?:hibit)?99", lowered)
        ):
            names.add(name)
    if not names:
        names.add(f"{filing.accession_number}.txt")
    return names


def _text_documents(
    directory: Path | None,
    filing: FilingRef,
) -> Iterable[Path]:
    if directory is None or not directory.is_dir():
        return ()
    names = _candidate_document_names(directory, filing)
    return tuple(
        sorted(
            (
                path
                for name in names
                if (path := directory / name).is_file()
                and path.suffix.lower() in TEXT_SUFFIXES
                and path.stat().st_size <= 25_000_000
            ),
            key=lambda path: path.name.lower(),
        )
    )


def _match_document(path: Path) -> tuple[str, str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "", ""
    searchable = html.unescape(re.sub(r"<[^>]+>", " ", text))
    searchable = re.sub(r"\s+", " ", searchable)
    exact = EXACT_NRR_PATTERN.search(searchable)
    alias = ALIAS_NRR_PATTERN.search(searchable)
    match = exact or alias
    if match is None:
        return "", ""
    nearby = searchable[
        max(0, match.start() - 180) : min(
            len(searchable), match.end() + 300
        )
    ]
    if PERCENT_NEARBY_PATTERN.search(nearby) is None:
        return "", ""
    return (
        "exact_parser_pattern" if exact is not None else "alias_review_pattern",
        " ".join(nearby.split())[:700],
    )


def build_nrr_discovery(
    *,
    selected_filings: list[dict[str, Any]],
    cache_dir: Path,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    document_hits: list[dict[str, Any]] = []
    hydration: list[dict[str, Any]] = []
    source_manifest: list[dict[str, Any]] = []
    cache_inventory = _cache_inventory(cache_dir)
    for row in selected_filings:
        filing = _filing_ref(row)
        directory = cache_inventory.get(
            filing.accession_number.replace("-", "")
        )
        cache_complete = bool(
            directory is not None
            and _cache_complete(directory, filing.accession_number)
        )
        hit_types: set[str] = set()
        for document in _text_documents(directory, filing):
            match_type, excerpt = _match_document(document)
            if not match_type:
                continue
            hit_types.add(match_type)
            hit = {
                "ticker": filing.ticker,
                "cik": filing.cik,
                "cohort_id": row["cohort_id"],
                "membership_status": row["membership_status"],
                "accession_number": filing.accession_number,
                "form_type": filing.form_type,
                "filing_date": filing.filing_date,
                "accepted_at": filing.accepted_at,
                "report_date": filing.report_date,
                "selection_tier": row["selection_tier"],
                "document_name": document.name,
                "match_type": match_type,
                "evidence_excerpt": excerpt,
                "content_sha256": file_sha256(document),
                "local_path": str(document.resolve()),
                "cache_complete_flag": int(cache_complete),
            }
            document_hits.append(hit)
            if match_type != "exact_parser_pattern":
                continue
            source_manifest.append(
                {
                    "ticker": filing.ticker,
                    "cik": filing.cik,
                    "accession_number": filing.accession_number,
                    "document_name": document.name,
                    "content_sha256": hit["content_sha256"],
                    "cache_status": "CACHED_HASHED",
                    "local_path": hit["local_path"],
                    "form_type": filing.form_type,
                    "filing_date": filing.filing_date,
                    "accepted_at": filing.accepted_at,
                    "report_date": filing.report_date,
                    "primary_document": filing.primary_document,
                    "source_id": filing.source_id,
                    "company_currency": "USD",
                    "is_primary": int(
                        document.name == filing.primary_document
                    ),
                    "is_full_submission": int(
                        document.name
                        == f"{filing.accession_number}.txt"
                    ),
                    "source_kind": "technology_nrr_cached_discovery",
                    "requested_metric_ids": NRR_METRIC,
                }
            )
        if not cache_complete:
            hydration.append(
                {
                    "accession_number": filing.accession_number,
                    "ticker": filing.ticker,
                    "cik": filing.cik,
                    "form_type": filing.form_type,
                    "filing_date": filing.filing_date,
                    "selection_tier": row["selection_tier"],
                    "cache_status": "INCOMPLETE_OR_MISSING",
                    "exact_cached_hit_flag": int(
                        "exact_parser_pattern" in hit_types
                    ),
                    "alias_cached_hit_flag": int(
                        "alias_review_pattern" in hit_types
                    ),
                }
            )
    return document_hits, hydration, source_manifest


def existing_nrr_coverage(
    conn: sqlite3.Connection,
) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT ticker, candidate_status, COUNT(*) AS evidence_count,
               MIN(accepted_at) AS first_accepted_at,
               MAX(accepted_at) AS latest_accepted_at
        FROM sec_parser_metric_evidence_shadow
        WHERE model_family = ?
          AND metric_name = ?
        GROUP BY ticker, candidate_status
        ORDER BY ticker, candidate_status
        """,
        (MODEL_FAMILY, NRR_METRIC),
    ).fetchall()
    status_counts: Counter[str] = Counter()
    for row in rows:
        status_counts[str(row["candidate_status"])] += int(
            row["evidence_count"]
        )
    return {
        "existing_evidence_count": sum(status_counts.values()),
        "existing_ticker_count": len(
            {str(row["ticker"]) for row in rows}
        ),
        "existing_status_counts": dict(sorted(status_counts.items())),
        "existing_rows": [dict(row) for row in rows],
    }
