#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import load_yaml  # noqa: E402
from industrials.core.csv_utils import read_csv_flexible  # noqa: E402
from industrials.core.db import connect  # noqa: E402
from industrials.core.family_universe import validate_database_contract  # noqa: E402
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
    resolve_foundation,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate transportation universe and PIT membership.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    paths = resolve_foundation(args.config, args.db)
    policy = load_yaml(paths.policy_path)
    expected_historical = len(read_csv_flexible(paths.historical_path))
    with connect(paths.db_path, timeout_sec=paths.timeout_sec) as conn:
        errors = validate_database_contract(
            conn,
            model_family=MODEL_FAMILY,
            active_source_id=paths.seed_source_id,
            historical_source_id=paths.historical_source_id,
            delisted_source_id=paths.delisted_source_id,
            expected_active=int(policy["expected_ticker_count"]),
            expected_historical=expected_historical,
            expected_delisted=int(policy["expected_delisted_count"]),
        )
    summary = {
        "status": "PASS" if not errors else "FAIL",
        "database": str(paths.db_path),
        "expected_active": int(policy["expected_ticker_count"]),
        "expected_historical": expected_historical,
        "expected_delisted": int(policy["expected_delisted_count"]),
        "errors": errors,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
