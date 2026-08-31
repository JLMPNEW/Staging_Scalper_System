#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.technology_hardware.optuna_calibration import (  # noqa: E402
    validate_technology_hardware_optuna_calibration,
)

from technology.core.optuna_artifact_governance import validate_stage8_from_argv  # noqa: E402


if __name__ == "__main__":
    native_status = validate_technology_hardware_optuna_calibration()
    hardened_status = validate_stage8_from_argv("technology_hardware")
    raise SystemExit(max(native_status, hardened_status))
