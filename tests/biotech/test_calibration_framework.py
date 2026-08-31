from __future__ import annotations

from datetime import date, timedelta

import pytest

from biotech_index.core.calibration_metrics import MetricSettings, paired_policy_comparison, summarize_returns
from biotech_index.core.calibration_splits import (
    WalkForwardFold,
    WalkForwardWindow,
    build_expanding_walk_forward_folds,
    partition_rows_for_fold,
)
from biotech_index.core.promotion_policy import (
    PromotionDecision,
    PromotionRules,
    apply_deployment_readiness_gate,
    apply_no_harm_gate,
    decide_promotion,
    deployment_active_weight,
    no_harm_reason_codes,
)
from biotech_index.core.score_reliability import (
    ReliabilityRecord,
    apply_reliability_threshold,
    blend_active_alpha_with_benchmark,
)


def daily_dates(start: date, count: int) -> list[str]:
    return [(start + timedelta(days=offset)).isoformat() for offset in range(count)]


def test_walk_forward_windows_are_non_overlapping_and_embargo_validated() -> None:
    window = WalkForwardWindow(
        horizon_bars=20,
        validation_months=6,
        test_months=6,
        step_months=6,
        embargo_days=40,
        min_training_years=1,
    )
    folds = build_expanding_walk_forward_folds(daily_dates(date(2019, 1, 1), 365 * 4), window)
    assert len(folds) >= 3
    for previous, current in zip(folds, folds[1:]):
        assert previous.test_end < current.test_start
        assert (previous.validation_end - previous.validation_start).days > 0
        assert (previous.test_start - previous.validation_end).days == 41

    with pytest.raises(ValueError, match="below the horizon minimum"):
        WalkForwardWindow(
            horizon_bars=120,
            validation_months=18,
            test_months=18,
            step_months=18,
            embargo_days=120,
        )


def test_partition_uses_completed_target_date_not_signal_date_only() -> None:
    fold = WalkForwardFold(
        fold_id="h20_f01",
        horizon_bars=20,
        train_start=date(2020, 1, 1),
        train_end=date(2020, 6, 30),
        validation_start=date(2020, 8, 1),
        validation_end=date(2020, 10, 31),
        test_start=date(2020, 12, 1),
        test_end=date(2021, 3, 31),
        embargo_days=30,
    )
    rows = [
        {
            "ticker": "SAFE",
            "asof_date": "2020-06-01",
            "fwd_20d_target_date": "2020-07-01",
            "ret": 0.1,
        },
        {
            "ticker": "LEAK",
            "asof_date": "2020-06-15",
            "fwd_20d_target_date": "2020-08-05",
            "ret": 0.2,
        },
        {
            "ticker": "VALIDATION_LEAK",
            "asof_date": "2020-10-15",
            "fwd_20d_target_date": "2020-12-02",
            "ret": 0.3,
        },
        {
            "ticker": "TEST_SAFE",
            "asof_date": "2020-12-15",
            "fwd_20d_target_date": "2021-01-15",
            "ret": 0.4,
        },
    ]
    partition = partition_rows_for_fold(rows, fold, return_key="ret")
    assert [row["ticker"] for row in partition.train] == ["SAFE"]
    assert [row["ticker"] for row in partition.test] == ["TEST_SAFE"]
    assert partition.exclusion_reasons["train_target_crosses_validation"] == 1
    assert partition.exclusion_reasons["validation_target_crosses_test"] == 1


def test_profit_factor_requires_losses_and_reports_robust_variants() -> None:
    settings = MetricSettings(min_profit_factor_wins=3, min_profit_factor_losses=3)
    summary = summarize_returns([0.9, 0.2, 0.1, 0.1, -0.1, -0.1, -0.1], settings)
    assert summary["profit_factor"] == pytest.approx(13.0 / 3.0)
    assert summary["profit_factor_ex_largest_winner"] == pytest.approx(4.0 / 3.0)
    assert summary["profit_factor_ex_top3_winners"] == ""
    assert summarize_returns([0.1, 0.2, 0.3, 0.0], settings)["profit_factor"] == ""


def test_paired_comparison_uses_common_dates_only() -> None:
    settings = MetricSettings(bootstrap_iterations=50, min_profit_factor_wins=1, min_profit_factor_losses=1)
    comparison = paired_policy_comparison(
        {"2020-01-01": 0.2, "2020-01-02": -0.1, "2020-01-03": 9.0},
        {"2020-01-01": 0.1, "2020-01-02": -0.2, "2020-01-04": -9.0},
        settings,
    )
    assert comparison["paired_date_count"] == 2
    assert comparison["delta_mean_return_pct"] == pytest.approx(10.0)


def test_adaptive_threshold_changes_name_count_by_score_reliability() -> None:
    records = [
        ReliabilityRecord("2020-01-01", "A", 100.0, 0.1),
        ReliabilityRecord("2020-01-01", "B", 92.0, 0.2),
        ReliabilityRecord("2020-01-01", "C", 89.0, 0.3),
        ReliabilityRecord("2020-01-02", "D", 50.0, 0.1),
        ReliabilityRecord("2020-01-02", "E", 44.0, 0.2),
    ]
    selected, returns, counts = apply_reliability_threshold(
        records,
        min_score_pct_of_top=90.0,
        max_names=5,
    )
    assert [(row.asof_date, row.ticker) for row in selected] == [
        ("2020-01-01", "A"),
        ("2020-01-01", "B"),
        ("2020-01-02", "D"),
    ]
    assert counts == {"2020-01-01": 2, "2020-01-02": 1}
    assert returns["2020-01-01"] == pytest.approx(0.15)


def comparison_payload() -> dict[str, object]:
    return {
        "paired_date_count": 40,
        "paired_delta_bootstrap_lcb_pct": 1.5,
        "candidate_profit_factor": 1.5,
        "incumbent_profit_factor": 1.2,
        "delta_profit_factor": 1.25,
        "candidate_winsorized_profit_factor": 1.4,
        "candidate_profit_factor_ex_largest_winner": 1.3,
        "candidate_profit_factor_ex_top3_winners": 1.1,
        "candidate_loss20_rate_pct": 8.0,
        "incumbent_loss20_rate_pct": 8.0,
        "candidate_loss40_rate_pct": 2.0,
        "incumbent_loss40_rate_pct": 2.0,
        "candidate_cvar_return_pct": -12.0,
        "incumbent_cvar_return_pct": -12.0,
        "candidate_max_drawdown_pct": -18.0,
        "incumbent_max_drawdown_pct": -18.0,
        "candidate_top3_gain_contribution_pct": 40.0,
        "candidate_active_date_coverage_pct": 75.0,
        "calibration_fallback_frequency_pct": 0.0,
    }


def test_promotion_requires_relative_lcb_pf_and_fold_evidence() -> None:
    folds = [
        {"paired_delta_bootstrap_lcb_pct": 1.0},
        {"paired_delta_bootstrap_lcb_pct": 0.5},
    ]
    decision = decide_promotion(comparison_payload(), folds, PromotionRules())
    assert decision.status == "full_promotion"
    assert decision.authorized is True
    assert decision.provisional is False


def test_one_clean_outer_fold_can_only_receive_provisional_promotion() -> None:
    decision = decide_promotion(
        comparison_payload(),
        [{"paired_delta_bootstrap_lcb_pct": 1.0}],
        PromotionRules(),
    )
    assert decision.status == "provisional_blended_promotion"
    assert decision.authorized is True
    assert decision.provisional is True
    assert "outer_folds<2" in decision.reason_codes


def test_better_lcb_does_not_override_material_tail_concentration() -> None:
    payload = comparison_payload()
    payload["candidate_top3_gain_contribution_pct"] = 90.0
    decision = decide_promotion(
        payload,
        [
            {"paired_delta_bootstrap_lcb_pct": 1.0},
            {"paired_delta_bootstrap_lcb_pct": 1.0},
        ],
        PromotionRules(),
    )
    assert decision.status == "research_only_relative_improvement"
    assert decision.authorized is False
    assert "top3_contribution_too_high" in decision.reason_codes


def test_absolute_and_robust_profit_factor_are_real_promotion_gates() -> None:
    payload = comparison_payload()
    payload["candidate_profit_factor"] = 0.9
    payload["candidate_profit_factor_ex_largest_winner"] = 0.8
    decision = decide_promotion(
        payload,
        [
            {"paired_delta_bootstrap_lcb_pct": 1.0},
            {"paired_delta_bootstrap_lcb_pct": 1.0},
        ],
        PromotionRules(),
    )
    assert decision.authorized is False
    assert "candidate_profit_factor_below_absolute_floor" in decision.reason_codes
    assert "robust_profit_factor_below_floor" in decision.reason_codes


def test_paired_delta_profit_factor_is_a_co_primary_gate() -> None:
    payload = comparison_payload()
    payload["delta_profit_factor"] = 0.8
    decision = decide_promotion(
        payload,
        [
            {"paired_delta_bootstrap_lcb_pct": 1.0},
            {"paired_delta_bootstrap_lcb_pct": 1.0},
        ],
        PromotionRules(),
    )
    assert decision.authorized is False
    assert "paired_delta_profit_factor_below_floor" in decision.reason_codes


def test_missing_pf_support_cannot_receive_provisional_promotion() -> None:
    payload = comparison_payload()
    payload["candidate_profit_factor"] = ""
    decision = decide_promotion(
        payload,
        [{"paired_delta_bootstrap_lcb_pct": 1.0}],
        PromotionRules(),
    )
    assert decision.authorized is False
    assert "profit_factor_insufficient_support" in decision.reason_codes


def test_cvar_drawdown_coverage_and_fallback_are_hard_gates() -> None:
    payload = comparison_payload()
    payload.update(
        {
            "candidate_cvar_return_pct": -25.0,
            "candidate_max_drawdown_pct": -30.0,
            "candidate_active_date_coverage_pct": 10.0,
            "calibration_fallback_frequency_pct": 75.0,
        }
    )
    decision = decide_promotion(
        payload,
        [
            {"paired_delta_bootstrap_lcb_pct": 1.0},
            {"paired_delta_bootstrap_lcb_pct": 1.0},
        ],
        PromotionRules(),
    )
    assert decision.authorized is False
    assert "cvar_materially_worse" in decision.reason_codes
    assert "max_drawdown_materially_worse" in decision.reason_codes
    assert "active_date_coverage_below_floor" in decision.reason_codes
    assert "fallback_frequency_too_high" in decision.reason_codes


def test_provisional_deployment_weight_is_capped() -> None:
    provisional = decide_promotion(
        comparison_payload(),
        [{"paired_delta_bootstrap_lcb_pct": 1.0}],
        PromotionRules(provisional_active_weight_cap=0.55),
    )
    full = decide_promotion(
        comparison_payload(),
        [
            {"paired_delta_bootstrap_lcb_pct": 1.0},
            {"paired_delta_bootstrap_lcb_pct": 1.0},
        ],
        PromotionRules(provisional_active_weight_cap=0.55),
    )
    assert provisional.provisional is True
    assert deployment_active_weight(provisional, 0.90, PromotionRules()) == pytest.approx(0.55)
    assert deployment_active_weight(full, 0.90, PromotionRules()) == pytest.approx(0.90)


def test_supported_cohort_no_harm_failure_revokes_authorization() -> None:
    rules = PromotionRules()
    decision = decide_promotion(
        comparison_payload(),
        [
            {"paired_delta_bootstrap_lcb_pct": 1.0},
            {"paired_delta_bootstrap_lcb_pct": 1.0},
        ],
        rules,
    )
    failures = no_harm_reason_codes(
        primary_horizon=120,
        horizon_comparisons={120: comparison_payload()},
        cohort_comparisons=[
            {
                "fold_id": "aggregate",
                "horizon_days": 120,
                "cohort": "early_clinical_speculative_or_single_asset_pipeline",
                "paired_date_count": 20,
                "paired_delta_bootstrap_lcb_pct": -6.0,
            }
        ],
        rules=rules,
    )
    gated = apply_no_harm_gate(decision, failures)
    assert gated.status == "research_only_no_harm_failure"
    assert gated.authorized is False
    assert "cohort_no_harm_failure:early_clinical_speculative_or_single_asset_pipeline" in gated.reason_codes


def test_deployment_readiness_gate_fails_closed() -> None:
    decision = PromotionDecision(
        status="full_promotion",
        authorized=True,
        provisional=False,
        reason_codes=("all_relative_promotion_gates_passed",),
        metrics={},
    )

    gated = apply_deployment_readiness_gate(
        decision,
        deployment_ready=False,
        reason="live_scorer_parity_not_implemented:research_policy",
    )

    assert gated.authorized is False
    assert gated.provisional is False
    assert gated.status == "research_only_deployment_not_ready"
    assert gated.metrics["live_deployment_ready"] is False
    assert "live_scorer_parity_not_implemented:research_policy" in gated.reason_codes

def test_benchmark_residual_blend_fills_inactive_dates() -> None:
    blended = blend_active_alpha_with_benchmark(
        {"2020-01-01": 0.10},
        ["2020-01-01", "2020-01-02"],
        active_weight=0.20,
    )

    assert blended == {"2020-01-01": pytest.approx(0.02), "2020-01-02": 0.0}

def test_missing_required_no_harm_evidence_allows_only_provisional_authorization() -> None:
    rules = PromotionRules(
        required_secondary_horizons=(20, 60, 120),
        required_no_harm_cohorts=("platform",),
    )
    decision = decide_promotion(
        comparison_payload(),
        [
            {"paired_delta_bootstrap_lcb_pct": 1.0},
            {"paired_delta_bootstrap_lcb_pct": 1.0},
        ],
        rules,
    )
    failures = no_harm_reason_codes(
        primary_horizon=120,
        horizon_comparisons={120: comparison_payload()},
        cohort_comparisons=[],
        rules=rules,
    )
    gated = apply_no_harm_gate(decision, failures)

    assert gated.authorized is True
    assert gated.provisional is True
    assert "secondary_horizon_insufficient_evidence:20d" in gated.reason_codes
    assert "secondary_horizon_insufficient_evidence:60d" in gated.reason_codes
    assert "cohort_insufficient_evidence:platform" in gated.reason_codes
