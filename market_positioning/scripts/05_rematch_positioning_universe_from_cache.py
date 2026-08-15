#!/usr/bin/env python3
"""Rematch one reviewed universe against sealed neutral 13F and FINRA caches."""

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

from market_positioning.api_collectors import (  # noqa: E402
    sync_finra_equity_short_interest_files,
    sync_sec_13f_data_sets,
)
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
    parser.add_argument(
        "--13f-start",
        dest="form13f_start",
        type=_iso_date,
        default=date(2019, 1, 2),
    )
    parser.add_argument("--short-start", type=_iso_date, default=date(2021, 7, 1))
    parser.add_argument("--skip-13f", action="store_true")
    parser.add_argument("--skip-short-interest", action="store_true")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip archives already represented in this target database.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    db_path = args.db.expanduser().resolve()
    universe_csv = args.universe_csv.expanduser().resolve()
    cache_root = args.cache_root.expanduser().resolve()
    if not universe_csv.is_file():
        raise FileNotFoundError(f"Reviewed positioning universe not found: {universe_csv}")
    if args.as_of < args.form13f_start or args.as_of < args.short_start:
        raise ValueError("Rematch source starts cannot be after --as-of")

    results: dict[str, object] = {
        "database": str(db_path),
        "universe_csv": str(universe_csv),
        "cache_root": str(cache_root),
        "as_of": args.as_of.isoformat(),
        "network_access": "forbidden",
    }
    with connect(db_path) as conn:
        init_db(conn)
        if not args.skip_short_interest:
            result = sync_finra_equity_short_interest_files(
                conn,
                tickers_csv=universe_csv,
                history_start_date=args.short_start,
                end_date=args.as_of,
                cache_dir=cache_root / "finra_short_interest",
                sleep_sec=0.0,
                cache_only=True,
            )
            results["short_interest"] = {
                "rows": result.rows,
                "message": result.message,
            }
        if not args.skip_13f:
            result = sync_sec_13f_data_sets(
                conn,
                tickers_csv=universe_csv,
                cusip_ticker_map_csv=universe_csv,
                history_start_date=args.form13f_start,
                end_date=args.as_of,
                cache_dir=cache_root / "sec_13f",
                sleep_sec=0.0,
                force_reprocess_archives=not args.resume,
                cache_only=True,
            )
            results["institutional_13f"] = {
                "rows": result.rows,
                "message": result.message,
            }

    print(json.dumps(results, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
