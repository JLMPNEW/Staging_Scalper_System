#!/usr/bin/env python3
"""Run the validated software-infrastructure refresh sequence.

The default path refreshes production inputs, rebuilds Stage 6/7 scores,
publishes reports/governance artifacts, and runs the existing validators.
Research diagnostics, Optuna calibration, portfolio backtests, walk-forward
calibration, and one-time Norgate backfill are opt-in so routine refreshes do
not accidentally change the model-review surface.
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from technology.core.positioning_window import resolve_positioning_window  # noqa: E402
from technology.core.refresh_orchestration import asof_governance_conflict  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
CONFIG_KEY = "software_infrastructure_refresh_pipeline"


@dataclass(frozen=True)
class Step:
    step_id: str
    stage: str
    description: str
    script: Path
    args: list[str] = field(default_factory=list)
    pass_db: bool = True
    network: bool = False
    research: bool = False
    optuna: bool = False
    norgate_backfill: bool = False
    blocking: bool = True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the software-infrastructure production refresh pipeline.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", default="", help="Production feature/score as-of date, YYYY-MM-DD. Defaults to each step's own default.")
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true", help="Print and record the planned sequence without running it.")
    parser.add_argument("--list-steps", action="store_true", help="List available step ids and exit.")
    parser.add_argument("--skip-network", action="store_true", help="Skip network/upstream refresh steps and rebuild from existing data.")
    parser.add_argument("--include-research", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--include-optuna", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--include-norgate-backfill", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--from-step", default="", help="Start at this step id, inclusive.")
    parser.add_argument("--to-step", default="", help="Stop at this step id, inclusive.")
    parser.add_argument("--only", default="", help="Comma-separated step ids to run.")
    parser.add_argument("--skip-step", action="append", default=[], help="Step id to skip. Can be repeated.")
    parser.add_argument("--skip-ibkr-borrow", action="store_true", help="Pass through to the upstream positioning sync.")
    parser.add_argument("--allow-stale-ibkr-borrow-on-error", action="store_true")
    parser.add_argument("--force-refresh", action="store_true", help="Force refresh for loaders that support it.")
    parser.add_argument("--refresh-sec-if-stale-hours", type=float, default=None, help="For current/as-of-today runs, refresh SEC submissions/companyfacts caches older than this many hours.")
    return parser.parse_args()


def py_script(relative: str) -> Path:
    return PROJECT_ROOT / relative


def build_steps(
    *,
    asof: str,
    skip_ibkr_borrow: bool,
    force_refresh: bool,
    financial_batch_size: int,
    financial_batch_timeout_sec: float,
    refresh_sec_if_stale_hours: float | None = None,
    allow_stale_ibkr_borrow_on_error: bool = False,
    positioning_lookback_days: int = 550,
    positioning_history_floor: str = "2013-01-01",
) -> list[Step]:
    asof_args = ["--asof", asof] if asof else []
    end_date_args = ["--end-date", asof] if asof else []
    refresh_args = ["--force-refresh"] if force_refresh else []
    sec_args = [
        *asof_args,
        "--force-submissions-refresh",
        "--current-members-only",
        *refresh_args,
    ]
    current_asof = not asof or asof == date.today().isoformat()
    if current_asof and refresh_sec_if_stale_hours and refresh_sec_if_stale_hours > 0:
        sec_args.extend(["--refresh-if-stale-hours", str(refresh_sec_if_stale_hours)])
    positioning_start, _ = resolve_positioning_window(
        asof=asof,
        configured_start=positioning_history_floor,
        lookback_days=positioning_lookback_days,
    )
    positioning_window_args = ["--history-start", positioning_start.isoformat()]
    positioning_args = [*positioning_window_args, *end_date_args]
    if skip_ibkr_borrow:
        positioning_args.append("--skip-ibkr-borrow")
    if allow_stale_ibkr_borrow_on_error:
        positioning_args.append("--allow-stale-ibkr-borrow-on-error")

    return [
        Step("00_init_db", "stage_1", "Initialize technology DB/schema/source registry", py_script("technology/scripts/00_init_technology_db.py")),
        Step("01_load_universe", "stage_2", "Load current software-infrastructure universe", py_script("technology/software_infrastructure/scripts/01_load_software_infrastructure_universe.py")),
        Step("02_validate_universe_initial", "stage_2", "Validate current software-infrastructure universe", py_script("technology/software_infrastructure/scripts/02_validate_software_infrastructure_universe.py")),
        Step("03_sync_prices", "stage_3", "Sync Yahoo adjusted prices and benchmarks", py_script("technology/software_infrastructure/scripts/03_sync_software_infrastructure_prices.py"), [*asof_args, *refresh_args], network=True),
        Step("01b_load_historical_membership", "stage_2", "Load PIT historical/current software-infrastructure membership after price availability", py_script("technology/software_infrastructure/scripts/01b_load_software_infrastructure_historical_membership.py")),
        Step("02_validate_universe", "stage_2", "Validate PIT software-infrastructure universe", py_script("technology/software_infrastructure/scripts/02_validate_software_infrastructure_universe.py")),
        Step("15_norgate_backfill", "stage_15", "Import Norgate delisted prices", py_script("technology/software_infrastructure/scripts/15_import_software_infrastructure_norgate_delisted_prices.py"), norgate_backfill=True),
        Step("05_build_market_features", "stage_3", "Build market technical features", py_script("technology/software_infrastructure/scripts/05_build_software_infrastructure_market_features.py"), asof_args),
        Step("06_validate_market", "stage_3", "Validate market stage", py_script("technology/software_infrastructure/scripts/06_validate_software_infrastructure_market_stage.py"), asof_args),
        Step("07_sync_sec_fundamentals", "stage_4", "Sync SEC submissions/companyfacts", py_script("technology/software_infrastructure/scripts/07_sync_software_infrastructure_sec_fundamentals.py"), sec_args, network=True),
        Step("07b_recover_6k_financials", "stage_4", "Recover cached foreign-filer 6-K financial facts", py_script("technology/scripts/07b_recover_technology_6k_financials.py"), ["--family", "software_infrastructure", *asof_args]),
        Step("11_sync_fx_rates", "stage_4", "Sync FX rates for non-USD reporters", py_script("technology/software_infrastructure/scripts/11_sync_software_infrastructure_fx_rates.py"), refresh_args, network=True),
        Step(
            "08_build_financial_features",
            "stage_4",
            "Build SEC financial features in recoverable sequential batches",
            py_script("technology/scripts/08_build_technology_financial_features_batched.py"),
            [
                "--current-members-only",
                "--model-family",
                "software_infrastructure",
                "--batch-size",
                str(financial_batch_size),
                "--batch-timeout-sec",
                str(financial_batch_timeout_sec),
            ],
        ),
        Step("12_sync_sec_ownership", "stage_5", "Sync direct SEC ownership filings", py_script("technology/software_infrastructure/scripts/12_sync_software_infrastructure_sec_ownership.py"), refresh_args, network=True),
        Step("13_sync_positioning_upstream", "stage_5", "Sync upstream 13F/FINRA/IBKR positioning feeds", py_script("technology/software_infrastructure/scripts/13_sync_software_infrastructure_positioning_upstream.py"), positioning_args, pass_db=False, network=True),
        Step("09_import_positioning", "stage_5", "Import positioning into technology.sqlite", py_script("technology/software_infrastructure/scripts/09_import_software_infrastructure_positioning.py"), [*asof_args, *positioning_window_args]),
        Step("10_validate_positioning", "stage_5", "Validate SEC/positioning stage", py_script("technology/software_infrastructure/scripts/10_validate_software_infrastructure_sec_positioning_stages.py")),
        Step("14_audit_form4_reconciliation", "stage_5", "Audit Form 4/direct ownership reconciliation", py_script("technology/software_infrastructure/scripts/14_audit_software_infrastructure_form4_reconciliation.py"), asof_args),
        Step("06a_build_scoring_contract", "stage_6a", "Build software-infrastructure scoring feature contract", py_script("technology/software_infrastructure/scripts/06a_build_software_infrastructure_scoring_features.py"), asof_args),
        Step("06a_validate_scoring_contract", "stage_6a", "Validate software-infrastructure scoring feature contract", py_script("technology/software_infrastructure/scripts/06a_validate_software_infrastructure_scoring_features.py"), asof_args),
        Step("06b_validate_overlay_closure", "stage_6b", "Validate deliberate neutral/no-overlay closure", py_script("technology/software_infrastructure/scripts/06b_validate_software_infrastructure_overlay_closure.py"), asof_args),
        Step("10_build_stage7_scores", "stage_7", "Build production calibrated scores", py_script("technology/software_infrastructure/scripts/10_build_software_infrastructure_calibrated_scores.py"), asof_args),
        Step("10_validate_stage7_scores", "stage_7", "Validate production calibrated scores", py_script("technology/software_infrastructure/scripts/10_validate_software_infrastructure_calibrated_scores.py"), asof_args),
        Step("07_signal_diagnostics", "research", "Run signal IC diagnostics", py_script("technology/software_infrastructure/scripts/07_run_software_infrastructure_signal_diagnostics.py"), research=True),
        Step("07_validate_signal_diagnostics", "research", "Validate signal diagnostics", py_script("technology/software_infrastructure/scripts/07_validate_software_infrastructure_signal_diagnostics.py"), pass_db=False, research=True),
        Step("08_run_optuna", "stage_8", "Run constrained Optuna calibration", py_script("technology/software_infrastructure/scripts/08_run_software_infrastructure_optuna_calibration.py"), optuna=True),
        Step("08_validate_optuna", "stage_8", "Validate Optuna calibration output", py_script("technology/software_infrastructure/scripts/08_validate_software_infrastructure_optuna_calibration.py"), optuna=True),
        Step("08c_walk_forward_calibration", "stage_8", "Run walk-forward calibration research", py_script("technology/software_infrastructure/scripts/08c_run_software_infrastructure_walk_forward_calibration.py"), optuna=True),
        Step("08c_validate_walk_forward", "stage_8", "Validate walk-forward calibration output", py_script("technology/software_infrastructure/scripts/08c_validate_software_infrastructure_walk_forward_calibration.py"), optuna=True),
        Step("09_portfolio_backtest", "stage_9", "Run portfolio backtest reports", py_script("technology/software_infrastructure/scripts/09_run_software_infrastructure_portfolio_backtest.py"), research=True),
        Step("09_validate_portfolio_backtest", "stage_9", "Validate portfolio backtest reports", py_script("technology/software_infrastructure/scripts/09_validate_software_infrastructure_portfolio_backtest.py"), pass_db=False, research=True),
        Step("10b_publish_dashboard", "stage_10", "Publish dashboard/static reports", py_script("technology/software_infrastructure/scripts/10b_publish_software_infrastructure_dashboard_reports.py")),
        Step("10c_financial_lineage_shadow", "stage_10_lineage", "Build blocking production financial-lineage sidecar", py_script("technology/scripts/10c_build_technology_financial_lineage_shadow.py"), ["--family", "software_infrastructure", "--policy-context", "production", *asof_args]),
        Step("10b_validate_dashboard", "stage_10", "Validate dashboard/static reports", py_script("technology/software_infrastructure/scripts/10b_validate_software_infrastructure_dashboard_reports.py"), pass_db=False),
        Step("16_publish_governance", "stage_10b", "Publish lockbox ledger and signal registry", py_script("technology/software_infrastructure/scripts/16_publish_software_infrastructure_lockbox_ledger.py")),
        Step("16_validate_governance", "stage_10b", "Validate lockbox ledger and signal registry", py_script("technology/software_infrastructure/scripts/16_validate_software_infrastructure_lockbox_ledger.py"), pass_db=False),
    ]


def step_index(steps: list[Step], step_id: str) -> int:
    for idx, step in enumerate(steps):
        if step.step_id == step_id:
            return idx
    raise ValueError(f"Unknown step id: {step_id}")


def selected_steps(steps: list[Step], args: argparse.Namespace, config: dict[str, Any]) -> list[Step]:
    include_research = bool(
        cfg_get(config, f"{CONFIG_KEY}.default_include_research", False)
        if args.include_research is None else args.include_research
    )
    include_optuna = bool(
        cfg_get(config, f"{CONFIG_KEY}.default_include_optuna", False)
        if args.include_optuna is None else args.include_optuna
    )
    include_norgate = bool(
        cfg_get(config, f"{CONFIG_KEY}.default_include_norgate_backfill", False)
        if args.include_norgate_backfill is None else args.include_norgate_backfill
    )

    out = list(steps)
    if args.from_step:
        out = out[step_index(out, args.from_step):]
    if args.to_step:
        idx = step_index(out, args.to_step)
        out = out[:idx + 1]
    if args.only:
        wanted = {item.strip() for item in args.only.split(",") if item.strip()}
        unknown = sorted(wanted.difference({step.step_id for step in steps}))
        if unknown:
            raise ValueError(f"Unknown --only step ids: {unknown}")
        out = [step for step in steps if step.step_id in wanted]
    explicit_only = bool(args.only.strip())
    skipped = {str(item).strip() for item in args.skip_step if str(item).strip()}
    out = [
        step for step in out
        if step.step_id not in skipped
        and (explicit_only or include_research or not step.research)
        and (explicit_only or include_optuna or not step.optuna)
        and (explicit_only or include_norgate or not step.norgate_backfill)
        and (not args.skip_network or not step.network)
    ]
    if include_optuna and not include_research:
        existing = {step.step_id for step in out}

        def insertable(step: Step) -> bool:
            return (
                step.step_id not in existing
                and step.step_id not in skipped
                and (not args.skip_network or not step.network)
            )

        research_steps = [step for step in steps if step.research]
        # Only the Stage 8A signal-diagnostics chain is a true prerequisite of
        # the Optuna steps; the Stage 9 backtest consumes Stage 8 outputs and
        # must stay positioned after them.
        diagnostics_steps = [step for step in research_steps if step.step_id.startswith("07_")]
        post_optuna_steps = [step for step in research_steps if not step.step_id.startswith("07_")]
        optuna_indices = [idx for idx, step in enumerate(out) if step.optuna]
        first_optuna_idx = (
            optuna_indices[0]
            if optuna_indices
            else max((idx for idx, step in enumerate(out) if step.stage == "stage_7"), default=-1) + 1
        )
        for step in reversed(diagnostics_steps):
            if insertable(step):
                out.insert(first_optuna_idx, step)
        after_optuna_idx = max((idx for idx, step in enumerate(out) if step.optuna), default=first_optuna_idx - 1) + 1
        for step in reversed(post_optuna_steps):
            if insertable(step):
                out.insert(after_optuna_idx, step)
    return out


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        if fields:
            writer.writeheader()
            writer.writerows(rows)


def command_for_step(step: Step, *, config_path: Path, db_path: Path | None) -> list[str]:
    cmd = [sys.executable, str(step.script), "--config", str(config_path)]
    if step.pass_db and db_path is not None:
        cmd.extend(["--db", str(db_path)])
    cmd.extend(step.args)
    return cmd


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else resolve_path(
        cfg_get(config, f"{CONFIG_KEY}.output_dir", "../output/technology_reports/software_infrastructure/orchestration"),
        base_dir=base_dir,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    logs_dir = output_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("software_infrastructure_refresh_%Y%m%dT%H%M%SZ")

    if args.refresh_sec_if_stale_hours is not None and args.refresh_sec_if_stale_hours < 0:
        raise SystemExit("--refresh-sec-if-stale-hours must be non-negative")
    steps = build_steps(
        asof=str(args.asof or "").strip(),
        skip_ibkr_borrow=bool(args.skip_ibkr_borrow),
        allow_stale_ibkr_borrow_on_error=bool(args.allow_stale_ibkr_borrow_on_error),
        force_refresh=bool(args.force_refresh),
        refresh_sec_if_stale_hours=(
            args.refresh_sec_if_stale_hours
            if args.refresh_sec_if_stale_hours is not None
            else float(cfg_get(config, "sec_fundamentals.refresh_if_stale_hours", 24.0))
        ),
        financial_batch_size=int(cfg_get(config, f"{CONFIG_KEY}.financial_feature_batch_size", 8)),
        financial_batch_timeout_sec=float(
            cfg_get(config, f"{CONFIG_KEY}.financial_feature_batch_timeout_sec", 1800.0)
        ),
        positioning_lookback_days=int(
            cfg_get(config, "positioning_import.incremental_lookback_days", 550)
        ),
        positioning_history_floor=str(
            cfg_get(config, "positioning_import.start_date", "2013-01-01")
        ),
    )
    if args.list_steps:
        for step in steps:
            flags = ",".join(flag for flag, enabled in [
                ("network", step.network),
                ("research", step.research),
                ("optuna", step.optuna),
                ("norgate", step.norgate_backfill),
            ] if enabled)
            print(f"{step.step_id}\t{step.stage}\t{flags}\t{step.description}")
        return 0

    planned = selected_steps(steps, args, config)
    governance_conflict = asof_governance_conflict(
        str(args.asof or ""),
        planned,
        publisher_script="16_publish_software_infrastructure_lockbox_ledger.py",
    )
    if governance_conflict:
        raise SystemExit(governance_conflict)
    rows: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    shadow_failures: list[dict[str, Any]] = []
    started = datetime.now(timezone.utc)

    for idx, step in enumerate(planned, start=1):
        cmd = command_for_step(step, config_path=config_path, db_path=db_path)
        log_path = logs_dir / f"{run_id}_{idx:02d}_{step.step_id}.log"
        row: dict[str, Any] = {
            "run_id": run_id,
            "step_number": idx,
            "step_id": step.step_id,
            "stage": step.stage,
            "description": step.description,
            "script": str(step.script),
            "network_flag": int(step.network),
            "research_flag": int(step.research),
            "optuna_flag": int(step.optuna),
            "norgate_backfill_flag": int(step.norgate_backfill),
            "pass_db_flag": int(step.pass_db),
            "blocking_flag": int(step.blocking),
            "command": " ".join(cmd),
            "log_path": str(log_path),
        }
        print(f"[{idx}/{len(planned)}] {step.step_id}: {step.description}")
        if args.dry_run:
            row.update({"status": "DRY_RUN", "return_code": "", "elapsed_sec": 0.0})
            rows.append(row)
            continue

        start = time.perf_counter()
        with log_path.open("w", encoding="utf-8", newline="") as log:
            log.write(f"run_id={run_id}\nstep={step.step_id}\nstarted_utc={datetime.now(timezone.utc).isoformat(timespec='seconds')}\n")
            log.write(f"command={' '.join(cmd)}\n\n")
            result = subprocess.run(cmd, cwd=PROJECT_ROOT, stdout=log, stderr=subprocess.STDOUT, text=True, check=False)
        elapsed = time.perf_counter() - start
        status = "PASS" if result.returncode == 0 else (
            "FAIL" if step.blocking else "SHADOW_FAIL"
        )
        row.update({"status": status, "return_code": result.returncode, "elapsed_sec": round(elapsed, 3)})
        rows.append(row)
        if result.returncode != 0:
            if step.blocking:
                failures.append(row)
                print(f"FAILED {step.step_id}; see {log_path}")
                if not args.continue_on_error:
                    break
            else:
                shadow_failures.append(row)
                print(f"SHADOW FAILED {step.step_id}; production continues; see {log_path}")

    ended = datetime.now(timezone.utc)
    summary = {
        "run_id": run_id,
        "started_at_utc": started.isoformat(timespec="seconds"),
        "ended_at_utc": ended.isoformat(timespec="seconds"),
        "dry_run": bool(args.dry_run),
        "asof": str(args.asof or "").strip(),
        "database_path": str(db_path),
        "config_path": str(config_path),
        "step_count": len(rows),
        "planned_step_count": len(planned),
        "failed_step_count": len(failures),
        "shadow_failed_step_count": len(shadow_failures),
        "status": "PASS" if not failures else "FAIL",
        "output_dir": str(output_dir),
        "manifest_json": str(output_dir / "software_infrastructure_refresh_manifest.json"),
        "manifest_csv": str(output_dir / "software_infrastructure_refresh_steps.csv"),
        "steps": rows,
    }
    manifest_json = output_dir / "software_infrastructure_refresh_manifest.json"
    manifest_csv = output_dir / "software_infrastructure_refresh_steps.csv"
    manifest_json.write_text(json.dumps(summary, indent=2, sort_keys=True, default=str), encoding="utf-8")
    write_csv(manifest_csv, rows)
    summary_fields = (
        "run_id",
        "status",
        "dry_run",
        "step_count",
        "failed_step_count",
        "shadow_failed_step_count",
        "output_dir",
    )
    print(
        json.dumps(
            {key: summary[key] for key in summary_fields},
            indent=2,
            sort_keys=True,
        )
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
