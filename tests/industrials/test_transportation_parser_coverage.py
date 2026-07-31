from __future__ import annotations

import sqlite3

from industrials.transportation.parser_coverage import (
    _derived_stats,
    _empty_stats,
    accepted_periods_for_final_metric,
    direct_status,
    load_review_evidence_stats,
    load_run,
)


def _stats(
    *,
    accepted: int = 0,
    review: int = 0,
    rejected: int = 0,
    failures: int = 0,
    periods: tuple[str, ...] = (),
) -> dict[str, object]:
    result = _empty_stats()
    result["text_hit_count"] = accepted + review + rejected
    result["value_candidate_count"] = accepted + review + rejected
    result["accepted_value_count"] = accepted
    result["review_value_count"] = review
    result["rejected_value_count"] = rejected
    result["parser_failure_count"] = failures
    result["periods"] = set(periods)
    result["accepted_periods"] = set(periods) if accepted else set()
    result["usable_periods"] = set(periods) if accepted or review else set()
    return result


def test_direct_status_keeps_accepted_review_and_failure_separate() -> None:
    work = {"searched": 2, "completed": 2, "failed": 0}
    assert direct_status(_stats(accepted=1), work) == "COVERED_ACCEPTED"
    assert (
        direct_status(_stats(review=1), work)
        == "COVERED_REVIEW_REQUIRED"
    )
    assert (
        direct_status(_stats(failures=1), work)
        == "PARSER_FAILURE_ONLY"
    )
    assert direct_status(_stats(), work) == "SEARCHED_NOT_FOUND"


def test_all_operand_derivation_requires_a_matching_period() -> None:
    evidence = {
        ("AAL", "airline_fuel_consumed"): _stats(
            review=1,
            periods=("2025-12-31",),
        ),
        ("AAL", "airline_capacity_units"): _stats(
            review=1,
            periods=("2026-03-31",),
        ),
    }
    result, _ = _derived_stats(
        ticker="AAL",
        metric_id="fuel_efficiency_per_capacity_unit",
        evidence=evidence,
    )
    assert result["value_candidate_count"] == 0

    evidence[("AAL", "airline_capacity_units")] = _stats(
        review=1,
        periods=("2025-12-31",),
    )
    result, _ = _derived_stats(
        ticker="AAL",
        metric_id="fuel_efficiency_per_capacity_unit",
        evidence=evidence,
    )
    assert result["value_candidate_count"] > 0
    assert result["review_value_count"] == 1


def test_series_derivation_requires_two_distinct_periods() -> None:
    one_period = {
        ("ZIM", "fleet_capacity"): _stats(
            review=1,
            periods=("2025-12-31",),
        )
    }
    result, _ = _derived_stats(
        ticker="ZIM",
        metric_id="fleet_capacity_growth",
        evidence=one_period,
    )
    assert result["value_candidate_count"] == 0

    two_periods = {
        ("ZIM", "fleet_capacity"): _stats(
            review=2,
            periods=("2024-12-31", "2025-12-31"),
        )
    }
    result, _ = _derived_stats(
        ticker="ZIM",
        metric_id="fleet_capacity_growth",
        evidence=two_periods,
    )
    assert result["review_value_count"] == 1


def test_accepted_periods_respect_derived_operand_contract() -> None:
    evidence = {
        ("AAL", "airline_fuel_consumed"): _stats(
            accepted=2,
            periods=("2024-12-31", "2025-12-31"),
        ),
        ("AAL", "airline_capacity_units"): _stats(
            accepted=2,
            periods=("2023-12-31", "2025-12-31"),
        ),
    }

    periods = accepted_periods_for_final_metric(
        ticker="AAL",
        metric_id="fuel_efficiency_per_capacity_unit",
        source_lane="DP-D",
        evidence=evidence,
    )

    assert periods == {"2025-12-31"}


def test_review_evidence_loader_requires_zero_source_operations() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE sec_parser_review_evaluation(
            evaluation_id INTEGER,
            base_run_id INTEGER,
            model_family TEXT,
            status TEXT,
            source_document_open_count INTEGER,
            arelle_invocation_count INTEGER,
            edgartools_invocation_count INTEGER,
            ocr_invocation_count INTEGER
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE sec_parser_review_evidence(
            evaluation_id INTEGER,
            ticker TEXT,
            metric_name TEXT,
            candidate_value REAL,
            candidate_status TEXT,
            period_end TEXT,
            provenance_json TEXT
        )
        """
    )
    connection.execute(
        """
        INSERT INTO sec_parser_review_evaluation
        VALUES(1, 58, 'transportation', 'COMPLETED', 0, 0, 0, 0)
        """
    )
    connection.execute(
        """
        INSERT INTO sec_parser_review_evidence
        VALUES(
            1, 'AAL', 'passenger_load_factor', 0.82, 'ACCEPTED',
            '2025-12-31', '{}'
        )
        """
    )

    stats = load_review_evidence_stats(connection, evaluation_id=1)

    assert stats[("AAL", "passenger_load_factor")][
        "accepted_value_count"
    ] == 1
    connection.execute(
        """
        UPDATE sec_parser_review_evaluation
        SET source_document_open_count=1
        WHERE evaluation_id=1
        """
    )
    try:
        load_review_evidence_stats(connection, evaluation_id=1)
    except ValueError as exc:
        assert "zero-source-operation" in str(exc)
    else:
        raise AssertionError("nonzero source operations must fail closed")


def test_load_run_reports_resume_linked_completed_work() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE sec_parser_run(
            run_id INTEGER,
            model_family TEXT,
            asof_date TEXT,
            adapter_version TEXT,
            status TEXT,
            planned_work_count INTEGER,
            completed_work_count INTEGER,
            failed_work_count INTEGER,
            metadata_json TEXT
        )
        """
    )
    connection.execute(
        """
        INSERT INTO sec_parser_run
        VALUES(
            58, 'transportation', '2026-07-22', 'adapter-v1',
            'COMPLETED', 0, 0, 0,
            '{"plan":{"linked_completed_work_count":4510}}'
        )
        """
    )

    run = load_run(connection, run_id=58)

    assert run["linked_completed_work_count"] == 4510
