#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from technology.software_infrastructure.dedicated_parser_baseline import (  # noqa: E402
    load_metric_registry,
    load_universe_members,
    open_read_only_database,
    parse_iso_date,
)
from technology.software_infrastructure.dedicated_parser_census import (  # noqa: E402
    build_source_scope_rows,
    write_source_scope_outputs,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_REGISTRY = (
    PACKAGE_ROOT
    / "software_infrastructure"
    / "data"
    / "software_infrastructure_specialized_metric_registry.yaml"
)
DEFAULT_CACHE = PROJECT_ROOT / "output" / "technology_cache" / "dedicated_parser"
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "output"
    / "technology_reports"
    / "software_infrastructure"
    / "dedicated_parser"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the read-only software SEC parser source scope and cache census."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", default=date.today().isoformat())
    parser.add_argument("--history-start-date", default="")
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    db_path = (
        args.db.expanduser().resolve()
        if args.db
        else resolve_path(
            cfg_get(config, "paths.database_path"),
            base_dir=config_path.parent,
        )
    )
    timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 120.0))
    asof_date = parse_iso_date(args.asof, field_name="asof")
    registry = load_metric_registry(args.registry.expanduser().resolve())
    history_start_date = parse_iso_date(
        args.history_start_date or registry.history_start_date,
        field_name="history_start_date",
    )
    with open_read_only_database(db_path, timeout_sec=timeout_sec) as conn:
        members = load_universe_members(
            conn,
            history_start_date=history_start_date,
            asof_date=asof_date,
        )
        rows = build_source_scope_rows(
            conn,
            cache_dir=args.cache_dir.expanduser().resolve(),
            registry=registry,
            members=members,
            history_start_date=history_start_date,
            asof_date=asof_date,
        )
    manifest = write_source_scope_outputs(
        output_dir=args.output_dir.expanduser().resolve() / asof_date,
        registry=registry,
        members=members,
        rows=rows,
        asof_date=asof_date,
        cache_dir=args.cache_dir.expanduser().resolve(),
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
