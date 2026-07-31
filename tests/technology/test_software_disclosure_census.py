from __future__ import annotations

from technology.software_infrastructure.software_disclosure_census import (
    build_metric_census,
    select_recent_earnings_events,
)


def _filing(
    ticker: str,
    form: str,
    filing_date: str,
    accession: str,
) -> dict[str, object]:
    return {
        "ticker": ticker,
        "cik": "0000000001",
        "accession_number": accession,
        "form_type": form,
        "filing_date": filing_date,
        "accepted_at": f"{filing_date}T20:00:00Z",
        "report_date": filing_date,
        "primary_document": "filing.htm",
        "source_id": "sec_submissions",
        "cohort_id": "software_test",
        "membership_status": "active",
    }


def test_recent_event_selector_uses_periodic_proximity_and_foreign_quarters() -> None:
    filings = [
        _filing("DOM", "10-Q", "2026-05-05", "dom-q1"),
        _filing("DOM", "8-K", "2026-05-04", "dom-e1"),
        _filing("DOM", "8-K", "2026-03-01", "dom-other"),
        _filing("DOM", "10-K", "2026-02-05", "dom-k"),
        _filing("DOM", "8-K", "2026-02-04", "dom-e2"),
        _filing("FPI", "6-K", "2026-05-10", "fpi-2a"),
        _filing("FPI", "6-K", "2026-06-20", "fpi-2b"),
        _filing("FPI", "6-K", "2026-02-10", "fpi-1"),
        _filing("FPI", "20-F", "2026-03-20", "fpi-20f"),
    ]
    selected = select_recent_earnings_events(
        filings,
        max_events_per_ticker=4,
        event_window_days=21,
    )
    accessions = {str(row["accession_number"]) for row in selected}
    assert accessions == {"dom-e1", "dom-e2", "fpi-1", "fpi-2b"}
    assert "dom-other" not in accessions


def test_metric_census_separates_policy_candidates_from_rejected_evidence() -> None:
    universe = [
        {
            "ticker": "A",
            "cohort_id": "c1",
            "historical_member_flag": 0,
        },
        {
            "ticker": "B",
            "cohort_id": "c1",
            "historical_member_flag": 1,
        },
    ]
    accessions = [
        {
            "ticker": "A",
            "accession_number": "a1",
            "cache_status": "CACHED_HASHED",
            "accepted_at": "2025-05-01T20:00:00Z",
            "report_date": "2025-03-31",
            "filing_date": "2025-05-01",
        },
        {
            "ticker": "A",
            "accession_number": "a2",
            "cache_status": "CACHED_HASHED",
            "accepted_at": "2026-05-01T20:00:00Z",
            "report_date": "2026-03-31",
            "filing_date": "2026-05-01",
        },
        {
            "ticker": "B",
            "accession_number": "b1",
            "cache_status": "CACHED_HASHED",
            "accepted_at": "2026-05-01T20:00:00Z",
            "report_date": "2026-03-31",
            "filing_date": "2026-05-01",
        },
    ]
    evidence = [
        {
            "ticker": "A",
            "accession_number": "a1",
            "metric_name": "annual_recurring_revenue",
            "candidate_value": 100.0,
            "candidate_status": "REVIEW_REQUIRED",
            "period_end": "2025-03-31",
        },
        {
            "ticker": "A",
            "accession_number": "a2",
            "metric_name": "annual_recurring_revenue",
            "candidate_value": 120.0,
            "candidate_status": "ACCEPTED",
            "period_end": "2026-03-31",
        },
        {
            "ticker": "B",
            "accession_number": "b1",
            "metric_name": "annual_recurring_revenue",
            "candidate_value": 999.0,
            "candidate_status": "REJECTED_POLICY",
            "period_end": "2026-03-31",
        },
    ]
    detail, summary = build_metric_census(
        universe=universe,
        accession_rows=accessions,
        completed_accessions={"a1", "a2", "b1"},
        evidence_rows=evidence,
        max_events_per_ticker=4,
    )
    arr_a = next(
        row
        for row in detail
        if row["ticker"] == "A"
        and row["metric_name"] == "annual_recurring_revenue"
    )
    arr_b = next(
        row
        for row in detail
        if row["ticker"] == "B"
        and row["metric_name"] == "annual_recurring_revenue"
    )
    assert arr_a["longitudinal_pair_candidate_flag"] == 1
    assert arr_a["coverage_status"] == (
        "PARSED_POLICY_CANDIDATE_DISCLOSURE"
    )
    assert arr_b["coverage_status"] == (
        "PARSED_REJECTED_ONLY_NUMERIC_DISCLOSURE"
    )
    assert arr_b["level_signal_candidate_flag"] == 0
    arr_summary = next(
        row
        for row in summary
        if row["metric_name"] == "annual_recurring_revenue"
    )
    assert arr_summary["policy_candidate_level_ticker_count"] == 1
    assert arr_summary["two_plus_policy_candidate_event_ticker_count"] == 1
    assert arr_summary["year_ago_pair_candidate_ticker_count"] == 1
    assert arr_summary["max_contemporaneous_policy_candidate_ticker_count"] == 1
    assert arr_summary["max_contemporaneous_year_ago_pair_ticker_count"] == 1
    assert arr_summary["level_gate_reachable_from_current_census_flag"] == 0
    assert arr_summary["growth_gate_reachable_from_current_census_flag"] == 0
    assert arr_summary["review_candidates_present_flag"] == 1
    assert arr_summary["adjudication_required_flag"] == 0
    assert arr_summary["adjudication_authorized_flag"] == 0
    assert arr_summary["branch_recommendation"] == (
        "CLOSE_COVERAGE_GATE_UNREACHABLE"
    )
    assert arr_summary["historical_calibration_coverage_assessed_flag"] == 0
    assert arr_summary["census_complete_flag"] == 1
    assert arr_summary["rejected_policy_numeric_ticker_count"] == 1
