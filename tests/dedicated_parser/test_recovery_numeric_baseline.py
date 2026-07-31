from __future__ import annotations

import math

import pytest

from dedicated_parser.recovery import _finite_float_or_none


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
