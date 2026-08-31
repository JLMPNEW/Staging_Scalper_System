from __future__ import annotations

import csv
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
OVERRIDES_PATH = PROJECT_ROOT / "portfolio_layer" / "data" / "canonical_sector_overrides.csv"


def test_reviewed_cross_sector_tickers_are_canonical_med_devices() -> None:
    with OVERRIDES_PATH.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = {
            str(row.get("ticker") or "").strip().upper(): str(
                row.get("canonical_pipeline") or ""
            ).strip()
            for row in csv.DictReader(handle)
        }

    expected = {
        "BBNX",
        "BSX",
        "BVS",
        "CBLL",
        "CDNA",
        "CERS",
        "CNMD",
        "EW",
        "IART",
        "IQV",
        "MDT",
        "MYGN",
        "NEO",
        "PODD",
        "PRCT",
        "PSNL",
        "RCEL",
        "STE",
        "TMDX",
        "TMO",
        "VCYT",
        "ZBH",
    }
    assert {ticker for ticker in expected if rows.get(ticker) == "med_devices"} == expected
