from __future__ import annotations

from pathlib import Path

from industrials.transportation.investable_universe import (
    load_investable_universe_policy,
    validate_investable_universe_policy,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = (
    PROJECT_ROOT
    / "industrials"
    / "transportation"
    / "data"
    / "transportation_investable_universe_v3.yaml"
)


def test_transportation_investable_v3_is_exact_and_outcome_blind() -> None:
    policy = load_investable_universe_policy(POLICY_PATH)
    assert [group.group_id for group in policy.groups] == [
        "surface_freight_core",
        "passenger_airlines",
        "oil_tanker_operators",
    ]
    assert [len(group.tickers) for group in policy.groups] == [19, 10, 11]
    assert len(policy.selected_tickers) == len(set(policy.selected_tickers)) == 40
    assert set(policy.new_tanker_tickers) == {
        "FRO",
        "DHT",
        "TNK",
        "STNG",
        "INSW",
        "TEN",
        "NAT",
        "TRMD",
    }
    assert set(policy.existing_tanker_tickers) == {"ECO", "ASC", "HAFN"}
    assert len(policy.direct_tanker_metrics) == 16
    assert policy.derived_tanker_metrics == ("fleet_capacity_growth",)
    text = POLICY_PATH.read_text(encoding="utf-8")
    assert "membership_selection_uses_outcomes: false" in text
    assert "historical_reconstruction_authorized: false" in text
    assert "calibration_authorized: false" in text
    assert "production_promotion_authorized: false" in text


def test_transportation_investable_v3_catalog_and_exclusions_partition() -> None:
    policy = load_investable_universe_policy(POLICY_PATH)
    errors, summary = validate_investable_universe_policy(policy)
    assert errors == []
    assert summary["acceptance"] == "PASS"
    assert summary["catalog_count"] == 120
    assert summary["selected_count"] == 40
    assert summary["excluded_count"] == 80
    assert summary["group_counts"] == {
        "surface_freight_core": 19,
        "passenger_airlines": 10,
        "oil_tanker_operators": 11,
    }
