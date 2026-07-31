from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_runner() -> ModuleType:
    path = PROJECT_ROOT / "portfolio_layer" / "macro" / "20a_run_macro_raw.py"
    spec = importlib.util.spec_from_file_location("portfolio_macro_raw_runner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_raw_refresh_skips_completed_same_or_later_date() -> None:
    runner = _load_runner()
    needed = runner._raw_refresh_needed
    assert needed("2026-07-24", "2026-07-24") is False
    assert needed("2026-07-25", "2026-07-24") is False


def test_raw_refresh_runs_for_missing_or_older_coverage() -> None:
    runner = _load_runner()
    needed = runner._raw_refresh_needed
    assert needed("", "2026-07-24") is True
    assert needed("2026-07-23", "2026-07-24") is True
