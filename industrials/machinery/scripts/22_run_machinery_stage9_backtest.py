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
from industrials.machinery.stage9_backtest import (  # noqa: E402
    run_stage9,
    stage9_paths,
    validate_stage9,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the report-only machinery Stage 9 portfolio backtest and "
            "capacity analysis."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--stage8-dir", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--require-stage12-ready", action="store_true")
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
    output_root = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else resolve_path(
            cfg_get(config, "machinery_stage9.output_root"),
            base_dir=config_path.parent,
        )
    )
    paths = stage9_paths(output_root)
    if paths.run_manifest_json.exists() and not args.force:
        validation = validate_stage9(
            config,
            stage8_root=stage8_root,
            output_root=output_root,
            require_stage12_ready=bool(args.require_stage12_ready),
        )
        if validation["acceptance"] == "PASS":
            print(json.dumps(validation, indent=2, sort_keys=True))
            return 0
        raise FileExistsError(
            "Existing Stage 9 artifacts are invalid; rerun with --force "
            f"after reviewing {paths.validation_json}"
        )
    acceptance = run_stage9(
        config,
        stage8_root=stage8_root,
        output_root=output_root,
    )
    validation = validate_stage9(
        config,
        stage8_root=stage8_root,
        output_root=output_root,
        require_stage12_ready=bool(args.require_stage12_ready),
    )
    print(
        json.dumps(
            {"stage9": acceptance, "validation": validation},
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if validation["acceptance"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
