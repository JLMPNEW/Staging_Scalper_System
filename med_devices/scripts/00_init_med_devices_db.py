#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.db import connect, finish_run, init_db, start_run  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402
from med_devices.core.source_registry import load_source_registry, upsert_source_registry  # noqa: E402


LOGGER = logging.getLogger("init_med_devices_db")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Initialize the independent medical-devices SQLite database.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None, help="Override SQLite database path.")
    parser.add_argument("--source-registry", type=Path, default=None, help="Override free source registry YAML.")
    parser.add_argument("--skip-source-registry", action="store_true", help="Create schema without loading source registry rows.")
    return parser.parse_args()


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    source_registry_path = (
        args.source_registry.expanduser().resolve()
        if args.source_registry
        else resolve_path(cfg_get(config, "source_registry.path"), base_dir=base_dir)
    )
    timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))

    with connect(db_path, timeout_sec=timeout_sec) as conn:
        init_db(conn)
        run_id = start_run(conn, run_type="init_med_devices_db", input_path=config_path)
        try:
            source_count = 0
            if not args.skip_source_registry:
                sources = load_source_registry(source_registry_path)
                source_count = upsert_source_registry(conn, sources)
            finish_run(
                conn,
                run_id=run_id,
                status="success",
                row_count=source_count,
                message=f"db={db_path} source_registry={source_registry_path} sources={source_count}",
            )
            LOGGER.info("Initialized med-devices DB: %s", db_path)
            LOGGER.info("Loaded source registry rows: %d", source_count)
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()

