from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = ROOT / "med_devices" / "scripts"


def load_script(filename: str, module_name: str) -> ModuleType:
    path = SCRIPT_DIR / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def panel_rows() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for asof in ("2025-06-02", "2025-06-03", "2025-06-04"):
        rows.append(
            {
                "asof_date": asof,
                "cohort_excess_return_30d": "0.01",
                "cohort_excess_return_60d": "0.02",
                "cohort_excess_return_120d": "0.03",
            }
        )
    rows.append(
        {
            "asof_date": "2025-06-05",
            "cohort_excess_return_30d": "0.01",
            "cohort_excess_return_60d": "0.02",
            "cohort_excess_return_120d": "",
        }
    )
    rows.append({"asof_date": "2025-06-06"})
    return rows


def test_gate_window_anchors_on_latest_complete_label_date() -> None:
    module = load_script("25_optimize_med_device_gates_by_cohort.py", "med_gate_complete_window_test")
    config = {"calibration": {"validation_window_asofs": 2}}

    assert module.resolve_calibration_dates(config, panel_rows(), horizons=[30, 60, 120]) == (
        "2025-06-02",
        "2025-06-03",
        "2025-06-04",
    )


def test_safe_core_window_anchors_on_latest_complete_label_date() -> None:
    module = load_script("50_calibrate_med_device_safe_core_thresholds.py", "med_safe_core_complete_window_test")
    config = {"calibration": {"validation_window_asofs": 2}}

    assert module.resolve_validation_window(
        config,
        panel_rows(),
        prefix="calibration.safe_core_threshold_sensitivity",
        horizons=[120],
    ) == ("2025-06-03", "2025-06-04")


def test_baseline_window_anchors_on_latest_complete_label_date() -> None:
    module = load_script("52_build_med_device_calibrated_baseline.py", "med_baseline_complete_window_test")

    assert module.resolve_validation_window(
        panel_rows(),
        validation_start_raw="auto",
        validation_end_raw="auto",
        horizons=[30, 60, 120],
        validation_window_asofs=2,
    ) == ("2025-06-03", "2025-06-04")

