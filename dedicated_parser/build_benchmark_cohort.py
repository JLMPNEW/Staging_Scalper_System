from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.adapters import load_registry  # noqa: E402
from dedicated_parser.benchmark import (  # noqa: E402
    rank_missing_metric_tickers,
    write_benchmark_cohort,
)
from dedicated_parser.storage import connect_database  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build a deterministic benchmark cohort from the tickers with "
            "the most unresolved parser-supported metrics."
        )
    )
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--adapter", required=True)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--minimum-missing", type=int, default=1)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-csv", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    registry = load_registry(args.adapter)
    with connect_database(args.db, readonly=True) as conn:
        payload = rank_missing_metric_tickers(
            conn,
            registry=registry,
            adapter_path=args.adapter,
            asof_date=args.asof,
            limit=args.limit,
            minimum_missing=args.minimum_missing,
        )
    write_benchmark_cohort(
        payload=payload,
        json_path=args.output_json,
        csv_path=args.output_csv,
    )
    print(
        json.dumps(
            {
                "cohort_id": payload["cohort_id"],
                "cohort_size": payload["cohort_size"],
                "selection_sha256": payload["selection_sha256"],
                "output_json": str(args.output_json),
                "output_csv": str(args.output_csv),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
