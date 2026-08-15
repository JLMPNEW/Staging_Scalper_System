from __future__ import annotations

import csv
from pathlib import Path

from industrials.transportation.investable_universe import (
    LATEST_POLICY_VERSION,
    load_investable_universe_policy,
    validate_investable_universe_policy,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = (
    PROJECT_ROOT
    / "industrials"
    / "transportation"
    / "data"
    / "transportation_investable_universe_v4.yaml"
)
AIRLINE_TICKERS = {
    "AAL",
    "ALGT",
    "ALK",
    "CPA",
    "DAL",
    "JBLU",
    "LUV",
    "RYAAY",
    "UAL",
    "ULCC",
}


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def test_transportation_investable_v4_excludes_airlines_from_production() -> None:
    policy = load_investable_universe_policy(POLICY_PATH)
    assert policy.policy_version == LATEST_POLICY_VERSION
    assert [group.group_id for group in policy.groups] == [
        "surface_freight_core",
        "oil_tanker_operators",
    ]
    assert [len(group.tickers) for group in policy.groups] == [19, 11]
    assert len(policy.selected_tickers) == len(set(policy.selected_tickers)) == 30
    assert set(policy.selected_tickers).isdisjoint(AIRLINE_TICKERS)
    config_text = (PROJECT_ROOT / "industrials" / "config.yaml").read_text(
        encoding="utf-8"
    )
    assert (
        'investable_universe_policy: "transportation/data/'
        'transportation_investable_universe_v4.yaml"'
    ) in config_text
    assert (
        "investable_universe_version: transportation_investable_universe_v4"
    ) in config_text


def test_transportation_investable_v4_catalog_partition_is_exact() -> None:
    policy = load_investable_universe_policy(POLICY_PATH)
    errors, summary = validate_investable_universe_policy(policy)
    assert errors == []
    assert summary["acceptance"] == "PASS"
    assert summary["catalog_count"] == 120
    assert summary["selected_count"] == 30
    assert summary["excluded_count"] == 90
    assert summary["group_counts"] == {
        "surface_freight_core": 19,
        "oil_tanker_operators": 11,
    }


def test_transportation_investable_v4_airlines_are_research_monitor_only() -> None:
    policy = load_investable_universe_policy(POLICY_PATH)
    excluded = {
        row["ticker"]: row for row in _read_csv(policy.exclusions_path)
    }
    assert AIRLINE_TICKERS <= set(excluded)
    for ticker in AIRLINE_TICKERS:
        assert excluded[ticker]["disposition"] == "research_only"
        assert (
            excluded[ticker]["exclusion_group"]
            == "passenger_airlines_monitor_only"
        )

    overlays = {
        row["ticker"]: row
        for row in _read_csv(
            PROJECT_ROOT
            / "industrials"
            / "transportation"
            / "system_csvs"
            / "transportation_classification_overlays.csv"
        )
    }
    for ticker in AIRLINE_TICKERS:
        assert (
            overlays[ticker]["portfolio_role"]
            == "airline_satellite_research"
        )
        assert overlays[ticker]["source"] == POLICY_PATH.stem
