from __future__ import annotations

import pytest

from portfolio_layer.costs.cost_common import published_weight_rounding_tolerance


def test_published_weight_tolerance_covers_aggregate_rounding_dust() -> None:
    tolerance = published_weight_rounding_tolerance(35)

    assert 0.0499999997 + tolerance >= 0.05
    assert 0.049999 + tolerance < 0.05


@pytest.mark.parametrize("row_count", [0, -1])
def test_published_weight_tolerance_rejects_invalid_row_count(row_count: int) -> None:
    with pytest.raises(ValueError, match="row_count must be positive"):
        published_weight_rounding_tolerance(row_count)


def test_published_weight_tolerance_rejects_invalid_precision() -> None:
    with pytest.raises(ValueError, match="decimal_places must be non-negative"):
        published_weight_rounding_tolerance(1, decimal_places=-1)
