#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.transportation.scripts._shared import run_financial_shared  # noqa: E402


if __name__ == "__main__":
    run_financial_shared("08_validate_industrials_financial_stage.py")
