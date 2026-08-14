#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from industrials.machinery.scripts._shared import run_shared


if __name__ == "__main__":
    run_shared("07c_recover_industrials_financial_lineage.py")
