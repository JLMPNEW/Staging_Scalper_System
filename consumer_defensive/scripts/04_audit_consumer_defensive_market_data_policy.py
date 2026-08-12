#!/usr/bin/env python3
from __future__ import annotations

# ruff: noqa: E402

import argparse
import json
import sys
from datetime import date
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from consumer_defensive.core.config import cfg_get, load_config
from consumer_defensive.core.db import connect, finish_run, start_run
from consumer_defensive.core.market_data import select_price_sources, write_csv, write_json
from consumer_defensive.core.script_runtime import iso_date
from consumer_defensive.core.stage3_runtime import DEFAULT_MARKET_POLICY, assert_stage2_ready, bootstrap_stage3, database_path, load_stage3_policy, stage3_output_dir


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit Stage 3 coverage and select one complete provider per ticker.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--market-policy", type=Path, default=DEFAULT_MARKET_POLICY)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--as-of", type=iso_date, default=date.today().isoformat())
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    bundle = load_config(args.config)
    policy = load_stage3_policy(args.market_policy)
    db_path = database_path(bundle, args.db)
    output_dir = stage3_output_dir(bundle, policy, as_of=args.as_of, override=args.output_dir)
    timeout = float(cfg_get(bundle.payload, "runtime.sqlite_timeout_sec", 30.0))
    with connect(db_path, timeout_sec=timeout) as conn:
        bootstrap_stage3(conn, bundle)
        readiness = assert_stage2_ready(conn, bundle)
        run_id = start_run(conn, run_type="consumer_defensive_stage3_market_audit", input_path=args.market_policy)
        try:
            result = select_price_sources(conn, policy, as_of=args.as_of)
            finish_run(conn, run_id=run_id, status="success" if result["status"] == "PASS" else "failed", row_count=len(result["rows"]), message=json.dumps({key: value for key, value in result.items() if key != "rows"}, sort_keys=True))
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", message=f"{type(exc).__name__}: {exc}")
            raise
    output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(output_dir / "market_data_coverage_audit.csv", result["rows"])
    summary = {"database": str(db_path), "stage2": readiness, **{key: value for key, value in result.items() if key != "rows"}}
    write_json(output_dir / "market_data_coverage_audit.json", summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
