from __future__ import annotations

import csv
from pathlib import Path

import yaml

from industrials.transportation.v3_preflight import (
    build_signal_values,
    read_peer_groups,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "industrials" / "transportation" / "data"
PEER_PATH = DATA_DIR / "transportation_v3_peer_groups.csv"
POLICY_PATH = DATA_DIR / "transportation_v3_preflight_policy.yaml"
MEMBERSHIP_PATH = (
    PROJECT_ROOT
    / "industrials"
    / "transportation"
    / "system_csvs"
    / "transportation_historical_membership.csv"
)


def load_policy() -> dict:
    return yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))


def test_peer_group_map_covers_full_surface_membership() -> None:
    peer_groups = read_peer_groups(PEER_PATH)
    policy = load_policy()
    assert len(peer_groups) == int(
        policy["universe"]["expected_member_count"]
    )
    with MEMBERSHIP_PATH.open("r", encoding="utf-8-sig", newline="") as handle:
        surface = {
            str(row["internal_ticker"]).upper(): str(row["membership_status"])
            for row in csv.DictReader(handle)
            if row["calibration_cohort_id"] == "surface_freight_and_logistics"
        }
    # RRTS is a provider-excluded identity: taxonomy rows exist, but there is
    # no usable membership row, so it appears only in the peer map.
    assert set(surface) <= set(peer_groups)
    assert set(peer_groups) - set(surface) == {"RRTS"}
    for ticker, row in peer_groups.items():
        if ticker == "RRTS":
            assert row.membership_status == "delisted"
            continue
        expected_status = (
            "active" if surface[ticker] == "active" else "delisted"
        )
        assert row.membership_status == expected_status, ticker


def test_merge_targets_are_valid_groups() -> None:
    peer_groups = read_peer_groups(PEER_PATH)
    groups = {row.peer_group for row in peer_groups.values()}
    for row in peer_groups.values():
        assert row.merge_target in groups, row.ticker


def test_policy_signals_reference_panel_metrics_and_signs() -> None:
    policy = load_policy()
    excluded = set(policy["panel"]["excluded_metrics"])
    for signal_id, (metric, transform, sign) in policy[
        "candidate_signals"
    ].items():
        assert transform in {"level", "yoy_change"}, signal_id
        assert int(sign) in {-1, 1}, signal_id
        assert metric not in excluded, signal_id


def test_yoy_change_uses_twelve_panel_months() -> None:
    dates = [f"2019-{month:02d}-28" for month in range(1, 13)] + [
        "2020-01-28"
    ]
    rows = [
        {
            "asof_date": asof,
            "ticker": "AAA",
            "metric_id": "operating_margin",
            "metric_value": str(index),
        }
        for index, asof in enumerate(dates)
    ]
    values = build_signal_values(
        rows,
        signals={"om_change": ("operating_margin", "yoy_change", 1)},
        dates=dates,
    )
    assert values == {("om_change", "2020-01-28", "AAA"): 12.0}


def test_preflight_policy_is_design_only() -> None:
    policy = load_policy()
    assert (
        policy["promotion"]["production_promotion_from_preflight"]
        == "forbidden"
    )
    assert policy["null_model"]["registered_before_outcomes"] is True
