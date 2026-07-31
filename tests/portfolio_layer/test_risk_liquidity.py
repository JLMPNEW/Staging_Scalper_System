from __future__ import annotations

import pytest

from portfolio_layer.risk.liquidity import validate_snapshot_requested_tickers


def test_snapshot_requested_tickers_match_current_universe() -> None:
    expected = {"GRC", "LII"}

    assert validate_snapshot_requested_tickers(
        expected, ["lii", "GRC"]
    ) == expected


def test_snapshot_requested_tickers_reject_stale_universe() -> None:
    with pytest.raises(ValueError, match="requested universe is stale"):
        validate_snapshot_requested_tickers(
            {"GRC", "LII"},
            ["GRC"],
        )


@pytest.mark.parametrize("requested", [["GRC", "GRC"], ["GRC", ""]])
def test_snapshot_requested_tickers_reject_duplicates_and_blanks(
    requested: list[str],
) -> None:
    with pytest.raises(ValueError, match="blank or duplicate"):
        validate_snapshot_requested_tickers({"GRC"}, requested)
