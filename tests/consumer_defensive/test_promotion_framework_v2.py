from __future__ import annotations

import ast
import copy
from pathlib import Path

import pytest

from consumer_defensive.core.promotion_framework_v2 import (
    REQUIRED_COHORTS,
    framework_sha256,
    load_framework,
    performance_gate_failures,
    validate_calibration_decision,
    validate_framework,
    validate_performance,
)


ROOT = Path(__file__).resolve().parents[2]
FRAMEWORK = ROOT / "consumer_defensive/data/consumer_defensive_promotion_framework_v2.yaml"


def _performance() -> dict[str, float | int]:
    return {
        "paired_net_alpha_lcb": 0.01,
        "net_alpha_mean": 0.02,
        "absolute_profit_factor": 1.5,
        "relative_profit_factor": 1.5,
        "robust_profit_factor": 1.5,
        "deflated_sharpe_ratio": 0.9,
        "probability_of_backtest_overfitting": 0.2,
        "maximum_drawdown": 0.1,
        "expected_shortfall_95": -0.02,
        "turnover": 0.5,
        "average_transaction_cost": 0.001,
        "liquidity_capacity_ratio": 2.0,
        "winner_concentration_hhi": 0.1,
        "maximum_single_name_weight": 0.1,
        "paired_observation_count": 40,
        "positive_return_count": 25,
        "negative_return_count": 15,
    }


def test_framework_is_consumer_owned_frozen_and_requires_recalibration() -> None:
    payload = load_framework(FRAMEWORK)
    assert payload["model_family"] == "consumer_defensive"
    assert payload["status"] == "recalibration_required"
    assert set(payload["cohorts"]) == REQUIRED_COHORTS
    assert payload["ownership"]["cross_sector_code_imports_allowed"] is False
    assert payload["evaluation"]["all_horizons_required_for_active"] is True
    assert payload["active_evidence_floors"]["minimum_deflated_sharpe_ratio"] == 0.8
    assert len(framework_sha256(payload)) == 64


@pytest.mark.parametrize(
    "mutator",
    [
        lambda value: value.update({"unexpected": True}),
        lambda value: value["evaluation"].update(
            {"purge_uses_label_completion_date": False}
        ),
        lambda value: value["evaluation"].update(
            {"outer_folds_are_selection_blind": False}
        ),
        lambda value: value["evaluation"].update({"returns_are_net_of_costs": False}),
        lambda value: value["active_evidence_floors"].update(
            {"minimum_deflated_sharpe_ratio": -999.0}
        ),
        lambda value: value["evaluation"]["estimator_settings"].update(
            {"bootstrap_samples": 10}
        ),
    ],
)
def test_framework_policy_mutations_fail_closed(mutator) -> None:
    payload = copy.deepcopy(load_framework(FRAMEWORK))
    mutator(payload)
    with pytest.raises(ValueError):
        validate_framework(payload)


def test_performance_ranges_counts_and_turnover_gate_are_enforced() -> None:
    framework = load_framework(FRAMEWORK)
    for key, invalid in (
        ("deflated_sharpe_ratio", 2.0),
        ("probability_of_backtest_overfitting", -1.0),
        ("winner_concentration_hhi", -3.0),
        ("paired_observation_count", 40.5),
    ):
        performance = _performance()
        performance[key] = invalid
        with pytest.raises(ValueError):
            validate_performance(performance, label="adversarial")
    inconsistent = _performance()
    inconsistent["positive_return_count"] = 30
    inconsistent["negative_return_count"] = 20
    with pytest.raises(ValueError, match="signed counts"):
        validate_performance(inconsistent, label="adversarial")
    high_turnover = _performance()
    high_turnover["turnover"] = 2.1
    assert "turnover" in performance_gate_failures(high_turnover, framework=framework)


def test_legacy_decision_schema_cannot_activate_v2() -> None:
    framework = load_framework(FRAMEWORK)
    with pytest.raises(ValueError, match="legacy or unsupported"):
        validate_calibration_decision(
            {"schema_version": "consumer_defensive_stage8_calibration_v1"},
            framework=framework,
        )


def test_consumer_package_has_no_cross_sector_imports() -> None:
    forbidden = {
        "biotech_index",
        "future_only_evidence",
        "industrials",
        "med_devices",
        "technology",
        "transportation",
    }
    violations: list[str] = []
    for path in sorted((ROOT / "consumer_defensive").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            else:
                names = []
            for name in names:
                if name.split(".", 1)[0] in forbidden:
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno}:{name}")
    assert violations == []


def test_retired_evidence_package_does_not_import_consumer() -> None:
    violations: list[str] = []
    for path in sorted((ROOT / "future_only_evidence").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            else:
                names = []
            for name in names:
                if name.split(".", 1)[0] == "consumer_defensive":
                    violations.append(f"{path.name}:{node.lineno}:{name}")
    assert violations == []
