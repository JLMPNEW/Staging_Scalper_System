from __future__ import annotations

from industrials.machinery.scoring import (
    _apply_negative_profit_valuation_cap,
    _development_score,
)
from industrials.machinery.stage12_activation import (
    production_policy_source_hashes,
)


def _development_row(
    dependence: float,
    *,
    partial: bool,
) -> dict[str, object]:
    return {
        "development_stage": "development_stage",
        "capital_raise_dependence": dependence,
        "canonical_quality": (
            "mapped_xbrl;capital_raise_proceeds_partial_component_coverage"
            if partial
            else "mapped_xbrl"
        ),
    }


def test_partial_capital_raise_lower_bound_penalizes_but_never_rewards() -> None:
    assert _development_score(_development_row(2.74, partial=True)) == 15.0
    assert _development_score(_development_row(1.0, partial=True)) == 35.0
    assert _development_score(_development_row(0.10, partial=True)) == 35.0
    assert _development_score(_development_row(0.10, partial=False)) == 75.0


def test_negative_profit_valuation_cap_is_absolute_and_one_sided() -> None:
    assert _apply_negative_profit_valuation_cap(
        50.0,
        {"negative_profit_valuation_flag": 1},
        cap=25.0,
    ) == 25.0
    assert _apply_negative_profit_valuation_cap(
        20.0,
        {"negative_profit_valuation_flag": 1},
        cap=25.0,
    ) == 20.0
    assert _apply_negative_profit_valuation_cap(
        80.0,
        {"negative_profit_valuation_flag": 0},
        cap=25.0,
    ) == 80.0


def test_production_source_seal_covers_financial_and_selection_logic() -> None:
    hashes = production_policy_source_hashes()
    assert {
        "scoring.py",
        "08_build_industrials_financial_features.py",
        "financial_metric_contract.py",
        "db.py",
        "06a_build_machinery_scoring_features.py",
        "stage9_backtest.py",
        "stage12_governance.py",
    }.issubset(hashes)
