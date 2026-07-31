from __future__ import annotations

from pathlib import Path

import pytest

from industrials.machinery.operational_amendment_v141 import (
    amendment_paths,
    assess_operational_amendment,
    load_amendment_protocol,
    recurring_turnover_diagnostics,
)


def _period(
    model: str,
    horizon: int,
    asof: str,
    turnover: float,
) -> dict[str, str]:
    traded = turnover if turnover == 1.0 else turnover * 2.0
    return {
        "model": model,
        "horizon_days": str(horizon),
        "asof_date": asof,
        "one_way_turnover": str(turnover),
        "traded_notional_fraction": str(traded),
        "transaction_cost": str(traded * 0.002),
    }


def test_operational_amendment_is_explicitly_post_lockbox() -> None:
    protocol = load_amendment_protocol()
    assert protocol["candidate_id"] == "equal_components"
    assert protocol["maximum_portfolio_cap"] == 0.05
    assert protocol["decision_contract"]["post_lockbox_governance_exception"]
    assert protocol["decision_contract"]["lockbox_result_must_remain_immutable"]


def test_operational_amendment_requires_token_before_writing(
    tmp_path: Path,
) -> None:
    with pytest.raises(PermissionError, match="token"):
        assess_operational_amendment(
            {},
            approval_token="WRONG",
            output_root=tmp_path,
        )
    assert not amendment_paths(tmp_path).acceptance_json.exists()


def test_recurring_turnover_excludes_only_initial_formation() -> None:
    rows = [
        _period(model, horizon, "2026-01-02", 1.0)
        for model in ("stage8_candidate", "active_model")
        for horizon in (21, 63)
    ]
    rows.extend(
        _period(model, horizon, "2026-04-10", 0.55)
        for model in ("stage8_candidate", "active_model")
        for horizon in (21, 63)
    )
    diagnostics = recurring_turnover_diagnostics(
        rows,
        transaction_cost_rate=0.002,
    )
    assert len(diagnostics) == 4
    assert all(
        row["average_turnover_including_formation"] == pytest.approx(0.775)
        for row in diagnostics
    )
    assert all(
        row["average_recurring_one_way_turnover"] == pytest.approx(0.55)
        for row in diagnostics
    )
    assert all(
        row["transaction_cost_reconciliation_status"] == "PASS"
        for row in diagnostics
    )


def test_recurring_turnover_requires_explicit_initial_formation() -> None:
    rows = [
        _period(model, horizon, "2026-01-02", 0.9)
        for model in ("stage8_candidate", "active_model")
        for horizon in (21, 63)
    ]
    rows.extend(
        _period(model, horizon, "2026-04-10", 0.5)
        for model in ("stage8_candidate", "active_model")
        for horizon in (21, 63)
    )
    with pytest.raises(ValueError, match="not portfolio formation"):
        recurring_turnover_diagnostics(rows, transaction_cost_rate=0.002)
