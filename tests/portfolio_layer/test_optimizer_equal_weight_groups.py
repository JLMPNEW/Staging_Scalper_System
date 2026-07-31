from __future__ import annotations

import numpy as np

from portfolio_layer.optimizer.optimizer_core import (
    finalize_with_group_caps,
    solve_long_only_mv,
)


def test_equal_weight_group_is_preserved_by_optimizer() -> None:
    weights, info = solve_long_only_mv(
        np.array([1.0, 0.1, 0.4]),
        np.eye(3) * 0.05,
        risk_aversion=5.0,
        max_weight=0.8,
        gross=1.0,
        solver="ECOS",
        equal_weight_groups=[[0, 1]],
    )

    assert info["status"] in {"optimal", "optimal_inaccurate"}
    assert abs(float(weights.sum()) - 1.0) < 1e-8
    assert abs(float(weights[0] - weights[1])) < 1e-8


def test_equal_weight_group_survives_group_cap_finalization() -> None:
    group_caps = [([0, 1], 0.20)]
    weights, info = solve_long_only_mv(
        np.array([1.0, 0.1, 0.4, 0.3]),
        np.eye(4) * 0.05,
        risk_aversion=5.0,
        max_weight=0.6,
        gross=1.0,
        solver="ECOS",
        group_caps=group_caps,
        equal_weight_groups=[[0, 1]],
    )
    finalized = finalize_with_group_caps(
        weights,
        group_caps=group_caps,
        min_weight=0.0005,
        max_weight=0.6,
        gross=1.0,
    )

    assert info["status"] in {"optimal", "optimal_inaccurate"}
    assert abs(float(finalized.sum()) - 1.0) < 1e-8
    assert abs(float(finalized[0] - finalized[1])) < 1e-8
    assert float(finalized[0] + finalized[1]) <= 0.20 + 1e-8
