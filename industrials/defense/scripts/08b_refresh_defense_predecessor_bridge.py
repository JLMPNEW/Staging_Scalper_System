#!/usr/bin/env python3
from __future__ import annotations

import runpy
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
SHARED_SCRIPT = PACKAGE_ROOT / "scripts" / "08b_refresh_industrials_predecessor_bridge.py"


if __name__ == "__main__":
    sys.argv = [str(SHARED_SCRIPT), "--model-family", "defense", *sys.argv[1:]]
    runpy.run_path(str(SHARED_SCRIPT), run_name="__main__")
