from __future__ import annotations

import json
from pathlib import Path

from industrials.transportation.coverage_lift import (
    build_metric_gate_rows,
    build_pair_review_queue,
    build_source_filing_rows,
    screen_cached_source_candidates,
)
from industrials.transportation.dedicated_parser_adapter import (
    metric_search_aliases,
)


def _coverage(
    *,
    ticker: str,
    metric_id: str,
    status: str,
    archetype: str = "passenger_airline",
    source_lane: str = "DP",
    metric_pack: str = "air",
) -> dict[str, str]:
    return {
        "run_id": "58",
        "ticker": ticker,
        "universe_role": "active",
        "calibration_cohort": "air_transport_and_aviation_services",
        "primary_archetype": archetype,
        "metric_id": metric_id,
        "metric_pack": metric_pack,
        "source_lane": source_lane,
        "applicability_status": "APPLICABLE",
        "coverage_status": status,
        "text_hit_count": "1",
        "value_candidate_count": "1",
        "review_value_count": "1",
        "rejected_value_count": "0",
        "distinct_period_count": "2",
    }


def test_metric_gate_marks_one_issuer_shortfall_as_near() -> None:
    rows = [
        _coverage(
            ticker=f"A{index}",
            metric_id="unit_cost",
            status=(
                "COVERED_REVIEW_REQUIRED"
                if index <= 4
                else "SEARCHED_NOT_FOUND"
            ),
            archetype=(
                "passenger_airline"
                if index % 2
                else "cargo_airline"
            ),
        )
        for index in range(1, 11)
    ]

    gate = build_metric_gate_rows(rows)[0]

    assert gate["broad_required_count"] == 5
    assert gate["active_usable_count"] == 4
    assert gate["minimum_usable_shortfall"] == 1
    assert gate["coverage_target_class"] == "NEAR_GATE_SHORTFALL_1"
    assert gate["source_search_target"] == 1


def test_exact_archetype_gate_can_retain_a_narrow_metric() -> None:
    rows = [
        _coverage(
            ticker=f"AP{index}",
            metric_id="aircraft_movements_growth",
            status=(
                "COVERED_REVIEW_REQUIRED"
                if index <= 3
                else "SEARCHED_NOT_FOUND"
            ),
            archetype="airport_operator",
        )
        for index in range(1, 5)
    ]

    gate = build_metric_gate_rows(rows)[0]

    assert gate["broad_usable_shortfall"] == 2
    assert gate["best_usable_niche_required_count"] == 3
    assert gate["best_usable_niche_shortfall"] == 0
    assert gate["usable_gate_pass"] == 1
    assert (
        gate["coverage_target_class"]
        == "USABLE_GATE_PASS_REVIEW_REQUIRED"
    )


def test_zero_active_discovery_is_named_and_source_targeted() -> None:
    rows = [
        _coverage(
            ticker=f"A{index}",
            metric_id="service_reliability_rate",
            status="SEARCHED_NOT_FOUND",
        )
        for index in range(1, 11)
    ]

    gate = build_metric_gate_rows(rows)[0]

    assert gate["active_discovered_count"] == 0
    assert (
        gate["coverage_target_class"]
        == "ZERO_ACTIVE_DISCOVERY_SOURCE_TARGET"
    )
    assert gate["source_search_target"] == 1


def test_pair_queue_prioritizes_gate_pass_review_without_reparse() -> None:
    rows = [
        _coverage(
            ticker=f"A{index}",
            metric_id="unit_cost",
            status="COVERED_REVIEW_REQUIRED",
        )
        for index in range(1, 6)
    ]
    gate = build_metric_gate_rows(rows)

    queue = build_pair_review_queue(rows, gate, [])

    assert len(queue) == 5
    assert queue[0]["review_priority"] == 1
    assert (
        queue[0]["desired_action"]
        == "ADJUDICATE_EXISTING_VALUE_NO_REPARSE"
    )


def test_cached_index_screen_targets_metric_aliases(tmp_path: Path) -> None:
    index_path = (
        tmp_path
        / "sec_archive_xbrl"
        / "CIK0000000001"
        / "000000000126000001"
        / "index.json"
    )
    index_path.parent.mkdir(parents=True)
    index_path.write_text(
        json.dumps(
            {
                "directory": {
                    "item": [
                        {
                            "name": "exhibit991.htm",
                            "type": "EX-99.1",
                            "description": (
                                "Firm orders and binding orders update"
                            ),
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    coverage = [
        _coverage(
            ticker="TEST",
            metric_id="binding_order_units",
            status="SEARCHED_NOT_FOUND",
            archetype="precommercial_transport",
            metric_pack="development",
        )
    ]
    gate = build_metric_gate_rows(coverage)
    assert gate[0]["source_search_target"] == 1
    decisions = [
        {
            "ticker": "TEST",
            "cik": "1",
            "accession_number": "0000000001-26-000001",
            "form_type": "8-K",
            "filing_date": "2026-01-15",
            "candidate_type": "supplemental_event",
            "decision": "EXCLUDE",
            "reason": "cached metadata has no generic result signal",
            "index_status": "CACHED",
            "submissions_items": "8.01,9.01",
        }
    ]

    candidates, counters = screen_cached_source_candidates(
        decisions=decisions,
        coverage_rows=coverage,
        gate_rows=gate,
        cache_dir=tmp_path,
        aliases=metric_search_aliases(),
        derived_dependencies={},
    )

    assert len(candidates) == 1
    assert candidates[0]["candidate_basis"] == "CACHED_INDEX_METRIC_ALIAS"
    assert "binding orders" in candidates[0]["matched_aliases"]
    assert counters["metadata_alias_match_rows"] == 1
    assert candidates[0]["hydration_authorized"] == 0
    assert candidates[0]["parser_authorized"] == 0


def test_source_filing_rows_group_metric_candidates() -> None:
    base = {
        "candidate_priority": 1,
        "ticker": "TEST",
        "cik": "0000000001",
        "accession_number": "0000000001-26-000001",
        "form_type": "8-K",
        "filing_date": "2026-01-15",
        "submissions_items": "8.01,9.01",
        "candidate_type": "supplemental_event",
        "candidate_basis": "CACHED_INDEX_METRIC_ALIAS",
        "matched_aliases": "firm orders",
        "matched_index_documents": "exhibit991.htm",
        "index_path": "index.json",
        "index_sha256": "0" * 64,
        "candidate_disposition": "PENDING_REVIEW",
        "hydration_authorized": 0,
        "parser_authorized": 0,
    }
    rows = [
        {**base, "metric_id": "binding_order_units"},
        {**base, "metric_id": "binding_order_value"},
    ]

    filings = build_source_filing_rows(rows)

    assert len(filings) == 1
    assert filings[0]["target_metric_count"] == 2
    assert (
        filings[0]["target_metric_ids"]
        == "binding_order_units|binding_order_value"
    )
