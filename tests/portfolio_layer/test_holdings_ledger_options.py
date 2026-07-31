from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any, cast


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (
    PROJECT_ROOT
    / "portfolio_layer"
    / "ledger"
    / "31_build_holdings_ledger.py"
)


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "test_build_holdings_ledger",
        SCRIPT,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_new_option_without_available_trade_history_is_warn_only() -> None:
    module = cast(Any, _load_script())

    lots, checks = module._build_option_lots(
        "2026-07-24",
        "source-hash",
        {
            "BSX 31JUL26 52 C": {
                "quantity": "-2",
                "cost_basis": "-30.732682",
            }
        },
        [],
        [],
        "2026-07-22",
    )

    assert lots[0]["quantity"] == "-2"
    assert lots[0]["entry_date_unknown"] == "1"
    assert checks[0]["check"] == "option_quantity_reconciles"
    assert checks[0]["status"] == "PASS"
    assert checks[1]["check"] == "option_pre_report_lots"
    assert checks[1]["status"] == "WARN"


def test_prior_option_quantity_mismatch_remains_hard_failure() -> None:
    module = cast(Any, _load_script())

    _lots, checks = module._build_option_lots(
        "2026-07-24",
        "source-hash",
        {
            "FISV 07AUG26 57 C": {
                "quantity": "-2",
                "cost_basis": "-100",
            }
        },
        [],
        [
            {
                "asset_category": "Equity and Index Options",
                "symbol": "FISV 07AUG26 57 C",
                "quantity": "-1",
                "entry_date": "2026-07-20",
                "entry_date_unknown": "0",
            }
        ],
        "2026-07-22",
    )

    assert checks[0]["check"] == "option_quantity_reconciles"
    assert checks[0]["status"] == "FAIL"
