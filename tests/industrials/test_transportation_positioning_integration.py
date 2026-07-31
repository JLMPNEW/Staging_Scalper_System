from __future__ import annotations

import csv

import pytest

from industrials.core.config import load_yaml
from industrials.transportation.positioning_integration import (
    DEFAULT_POSITIONING_CONFIG,
    shared_argv,
    validate_positioning_config,
)


def test_transportation_positioning_config_is_family_scoped() -> None:
    result = validate_positioning_config(DEFAULT_POSITIONING_CONFIG)
    config = load_yaml(DEFAULT_POSITIONING_CONFIG)
    positioning = config["positioning_import"]
    assert result["model_family"] == "transportation"
    assert result["min_form4_covered_fraction"] == 1.00
    assert positioning["require_upstream_13f_for_gate"] is True
    assert positioning["require_upstream_short_for_gate"] is True
    assert positioning["require_short_pct_float_for_gate"] is False
    assert positioning["require_upstream_borrow_for_gate"] is True
    for value in result["paths"].values():
        lowered = str(value).lower()
        assert "transportation" in lowered
        assert "defense" not in lowered
        assert "machinery" not in lowered


def test_azul_post_restructuring_13f_gap_is_explicit_and_time_bounded() -> None:
    overrides_path = (
        DEFAULT_POSITIONING_CONFIG.parent
        / "system_csvs"
        / "transportation_positioning_overrides.csv"
    )
    with overrides_path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = {row["ticker"]: row for row in csv.DictReader(handle)}
    azul = rows["AZUL"]
    assert azul["institutional_13f_exempt"] == "1"
    assert (
        azul["institutional_13f_exemption_reason"]
        == "POST_RESTRUCTURING_NO_Q1_2026_13F_HOLDINGS"
    )
    assert azul["institutional_13f_exempt_until"] == "2026-10-15"


def test_positioning_wrapper_pins_family_and_config() -> None:
    argv = shared_argv(
        "09_import_industrials_positioning.py",
        ["--asof", "2026-07-22", "--features-only"],
    )
    assert argv[1:5] == [
        "--config",
        str(DEFAULT_POSITIONING_CONFIG.resolve()),
        "--model-family",
        "transportation",
    ]
    assert argv[-3:] == ["--asof", "2026-07-22", "--features-only"]


@pytest.mark.parametrize(
    "argument",
    ("--config=bad.yaml", "--model-family=defense", "--output-csv=bad.csv"),
)
def test_positioning_wrapper_rejects_pinned_overrides(
    argument: str,
) -> None:
    with pytest.raises(ValueError, match="pinned"):
        shared_argv(
            "14_validate_industrials_sec_positioning_stages.py",
            [argument],
        )
