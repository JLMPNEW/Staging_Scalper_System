#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from market_positioning.core import (  # noqa: E402
    DEFAULT_DB_PATH,
    DEFAULT_HISTORY_START_DATE,
    connect,
    ingest_13f_csv,
    init_db,
    parse_history_start,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Ingest normalized 13F holdings or aggregated institutional ownership snapshots into "
            "market_positioning.sqlite."
        )
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--history-start", type=str, default=DEFAULT_HISTORY_START_DATE.isoformat())
    parser.add_argument("--source", type=str, default="csv")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    with connect(args.db) as conn:
        init_db(conn)
        filing_count, row_count = ingest_13f_csv(
            conn,
            args.csv,
            history_start_date=parse_history_start(args.history_start),
            source=args.source,
        )
    print(f"Ingested 13F filings={filing_count} rows={row_count} db={args.db}")


if __name__ == "__main__":
    main()

