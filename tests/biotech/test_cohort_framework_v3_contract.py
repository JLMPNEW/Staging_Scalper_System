from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from biotech_index.core.cohort_history import load_cohort_history
from biotech_index.core.portfolio_candidate_policy import portfolio_candidate_base_eligible
from biotech_index.core.score_reliability import blend_active_alpha_with_benchmark
from biotech_index.core.selection_robustness import replay_selected_policy_returns
from tests.biotech.conftest import load_script_module


ROOT = Path(__file__).resolve().parents[2]


def test_effective_dated_cohort_history_does_not_backcast_current_assignment() -> None:
    history = load_cohort_history(
        ROOT / "biotech_index/data/biotech_calibration_cohorts.csv",
        migration_path=ROOT / "biotech_index/data/biotech_cohort_migration_20260831.csv",
    )

    prior = history.resolve("ARGX", date(2026, 8, 30))
    current = history.resolve("ARGX", date(2026, 8, 31))

    assert prior is not None
    assert current is not None
    assert prior.cohort == "late_clinical_pivotal_or_registrational"
    assert prior.valid_to == date(2026, 8, 30)
    assert current.cohort == "commercial_profitable_quality_or_mature"
    assert current.valid_from == date(2026, 8, 31)


def test_shared_live_base_gate_fails_closed_before_ranking() -> None:
    valid = {
        "ticker": "AAA",
        "candidate_selection_score": 60.0,
        "score_zero_is_missing_flag": 0.0,
        "biotech_cohort_investible_flag": 1.0,
        "universe_status": "live",
        "core_structural_veto_flag": 0.0,
        "rank_quality_cap_vetoed": 0.0,
        "price_data_asof_date": "2026-08-31",
    }
    assert portfolio_candidate_base_eligible(valid)
    for field, value in (
        ("candidate_selection_score", 0.0),
        ("score_zero_is_missing_flag", 1.0),
        ("biotech_cohort_investible_flag", 0.0),
        ("universe_status", "calibration_only"),
        ("core_structural_veto_flag", 1.0),
        ("rank_quality_cap_vetoed", 1.0),
        ("price_data_asof_date", ""),
    ):
        row = dict(valid)
        row[field] = value
        assert not portfolio_candidate_base_eligible(row), field


def test_name_capacity_caps_date_specific_active_exposure() -> None:
    blended = blend_active_alpha_with_benchmark(
        {"2026-01-02": 0.20, "2026-01-03": 0.20},
        ["2026-01-02", "2026-01-03"],
        active_weight=0.80,
        selected_counts={"2026-01-02": 1, "2026-01-03": 4},
        max_name_weight=0.25,
    )
    assert blended == {
        "2026-01-02": pytest.approx(0.05),
        "2026-01-03": pytest.approx(0.16),
    }


def test_selected_policy_replay_recomputes_weight_after_ticker_jackknife() -> None:
    selected = [
        {
            "fold_id": "h120_f01",
            "evaluation_split": "outer_test_candidate",
            "asof_date": "2026-01-02",
            "ticker": "AAA",
            "objective_return": 0.20,
        },
        {
            "fold_id": "h120_f01",
            "evaluation_split": "outer_test_candidate",
            "asof_date": "2026-01-02",
            "ticker": "BBB",
            "objective_return": 0.00,
        },
        {
            "fold_id": "h120_f01",
            "evaluation_split": "outer_test_incumbent",
            "asof_date": "2026-01-02",
            "ticker": "INC",
            "objective_return": 0.05,
        },
    ]
    sleeves = [{"fold_id": "h120_f01", "horizon_days": 120, "asof_date": "2026-01-02"}]
    comparisons = [
        {
            "fold_id": "h120_f01",
            "horizon_days": 120,
            "active_weight": 0.80,
            "frozen_max_name_weight": 0.25,
        }
    ]

    candidate, incumbent, _ = replay_selected_policy_returns(
        selected_rows=selected,
        sleeve_rows=sleeves,
        comparison_rows=comparisons,
        horizon=120,
    )
    leaveout, _, _ = replay_selected_policy_returns(
        selected_rows=selected,
        sleeve_rows=sleeves,
        comparison_rows=comparisons,
        horizon=120,
        exclude_candidate_ticker="BBB",
    )

    assert candidate["2026-01-02"] == pytest.approx(0.05)
    assert incumbent["2026-01-02"] == pytest.approx(0.05)
    assert leaveout["2026-01-02"] == pytest.approx(0.05)


def test_xbi_regime_classifier_ignores_future_bars() -> None:
    module = load_script_module(
        "28_calibrate_biotech_opportunity.py",
        "biotech_framework_v3_regime_test",
    )
    asof = date(2026, 6, 30)
    bars = [
        module.Bar(asof - timedelta(days=129 - index), 100.0 + index * 0.20)
        for index in range(130)
    ]
    future = module.Bar(asof + timedelta(days=1), 1.0)

    expected = module.point_in_time_benchmark_regime(bars, asof)
    observed = module.point_in_time_benchmark_regime([*bars, future], asof)

    assert observed == expected
    assert observed["xbi_regime_asof_date"] == asof.isoformat()
    assert observed["xbi_regime_definition_version"] == "pit_xbi_regime_v1"
