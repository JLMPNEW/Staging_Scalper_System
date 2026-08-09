from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from portfolio_layer.risk.liquidity import validate_snapshot_requested_tickers


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_collector() -> ModuleType:
    path = (
        PROJECT_ROOT
        / "portfolio_layer"
        / "risk"
        / "05c_collect_ib_historical_spread_samples.py"
    )
    spec = importlib.util.spec_from_file_location("liquidity_collector_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def test_connection_fallback_replays_only_bounded_prior_partition(
    tmp_path: Path,
) -> None:
    collector = _load_collector()
    db_path = tmp_path / "portfolio.sqlite"
    sample: dict[str, object] = {
        field: "" for field in collector.IB_SPREAD_SAMPLE_FIELDS
    }
    sample.update(
        {
            "as_of_date": "2026-08-06",
            "ticker": "AAA",
            "query_symbol": "AAA",
            "target_time_et": "11:00",
            "bar_date_et": "2026-08-06",
            "bar_timestamp_et": "2026-08-06T11:00:00-04:00",
            "bar_size": "5 mins",
            "bid": 99.9,
            "ask": 100.1,
            "midpoint": 100.0,
            "spread_bps": 20.0,
            "half_spread_bps": 10.0,
            "source": "ibkr_historical_bid_ask",
            "status": "ok",
            "reason": "",
        }
    )
    with collector.connect(db_path) as conn:
        collector.init_liquidity_tables(conn)
        collector.upsert_spread_samples(conn, [sample])

    rows, source_as_of = collector._load_latest_bounded_db_samples(
        db_path,
        as_of="2026-08-07",
        tickers=["AAA"],
        max_partition_age_days=1,
    )

    assert source_as_of == "2026-08-06"
    assert rows[0]["as_of_date"] == "2026-08-07"
    assert rows[0]["bar_date_et"] == "2026-08-06"
    with pytest.raises(ValueError, match="beyond max_stale_liquidity_days"):
        collector._load_latest_bounded_db_samples(
            db_path,
            as_of="2026-08-08",
            tickers=["AAA"],
            max_partition_age_days=1,
        )
