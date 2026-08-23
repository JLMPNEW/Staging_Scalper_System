from __future__ import annotations

import importlib
from pathlib import Path

from industrials.transportation.dedicated_parser_adapter import (
    ADAPTER_VERSION,
    applicable_parser_metrics,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = PROJECT_ROOT / "industrials" / "transportation" / "data"


def test_surface_delta_contract_is_exactly_24_tickers_and_23_direct_metrics() -> None:
    module = importlib.import_module(
        "industrials.transportation.scripts.36j_build_transportation_surface_delta_census"
    )
    tickers, metrics, scope = module._surface_contract(
        DATA_ROOT / "transportation_specialized_metric_discovery_registry.csv"
    )

    assert tickers == (
        "CNI",
        "CP",
        "CSX",
        "NSC",
        "UNP",
        "ARCB",
        "ODFL",
        "SAIA",
        "XPO",
        "HUBG",
        "JBHT",
        "KNX",
        "SNDR",
        "TFII",
        "CHRW",
        "EXPD",
        "FDX",
        "LSTR",
        "UPS",
        "CVLG",
        "FWRD",
        "HTLD",
        "MRTN",
        "WERN",
    )
    assert len(metrics) == 23
    assert len(set(metrics)) == len(metrics)
    assert "surface_volume_growth" not in metrics
    assert scope
    assert len(
        {(str(row["ticker"]), str(row["metric_id"])) for row in scope}
    ) == len(scope)
    assert {
        str(row["ticker"])
        for row in scope
    } == set(tickers)
    assert all(
        str(row["metric_id"]) in applicable_parser_metrics(str(row["ticker"]))
        for row in scope
    )
    assert all(row["source_lane"] == "DP" for row in scope)
    assert all(row["discovery_status"] == "coverage_pending" for row in scope)


def test_surface_parser_release_is_incremented_for_new_work_identity() -> None:
    assert ADAPTER_VERSION == "transportation_specialized_metrics_v3.discovery9"
