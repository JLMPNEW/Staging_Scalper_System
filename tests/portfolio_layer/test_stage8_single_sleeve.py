from __future__ import annotations

import pandas as pd
import pytest

from portfolio_layer.sleeves.risk_model import build_sleeve_risk_proposal


def _covariance() -> pd.DataFrame:
    return pd.DataFrame(
        [[0.04, 0.01, 0.00], [0.01, 0.09, 0.00], [0.00, 0.00, 0.16]],
        index=pd.Index(["A", "B", "C"]),
        columns=pd.Index(["A", "B", "C"]),
    )


def test_single_active_sleeve_preserves_stage7_before_rc_enforcement() -> None:
    prior = {"A": 0.4, "B": 0.3}
    proposal, mode, sleeves = build_sleeve_risk_proposal(
        _covariance(),
        {"A": 0.5, "B": 0.5},
        prior_weights=prior,
        sleeve_of={"A": "long_core", "B": "long_core"},
        gross=0.7,
        max_weight=0.5,
    )

    assert proposal == prior
    assert mode == "single_sleeve_rc_cap_only"
    assert sleeves == ("long_core",)


def test_multiple_active_sleeves_runs_risk_budget_solver() -> None:
    prior = {"A": 0.4, "B": 0.3}
    proposal, mode, sleeves = build_sleeve_risk_proposal(
        _covariance(),
        {"A": 0.5, "B": 0.5},
        prior_weights=prior,
        sleeve_of={"A": "long_core", "B": "medium_rotation"},
        gross=0.7,
        max_weight=0.5,
    )

    assert proposal != prior
    assert sum(proposal.values()) == pytest.approx(0.7)
    assert mode == "multi_sleeve_risk_budget"
    assert sleeves == ("long_core", "medium_rotation")


def test_positive_prior_without_sleeve_assignment_fails_closed() -> None:
    with pytest.raises(ValueError, match="missing sleeve assignments"):
        build_sleeve_risk_proposal(
            _covariance(),
            {"A": 1.0},
            prior_weights={"A": 0.7},
            sleeve_of={},
            gross=0.7,
            max_weight=0.5,
        )
