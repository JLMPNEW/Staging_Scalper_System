#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
from pathlib import Path

from macro_raw_config import configure_pipeline_logging, connect_sqlite, load_macro_raw_config, resolve_db_path
from macro_storage import init_db

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Initialize macro_raw SQLite schema.")
    parser.add_argument("--config", type=Path, default=None, help="Path to macro raw YAML config.")
    parser.add_argument("--db-path", type=Path, default=None, help="Optional SQLite DB path override.")
    return parser.parse_args()


def main() -> None:
    configure_pipeline_logging()
    args = parse_args()
    config_path, cfg = load_macro_raw_config(args.config)
    db_path = resolve_db_path(cfg, config_path, override=args.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect_sqlite(db_path)
    try:
        init_db(conn)
    finally:
        conn.close()
    logger.info("Initialized macro raw DB at: %s", db_path)


if __name__ == "__main__":
    main()
