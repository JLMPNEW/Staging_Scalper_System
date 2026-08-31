from __future__ import annotations

# pyright: reportMissingImports=false

import importlib.util
from pathlib import Path
from types import ModuleType

import pandas as pd

from portfolio_layer.sleeves.risk_model import enforce_rc_cap_with_enb_guard


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_validator() -> ModuleType:
    script = PROJECT_ROOT / "portfolio_layer" / "sleeves" / "29_validate_sleeves.py"
    spec = importlib.util.spec_from_file_location("sleeve_validator_policy_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_multi_sleeve_idio_ratio_decline_is_warn_only_when_enb_improves() -> None:
    module = _load_validator()
    assert module.diversification_failures(
        allocation_mode="multi_sleeve_risk_budget",
        enb_before=3.7635,
        enb_after=4.4838,
        total_variance_before=0.04,
        total_variance_after=0.04,
        systematic_variance_before=0.03,
        systematic_variance_after=0.035,
    ) == []


def test_material_enb_decline_remains_a_hard_failure() -> None:
    module = _load_validator()
    failures = module.diversification_failures(
        allocation_mode="multi_sleeve_risk_budget",
        enb_before=10.0,
        enb_after=9.0,
        total_variance_before=0.04,
        total_variance_after=0.03,
        systematic_variance_before=0.03,
        systematic_variance_after=0.02,
    )
    assert failures == ["enb 9.000<10.000"]


def test_single_sleeve_cap_only_cannot_add_absolute_risk() -> None:
    module = _load_validator()
    failures = module.diversification_failures(
        allocation_mode="single_sleeve_rc_cap_only",
        enb_before=10.0,
        enb_after=10.0,
        total_variance_before=0.04,
        total_variance_after=0.041,
        systematic_variance_before=0.03,
        systematic_variance_after=0.031,
    )
    assert len(failures) == 2
    assert failures[0].startswith("total_var")
    assert failures[1].startswith("systematic_var")


def test_harmful_multi_sleeve_proposal_falls_back_to_rc_capped_baseline() -> None:
    names = ["A", "B", "C", "D"]
    axes = pd.Index(names, dtype="object")
    covariance = pd.DataFrame(
        [[1.0 if left == right else 0.0 for right in names] for left in names],
        index=axes,
        columns=axes,
    )
    prior = {name: 0.25 for name in names}
    proposal = {"A": 0.70, "B": 0.10, "C": 0.10, "D": 0.10}

    result = enforce_rc_cap_with_enb_guard(
        proposal,
        prior,
        covariance,
        rc_cap=0.50,
        allocation_mode="multi_sleeve_risk_budget",
    )

    assert result.fallback_applied is True
    assert result.allocation_mode == "multi_sleeve_enb_fallback_rc_cap_only"
    assert result.proposal_enb < result.required_enb
    assert result.final_enb >= result.required_enb
    assert result.enforcement.weights == prior


def test_compliant_multi_sleeve_proposal_is_preserved() -> None:
    names = ["A", "B", "C", "D"]
    axes = pd.Index(names, dtype="object")
    covariance = pd.DataFrame(
        [[1.0 if left == right else 0.0 for right in names] for left in names],
        index=axes,
        columns=axes,
    )
    weights = {name: 0.25 for name in names}

    result = enforce_rc_cap_with_enb_guard(
        weights,
        weights,
        covariance,
        rc_cap=0.50,
        allocation_mode="multi_sleeve_risk_budget",
    )

    assert result.fallback_applied is False
    assert result.allocation_mode == "multi_sleeve_risk_budget"
    assert result.enforcement.weights == weights
