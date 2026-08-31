from __future__ import annotations

from dataclasses import dataclass

from biotech_index.core.portfolio_validation import validation_candidate_survives_multimetric


@dataclass(frozen=True)
class Rules:
    min_paired_dates: int = 20
    prefer_profit_factor_at_least: float = 1.0
    max_loss20_deterioration_pct: float = 2.0
    max_loss40_deterioration_pct: float = 1.0
    max_cvar_deterioration_pct: float = 5.0
    max_drawdown_deterioration_pct: float = 5.0
    max_top3_contribution_pct: float = 55.0
    min_active_date_coverage_pct: float = 25.0
    min_robust_profit_factor: float = 1.0
    require_robust_profit_factor_support: bool = True


def _metrics() -> dict[str, object]:
    return {
        "paired_date_count": 40,
        "active_date_count": 30,
        "evaluation_date_count": 40,
        "paired_delta_bootstrap_lcb_pct": -1.0,
        "candidate_mean_return_pct": 12.0,
        "incumbent_mean_return_pct": 6.0,
        "candidate_lcb_return_pct": 2.0,
        "incumbent_lcb_return_pct": 1.0,
        "candidate_hit_rate_pct": 58.0,
        "incumbent_hit_rate_pct": 52.0,
        "candidate_profit_factor": 1.4,
        "incumbent_profit_factor": 1.1,
        "candidate_winsorized_profit_factor": 1.3,
        "candidate_profit_factor_ex_largest_winner": 1.2,
        "candidate_profit_factor_ex_top3_winners": 0.9,
        "candidate_loss20_rate_pct": 10.0,
        "incumbent_loss20_rate_pct": 12.0,
        "candidate_loss40_rate_pct": 2.0,
        "incumbent_loss40_rate_pct": 3.0,
        "candidate_cvar_return_pct": -25.0,
        "incumbent_cvar_return_pct": -27.0,
        "candidate_max_drawdown_pct": -30.0,
        "incumbent_max_drawdown_pct": -32.0,
        "candidate_top3_gain_contribution_pct": 40.0,
    }


def test_negative_bootstrap_lcb_does_not_veto_balanced_validation_winner() -> None:
    assert validation_candidate_survives_multimetric(_metrics(), Rules())


def test_material_tail_harm_blocks_validation_candidate() -> None:
    metrics = _metrics()
    metrics["candidate_loss40_rate_pct"] = 8.0
    assert not validation_candidate_survives_multimetric(metrics, Rules())


def test_inadequate_robust_profit_factor_support_blocks_candidate() -> None:
    metrics = _metrics()
    metrics["candidate_winsorized_profit_factor"] = 0.8
    metrics["candidate_profit_factor_ex_largest_winner"] = 0.8
    assert not validation_candidate_survives_multimetric(metrics, Rules())
