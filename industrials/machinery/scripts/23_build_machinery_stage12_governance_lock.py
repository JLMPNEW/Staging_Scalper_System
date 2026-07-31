#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import (  # noqa: E402
    cfg_get,
    load_yaml,
    resolve_path,
)
from industrials.machinery.stage12_governance import (  # noqa: E402
    build_stage12_lock,
    validate_stage12_lock,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build a fail-closed machinery Stage 12 governance lock and "
            "production-rank preview without activating production."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--asof", default=None)
    parser.add_argument("--stage8-dir", type=Path, default=None)
    parser.add_argument("--stage9-dir", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument(
        "--source-dashboard-dir",
        type=Path,
        default=None,
        help="Reviewed shadow dashboard directory used by this governance cycle.",
    )
    parser.add_argument(
        "--active-upgrade",
        action="store_true",
        help="Build a replacement cycle while the prior machinery model remains active.",
    )
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    stage8_root = (
        args.stage8_dir.expanduser().resolve()
        if args.stage8_dir
        else resolve_path(
            cfg_get(config, "machinery_stage8.output_root"),
            base_dir=config_path.parent,
        )
    )
    stage9_root = (
        args.stage9_dir.expanduser().resolve()
        if args.stage9_dir
        else resolve_path(
            cfg_get(config, "machinery_stage9.output_root"),
            base_dir=config_path.parent,
        )
    )
    output_root = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else resolve_path(
            cfg_get(config, "machinery_stage12.output_root"),
            base_dir=config_path.parent,
        )
    )
    if output_root.exists() and any(output_root.iterdir()) and not args.force:
        validation = validate_stage12_lock(output_root=output_root)
        if validation["acceptance"] == "PASS":
            print(json.dumps(validation, indent=2, sort_keys=True))
            return 0
        raise FileExistsError(
            f"Invalid Stage 12 candidate exists under {output_root}; "
            "review it before rerunning with --force"
        )
    asof = str(
        args.asof
        or cfg_get(config, "machinery_stage12.promotion_candidate_asof")
    )
    lock = build_stage12_lock(
        config,
        config_path=config_path,
        stage8_root=stage8_root,
        stage9_root=stage9_root,
        output_root=output_root,
        asof=asof,
        allow_active_upgrade=bool(args.active_upgrade),
        source_dashboard_dir=(
            args.source_dashboard_dir.expanduser().resolve()
            if args.source_dashboard_dir
            else None
        ),
    )
    validation = validate_stage12_lock(output_root=output_root)
    print(
        json.dumps(
            {"stage12": lock, "validation": validation},
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if validation["acceptance"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
