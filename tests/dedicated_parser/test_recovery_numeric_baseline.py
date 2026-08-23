from __future__ import annotations

import math
import sqlite3

import pytest

from dedicated_parser.recovery import _anchor_periods, _finite_float_or_none


@pytest.mark.parametrize("value", [None, "", "   ", "not-a-number", True])
def test_non_numeric_baseline_values_are_not_compared(value: object) -> None:
    assert _finite_float_or_none(value) is None


@pytest.mark.parametrize("value", [math.inf, -math.inf, math.nan, "NaN"])
def test_non_finite_baseline_values_are_not_compared(value: object) -> None:
    assert _finite_float_or_none(value) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1, 1.0), (1.25, 1.25), (" 12.5 ", 12.5)],
)
def test_numeric_baseline_values_are_compared(
    value: object,
    expected: float,
) -> None:
    assert _finite_float_or_none(value) == expected


def test_anchor_periods_fail_closed_when_sector_table_lacks_period_column() -> None:
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            '''CREATE TABLE feature_financial_statement (
                   ticker TEXT, model_family TEXT, asof_date TEXT
               )'''
        )
        assert _anchor_periods(
            conn,
            model_family='consumer_defensive',
            asof_date='2026-08-14',
            tickers=['KO'],
        ) == {}
    finally:
        conn.close()
