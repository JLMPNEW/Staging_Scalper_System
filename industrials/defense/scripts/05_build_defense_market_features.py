#!/usr/bin/env python3
from __future__ import annotations

import runpy
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
SHARED_SCRIPT = PACKAGE_ROOT / "scripts" / "05_build_industrials_market_features.py"
MODEL_FAMILY = "defense"
BENCHMARKS = "XAR,ITA,SPY"
PRIMARY_BENCHMARK = "XAR"
OUTPUT_CSV = PROJECT_ROOT / "output" / "industrials" / "defense" / "stage3" / "market_feature_coverage.csv"


if __name__ == "__main__":
    sys.argv = [
        str(SHARED_SCRIPT),
        "--model-family",
        MODEL_FAMILY,
        "--benchmark-tickers",
        BENCHMARKS,
        "--primary-benchmark",
        PRIMARY_BENCHMARK,
        "--output-csv",
        str(OUTPUT_CSV),
        *sys.argv[1:],
    ]
    runpy.run_path(str(SHARED_SCRIPT), run_name="__main__")
