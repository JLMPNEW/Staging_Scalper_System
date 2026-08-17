#!/usr/bin/env python3
"""Refresh the latest reviewed-universe FINRA short-interest cache files."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from market_positioning.api_collectors import sync_finra_equity_short_interest_files  # noqa: E402
from market_positioning.core import connect, init_db  # noqa: E402


def _iso_date(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid ISO date: {raw}") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--universe-csv", type=Path, required=True)
    parser.add_argument(
        "--cache-root",
        type=Path,
        default=REPO_ROOT / "output" / "market_positioning_cache",
    )
    parser.add_argument("--as-of", type=_iso_date, required=True)
    parser.add_argument("--history-start", type=_iso_date, default=date(2021, 7, 1))
    parser.add_argument(
        "--max-files",
        type=int,
        default=2,
        help="Refresh only the latest N expected settlement files (default: 2).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.max_files < 1:
        raise ValueError("--max-files must be positive")
    if args.history_start > args.as_of:
        raise ValueError("--history-start cannot be after --as-of")
    db_path = args.db.expanduser().resolve()
    universe_csv = args.universe_csv.expanduser().resolve(strict=True)
    cache_root = args.cache_root.expanduser().resolve()
    with connect(db_path) as conn:
        init_db(conn)
        result = sync_finra_equity_short_interest_files(
            conn,
            tickers_csv=universe_csv,
            history_start_date=args.history_start,
            end_date=args.as_of,
            cache_dir=cache_root / "finra_short_interest",
            sleep_sec=0.0,
            max_files=args.max_files,
            cache_only=False,
        )
    payload = {
        "database": str(db_path),
        "universe_csv": str(universe_csv),
        "cache_root": str(cache_root),
        "as_of": args.as_of.isoformat(),
        "history_start": args.history_start.isoformat(),
        "max_files": args.max_files,
        "network_access": "permitted_for_validated_finra_refresh",
        "rows": result.rows,
        "message": result.message,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
