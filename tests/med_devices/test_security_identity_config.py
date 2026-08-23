from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def test_ticker_transition_financial_boundaries_fail_closed() -> None:
    config = yaml.safe_load((ROOT / "med_devices" / "config.yaml").read_text(encoding="utf-8"))
    boundaries = config["financial_features"]["financial_history_start_by_ticker"]
    expected_missing = config["financial_features"]["expected_precommercial_missing_revenue"]

    assert boundaries["PRPO"] == "2017-06-30"
    assert boundaries["FRNM"] == "2026-07-21"
    assert expected_missing["FRNM"] == "precommercial_no_reported_revenue_history"
