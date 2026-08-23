from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load_script(name: str):
    path = PROJECT_ROOT / "industrials" / "transportation" / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.replace(".", "_"), path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_forensic_candidate_preserves_zero_score_and_cost_math() -> None:
    audit = load_script("38y_audit_transportation_v5_model_forensics.py")
    rows = []
    for asof, exit_date in (
        ("2024-01-31", "2024-05-01"),
        ("2024-05-31", "2024-09-03"),
    ):
        rows.extend(
            [
                {
                    "asof_date": asof,
                    "ticker": "ZERO",
                    "calibration_cohort": "test",
                    "horizon_sessions": "63",
                    "calibration_eligible_flag": "1",
                    "outcome_available_flag": "1",
                    "forward_excess_return": "0.02",
                    "benchmark_exit_date": exit_date,
                    "quality_score": "0",
                },
                {
                    "asof_date": asof,
                    "ticker": "NEG",
                    "calibration_cohort": "test",
                    "horizon_sessions": "63",
                    "calibration_eligible_flag": "1",
                    "outcome_available_flag": "1",
                    "forward_excess_return": "-0.01",
                    "benchmark_exit_date": exit_date,
                    "quality_score": "-1",
                },
            ]
        )
    result = audit.independent_candidate(
        rows,
        cohort_id="test",
        weights={"quality_score": 1.0},
        horizon_sessions=63,
        minimum_cross_section=2,
        top_fraction=0.5,
        transaction_cost_bps=20.0,
    )
    assert result["period_rows"][0]["selected_tickers"] == ["ZERO"]
    assert result["period_rows"][1]["turnover"] == 0.0
    assert result["mean_top_excess_net"] == 0.02


def test_forensic_metric_screen_does_not_authorize_ambiguous_freight_weight() -> None:
    audit = load_script("38y_audit_transportation_v5_model_forensics.py")
    rows = []
    tickers = ("A", "B", "C")
    for year in (2024, 2025):
        for month in range(1, 13):
            asof = f"{year}-{month:02d}-28"
            for index, ticker in enumerate(tickers):
                rows.append(
                    {
                        "asof_date": asof,
                        "ticker": ticker,
                        "calibration_cohort": "surface",
                        "horizon_sessions": "63",
                        "calibration_eligible_flag": "1",
                        "outcome_available_flag": "1",
                        "forward_excess_return": str(index / 100),
                        "metric_values_json": json.dumps(
                            {"freight_weight_per_shipment": 100 + index}
                        ),
                        "metric_status_json": json.dumps(
                            {"freight_weight_per_shipment": "REPORTED"}
                        ),
                    }
                )
    result = audit.metric_diagnostics(
        rows,
        cohort_id="surface",
        policy={
            "score_construction": {
                "retained_specialized_metrics": ["freight_weight_per_shipment"]
            },
            "metric_comparison_domains": {
                "freight_weight_per_shipment": {"ltl": list(tickers)}
            },
        },
        registry_definitions=[
            SimpleNamespace(metric_id="freight_weight_per_shipment", direction=1)
        ],
        horizon_sessions=63,
        calendar_blocks=[
            {
                "block_id": "diagnostic_block_3",
                "start_date": "2024-01-01",
                "end_date": "2026-07-30",
            }
        ],
    )
    assert result[0]["mean_directional_ic"] == 1.0
    assert result[0]["research_disposition"] == (
        "RESEARCH_DIRECTION_UNRESOLVED_NO_EXTRACTION"
    )


def test_v7_specification_is_future_only_and_fail_closed() -> None:
    decision = load_script("38z_build_transportation_v7_research_decision.py")
    spec = decision.build_spec()
    assert spec["production_authority"] is False
    assert spec["first_future_signal_date"] == "2026-08-24"
    gate = spec["promotion_gate"]
    assert gate["minimum_future_21_session_non_overlapping_outcomes"] == 12
    assert gate["minimum_future_63_session_non_overlapping_outcomes"] == 4
    assert gate["cohort_isolation_required"] is True


def test_incremental_proof_fails_when_feature_does_not_change_ranking() -> None:
    replay = load_script("38za_replay_transportation_v7_accepted_metric_proof.py")
    observations = []
    for month in range(1, 13):
        asof = f"2024-{month:02d}-01"
        exit_date = f"2024-{month:02d}-28"
        for index, ticker in enumerate(("A", "B", "C")):
            observations.append(
                {
                    "asof_date": asof,
                    "exit_date": exit_date,
                    "ticker": ticker,
                    "baseline_score": float(index),
                    "metric_value": float(index),
                    "outcome": float(index) / 100,
                }
            )
    result = replay.evaluate_feature(
        observations,
        cohort_id="test",
        metric_id="same_rank",
        domain="test_domain",
    )
    assert result["incremental_mean_ic"] == 0.0
    assert result["proof_gate"] == "FAIL"
    assert result["parser_authorization"] == "DENY_MORE_PARSING_CURRENT_DEFINITION"
