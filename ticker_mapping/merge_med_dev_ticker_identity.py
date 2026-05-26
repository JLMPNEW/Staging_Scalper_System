#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


LOGGER = logging.getLogger("merge_med_dev_ticker_identity")
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_SOURCE = SCRIPT_DIR / "med_dev_tickers.csv"
DEFAULT_ENRICHED = SCRIPT_DIR / "med_dev_tickers_enriched.csv"
DEFAULT_OUTPUT = SCRIPT_DIR / "med_dev_tickers.csv"

SOURCE_COLUMNS = ["Name", "Company_Name", "Industry", "Index"]
IDENTITY_COLUMNS = [
    "CIK",
    "Exchange",
    "SecurityType",
    "ListingStatus",
    "IsPrimaryListing",
    "Country",
    "Currency",
    "CompanyName",
    "MatchedTicker",
    "MatchType",
    "Source",
    "IdentityDataSources",
    "MissingIdentityFields",
    "ManualInclude",
    "ManualExclude",
    "ManualReview",
    "Notes",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Merge CIK/listing identity enrichment back into med_dev_tickers.csv."
    )
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--enriched", type=Path, default=DEFAULT_ENRICHED)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--no-backup", action="store_true", help="Do not back up the output file before overwriting it.")
    return parser.parse_args()


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")


def read_csv_flexible(path: Path) -> pd.DataFrame:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return pd.read_csv(path, dtype=str, encoding=encoding).fillna("")
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
        except pd.errors.EmptyDataError:
            raise ValueError(f"Input CSV is empty: {path}")
    raise ValueError(f"Could not decode CSV {path}: {last_error}")


def normalize_ticker(raw: Any) -> str:
    return str(raw or "").strip().upper().replace(".", "-")


def normalize_header(raw: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(raw or "").strip().lower())


def require_columns(df: pd.DataFrame, columns: list[str], *, path: Path) -> None:
    missing = [column for column in columns if column not in df.columns]
    if missing:
        raise ValueError(f"{path} is missing required column(s): {missing}")


def clean_source_rows(df: pd.DataFrame) -> tuple[pd.DataFrame, int, int]:
    require_columns(df, SOURCE_COLUMNS, path=DEFAULT_SOURCE)
    out = df[SOURCE_COLUMNS].copy()
    out["Name"] = out["Name"].map(normalize_ticker)
    out = out[out["Name"] != ""].copy()

    repeated_header = (
        out["Company_Name"].astype(str).str.strip().str.lower().eq("name")
        & out["Industry"].astype(str).str.strip().str.lower().eq("sector")
        & out["Index"].astype(str).str.strip().str.lower().eq("industry")
    )
    bad_header_rows = int(repeated_header.sum())
    if bad_header_rows:
        out = out[~repeated_header].copy()

    duplicate_count = int(out.duplicated(subset=["Name"], keep="first").sum())
    out = out.drop_duplicates(subset=["Name"], keep="first").reset_index(drop=True)
    return out, bad_header_rows, duplicate_count


def merge_identity(source: pd.DataFrame, enriched: pd.DataFrame) -> pd.DataFrame:
    if "Ticker" not in enriched.columns:
        raise ValueError("Enriched CSV must contain a Ticker column.")
    enriched = enriched.copy()
    enriched["Ticker"] = enriched["Ticker"].map(normalize_ticker)
    for column in IDENTITY_COLUMNS:
        if column not in enriched.columns:
            enriched[column] = ""
    enriched = enriched.drop_duplicates(subset=["Ticker"], keep="first")
    identity = enriched[["Ticker", *IDENTITY_COLUMNS]]

    merged = source.merge(identity, left_on="Name", right_on="Ticker", how="left")
    merged = merged.drop(columns=["Ticker"])
    for column in IDENTITY_COLUMNS:
        if column not in merged.columns:
            merged[column] = ""
        merged[column] = merged[column].fillna("")
    missing_company_name = merged["CompanyName"].astype(str).str.strip().eq("")
    merged.loc[missing_company_name, "CompanyName"] = merged.loc[missing_company_name, "Company_Name"]
    return merged[[*SOURCE_COLUMNS, *IDENTITY_COLUMNS]]


def backup_output(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = path.with_name(f"{path.stem}.pre_identity_enrichment_{stamp}{path.suffix}")
    shutil.copy2(path, backup_path)
    return backup_path


def main() -> None:
    configure_logging()
    args = parse_args()
    source_path = args.source.expanduser().resolve()
    enriched_path = args.enriched.expanduser().resolve()
    output_path = args.output.expanduser().resolve()

    source = read_csv_flexible(source_path)
    enriched = read_csv_flexible(enriched_path)
    clean_source, bad_header_rows, duplicate_rows = clean_source_rows(source)
    merged = merge_identity(clean_source, enriched)

    backup_path = None if args.no_backup else backup_output(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    merged.to_csv(output_path, index=False, encoding="utf-8")

    LOGGER.info("Wrote cleaned med-device tickers: %s", output_path)
    if backup_path is not None:
        LOGGER.info("Backup saved: %s", backup_path)
    LOGGER.info(
        "Rows=%d dropped_bad_header_rows=%d dropped_duplicate_rows=%d cik_populated=%d listing_status_populated=%d",
        len(merged),
        bad_header_rows,
        duplicate_rows,
        int(merged["CIK"].astype(str).str.strip().ne("").sum()),
        int(merged["ListingStatus"].astype(str).str.strip().ne("").sum()),
    )


if __name__ == "__main__":
    main()
