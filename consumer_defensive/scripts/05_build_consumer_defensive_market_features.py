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
from consumer_defensive.core.market_data import build_market_features, select_price_sources, write_csv, write_json
from consumer_defensive.core.script_runtime import iso_date
from consumer_defensive.core.stage3_runtime import DEFAULT_MARKET_POLICY, assert_stage2_ready, bootstrap_stage3, database_path, load_stage3_policy, stage3_output_dir


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build Consumer Defensive Stage 3 market and technical features.")
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
        readiness = assert_stage2_ready(conn, bundle, as_of=args.as_of)
        run_id = start_run(conn, run_type="consumer_defensive_stage3_market_features", input_path=args.market_policy)
        try:
            audit = select_price_sources(conn, policy, as_of=args.as_of)
            write_csv(output_dir / "market_data_coverage_audit.csv", audit["rows"])
            write_json(
                output_dir / "market_data_coverage_audit.json",
                {key: value for key, value in audit.items() if key != "rows"},
            )
            if audit["status"] != "PASS":
                raise RuntimeError("Market features blocked because the provider coverage audit failed: " + "; ".join(audit["errors"]))
            result = build_market_features(conn, policy, as_of=args.as_of)
            if not result["eligible_tickers"] or result["features_written"] != result["eligible_tickers"]:
                raise RuntimeError(f"Market feature build was incomplete: {result}")
            finish_run(conn, run_id=run_id, status="success", row_count=int(result["features_written"]), message=json.dumps(result, sort_keys=True))
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", message=f"{type(exc).__name__}: {exc}")
            raise
    summary = {"database": str(db_path), "stage2": readiness, "coverage_audit_status": audit["status"], **result}
    write_json(output_dir / "market_feature_build_summary.json", summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
