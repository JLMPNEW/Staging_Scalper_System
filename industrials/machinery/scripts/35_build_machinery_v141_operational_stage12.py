#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import load_yaml  # noqa: E402
from industrials.machinery.operational_amendment_v141 import (  # noqa: E402
    DEFAULT_CONFIG_PATH,
    DEFAULT_OUTPUT_ROOT,
    amendment_paths,
    build_operational_stage12_candidate,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build a current-schema shadow and amended machinery Stage 12 candidate."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--asof", required=True)
    return parser.parse_args()


def _run(command: list[str]) -> None:
    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"Command failed ({completed.returncode}): {' '.join(command)}\n"
            f"{completed.stdout}\n{completed.stderr}"
        )


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    output_root = args.output_root.expanduser().resolve()
    paths = amendment_paths(output_root)
    source_root = paths.source_root / args.asof
    features = source_root / "machinery_scoring_feature_contract.csv"
    scores = source_root / "machinery_calibrated_scores.csv"
    dashboard = source_root / "dashboard"
    _run(
        [
            sys.executable,
            str(
                PACKAGE_ROOT
                / "scripts"
                / "06a_build_machinery_scoring_features.py"
            ),
            "--config",
            str(config_path),
            "--asof",
            args.asof,
            "--output-csv",
            str(features),
            "--force",
        ]
    )
    _run(
        [
            sys.executable,
            str(
                PACKAGE_ROOT
                / "scripts"
                / "10_build_machinery_calibrated_scores.py"
            ),
            "--config",
            str(config_path),
            "--asof",
            args.asof,
            "--input-csv",
            str(features),
            "--output-csv",
            str(scores),
            "--shadow-only",
            "--force",
        ]
    )
    _run(
        [
            sys.executable,
            str(
                PACKAGE_ROOT
                / "scripts"
                / "10b_publish_machinery_dashboard_reports.py"
            ),
            "--config",
            str(config_path),
            "--asof",
            args.asof,
            "--input-csv",
            str(scores),
            "--output-dir",
            str(dashboard),
            "--allow-overwrite",
        ]
    )
    result = build_operational_stage12_candidate(
        load_yaml(config_path),
        config_path=config_path,
        asof=args.asof,
        source_dashboard_dir=dashboard,
        output_root=output_root,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
