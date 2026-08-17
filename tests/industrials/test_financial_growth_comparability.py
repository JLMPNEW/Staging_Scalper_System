from __future__ import annotations

import importlib

import pytest


financials = importlib.import_module(
    "industrials.scripts.08_build_industrials_financial_features"
)


def _duration_row(
    *,
    metric: str = "revenue",
    value: float = 100.0,
    start: str,
    end: str,
    form: str = "10-K",
    fiscal_period: str = "FY",
) -> dict[str, object]:
    return {
        "canonical_metric": metric,
        "value": value,
        "period_start": start,
        "period_end": end,
        "form_type": form,
        "fiscal_period": fiscal_period,
        "filing_date": end,
        "source_priority": 10,
    }


def test_quarterly_comparative_inside_annual_filing_is_not_annual() -> None:
    comparative_q3 = _duration_row(
        start="2024-07-01",
        end="2024-09-30",
        form="10-K",
        fiscal_period="FY",
    )
    assert not financials.is_annual_fact(comparative_q3)


def test_full_fiscal_year_is_annual() -> None:
    annual = _duration_row(start="2024-01-01", end="2024-12-31")
    assert financials.is_annual_fact(annual)


def test_previous_annual_skips_embedded_quarterly_comparative() -> None:
    current = _duration_row(
        value=120.0, start="2024-01-01", end="2024-12-31"
    )
    prior_annual = _duration_row(
        value=100.0, start="2023-01-01", end="2023-12-31"
    )
    later_quarter = _duration_row(
        value=30.0, start="2023-07-01", end="2023-09-30"
    )
    selected = financials.select_previous_annual(
        [current, prior_annual, later_quarter], "revenue", current
    )
    assert selected is prior_annual


def test_previous_annual_fails_closed_without_consecutive_period() -> None:
    current = _duration_row(
        value=120.0, start="2024-01-01", end="2024-12-31"
    )
    stale = _duration_row(
        value=80.0, start="2021-01-01", end="2021-12-31"
    )
    assert (
        financials.select_previous_annual([current, stale], "revenue", current)
        is None
    )


def test_transportation_structural_growth_requires_bridge() -> None:
    current = _duration_row(
        value=250.0, start="2024-01-01", end="2024-12-31"
    )
    prior = _duration_row(
        value=100.0, start="2023-01-01", end="2023-12-31"
    )
    value, flag = financials.validated_annual_growth(
        250.0,
        100.0,
        current,
        prior,
        model_family="transportation",
        metric="revenue",
    )
    assert value is None
    assert flag == "revenue_yoy_structural_bridge_required"


def test_ordinary_transportation_growth_is_retained() -> None:
    current = _duration_row(
        value=120.0, start="2024-01-01", end="2024-12-31"
    )
    prior = _duration_row(
        value=100.0, start="2023-01-01", end="2023-12-31"
    )
    value, flag = financials.validated_annual_growth(
        120.0,
        100.0,
        current,
        prior,
        model_family="transportation",
        metric="revenue",
    )
    assert value == pytest.approx(0.2)
    assert flag == ""
