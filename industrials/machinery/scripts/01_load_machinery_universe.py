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
from industrials.machinery.universe import load_active_universe, load_ticker_aliases  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load the machinery active universe into industrials.sqlite.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    active_path = resolve_path(cfg_get(config, "industrials_universe.seed_csv"), base_dir=base_dir)
    delisted_path = resolve_path(cfg_get(config, "industrials_universe.delisted_seed_csv"), base_dir=base_dir)
    cohort_path = resolve_path(cfg_get(config, "industrials_universe.cohort_path"), base_dir=base_dir)
    listing_path = resolve_path(cfg_get(config, "industrials_universe.listing_dates_csv"), base_dir=base_dir)
    aliases_path = resolve_path(cfg_get(config, "industrials_universe.ticker_aliases_csv"), base_dir=base_dir)
    registry_path = resolve_path(cfg_get(config, "source_registry.path"), base_dir=base_dir)
    seed_source_id = str(cfg_get(config, "industrials_universe.seed_source_id"))
    cohort_source_id = str(cfg_get(config, "industrials_universe.cohort_source_id"))
    alias_source_id = str(cfg_get(config, "industrials_universe.ticker_aliases_source_id"))
    policy = load_yaml(resolve_path(cfg_get(config, "industrials_universe.policy_path"), base_dir=base_dir))
    timeout = float(cfg_get(config, "runtime.sqlite_timeout_sec", 120.0))

    with connect(db_path, timeout_sec=timeout) as conn:
        init_db(conn)
        upsert_source_registry(conn, load_source_registry(registry_path))
        run_id = start_run(conn, run_type="load_machinery_universe", input_path=active_path)
        try:
            with conn:
                count = load_active_universe(
                    conn,
                    active_path=active_path,
                    delisted_path=delisted_path,
                    cohort_path=cohort_path,
                    listing_path=listing_path,
                    seed_source_id=seed_source_id,
                    cohort_source_id=cohort_source_id,
                    optimization_start=str(cfg_get(config, "industrials_universe.optimization_start_date", "2019-01-02")),
                    expected_active=int(policy.get("expected_ticker_count") or 0),
                    expected_delisted=int(policy.get("expected_delisted_count") or 0),
                )
                alias_count = load_ticker_aliases(conn, path=aliases_path, source_id=alias_source_id)
            finish_run(conn, run_id=run_id, status="success", row_count=count, message=f"active={count} aliases={alias_count}")
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise
    print(f"PASS: loaded machinery active universe rows={count} aliases={alias_count} db={db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
