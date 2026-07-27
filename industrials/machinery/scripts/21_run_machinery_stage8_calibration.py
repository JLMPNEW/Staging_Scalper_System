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
from industrials.machinery.stage8_calibration import (  # noqa: E402
    run_stage8,
    stage8_paths,
    validate_stage8,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run report-only machinery Stage 8 diagnostics, constrained "
            "calibration, and walk-forward validation."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--trials", type=int, default=None)
    parser.add_argument("--walk-forward-trials", type=int, default=None)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--require-stage9-ready", action="store_true")
    return parser.parse_args()


def main() -> int:
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
    output_root = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else resolve_path(
            cfg_get(config, "machinery_stage8.output_root"),
            base_dir=config_path.parent,
        )
    )
    paths = stage8_paths(output_root)
    if paths.run_manifest_json.exists() and not args.force:
        validation = validate_stage8(
            config,
            output_root=output_root,
            require_stage9_ready=bool(args.require_stage9_ready),
        )
        if validation["acceptance"] == "PASS":
            print(json.dumps(validation, indent=2, sort_keys=True))
            return 0
        raise FileExistsError(
            "Existing Stage 8 artifacts are invalid; rerun with --force "
            f"after reviewing {paths.validation_json}"
        )
    trials = int(
        args.trials
        if args.trials is not None
        else cfg_get(config, "machinery_stage8.calibration_trials", 96)
    )
    walk_forward_trials = int(
        args.walk_forward_trials
        if args.walk_forward_trials is not None
        else cfg_get(
            config,
            "machinery_stage8.walk_forward.trials_per_refit",
            24,
        )
    )
    if trials < 2 or walk_forward_trials < 2:
        raise ValueError("Stage 8 trial counts must be at least two")
    acceptance = run_stage8(
        config,
        config_path=config_path,
        db_path=db_path,
        output_root=output_root,
        trials=trials,
        walk_forward_trials=walk_forward_trials,
    )
    validation = validate_stage8(
        config,
        output_root=output_root,
        require_stage9_ready=bool(args.require_stage9_ready),
    )
    print(
        json.dumps(
            {
                "stage8": acceptance,
                "validation": validation,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if validation["acceptance"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
