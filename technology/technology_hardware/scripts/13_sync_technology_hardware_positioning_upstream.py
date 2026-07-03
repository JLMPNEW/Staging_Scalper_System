#!/usr/bin/env python3
from __future__ import annotations

import csv
import runpy
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
SHARED_SCRIPT = PACKAGE_ROOT / "scripts" / "13_sync_technology_positioning_upstream.py"
TICKERS_CSV = PROJECT_ROOT / "ticker_mapping" / "technology_hardware_cleaned.csv"
HISTORICAL_CSV = PACKAGE_ROOT / "technology_hardware" / "data" / "technology_hardware_historical_membership.csv"
POSITIONING_UNIVERSE_CSV = PROJECT_ROOT / "output" / "technology_cache" / "positioning" / "technology_hardware_positioning_universe.csv"
POSITIONING_UNIVERSE_AUDIT_CSV = (
    PROJECT_ROOT / "output" / "technology_cache" / "positioning" / "technology_hardware_positioning_universe_skipped.csv"
)


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(
            f"Required positioning-universe input CSV is missing: {path}. "
            "A silently empty universe would publish an empty positioning feed."
        )
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [{str(key): str(value or "").strip() for key, value in row.items()} for row in csv.DictReader(handle)]


def build_positioning_universe() -> Path:
    historical_rows = read_rows(HISTORICAL_CSV)
    rows_by_ticker: dict[str, dict[str, str]] = {}
    skipped: list[dict[str, str]] = []
    for row in read_rows(TICKERS_CSV):
        ticker = str(row.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        out = {str(key): str(value or "") for key, value in row.items()}
        out["ticker"] = ticker
        out["internal_ticker"] = ticker
        out["exchange_ticker"] = ticker
        out["industry"] = str(row.get("industry") or "Technology Hardware")
        out["source_membership"] = "current_source_of_truth"
        rows_by_ticker[ticker] = out

    source_counts: dict[str, int] = {}
    for row in historical_rows:
        exchange_ticker = str(row.get("exchange_ticker") or row.get("internal_ticker") or "").strip().upper()
        if exchange_ticker:
            source_counts[exchange_ticker] = source_counts.get(exchange_ticker, 0) + 1

    for row in historical_rows:
        internal_ticker = str(row.get("internal_ticker") or row.get("ticker") or "").strip().upper()
        exchange_ticker = str(row.get("exchange_ticker") or internal_ticker).strip().upper()
        if not internal_ticker or not exchange_ticker:
            continue
        if exchange_ticker in rows_by_ticker:
            skipped.append(
                {
                    "internal_ticker": internal_ticker,
                    "exchange_ticker": exchange_ticker,
                    "reason": "exchange_ticker_conflicts_with_current_member",
                }
            )
            continue
        if source_counts.get(exchange_ticker, 0) > 1:
            skipped.append(
                {
                    "internal_ticker": internal_ticker,
                    "exchange_ticker": exchange_ticker,
                    "reason": "exchange_ticker_maps_to_multiple_historical_intervals",
                }
            )
            continue
        rows_by_ticker[exchange_ticker] = {
            "ticker": exchange_ticker,
            "internal_ticker": internal_ticker,
            "exchange_ticker": exchange_ticker,
            "company_name": str(row.get("company_name") or ""),
            "cik": str(row.get("cik") or ""),
            "cusip": str(row.get("cusip") or ""),
            "exchange": str(row.get("exchange") or ""),
            "sector": "Technology",
            "industry": "Technology Hardware",
            "subsector": str(row.get("calibration_cohort") or "Technology Hardware"),
            "cohort": str(row.get("calibration_cohort") or ""),
            "country": str(row.get("country") or ""),
            "currency": str(row.get("currency") or "USD"),
            "security_type": str(row.get("security_type") or "Common Stock"),
            "listing_status": str(row.get("membership_status") or "historical"),
            "is_primary_listing": "FALSE",
            "membership_start_date": str(row.get("start_date") or ""),
            "membership_end_date": str(row.get("end_date") or ""),
            "successor_ticker": str(row.get("successor_ticker") or ""),
            "source_membership": "historical_point_in_time",
        }

    POSITIONING_UNIVERSE_CSV.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "ticker",
        "internal_ticker",
        "exchange_ticker",
        "company_name",
        "cik",
        "cusip",
        "exchange",
        "sector",
        "industry",
        "subsector",
        "cohort",
        "country",
        "currency",
        "security_type",
        "listing_status",
        "is_primary_listing",
        "membership_start_date",
        "membership_end_date",
        "successor_ticker",
        "source_membership",
    ]
    with POSITIONING_UNIVERSE_CSV.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for ticker in sorted(rows_by_ticker):
            writer.writerow(rows_by_ticker[ticker])
    with POSITIONING_UNIVERSE_AUDIT_CSV.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["internal_ticker", "exchange_ticker", "reason"], lineterminator="\n")
        writer.writeheader()
        writer.writerows(skipped)
    return POSITIONING_UNIVERSE_CSV


if __name__ == "__main__":
    positioning_universe = build_positioning_universe()
    sys.argv = [
        str(SHARED_SCRIPT),
        "--tickers-csv",
        str(positioning_universe),
        "--skip-technology-import",
        *sys.argv[1:],
    ]
    runpy.run_path(str(SHARED_SCRIPT), run_name="__main__")
