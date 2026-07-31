from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def load_score_module() -> Any:
    path = ROOT / "med_devices/scripts/13_build_med_device_daily_scores.py"
    spec = importlib.util.spec_from_file_location("phase1_med_lock_scores", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_phase1_lock_overrides_accidental_replace_configuration(tmp_path: Path) -> None:
    module = load_score_module()
    config = {
        "scoring": {
            "ic_tilted_composite": {
                "enabled": True,
                "mode": "replace_raw",
                "allow_production_replace": True,
                "phase1_safety_lock": True,
            }
        }
    }

    policy = module.load_ic_tilted_composite_policy(config, base_dir=tmp_path)

    assert policy["phase1_safety_lock"] is True
    assert policy["mode"] == "shadow"
    assert policy["allow_replace"] is False
