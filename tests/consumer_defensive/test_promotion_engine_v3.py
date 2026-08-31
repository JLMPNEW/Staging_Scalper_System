from __future__ import annotations

import copy
from datetime import date, timedelta
from pathlib import Path
import consumer_defensive.core.promotion_engine_v3 as promotion_engine_v3

import pytest

from consumer_defensive.core.promotion_evidence_v3 import (
    build_fresh_evidence_manifest,
    build_registration_anchor,
    build_review_preregistration,
)
from consumer_defensive.core.promotion_engine_v3 import (
    PROMOTION_INPUT_SCHEMA,
    REQUIRED_COHORTS,
    allocation_adjusted_capacity_evidence,
    apply_activation_to_rank_rows,
    build_activation_registry,
    build_capital_allocation_context,
    build_production_model_contract,
    build_promotion_decision,
    compute_path_metrics,
    confidence_multiplier,
    load_framework,
    score_metric,
    seal_promotion_input,
    target_state_for_score,
    validate_activation_registry,
    validate_promotion_decision,
    value_sha256,
)


ROOT = Path(__file__).resolve().parents[2]
FRAMEWORK_PATH = (
    ROOT
    / "consumer_defensive/data/consumer_defensive_promotion_framework_v3.yaml"
)
METHODOLOGY_FILE_HASHES = {"promotion_engine_v3.py": "e" * 64}
METHODOLOGY_SHA256 = value_sha256(METHODOLOGY_FILE_HASHES)


def _performance(*, count: int = 40) -> dict[str, float | int]:
    return {
        "paired_net_alpha_lcb": 0.02,
        "net_alpha_mean": 0.03,
        "absolute_profit_factor": 2.0,
        "relative_profit_factor": 2.0,
        "robust_profit_factor": 2.0,
        "deflated_sharpe_ratio": 0.9,
        "probability_of_backtest_overfitting": 0.1,
        "maximum_drawdown": 0.10,
        "expected_shortfall_95": -0.02,
        "turnover": 0.50,
        "average_transaction_cost": 0.001,
        "liquidity_capacity_ratio": 4.0,
        "winner_concentration_hhi": 0.10,
        "maximum_single_name_weight": 0.10,
        "paired_observation_count": count,
        "positive_return_count": count - 10,
        "negative_return_count": 10,
    }


def _path(*, start: date = date(2025, 1, 2), count: int = 100):
    return [
        {
            "date": (start + timedelta(days=position)).isoformat(),
            "strategy_net_return": 0.0015,
            "primary_benchmark_return": 0.0002,
            "xlp_return": 0.0001,
            "spy_return": 0.0003,
        }
        for position in range(count)
    ]

def _outer_oos(*, count: int = 40, include_fresh: bool = False) -> list[dict[str, str]]:
    rows = [
        {
            "observation_id": f"outer-{position:03d}",
            "fold_id": f"fold-{position // 10:02d}",
            "signal_date": (date(2025, 1, 1) + timedelta(days=position)).isoformat(),
            "label_completion_date": (
                date(2025, 1, 2) + timedelta(days=position)
            ).isoformat(),
        }
        for position in range(count - int(include_fresh))
    ]
    if include_fresh:
        rows.append(
            {
                "observation_id": "outer-fresh-040",
                "fold_id": "fold-fresh",
                "signal_date": "2026-02-02",
                "label_completion_date": "2026-02-03",
            }
        )
    return rows


def _path_with_optional_fresh(*, include_fresh: bool) -> list[dict[str, object]]:
    rows = _path()
    if include_fresh:
        rows.append(
            {
                "date": "2026-02-03",
                "strategy_net_return": 0.0015,
                "primary_benchmark_return": 0.0002,
                "xlp_return": 0.0001,
                "spy_return": 0.0003,
            }
        )
    return rows


def _input(
    *,
    evidence_role: str = "design_evidence",
    asof: str = "2026-01-31",
    count: int = 40,
    include_fresh: bool = False,
    capital_asof: str | None = None,
    input_panel_sha256: str = "3" * 64,
) -> dict:
    framework = load_framework(FRAMEWORK_PATH)
    registry_hash = "a" * 64
    cohorts = {}
    for cohort in sorted(REQUIRED_COHORTS):
        contract = build_production_model_contract(
            cohort=cohort,
            selected_candidate_id=f"{cohort}_champion",
            candidate_definition={
                "candidate_id": f"{cohort}_champion",
                "core_weights": {"quality": 1.0},
                "specialized_weights": {},
            },
            candidate_registry_sha256=registry_hash,
            score_model_version="consumer_defensive_v3_test",
            scoring_contract_version="consumer_defensive_v3_test",
        )
        cohorts[cohort] = {
            "production_model_contract": contract,
            "horizons": {
                str(horizon): {
                    "performance": _performance(count=count),
                    "daily_path": _path_with_optional_fresh(include_fresh=include_fresh),
                    "outer_oos_observations": _outer_oos(
                        count=count, include_fresh=include_fresh
                    ),
                }
                for horizon in (21, 63, 126)
            },
        }
    return seal_promotion_input(
        {
            "schema_version": PROMOTION_INPUT_SCHEMA,
            "model_family": "consumer_defensive",
            "asof_date": asof,
            "framework_sha256": __import__(
                "consumer_defensive.core.promotion_engine_v3",
                fromlist=["framework_sha256"],
            ).framework_sha256(framework),
            "evidence_role": evidence_role,
            "capital_allocation_context": build_capital_allocation_context(
                asof_date=asof if capital_asof is None else capital_asof,
                account_aum_usd=500_000.0,
                active_sector_count=8,
                sector_max_fraction=0.125,
                calibration_reference_notional_usd=1_000_000.0,
            ),
            "source_lineage": {
                "source_decision_sha256": "1" * 64,
                "source_results_sha256": "2" * 64,
                "input_panel_sha256": input_panel_sha256,
                "fold_registry_sha256": "4" * 64,
                "candidate_registry_sha256": registry_hash,
                "code_sha256": METHODOLOGY_SHA256,
                "benchmark_path_source_sha256": "6" * 64,
            },
            "safety_attestations": {
                "independent_validation_passed": True,
                "source_hashes_verified": True,
                "outer_oos_only": True,
                "no_lookahead": True,
                "chronology_complete": True,
                "returns_net_of_costs": True,
                "matched_daily_benchmark": True,
                "corporate_actions_reconciled": True,
                "terminal_events_reconciled": True,
                "production_model_contract_bound": True,
            },
            "cohorts": cohorts,
        }
    )


def test_framework_and_metric_normalization_are_frozen_and_monotone() -> None:
    framework = load_framework(FRAMEWORK_PATH)
    assert framework["model_family"] == "consumer_defensive"
    high = {"direction": "higher", "bad": -1.0, "neutral": 0.0, "good": 1.0}
    low = {"direction": "lower", "bad": 1.0, "neutral": 0.0, "good": -1.0}
    assert [score_metric(value, high) for value in (-2.0, -1.0, 0.0, 1.0, 2.0)] == [
        0.0,
        0.0,
        50.0,
        100.0,
        100.0,
    ]
    assert score_metric(-1.0, low) == 100.0
    assert score_metric(1.0, low) == 0.0


def test_model_contract_rejects_a_definition_for_a_different_candidate() -> None:
    with pytest.raises(ValueError, match="not bound"):
        build_production_model_contract(
            cohort="beverages",
            selected_candidate_id="beverages_champion",
            candidate_definition={
                "candidate_id": "different_candidate",
                "core_weights": {"quality": 1.0},
                "specialized_weights": {},
            },
            candidate_registry_sha256="a" * 64,
            score_model_version="consumer_defensive_v3_test",
            scoring_contract_version="consumer_defensive_v3_test",
        )


def test_compounded_path_metrics_are_not_arithmetic_shortcuts() -> None:
    rows = [
        {
            "date": "2026-01-02",
            "strategy_net_return": 0.10,
            "primary_benchmark_return": 0.0,
            "xlp_return": 0.0,
            "spy_return": 0.0,
        },
        {
            "date": "2026-01-03",
            "strategy_net_return": -0.10,
            "primary_benchmark_return": 0.0,
            "xlp_return": 0.0,
            "spy_return": 0.0,
        },
    ]
    metrics = compute_path_metrics(rows)
    assert metrics["cumulative_net_return"] == pytest.approx(-0.01)
    assert metrics["relative_wealth"] == pytest.approx(0.99)
    assert metrics["maximum_drawdown"] == pytest.approx(0.10)
    assert metrics["net_pnl_per_starting_dollar"] == pytest.approx(-0.01)


def test_confidence_formula_and_standard_eligibility_boundary() -> None:
    framework = load_framework(FRAMEWORK_PATH)
    assert confidence_multiplier(
        deflated_sharpe_ratio=0.0,
        probability_of_backtest_overfitting=1.0,
        framework=framework,
    ) == pytest.approx(0.50)
    assert confidence_multiplier(
        deflated_sharpe_ratio=1.0,
        probability_of_backtest_overfitting=0.0,
        framework=framework,
    ) == pytest.approx(1.00)
    assert confidence_multiplier(
        deflated_sharpe_ratio=0.8,
        probability_of_backtest_overfitting=0.2,
        framework=framework,
    ) == pytest.approx(0.90)
    expected = {
        59.999: "benchmark_production",
        60.0: "active_full",
        69.999: "active_full",
        70.0: "active_full",
        79.999: "active_full",
        80.0: "active_full",
        89.999: "active_full",
        90.0: "active_full",
    }
    assert {
        score: target_state_for_score(score, framework=framework)
        for score in expected
    } == expected


def test_capacity_is_rebased_to_the_full_consumer_sector_budget() -> None:
    context = build_capital_allocation_context(
        asof_date="2026-08-28",
        account_aum_usd=500_000.0,
        active_sector_count=8,
        sector_max_fraction=0.125,
        calibration_reference_notional_usd=1_000_000.0,
    )
    performance = {str(horizon): _performance() for horizon in (21, 63, 126)}
    for item in performance.values():
        item["liquidity_capacity_ratio"] = 0.23649023760386881

    adjusted, audit = allocation_adjusted_capacity_evidence(
        performance,
        capital_allocation_context=context,
    )

    assert performance["21"]["liquidity_capacity_ratio"] == pytest.approx(
        0.23649023760386881
    )
    assert audit["21"]["executable_capacity_usd"] == pytest.approx(
        236_490.23760386882
    )
    assert audit["21"][
        "allocation_adjusted_liquidity_capacity_ratio"
    ] == pytest.approx(3.783843801661901)
    assert adjusted["21"]["liquidity_capacity_ratio"] == pytest.approx(
        3.783843801661901
    )


def test_promotion_input_requires_a_sealed_capital_context() -> None:
    framework = load_framework(FRAMEWORK_PATH)
    source = _input()
    source.pop("capital_allocation_context")
    source = seal_promotion_input(source)

    with pytest.raises(ValueError, match="promotion input v3 must contain exactly"):
        build_promotion_decision(promotion_input=source, framework=framework)


def test_all_layers_use_adjusted_capacity_while_decision_preserves_raw_ratio() -> None:
    framework = load_framework(FRAMEWORK_PATH)
    source = _input(asof="2026-08-27", capital_asof="2026-08-28")
    for cohort in source["cohorts"].values():
        for horizon in cohort["horizons"].values():
            horizon["performance"]["liquidity_capacity_ratio"] = (
                0.23649023760386881
            )
    source = seal_promotion_input(source)

    decision = build_promotion_decision(
        promotion_input=source,
        framework=framework,
    )

    assert decision["asof_date"] == "2026-08-27"
    assert decision["evidence_asof_date"] == "2026-08-27"
    assert decision["capital_allocation_context_asof_date"] == "2026-08-28"
    assert decision["capital_context_is_predictive_evidence"] is False
    for item in decision["cohorts"].values():
        assert item["data_and_safety_status"] == "PASS"
        assert item["hard_failures"] == []
        assert item["horizon_performance"]["21"][
            "liquidity_capacity_ratio"
        ] == pytest.approx(0.23649023760386881)
        assert item["minimum_executable_capacity_usd"] == pytest.approx(
            236_490.23760386882
        )
        assert item[
            "minimum_allocation_adjusted_liquidity_capacity_ratio"
        ] == pytest.approx(3.783843801661901)
        assert item["capacity_modifier"] == pytest.approx(1.0)
        expected_score = score_metric(
            3.783843801661901,
            framework["layer_2_economic_performance"]["metric_anchors"][
                "liquidity_capacity_ratio"
            ],
        )
        assert item["horizon_metric_scores"]["21"][
            "liquidity_capacity_ratio"
        ] == pytest.approx(expected_score)

    registry = build_activation_registry(
        decision=decision,
        promotion_input=source,
        framework=framework,
    )
    assert registry["asof_date"] == "2026-08-27"
    assert registry["effective_from"] == "2026-08-28"


def test_capacity_below_the_full_sector_budget_still_fails_closed() -> None:
    framework = load_framework(FRAMEWORK_PATH)
    source = _input()
    for cohort in source["cohorts"].values():
        for horizon in cohort["horizons"].values():
            horizon["performance"]["liquidity_capacity_ratio"] = 0.05
    source = seal_promotion_input(source)

    decision = build_promotion_decision(promotion_input=source, framework=framework)

    for item in decision["cohorts"].values():
        assert item["data_and_safety_status"] == "FAIL"
        assert item[
            "minimum_allocation_adjusted_liquidity_capacity_ratio"
        ] == pytest.approx(0.8)
        assert all(
            failure.endswith(":minimum_liquidity_capacity_ratio")
            for failure in item["hard_failures"]
        )
        assert item["optimizer_cap"] == 0.0


def test_capacity_at_the_safety_threshold_receives_full_credit() -> None:
    framework = load_framework(FRAMEWORK_PATH)
    source = _input()
    for cohort in source["cohorts"].values():
        for horizon in cohort["horizons"].values():
            # $62,500 executable capacity / $1,000,000 calibration reference.
            horizon["performance"]["liquidity_capacity_ratio"] = 0.0625
    source = seal_promotion_input(source)

    decision = build_promotion_decision(promotion_input=source, framework=framework)

    for item in decision["cohorts"].values():
        assert item[
            "minimum_allocation_adjusted_liquidity_capacity_ratio"
        ] == pytest.approx(1.0)
        assert item["capacity_modifier"] == pytest.approx(1.0)
        assert item["standard_production_eligible"] is True
        assert item["optimizer_cap"] == pytest.approx(0.03125)


def test_failed_cohort_standard_slot_stays_in_cash() -> None:
    framework = load_framework(FRAMEWORK_PATH)
    source = _input()
    for horizon in source["cohorts"]["beverages"]["horizons"].values():
        horizon["performance"]["liquidity_capacity_ratio"] = 0.05
    source = seal_promotion_input(source)

    decision = build_promotion_decision(promotion_input=source, framework=framework)

    beverage = decision["cohorts"]["beverages"]
    assert beverage["standard_production_eligible"] is False
    assert beverage["optimizer_cap"] == pytest.approx(0.0)
    assert decision["standard_production_eligible_cohort_count"] == 3
    assert decision["allocated_sector_fraction"] == pytest.approx(0.09375)
    assert decision["allocated_sector_notional_usd"] == pytest.approx(46_875.0)
    assert decision["unallocated_sector_fraction"] == pytest.approx(0.03125)
    assert decision["unallocated_sector_notional_usd"] == pytest.approx(15_625.0)
    for cohort, item in decision["cohorts"].items():
        if cohort != "beverages":
            assert item["optimizer_cap"] == pytest.approx(0.03125)


def test_within_limit_concentration_does_not_haircut_standard_allocation() -> None:
    framework = load_framework(FRAMEWORK_PATH)
    source = _input()
    for cohort in source["cohorts"].values():
        for horizon in cohort["horizons"].values():
            horizon["performance"]["winner_concentration_hhi"] = 0.34
            horizon["performance"]["maximum_single_name_weight"] = 0.24
    source = seal_promotion_input(source)

    decision = build_promotion_decision(promotion_input=source, framework=framework)

    for item in decision["cohorts"].values():
        assert item["data_and_safety_status"] == "PASS"
        assert item["standard_production_eligible"] is True
        assert item["diversification_modifier"] == pytest.approx(1.0)
        assert item["cohort_level_diversification_haircut_applied"] is False
        assert item["optimizer_cap"] == pytest.approx(0.03125)


def test_economic_score_below_sixty_is_safe_but_not_production_eligible() -> None:
    framework = load_framework(FRAMEWORK_PATH)
    source = _input()
    for cohort in source["cohorts"].values():
        for horizon in cohort["horizons"].values():
            performance = horizon["performance"]
            performance["paired_net_alpha_lcb"] = -0.05
            performance["absolute_profit_factor"] = 0.75
            performance["relative_profit_factor"] = 0.75
            performance["robust_profit_factor"] = 0.75
            performance["deflated_sharpe_ratio"] = 0.0
            performance["probability_of_backtest_overfitting"] = 1.0
            performance["turnover"] = 2.0
            performance["average_transaction_cost"] = 0.01
            performance["winner_concentration_hhi"] = 0.35
            performance["maximum_single_name_weight"] = 0.25
            for row in horizon["daily_path"]:
                row["strategy_net_return"] = -0.0005
    source = seal_promotion_input(source)

    decision = build_promotion_decision(promotion_input=source, framework=framework)

    for item in decision["cohorts"].values():
        assert item["data_and_safety_status"] == "PASS"
        assert item["confidence_adjusted_score"] < 60.0
        assert item["state"] == "benchmark_production"
        assert item["standard_production_eligible"] is False
        assert item["optimizer_cap"] == pytest.approx(0.0)


def test_capital_only_change_cannot_satisfy_new_input_panel_requirement() -> None:
    framework = load_framework(FRAMEWORK_PATH)
    prior = {
        "state": "benchmark_production",
        "state_entered_asof": "2026-01-01",
        "transition_blockers": [],
        "paired_observation_count_by_horizon": {"21": 40, "63": 40, "126": 40},
    }

    state, entered, blockers = promotion_engine_v3._transition_state(
        prior_item=prior,
        target="active_full",
        hard_failures=(),
        evidence_role="fresh_chronological",
        material_model_change=False,
        asof_date=date(2026, 8, 28),
        current_input_panel_sha256="3" * 64,
        prior_input_panel_sha256="3" * 64,
        current_counts={"21": 41, "63": 41, "126": 41},
        framework=framework,
    )

    assert state == "benchmark_production"
    assert entered == "2026-01-01"
    assert "new_input_panel_required" in blockers


def test_genesis_qualifier_receives_standard_allocation_and_is_reproducible() -> None:
    framework = load_framework(FRAMEWORK_PATH)
    source = _input()
    decision = build_promotion_decision(
        promotion_input=source,
        framework=framework,
    )
    validate_promotion_decision(
        decision,
        promotion_input=source,
        framework=framework,
    )
    for item in decision["cohorts"].values():
        assert item["data_and_safety_status"] == "PASS"
        assert item["economic_target_state"] == "active_full"
        assert item["state"] == "active_full"
        assert item["standard_production_eligible"] is True
        assert item["tier_deployment_fraction"] == pytest.approx(1.0)
        assert item["effective_deployment_fraction"] == pytest.approx(1.0)
        assert item["capacity_modifier"] == pytest.approx(1.0)
        assert item["diversification_modifier"] == pytest.approx(1.0)
        assert item["cohort_level_diversification_haircut_applied"] is False
        assert item["approved_full_portfolio_cap"] == pytest.approx(0.03125)
        assert item["optimizer_cap"] == pytest.approx(0.03125)
        assert item["standard_allocation_notional_usd"] == pytest.approx(15_625.0)
        assert item["confidence_adjusted_score"] == pytest.approx(
            50.0
            + item["confidence_multiplier"]
            * (item["base_economic_score"] - 50.0)
        )
    assert decision["standard_production_eligible_cohort_count"] == 4
    assert decision["allocated_sector_fraction"] == pytest.approx(0.125)
    assert decision["allocated_sector_notional_usd"] == pytest.approx(62_500.0)
    assert decision["unallocated_sector_fraction"] == pytest.approx(0.0)


def test_low_pf_and_lcb_reduce_score_but_are_not_hard_safety_vetoes() -> None:
    framework = load_framework(FRAMEWORK_PATH)
    source = _input()
    for cohort in source["cohorts"].values():
        for horizon in cohort["horizons"].values():
            horizon["performance"]["paired_net_alpha_lcb"] = -0.04
            horizon["performance"]["absolute_profit_factor"] = 0.80
            horizon["performance"]["relative_profit_factor"] = 0.80
            horizon["performance"]["robust_profit_factor"] = 0.80
    source = seal_promotion_input(source)
    decision = build_promotion_decision(promotion_input=source, framework=framework)
    assert all(
        item["data_and_safety_status"] == "PASS"
        and not any("profit_factor" in failure for failure in item["hard_failures"])
        for item in decision["cohorts"].values()
    )


def test_hard_failure_dominates_a_high_score_and_zeroes_cap() -> None:
    framework = load_framework(FRAMEWORK_PATH)
    source = _input()
    source["safety_attestations"]["no_lookahead"] = False
    source = seal_promotion_input(source)
    decision = build_promotion_decision(promotion_input=source, framework=framework)
    for item in decision["cohorts"].values():
        assert item["data_and_safety_status"] == "FAIL"
        assert "attestation:no_lookahead" in item["hard_failures"]
        assert item["state"] == "benchmark_production"
        assert item["optimizer_cap"] == 0.0


def test_fresh_evidence_reauthorizes_standard_production() -> None:
    framework = load_framework(FRAMEWORK_PATH)
    first_input = _input()
    first = build_promotion_decision(
        promotion_input=first_input,
        framework=framework,
    )
    second_input = _input(
        evidence_role="fresh_chronological",
        asof="2026-04-30",
        count=41,
        include_fresh=True,
        input_panel_sha256="7" * 64,
    )
    outer_dates = {
        cohort: {str(horizon): ["2026-02-02"] for horizon in (21, 63, 126)}
        for cohort in sorted(REQUIRED_COHORTS)
    }
    plan = build_review_preregistration(
        review_id="consumer-v3-test-review",
        registered_at_utc="2026-01-31T20:00:00Z",
        fresh_start_exclusive="2026-01-31",
        scheduled_decision_asof="2026-04-30",
        eligible_return_dates=["2026-02-03"],
        eligible_outer_oos_dates_by_cohort_horizon=outer_dates,
        minimum_new_paired_observations_by_horizon={
            "21": 1,
            "63": 1,
            "126": 1,
        },
        methodology_file_sha256s=METHODOLOGY_FILE_HASHES,
        framework=framework,
        previous_decision=first,
        previous_promotion_input=first_input,
        trusted_previous_decision_sha256=first["payload_sha256"],
    )
    anchor = build_registration_anchor(
        preregistration=plan,
        framework=framework,
        anchor_created_at_utc="2026-01-31T20:01:00Z",
        registration_authority="independent-test-authority",
        anchor_id="anchor-001",
    )
    manifest = build_fresh_evidence_manifest(
        preregistration=plan,
        registration_anchor=anchor,
        trusted_anchor_sha256=anchor["payload_sha256"],
        previous_decision=first,
        previous_promotion_input=first_input,
        current_promotion_input=second_input,
        framework=framework,
    )
    second = build_promotion_decision(
        promotion_input=second_input,
        framework=framework,
        previous_decision=first,
        trusted_previous_decision_sha256=first["payload_sha256"],
        previous_promotion_input=first_input,
        preregistration=plan,
        registration_anchor=anchor,
        trusted_registration_anchor_sha256=anchor["payload_sha256"],
        fresh_evidence_manifest=manifest,
    )
    evidence_kwargs = {
        "trusted_previous_decision_sha256": first["payload_sha256"],
        "previous_promotion_input": first_input,
        "preregistration": plan,
        "registration_anchor": anchor,
        "trusted_registration_anchor_sha256": anchor["payload_sha256"],
        "fresh_evidence_manifest": manifest,
    }
    assert validate_promotion_decision(
        second,
        promotion_input=second_input,
        framework=framework,
        previous_decision=first,
        **evidence_kwargs,
    ) == second
    fresh_registry = build_activation_registry(
        decision=second,
        promotion_input=second_input,
        framework=framework,
        previous_decision=first,
        **evidence_kwargs,
    )
    assert validate_activation_registry(fresh_registry) == fresh_registry
    assert {
        item["deployment_state"]
        for item in fresh_registry["cohorts"].values()
    } == {"active_full"}
    assert all(
        item["investable"] and item["optimizer_cap"] > 0.0
        for item in fresh_registry["cohorts"].values()
    )
    for item in second["cohorts"].values():
        assert item["state"] == "active_full"
        assert item["standard_production_eligible"] is True
        assert item["optimizer_cap"] == pytest.approx(0.03125)
        assert item["transition_blockers"] == []


@pytest.mark.parametrize("late_field", ["effective_from", "valid_until"])
def test_activation_authority_is_anchored_to_decision_date(late_field: str) -> None:
    framework = load_framework(FRAMEWORK_PATH)
    source = _input()
    decision = build_promotion_decision(
        promotion_input=source,
        framework=framework,
    )
    decision_date = date.fromisoformat(decision["asof_date"])
    kwargs: dict[str, str] = {}
    if late_field == "effective_from":
        late = (decision_date + timedelta(days=64)).isoformat()
        kwargs = {"effective_from": late, "valid_until": late}
    else:
        kwargs = {
            "effective_from": (decision_date + timedelta(days=1)).isoformat(),
            "valid_until": (decision_date + timedelta(days=64)).isoformat(),
        }

    with pytest.raises(ValueError, match="decision authority window"):
        build_activation_registry(
            decision=decision,
            promotion_input=source,
            framework=framework,
            **kwargs,
        )


def test_activation_registry_is_hash_bound_and_rank_overlay_is_explicit() -> None:
    framework = load_framework(FRAMEWORK_PATH)
    source = _input()
    decision = build_promotion_decision(promotion_input=source, framework=framework)
    registry = build_activation_registry(
        decision=decision,
        promotion_input=source,
        framework=framework,
    )
    validate_activation_registry(registry)
    lock = registry["cohorts"]["beverages"]
    rows = apply_activation_to_rank_rows(
        [
            {
                "ticker": "KO",
                "asof_date": registry["effective_from"],
                "calibration_cohort": "beverages",
                "rank_ready_flag": "1",
                "oos_score_valid_flag": "1",
                "score_model_version": lock["score_model_version"],
                "scoring_contract_version": lock["scoring_contract_version"],
                "consumer_defensive_selected_candidate_id": lock[
                    "selected_candidate_id"
                ],
                "consumer_defensive_model_contract_sha256": lock[
                    "model_contract_sha256"
                ],
            }
        ],
        activation_registry=registry,
    )
    assert rows[0]["promotion_state"] == "promoted"
    assert rows[0]["portfolio_candidate_gate"] == 1
    assert rows[0]["consumer_defensive_production_lock_sha256"] == lock[
        "payload_sha256"
    ]
    tampered = copy.deepcopy(registry)
    tampered["cohorts"]["beverages"]["optimizer_cap"] = 0.20
    tampered["cohorts"]["beverages"]["payload_sha256"] = __import__(
        "consumer_defensive.core.promotion_engine_v3", fromlist=["canonical_sha256"]
    ).canonical_sha256(tampered["cohorts"]["beverages"])
    tampered["payload_sha256"] = __import__(
        "consumer_defensive.core.promotion_engine_v3", fromlist=["canonical_sha256"]
    ).canonical_sha256(tampered)
    with pytest.raises(ValueError):
        validate_activation_registry(tampered)


def test_promoted_cohort_keeps_a_ticker_without_oos_evidence_noninvestable() -> None:
    framework = load_framework(FRAMEWORK_PATH)
    source = _input()
    decision = build_promotion_decision(promotion_input=source, framework=framework)
    registry = build_activation_registry(
        decision=decision,
        promotion_input=source,
        framework=framework,
    )
    lock = registry["cohorts"]["beverages"]
    [row] = apply_activation_to_rank_rows(
        [{
            "ticker": "KO",
            "asof_date": registry["effective_from"],
            "calibration_cohort": "beverages",
            "rank_ready_flag": "1",
            "oos_score_valid_flag": "0",
            "oos_invalid_reason": "insufficient_ticker_history",
            "score_model_version": lock["score_model_version"],
            "scoring_contract_version": lock["scoring_contract_version"],
            "consumer_defensive_selected_candidate_id": lock["selected_candidate_id"],
            "consumer_defensive_model_contract_sha256": lock["model_contract_sha256"],
        }],
        activation_registry=registry,
    )
    assert row["promotion_state"] == "promoted"
    assert row["portfolio_candidate_gate"] == 0
    assert row["portfolio_candidate_status"] == "not_eligible"
    assert row["oos_score_valid_flag"] == "0"
    assert row["oos_invalid_reason"] == "insufficient_ticker_history"


def test_shadow_time_after_a_blocker_does_not_preserve_state_entry_date() -> None:
    framework = load_framework(FRAMEWORK_PATH)
    prior = {
        "state": "active_full",
        "state_entered_asof": "2026-01-31",
        "transition_blockers": ["material_model_change_reset"],
        "paired_observation_count_by_horizon": {
            "21": 40,
            "63": 40,
            "126": 40,
        },
    }
    state, entered, blockers = promotion_engine_v3._transition_state(
        prior_item=prior,
        target="active_full",
        hard_failures=(),
        evidence_role="fresh_chronological",
        material_model_change=False,
        asof_date=date(2026, 7, 31),
        current_input_panel_sha256="2" * 64,
        prior_input_panel_sha256="1" * 64,
        current_counts={"21": 41, "63": 41, "126": 41},
        framework=framework,
    )
    assert (state, entered, blockers) == (
        "active_full",
        "2026-07-31",
        [],
    )


def test_decision_tampering_fails_even_after_self_rehash_without_input_match() -> None:
    framework = load_framework(FRAMEWORK_PATH)
    source = _input()
    decision = build_promotion_decision(promotion_input=source, framework=framework)
    tampered = copy.deepcopy(decision)
    tampered["cohorts"]["beverages"]["base_economic_score"] = 99.0
    tampered["payload_sha256"] = __import__(
        "consumer_defensive.core.promotion_engine_v3",
        fromlist=["canonical_sha256"],
    ).canonical_sha256(tampered)
    with pytest.raises(ValueError, match="does not reproduce"):
        validate_promotion_decision(
            tampered,
            promotion_input=source,
            framework=framework,
        )
