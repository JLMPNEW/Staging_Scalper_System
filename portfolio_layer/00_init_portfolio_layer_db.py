#!/usr/bin/env python3
"""Stage 0 - initialize the independent portfolio-layer SQLite database."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[0]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.db import connect, finish_run, init_db, start_run, table_names  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402


LOGGER = logging.getLogger("init_portfolio_layer_db")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Initialize the independent portfolio-layer SQLite database.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None, help="Override SQLite database path.")
    return parser.parse_args()


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    db_path = args.db.expanduser().resolve() if args.db else paths.database_path
    # Independence guard: the layer must never write into the PROD tree, even via --db override.
    if "PROD_Scalper_System" in db_path.as_posix():
        raise SystemExit(f"Refusing to write DB into the PROD tree: {db_path}")
    timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))

    with connect(db_path, timeout_sec=timeout_sec) as conn:
        init_db(conn)
        run_id = start_run(conn, run_type="init_portfolio_layer_db", input_path=config_path)
        try:
            tables = table_names(conn)
            finish_run(conn, run_id=run_id, status="success", row_count=len(tables), message=f"db={db_path}")
            LOGGER.info("Initialized portfolio-layer DB: %s", db_path)
            LOGGER.info("Tables: %s", ", ".join(tables))
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()