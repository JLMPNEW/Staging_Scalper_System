#!/usr/bin/env python3
from __future__ import annotations

import runpy
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
SHARED_SCRIPT = PACKAGE_ROOT / "scripts" / "10_validate_technology_sec_positioning_stages.py"
MODEL_FAMILY = "software_infrastructure"


if __name__ == "__main__":
    sys.argv = [
        str(SHARED_SCRIPT),
        "--model-family",
        MODEL_FAMILY,
        "--13f-exempt-tickers",
        "FATN",
        *sys.argv[1:],
    ]
    runpy.run_path(str(SHARED_SCRIPT), run_name="__main__")
