#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.semiconductors.optuna_calibration import run_semiconductor_walk_forward_calibration  # noqa: E402

from technology.core.optuna_artifact_governance import run_walk_forward_with_governance  # noqa: E402


if __name__ == "__main__":
    run_walk_forward_with_governance(run_semiconductor_walk_forward_calibration, "semiconductors")
