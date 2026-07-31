#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.transportation.positioning_integration import (  # noqa: E402
    run_positioning_shared,
)


if __name__ == "__main__":
    run_positioning_shared("09_import_industrials_positioning.py")
