from __future__ import annotations

# pyright: reportMissingImports=false

import sys
from datetime import date
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MACRO_LAYER_ROOT = PROJECT_ROOT / "portfolio_layer" / "MacroLayer"
if str(MACRO_LAYER_ROOT) not in sys.path:
    sys.path.insert(0, str(MACRO_LAYER_ROOT))

from audit_macro_v2_vintage_gaps import (  # noqa: E402
    recovery_status,
    required_first_oos_date,
    subtract_periods,
)


def test_subtract_periods_handles_calendar_boundaries() -> None:
    assert subtract_periods(date(2024, 3, 31), 1, "monthly") == date(2024, 2, 29)
    assert subtract_periods(date(2026, 6, 30), 2, "quarterly") == date(2025, 12, 30)
    assert subtract_periods(date(2026, 7, 7), 3, "weekly") == date(2026, 6, 16)


def test_required_first_oos_date_uses_most_recent_required_window() -> None:
    dates = [date(2020, month, 1) for month in range(1, 7)]
    assert required_first_oos_date(dates, 4) == date(2020, 3, 1)
    assert required_first_oos_date(dates, 7) is None


def test_local_history_takes_precedence_over_provider_probe() -> None:
    assert (
        recovery_status(
            required_start=date(2010, 1, 1),
            local_earliest=date(2009, 1, 1),
            source_name="fred_alfred",
            probe_status="NOT_RUN",
            provider_earliest=None,
        )
        == "LOCAL_HISTORY_SUFFICIENT"
    )


def test_fred_gap_requires_probe_then_distinguishes_backfill_from_archive_limit() -> None:
    arguments = {
        "required_start": date(2010, 1, 1),
        "local_earliest": date(2012, 1, 1),
        "source_name": "fred_alfred",
    }
    assert recovery_status(**arguments, probe_status="NOT_RUN", provider_earliest=None) == "PROVIDER_PROBE_REQUIRED"
    assert (
        recovery_status(
            **arguments,
            probe_status="PASS",
            provider_earliest=date(2008, 1, 1),
        )
        == "PROVIDER_BACKFILL_AVAILABLE"
    )
    assert (
        recovery_status(
            **arguments,
            probe_status="PASS",
            provider_earliest=date(2011, 1, 1),
        )
        == "PROVIDER_ARCHIVE_STARTS_LATE"
    )


def test_non_fred_gap_is_routed_to_source_specific_review() -> None:
    assert (
        recovery_status(
            required_start=date(2007, 1, 1),
            local_earliest=date(2008, 1, 1),
            source_name="phillyfed_ads",
            probe_status="NOT_RUN",
            provider_earliest=None,
        )
        == "SOURCE_SPECIFIC_ARCHIVE_REVIEW"
    )
