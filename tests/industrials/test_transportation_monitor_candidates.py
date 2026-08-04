from __future__ import annotations

from pathlib import Path

import yaml

from industrials.transportation.monitor_candidates import (
    _zscores,
    sleeve_rows,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = (
    PROJECT_ROOT
    / "industrials"
    / "transportation"
    / "data"
    / "transportation_monitor_candidates_v1.yaml"
)


def load_contract() -> dict:
    return yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))


def test_contract_registers_three_candidates_with_fixed_weights() -> None:
    contract = load_contract()
    candidates = contract["candidates"]
    assert set(candidates) == {
        "C1_SLEEVE",
        "C2_FROZEN_V2_RANK",
        "C3_ASSET_LIGHT_THREE_SIGNAL",
    }
    asset_light = candidates["C3_ASSET_LIGHT_THREE_SIGNAL"]
    assert len(asset_light["universe"]) == 13
    weights = [
        float(spec["weight"]) for spec in asset_light["signals"].values()
    ]
    assert abs(sum(weights) - 1.0) < 1e-9
    signs = {
        signal_id: int(spec["sign"])
        for signal_id, spec in asset_light["signals"].items()
    }
    assert signs == {
        "asset_turnover_yoy_change": 1,
        "interest_coverage_level": 1,
        "realized_volatility_60d": -1,
    }
    assert contract["evaluation"]["null_candidate"] == "C1_SLEEVE"


def test_sleeve_membership_is_rank_ready_surface_only() -> None:
    rank_rows = [
        {
            "ticker": "UNP",
            "calibration_cohort": "surface_freight_and_logistics",
            "rank_ready_flag": "1",
        },
        {
            "ticker": "ODFL",
            "calibration_cohort": "surface_freight_and_logistics",
            "rank_ready_flag": "1",
        },
        {
            "ticker": "DAL",
            "calibration_cohort": "air_transport_and_aviation_services",
            "rank_ready_flag": "1",
        },
        {
            "ticker": "CHRW",
            "calibration_cohort": "surface_freight_and_logistics",
            "rank_ready_flag": "0",
        },
    ]
    rows = sleeve_rows(rank_rows, asof="2026-07-30")
    assert [row["ticker"] for row in rows] == ["ODFL", "UNP"]
    assert all(float(row["weight"]) == 0.5 for row in rows)


def test_zscores_zero_spread_and_small_samples_are_safe() -> None:
    assert _zscores({"A": 3.0}) == {"A": 0.0}
    assert _zscores({"A": 2.0, "B": 2.0}) == {"A": 0.0, "B": 0.0}
    scores = _zscores({"A": 1.0, "B": 3.0})
    assert scores["A"] < 0 < scores["B"]
    assert abs(scores["A"] + scores["B"]) < 1e-12


def test_capture_is_registered_on_the_nightly_rail() -> None:
    registry = yaml.safe_load(
        (PROJECT_ROOT / "orchestration" / "registry.yaml").read_text(
            encoding="utf-8"
        )
    )
    sector = next(
        entry
        for entry in registry["sectors"]
        if entry["name"] == "transportation"
    )
    assert sector["required"] is False
    assert sector["require_oos_valid"] is False
    scripts = [step["script"] for step in sector["daily_post_steps"]]
    assert scripts[-1].endswith(
        "21d_capture_transportation_monitor_candidates.py"
    )
    assert registry["group_order"]["industrials"][-1] == "transportation"
