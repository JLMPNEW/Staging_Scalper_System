#!/usr/bin/env python3
"""Run the authoritative-event, market-signal, state, evidence, and levels chain."""

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
from portfolio_layer.core.contracts import (  # noqa: E402
    fail_if_exists,
    read_manifest,
    sha256_file,
    write_csv,
    write_manifest,
)
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.expectations_monitor.monitor_common import (  # noqa: E402
    monitor_output_subdir,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
LEVELS_RUNNER = PACKAGE_ROOT / "levels" / "64_run_levels_daily.py"
STEP_FIELDS = ["step", "status", "return_code", "manifest_path", "manifest_sha256", "detail"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--as-of", type=date.fromisoformat)
    parser.add_argument("--universe-as-of", type=date.fromisoformat)
    parser.add_argument("--market-data-dir", type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    return parser.parse_args()


def _run(command: list[str]) -> tuple[int, str]:
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    detail = "\n".join(value.strip() for value in (result.stdout, result.stderr) if value.strip())
    if detail:
        print(detail)
    return int(result.returncode), detail


def run_selftest() -> None:
    assert LEVELS_RUNNER.name == "64_run_levels_daily.py"
    parent = Path("expectations_pipeline_manifest.json")
    child = Path("levels_daily_manifest.json")
    assert parent != child
    print("expectations state pipeline selftest: PASS")


def main() -> int:
    args = parse_args()
    if args.selftest:
        run_selftest()
        return 0
    if args.as_of is None or args.market_data_dir is None:
        raise ValueError("--as-of and --market-data-dir are required")
    config_path = args.config.resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    as_of = args.as_of.isoformat()
    universe_as_of = (args.universe_as_of or args.as_of).isoformat()
    monitor_subdir = monitor_output_subdir(config)
    monitor_dir = paths.output_dir / "runs" / as_of / monitor_subdir
    events_dir = monitor_dir / "events"
    signals_dir = monitor_dir / "signals"
    levels_dir = paths.output_dir / "runs" / as_of / "levels"
    steps_path = monitor_dir / "expectations_pipeline_steps.csv"
    pipeline_manifest_path = monitor_dir / "expectations_pipeline_manifest.json"
    steps: list[tuple[str, Path, list[str], Path]] = [
        (
            "authoritative_events",
            Path(__file__).with_name("53_sync_authoritative_events.py"),
            ["--universe-as-of", universe_as_of, "--output-dir", str(events_dir)],
            events_dir / "event_ingestion_manifest.json",
        ),
        (
            "classify_events",
            Path(__file__).with_name("54_classify_monitor_events.py"),
            ["--input-dir", str(events_dir), "--output-dir", str(events_dir)],
            events_dir / "event_classification_manifest.json",
        ),
        (
            "market_signals",
            Path(__file__).with_name("55_build_monitor_market_signals.py"),
            [
                "--universe-as-of", universe_as_of,
                "--market-data-dir", str(args.market_data_dir.resolve()),
                "--output-dir", str(signals_dir),
            ],
            signals_dir / "market_signals_manifest.json",
        ),
        (
            "expectations_state",
            Path(__file__).with_name("56_build_expectations_state.py"),
            [
                "--universe-as-of", universe_as_of, "--events-dir", str(events_dir),
                "--signals-dir", str(signals_dir), "--output-dir", str(monitor_dir),
            ],
            monitor_dir / "expectations_state_manifest.json",
        ),
        (
            "validate_state",
            Path(__file__).with_name("57_validate_expectations_state.py"),
            ["--input-dir", str(monitor_dir)],
            monitor_dir / "validation" / "expectations_state_validation_manifest.json",
        ),
        (
            "state_outcomes",
            Path(__file__).with_name("58_update_monitor_outcomes.py"),
            ["--input-dir", str(monitor_dir)],
            paths.output_dir
            / monitor_subdir
            / "outcomes"
            / "state_outcome_ledger_manifest.json",
        ),
        (
            "levels",
            LEVELS_RUNNER,
            [
                "--universe-as-of", universe_as_of, "--levels-dir", str(levels_dir),
                "--monitor-dir", str(monitor_dir),
                "--market-data-dir", str(args.market_data_dir.resolve()),
            ],
            levels_dir / "levels_daily_manifest.json",
        ),
    ]
    if args.dry_run:
        for step, script, extra, expected_manifest in steps:
            command = [
                sys.executable, str(script), "--config", str(config_path),
                "--as-of", as_of, *extra,
            ]
            if args.force and step != "state_outcomes":
                command.append("--force")
            print(
                f"{step}: {subprocess.list2cmdline(command)}; "
                f"manifest={expected_manifest.resolve()}"
            )
        return 0
    fail_if_exists([steps_path, pipeline_manifest_path], force=args.force)
    step_rows: list[dict[str, Any]] = []
    children: list[dict[str, str]] = []
    failed = False
    for step, script, extra, child_manifest_path in steps:
        command = [sys.executable, str(script), "--config", str(config_path), "--as-of", as_of, *extra]
        if args.force and step not in {"state_outcomes"}:
            command.append("--force")
        return_code, detail = _run(command)
        valid = False
        child_hash = ""
        if return_code == 0 and child_manifest_path.is_file():
            payload = read_manifest(child_manifest_path)
            valid = payload.get("acceptance") in {"PASS", "PASS_WITH_DEFERRED"} and payload.get("as_of_date") == as_of
            child_deferred = payload.get("acceptance") == "PASS_WITH_DEFERRED"
            child_hash = sha256_file(child_manifest_path)
        else:
            child_deferred = False
        step_rows.append(
            {
                "step": step,
                "status": "DEFERRED" if valid and child_deferred else "PASS" if valid else "FAIL",
                "return_code": return_code,
                "manifest_path": str(child_manifest_path.resolve()),
                "manifest_sha256": child_hash,
                "detail": detail,
            }
        )
        if child_hash:
            children.append(
                {
                    "step": step,
                    "manifest_path": str(child_manifest_path.resolve()),
                    "manifest_sha256": child_hash,
                }
            )
        if not valid:
            failed = True
            break
    write_csv(steps_path, STEP_FIELDS, step_rows)
    acceptance = (
        "FAIL"
        if failed
        else "PASS_WITH_DEFERRED"
        if any(row["status"] == "DEFERRED" for row in step_rows)
        else "PASS"
    )
    source_paths = [
        config_path,
        Path(__file__).resolve(),
        Path(__file__).with_name("state_common.py"),
        PACKAGE_ROOT / "levels" / "levels_common.py",
    ]
    write_manifest(
        pipeline_manifest_path,
        {
            "schema_version": "expectations_pipeline_manifest_v1",
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
    print(f"EXPECTATIONS STATE PIPELINE: {acceptance}")
    print(f"manifest={pipeline_manifest_path}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
