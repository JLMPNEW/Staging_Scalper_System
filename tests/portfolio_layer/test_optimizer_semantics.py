from __future__ import annotations

from pathlib import Path

import pytest

from portfolio_layer.optimizer.optimizer_semantics import (
    machinery_optimizer_semantic_sha256,
)


ROOT = Path(__file__).resolve().parents[2]
OPTIMIZER_PATH = ROOT / "portfolio_layer" / "optimizer" / "optimizer_core.py"
RUNNER_PATH = ROOT / "portfolio_layer" / "optimizer" / "09_run_portfolio_optimizer.py"


def test_machinery_optimizer_seal_ignores_unrelated_sector_helper_changes() -> None:
    optimizer_source = OPTIMIZER_PATH.read_text(encoding="utf-8")
    runner_source = RUNNER_PATH.read_text(encoding="utf-8")
    changed_biotech = runner_source.replace(
        "Biotech adaptive sleeve contract has invalid breadth/name-cap fields",
        "Biotech adaptive sleeve contract has invalid selected-name capacity",
        1,
    )
    assert changed_biotech != runner_source

    baseline = machinery_optimizer_semantic_sha256(
        optimizer_source=optimizer_source,
        runner_source=runner_source,
    )
    assert (
        machinery_optimizer_semantic_sha256(
            optimizer_source=optimizer_source,
            runner_source=changed_biotech,
        )
        == baseline
    )


def test_machinery_optimizer_seal_detects_group_cap_runner_changes() -> None:
    optimizer_source = OPTIMIZER_PATH.read_text(encoding="utf-8")
    runner_source = RUNNER_PATH.read_text(encoding="utf-8")
    changed_cap_rule = runner_source.replace(
        "if not np.isfinite(cap) or cap < 0:",
        "if not np.isfinite(cap) or cap <= 0:",
        1,
    )
    assert changed_cap_rule != runner_source

    assert machinery_optimizer_semantic_sha256(
        optimizer_source=optimizer_source,
        runner_source=changed_cap_rule,
    ) != machinery_optimizer_semantic_sha256(
        optimizer_source=optimizer_source,
        runner_source=runner_source,
    )


def test_machinery_optimizer_seal_detects_core_sizing_changes() -> None:
    optimizer_source = OPTIMIZER_PATH.read_text(encoding="utf-8")
    runner_source = RUNNER_PATH.read_text(encoding="utf-8")
    changed_margin = optimizer_source.replace(
        "CONSTRAINT_CASH_MARGIN = 1e-7",
        "CONSTRAINT_CASH_MARGIN = 2e-7",
        1,
    )
    assert changed_margin != optimizer_source

    assert machinery_optimizer_semantic_sha256(
        optimizer_source=changed_margin,
        runner_source=runner_source,
    ) != machinery_optimizer_semantic_sha256(
        optimizer_source=optimizer_source,
        runner_source=runner_source,
    )


def test_machinery_optimizer_seal_fails_if_contract_root_disappears() -> None:
    optimizer_source = OPTIMIZER_PATH.read_text(encoding="utf-8").replace(
        "def constraint_aware_invested_gross(",
        "def renamed_constraint_aware_invested_gross(",
        1,
    )

    with pytest.raises(ValueError, match="root or dependency is missing"):
        machinery_optimizer_semantic_sha256(
            optimizer_source=optimizer_source,
            runner_source=RUNNER_PATH.read_text(encoding="utf-8"),
        )
