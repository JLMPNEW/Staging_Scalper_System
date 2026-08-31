from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "industrials" / "scripts" / "03_sync_industrials_yahoo_adjusted_prices.py"


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("industrials_yahoo_price_sync", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_daily_fetch_uses_bounded_overlap() -> None:
    module = load_script()
    assert module.incremental_fetch_start(
        configured_start=date(2010, 1, 1),
        existing_last_bar=date(2026, 8, 27),
        overlap_calendar_days=14,
        full_history=False,
        adjustment_coverage_complete=True,
    ) == date(2026, 8, 13)


def test_explicit_repair_and_incomplete_adjustments_keep_full_history() -> None:
    module = load_script()
    kwargs = {
        "configured_start": date(2010, 1, 1),
        "existing_last_bar": date(2026, 8, 27),
        "overlap_calendar_days": 14,
    }
    assert module.incremental_fetch_start(**kwargs, full_history=True, adjustment_coverage_complete=True) == date(2010, 1, 1)
    assert module.incremental_fetch_start(**kwargs, full_history=False, adjustment_coverage_complete=False) == date(2010, 1, 1)


def test_action_signature_is_stable_across_database_and_payload_types() -> None:
    module = load_script()
    left = module.action_signature(
        action_type="Dividend",
        action_date="2026-08-28T00:00:00Z",
        cash_amount="0.2500000000000",
    )
    right = module.action_signature(
        action_type="dividend",
        action_date="2026-08-28",
        cash_amount=0.25,
    )
    assert left == right
