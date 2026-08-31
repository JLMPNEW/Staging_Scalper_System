from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / "technology/scripts/08_build_technology_financial_features.py"
SPEC = importlib.util.spec_from_file_location("technology_financial_features_6k_test", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _fact(metric: str, end_date: date) -> object:
    return MODULE.CanonicalFact(
        metric=metric,
        value=1.0,
        reported_currency="USD",
        source_unit="USD",
        source_taxonomy="ifrs-full",
        source_concept=metric,
        source_priority=1,
        source_quality=1.0,
        start_date=date(2025, 10, 1),
        end_date=end_date,
        filing_date=date(2026, 4, 1),
        accession="0001171843-26-002167",
        form_type="6-K",
        fiscal_year=2025,
        fiscal_period="Q4",
    )


def test_6k_uses_coherent_fact_period_instead_of_event_report_date() -> None:
    facts = [
        _fact("revenue", date(2025, 12, 31)),
        _fact("net_income", date(2025, 12, 31)),
        _fact("operating_cash_flow", date(2025, 12, 31)),
    ]

    period_end = MODULE.filing_financial_period_end(
        "6-K", date(2026, 3, 31), facts
    )

    assert period_end == date(2025, 12, 31)


def test_6k_period_resolution_fails_closed_without_two_core_metrics() -> None:
    period_end = MODULE.filing_financial_period_end(
        "6-K",
        date(2026, 3, 31),
        [_fact("revenue", date(2025, 12, 31))],
    )

    assert period_end is None


def test_financial_feature_defers_after_close_acceptance() -> None:
    availability = MODULE.filing_availability_date(
        {
            "accepted_at": "2026-08-26T22:54:46.000Z",
            "filing_date": "2026-08-27",
        }
    )

    assert availability == date(2026, 8, 27)


def test_financial_feature_uses_preclose_acceptance_same_day() -> None:
    availability = MODULE.filing_availability_date(
        {
            "accepted_at": "2026-08-26T19:30:00.000Z",
            "filing_date": "2026-08-26",
        }
    )

    assert availability == date(2026, 8, 26)
