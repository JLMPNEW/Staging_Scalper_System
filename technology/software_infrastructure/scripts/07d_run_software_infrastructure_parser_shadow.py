#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.cli import main as parser_main  # noqa: E402
from technology.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from technology.core.db import connect, init_db  # noqa: E402
from technology.core.dedicated_parser.db_contract import (  # noqa: E402
    ensure_technology_parser_schema,
)
from technology.core.dedicated_parser.planner_compat import (  # noqa: E402
    ensure_shared_planner_compatibility,
    validate_shared_planner_compatibility,
)
from technology.software_infrastructure.dedicated_parser_baseline import (  # noqa: E402
    parse_iso_date,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_CACHE = PROJECT_ROOT / "output" / "technology_cache" / "dedicated_parser"
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "output"
    / "technology_reports"
    / "software_infrastructure"
    / "dedicated_parser"
)
ADAPTER = (
    "technology.software_infrastructure.dedicated_parser_adapter:"
    "extract_metric_evidence"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the software dedicated parser against a sealed technology manifest."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", default=date.today().isoformat())
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    db_path = (
        args.db.expanduser().resolve()
        if args.db
        else resolve_path(cfg_get(config, "paths.database_path"), base_dir=config_path.parent)
    )
    asof_date = parse_iso_date(args.asof, field_name="asof")
    source_manifest = args.source_manifest.expanduser().resolve()
    if not source_manifest.is_file():
        raise FileNotFoundError(source_manifest)
    timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 120.0))
    with connect(db_path, timeout_sec=timeout_sec) as conn:
        init_db(conn)
        with conn:
            ensure_technology_parser_schema(conn)
            ensure_shared_planner_compatibility(conn)
        validate_shared_planner_compatibility(conn)
    output_dir = args.output_dir.expanduser().resolve() / asof_date / "shadow"
    parser_args = [
        "--db",
        str(db_path),
        "--cache-dir",
        str(args.cache_dir.expanduser().resolve()),
        "--asof",
        asof_date,
        "--adapter",
        ADAPTER,
        "--source-manifest",
        str(source_manifest),
        "--workers",
        str(max(1, args.workers)),
        "--max-filings-per-ticker",
        "0",
        "--max-documents-per-filing",
        "0",
        "--all-metrics",
        "--require-complete-cache",
        "--output-json",
        str(output_dir / "software_parser_shadow_run.json"),
        "--cache-gate-output-json",
        str(output_dir / "software_parser_cache_gate.json"),
    ]
    if args.plan_only:
        parser_args.append("--plan-only")
    if args.force:
        parser_args.append("--force")
    return parser_main(parser_args)


if __name__ == "__main__":
    raise SystemExit(main())
