#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from technology.core.financial_lineage_shadow import (  # noqa: E402
    build_financial_lineage_shadow,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
FAMILY_CONFIG = {
    "semiconductors": (
        "semiconductor_dashboard_reports",
        "../output/technology_reports/semi_dashboard",
        "semiconductor_final_rank_table.csv",
    ),
    "software_infrastructure": (
        "software_infrastructure_dashboard_reports",
        "../output/technology_reports/software_infrastructure/dashboard",
        "software_infrastructure_final_rank_table.csv",
    ),
    "technology_hardware": (
        "technology_hardware_dashboard_reports",
        "../output/technology_reports/technology_hardware/dashboard",
        "technology_hardware_final_rank_table.csv",
    ),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a non-production technology-family financial-lineage shadow report."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--family", required=True, choices=sorted(FAMILY_CONFIG))
    parser.add_argument("--asof", default="")
    parser.add_argument("--rank-table", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = (
        args.db.expanduser().resolve()
        if args.db
        else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    )
    config_key, default_dashboard, rank_name = FAMILY_CONFIG[args.family]
    dashboard_root = resolve_path(
        cfg_get(config, f"{config_key}.output_dir", default_dashboard),
        base_dir=base_dir,
    )
    if args.rank_table:
        rank_table = args.rank_table.expanduser().resolve()
    elif args.asof:
        rank_table = dashboard_root / args.asof / rank_name
    else:
        rank_table = dashboard_root / rank_name
    output_root = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else PROJECT_ROOT
        / "output"
        / "technology_reports"
        / args.family
        / "financial_lineage_shadow"
    )
    output_dir = output_root / args.asof if args.asof else output_root
    manifest = build_financial_lineage_shadow(
        db_path=db_path,
        rank_table_path=rank_table,
        output_dir=output_dir,
        model_family=args.family,
        expected_asof=str(args.asof or ""),
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0 if manifest["acceptance"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
