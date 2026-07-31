from __future__ import annotations

import copy
import runpy
from pathlib import Path
from typing import Any

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def script_namespace(name: str) -> dict[str, Any]:
    return runpy.run_path(str(PROJECT_ROOT / "industrials" / "defense" / "scripts" / name))


def specialized_rows() -> list[dict[str, object]]:
    return [
        {
            "ticker": ticker,
            "financial_orders_yoy_growth": scale,
            "financial_backlog_yoy_growth": scale,
            "financial_backlog_to_revenue": 1.0 + scale,
            "financial_book_to_bill": 1.0 + scale,
        }
        for ticker, scale in [("AAA", -0.10), ("BBB", 0.00), ("CCC", 0.10), ("DDD", 0.20)]
    ]


def test_specialized_candidate_activates_only_the_defense_demand_pillar() -> None:
    namespace = script_namespace("17_publish_defense_shadow_rank_table.py")
    build_scores = namespace["build_scores"]

    baseline = build_scores(copy.deepcopy(specialized_rows()), scoring_mode="baseline")
    candidate = build_scores(copy.deepcopy(specialized_rows()), scoring_mode="specialized_v1")

    assert {item["defense_budget_backlog"].status for item in baseline.values()} == {
        "neutralized_not_loaded"
    }
    assert {item["defense_budget_backlog"].score for item in baseline.values()} == {50.0}
    assert candidate["AAA"]["defense_budget_backlog"].score < candidate["DDD"]["defense_budget_backlog"].score
    assert {item["defense_budget_backlog"].status for item in candidate.values()} == {
        "candidate_specialized_complete"
    }
    for ticker in baseline:
        for pillar in [
            "valuation",
            "quality",
            "risk_control",
            "positioning",
            "market_behavior",
            "growth",
            "sector_cycle",
        ]:
            assert candidate[ticker][pillar] == baseline[ticker][pillar]
        assert candidate[ticker]["final_score"] == baseline[ticker]["final_score"]


def test_specialized_candidate_neutralizes_missing_disclosures() -> None:
    namespace = script_namespace("17_publish_defense_shadow_rank_table.py")
    scores = namespace["build_scores"]([{"ticker": "AAA"}], scoring_mode="specialized_v1")

    component = scores["AAA"]["defense_budget_backlog"]
    assert component.score == 50.0
    assert component.quality == 0.0
    assert component.status == "candidate_specialized_missing_neutralized"


def test_percentile_map_uses_average_ranks_for_ties_and_neutral_singleton() -> None:
    namespace = script_namespace("17_publish_defense_shadow_rank_table.py")
    percentile_map = namespace["percentile_map"]
    tied_rows = [
        {"ticker": "AAA", "metric": "1"},
        {"ticker": "BBB", "metric": "1"},
        {"ticker": "CCC", "metric": "3"},
    ]

    higher = percentile_map(tied_rows, "metric")
    lower = percentile_map(tied_rows, "metric", higher_is_better=False)

    assert higher == {"AAA": 25.0, "BBB": 25.0, "CCC": 100.0}
    assert lower == {"AAA": 75.0, "BBB": 75.0, "CCC": 0.0}
    assert percentile_map([{"ticker": "ONLY", "metric": "7"}], "metric") == {
        "ONLY": 50.0
    }


def test_research_candidate_stamps_close_every_production_gate() -> None:
    namespace = script_namespace("17_publish_defense_shadow_rank_table.py")
    stamp = namespace["apply_research_candidate_stamps"]
    row = {
        "oos_score_valid_flag": "1",
        "portfolio_candidate_gate": "1",
        "calibration_eligible_flag": "1",
        "research_calibration_input_eligible_flag": "1",
        "stage11_calibration_input_eligible_flag": "1",
        "calibration_lock_date": "2026-07-02",
        "calibration_production_start_date": "2026-07-03",
    }

    stamped = stamp([row], scoring_mode="specialized_v1")[0]

    assert stamped["oos_score_valid_flag"] == "0"
    assert stamped["portfolio_candidate_gate"] == "0"
    assert stamped["calibration_eligible_flag"] == "0"
    assert stamped["research_calibration_input_eligible_flag"] == "0"
    assert stamped["stage11_calibration_input_eligible_flag"] == "0"
    assert stamped["calibration_lock_date"] == ""
    assert stamped["calibration_production_start_date"] == ""
    assert stamped["portfolio_candidate_status"] == "research_candidate"


def test_optuna_ic_is_mean_cross_sectional_ic_not_pooled_time_series() -> None:
    namespace = script_namespace("24_run_defense_optuna_calibration.py")
    stats_fn = namespace["information_coefficient_stats"]
    rows: list[dict[str, str]] = []
    for asof, outcomes in [
        ("2026-01-02", [1.0, 2.0, 3.0]),
        ("2026-01-09", [3.0, 2.0, 1.0]),
    ]:
        for index, outcome in enumerate(outcomes, start=1):
            rows.append(
                {
                    "asof_date": asof,
                    "valuation_score": str(index),
                    "forward_excess_return_vs_sector": str(outcome),
                }
            )

    stats = stats_fn(
        rows,
        {"valuation_score": 1.0},
        objective="forward_excess_return_vs_sector",
    )

    assert stats.period_count == 2
    assert stats.mean_ic == pytest.approx(0.0)
    assert stats.stdev_ic == pytest.approx(2**0.5)


def test_portfolio_selection_uses_validation_excess_with_positive_ic_guard() -> None:
    namespace = script_namespace("24_run_defense_optuna_calibration.py")
    select_best = namespace["select_best_trial"]
    records = [
        {
            "trial_number": "0",
            "validation_ic": "0.20",
            "validation_top_quantile_excess": "0.01",
        },
        {
            "trial_number": "1",
            "validation_ic": "-0.10",
            "validation_top_quantile_excess": "0.05",
        },
        {
            "trial_number": "2",
            "validation_ic": "0.10",
            "validation_top_quantile_excess": "0.03",
        },
    ]

    best, metric = select_best(
        records,
        selection_metric="validation_top_quantile_excess",
    )

    assert metric == "validation_top_quantile_excess"
    assert best is not None
    assert best["trial_number"] == "2"


def test_calibration_masks_constant_pillars_without_changing_proposal_bank() -> None:
    namespace = script_namespace("24_run_defense_optuna_calibration.py")
    inactive = namespace["inactive_pillars_for_calibration"]
    constrain = namespace["constrain_trial_weights"]
    rows = [
        {
            "valuation_score": "50",
            "quality_score": str(value),
            "sector_cycle_score": "50",
        }
        for value in (10, 20, 30)
    ]

    masked = inactive(rows)
    weights = {
        "valuation_score": 0.25,
        "quality_score": 0.25,
        "risk_control_score": 0.0,
        "positioning_score": 0.0,
        "market_behavior_score": 0.0,
        "growth_score": 0.0,
        "sector_cycle_score": 0.25,
        "defense_budget_backlog_score": 0.25,
    }
    effective = constrain(weights, inactive_pillars=masked)

    assert "valuation_score" in masked
    assert "sector_cycle_score" in masked
    assert "quality_score" not in masked
    assert effective["valuation_score"] == 0.0
    assert effective["sector_cycle_score"] == 0.0
    assert effective["quality_score"] > 0.0


def test_portfolio_stats_matches_top_quantile_excess_definition() -> None:
    namespace = script_namespace("24_run_defense_optuna_calibration.py")
    portfolio_stats = namespace["portfolio_stats"]
    rows = [
        {
            "asof_date": "2026-01-02",
            "valuation_score": str(score),
            "forward_excess_return_vs_sector": str(outcome),
        }
        for score, outcome in [(100, 0.04), (80, 0.02), (20, -0.01), (0, -0.03)]
    ]

    stats = portfolio_stats(
        rows,
        {"valuation_score": 1.0},
        objective="forward_excess_return_vs_sector",
        top_quantile=0.25,
        min_positions=1,
    )

    assert stats.period_count == 1
    assert stats.mean_top_quantile_excess == pytest.approx(0.04)
    assert stats.mean_top_bottom_spread == pytest.approx(0.07)


def test_paired_block_bootstrap_detects_consistent_candidate_lift() -> None:
    namespace = script_namespace("29_compare_defense_baseline_vs_candidate.py")
    bootstrap = namespace["moving_block_bootstrap_mean_delta"]
    baseline = {f"2026-{month:02d}-01": 0.0 for month in range(1, 13)}
    candidate = {
        asof: 0.01 + (index % 3) * 0.001
        for index, asof in enumerate(baseline)
    }

    result = bootstrap(
        baseline,
        candidate,
        samples=1_000,
        block_periods=3,
        seed=17,
    )

    assert result["paired_periods"] == 12
    assert float(result["ci_95_lower"]) > 0.0
    assert result["probability_positive"] == 1.0


def test_evaluation_calendar_rejects_duplicates_and_unsorted_dates(tmp_path: Path) -> None:
    namespace = script_namespace("19_build_defense_shadow_snapshot_history.py")
    read_calendar = namespace["read_evaluation_calendar"]
    calendar = tmp_path / "calendar.csv"
    calendar.write_text("asof_date\n2026-01-09\n2026-01-02\n", encoding="utf-8")

    with pytest.raises(ValueError, match="unique, and ascending"):
        read_calendar(calendar)
