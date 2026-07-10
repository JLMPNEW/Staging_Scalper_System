from __future__ import annotations

# pyright: reportMissingImports=false

import sys
from datetime import date
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MACRO_LAYER_ROOT = PROJECT_ROOT / "portfolio_layer" / "MacroLayer"
if str(MACRO_LAYER_ROOT) not in sys.path:
    sys.path.insert(0, str(MACRO_LAYER_ROOT))

from macro_serving_common import release_staleness_days  # noqa: E402
from portfolio_layer.macro.contract import (  # noqa: E402
    regime_application_errors,
    regime_table_for_source,
    verify_v2_promotion_manifest,
)


def test_daily_staleness_ignores_weekend_and_observed_federal_holiday() -> None:
    assert release_staleness_days(
        as_of=date(2026, 7, 7),
        anchor=date(2026, 7, 2),
        frequency="daily",
    ) == 2


def test_lower_frequency_staleness_remains_calendar_based() -> None:
    assert release_staleness_days(
        as_of=date(2026, 7, 7),
        anchor=date(2026, 7, 2),
        frequency="weekly",
    ) == 5


def test_covered_regime_is_allocation_safe() -> None:
    row = {
        "active_current_regime": "SLOW_GROWTH",
        "active_next_regime": "STAGFLATION",
        "current_confidence": "0.36",
        "next_confidence": "0.22",
        "coverage_flag": "1",
        "regime_override_reason": "CURRENT:NON_DECISION_DATE|NEXT:NON_DECISION_DATE",
    }
    assert regime_application_errors(row) == []


def test_uncovered_carried_regime_is_not_allocation_safe() -> None:
    row = {
        "active_current_regime": "SLOW_GROWTH",
        "active_next_regime": "STAGFLATION",
        "current_confidence": "",
        "next_confidence": "",
        "coverage_flag": "0",
        "regime_override_reason": "CURRENT:UNCOVERED|NEXT:UNCOVERED",
    }
    errors = regime_application_errors(row)
    assert "coverage_flag=0" in errors
    assert "invalid_current_confidence=''" in errors
    assert "invalid_next_confidence=''" in errors
    assert "override_reason=CURRENT:UNCOVERED|NEXT:UNCOVERED" in errors


def test_malformed_regime_fields_are_not_allocation_safe() -> None:
    row = {
        "active_current_regime": "UNKNOWN",
        "active_next_regime": "STAGFLATION",
        "current_confidence": "1.2",
        "next_confidence": "0.2",
        "coverage_flag": "1.5",
        "regime_override_reason": "",
    }
    errors = regime_application_errors(row)
    assert "coverage_flag=1.5" in errors
    assert "invalid_current_regime='UNKNOWN'" in errors
    assert "invalid_current_confidence='1.2'" in errors


def test_regime_source_selector_is_allowlisted() -> None:
    assert regime_table_for_source("v1") == "macro_regime_decision_daily"
    assert regime_table_for_source(" V2 ") == "macro_regime_v2_decision_daily"


def test_regime_source_selector_rejects_unknown_source() -> None:
    try:
        regime_table_for_source("latest")
    except ValueError as exc:
        assert "Unsupported macro regime source" in str(exc)
    else:
        raise AssertionError("Unknown regime source was accepted")


def test_v2_promotion_manifest_must_be_inside_allowed_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside" / "macro_regime_v2_promotion_manifest.json"
    outside.parent.mkdir()
    outside.write_text("{}", encoding="utf-8")
    errors = verify_v2_promotion_manifest(
        outside,
        model_version="v2",
        macro_config_path=tmp_path / "config.yaml",
        builder_path=tmp_path / "builder.py",
        allowed_root=tmp_path / "allowed",
    )
    assert errors == [f"manifest_outside_v2_output_root={outside.resolve()}"]
