#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.transportation.scripts._shared import run_market_shared  # noqa: E402


if __name__ == "__main__":
    run_market_shared("04_audit_industrials_market_data_policy.py")
