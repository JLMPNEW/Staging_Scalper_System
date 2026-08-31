from __future__ import annotations

import copy
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

from consumer_defensive.core.calibration_v2 import (
    RealizedReturnObservation,
    ReturnObservation,
    SelectedPortfolioObservation,
    WalkForwardFold,
    build_calibration_decision,
    build_nested_purged_walk_forward,
    evaluate_all_horizons,
    evaluate_cohort,
    probability_of_backtest_overfitting,
    recommend_next_state,
)
from consumer_defensive.core.promotion_framework_v2 import (
    REQUIRED_COHORTS,
    canonical_sha256,
    load_framework,
    validate_calibration_decision,
)


ROOT = Path(__file__).resolve().parents[2]
FRAMEWORK_PATH = ROOT / "consumer_defensive/data/consumer_defensive_promotion_framework_v2.yaml"


def _observations(cohort: str, *, horizon: int, alpha: float, count: int = 40) -> list[ReturnObservation]:
    start = date(2019, 1, 2)
    return [
        ReturnObservation(
            observation_id=f"{cohort}:{horizon}:{index:03d}",
            fold_id=f"outer_{index // 7 + 1:02d}",
            evaluation_role="outer_test",
            asof_date=start + timedelta(days=31 * index),
            label_completion_date=start + timedelta(days=31 * index + horizon),
            cohort=cohort,
            horizon_sessions=horizon,
            strategy_return=0.002 + alpha + (-0.04 if index % 5 == 0 else 0.005),
            benchmark_return=0.002,
            transaction_cost=0.0002,
            turnover=0.10,
            liquidity_capacity_ratio=2.0,
        )
        for index in range(count)
    ]


def _outer_folds(rows: list[ReturnObservation]) -> tuple[WalkForwardFold, ...]:
    groups: dict[str, list[date]] = {}
    for row in rows:
        groups.setdefault(row.fold_id, []).append(row.asof_date)
    folds = []
    for fold_id, test_dates in sorted(groups.items()):
        first = min(test_dates)
        folds.append(
            WalkForwardFold(
                fold_id=fold_id,
                train_dates=(first - timedelta(days=3), first - timedelta(days=2)),
                validation_dates=(first - timedelta(days=1),),
                test_dates=tuple(sorted(test_dates)),
                purged_train_count=0,
                purged_validation_count=0,
            )
        )
    return tuple(folds)


def _realized(
    rows: list[ReturnObservation],
    *,
    count: int = 250,
) -> list[RealizedReturnObservation]:
    ordered = sorted(rows, key=lambda row: (row.asof_date, row.observation_id))
    if count < len(ordered):
        raise ValueError("test realized-return census must cover every selected portfolio")
    base_size, extra = divmod(count, len(ordered))
    result: list[RealizedReturnObservation] = []
    sequence = 0
    for portfolio_index, row in enumerate(ordered):
        block_size = base_size + (1 if portfolio_index < extra else 0)
        for day_index in range(block_size):
            result.append(
                RealizedReturnObservation(
                    observation_id=f"{row.observation_id}:daily:{day_index + 1:02d}",
                    source_portfolio_observation_id=row.observation_id,
                    fold_id=row.fold_id,
                    evaluation_role="outer_test",
                    return_date=row.asof_date + timedelta(days=day_index + 1),
                    cohort=row.cohort,
                    horizon_sessions=row.horizon_sessions,
                    strategy_return=-0.003 if sequence % 8 == 0 else 0.002,
                    transaction_cost=0.0001,
                )
            )
            sequence += 1
    return result


def _selected_portfolios(
    rows: list[ReturnObservation],
    *,
    weights: tuple[tuple[str, float], ...] | None = None,
) -> list[SelectedPortfolioObservation]:
    holdings = weights or tuple((f"T{index:02d}", 0.1) for index in range(10))
    return [
        SelectedPortfolioObservation(
            observation_id=row.observation_id,
            fold_id=row.fold_id,
            asof_date=row.asof_date,
            cohort=row.cohort,
            horizon_sessions=row.horizon_sessions,
            selected_candidate_id="winner",
            weights=holdings,
        )
        for row in rows
    ]

def _stable_candidates() -> dict[str, dict[str, float]]:
    values = {
        "winner": [0.020, -0.010, 0.025, 0.015, -0.005, 0.025],
        "middle": [0.015, -0.015, 0.020, 0.010, -0.010, 0.020],
        "loser": [0.010, -0.020, 0.015, 0.005, -0.015, 0.015],
    }
    return {
        candidate: {f"outer_{index + 1:02d}": value for index, value in enumerate(row)}
        for candidate, row in values.items()
    }


def _cohort_results(
    cohort: str,
    *,
    alpha: float,
    count: int = 40,
    realized_count: int = 250,
    decision_asof: date = date(2024, 1, 2),
) -> dict[str, dict[str, object]]:
    framework = load_framework(FRAMEWORK_PATH)
    observations = [
        row for horizon in (21, 63, 126) for row in _observations(cohort, horizon=horizon, alpha=alpha, count=count)
    ]
    rows_by_horizon = {
        key: [row for row in observations if row.horizon_sessions == int(key)]
        for key in ("21", "63", "126")
    }
    folds_by_horizon = {key: _outer_folds(rows_by_horizon[key]) for key in ("21", "63", "126")}
    return evaluate_all_horizons(
        observations,
        realized_returns_by_horizon={
            key: _realized(rows_by_horizon[key], count=realized_count)
            for key in ("21", "63", "126")
        },
        outer_test_folds_by_horizon=folds_by_horizon,
        decision_asof=decision_asof,
        framework=framework,
        candidate_performance_by_horizon={key: _stable_candidates() for key in ("21", "63", "126")},
        selected_portfolios_by_horizon={
            key: _selected_portfolios(rows_by_horizon[key])
            for key in ("21", "63", "126")
        },
    )

def _all_results(
    *, count: int = 40, realized_count: int = 250, decision_asof: date
) -> dict[str, dict[str, dict[str, object]]]:
    return {
        cohort: _cohort_results(
            cohort,
            alpha=-0.01 if cohort == "household_personal_tobacco" else 0.02,
            count=count,
            realized_count=realized_count,
            decision_asof=decision_asof,
        )
        for cohort in REQUIRED_COHORTS
    }


def test_nested_walk_forward_purges_labels_and_rejects_overlapping_outer_tests() -> None:
    dates = [date(2024, 1, 1) + timedelta(days=index) for index in range(20)]
    completions = {value: value + timedelta(days=1) for value in dates}
    completions[dates[4]] = dates[9]
    folds = build_nested_purged_walk_forward(
        dates,
        label_completion_by_date=completions,
        initial_train_size=6,
        validation_size=2,
        test_size=2,
    )
    assert folds
    assert dates[4] not in folds[0].train_dates
    assert len(folds[0].train_dates) >= 6
    assert all(completions[value] < folds[0].validation_dates[0] for value in folds[0].train_dates)
    assert all(completions[value] < folds[0].test_dates[0] for value in folds[0].validation_dates)
    with pytest.raises(ValueError, match="cannot be smaller"):
        build_nested_purged_walk_forward(
            dates,
            label_completion_by_date=completions,
            initial_train_size=6,
            validation_size=2,
            test_size=2,
            step_size=1,
        )


def test_positive_horizon_passes_profitability_and_risk_gates() -> None:
    framework = load_framework(FRAMEWORK_PATH)
    rows = _observations("beverages", horizon=21, alpha=0.02)
    result = evaluate_cohort(
        rows,
        realized_returns=_realized(rows),
        outer_test_folds=_outer_folds(rows),
        decision_asof=date(2024, 1, 2),
        framework=framework,
        candidate_performance_by_fold=_stable_candidates(),
        selected_portfolios=_selected_portfolios(rows),
    )
    performance = result["performance"]
    state, failures = recommend_next_state(performance, framework=framework, current_state="benchmark_production")
    assert state == "active_pilot"
    assert failures == ()
    assert performance["paired_net_alpha_lcb"] > 0.0
    assert performance["deflated_sharpe_ratio"] >= 0.8
    assert result["evidence"]["evaluation_role"] == "outer_test"


def test_horizon_pooling_future_labels_duplicate_rows_and_excess_exposure_fail_closed() -> None:
    framework = load_framework(FRAMEWORK_PATH)
    rows = _observations("beverages", horizon=21, alpha=0.02)
    common = dict(
        realized_returns=_realized(rows),
        outer_test_folds=_outer_folds(rows),
        decision_asof=date(2024, 1, 2),
        framework=framework,
        candidate_performance_by_fold=_stable_candidates(),
        selected_portfolios=_selected_portfolios(rows),
    )
    with pytest.raises(ValueError, match="independently"):
        evaluate_cohort(rows + _observations("beverages", horizon=63, alpha=0.02), **common)
    with pytest.raises(ValueError, match="identities"):
        evaluate_cohort(rows + [rows[0]], **common)
    with pytest.raises(ValueError, match="complete"):
        evaluate_cohort(rows, **{**common, "decision_asof": date(2020, 1, 1)})
    with pytest.raises(ValueError, match="gross exposure"):
        evaluate_cohort(
            rows,
            **{
                **common,
                "selected_portfolios": _selected_portfolios(
                    rows,
                    weights=tuple((f"T{index:02d}", 0.11) for index in range(10)),
                ),
            },
        )

    mismatched_candidates = _stable_candidates()
    for candidate in mismatched_candidates.values():
        candidate["not_an_outer_fold"] = candidate.pop("outer_06")
    with pytest.raises(ValueError, match="exact outer-test fold census"):
        evaluate_cohort(
            rows,
            **{**common, "candidate_performance_by_fold": mismatched_candidates},
        )


def test_pbo_is_balanced_bounded_and_rejects_odd_or_oversized_fold_census() -> None:
    pbo = probability_of_backtest_overfitting(_stable_candidates())
    assert 0.0 <= pbo < 0.5
    odd = {name: dict(list(values.items())[:5]) for name, values in _stable_candidates().items()}
    with pytest.raises(ValueError, match="even census"):
        probability_of_backtest_overfitting(odd)
    large = {
        candidate: {f"fold_{index:02d}": float(index + offset) for index in range(20)}
        for offset, candidate in enumerate(("a", "b"))
    }
    with pytest.raises(ValueError, match="above frozen maximum"):
        probability_of_backtest_overfitting(large, maximum_combinations=100)


def test_four_cohorts_are_horizon_specific_and_decided_independently() -> None:
    framework = load_framework(FRAMEWORK_PATH)
    asof = date(2024, 1, 2)
    decision = build_calibration_decision(
        asof_date=asof,
        framework=framework,
        horizon_results_by_cohort=_all_results(decision_asof=asof),
        input_panel_sha256="1" * 64,
        fold_registry_sha256="2" * 64,
        candidate_registry_sha256="3" * 64,
        code_sha256="4" * 64,
    )
    validate_calibration_decision(decision, framework=framework)
    assert decision["cohorts"]["household_personal_tobacco"]["state"] == "benchmark_production"
    assert decision["cohorts"]["beverages"]["state"] == "active_pilot"
    assert set(decision["cohorts"]["beverages"]["horizon_performance"]) == {"21", "63", "126"}


def test_decision_replay_direct_jump_and_fractional_counts_fail_closed() -> None:
    framework = load_framework(FRAMEWORK_PATH)
    asof = date(2024, 1, 2)
    first = build_calibration_decision(
        asof_date=asof,
        framework=framework,
        horizon_results_by_cohort=_all_results(decision_asof=asof),
        input_panel_sha256="1" * 64,
        fold_registry_sha256="2" * 64,
        candidate_registry_sha256="3" * 64,
        code_sha256="4" * 64,
    )
    jump = copy.deepcopy(first)
    item = jump["cohorts"]["beverages"]
    item["state"] = "active_full"
    item["active_cap"] = 1.0
    jump["payload_sha256"] = canonical_sha256(jump)
    with pytest.raises(ValueError, match="genesis transition"):
        validate_calibration_decision(jump, framework=framework)
    fractional = copy.deepcopy(first)
    fractional["cohorts"]["beverages"]["horizon_performance"]["21"]["paired_observation_count"] = 40.5
    fractional["payload_sha256"] = canonical_sha256(fractional)
    with pytest.raises(ValueError, match="integer"):
        validate_calibration_decision(fractional, framework=framework)

    later = asof + timedelta(days=70)
    replay_results = _all_results(decision_asof=later)
    with pytest.raises(ValueError, match="replayed|did not advance"):
        build_calibration_decision(
            asof_date=later,
            framework=framework,
            horizon_results_by_cohort=replay_results,
            input_panel_sha256="5" * 64,
            fold_registry_sha256="2" * 64,
            candidate_registry_sha256="3" * 64,
            code_sha256="4" * 64,
            previous_decision=first,
        )


def test_fresh_evidence_and_dwell_govern_scaling_and_model_changes_reset_pilot() -> None:
    framework = load_framework(FRAMEWORK_PATH)
    first_asof = date(2024, 1, 2)
    first = build_calibration_decision(
        asof_date=first_asof,
        framework=framework,
        horizon_results_by_cohort=_all_results(decision_asof=first_asof),
        input_panel_sha256="1" * 64,
        fold_registry_sha256="2" * 64,
        candidate_registry_sha256="3" * 64,
        code_sha256="4" * 64,
    )
    second_asof = first_asof + timedelta(days=70)
    second = build_calibration_decision(
        asof_date=second_asof,
        framework=framework,
        horizon_results_by_cohort=_all_results(count=41, realized_count=251, decision_asof=second_asof),
        input_panel_sha256="5" * 64,
        fold_registry_sha256="6" * 64,
        candidate_registry_sha256="3" * 64,
        code_sha256="4" * 64,
        previous_decision=first,
    )
    assert second["cohorts"]["beverages"]["state"] == "active_scaled"
    third_asof = second_asof + timedelta(days=130)
    third_results = _all_results(count=42, realized_count=252, decision_asof=third_asof)
    with pytest.raises(ValueError, match="complete genesis-to-predecessor history"):
        build_calibration_decision(
            asof_date=third_asof,
            framework=framework,
            horizon_results_by_cohort=third_results,
            input_panel_sha256="7" * 64,
            fold_registry_sha256="8" * 64,
            candidate_registry_sha256="3" * 64,
            code_sha256="4" * 64,
            previous_decision=second,
        )
    third = build_calibration_decision(
        asof_date=third_asof,
        framework=framework,
        horizon_results_by_cohort=third_results,
        input_panel_sha256="7" * 64,
        fold_registry_sha256="8" * 64,
        candidate_registry_sha256="3" * 64,
        code_sha256="4" * 64,
        previous_decision=second,
        decision_history=[first, second],
    )
    assert third["cohorts"]["beverages"]["state"] == "active_full"

    reset = build_calibration_decision(
        asof_date=second_asof,
        framework=framework,
        horizon_results_by_cohort=_all_results(count=41, realized_count=251, decision_asof=second_asof),
        input_panel_sha256="7" * 64,
        fold_registry_sha256="6" * 64,
        candidate_registry_sha256="8" * 64,
        code_sha256="4" * 64,
        previous_decision=first,
    )
    beverage = reset["cohorts"]["beverages"]
    assert beverage["state"] == "active_pilot"
    assert beverage["state_entered_asof"] == second_asof.isoformat()
    assert beverage["transition_blockers"] == ["material_model_change_reset"]

def test_selected_holdings_and_realized_path_require_exact_lineage_and_census() -> None:
    framework = load_framework(FRAMEWORK_PATH)
    rows = _observations("beverages", horizon=21, alpha=0.02)
    portfolios = _selected_portfolios(rows)
    realized = _realized(rows)
    common = dict(
        realized_returns=realized,
        outer_test_folds=_outer_folds(rows),
        decision_asof=date(2024, 1, 2),
        framework=framework,
        candidate_performance_by_fold=_stable_candidates(),
        selected_portfolios=portfolios,
    )

    with pytest.raises(ValueError, match="exact outer-test observation census"):
        evaluate_cohort(rows, **{**common, "selected_portfolios": portfolios[:-1]})

    bad_portfolio_lineage = list(portfolios)
    bad_portfolio_lineage[0] = replace(bad_portfolio_lineage[0], fold_id="outer_99")
    with pytest.raises(ValueError, match="portfolio lineage"):
        evaluate_cohort(rows, **{**common, "selected_portfolios": bad_portfolio_lineage})

    unknown_source = list(realized)
    unknown_source[0] = replace(unknown_source[0], source_portfolio_observation_id="missing_source")
    with pytest.raises(ValueError, match="unknown selected portfolio"):
        evaluate_cohort(rows, **{**common, "realized_returns": unknown_source})

    mismatched_source = list(realized)
    mismatched_source[0] = replace(
        mismatched_source[0],
        source_portfolio_observation_id=portfolios[7].observation_id,
    )
    with pytest.raises(ValueError, match="lineage disagrees"):
        evaluate_cohort(rows, **{**common, "realized_returns": mismatched_source})

    missing_source_block = [
        row for row in realized
        if row.source_portfolio_observation_id != portfolios[-1].observation_id
    ]
    with pytest.raises(ValueError, match="cover selected portfolios"):
        evaluate_cohort(rows, **{**common, "realized_returns": missing_source_block})

    mismatched_horizon = list(realized)
    mismatched_horizon[0] = replace(mismatched_horizon[0], horizon_sessions=63)
    with pytest.raises(ValueError, match="cannot pool horizons"):
        evaluate_cohort(rows, **{**common, "realized_returns": mismatched_horizon})


def test_holdings_hash_and_concentration_use_the_full_dated_path() -> None:
    framework = load_framework(FRAMEWORK_PATH)
    rows = _observations("beverages", horizon=21, alpha=0.02)
    portfolios = _selected_portfolios(rows)
    common = dict(
        observations=rows,
        realized_returns=_realized(rows),
        outer_test_folds=_outer_folds(rows),
        decision_asof=date(2024, 1, 2),
        framework=framework,
        candidate_performance_by_fold=_stable_candidates(),
    )
    baseline = evaluate_cohort(**common, selected_portfolios=portfolios)

    changed_holdings = list(portfolios)
    changed_holdings[0] = replace(
        changed_holdings[0],
        weights=tuple((f"T{index:02d}", 0.1) for index in range(1, 10)) + (("Z99", 0.1),),
    )
    changed = evaluate_cohort(**common, selected_portfolios=changed_holdings)
    assert changed["evidence"]["selected_weights_sha256"] != baseline["evidence"]["selected_weights_sha256"]

    concentrated_path = list(portfolios)
    concentrated_path[0] = replace(
        concentrated_path[0],
        weights=tuple((f"T{index:02d}", 0.2) for index in range(5)),
    )
    concentrated = evaluate_cohort(**common, selected_portfolios=concentrated_path)
    assert baseline["performance"]["winner_concentration_hhi"] == pytest.approx(0.1)
    assert baseline["performance"]["maximum_single_name_weight"] == pytest.approx(0.1)
    assert concentrated["performance"]["winner_concentration_hhi"] == pytest.approx(0.2)
    assert concentrated["performance"]["maximum_single_name_weight"] == pytest.approx(0.2)


def test_realized_return_streams_are_horizon_specific_and_not_reusable() -> None:
    framework = load_framework(FRAMEWORK_PATH)
    rows_by_horizon = {
        str(horizon): _observations("beverages", horizon=horizon, alpha=0.02)
        for horizon in (21, 63, 126)
    }
    observations = [row for key in ("21", "63", "126") for row in rows_by_horizon[key]]
    realized_by_horizon = {key: _realized(rows_by_horizon[key]) for key in ("21", "63", "126")}
    common = dict(
        observations=observations,
        decision_asof=date(2024, 1, 2),
        framework=framework,
        outer_test_folds_by_horizon={
            key: _outer_folds(rows_by_horizon[key]) for key in ("21", "63", "126")
        },
        candidate_performance_by_horizon={
            key: _stable_candidates() for key in ("21", "63", "126")
        },
        selected_portfolios_by_horizon={
            key: _selected_portfolios(rows_by_horizon[key]) for key in ("21", "63", "126")
        },
    )
    result = evaluate_all_horizons(**common, realized_returns_by_horizon=realized_by_horizon)
    stream_hashes = {
        item["evidence"]["realized_return_stream_sha256"] for item in result.values()
    }
    assert len(stream_hashes) == 3

    reused = dict(realized_by_horizon)
    reused["63"] = realized_by_horizon["21"]
    with pytest.raises(ValueError, match="cannot pool horizons"):
        evaluate_all_horizons(**common, realized_returns_by_horizon=reused)


