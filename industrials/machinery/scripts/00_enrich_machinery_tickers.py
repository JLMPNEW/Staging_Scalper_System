#!/usr/bin/env python3
from __future__ import annotations

import runpy
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
ENRICHER = PROJECT_ROOT / "ticker_mapping" / "enrich_technology_tickers.py"
SEED = PACKAGE_ROOT / "system_csvs" / "machinery_tickers.csv"
AUDIT = PROJECT_ROOT / "output" / "industrials" / "machinery" / "universe" / "machinery_tickers_enrichment_audit.csv"


if __name__ == "__main__":
    args = list(sys.argv[1:])
    if "--input" not in args:
        args = ["--input", str(SEED), *args]
    defaults = [
        "--audit-output",
        str(AUDIT),
        "--cache-prefix",
        "machinery",
        "--default-sector",
        "Industrials",
        "--default-subsector",
        "Machinery",
        "--default-country",
        "United States",
        "--default-currency",
        "USD",
        "--default-security-type",
        "Common Stock",
        "--default-listing-status",
        "active",
        "--default-primary-listing",
        "TRUE",
    ]
    sys.argv = [str(ENRICHER), *defaults, *args]
    runpy.run_path(str(ENRICHER), run_name="__main__")
