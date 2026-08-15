#!/usr/bin/env python3
"""Extract Consumer Defensive financial facts from exact sealed inline XBRL."""

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
from consumer_defensive.core.market_data import write_json
from consumer_defensive.core.script_runtime import iso_date, stage4_output_dir
from consumer_defensive.core.stage3_runtime import database_path
from consumer_defensive.core.stage4 import bootstrap_stage4, sync_inline_xbrl_fallback

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
    output_dir = stage4_output_dir(bundle, as_of=args.as_of, override=args.output_dir)
    timeout = float(cfg_get(bundle.payload, "runtime.sqlite_timeout_sec", 30.0))
    with connect(db_path, timeout_sec=timeout) as conn:
        bootstrap_stage4(conn, bundle)
        run_id = start_run(
            conn,
            run_type="consumer_defensive_stage4_inline_xbrl_fallback",
            input_path=bundle.path,
        )
        try:
            result = sync_inline_xbrl_fallback(conn, bundle, as_of=args.as_of)
            unresolved = int(result["insufficient_mapped_facts"])
            finish_run(
                conn,
                run_id=run_id,
                status="partial" if unresolved else "success",
                row_count=int(result["consolidated_facts"]),
                message=json.dumps(result, sort_keys=True),
            )
        except BaseException as exc:
            finish_run(
                conn,
                run_id=run_id,
                status="failed",
                message=f"{type(exc).__name__}: {exc}",
            )
            raise
    payload = {"database": str(db_path), **result}
    report_path = output_dir / "inline_xbrl_fallback_sync.json"
    write_json(report_path, payload)
    print(json.dumps({**payload, "report": str(report_path)}, indent=2, sort_keys=True))
    return 1 if unresolved else 0


if __name__ == "__main__":
    raise SystemExit(main())
