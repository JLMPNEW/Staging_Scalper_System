from __future__ import annotations

from industrials.transportation.post_review import (
    build_post_review_metric_rows,
)


def test_formal_post_review_gate_requires_breadth_precision_and_history() -> None:
    gate = {
        "metric_id": "operating_ratio",
        "metric_pack": "surface",
        "source_lane": "DP",
        "active_accepted_count": 5,
        "active_usable_count": 5,
        "broad_required_count": 5,
        "broad_accepted_shortfall": 0,
        "best_accepted_niche_archetype": "surface_rail_operator",
        "best_accepted_niche_required_count": 3,
        "best_accepted_niche_count": 3,
        "best_accepted_niche_shortfall": 0,
        "accepted_gate_pass": 1,
    }
    coverage = [
        {
            "ticker": f"R{index}",
            "metric_id": "operating_ratio",
            "metric_pack": "surface",
            "source_lane": "DP",
            "universe_role": "active",
            "applicability_status": "APPLICABLE",
            "coverage_status": "COVERED_ACCEPTED",
        }
        for index in range(5)
    ]
    decisions = [
        {
            "metric_id": "operating_ratio",
            "review_decision": "ACCEPT",
            "confirmation_basis": "EXACT_ACCEPTED_LEGACY_MATCH",
        }
        for _ in range(5)
    ]
    periods = {
        (f"R{index}", "operating_ratio"): {
            "2019-12-31",
            "2020-12-31",
            "2021-12-31",
            "2022-12-31",
        }
        for index in range(5)
    }

    row = build_post_review_metric_rows(
        run_id=58,
        evaluation_id=1,
        pre_gate_rows=[gate],
        post_gate_rows=[gate],
        post_coverage_rows=coverage,
        adjudication_rows=decisions,
        accepted_periods=periods,
    )[0]

    assert row["accepted_breadth_gate_pass"] == 1
    assert row["accepted_validation_rate"] == 1.0
    assert row["historical_depth_gate_pass"] == 1
    assert row["formal_calibration_gate_pass"] == 1
    assert row["metric_disposition"] == "CALIBRATION_CANDIDATE"


def test_insufficient_history_keeps_accepted_metric_diagnostic_only() -> None:
    gate = {
        "metric_id": "operating_ratio",
        "metric_pack": "surface",
        "source_lane": "DP",
        "active_accepted_count": 1,
        "active_usable_count": 1,
        "broad_required_count": 1,
        "broad_accepted_shortfall": 0,
        "best_accepted_niche_archetype": "surface_rail_operator",
        "best_accepted_niche_required_count": 1,
        "best_accepted_niche_count": 1,
        "best_accepted_niche_shortfall": 0,
        "accepted_gate_pass": 1,
    }
    row = build_post_review_metric_rows(
        run_id=58,
        evaluation_id=1,
        pre_gate_rows=[gate],
        post_gate_rows=[gate],
        post_coverage_rows=[
            {
                "ticker": "R1",
                "metric_id": "operating_ratio",
                "metric_pack": "surface",
                "source_lane": "DP",
                "universe_role": "active",
                "applicability_status": "APPLICABLE",
                "coverage_status": "COVERED_ACCEPTED",
            }
        ],
        adjudication_rows=[
            {
                "metric_id": "operating_ratio",
                "review_decision": "ACCEPT",
                "confirmation_basis": "EXACT_ACCEPTED_LEGACY_MATCH",
            }
        ],
        accepted_periods={
            ("R1", "operating_ratio"): {"2025-12-31"}
        },
    )[0]

    assert row["formal_calibration_gate_pass"] == 0
    assert row["metric_disposition"] == "DIAGNOSTIC_ONLY"
