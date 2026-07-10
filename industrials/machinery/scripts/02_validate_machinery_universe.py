#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.db import connect  # noqa: E402
from industrials.machinery.contracts import read_csv_rows  # noqa: E402
from industrials.machinery.universe import validate_database_contract  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate the loaded machinery universe and PIT membership.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    policy = load_yaml(resolve_path(cfg_get(config, "industrials_universe.policy_path"), base_dir=base_dir))
    membership_path = resolve_path(cfg_get(config, "industrials_universe.historical_membership_csv"), base_dir=base_dir)
    expected_historical = len(read_csv_rows(membership_path))
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 120.0))) as conn:
        errors = validate_database_contract(
            conn,
            active_source_id=str(cfg_get(config, "industrials_universe.seed_source_id")),
            historical_source_id=str(cfg_get(config, "industrials_universe.historical_membership_source_id")),
            delisted_source_id=str(cfg_get(config, "industrials_universe.delisted_source_id")),
            expected_active=int(policy.get("expected_ticker_count") or 0),
            expected_historical=expected_historical,
            expected_delisted=int(policy.get("expected_delisted_count") or 0),
        )
    summary = {
        "status": "PASS" if not errors else "FAIL",
        "database": str(db_path),
        "expected_active": int(policy.get("expected_ticker_count") or 0),
        "expected_historical": expected_historical,
        "expected_delisted": int(policy.get("expected_delisted_count") or 0),
        "errors": errors,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
