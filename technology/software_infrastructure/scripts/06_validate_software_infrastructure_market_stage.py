#!/usr/bin/env python3
from __future__ import annotations

import runpy
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
SHARED_SCRIPT = PACKAGE_ROOT / "scripts" / "06_validate_technology_market_stage.py"
MODEL_FAMILY = "software_infrastructure"
BENCHMARKS = "IGV,SKYY,WCLD,HACK,CIBR,QQQ,SPY"
POLICY_PATH = PACKAGE_ROOT / "software_infrastructure" / "data" / "software_infrastructure_universe_policy.yaml"


if __name__ == "__main__":
    sys.argv = [
        str(SHARED_SCRIPT),
        "--model-family",
        MODEL_FAMILY,
        "--benchmark-tickers",
        BENCHMARKS,
        "--policy",
        str(POLICY_PATH),
        *sys.argv[1:],
    ]
    runpy.run_path(str(SHARED_SCRIPT), run_name="__main__")
