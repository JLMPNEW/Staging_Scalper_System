#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.db import connect, finish_run, init_db, start_run  # noqa: E402
from industrials.core.source_registry import load_source_registry, upsert_source_registry  # noqa: E402
from industrials.machinery.universe import load_historical_membership  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load machinery survivorship-corrected PIT membership.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    membership_path = resolve_path(cfg_get(config, "industrials_universe.historical_membership_csv"), base_dir=base_dir)
    delisted_path = resolve_path(cfg_get(config, "industrials_universe.delisted_seed_csv"), base_dir=base_dir)
    cohort_path = resolve_path(cfg_get(config, "industrials_universe.cohort_path"), base_dir=base_dir)
    registry_path = resolve_path(cfg_get(config, "source_registry.path"), base_dir=base_dir)
    membership_source_id = str(cfg_get(config, "industrials_universe.historical_membership_source_id"))
    delisted_source_id = str(cfg_get(config, "industrials_universe.delisted_source_id"))
    norgate_source_id = str(cfg_get(config, "norgate_delisted_import.source_id", "norgate_us_equities_total_return"))
    timeout = float(cfg_get(config, "runtime.sqlite_timeout_sec", 120.0))
    with connect(db_path, timeout_sec=timeout) as conn:
        init_db(conn)
        upsert_source_registry(conn, load_source_registry(registry_path))
        run_id = start_run(conn, run_type="load_machinery_historical_membership", input_path=membership_path)
        try:
            with conn:
                membership_count, delisted_count = load_historical_membership(
                    conn,
                    membership_path=membership_path,
                    delisted_path=delisted_path,
                    cohort_path=cohort_path,
                    membership_source_id=membership_source_id,
                    delisted_source_id=delisted_source_id,
                    norgate_source_id=norgate_source_id,
                )
            finish_run(
                conn,
                run_id=run_id,
                status="success",
                row_count=membership_count,
                message=f"membership={membership_count} delisted_seed={delisted_count}",
            )
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise
    print(f"PASS: loaded machinery historical membership={membership_count} delisted_seed={delisted_count} db={db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
