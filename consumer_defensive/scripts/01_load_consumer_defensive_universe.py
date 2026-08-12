#!/usr/bin/env python3
from __future__ import annotations

# ruff: noqa: E402

import argparse
import json
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from consumer_defensive.core.config import cfg_get, load_config, resolve_path
from consumer_defensive.core.db import connect, finish_run, init_db, start_run
from consumer_defensive.core.source_registry import load_source_registry, upsert_source_registry
from consumer_defensive.core.universe import load_current_universe, load_policy, upsert_stage2_sources


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_POLICY = PACKAGE_ROOT / "data" / "consumer_defensive_universe_policy.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Load the independent Consumer Defensive Stage 2 current universe."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--universe-csv", type=Path, default=None)
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
    universe_path = args.universe_csv.expanduser().resolve() if args.universe_csv else policy.resolve("authoritative_current_csv")
    with connect(db_path, timeout_sec=timeout) as conn:
        init_db(conn)
        upsert_source_registry(
            conn,
            load_source_registry(
                resolve_path(cfg_get(bundle.payload, "source_registry.path"), base_dir=bundle.base_dir)
            ),
        )
        upsert_stage2_sources(
            conn,
            load_source_registry(
                resolve_path(cfg_get(bundle.payload, "source_registry.stage2_path"), base_dir=bundle.base_dir)
            ),
        )
        run_id = start_run(conn, run_type="consumer_defensive_stage2_current_universe", input_path=universe_path)
        try:
            stats = load_current_universe(conn, policy, universe_path)
            finish_run(
                conn,
                run_id=run_id,
                status="success",
                row_count=stats["current_rows"],
                message=json.dumps(stats, sort_keys=True),
            )
        except BaseException as exc:
            finish_run(
                conn,
                run_id=run_id,
                status="failed",
                message=f"{type(exc).__name__}: {exc}",
            )
            raise
    print(json.dumps({"database": str(db_path), **stats}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
