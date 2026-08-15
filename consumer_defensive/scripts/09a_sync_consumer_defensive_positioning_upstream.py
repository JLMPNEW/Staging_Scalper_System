#!/usr/bin/env python3
# ruff: noqa: E402
"""Build the Consumer Defensive upstream universe and audit the read-only positioning store."""

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
from consumer_defensive.core.db import connect
from consumer_defensive.core.market_data import write_json
from consumer_defensive.core.script_runtime import iso_date
from consumer_defensive.core.stage3_runtime import database_path
from consumer_defensive.core.stage5 import audit_upstream_positioning, build_positioning_universe_rows, bootstrap_stage5, write_positioning_universe_csv

DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--as-of", type=iso_date, default=date.today().isoformat())
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument(
        "--universe-csv",
        type=Path,
        default=None,
        help="Override the neutral upstream handoff CSV path.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    bundle = load_config(args.config)
    db_path = database_path(bundle, args.db)
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else resolve_path(cfg_get(bundle.payload, "paths.output_dir"), base_dir=bundle.base_dir) / "stage5" / args.as_of
    universe_csv = (
        args.universe_csv.expanduser().resolve()
        if args.universe_csv
        else resolve_path(
            cfg_get(bundle.payload, "positioning.upstream_universe_csv"),
            base_dir=bundle.base_dir,
        )
    )
    with connect(db_path, timeout_sec=float(cfg_get(bundle.payload, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        bootstrap_stage5(conn, bundle)
        rows = build_positioning_universe_rows(conn, bundle)
        if not rows:
            raise RuntimeError("Consumer Defensive positioning universe is empty; run Stage 2 first.")
        write_positioning_universe_csv(universe_csv, rows)
        result = audit_upstream_positioning(conn, bundle, as_of=args.as_of)
    payload = {"database": str(db_path), "universe_csv": str(universe_csv), "universe_rows": len(rows), **result, "mutation_contract": "Consumer Defensive treats the upstream database as read-only; its owning neutral pipeline must rematch this CSV."}
    report = output_dir / "positioning_upstream_audit.json"
    write_json(report, payload)
    print(json.dumps({**payload, "report": str(report)}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
