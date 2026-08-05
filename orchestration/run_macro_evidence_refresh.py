#!/usr/bin/env python3
"""Monthly macro evidence refresh: industry ablation + stock-level shadow backtest.

Runs both evidence harnesses against the current serving data so the
macro-vs-baseline record accumulates without manual intervention. Each step
fails closed; a step failure exits nonzero so the scheduled task reports it.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MACRO_ROOT = REPO_ROOT / "portfolio_layer" / "MacroLayer"
CONFIG = MACRO_ROOT / "config_macro_raw.yaml"
STEPS = (
    "run_macro_industry_ablation.py",
    "run_macro_shadow_backtest.py",
)


def main() -> int:
    for script in STEPS:
        cmd = [sys.executable, str(MACRO_ROOT / script), "--config", str(CONFIG)]
        print(f"[evidence-refresh] running {script}", flush=True)
        result = subprocess.run(cmd, cwd=str(MACRO_ROOT.parent), check=False)
        if result.returncode != 0:
            print(f"[evidence-refresh] FAILED {script} exit={result.returncode}", flush=True)
            return result.returncode
    print("[evidence-refresh] complete", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
