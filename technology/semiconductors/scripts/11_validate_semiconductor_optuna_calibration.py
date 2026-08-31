#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.semiconductors.optuna_calibration import validate_semiconductor_optuna_calibration  # noqa: E402

from technology.core.optuna_artifact_governance import validate_stage8_from_argv  # noqa: E402


if __name__ == "__main__":
    native_status = validate_semiconductor_optuna_calibration()
    hardened_status = validate_stage8_from_argv("semiconductors")
    raise SystemExit(max(native_status, hardened_status))
