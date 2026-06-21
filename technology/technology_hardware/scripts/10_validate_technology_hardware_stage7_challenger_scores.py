#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.technology_hardware.calibrated_scoring import (  # noqa: E402
    validate_technology_hardware_stage7_challenger_scores,
)


if __name__ == "__main__":
    raise SystemExit(validate_technology_hardware_stage7_challenger_scores())
