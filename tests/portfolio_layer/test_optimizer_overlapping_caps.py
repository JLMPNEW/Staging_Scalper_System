from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
from types import ModuleType

import numpy as np
import pytest

from portfolio_layer.optimizer.optimizer_core import (
    constraint_aware_invested_gross,
    finalize_with_group_caps,
    maximum_investable_gross,
    rescale_group_caps_for_invested_gross,
    snap_rounded_weights,
    solve_long_only_mv,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_solver_and_finalizer_support_hierarchical_overlapping_caps() -> None:
    group_caps = [([0, 1, 2], 0.30), ([0, 1], 0.15)]
    weights, info = solve_long_only_mv(
        np.array([2.0, 1.8, 1.5, 0.4, 0.3, 0.2]),
        np.eye(6) * 0.05,
        risk_aversion=5.0,
        max_weight=0.50,
        gross=1.0,
        solver="ECOS",
        group_caps=group_caps,
    )
    finalized = finalize_with_group_caps(
        weights,
        group_caps=group_caps,
        min_weight=0.0005,
        max_weight=0.50,
        gross=1.0,
    )

    assert info["status"] in {"optimal", "optimal_inaccurate"}
    assert float(finalized.sum()) == pytest.approx(1.0, abs=1e-10)
    assert float(finalized[[0, 1, 2]].sum()) <= 0.30 + 1e-10
    assert float(finalized[[0, 1]].sum()) <= 0.15 + 1e-10
    assert not ((finalized > 0.0) & (finalized < 0.0005 - 1e-10)).any()


def test_publication_rounding_preserves_overlapping_caps_and_exact_gross() -> None:
    # Independent nearest rounding breaches the first cap by one 1e-4 unit.
    weights = np.array([0.050051, 0.050051, 0.049951, 0.049851, 0.4, 0.400096])
    assert float(np.round(weights[:4], 4).sum()) > 0.20

    rounded = snap_rounded_weights(
        weights,
        gross=1.0,
        max_weight=0.50,
        decimals=4,
        group_caps=[([0, 1, 2, 3], 0.20), ([0, 1], 0.1002)],
    )

    assert float(rounded.sum()) == pytest.approx(1.0, abs=1e-12)
    assert float(rounded[[0, 1, 2, 3]].sum()) <= 0.20
    assert float(rounded[[0, 1]].sum()) <= 0.1002


def test_publication_rounding_fails_when_precision_makes_caps_infeasible() -> None:
    with pytest.raises(ValueError, match="without breaching caps"):
        snap_rounded_weights(
            np.array([0.33335, 0.66665]),
            gross=1.0,
            max_weight=1.0,
            decimals=4,
            group_caps=[([0], 0.33335), ([1], 0.66665)],
        )


def test_publication_rounding_accepts_decimal_gross_despite_binary_float_noise() -> None:
    gross = 0.6199999
    rounded = snap_rounded_weights(
        np.array([0.31, 0.3099999]),
        gross=gross,
        max_weight=0.50,
        decimals=12,
    )

    assert float(rounded.sum()) == pytest.approx(gross, abs=1e-12)


def test_publication_rounding_rejects_genuinely_overprecise_gross() -> None:
    with pytest.raises(ValueError, match="not representable"):
        snap_rounded_weights(
            np.array([0.06, 0.0634567890125]),
            gross=0.1234567890125,
            max_weight=0.50,
            decimals=12,
        )


def test_constraint_cash_preserves_absolute_caps_in_degraded_universe() -> None:
    # Three capped sleeves can invest only 65% of NAV. The effective invested
    # gross must not shrink those absolute NAV caps a second time.
    caps = [([0, 1], 0.30), ([2, 3], 0.30), ([4], 0.05)]
    capacity, attempts = maximum_investable_gross(
        5,
        group_caps=caps,
        cap_base_gross=1.0,
        max_weight=0.50,
    )
    assert attempts
    assert capacity == pytest.approx(0.65, abs=1e-6)
    invested, triggered = constraint_aware_invested_gross(
        requested_gross=1.0,
        capacity=capacity,
        allow_constraint_cash=True,
    )
    assert triggered is True
    assert 0.64999 < invested < 0.65

    solve_caps = rescale_group_caps_for_invested_gross(
        caps,
        cap_base_gross=1.0,
        invested_gross=invested,
    )
    weights, info = solve_long_only_mv(
        np.array([2.0, 1.8, 1.5, 1.2, 0.5]),
        np.eye(5) * 0.05,
        risk_aversion=5.0,
        max_weight=0.50,
        gross=invested,
        solver="ECOS",
        group_caps=solve_caps,
    )
    assert info["status"] in {"optimal", "optimal_inaccurate"}
    assert float(weights.sum()) == pytest.approx(invested, abs=1e-8)
    assert float(weights[[0, 1]].sum()) <= 0.30 + 1e-8
    assert float(weights[[2, 3]].sum()) <= 0.30 + 1e-8
    assert float(weights[[4]].sum()) <= 0.05 + 1e-8


def test_constraint_cash_remains_fail_closed_when_disabled() -> None:
    with pytest.raises(ValueError, match="constraint cash is disabled"):
        constraint_aware_invested_gross(
            requested_gross=1.0,
            capacity=0.67,
            allow_constraint_cash=False,
        )


def test_scope_cap_validator_uses_sealed_membership_and_rejects_unknown_scope() -> None:
    module = _load(
        PROJECT_ROOT
        / "portfolio_layer"
        / "optimizer"
        / "10_validate_optimizer_outputs.py",
        "optimizer_scope_cap_validator_test",
    )
    rows = [
        {"ticker": "A", "source_pipeline": "other", "weight": "0.04"},
        {"ticker": "B", "source_pipeline": "other", "weight": "0.03"},
        {"ticker": "C", "source_pipeline": "other", "weight": "0.02"},
    ]
    scores = {
        "A": {
            "ticker": "A",
            "source_pipeline": "consumer_defensive",
            "model_scope_id": "beverages",
        },
        "B": {
            "ticker": "B",
            "source_pipeline": "consumer_defensive",
            "model_scope_id": "beverages",
        },
        "C": {
            "ticker": "C",
            "source_pipeline": "consumer_defensive",
            "model_scope_id": "",
        },
    }
    details, violations = module.evaluate_scope_weight_caps(
        rows,
        scores,
        {
            "consumer_defensive": {
                "beverages": 0.05,
                "packaged_foods_agricultural_products": 0.05,
            }
        },
        gross=1.0,
        tolerance=1e-6,
    )

    assert any("beverages=0.070000<=cap 0.050000" in item for item in details)
    assert any("beverages:weight=0.070000>cap=0.050000" in item for item in violations)
    assert any("C:<blank>" in item for item in violations)


def test_stage3_producer_and_validator_bind_current_config_to_stage1(
    tmp_path: Path,
) -> None:
    producer = _load(
        PROJECT_ROOT
        / "portfolio_layer"
        / "optimizer"
        / "09_run_portfolio_optimizer.py",
        "optimizer_config_binding_producer_test",
    )
    validator = _load(
        PROJECT_ROOT
        / "portfolio_layer"
        / "optimizer"
        / "10_validate_optimizer_outputs.py",
        "optimizer_config_binding_validator_test",
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text("optimizer:\n  gross_exposure: 1.0\n", encoding="utf-8")
    sealed_sha = hashlib.sha256(config_path.read_bytes()).hexdigest()
    manifest = {
        "provenance": {
            "config_yaml": {"path": str(config_path), "sha256": sealed_sha}
        }
    }

    for module in (producer, validator):
        valid, _detail = module.stage1_config_binding(manifest, config_path)
        assert valid is True

    config_path.write_text("optimizer:\n  gross_exposure: 0.5\n", encoding="utf-8")
    for module in (producer, validator):
        valid, detail = module.stage1_config_binding(manifest, config_path)
        assert valid is False
        assert sealed_sha in detail

    missing_hash = {"provenance": {"config_yaml": {"path": str(config_path)}}}
    for module in (producer, validator):
        valid, detail = module.stage1_config_binding(missing_hash, config_path)
        assert valid is False
        assert "expected=<missing>" in detail
