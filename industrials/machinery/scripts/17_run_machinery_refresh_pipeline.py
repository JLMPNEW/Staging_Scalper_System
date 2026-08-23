#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
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

from industrials.core.config import (  # noqa: E402
    cfg_get,
    expand_env_vars,
    load_yaml,
    resolve_path,
)
from industrials.core.refresh_lock import RefreshLock  # noqa: E402
from industrials.core.refresh_resume import load_resume_plan  # noqa: E402
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.core.source_coverage import (  # noqa: E402
    audit_industrials_source_coverage,
    require_source_coverage,
)
from industrials.machinery.scoring import parse_asof  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
COVERAGE_AUDIT_STEP_ID = "21_coverage_audit"
NON_RETRYABLE_POLICY_FAILURE = 78


@dataclass(frozen=True)
class Step:
    step_id: str
    stage: str
    script: str
    args: list[str] = field(default_factory=list)
    network: bool = False
    pass_db: bool = True


def _accepted_manifest_asof(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if payload.get("acceptance") != "PASS" or payload.get("dry_run"):
        return ""
    try:
        return parse_asof(str(payload.get("asof_date") or ""))
    except ValueError:
        return ""


def latest_committed_asof(
    *,
    db_path: Path,
    dashboard_root: Path,
    orchestration_root: Path,
) -> str:
    candidates: list[str] = []
    if db_path.exists():
        connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        try:
            table = connection.execute(
                """
                SELECT 1
                FROM sqlite_master
                WHERE type = 'table'
                  AND name = 'feature_financial_metric_availability'
                """
            ).fetchone()
            if table is not None:
                row = connection.execute(
                    """
                    SELECT MAX(asof_date)
                    FROM feature_financial_metric_availability
                    WHERE model_family = 'machinery'
                    """
                ).fetchone()
                if row is not None and row[0]:
                    candidates.append(parse_asof(row[0]))
        finally:
            connection.close()
    manifest_asof = _accepted_manifest_asof(
        orchestration_root / "machinery_refresh_manifest.json"
    )
    if manifest_asof:
        candidates.append(manifest_asof)
    if dashboard_root.exists():
        for manifest_path in dashboard_root.glob(
            "*/machinery_final_rank_table_manifest.json"
        ):
            dashboard_asof = _accepted_manifest_asof(manifest_path)
            if dashboard_asof:
                candidates.append(dashboard_asof)
    return max(candidates, default="")


def validate_non_regressive_asof(*, requested_asof: str, committed_asof: str) -> None:
    if committed_asof and requested_asof < committed_asof:
        raise ValueError(
            "Refusing regressive machinery current refresh: "
            f"requested_asof={requested_asof} latest_committed_asof={committed_asof}. "
            "Use the historical backfill runner for older dates."
        )


def resolve_dedicated_parser_python(
    *,
    cli_value: Path | None,
    config: dict[str, Any],
    base_dir: Path,
) -> Path:
    raw: object = (
        cli_value
        or cfg_get(config, "dedicated_parser.python_executable")
        or sys.executable
    )
    path = Path(expand_env_vars(raw)).expanduser()
    resolved = path.resolve() if path.is_absolute() else (base_dir / path).resolve()
    if not resolved.is_file():
        raise FileNotFoundError(
            f"Dedicated-parser Python executable not found: {resolved}"
        )
    return resolved


def validate_dedicated_parser_python(python_executable: Path) -> None:
    probe = subprocess.run(
        [str(python_executable), "-c", "import arelle.Cntlr, edgar.sgml"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if probe.returncode != 0:
        detail = (
            probe.stderr or probe.stdout or "provider import probe failed"
        ).strip()
        raise RuntimeError(
            "Dedicated-parser Python lacks Arelle or EdgarTools: "
            f"{python_executable}: {detail}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the machinery pipeline against the shared industrials database.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume", action="store_true")
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
        "--include-dedicated-parser-shadow",
        action="store_true",
        help=(
            "Run the independent SEC parser without production promotion when "
            "production mode is disabled in config."
        ),
    )
    parser.add_argument(
        "--skip-dedicated-parser-production",
        action="store_true",
        help="Emergency bypass for the config-enabled parser production lane.",
    )
    parser.add_argument(
        "--dedicated-parser-python",
        type=Path,
        default=None,
        help="Python executable containing EdgarTools and Arelle for the optional shadow parser.",
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
    overwrite_outputs: bool = False,
    include_norgate_backfill: bool,
    refresh_sec_insider: bool = True,
    full_positioning_refresh: bool = False,
    bootstrap_sec_archives: bool = False,
    include_dedicated_parser_shadow: bool = False,
    include_dedicated_parser_production: bool = False,
    include_historical_backfill: bool = False,
    history_start_date: str = "2019-01-02",
    history_frequency: str = "daily",
) -> list[Step]:
    source_force_args = ["--force"] if force else []
    derived_force_args = ["--force"] if (force or overwrite_outputs) else []
    publish_force = (
        ["--allow-overwrite"] if (force or overwrite_outputs) else []
    )
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
        Step(
            "00a_validate_production_source_seal",
            "stage_0",
            "00a_validate_machinery_production_source_seal.py",
            ["--asof", asof],
            pass_db=False,
        ),
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
        Step(
            "07c_recover_financial_lineage",
            "stage_4",
            "07c_recover_machinery_financial_lineage.py",
            ["--asof", asof],
            True,
        ),
        Step(
            "07b_sync_issuer_ir",
            "stage_4",
            "07b_sync_machinery_issuer_ir_disclosures.py",
            ["--asof", asof, "--allow-partial", *source_force_args],
            True,
        ),
        Step("11_sync_fx", "stage_4", "11_sync_machinery_fx_rates.py", ["--end-date", asof, "--allow-partial", *source_force_args], True),
        Step(
            "08b_scan_disclosures",
            "stage_4",
            "08b_audit_machinery_disclosure_candidates.py",
            ["--asof", asof, "--limit", "40", "--scan-cache", "--resume"],
        ),
        Step("08_build_financial", "stage_4", "08_build_machinery_financial_features.py", ["--asof", asof]),
        Step("08_validate_financial", "stage_4", "08_validate_machinery_financial_stage.py", ["--asof", asof]),
        Step(
            "08a_audit_special_metrics",
            "stage_4",
            "08a_audit_machinery_financial_metrics.py",
            ["--asof", asof],
        ),
        Step(
            "08b_audit_disclosures",
            "stage_4",
            "08b_audit_machinery_disclosure_candidates.py",
            ["--asof", asof, "--limit", "40"],
        ),
        Step(
            "08c_classify_recoverable_coverage",
            "stage_4",
            "08c_audit_machinery_recoverable_coverage.py",
            ["--asof", asof],
        ),
        Step(
            "08f_generate_lifecycle_candidates",
            "stage_4",
            "08f_generate_machinery_lifecycle_candidates.py",
            ["--asof", asof],
        ),
        Step(
            "08g_validate_lifecycle_policy",
            "stage_4",
            "08g_validate_machinery_lifecycle_policy.py",
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
        Step("06a_build_scoring", "stage_6", "06a_build_machinery_scoring_features.py", ["--asof", asof, *derived_force_args]),
        Step("06a_validate_scoring", "stage_6", "06a_validate_machinery_scoring_features.py", ["--asof", asof]),
        Step(
            "10_build_scores",
            "stage_7",
            "10_build_machinery_calibrated_scores.py",
            ["--asof", asof, *derived_force_args],
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
            "10c_lifecycle_shadow",
            "stage_10",
            "10c_build_machinery_lifecycle_shadow.py",
            ["--asof", asof],
            pass_db=False,
        ),
        Step(
            "20_validate_portfolio",
            "stage_10",
            "20_validate_machinery_portfolio_adapter.py",
            ["--asof", asof, "--expect-research-eligible"],
            pass_db=False,
        ),
    ]
    if include_dedicated_parser_shadow or include_dedicated_parser_production:
        insert_at = next(
            index
            for index, step in enumerate(steps)
            if step.step_id == "08_build_financial"
        )
        steps.insert(
            insert_at,
            Step(
                "08d_dedicated_parser_shadow",
                "stage_4",
                "08d_run_machinery_dedicated_parser_shadow.py",
                [
                    "--asof",
                    asof,
                    "--all-metrics",
                    *(
                        ["--require-complete-cache"]
                        if include_dedicated_parser_production
                        else []
                    ),
                ],
            ),
        )
        if include_dedicated_parser_production:
            steps.insert(
                insert_at + 1,
                Step(
                    "08e_dedicated_parser_production",
                    "stage_4",
                    "08e_promote_machinery_dedicated_parser.py",
                    ["--asof", asof],
                ),
            )
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
                    *derived_force_args,
                ],
            ),
        )
    return steps


def overwrite_tail_on_resume(*, force: bool, resume: bool) -> bool:
    """Return whether derived outputs may replace a prior partial tail."""

    return bool(force or resume)


def select_steps(steps: list[Step], args: argparse.Namespace) -> list[Step]:
    selected = list(steps)
    positions = {step.step_id: index for index, step in enumerate(steps)}
    skipped = {str(item).strip() for item in args.skip_step if str(item).strip()}
    unknown_skips = sorted(skipped - set(positions))
    if unknown_skips:
        raise ValueError(f"Unknown --skip-step values={unknown_skips}")
    if args.from_step and args.to_step and args.from_step in positions and args.to_step in positions:
        if positions[args.from_step] > positions[args.to_step]:
            raise ValueError(
                f"--from-step={args.from_step} occurs after --to-step={args.to_step}"
            )
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
    selected = [
        step
        for step in selected
        if step.step_id not in skipped and not (args.skip_network and step.network)
    ]
    if not selected:
        raise ValueError("Step selection is empty; refusing to publish a successful zero-step run")
    return selected



def coverage_audit(db_path: Path, asof: str) -> None:
    with sqlite3.connect(db_path) as conn:
        result = audit_industrials_source_coverage(
            conn,
            model_family="machinery",
            asof=asof,
        )
    print(
        f"[machinery_refresh] active_machinery_tickers={result.active_ticker_count}",
        flush=True,
    )
    for observation in result.observations:
        coverage = (
            f"active_tickers_on_{asof}={observation.active_tickers_on_asof}/"
            f"{result.active_ticker_count}"
            if observation.active_tickers_on_asof is not None
            else f"distinct_{observation.identity_column}_on_{asof}="
            f"{observation.distinct_identities_on_asof}"
        )
        print(
            f"[machinery_refresh] {observation.table}.{observation.date_column}: "
            f"max={observation.max_date} rows_on_{asof}={observation.rows_on_asof} "
            f"{coverage}",
            flush=True,
        )
    require_source_coverage(result)


def persist_orchestration_result(
    *,
    orchestration_root: Path,
    run_id: str,
    asof: str,
    db_path: Path,
    config_path: Path,
    dry_run: bool,
    latest_before_run: str,
    planned_step_count: int,
    report_rows: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    resume_start_step: str = "",
    resumed_from_run_id: str = "",
) -> dict[str, Any]:
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
    acceptance = (
        "DRY_RUN"
        if dry_run
        else "PASS"
        if not failures
        and len(report_rows) == planned_step_count
        and all(str(row.get("status") or "") == "PASS" for row in report_rows)
        else "FAIL"
    )
    summary = {
        "acceptance": acceptance,
        "run_id": run_id,
        "asof_date": asof,
        "database_path": str(db_path),
        "config_path": str(config_path),
        "dry_run": dry_run,
        "planned_step_count": planned_step_count,
        "resume_start_step": resume_start_step,
        "resumed_from_run_id": resumed_from_run_id,
        "completed_step_count": len(report_rows),
        "failed_step_count": len(failures),
        "previous_committed_asof": latest_before_run,
        "steps": report_rows,
    }
    runs_root = orchestration_root / "runs"
    write_csv_atomic(
        runs_root / f"{run_id}_steps.csv",
        fields,
        report_rows,
    )
    write_text_atomic(
        runs_root / f"{run_id}_manifest.json",
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
    )
    if acceptance == "PASS":
        write_csv_atomic(
            orchestration_root / "machinery_refresh_steps.csv",
            fields,
            report_rows,
        )
        write_text_atomic(
            orchestration_root / "machinery_refresh_manifest.json",
            json.dumps(summary, indent=2, sort_keys=True) + "\n",
        )
    else:
        write_text_atomic(
            orchestration_root / "machinery_refresh_last_attempt.json",
            json.dumps(summary, indent=2, sort_keys=True) + "\n",
        )
    return summary


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
        overwrite_outputs=overwrite_tail_on_resume(
            force=args.force, resume=args.resume
        ),
        include_norgate_backfill=args.include_norgate_backfill,
        refresh_sec_insider=not args.skip_sec_insider_refresh,
        full_positioning_refresh=args.full_positioning_refresh,
        bootstrap_sec_archives=args.bootstrap_sec_archives,
        include_dedicated_parser_shadow=args.include_dedicated_parser_shadow,
        include_dedicated_parser_production=(
            bool(
                cfg_get(
                    config,
                    "dedicated_parser.production_enabled",
                    False,
                )
            )
            and not args.skip_dedicated_parser_production
        ),
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
    resume_start_step = ""
    resumed_from_run_id = ""
    if args.resume:
        if (
            args.from_step
            or args.to_step
            or args.only
            or args.skip_step
            or args.skip_network
        ):
            raise ValueError(
                "--resume cannot be combined with manual step selection; "
                "--force is retained for overwrite-safe tail steps"
            )
        resume_plan = load_resume_plan(
            orchestration_root / "machinery_refresh_last_attempt.json",
            asof=asof,
            current_step_ids=[step.step_id for step in steps],
        )
        args.from_step = resume_plan.start_step
        resume_start_step = resume_plan.start_step
        resumed_from_run_id = resume_plan.source_run_id
    selected = select_steps(steps, args)
    has_parser_steps = any(
        step.step_id
        in {
            "08d_dedicated_parser_shadow",
            "08e_dedicated_parser_production",
        }
        for step in selected
    )
    dedicated_parser_python = (
        resolve_dedicated_parser_python(
            cli_value=args.dedicated_parser_python,
            config=config,
            base_dir=base_dir,
        )
        if has_parser_steps
        else None
    )
    if dedicated_parser_python is not None and not args.dry_run:
        validate_dedicated_parser_python(dedicated_parser_python)
    run_id = datetime.now(timezone.utc).strftime("machinery_refresh_%Y%m%dT%H%M%SZ")
    logs_dir = orchestration_root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    report_rows: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    latest_before_run = ""
    run_final_audit = any(step.step_id == "10b_publish" for step in selected)
    planned_step_count = len(selected) + int(run_final_audit)
    with RefreshLock(lock_path):
        if not args.dry_run:
            latest_before_run = latest_committed_asof(
                db_path=db_path,
                dashboard_root=dashboard_root,
                orchestration_root=orchestration_root,
            )
            validate_non_regressive_asof(
                requested_asof=asof,
                committed_asof=latest_before_run,
            )
        for index, step in enumerate(selected, start=1):
            script_path = PACKAGE_ROOT / "scripts" / step.script
            python_executable = (
                str(args.norgate_python.expanduser().resolve())
                if step.step_id == "15_norgate_backfill" and args.norgate_python is not None
                else str(dedicated_parser_python)
                if step.step_id
                in {
                    "08d_dedicated_parser_shadow",
                    "08e_dedicated_parser_production",
                }
                and dedicated_parser_python is not None
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
            print(f"[{index}/{planned_step_count}] {step.step_id}", flush=True)
            if args.dry_run:
                row.update({"status": "DRY_RUN", "return_code": "", "elapsed_sec": 0.0})
                report_rows.append(row)
                continue
            started = time.perf_counter()
            with log_path.open("w", encoding="utf-8", newline="") as log:
                result = subprocess.run(
                    command,
                    cwd=PROJECT_ROOT,
                    env={
                        **os.environ,
                        "INDUSTRIALS_REFRESH_LOCK_HELD": "1",
                    },
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
        if run_final_audit and not failures:
            audit_row: dict[str, Any] = {
                "run_id": run_id,
                "step_number": len(selected) + 1,
                "step_id": COVERAGE_AUDIT_STEP_ID,
                "stage": "stage_11",
                "script": "coverage_audit",
                "network_flag": 0,
                "command": f"coverage_audit(db={db_path}, asof={asof})",
                "log_path": "",
            }
            if args.dry_run:
                audit_row.update(
                    {"status": "DRY_RUN", "return_code": "", "elapsed_sec": 0.0}
                )
            else:
                started = time.perf_counter()
                try:
                    coverage_audit(db_path, asof)
                    audit_row.update({"status": "PASS", "return_code": 0})
                except Exception as exc:  # noqa: BLE001 - audit failures gate the run
                    audit_row.update(
                        {
                            "status": "FAIL",
                            "return_code": 1,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    failures.append(audit_row)
                audit_row["elapsed_sec"] = round(time.perf_counter() - started, 3)
            report_rows.append(audit_row)
        summary = persist_orchestration_result(
            orchestration_root=orchestration_root,
            run_id=run_id,
            asof=asof,
            db_path=db_path,
            resume_start_step=resume_start_step,
            resumed_from_run_id=resumed_from_run_id,
            config_path=config_path,
            dry_run=bool(args.dry_run),
            latest_before_run=latest_before_run,
            planned_step_count=planned_step_count,
            report_rows=report_rows,
            failures=failures,
        )
    print(json.dumps({key: summary[key] for key in ("acceptance", "run_id", "dry_run", "failed_step_count")}, indent=2))
    if not failures:
        return 0
    if any(row.get("return_code") == NON_RETRYABLE_POLICY_FAILURE for row in failures):
        return NON_RETRYABLE_POLICY_FAILURE
    return 1

if __name__ == "__main__":
    raise SystemExit(main())
