from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from industrials.core.pit_lineage_canary import (  # noqa: E402
    representative_dates,
    run_pit_lineage_canary,
)


DEFAULT_DB = Path("C:/Users/josel/Documents/STAGING/DB/industrials.sqlite")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run a read-only point-in-time financial-lineage canary without "
            "publishing or modifying historical production artifacts."
        )
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "output" / "industrials" / "pit_lineage_canary",
    )
    parser.add_argument(
        "--families",
        default="defense,machinery",
        help="Comma-separated model families.",
    )
    parser.add_argument("--as-of", default="2026-08-14")
    parser.add_argument("--start-year", type=int, default=2019)
    parser.add_argument(
        "--dates",
        default="",
        help="Optional comma-separated PIT dates; overrides representative dates.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    dates = (
        [value.strip() for value in args.dates.split(",") if value.strip()]
        if args.dates.strip()
        else representative_dates(start_year=args.start_year, asof=args.as_of)
    )
    manifest = run_pit_lineage_canary(
        db_path=args.db,
        output_dir=args.output_dir,
        model_families=[value.strip() for value in args.families.split(",") if value.strip()],
        dates=dates,
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0 if manifest["acceptance"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
