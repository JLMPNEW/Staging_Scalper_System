#!/usr/bin/env python3
from __future__ import annotations

import runpy
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
SHARED_SCRIPT = PACKAGE_ROOT / "scripts" / "08_build_technology_financial_features.py"
MODEL_FAMILY = "software_infrastructure"
OUTPUT_CSV = PROJECT_ROOT / "output" / "technology_reports" / "software_infrastructure" / "sec_fundamentals" / "sec_financial_feature_coverage.csv"


if __name__ == "__main__":
    sys.argv = [
        str(SHARED_SCRIPT),
        "--model-family",
        MODEL_FAMILY,
        "--output-csv",
        str(OUTPUT_CSV),
        *sys.argv[1:],
    ]
    runpy.run_path(str(SHARED_SCRIPT), run_name="__main__")
