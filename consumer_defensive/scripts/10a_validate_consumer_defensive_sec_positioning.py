#!/usr/bin/env python3
# ruff: noqa: E402
"""Validate the Consumer Defensive Stage 5 ownership and positioning gate."""

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

from consumer_defensive.core.config import cfg_get, load_config, resolve_path
from consumer_defensive.core.db import connect, finish_run, start_run
from consumer_defensive.core.market_data import write_json
from consumer_defensive.core.script_runtime import iso_date
from consumer_defensive.core.stage3_runtime import database_path
from consumer_defensive.core.stage5 import validate_stage5

DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--as-of", type=iso_date, default=date.today().isoformat())
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    bundle = load_config(args.config)
    db_path = database_path(bundle, args.db)
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else resolve_path(cfg_get(bundle.payload, "paths.output_dir"), base_dir=bundle.base_dir) / "stage5" / args.as_of
    with connect(db_path, timeout_sec=float(cfg_get(bundle.payload, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        run_id = start_run(conn, run_type="consumer_defensive_stage5_validation", input_path=bundle.path)
        try:
            result = validate_stage5(conn, bundle, as_of=args.as_of)
            finish_run(conn, run_id=run_id, status="success" if result["status"] == "PASS" else "failed", row_count=len(result["checks"]), message=json.dumps({key: value for key, value in result.items() if key != "checks"}, sort_keys=True))
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", message=f"{type(exc).__name__}: {exc}")
            raise
    payload = {"database": str(db_path), **result}
    report = output_dir / "stage5_validation.json"
    write_json(report, payload)
    print(json.dumps({**payload, "report": str(report)}, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
