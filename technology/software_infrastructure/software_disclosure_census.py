from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

from dedicated_parser.catalog import accession_directory, relevant_document_names
from dedicated_parser.contracts import FilingRef, file_sha256
from technology.software_infrastructure.dedicated_parser_adapter import (
    ADAPTER_VERSION,
    get_registry,
)
from technology.software_infrastructure.software_nrr_discovery import (
    EVENT_FORMS,
    MODEL_FAMILY,
    PERIODIC_FORMS,
    load_nrr_filings,
)


CENSUS_METRICS = (
    "annual_recurring_revenue",
    "net_revenue_retention",
    "disclosed_billings",
    "subscription_revenue",
)
CENSUS_MAX_AGE_DAYS = 200
YOY_PAIR_MIN_DAYS = 300
YOY_PAIR_MAX_DAYS = 430


def _iso(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def load_historical_universe(
    conn: sqlite3.Connection,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            m.ticker,
            MAX(COALESCE(t.calibration_cohort_id, '')) AS cohort_id,
            MIN(m.start_date) AS first_membership_date,
            MAX(COALESCE(NULLIF(m.end_date, ''), '9999-12-31'))
                AS last_membership_date,
            MAX(CASE WHEN m.membership_status <> 'active' THEN 1 ELSE 0 END)
                AS historical_member_flag
        FROM dim_universe_membership AS m
        LEFT JOIN dim_technology_taxonomy AS t
          ON t.model_family = m.model_family AND t.ticker = m.ticker
        WHERE m.model_family = ? AND m.point_in_time_flag = 1
        GROUP BY m.ticker
        ORDER BY m.ticker
        """,
        (MODEL_FAMILY,),
    ).fetchall()
    return [dict(row) for row in rows]


def select_recent_earnings_events(
    filings: list[dict[str, Any]],
    *,
    max_events_per_ticker: int = 4,
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
        candidates: list[dict[str, Any]] = []
        foreign_by_quarter: dict[tuple[int, int], dict[str, Any]] = {}
        for row in issuer_rows:
            form = str(row["form_type"])
            filing_date = _iso(row["filing_date"])
            if form not in EVENT_FORMS or filing_date is None:
                continue
            if form.startswith("6-K"):
                quarter = (
                    filing_date.year,
                    (filing_date.month - 1) // 3 + 1,
                )
                existing = foreign_by_quarter.get(quarter)
                if existing is None or str(row["filing_date"]) > str(
                    existing["filing_date"]
                ):
                    foreign_by_quarter[quarter] = {
                        **row,
                        "selection_tier": "foreign_quarterly_event_proxy",
                    }
                continue
            nearest_periodic_days = min(
                (
                    abs((filing_date - periodic).days)
                    for periodic in periodic_dates
                ),
                default=10_000,
            )
            if nearest_periodic_days <= event_window_days:
                candidates.append(
                    {
                        **row,
                        "selection_tier": "earnings_adjacent_event",
                        "nearest_periodic_filing_days": nearest_periodic_days,
                    }
                )
        candidates.extend(foreign_by_quarter.values())
        candidates.sort(
            key=lambda row: (
                str(row["filing_date"]),
                str(row["accepted_at"]),
                str(row["accession_number"]),
            ),
            reverse=True,
        )
        for rank, row in enumerate(
            candidates[: max(1, max_events_per_ticker)],
            start=1,
        ):
            selected.append({**row, "recency_rank": rank})
    return selected


def load_recent_earnings_events(
    conn: sqlite3.Connection,
    *,
    start_date: str,
    asof_date: str,
    max_events_per_ticker: int = 4,
    event_window_days: int = 21,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    universe = load_historical_universe(conn)
    cohorts = tuple(
        sorted(
            {
                str(row["cohort_id"])
                for row in universe
                if str(row["cohort_id"])
            }
        )
    )
    if not cohorts:
        raise RuntimeError("Software disclosure census found no cohorts")
    filings = load_nrr_filings(
        conn,
        start_date=start_date,
        asof_date=asof_date,
        cohorts=cohorts,
    )
    return universe, select_recent_earnings_events(
        filings,
        max_events_per_ticker=max_events_per_ticker,
        event_window_days=event_window_days,
    )


def _filing_ref(row: dict[str, Any]) -> FilingRef:
    return FilingRef(
        ticker=str(row["ticker"]),
        cik=str(row.get("cik") or "").zfill(10),
        accession_number=str(row["accession_number"]),
        form_type=str(row["form_type"]),
        filing_date=str(row["filing_date"]),
        accepted_at=str(row.get("accepted_at") or ""),
        report_date=str(row.get("report_date") or ""),
        primary_document=str(row.get("primary_document") or ""),
        source_id=str(row.get("source_id") or ""),
    )


def build_cache_scope(
    selected: list[dict[str, Any]],
    *,
    cache_dir: Path,
    asof_date: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    registry = get_registry()
    keywords = tuple(
        dict.fromkeys(
            (
                *registry.document_keywords,
                *CENSUS_METRICS,
            )
        )
    )
    source_rows: list[dict[str, Any]] = []
    accession_rows: list[dict[str, Any]] = []
    for row in selected:
        filing = _filing_ref(row)
        directory = accession_directory(cache_dir, filing)
        names = (
            relevant_document_names(
                directory,
                filing=filing,
                keywords=keywords,
            )
            if directory.is_dir()
            else ()
        )
        cached = bool(names)
        accession_rows.append(
            {
                **row,
                "cache_status": "CACHED_HASHED" if cached else "MISSING_CACHE",
                "cached_document_count": len(names),
            }
        )
        for name in names:
            document = directory / name
            source_rows.append(
                {
                    "ticker": filing.ticker,
                    "cik": filing.cik,
                    "accession_number": filing.accession_number,
                    "document_name": name,
                    "content_sha256": file_sha256(document),
                    "cache_status": "CACHED_HASHED",
                    "local_path": str(document.resolve()),
                    "form_type": filing.form_type,
                    "filing_date": filing.filing_date,
                    "accepted_at": filing.accepted_at,
                    "report_date": filing.report_date,
                    "primary_document": filing.primary_document,
                    "source_id": filing.source_id,
                    "company_currency": "USD",
                    "is_primary": int(name == filing.primary_document),
                    "is_full_submission": int(
                        name == f"{filing.accession_number}.txt"
                    ),
                    "source_kind": "technology_software_disclosure_census",
                    "requested_metric_ids": "|".join(CENSUS_METRICS),
                    "asof_date": asof_date,
                }
            )
    return accession_rows, source_rows


def _selected_accessions(rows: Iterable[dict[str, Any]]) -> list[str]:
    return sorted({str(row["accession_number"]) for row in rows})


def load_parser_completion(
    conn: sqlite3.Connection,
    *,
    accession_rows: list[dict[str, Any]],
) -> set[str]:
    accessions = _selected_accessions(accession_rows)
    if not accessions:
        return set()
    placeholders = ",".join("?" for _ in accessions)
    rows = conn.execute(
        f"""
        SELECT DISTINCT accession_number
        FROM sec_parser_work_ledger
        WHERE model_family = ?
          AND adapter_version = ?
          AND status = 'COMPLETED'
          AND accession_number IN ({placeholders})
        """,
        (MODEL_FAMILY, ADAPTER_VERSION, *accessions),
    ).fetchall()
    return {str(row["accession_number"]) for row in rows}


def load_metric_evidence(
    conn: sqlite3.Connection,
    *,
    accession_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    accessions = _selected_accessions(accession_rows)
    if not accessions:
        return []
    placeholders = ",".join("?" for _ in accessions)
    metric_placeholders = ",".join("?" for _ in CENSUS_METRICS)
    rows = conn.execute(
        f"""
        SELECT ticker, accession_number, metric_name, candidate_value,
               candidate_status, status_reason, period_end, scope,
               evidence_key
        FROM sec_parser_metric_evidence_shadow
        WHERE model_family = ?
          AND accession_number IN ({placeholders})
          AND metric_name IN ({metric_placeholders})
        """,
        (MODEL_FAMILY, *accessions, *CENSUS_METRICS),
    ).fetchall()
    return [dict(row) for row in rows]


def _candidate_event_windows(
    *,
    accession_rows: list[dict[str, Any]],
    evidence_rows: list[dict[str, Any]],
    metric_name: str,
) -> tuple[
    dict[str, list[tuple[date, date]]],
    dict[str, list[tuple[date, date]]],
]:
    metadata = {
        (str(row["ticker"]), str(row["accession_number"])): row
        for row in accession_rows
    }
    events: dict[str, set[tuple[date, date]]] = defaultdict(set)
    for evidence in evidence_rows:
        if (
            str(evidence["metric_name"]) != metric_name
            or evidence.get("candidate_value") is None
            or str(evidence.get("candidate_status"))
            not in {"ACCEPTED", "REVIEW_REQUIRED"}
        ):
            continue
        ticker = str(evidence["ticker"])
        accession = str(evidence["accession_number"])
        event = metadata.get((ticker, accession))
        if event is None:
            continue
        available_date = _iso(event.get("accepted_at"))
        period_date = (
            _iso(evidence.get("period_end"))
            or _iso(event.get("report_date"))
            or _iso(event.get("filing_date"))
        )
        if available_date is not None and period_date is not None:
            events[ticker].add((period_date, available_date))

    level_windows = {
        ticker: sorted(values) for ticker, values in events.items()
    }
    yoy_windows: dict[str, list[tuple[date, date]]] = {}
    for ticker, values in level_windows.items():
        pair_events: list[tuple[date, date]] = []
        for period_date, available_date in values:
            if any(
                YOY_PAIR_MIN_DAYS
                <= (period_date - prior_period).days
                <= YOY_PAIR_MAX_DAYS
                and prior_available <= available_date
                for prior_period, prior_available in values
                if prior_period < period_date
            ):
                pair_events.append((period_date, available_date))
        if pair_events:
            yoy_windows[ticker] = pair_events
    return level_windows, yoy_windows


def _max_contemporaneous_coverage(
    windows: dict[str, list[tuple[date, date]]],
    *,
    max_age_days: int,
) -> tuple[int, str]:
    evaluation_dates = sorted(
        {
            available_date
            for ticker_windows in windows.values()
            for _, available_date in ticker_windows
        }
    )
    best_count = 0
    best_date = ""
    for evaluation_date in evaluation_dates:
        count = sum(
            any(
                available_date <= evaluation_date
                <= available_date + timedelta(days=max_age_days)
                for _, available_date in ticker_windows
            )
            for ticker_windows in windows.values()
        )
        if count >= best_count:
            best_count = count
            best_date = evaluation_date.isoformat()
    return best_count, best_date


def build_metric_census(
    *,
    universe: list[dict[str, Any]],
    accession_rows: list[dict[str, Any]],
    completed_accessions: set[str],
    evidence_rows: list[dict[str, Any]],
    max_events_per_ticker: int,
    max_age_days: int = CENSUS_MAX_AGE_DAYS,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    accessions_by_ticker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in accession_rows:
        accessions_by_ticker[str(row["ticker"])].append(row)
    evidence_by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in evidence_rows:
        evidence_by_key[(str(row["ticker"]), str(row["metric_name"]))].append(row)

    candidate_windows = {
        metric: _candidate_event_windows(
            accession_rows=accession_rows,
            evidence_rows=evidence_rows,
            metric_name=metric,
        )
        for metric in CENSUS_METRICS
    }
    detail: list[dict[str, Any]] = []
    for member in universe:
        ticker = str(member["ticker"])
        events = accessions_by_ticker[ticker]
        event_accessions = {str(row["accession_number"]) for row in events}
        cached_accessions = {
            str(row["accession_number"])
            for row in events
            if row["cache_status"] == "CACHED_HASHED"
        }
        parsed_accessions = event_accessions & completed_accessions
        for metric in CENSUS_METRICS:
            evidence = evidence_by_key[(ticker, metric)]
            numeric_accessions = {
                str(row["accession_number"])
                for row in evidence
                if row.get("candidate_value") is not None
            }
            accepted_accessions = {
                str(row["accession_number"])
                for row in evidence
                if row.get("candidate_value") is not None
                and row.get("candidate_status") == "ACCEPTED"
            }
            review_accessions = {
                str(row["accession_number"])
                for row in evidence
                if row.get("candidate_value") is not None
                and row.get("candidate_status") == "REVIEW_REQUIRED"
            }
            rejected_accessions = {
                str(row["accession_number"])
                for row in evidence
                if row.get("candidate_value") is not None
                and row.get("candidate_status") == "REJECTED_POLICY"
            }
            policy_candidate_accessions = accepted_accessions | review_accessions
            selected_count = len(event_accessions)
            complete = bool(selected_count) and parsed_accessions == event_accessions
            detail.append(
                {
                    "ticker": ticker,
                    "cohort_id": member["cohort_id"],
                    "historical_member_flag": member["historical_member_flag"],
                    "metric_name": metric,
                    "target_event_count": max_events_per_ticker,
                    "selected_earnings_event_count": selected_count,
                    "cached_earnings_event_count": len(cached_accessions),
                    "parsed_earnings_event_count": len(parsed_accessions),
                    "raw_numeric_candidate_event_count": len(numeric_accessions),
                    "policy_candidate_numeric_event_count": len(
                        policy_candidate_accessions
                    ),
                    "accepted_numeric_event_count": len(accepted_accessions),
                    "review_required_numeric_event_count": len(review_accessions),
                    "rejected_policy_numeric_event_count": len(
                        rejected_accessions
                    ),
                    "census_complete_flag": int(complete),
                    "level_signal_candidate_flag": int(
                        bool(policy_candidate_accessions)
                    ),
                    "longitudinal_pair_candidate_flag": int(
                        len(policy_candidate_accessions) >= 2
                    ),
                    "year_ago_pair_candidate_flag": int(
                        ticker in candidate_windows[metric][1]
                    ),
                    "every_observed_event_discloses_flag": int(
                        bool(selected_count)
                        and len(policy_candidate_accessions) == selected_count
                    ),
                    "coverage_status": (
                        "NO_QUALIFYING_EARNINGS_EVENT"
                        if not selected_count
                        else "MISSING_CACHE"
                        if cached_accessions != event_accessions
                        else "CACHED_NOT_FULLY_PARSED"
                        if parsed_accessions != event_accessions
                        else "PARSED_POLICY_CANDIDATE_DISCLOSURE"
                        if policy_candidate_accessions
                        else "PARSED_REJECTED_ONLY_NUMERIC_DISCLOSURE"
                        if numeric_accessions
                        else "PARSED_NO_NUMERIC_DISCLOSURE"
                    ),
                }
            )

    summary: list[dict[str, Any]] = []
    for metric in CENSUS_METRICS:
        rows = [row for row in detail if row["metric_name"] == metric]
        level_count = sum(row["level_signal_candidate_flag"] for row in rows)
        pair_count = sum(
            row["longitudinal_pair_candidate_flag"] for row in rows
        )
        complete_count = sum(row["census_complete_flag"] for row in rows)
        level_windows, yoy_windows = candidate_windows[metric]
        max_level_count, max_level_date = _max_contemporaneous_coverage(
            level_windows,
            max_age_days=max_age_days,
        )
        max_yoy_count, max_yoy_date = _max_contemporaneous_coverage(
            yoy_windows,
            max_age_days=max_age_days,
        )
        level_gate_reachable = level_count >= 30
        growth_gate_reachable = pair_count >= 30
        review_candidates_present = any(
            row["review_required_numeric_event_count"] > 0 for row in rows
        )
        census_complete = complete_count == len(rows)
        summary.append(
            {
                "metric_name": metric,
                "historical_universe_ticker_count": len(rows),
                "ticker_with_qualifying_event_count": sum(
                    int(row["selected_earnings_event_count"] > 0)
                    for row in rows
                ),
                "fully_parsed_ticker_count": complete_count,
                "raw_numeric_candidate_ticker_count": sum(
                    int(row["raw_numeric_candidate_event_count"] > 0)
                    for row in rows
                ),
                "policy_candidate_level_ticker_count": level_count,
                "two_plus_policy_candidate_event_ticker_count": pair_count,
                "year_ago_pair_candidate_ticker_count": len(yoy_windows),
                "max_contemporaneous_policy_candidate_ticker_count": (
                    max_level_count
                ),
                "max_contemporaneous_policy_candidate_asof_date": (
                    max_level_date
                ),
                "max_contemporaneous_year_ago_pair_ticker_count": (
                    max_yoy_count
                ),
                "max_contemporaneous_year_ago_pair_asof_date": max_yoy_date,
                "contemporaneous_coverage_is_observed_sample_only_flag": 1,
                "historical_calibration_coverage_assessed_flag": 0,
                "growth_gate_basis": (
                    "two_plus_disclosure_events_upper_bound_not_final_yoy"
                ),
                "candidate_carry_forward_max_age_days": max_age_days,
                "accepted_numeric_ticker_count": sum(
                    int(row["accepted_numeric_event_count"] > 0) for row in rows
                ),
                "review_required_numeric_ticker_count": sum(
                    int(row["review_required_numeric_event_count"] > 0)
                    for row in rows
                ),
                "rejected_policy_numeric_ticker_count": sum(
                    int(row["rejected_policy_numeric_event_count"] > 0)
                    for row in rows
                ),
                "every_event_disclosure_ticker_count": sum(
                    row["every_observed_event_discloses_flag"] for row in rows
                ),
                "minimum_cross_section_required": 30,
                "level_gate_reachable_from_current_census_flag": int(
                    level_gate_reachable
                ),
                "growth_gate_reachable_from_current_census_flag": int(
                    growth_gate_reachable
                ),
                "census_complete_flag": int(census_complete),
                "interpretation": (
                    "COMPLETE_CENSUS_POLICY_CANDIDATE_UPPER_BOUND"
                    if census_complete
                    else "LOWER_BOUND_PENDING_CACHE_OR_PARSE_COMPLETION"
                ),
                "review_candidates_present_flag": int(
                    review_candidates_present
                ),
                "adjudication_required_flag": int(
                    review_candidates_present
                    and (level_gate_reachable or growth_gate_reachable)
                ),
                "adjudication_authorized_flag": int(
                    review_candidates_present
                    and (level_gate_reachable or growth_gate_reachable)
                ),
                "further_hydration_authorized_flag": int(not census_complete),
                "historical_series_hydration_authorized_flag": 0,
                "branch_recommendation": (
                    "COMPLETE_CENSUS_FIRST"
                    if not census_complete
                    else "PROCEED_TO_ADJUDICATION"
                    if review_candidates_present
                    and (level_gate_reachable or growth_gate_reachable)
                    else "PROCEED_TO_MEASUREMENT"
                    if level_gate_reachable or growth_gate_reachable
                    else "CLOSE_COVERAGE_GATE_UNREACHABLE"
                ),
                "measurement_only_flag": 1,
                "production_weight": 0.0,
            }
        )
    return detail, summary


def manifest_payload(
    *,
    asof_date: str,
    start_date: str,
    universe: list[dict[str, Any]],
    accession_rows: list[dict[str, Any]],
    source_rows: list[dict[str, Any]],
    detail_rows: list[dict[str, Any]],
    summary_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    selected = _selected_accessions(accession_rows)
    cached = {
        str(row["accession_number"])
        for row in accession_rows
        if row["cache_status"] == "CACHED_HASHED"
    }
    return {
        "manifest_version": "software_disclosure_census_v1",
        "model_family": MODEL_FAMILY,
        "asof_date": asof_date,
        "start_date": start_date,
        "metrics": list(CENSUS_METRICS),
        "historical_universe_ticker_count": len(universe),
        "selected_earnings_accession_count": len(selected),
        "selected_earnings_ticker_count": len(
            {str(row["ticker"]) for row in accession_rows}
        ),
        "cached_earnings_accession_count": len(cached),
        "missing_cache_accession_count": len(set(selected) - cached),
        "sealed_source_document_count": len(source_rows),
        "detail_row_count": len(detail_rows),
        "metric_summary": {
            str(row["metric_name"]): {
                key: value
                for key, value in row.items()
                if key != "metric_name"
            }
            for row in summary_rows
        },
        "census_scope": "latest_four_pit_earnings_events_per_issuer",
        "production_facts_modified_flag": 0,
        "production_scores_modified_flag": 0,
    }
