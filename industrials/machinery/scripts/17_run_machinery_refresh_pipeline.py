#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.machinery.scoring import parse_asof  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


@dataclass(frozen=True)
class Step:
    step_id: str
    stage: str
    script: str
    args: list[str] = field(default_factory=list)
    network: bool = False
    pass_db: bool = True


class RefreshLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: int | None = None

    def __enter__(self) -> RefreshLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            detail = self.path.read_text(encoding="utf-8", errors="replace") if self.path.exists() else ""
            raise RuntimeError(f"Another industrials refresh owns {self.path}: {detail.strip()}") from exc
        os.write(
            self.fd,
            f"pid={os.getpid()} started_utc={datetime.now(timezone.utc).isoformat(timespec='seconds')}\n".encode(),
        )
        return self

    def __exit__(self, *_args: object) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        self.path.unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the machinery pipeline against the shared industrials database.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--list-steps", action="store_true")
    parser.add_argument("--skip-network", action="store_true")
    parser.add_argument(
        "--skip-sec-insider-refresh",
        action="store_true",
        help="Do not refresh the shared SEC Form 4 upstream before importing machinery positioning.",
    )
    parser.add_argument(
        "--full-positioning-refresh",
        action="store_true",
        help="Bootstrap FINRA, 13F, and IBKR history instead of using the lightweight daily positioning refresh.",
    )
    parser.add_argument("--include-norgate-backfill", action="store_true")
    parser.add_argument(
        "--include-historical-backfill",
        action="store_true",
        help="Build survivorship-corrected machinery dashboard history before publishing the current snapshot.",
    )
    parser.add_argument("--history-start-date", default="", help="Historical backfill start; defaults to config history_start_date.")
    parser.add_argument("--history-frequency", choices=("daily", "weekly"), default="daily")
    parser.add_argument(
        "--bootstrap-sec-archives",
        action="store_true",
        help="Populate the resumable SEC archive cache used by machinery-specific financial metrics.",
    )
    parser.add_argument(
        "--norgate-python",
        type=Path,
        default=None,
        help="Python executable containing norgatedata; used only by optional Stage 15.",
    )
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--from-step", default="")
    parser.add_argument("--to-step", default="")
    parser.add_argument("--only", default="")
    parser.add_argument("--skip-step", action="append", default=[])
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def build_steps(
    asof: str,
    *,
    force: bool,
    include_norgate_backfill: bool,
    refresh_sec_insider: bool = True,
    full_positioning_refresh: bool = False,
    bootstrap_sec_archives: bool = False,
    include_historical_backfill: bool = False,
    history_start_date: str = "2019-01-02",
    history_frequency: str = "daily",
) -> list[Step]:
    force_args = ["--force"] if force else []
    publish_force = ["--allow-overwrite"] if force else []
    sec_args = ["--force", "--allow-partial", "--asof", asof] if force else [
        "--incremental",
        "--allow-partial",
        "--asof",
        asof,
    ]
    if bootstrap_sec_archives:
        sec_args.append("--archive-bootstrap")
    positioning_args = ["--end-date", asof] if full_positioning_refresh else ["--daily-refresh", "--end-date", asof]
    steps = [
        Step("00_validate_seed", "stage_0", "00_validate_machinery_seed.py", pass_db=False),
        Step("00_init_db", "stage_0", "00_init_machinery_db.py"),
        Step("01_load_universe", "stage_1", "01_load_machinery_universe.py"),
        Step("01b_load_history", "stage_1", "01b_load_machinery_historical_membership.py"),
        Step("02_validate_universe", "stage_2", "02_validate_machinery_universe.py"),
        Step(
            "03_sync_prices",
            "stage_3",
            "03_sync_machinery_prices.py",
            ["--asof", asof, "--allow-partial", *(["--force-refresh"] if force else [])],
            True,
        ),
        Step("04_audit_market", "stage_3", "04_audit_machinery_market_data_policy.py", ["--asof", asof]),
        Step("05_build_market", "stage_3", "05_build_machinery_market_features.py", ["--asof", asof]),
        Step("06_validate_market", "stage_3", "06_validate_machinery_market_stage.py", ["--asof", asof]),
        Step("07_sync_sec", "stage_4", "07_sync_machinery_sec_fundamentals.py", sec_args, True),
        Step("11_sync_fx", "stage_4", "11_sync_machinery_fx_rates.py", ["--end-date", asof, "--allow-partial", *force_args], True),
        Step("08_build_financial", "stage_4", "08_build_machinery_financial_features.py", ["--asof", asof]),
        Step("08_validate_financial", "stage_4", "08_validate_machinery_financial_stage.py", ["--asof", asof]),
        Step(
            "08a_audit_special_metrics",
            "stage_4",
            "08a_audit_machinery_financial_metrics.py",
            ["--asof", asof],
        ),
        Step(
            "13_sync_positioning",
            "stage_5",
            "13_sync_machinery_positioning_upstream.py",
            positioning_args,
            True,
            False,
        ),
        Step("09_import_positioning", "stage_5", "09_import_machinery_positioning.py", ["--asof", asof]),
        Step("14_validate_positioning", "stage_5", "14_validate_machinery_positioning.py"),
        Step("10_validate_eligibility", "stage_6", "10_validate_machinery_scoring_eligibility.py", ["--asof", asof]),
        Step("06a_build_scoring", "stage_6", "06a_build_machinery_scoring_features.py", ["--asof", asof, *force_args]),
        Step("06a_validate_scoring", "stage_6", "06a_validate_machinery_scoring_features.py", ["--asof", asof]),
        Step(
            "10_build_scores",
            "stage_7",
            "10_build_machinery_calibrated_scores.py",
            ["--asof", asof, *force_args],
            pass_db=False,
        ),
        Step(
            "10b_publish",
            "stage_10",
            "10b_publish_machinery_dashboard_reports.py",
            ["--asof", asof, *publish_force],
            pass_db=False,
        ),
        Step(
            "10b_validate",
            "stage_10",
            "10b_validate_machinery_dashboard_reports.py",
            ["--asof", asof],
            pass_db=False,
        ),
        Step(
            "20_validate_portfolio",
            "stage_10",
            "20_validate_machinery_portfolio_adapter.py",
            ["--asof", asof],
            pass_db=False,
        ),
    ]
    if refresh_sec_insider:
        insert_at = next(index for index, step in enumerate(steps) if step.step_id == "13_sync_positioning")
        steps.insert(
            insert_at,
            Step(
                "12_sync_sec_ownership",
                "stage_5",
                "12_sync_machinery_sec_ownership.py",
                ["--asof", asof],
                True,
                False,
            ),
        )
    if include_norgate_backfill:
        insert_at = next(index for index, step in enumerate(steps) if step.step_id == "04_audit_market")
        steps.insert(
            insert_at,
            Step("15_norgate_backfill", "stage_3", "15_import_machinery_norgate_prices.py", ["--end-date", asof]),
        )
    if include_historical_backfill:
        insert_at = next(index for index, step in enumerate(steps) if step.step_id == "10b_publish")
        steps.insert(
            insert_at,
            Step(
                "18_backfill_history",
                "stage_11",
                "18_backfill_machinery_historical_dashboard_reports.py",
                [
                    "--start-date",
                    history_start_date,
                    "--end-date",
                    asof,
                    "--frequency",
                    history_frequency,
                    "--exclude-end-date",
                    "--rebuild-features",
                    *force_args,
                ],
            ),
        )
    return steps


def select_steps(steps: list[Step], args: argparse.Namespace) -> list[Step]:
    selected = list(steps)
    positions = {step.step_id: index for index, step in enumerate(steps)}
    if args.from_step:
        if args.from_step not in positions:
            raise ValueError(f"Unknown --from-step={args.from_step}")
        selected = selected[positions[args.from_step] :]
    if args.to_step:
        if args.to_step not in positions:
            raise ValueError(f"Unknown --to-step={args.to_step}")
        selected = [step for step in selected if positions[step.step_id] <= positions[args.to_step]]
    if args.only:
        if args.from_step or args.to_step:
            raise ValueError("--only cannot be combined with --from-step/--to-step")
        wanted = {item.strip() for item in args.only.split(",") if item.strip()}
        unknown = sorted(wanted - set(positions))
        if unknown:
            raise ValueError(f"Unknown --only steps={unknown}")
        selected = [step for step in steps if step.step_id in wanted]
    skipped = {str(item).strip() for item in args.skip_step}
    return [step for step in selected if step.step_id not in skipped and not (args.skip_network and step.network)]


def main() -> int:
    args = parse_args()
    asof = parse_asof(args.asof)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    dashboard_root = resolve_path(cfg_get(config, "machinery_scoring.dashboard_root"), base_dir=base_dir)
    orchestration_root = dashboard_root.parent / "orchestration"
    lock_path = dashboard_root.parent.parent / ".industrials_refresh.lock"
    steps = build_steps(
        asof,
        force=args.force,
        include_norgate_backfill=args.include_norgate_backfill,
        refresh_sec_insider=not args.skip_sec_insider_refresh,
        full_positioning_refresh=args.full_positioning_refresh,
        bootstrap_sec_archives=args.bootstrap_sec_archives,
        include_historical_backfill=args.include_historical_backfill,
        history_start_date=parse_asof(
            args.history_start_date
            or str(cfg_get(config, "machinery_scoring.history_start_date", "2019-01-02"))
        ),
        history_frequency=args.history_frequency,
    )
    if args.list_steps:
        for step in steps:
            print(f"{step.step_id}\t{step.stage}\t{'network' if step.network else 'local'}\t{step.script}")
        return 0
    selected = select_steps(steps, args)
    run_id = datetime.now(timezone.utc).strftime("machinery_refresh_%Y%m%dT%H%M%SZ")
    logs_dir = orchestration_root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    report_rows: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    with RefreshLock(lock_path):
        for index, step in enumerate(selected, start=1):
            script_path = PACKAGE_ROOT / "scripts" / step.script
            python_executable = (
                str(args.norgate_python.expanduser().resolve())
                if step.step_id == "15_norgate_backfill" and args.norgate_python is not None
                else sys.executable
            )
            command = [python_executable, str(script_path), "--config", str(config_path)]
            if step.pass_db:
                command.extend(["--db", str(db_path)])
            command.extend(step.args)
            log_path = logs_dir / f"{run_id}_{index:02d}_{step.step_id}.log"
            row: dict[str, Any] = {
                "run_id": run_id,
                "step_number": index,
                "step_id": step.step_id,
                "stage": step.stage,
                "script": str(script_path),
                "network_flag": int(step.network),
                "command": subprocess.list2cmdline(command),
                "log_path": str(log_path),
            }
            print(f"[{index}/{len(selected)}] {step.step_id}", flush=True)
            if args.dry_run:
                row.update({"status": "DRY_RUN", "return_code": "", "elapsed_sec": 0.0})
                report_rows.append(row)
                continue
            started = time.perf_counter()
            with log_path.open("w", encoding="utf-8", newline="") as log:
                result = subprocess.run(
                    command,
                    cwd=PROJECT_ROOT,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                    check=False,
                )
            row.update(
                {
                    "status": "PASS" if result.returncode == 0 else "FAIL",
                    "return_code": result.returncode,
                    "elapsed_sec": round(time.perf_counter() - started, 3),
                }
            )
            report_rows.append(row)
            if result.returncode != 0:
                failures.append(row)
                if not args.continue_on_error:
                    break
    orchestration_root.mkdir(parents=True, exist_ok=True)
    fields = [
        "run_id",
        "step_number",
        "step_id",
        "stage",
        "script",
        "network_flag",
        "command",
        "log_path",
        "status",
        "return_code",
        "elapsed_sec",
    ]
    write_csv_atomic(orchestration_root / "machinery_refresh_steps.csv", fields, report_rows)
    summary = {
        "acceptance": "PASS" if not failures else "FAIL",
        "run_id": run_id,
        "asof_date": asof,
        "database_path": str(db_path),
        "config_path": str(config_path),
        "dry_run": bool(args.dry_run),
        "planned_step_count": len(selected),
        "completed_step_count": len(report_rows),
        "failed_step_count": len(failures),
        "steps": report_rows,
    }
    write_text_atomic(
        orchestration_root / "machinery_refresh_manifest.json",
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps({key: summary[key] for key in ("acceptance", "run_id", "dry_run", "failed_step_count")}, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
