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

from consumer_defensive.core.config import cfg_get, load_config, resolve_path
from consumer_defensive.core.db import connect, finish_run, init_db, start_run
from consumer_defensive.core.market_data import write_json
from consumer_defensive.core.script_runtime import iso_date
from consumer_defensive.core.universe import ensure_stage2_schema, load_policy
from consumer_defensive.core.universe_validation import validate_stage2


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_POLICY = PACKAGE_ROOT / "data" / "consumer_defensive_universe_policy.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate Consumer Defensive Stage 2 universe contracts.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--universe-csv", type=Path, default=None)
    parser.add_argument("--identity-only", action="store_true")
    parser.add_argument("--as-of", type=iso_date, default=date.today().isoformat())
    parser.add_argument("--report", type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    bundle = load_config(args.config)
    policy = load_policy(args.policy)
    db_path = (
        args.db.expanduser().resolve()
        if args.db
        else resolve_path(cfg_get(bundle.payload, "paths.database_path"), base_dir=bundle.base_dir)
    )
    timeout = float(cfg_get(bundle.payload, "runtime.sqlite_timeout_sec", 30.0))
    with connect(db_path, timeout_sec=timeout) as conn:
        init_db(conn)
        ensure_stage2_schema(conn)
        run_id = start_run(conn, run_type="consumer_defensive_stage2_validation", input_path=args.policy)
        try:
            result = validate_stage2(
                conn,
                policy,
                current_csv=args.universe_csv,
                require_pit_membership=not args.identity_only,
                as_of=args.as_of,
            )
            finish_run(
                conn,
                run_id=run_id,
                status="success" if result["status"] == "PASS" else "failed",
                row_count=int(result["current_rows"]),
                message=json.dumps({key: value for key, value in result.items() if key != "checks"}, sort_keys=True),
            )
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", message=f"{type(exc).__name__}: {exc}")
            raise
    report = (
        args.report.expanduser().resolve()
        if args.report
        else (
            resolve_path(cfg_get(bundle.payload, "paths.output_dir"), base_dir=bundle.base_dir)
            / "stage2"
            / "universe"
            / args.as_of
            / "stage2_validation.json"
        )
    )
    write_json(report, result)
    print(json.dumps({"database": str(db_path), "report": str(report), **result}, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
