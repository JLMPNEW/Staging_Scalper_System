#!/usr/bin/env python3
"""Run valuation contracts, level construction, validation, and evidence capture."""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import load_yaml  # noqa: E402
from portfolio_layer.core.contracts import fail_if_exists, read_manifest, sha256_file, write_csv, write_manifest  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
STEPS = (
    ("valuation_inputs", "60_build_valuation_inputs.py", "valuation_inputs_manifest.json"),
    ("build_levels", "61_build_levels.py", "levels_build_manifest.json"),
    ("validate_levels", "62_validate_levels.py", "levels_manifest.json"),
    ("level_outcomes", "63_update_level_outcomes.py", "level_outcome_ledger_manifest.json"),
)
STEP_FIELDS = ["step", "status", "return_code", "manifest_path", "manifest_sha256", "detail"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--as-of", type=date.fromisoformat)
    parser.add_argument("--universe-as-of", type=date.fromisoformat)
    parser.add_argument("--levels-dir", type=Path)
    parser.add_argument("--monitor-dir", type=Path)
    parser.add_argument("--market-data-dir", type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    return parser.parse_args()


def _run(command: list[str], *, dry_run: bool) -> tuple[int, str]:
    if dry_run:
        return 0, subprocess.list2cmdline(command)
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    detail = "\n".join(text.strip() for text in (result.stdout, result.stderr) if text.strip())
    if detail:
        print(detail)
    return int(result.returncode), detail


def run_selftest() -> None:
    assert [step[0] for step in STEPS] == ["valuation_inputs", "build_levels", "validate_levels", "level_outcomes"]
    print("levels daily orchestrator selftest: PASS")


def main() -> int:
    args = parse_args()
    if args.selftest:
        run_selftest()
        return 0
    if args.as_of is None:
        raise ValueError("--as-of is required")
    config_path = args.config.resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    as_of = args.as_of.isoformat()
    universe_as_of = (args.universe_as_of or args.as_of).isoformat()
    output_dir = args.levels_dir or paths.output_dir / "runs" / as_of / "levels"
    steps_path = output_dir / "levels_daily_steps.csv"
    manifest_path = output_dir / "levels_daily_manifest.json"
    if args.dry_run:
        for step, script_name, _manifest_relative in STEPS:
            script = Path(__file__).with_name(script_name)
            command = [sys.executable, str(script), "--config", str(config_path), "--as-of", as_of]
            if step == "valuation_inputs":
                command.extend(["--universe-as-of", universe_as_of, "--output-dir", str(output_dir)])
            elif step == "build_levels":
                command.extend(["--levels-dir", str(output_dir)])
                if args.monitor_dir is not None:
                    command.extend(["--monitor-dir", str(args.monitor_dir.resolve())])
                if args.market_data_dir is not None:
                    command.extend(["--market-data-dir", str(args.market_data_dir.resolve())])
            elif step in {"validate_levels", "level_outcomes"}:
                command.extend(["--input-dir", str(output_dir)])
                if step == "level_outcomes" and args.market_data_dir is not None:
                    command.extend(["--market-data-dir", str(args.market_data_dir.resolve())])
            if args.force and step != "level_outcomes":
                command.append("--force")
            print(f"{step}: {subprocess.list2cmdline(command)}")
        return 0
    fail_if_exists([steps_path, manifest_path], force=args.force)
    rows: list[dict[str, Any]] = []
    children: list[dict[str, str]] = []
    failed = False
    for step, script_name, manifest_relative in STEPS:
        script = Path(__file__).with_name(script_name)
        command = [sys.executable, str(script), "--config", str(config_path), "--as-of", as_of]
        if step == "valuation_inputs":
            command.extend(["--universe-as-of", universe_as_of])
            command.extend(["--output-dir", str(output_dir)])
        elif step == "build_levels":
            command.extend(["--levels-dir", str(output_dir)])
            if args.monitor_dir is not None:
                command.extend(["--monitor-dir", str(args.monitor_dir.resolve())])
            if args.market_data_dir is not None:
                command.extend(["--market-data-dir", str(args.market_data_dir.resolve())])
        elif step in {"validate_levels", "level_outcomes"}:
            command.extend(["--input-dir", str(output_dir)])
            if step == "level_outcomes" and args.market_data_dir is not None:
                command.extend(["--market-data-dir", str(args.market_data_dir.resolve())])
        if args.force and step != "level_outcomes":
            command.append("--force")
        return_code, detail = _run(command, dry_run=args.dry_run)
        child_manifest = (
            paths.output_dir / "levels" / "outcomes" / "level_outcome_ledger_manifest.json"
            if step == "level_outcomes"
            else (output_dir / manifest_relative).resolve()
        )
        valid = args.dry_run
        child_hash = ""
        if not args.dry_run and return_code == 0 and child_manifest.is_file():
            payload = read_manifest(child_manifest)
            valid = payload.get("acceptance") in {"PASS", "PASS_WITH_DEFERRED"} and payload.get("as_of_date") == as_of
            child_deferred = payload.get("acceptance") == "PASS_WITH_DEFERRED"
            child_hash = sha256_file(child_manifest)
        else:
            child_deferred = False
        rows.append(
            {
                "step": step,
                "status": "DRY_RUN" if args.dry_run else "DEFERRED" if valid and child_deferred else "PASS" if valid else "FAIL",
                "return_code": return_code,
                "manifest_path": str(child_manifest),
                "manifest_sha256": child_hash,
                "detail": detail,
            }
        )
        if child_hash:
            children.append({"step": step, "manifest_path": str(child_manifest), "manifest_sha256": child_hash})
        if not valid:
            failed = True
            if not args.dry_run:
                break
    write_csv(steps_path, STEP_FIELDS, rows)
    acceptance = (
        "DRY_RUN"
        if args.dry_run
        else "FAIL"
        if failed
        else "PASS_WITH_DEFERRED"
        if any(row["status"] == "DEFERRED" for row in rows)
        else "PASS"
    )
    source_paths = [config_path, Path(__file__).resolve(), Path(__file__).with_name("levels_common.py")]
    write_manifest(
        manifest_path,
        {
            "schema_version": "levels_daily_manifest_v1",
            "acceptance": acceptance,
            "as_of_date": as_of,
            "universe_as_of": universe_as_of,
            "shadow_only": True,
            "broker_execution_prohibited": True,
            "child_manifests": children,
            "inputs_sha256": {str(path): sha256_file(path) for path in source_paths},
            "outputs_sha256": {steps_path.name: sha256_file(steps_path)},
        },
    )
    print(f"LEVELS DAILY: {acceptance}")
    print(f"manifest={manifest_path}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
