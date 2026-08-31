from __future__ import annotations

from biotech_index.core.portfolio_governance import (
    ProfitabilityPromotionRules,
    decide_profitability_promotion,
    evaluate_champion_challenger_monitoring,
)


def comparison(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "paired_daily_count": 504,
        "candidate_terminal_wealth": 1_300_000.0,
        "incumbent_terminal_wealth": 1_200_000.0,
        "candidate_profit_factor": 1.4,
        "incumbent_profit_factor": 1.2,
        "candidate_max_drawdown_pct": -18.0,
        "incumbent_max_drawdown_pct": -20.0,
        "candidate_daily_cvar_5pct": -2.0,
        "incumbent_daily_cvar_5pct": -2.2,
        "candidate_deflated_sharpe_probability": 0.80,
        "paired_annualized_delta_bootstrap_lcb_pct": -1.0,
        "delta_terminal_wealth": 100_000.0,
        "delta_cagr_pct": 4.0,
        "delta_calmar_ratio": 0.4,
        "delta_profit_factor": 0.2,
        "delta_max_drawdown_pct": 2.0,
        "delta_daily_cvar_5pct": 0.2,
        "delta_gross_turnover_multiple": 0.1,
    }
    payload.update(overrides)
    return payload


def folds(*wealth_deltas: float) -> list[dict[str, object]]:
    return [{"delta_terminal_wealth": value} for value in wealth_deltas]


def test_negative_bootstrap_delta_allows_provisional_profitability_promotion() -> None:
    decision = decide_profitability_promotion(
        comparison(),
        folds(20_000.0, 30_000.0, -5_000.0),
        ProfitabilityPromotionRules(),
    )
    assert decision.authorized
    assert decision.provisional
    assert decision.status == "provisional_profitability_promotion"
    assert decision.active_weight_cap == 0.25
    assert "bootstrap_uncertainty_limits_deployment_weight" in decision.reason_codes


def test_positive_confidence_allows_full_profitability_promotion() -> None:
    decision = decide_profitability_promotion(
        comparison(
            candidate_deflated_sharpe_probability=0.97,
            paired_annualized_delta_bootstrap_lcb_pct=1.5,
        ),
        folds(20_000.0, 30_000.0, 10_000.0),
        ProfitabilityPromotionRules(),
    )
    assert decision.authorized
    assert not decision.provisional
    assert decision.status == "full_profitability_promotion"


def test_higher_wealth_cannot_override_material_tail_harm() -> None:
    decision = decide_profitability_promotion(
        comparison(candidate_max_drawdown_pct=-40.0),
        folds(20_000.0, 30_000.0, 10_000.0),
        ProfitabilityPromotionRules(),
    )
    assert not decision.authorized
    assert "max_drawdown_materially_worse" in decision.reason_codes


def test_monitoring_rolls_back_on_policy_hash_mismatch() -> None:
    result = evaluate_champion_challenger_monitoring(
        comparison(),
        min_live_paired_days=20,
        max_drawdown_deterioration_pct=5.0,
        max_daily_cvar_deterioration_pct=0.5,
        policy_hash_consistent=False,
    )
    assert result["monitoring_status"] == "rollback_to_champion"
    assert result["monitoring_action"] == "xbi_residual_only"


def test_monitoring_waits_for_live_support_before_scaling() -> None:
    result = evaluate_champion_challenger_monitoring(
        comparison(paired_daily_count=10),
        min_live_paired_days=20,
        max_drawdown_deterioration_pct=5.0,
        max_daily_cvar_deterioration_pct=0.5,
        policy_hash_consistent=True,
    )
    assert result["monitoring_status"] == "continue_shadow_observation"


def test_monitoring_cannot_scale_an_unauthorized_production_contract() -> None:
    result = evaluate_champion_challenger_monitoring(
        comparison(),
        min_live_paired_days=20,
        max_drawdown_deterioration_pct=5.0,
        max_daily_cvar_deterioration_pct=0.5,
        policy_hash_consistent=True,
        contract_activation_authorized=False,
    )

    assert result["monitoring_status"] == "shadow_only_not_activatable"
    assert result["monitoring_action"] == "do_not_scale"
    assert result["contract_activation_authorized"] is False
    assert "production_contract_not_authorized" in str(result["monitoring_reason_codes"])
