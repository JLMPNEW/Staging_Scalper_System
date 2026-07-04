#!/usr/bin/env python3
from __future__ import annotations

import runpy
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
SHARED_SCRIPT = PACKAGE_ROOT / "scripts" / "05_build_industrials_market_features.py"
MODEL_FAMILY = "defense"


if __name__ == "__main__":
    # Pin only the family; benchmarks and output CSV come from config
    # (industrials_universe.benchmark_tickers / benchmark_ticker and
    # market_feature_build.output_csv) so config edits are honored when the
    # pipeline is driven through this wrapper.
    sys.argv = [
        str(SHARED_SCRIPT),
        "--model-family",
        MODEL_FAMILY,
        *sys.argv[1:],
    ]
    runpy.run_path(str(SHARED_SCRIPT), run_name="__main__")
