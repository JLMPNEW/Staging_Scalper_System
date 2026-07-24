#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.db import connect, finish_run, init_db, start_run  # noqa: E402
from industrials.core.family_universe import load_historical_and_delisted  # noqa: E402
from industrials.core.source_registry import load_source_registry, upsert_source_registry  # noqa: E402
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
    resolve_foundation,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load transportation PIT historical and delisted membership.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    paths = resolve_foundation(args.config, args.db)
    with connect(paths.db_path, timeout_sec=paths.timeout_sec) as conn:
        init_db(conn)
        upsert_source_registry(conn, load_source_registry(paths.registry_path))
        run_id = start_run(conn, run_type="load_transportation_historical_membership", input_path=paths.delisted_path)
        try:
            with conn:
                historical_count, delisted_count = load_historical_and_delisted(
                    conn,
                    historical_path=paths.historical_path,
                    delisted_path=paths.delisted_path,
                    cohort_path=paths.cohort_path,
                    model_family=MODEL_FAMILY,
                    historical_source_id=paths.historical_source_id,
                    delisted_source_id=paths.delisted_source_id,
                    default_start_date=paths.default_start_date,
                )
            finish_run(
                conn,
                run_id=run_id,
                status="success",
                row_count=historical_count + delisted_count,
                message=f"historical={historical_count} delisted={delisted_count}",
            )
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise
    print(f"PASS: loaded transportation historical={historical_count} delisted={delisted_count} db={paths.db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
