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
    paths = result["paths"]
    assert isinstance(paths, dict)
    for value in paths.values():
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


def test_fro_form4_gap_is_governed_as_foreign_private_issuer() -> None:
    overrides_path = (
        DEFAULT_POSITIONING_CONFIG.parent
        / "system_csvs"
        / "transportation_positioning_overrides.csv"
    )
    with overrides_path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = {row["ticker"]: row for row in csv.DictReader(handle)}
    fro = rows["FRO"]
    assert fro["form4_exempt"] == "1"
    assert (
        fro["form4_exemption_reason"]
        == "FOREIGN_PRIVATE_ISSUER_SECTION16_NOT_APPLICABLE"
    )
    assert fro["valid_from"] == "2026-08-24"
    assert fro["reviewed_at"] == "2026-08-29"


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


def test_positioning_wrapper_allows_family_scoped_snapshot_output(
    tmp_path,
) -> None:
    output = tmp_path / "transportation" / "positioning.csv"
    argv = shared_argv(
        "09_import_industrials_positioning.py",
        ["--snapshot-output-csv", str(output)],
    )
    assert "--snapshot-output-csv" not in argv
    assert argv[argv.index("--output-csv") + 1] == str(output.resolve())


def test_positioning_wrapper_rejects_cross_family_snapshot_output(
    tmp_path,
) -> None:
    output = tmp_path / "defense" / "positioning.csv"
    with pytest.raises(ValueError, match="transportation-scoped"):
        shared_argv(
            "09_import_industrials_positioning.py",
            ["--snapshot-output-csv", str(output)],
        )
