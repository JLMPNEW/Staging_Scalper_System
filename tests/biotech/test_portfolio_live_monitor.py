from __future__ import annotations

from datetime import date, timedelta

from biotech_index.core.portfolio_live_monitor import (
    evaluate_live_monitoring_windows,
    overall_monitoring_action,
)


def _rows(*, contract_id: str = "c1", candidate: float = 0.001, incumbent: float = 0.0) -> list[dict[str, object]]:
    end = date(2026, 8, 27)
    return [
        {
            "date": (end - timedelta(days=offset)).isoformat(),
            "contract_id": contract_id,
            "candidate_net_return": candidate,
            "incumbent_net_return": incumbent,
        }
        for offset in range(40)
    ]


def test_live_monitor_allows_scaling_after_supported_outperformance() -> None:
    results = evaluate_live_monitoring_windows(
        _rows(),
        asof_date=date(2026, 8, 27),
        windows_days=(30,),
        expected_contract_id="c1",
        effective_trials=10,
        min_live_paired_days=20,
        max_drawdown_deterioration_pct=5.0,
        max_daily_cvar_deterioration_pct=0.5,
        bootstrap_iterations=25,
    )
    assert overall_monitoring_action(results) == ("eligible_to_scale", "advance_one_weight_stage")


def test_policy_identity_mismatch_forces_rollback() -> None:
    results = evaluate_live_monitoring_windows(
        _rows(contract_id="wrong"),
        asof_date=date(2026, 8, 27),
        windows_days=(30,),
        expected_contract_id="c1",
        effective_trials=10,
        min_live_paired_days=20,
        max_drawdown_deterioration_pct=5.0,
        max_daily_cvar_deterioration_pct=0.5,
        bootstrap_iterations=25,
    )
    assert overall_monitoring_action(results) == ("rollback_to_champion", "xbi_residual_only")
