#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from macro_raw_config import configure_pipeline_logging, connect_sqlite, load_macro_raw_config
from macro_serving_common import resolve_serving_db_path
from macro_serving_storage import init_db


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Initialize the macro serving SQLite database.")
    parser.add_argument("--config", type=Path, default=None, help="Path to macro raw YAML config.")
    parser.add_argument("--serving-db-path", type=Path, default=None, help="Optional serving SQLite path override.")
    return parser.parse_args()


def main() -> None:
    configure_pipeline_logging()
    args = parse_args()
    config_path, cfg = load_macro_raw_config(args.config)
    serving_db_path = resolve_serving_db_path(cfg, config_path, override=args.serving_db_path)
    serving_db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect_sqlite(serving_db_path)
    try:
        init_db(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
