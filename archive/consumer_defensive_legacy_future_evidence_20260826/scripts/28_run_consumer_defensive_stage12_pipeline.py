"""Run validated Stage 10 publishing and the Stage 12 operational handoff."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from consumer_defensive.core.config import (  # noqa: E402
    cfg_get,
    load_config,
    resolve_path,
)
from consumer_defensive.core.stage3_runtime import database_path  # noqa: E402
from consumer_defensive.core.stage10_publishing import stage10_policy  # noqa: E402

DEFAULT_CONFIG = ROOT / "consumer_defensive" / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--asof", "--as-of", dest="asof", required=True)
    parser.add_argument("--stage8-root", type=Path)
    parser.add_argument("--stage9-root", type=Path)
    parser.add_argument("--factor-validation-root", type=Path)
    parser.add_argument("--stage10-output-root", type=Path)
    parser.add_argument("--operational-output-root", type=Path)
    parser.add_argument("--activation-registry", type=Path)
    parser.add_argument("--activation-registry-sha256", default="")
    parser.add_argument("--change-control-public-key", type=Path)
    parser.add_argument("--skip-local-score-build", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def _run(command: list[str], *, dry_run: bool) -> dict[str, object]:
    if dry_run:
        return {"status": "DRY_RUN", "command": command}
    completed = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
    )
    result: dict[str, object] = {
        "status": "PASS" if completed.returncode == 0 else "FAIL",
        "returncode": completed.returncode,
        "command": command,
        "stdout_tail": completed.stdout[-4000:],
        "stderr_tail": completed.stderr[-4000:],
    }
    if completed.returncode:
        raise RuntimeError(json.dumps(result, indent=2, sort_keys=True))
    return result


def _latest_artifact_root(
    base: Path,
    filename: str,
    *,
    dry_run: bool = False,
) -> Path:
    candidates = sorted(
        path.parent.resolve() for path in base.glob(f"**/{filename}")
    )
    if not candidates:
        if dry_run:
            return (base / "_dry_run_unresolved").resolve()
        raise FileNotFoundError(f"no accepted {filename} found below {base}")
    return candidates[-1]


def main() -> int:
    args = parse_args()
    bundle = load_config(args.config)
    base_output = resolve_path(
        cfg_get(bundle.payload, "paths.output_dir"),
        base_dir=bundle.base_dir,
    )
    db_path = database_path(bundle, args.db).expanduser().resolve()
    stage8_root = (
        args.stage8_root.resolve()
        if args.stage8_root
        else _latest_artifact_root(
            base_output / "stage8",
            "stage8_contract.json",
            dry_run=args.dry_run,
        )
    )
    stage9_root = (
        args.stage9_root.resolve()
        if args.stage9_root
        else _latest_artifact_root(
            base_output / "stage9",
            "stage9_contract.json",
            dry_run=args.dry_run,
        )
    )
    factor_root = (args.factor_validation_root or base_output / "factor_validation").resolve()
    stage10_root = (args.stage10_output_root or base_output / "stage10").resolve()
    operational_root = (args.operational_output_root or base_output / "dashboard").resolve()
    common = [
        "--config", str(args.config.resolve()),
        "--db", str(db_path),
        "--as-of", args.asof,
        "--stage9-root", str(stage9_root),
        "--stage8-root", str(stage8_root),
        "--factor-validation-root", str(factor_root),
        "--output-root", str(stage10_root),
    ]
    local_common = [
        "--config", str(args.config.resolve()),
        "--db", str(db_path),
        "--as-of", args.asof,
    ]
    commands = []
    if not args.skip_local_score_build:
        commands.extend(
            [
                [sys.executable, str(ROOT / "consumer_defensive/scripts/12_build_consumer_defensive_scoring_features.py"), *local_common],
                [sys.executable, str(ROOT / "consumer_defensive/scripts/12a_validate_consumer_defensive_scoring_features.py"), *local_common],
                [sys.executable, str(ROOT / "consumer_defensive/scripts/14_build_consumer_defensive_stage6c_panel.py"), *local_common],
                [sys.executable, str(ROOT / "consumer_defensive/scripts/16_build_consumer_defensive_stage7_scores.py"), *local_common],
                [sys.executable, str(ROOT / "consumer_defensive/scripts/16a_validate_consumer_defensive_stage7_scores.py"), *local_common],
            ]
        )
    commands.extend([
        [sys.executable, str(ROOT / "consumer_defensive/scripts/19_publish_consumer_defensive_stage10_reports.py"), *common],
        [sys.executable, str(ROOT / "consumer_defensive/scripts/19a_validate_consumer_defensive_stage10_reports.py"), *common],
    ])
    version = str(stage10_policy(bundle)["output_version"])
    stage10_dir = stage10_root / args.asof / version
    operational_command = [
        sys.executable,
        str(ROOT / "consumer_defensive/scripts/27_publish_consumer_defensive_operational.py"),
        "--as-of", args.asof,
        "--stage10-output-dir", str(stage10_dir),
        "--output-root", str(operational_root),
    ]
    activation_values = (
        args.activation_registry,
        args.change_control_public_key,
        str(args.activation_registry_sha256 or "").strip(),
    )
    if any(activation_values):
        if not all(activation_values):
            raise ValueError("all activation registry arguments are required together")
        operational_command.extend(
            [
                "--activation-registry", str(args.activation_registry.resolve()),
                "--activation-registry-sha256", args.activation_registry_sha256,
                "--change-control-public-key", str(args.change_control_public_key.resolve()),
            ]
        )
    commands.append(operational_command)
    steps = [_run(command, dry_run=args.dry_run) for command in commands]
    result = {
        "schema_version": "consumer_defensive_stage12_refresh_v1",
        "acceptance": "DRY_RUN" if args.dry_run else "PASS",
        "asof_date": args.asof,
        "mode": "bounded_production" if any(activation_values) else "shadow",
        "stage8_root": str(stage8_root),
        "stage9_root": str(stage9_root),
        "factor_validation_root": str(factor_root),
        "stage10_output_dir": str(stage10_dir),
        "operational_output_dir": str(operational_root / args.asof),
        "steps": steps,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
