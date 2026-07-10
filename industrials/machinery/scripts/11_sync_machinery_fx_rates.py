#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from industrials.machinery.scripts._shared import run_shared


if __name__ == "__main__":
    run_shared("11_sync_industrials_yahoo_fx_rates.py", pin_model_family=False)
