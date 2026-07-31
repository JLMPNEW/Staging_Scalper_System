from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = (
    PROJECT_ROOT
    / "industrials"
    / "transportation"
    / "scripts"
    / "22_finalize_transportation_implementation.py"
)


def _module():
    spec = importlib.util.spec_from_file_location(
        "transportation_completion_script",
        SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _calibration_payloads():
    decisions = {
        metric: {
            "validation_selected_weight": 0.10,
            "final_research_weight": 0.0,
            "decision": "RETAIN_ZERO_OVERLAY",
        }
        for metric in (
            "fleet_utilization",
            "operating_ratio",
            "passenger_load_factor",
        )
    }
    manifest = {
        "acceptance": "PASS",
        "calibration_executed": True,
        "holdout_used_for_selection": False,
        "candidate_decisions": decisions,
        "operations": {
            "calibration_invocations": 1,
            "database_writes": 0,
            "feature_rebuilds": 0,
            "membership_rebuilds": 0,
            "network_requests": 0,
            "parser_invocations": 0,
            "portfolio_writes": 0,
            "production_config_writes": 0,
        },
    }
    validation = {
        "acceptance": "PASS",
        "holdout_used_for_selection": False,
        "confirmed_research_metric_count": 0,
        "final_research_weights": {
            metric: 0.0 for metric in decisions
        },
    }
    return manifest, validation


def test_price_gate_requires_xtn_and_exact_right_edge() -> None:
    module = _module()
    rows = [
        {
            "ticker": f"T{index}",
            "is_benchmark": "0",
            "status": "success",
            "last_bar_date": "2026-07-30",
        }
        for index in range(112)
    ]
    rows.extend(
        {
            "ticker": ticker,
            "is_benchmark": "1",
            "status": "success",
            "last_bar_date": "2026-07-30",
        }
        for ticker in ("IYT", "XTN", "SPY")
    )
    rows[0]["status"] = "already_current"
    passed, detail = module.price_gate(rows, asof="2026-07-30")
    assert passed
    assert detail["benchmarks"] == ["IYT", "SPY", "XTN"]

    rows[-2]["last_bar_date"] = "2026-07-29"
    passed, detail = module.price_gate(rows, asof="2026-07-30")
    assert not passed
    assert detail["right_edge_failures"] == ["XTN"]


def test_zero_overlay_is_a_completed_calibration_result() -> None:
    module = _module()
    manifest, validation = _calibration_payloads()
    assert module.calibration_errors(manifest, validation) == []


def test_second_calibration_invocation_fails_completion_gate() -> None:
    module = _module()
    manifest, validation = _calibration_payloads()
    manifest["operations"]["calibration_invocations"] = 2
    errors = module.calibration_errors(manifest, validation)
    assert "calibration manifest is not a passing exactly-once run" in errors
