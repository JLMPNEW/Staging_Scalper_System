#!/usr/bin/env python3
from __future__ import annotations

# ruff: noqa: E402

import argparse
import logging
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from consumer_defensive.core.config import cfg_get, load_config, resolve_path  # noqa: E402
from consumer_defensive.core.db import (  # noqa: E402
    connect,
    finish_run,
    init_db,
    start_run,
    table_names,
)
from consumer_defensive.core.metric_registry import (  # noqa: E402
    load_metric_registry,
    upsert_metric_registry,
)
from consumer_defensive.core.source_registry import (  # noqa: E402
    load_source_registry,
    upsert_source_registry,
)


LOGGER = logging.getLogger("init_consumer_defensive_db")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Initialize the independent Consumer Defensive SQLite database."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None, help="Override SQLite database path.")
    parser.add_argument("--source-registry", type=Path, default=None)
    parser.add_argument("--metric-registry", type=Path, default=None)
    parser.add_argument("--skip-source-registry", action="store_true")
    parser.add_argument("--skip-metric-registry", action="store_true")
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)sZ %(levelname)s %(name)s %(message)s",
    )
    args = parse_args()
    bundle = load_config(args.config)
    config = bundle.payload
    timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))
    db_path = (
        args.db.expanduser().resolve()
        if args.db is not None
        else resolve_path(cfg_get(config, "paths.database_path"), base_dir=bundle.base_dir)
    )
    source_path = (
        args.source_registry.expanduser().resolve()
        if args.source_registry is not None
        else resolve_path(cfg_get(config, "source_registry.path"), base_dir=bundle.base_dir)
    )
    metric_path = (
        args.metric_registry.expanduser().resolve()
        if args.metric_registry is not None
        else resolve_path(
            cfg_get(config, "specialized_metrics.registry_path"),
            base_dir=bundle.base_dir,
        )
    )

    with connect(db_path, timeout_sec=timeout_sec) as conn:
        init_db(conn)
        run_id = start_run(
            conn,
            run_type="init_consumer_defensive_db",
            input_path=bundle.path,
        )
        try:
            source_count = 0
            if not args.skip_source_registry:
                sources = load_source_registry(source_path)
                if args.source_registry is None:
                    stage2_path = resolve_path(
                        cfg_get(config, 'source_registry.stage2_path'),
                        base_dir=bundle.base_dir,
                    )
                    sources.extend(load_source_registry(stage2_path))
                if len({row.source_id for row in sources}) != len(sources):
                    raise ValueError('Duplicate source_id across authoritative source registries.')
                source_count = upsert_source_registry(
                    conn,
                    sources,
                    retire_absent=True,
                )

            metric_count = 0
            if not args.skip_metric_registry:
                registry_version, metrics = load_metric_registry(metric_path)
                metric_count = upsert_metric_registry(
                    conn,
                    registry_version=registry_version,
                    metrics=metrics,
                )

            finish_run(
                conn,
                run_id=run_id,
                status="success",
                row_count=source_count + metric_count,
                message=(
                    f"db={db_path} sources={source_count} "
                    f"specialized_metrics={metric_count}"
                ),
            )
        except BaseException as exc:
            finish_run(
                conn,
                run_id=run_id,
                status="failed",
                message=f"{type(exc).__name__}: {exc}",
            )
            raise

        LOGGER.info("Initialized Consumer Defensive DB: %s", db_path)
        LOGGER.info("Loaded source registry rows: %d", source_count)
        LOGGER.info("Loaded specialized metric rows: %d", metric_count)
        LOGGER.info("Tables: %s", ", ".join(table_names(conn)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
