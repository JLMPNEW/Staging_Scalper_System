from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path


def load_validation_module():
    root = Path(__file__).resolve().parents[2]
    path = root / "biotech_index" / "scripts" / "40_validate_biotech_borrow_availability.py"
    spec = importlib.util.spec_from_file_location("borrow_validation_script", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_point_in_time_borrow_features_uses_fee_history_without_future_snapshot() -> None:
    module = load_validation_module()
    history = {
        "AAA": module.BorrowHistory(
            days=[date(2025, 1, 15), date(2025, 2, 15), date(2025, 3, 1)],
            rates=[0.02, 0.08, 0.12],
        )
    }
    snapshots = {"AAA": [module.ShortableSnapshot(day=date(2025, 6, 1), shares=25_000.0)]}

    features = module.point_in_time_borrow_features(
        ticker="AAA",
        asof=date(2025, 3, 2),
        history_by_ticker=history,
        snapshots_by_ticker=snapshots,
        max_fee_staleness_days=10,
        max_snapshot_staleness_days=7,
        hard_to_borrow_shares=50_000.0,
    )

    assert features["borrow_data_available_flag"] == 1.0
    assert features["borrow_snapshot_available_flag"] == 0.0
    assert features["borrow_rate_current"] == 0.12
    assert features["hard_to_borrow_flag"] == 0.0
    assert features["shortable_shares"] == ""


def test_point_in_time_borrow_features_uses_current_shortable_snapshot_only_when_available() -> None:
    module = load_validation_module()
    history = {
        "AAA": module.BorrowHistory(
            days=[date(2025, 5, 15), date(2025, 5, 30)],
            rates=[0.08, 0.10],
        )
    }
    snapshots = {"AAA": [module.ShortableSnapshot(day=date(2025, 6, 1), shares=25_000.0)]}

    features = module.point_in_time_borrow_features(
        ticker="AAA",
        asof=date(2025, 6, 2),
        history_by_ticker=history,
        snapshots_by_ticker=snapshots,
        max_fee_staleness_days=10,
        max_snapshot_staleness_days=7,
        hard_to_borrow_shares=50_000.0,
    )

    assert features["borrow_data_available_flag"] == 1.0
    assert features["borrow_snapshot_available_flag"] == 1.0
    assert features["hard_to_borrow_flag"] == 1.0
    assert features["shortable_shares"] == 25_000.0
