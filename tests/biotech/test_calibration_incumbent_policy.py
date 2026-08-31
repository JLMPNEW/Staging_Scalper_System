from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[2]


def load_calibration_script() -> ModuleType:
    path = ROOT / "biotech_index" / "scripts" / "28_calibrate_biotech_opportunity.py"
    spec = importlib.util.spec_from_file_location("test_calibration_incumbent_policy_module", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_custom_policy_grid_always_contains_configured_production_incumbent() -> None:
    module = load_calibration_script()
    config = {
        "biotech_scoring": {
            "production_baseline": {"selection_policy": "core_structural_veto"},
        },
        "calibration": {
            "tier1": {
                "selection_policies": [
                    {"name": "challenger", "hard_veto": False},
                ]
            }
        },
    }
    policies = module.generate_selection_policies(config)
    names = [policy.policy_name for policy in policies]
    assert names == ["raw_legacy_score", "challenger", "core_structural_veto"]


def test_missing_unsupported_incumbent_policy_fails_closed() -> None:
    module = load_calibration_script()
    config = {
        "biotech_scoring": {
            "production_baseline": {"selection_policy": "undefined_live_policy"},
        },
        "calibration": {
            "tier1": {
                "selection_policies": [
                    {"name": "challenger", "hard_veto": False},
                ]
            }
        },
    }
    with pytest.raises(ValueError, match="omits the configured production incumbent"):
        module.generate_selection_policies(config)
