from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date

import pytest

from biotech_index.core.cohort_calibration import (
    BIOTECH_CALIBRATION_COHORTS,
    cohort_output_directory_name,
    policy_supports_cohort,
    rows_for_cohort,
    validate_cohort_budget_weights,
)
from biotech_index.core.cohort_portfolio import (
    CohortPromotionStatus,
    aligned_fold_manifest,
    cohort_promotion_status,
    combine_cohort_selection_rows,
)
from biotech_index.core.portfolio_profitability import targets_from_selection_rows
from tests.biotech.conftest import load_script_module


def equal_budgets() -> dict[str, float]:
    return {cohort: 0.2 for cohort in BIOTECH_CALIBRATION_COHORTS}


def test_cohort_output_directories_are_stable_and_compact() -> None:
    directories = [cohort_output_directory_name(cohort) for cohort in BIOTECH_CALIBRATION_COHORTS]

    assert directories == ["c01", "c02", "c03", "c04", "c05"]
    assert len(set(directories)) == len(BIOTECH_CALIBRATION_COHORTS)


def test_completed_cohort_requires_verified_profitability_contract(tmp_path) -> None:
    module = load_script_module(
        "64_run_biotech_cohort_walk_forward_calibration.py",
        "cohort_orchestrator_completion_contract",
    )
    cohort = BIOTECH_CALIBRATION_COHORTS[0]

    def dump(name: str, payload: dict[str, object]) -> None:
        (tmp_path / name).write_text(json.dumps(payload), encoding="utf-8")

    dump(
        "walk_forward_run_manifest.json",
        {
            "status": "success",
            "calibration_scope": "cohort",
            "calibration_cohort": cohort,
        },
    )
    assert not module.successful_run(tmp_path, cohort)

    dump("portfolio_profitability_manifest.json", {"status": "success"})
    dump(
        "portfolio_profitability_verification.json",
        {
            "verification_status": "pass",
            "independent_normalized_input_replay": True,
        },
    )
    dump(
        "production_policy_contract_profitability_candidate.json",
        {"profitability_replay_verification": {"verification_status": "pass"}},
    )
    assert module.successful_run(tmp_path, cohort)

    dump(
        "portfolio_profitability_verification.json",
        {
            "verification_status": "fail",
            "independent_normalized_input_replay": True,
        },
    )
    assert not module.successful_run(tmp_path, cohort)


@dataclass(frozen=True)
class StubPolicy:
    allowed_primary_cohorts: tuple[str, ...]
    post_selection_allowed_primary_cohorts: tuple[str, ...]


def test_cohort_filter_and_policy_scope_are_independent() -> None:
    target = BIOTECH_CALIBRATION_COHORTS[2]
    rows = [
        {"ticker": "AAA", "biotech_primary_cohort": target},
        {"ticker": "BBB", "biotech_primary_cohort": BIOTECH_CALIBRATION_COHORTS[0]},
    ]
    assert [row["ticker"] for row in rows_for_cohort(rows, target)] == ["AAA"]
    global_policy = StubPolicy(allowed_primary_cohorts=(), post_selection_allowed_primary_cohorts=())
    targeted_policy = StubPolicy(
        allowed_primary_cohorts=(target,),
        post_selection_allowed_primary_cohorts=(target,),
    )
    wrong_policy = StubPolicy(
        allowed_primary_cohorts=(BIOTECH_CALIBRATION_COHORTS[0],),
        post_selection_allowed_primary_cohorts=(),
    )
    assert policy_supports_cohort(global_policy, target)
    assert policy_supports_cohort(targeted_policy, target)
    assert not policy_supports_cohort(wrong_policy, target)


def test_budget_weights_must_cover_exactly_one_portfolio() -> None:
    assert validate_cohort_budget_weights(BIOTECH_CALIBRATION_COHORTS, equal_budgets()) == equal_budgets()
    invalid = equal_budgets()
    invalid[BIOTECH_CALIBRATION_COHORTS[0]] = 0.1
    with pytest.raises(ValueError, match="expected 1.0"):
        validate_cohort_budget_weights(BIOTECH_CALIBRATION_COHORTS, invalid)


def test_cohort_promotion_requires_statistics_profitability_and_portability() -> None:
    cohort = BIOTECH_CALIBRATION_COHORTS[0]
    fold = {
        "candidate_id": "candidate-1",
        "candidate_spec": {"candidate_name": "quality_challenger"},
        "selection_policy": {"policy_name": "core_structural_veto"},
    }
    status = cohort_promotion_status(
        cohort,
        statistical_decision={"production_promotion_authorized": True},
        profitability_decision={"profitability_promotion_authorized": True},
        fold_contract=fold,
    )
    assert status.authorized
    fallback = cohort_promotion_status(
        cohort,
        statistical_decision={"production_promotion_authorized": True},
        profitability_decision={"profitability_promotion_authorized": True},
        fold_contract={
            **fold,
            "candidate_id": "production_incumbent_fallback",
        },
    )
    assert not fallback.authorized
    assert "no_challenger_incumbent_retained" in fallback.reason_codes


def fold_row(cohort: str, *, test_end: str = "2025-12-31") -> dict[str, object]:
    return {
        "fold_id": "h120_F1",
        "horizon_bars": 120,
        "train_start": "2019-01-04",
        "train_end": "2022-12-30",
        "validation_start": "2023-01-06",
        "validation_end": "2023-12-29",
        "test_start": "2024-01-05",
        "test_end": test_end,
        "embargo_days": 185,
        "support_status": "PASS",
        "calibration_cohort": cohort,
    }


def test_fold_alignment_rejects_different_outer_test_windows() -> None:
    aligned = {cohort: [fold_row(cohort)] for cohort in BIOTECH_CALIBRATION_COHORTS}
    assert len(aligned_fold_manifest(aligned)) == 1
    aligned[BIOTECH_CALIBRATION_COHORTS[-1]] = [
        fold_row(BIOTECH_CALIBRATION_COHORTS[-1], test_end="2026-01-30")
    ]
    with pytest.raises(ValueError, match="not aligned"):
        aligned_fold_manifest(aligned)


def selection_row(cohort: str, ticker: str, split: str) -> dict[str, object]:
    return {
        "fold_id": "h120_F1",
        "evaluation_split": split,
        "asof_date": "2025-01-03",
        "ticker": ticker,
        "biotech_primary_cohort": cohort,
        "candidate_selection_score": 75.0,
        "objective_return": 0.10,
    }


def promotion_status(cohort: str, *, authorized: bool) -> CohortPromotionStatus:
    return CohortPromotionStatus(
        cohort=cohort,
        statistical_authorized=authorized,
        profitability_authorized=authorized,
        live_portable=authorized,
        candidate_id=f"candidate-{cohort}" if authorized else "production_incumbent_fallback",
        candidate_name="challenger" if authorized else "incumbent",
        selection_policy_name="core_structural_veto",
        authorized=authorized,
        reason_codes=("authorized",) if authorized else ("no_challenger_incumbent_retained",),
    )


def test_combination_promotes_one_cohort_without_suppressing_the_other_four() -> None:
    selected: dict[str, list[dict[str, object]]] = {}
    sleeves: dict[str, list[dict[str, object]]] = {}
    comparisons: dict[str, list[dict[str, object]]] = {}
    statuses: dict[str, CohortPromotionStatus] = {}
    promoted = BIOTECH_CALIBRATION_COHORTS[0]
    for index, cohort in enumerate(BIOTECH_CALIBRATION_COHORTS):
        selected[cohort] = [
            selection_row(cohort, f"C{index}", "outer_test_candidate"),
            selection_row(cohort, f"I{index}", "outer_test_incumbent"),
        ]
        sleeves[cohort] = [
            {
                "fold_id": "h120_F1",
                "horizon_days": 120,
                "asof_date": "2025-01-03",
                "active_stock_selection_weight": 0.5,
            }
        ]
        comparisons[cohort] = [
            {
                "fold_id": "h120_F1",
                "horizon_days": 120,
                "candidate_id": f"candidate-{cohort}",
            }
        ]
        statuses[cohort] = promotion_status(cohort, authorized=cohort == promoted)

    rows, sleeve_rows = combine_cohort_selection_rows(
        selected_rows_by_cohort=selected,
        sleeve_rows_by_cohort=sleeves,
        comparison_rows_by_cohort=comparisons,
        promotion_status_by_cohort=statuses,
        cohort_budget_weights=equal_budgets(),
        primary_horizon=120,
    )
    candidate = [row for row in rows if row["evaluation_split"] == "outer_test_candidate"]
    assert len(candidate) == 5
    weights = {str(row["ticker"]): float(str(row["portfolio_target_weight"])) for row in candidate}
    assert weights["C0"] == pytest.approx(0.1)
    assert all(weights[f"I{index}"] == pytest.approx(0.2) for index in range(1, 5))
    assert sleeve_rows[0]["active_stock_selection_weight"] == pytest.approx(0.9)
    assert sleeve_rows[0]["xbi_residual_weight"] == pytest.approx(0.1)


def test_explicit_target_weights_preserve_cohort_budget_and_benchmark_residual() -> None:
    targets = targets_from_selection_rows(
        [
            {"asof_date": "2025-01-03", "ticker": "AAA", "portfolio_target_weight": 0.15},
            {"asof_date": "2025-01-03", "ticker": "BBB", "portfolio_target_weight": 0.25},
        ],
        ["2025-01-03"],
        active_weight_by_date={},
        benchmark_ticker="XBI",
        target_weight_field="portfolio_target_weight",
    )
    assert targets[0].signal_date == date(2025, 1, 3)
    assert targets[0].weights == pytest.approx({"AAA": 0.15, "BBB": 0.25, "XBI": 0.60})


def test_explicit_target_weights_reject_duplicate_ticker_rows() -> None:
    with pytest.raises(ValueError, match="Duplicate explicit target weight"):
        targets_from_selection_rows(
            [
                {"asof_date": "2025-01-03", "ticker": "AAA", "portfolio_target_weight": 0.1},
                {"asof_date": "2025-01-03", "ticker": "AAA", "portfolio_target_weight": 0.1},
            ],
            ["2025-01-03"],
            active_weight_by_date={},
            benchmark_ticker="XBI",
            target_weight_field="portfolio_target_weight",
        )
