#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.refresh_lock import RefreshLock  # noqa: E402
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
)


ORCHESTRATOR_VERSION = "transportation_current_refresh_v1"
DEFAULT_OUTPUT_ROOT = (
    PROJECT_ROOT
    / "output"
    / "industrials"
    / "transportation"
    / "orchestration"
)
STEP_FIELDS = (
    "step_id",
    "stage",
    "label",
    "network",
    "status",
    "return_code",
    "duration_seconds",
    "command",
    "stdout_log",
    "stderr_log",
)


@dataclass(frozen=True)
class Step:
    step_id: str
    stage: str
    label: str
    script: str
    args: list[str] = field(default_factory=list)
    network: bool = False
    pass_config: bool = True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the transportation current-data refresh for one completed "
            "as-of date without modifying frozen historical/calibration inputs."
        )
    )
    parser.add_argument("--asof", required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--list-steps", action="store_true")
    parser.add_argument("--from-step", default="")
    parser.add_argument("--to-step", default="")
    parser.add_argument(
        "--skip-positioning-upstream",
        action="store_true",
        help=(
            "Skip only the upstream network refresh when its raw data was "
            "already refreshed for this date; local positioning import and "
            "validation still run."
        ),
    )
    return parser.parse_args()


def _script(relative: str) -> str:
    path = (PROJECT_ROOT / relative).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    return relative.replace("\\", "/")


def build_steps(
    asof: str,
    *,
    skip_positioning_upstream: bool = False,
) -> list[Step]:
    snapshot_complete = (
        "output/industrials/transportation/current_panels/"
        f"{asof}/transportation_current_complete_panel.csv.gz"
    )
    current_pit_root = (
        "output/industrials/transportation/current_panels/"
        f"{asof}"
    )
    steps = [
        Step(
            "00_validate_seed",
            "stage_0",
            "validate transportation seed contract",
            "industrials/transportation/scripts/00_validate_transportation_seed.py",
        ),
        Step(
            "02_validate_universe",
            "stage_2",
            "validate current and historical universe",
            "industrials/transportation/scripts/02_validate_transportation_universe.py",
        ),
        Step(
            "02b_validate_identity",
            "stage_2",
            "validate ticker aliases and security continuity",
            "industrials/transportation/scripts/02b_validate_transportation_identity_reconciliation.py",
        ),
        Step(
            "03_sync_prices",
            "stage_3",
            "sync active and benchmark adjusted prices",
            "industrials/transportation/scripts/03_sync_transportation_prices.py",
            ["--asof", asof, "--allow-partial"],
            network=True,
        ),
        Step(
            "04_audit_market",
            "stage_3",
            "audit market data policy",
            "industrials/transportation/scripts/04_audit_transportation_market_data_policy.py",
            ["--asof", asof],
        ),
        Step(
            "11_sync_fx",
            "stage_4",
            "sync required FX pairs",
            "industrials/transportation/scripts/11_sync_transportation_fx_rates.py",
            ["--end-date", asof, "--allow-partial"],
            network=True,
        ),
        Step(
            "07_sync_sec",
            "stage_4",
            "sync SEC submissions, CompanyFacts, and profiles incrementally",
            "industrials/transportation/scripts/07_sync_transportation_sec_fundamentals.py",
            ["--incremental", "--allow-partial", "--asof", asof],
            network=True,
        ),
        Step(
            "08c_sync_disclosures",
            "stage_4",
            "sync bounded active and historical specialized disclosures",
            "industrials/transportation/scripts/08c_sync_transportation_specialized_disclosures.py",
            ["--allow-partial", "--asof", asof],
            network=True,
        ),
        Step(
            "19_build_exact_pit",
            "stage_4",
            "build the exact-date PIT market, financial, and metric snapshot",
            "industrials/transportation/scripts/19_build_transportation_pit_feature_history.py",
            [
                "--dates",
                asof,
                "--end-date",
                asof,
                "--max-dates",
                "1",
                "--output-csv",
                f"{current_pit_root}/transportation_current_pit_build.csv",
                "--output-json",
                f"{current_pit_root}/transportation_current_pit_build_manifest.json",
            ],
        ),
        Step(
            "06_validate_market",
            "stage_4",
            "validate exact-date market features",
            "industrials/transportation/scripts/06_validate_transportation_market_stage.py",
            ["--asof", asof],
        ),
        Step(
            "08_validate_financial",
            "stage_4",
            "validate exact-date financial features",
            "industrials/transportation/scripts/08_validate_transportation_financial_stage.py",
            ["--asof", asof],
        ),
        Step(
            "08a_validate_metrics",
            "stage_4",
            "validate exact-date generic and specialized metric availability",
            "industrials/transportation/scripts/08a_validate_transportation_specialized_metrics.py",
            ["--asof", asof],
        ),
        Step(
            "08c_validate_disclosures",
            "stage_4",
            "validate current specialized disclosure recovery",
            "industrials/transportation/scripts/08c_validate_transportation_specialized_disclosures.py",
            ["--asof", asof],
        ),
    ]
    if not skip_positioning_upstream:
        steps.append(
            Step(
                "13_sync_positioning",
                "stage_5",
                "refresh current positioning upstream",
                "industrials/transportation/scripts/13_sync_transportation_positioning_upstream.py",
                [
                    "--daily-refresh",
                    "--end-date",
                    asof,
                    "--skip-ibkr-shortable-snapshot",
                ],
                network=True,
                pass_config=False,
            )
        )
    steps.extend(
        [
            Step(
                "09_import_positioning",
                "stage_5",
                "import and build exact-date positioning features",
                "industrials/transportation/scripts/09_import_transportation_positioning.py",
                ["--asof", asof],
                pass_config=False,
            ),
            Step(
                "14_validate_positioning",
                "stage_5",
                "validate exact-date positioning coverage",
                "industrials/transportation/scripts/14_validate_transportation_positioning.py",
                ["--asof", asof],
                pass_config=False,
            ),
            Step(
                "10_validate_eligibility",
                "stage_6",
                "validate scoring eligibility policy",
                "industrials/transportation/scripts/10_validate_transportation_scoring_eligibility_policy.py",
                ["--asof", asof],
            ),
            Step(
                "06a_build_scoring",
                "stage_6",
                "build exact-date transportation scoring features",
                "industrials/transportation/scripts/06a_build_transportation_scoring_features.py",
                ["--asof", asof, "--force"],
            ),
            Step(
                "06a_validate_scoring",
                "stage_6",
                "validate exact-date scoring features",
                "industrials/transportation/scripts/06a_validate_transportation_scoring_features.py",
                ["--asof", asof],
            ),
            Step(
                "17_publish_shadow",
                "stage_10",
                "publish exact-date shadow rank table",
                "industrials/transportation/scripts/17_publish_transportation_shadow_rank_table.py",
                ["--asof", asof],
            ),
            Step(
                "18_validate_shadow",
                "stage_10",
                "validate exact-date shadow rank table",
                "industrials/transportation/scripts/18_validate_transportation_shadow_rank_table.py",
                ["--asof", asof],
            ),
            Step(
                "19j_build_current_panel",
                "stage_11",
                "build isolated current-only v3 complete panel",
                "industrials/transportation/scripts/19j_build_transportation_current_complete_panel.py",
                ["--asof", asof],
            ),
            Step(
                "21b_export_monitor_source",
                "stage_11",
                "export outcome-blind current monitoring source",
                "industrials/transportation/scripts/21b_export_transportation_monitoring_source.py",
                [
                    "--asof",
                    asof,
                    "--complete-panel",
                    snapshot_complete,
                ],
            ),
            Step(
                "20_validate_portfolio",
                "stage_12",
                "validate portfolio adapter remains fail-closed shadow",
                "industrials/transportation/scripts/20_validate_transportation_portfolio_adapter_shadow.py",
                ["--asof", asof],
                pass_config=False,
            ),
            Step(
                "21a_audit_monitor",
                "stage_12",
                "audit zero-overlay monitor without outcomes or calibration",
                "industrials/transportation/scripts/21a_audit_transportation_zero_overlay_monitor.py",
                ["--asof", asof],
                pass_config=False,
            ),
        ]
    )
    for step in steps:
        _script(step.script)
    return steps


def select_steps(
    steps: list[Step],
    *,
    from_step: str,
    to_step: str,
) -> list[Step]:
    identifiers = [step.step_id for step in steps]
    start = 0
    end = len(steps)
    if from_step:
        if from_step not in identifiers:
            raise ValueError(f"unknown --from-step={from_step}; valid={identifiers}")
        start = identifiers.index(from_step)
    if to_step:
        if to_step not in identifiers:
            raise ValueError(f"unknown --to-step={to_step}; valid={identifiers}")
        end = identifiers.index(to_step) + 1
    if start >= end:
        raise ValueError("--from-step must not follow --to-step")
    return steps[start:end]


def _command(
    step: Step,
    *,
    config_path: Path,
) -> list[str]:
    command = [
        sys.executable,
        str((PROJECT_ROOT / step.script).resolve()),
    ]
    if step.pass_config:
        command.extend(["--config", str(config_path)])
    command.extend(step.args)
    return command


def _manifest_paths(output_root: Path, asof: str) -> tuple[Path, Path, Path]:
    directory = output_root / asof
    return (
        directory / "transportation_current_refresh_manifest.json",
        directory / "transportation_current_refresh_steps.csv",
        directory / "logs",
    )


def _write_manifest(path: Path, payload: dict[str, Any]) -> None:
    write_text_atomic(
        path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )


def main() -> int:
    args = parse_args()
    asof = datetime.strptime(args.asof[:10], "%Y-%m-%d").date().isoformat()
    config_path = args.config.expanduser().resolve()
    output_root = args.output_root.expanduser().resolve()
    all_steps = build_steps(
        asof,
        skip_positioning_upstream=args.skip_positioning_upstream,
    )
    selected = select_steps(
        all_steps,
        from_step=args.from_step.strip(),
        to_step=args.to_step.strip(),
    )
    if args.list_steps:
        for step in all_steps:
            print(
                f"{step.step_id}\t{step.stage}\t"
                f"network={int(step.network)}\t{step.label}"
            )
        return 0
    manifest_path, steps_path, logs_dir = _manifest_paths(output_root, asof)
    commands = [
        {
            "step_id": step.step_id,
            "stage": step.stage,
            "label": step.label,
            "network": step.network,
            "command": _command(step, config_path=config_path),
        }
        for step in selected
    ]
    if args.dry_run:
        payload = {
            "acceptance": "DRY_RUN",
            "orchestrator_version": ORCHESTRATOR_VERSION,
            "model_family": MODEL_FAMILY,
            "asof_date": asof,
            "selected_step_count": len(selected),
            "steps": commands,
            "frozen_historical_panel_modified": False,
            "calibration_executed": False,
            "production_promotion_authorized": False,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    logs_dir.mkdir(parents=True, exist_ok=True)
    lock_path = (
        PROJECT_ROOT / "output" / "industrials" / ".industrials_refresh.lock"
    ).resolve()
    started_at = datetime.now(timezone.utc)
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    with RefreshLock(lock_path):
        for step in selected:
            command = _command(step, config_path=config_path)
            stdout_path = logs_dir / f"{step.step_id}.stdout.log"
            stderr_path = logs_dir / f"{step.step_id}.stderr.log"
            start = time.monotonic()
            status = "PASS"
            return_code = 0
            with stdout_path.open("w", encoding="utf-8") as stdout_handle:
                with stderr_path.open("w", encoding="utf-8") as stderr_handle:
                    process = subprocess.run(
                        command,
                        cwd=PROJECT_ROOT,
                        stdout=stdout_handle,
                        stderr=stderr_handle,
                        check=False,
                        text=True,
                    )
                    return_code = int(process.returncode)
            duration = time.monotonic() - start
            if return_code != 0:
                status = "FAIL"
                errors.append(
                    f"{step.step_id}: return_code={return_code}; "
                    f"stderr={stderr_path}"
                )
            records.append(
                {
                    "step_id": step.step_id,
                    "stage": step.stage,
                    "label": step.label,
                    "network": int(step.network),
                    "status": status,
                    "return_code": return_code,
                    "duration_seconds": f"{duration:.3f}",
                    "command": json.dumps(command),
                    "stdout_log": str(stdout_path.resolve()),
                    "stderr_log": str(stderr_path.resolve()),
                }
            )
            write_csv_atomic(steps_path, STEP_FIELDS, records)
            print(
                f"transportation_current_refresh step={step.step_id} "
                f"status={status} seconds={duration:.1f}",
                flush=True,
            )
            if status == "FAIL":
                break
    finished_at = datetime.now(timezone.utc)
    acceptance = "PASS" if not errors and len(records) == len(selected) else "FAIL"
    payload = {
        "acceptance": acceptance,
        "orchestrator_version": ORCHESTRATOR_VERSION,
        "model_family": MODEL_FAMILY,
        "asof_date": asof,
        "started_at_utc": started_at.isoformat(),
        "finished_at_utc": finished_at.isoformat(),
        "selected_step_ids": [step.step_id for step in selected],
        "completed_step_ids": [
            str(record["step_id"])
            for record in records
            if record["status"] == "PASS"
        ],
        "failed_step_ids": [
            str(record["step_id"])
            for record in records
            if record["status"] == "FAIL"
        ],
        "network_step_count": sum(
            int(record["network"]) for record in records
        ),
        "step_report": str(steps_path.resolve()),
        "frozen_historical_panel_modified": False,
        "calibration_input_modified": False,
        "calibration_executed": False,
        "outcomes_accessed": False,
        "production_promotion_authorized": False,
        "errors": errors,
        "next_gate": (
            "AUDIT_CURRENT_REFRESH_COVERAGE"
            if acceptance == "PASS"
            else "RESUME_CURRENT_REFRESH_FROM_FAILED_STEP"
        ),
    }
    _write_manifest(manifest_path, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if acceptance == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
