#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from industrials.machinery.scripts._shared import run_shared


if __name__ == "__main__":
    run_shared("04_audit_industrials_market_data_policy.py")
