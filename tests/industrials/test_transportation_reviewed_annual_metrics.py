from __future__ import annotations

import math

from industrials.transportation.reviewed_annual_metrics import (
    _calculate,
    load_scope_pairs,
)


def _operands(**values: float) -> dict[str, dict[str, float]]:
    return {
        metric: {"value": value}
        for metric, value in values.items()
    }


def test_required_metric_repair_scope_is_complete_and_narrow() -> None:
    pairs = load_scope_pairs()
    assert len(pairs) == 32
    assert ("ASR", "capex_to_revenue") in pairs
    assert ("ASR", "fcf_margin") in pairs
    assert ("PBI", "operating_margin") in pairs
    assert ("RUBI", "maximum_drawdown_12m") in pairs
    assert ("GATX", "capex_to_revenue") not in pairs


def test_aligned_annual_financial_formulas_use_one_currency_window() -> None:
    capex_status, capex_ratio, _ = _calculate(
        "capex_to_revenue",
        _operands(revenue=200.0, capex=50.0),
    )
    fcf_status, fcf_margin, _ = _calculate(
        "fcf_margin",
        _operands(
            revenue=200.0,
            operating_cash_flow=80.0,
            capex=50.0,
        ),
    )
    margin_status, operating_margin, _ = _calculate(
        "operating_margin",
        _operands(revenue=200.0, operating_income=30.0),
    )

    assert capex_status == fcf_status == margin_status == "DERIVED"
    assert math.isclose(capex_ratio or 0.0, 0.25)
    assert math.isclose(fcf_margin or 0.0, 0.15)
    assert math.isclose(operating_margin or 0.0, 0.15)


def test_non_burning_annual_window_resolves_runway_and_dependence() -> None:
    operands = _operands(operating_cash_flow=80.0, capex=50.0)
    runway_status, runway, runway_reason = _calculate(
        "cash_runway_years",
        operands,
    )
    dependence_status, dependence, dependence_reason = _calculate(
        "capital_raise_dependence",
        operands,
    )

    assert runway_status == "NOT_APPLICABLE"
    assert runway is None
    assert dependence_status == "DERIVED"
    assert dependence == 0.0
    assert runway_reason == dependence_reason


def test_burning_annual_window_requires_aligned_cash_and_proceeds() -> None:
    base = _operands(operating_cash_flow=20.0, capex=50.0)
    assert _calculate("cash_runway_years", base)[0] == "NOT_DISCLOSED"
    assert _calculate("capital_raise_dependence", base)[0] == "NOT_DISCLOSED"

    runway = _calculate(
        "cash_runway_years",
        {**base, **_operands(cash_and_equivalents=90.0)},
    )
    dependence = _calculate(
        "capital_raise_dependence",
        {**base, **_operands(equity_issuance_proceeds=15.0)},
    )
    assert runway[:2] == ("DERIVED", 3.0)
    assert dependence[:2] == ("DERIVED", 0.5)
