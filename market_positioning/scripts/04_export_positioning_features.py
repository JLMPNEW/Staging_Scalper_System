#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from market_positioning.core import DEFAULT_DB_PATH, connect, export_positioning_features, init_db, parse_date  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export point-in-time short-interest and 13F ownership feature CSVs from market_positioning.sqlite."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--asof", type=str, required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("output/biotech_index_reports"))
    parser.add_argument("--tickers-csv", type=Path, default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    asof = parse_date(args.asof)
    if asof is None:
        raise ValueError(f"Invalid --asof date: {args.asof!r}")
    with connect(args.db) as conn:
        init_db(conn)
        short_path, institutional_path, short_count, institutional_count = export_positioning_features(
            conn,
            asof_date=asof,
            output_dir=args.output_dir,
            tickers_csv=args.tickers_csv,
        )
    print(
        "Exported positioning features "
        f"short_rows={short_count} institutional_rows={institutional_count} "
        f"short_csv={short_path} institutional_csv={institutional_path}"
    )


if __name__ == "__main__":
    main()

