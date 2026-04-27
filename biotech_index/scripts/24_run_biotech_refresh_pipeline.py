#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import logging
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, resolve_path
from biotech_index.core.db import connect, init_db


LOGGER = logging.getLogger("run_biotech_refresh_pipeline")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


@dataclass(frozen=True)
class Step:
    name: str
    script: str
    args: tuple[str, ...] = ()
    supports_asof: bool = True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the biotech refresh pipeline with explicit delta/reconcile/backfill modes.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", type=str, default="", help="As-of date in YYYY-MM-DD. Defaults to UTC today.")
    parser.add_argument("--mode", choices=["daily_delta", "weekly_reconcile", "full_backfill"], default="daily_delta")
    parser.add_argument("--steps", type=str, default="", help="Optional comma-separated step names to run.")
    parser.add_argument("--skip-ctgov", action="store_true", help="Skip CTGov sync/link/audit upstream steps.")
    parser.add_argument("--skip-ib", action="store_true", help="Skip the IB market-data step.")
    parser.add_argument("--skip-analyze", action="store_true", help="Skip SQLite ANALYZE at the end.")
    return parser.parse_args()


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)sZ %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    logging.Formatter.converter = time.gmtime


def pipeline_steps(mode: str, *, skip_ctgov: bool, skip_ib: bool) -> list[Step]:
    sec_event_args: tuple[str, ...] = ("--full-rescan",) if mode in {"weekly_reconcile", "full_backfill"} else ()
    companyfacts_args: tuple[str, ...] = ("--full-refresh",) if mode == "full_backfill" else ()
    forward_args: tuple[str, ...] = ("--run-mode", mode)
    steps = [
        Step("company_master", "02_build_company_master.py", supports_asof=False),
    ]
    if not skip_ctgov:
        steps.extend(
            [
                Step("ctgov_trials", "03_sync_ctgov_trials.py"),
                Step("trial_links", "04_link_trials_to_companies.py", supports_asof=False),
                Step("ctgov_audit", "05_audit_ctgov_trial_links.py"),
            ]
        )
    steps.extend(
        [
            Step("sec_filings", "06_sync_sec_filings.py"),
            Step("sec_events", "07_parse_sec_biotech_events.py", sec_event_args),
            Step("sec_companyfacts", "15_sync_sec_companyfacts_history.py", companyfacts_args),
            Step("financial_survival", "16_build_financial_survival_features.py"),
        ]
    )
    if not skip_ib:
        steps.append(Step("ib_market", "17_sync_market_data_ib.py"))
    steps.extend(
        [
            Step("commercial_value", "18_build_commercial_value_features.py"),
            Step("forward_guidance", "19_parse_forward_guidance.py", forward_args),
            Step("governance_events", "20_build_governance_event_features.py"),
            Step("biotech_features", "10_build_biotech_features.py"),
            Step("biotech_scores", "11_score_biotech_index.py"),
            Step("biotech_reports", "12_publish_biotech_reports.py"),
            Step("multibagger_features", "21_build_multibagger_features.py"),
            Step("multibagger_scores", "22_score_multibagger_candidates.py"),
            Step("multibagger_reports", "23_publish_multibagger_report.py"),
        ]
    )
    return steps


def write_timing_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["run_started_at", "mode", "step", "status", "elapsed_sec", "returncode", "command"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def build_step_command(
    step: Step,
    *,
    config_path: Path,
    db_path: Path,
    asof: str,
) -> list[str]:
    script_path = Path(__file__).resolve().with_name(step.script)
    cmd = [sys.executable, str(script_path), "--config", str(config_path), "--db", str(db_path), *step.args]
    if asof and step.supports_asof:
        cmd.extend(["--asof", asof])
    return cmd


def run_step(
    step: Step,
    *,
    command: list[str],
    mode: str,
    run_started_at: str,
) -> dict[str, Any]:
    start = time.monotonic()
    LOGGER.info("Starting %s", step.name)
    completed = subprocess.run(command, cwd=str(PROJECT_ROOT))
    elapsed = round(time.monotonic() - start, 3)
    status = "success" if completed.returncode == 0 else "failed"
    LOGGER.info("Finished %s status=%s elapsed=%.3fs", step.name, status, elapsed)
    return {
        "run_started_at": run_started_at,
        "mode": mode,
        "step": step.name,
        "status": status,
        "elapsed_sec": elapsed,
        "returncode": completed.returncode,
        "command": " ".join(command),
    }


def analyze_db(db_path: Path, *, run_started_at: str, mode: str) -> dict[str, Any]:
    start = time.monotonic()
    LOGGER.info("Starting sqlite_analyze")
    with connect(db_path) as conn:
        init_db(conn)
        conn.execute("ANALYZE")
        conn.commit()
    elapsed = round(time.monotonic() - start, 3)
    LOGGER.info("Finished sqlite_analyze status=success elapsed=%.3fs", elapsed)
    return {
        "run_started_at": run_started_at,
        "mode": mode,
        "step": "sqlite_analyze",
        "status": "success",
        "elapsed_sec": elapsed,
        "returncode": 0,
        "command": f"ANALYZE {db_path}",
    }


def main() -> None:
    configure_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    asof = args.asof or datetime.now(timezone.utc).date().isoformat()
    timing_csv = resolve_path(
        cfg_get(config, "biotech_refresh.timing_csv", "../output/biotech_index_reports/biotech_refresh_timing.csv"),
        base_dir=base_dir,
    )
    selected_steps = {step.strip() for step in args.steps.split(",") if step.strip()}
    steps = [
        step
        for step in pipeline_steps(args.mode, skip_ctgov=args.skip_ctgov, skip_ib=args.skip_ib)
        if not selected_steps or step.name in selected_steps
    ]
    if selected_steps:
        known = {step.name for step in pipeline_steps(args.mode, skip_ctgov=args.skip_ctgov, skip_ib=args.skip_ib)}
        unknown = sorted(selected_steps - known)
        if unknown:
            raise ValueError(f"Unknown pipeline step(s): {', '.join(unknown)}")

    run_started_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    timing_rows: list[dict[str, Any]] = []
    try:
        for step in steps:
            command = build_step_command(step, config_path=config_path, db_path=db_path, asof=asof)
            row = {
                "run_started_at": run_started_at,
                "mode": args.mode,
                "step": step.name,
                "status": "running",
                "elapsed_sec": "",
                "returncode": "",
                "command": " ".join(command),
            }
            timing_rows.append(row)
            write_timing_csv(timing_csv, timing_rows)
            timing_rows[-1] = run_step(step, command=command, mode=args.mode, run_started_at=run_started_at)
            write_timing_csv(timing_csv, timing_rows)
            if timing_rows[-1]["status"] != "success":
                raise SystemExit(int(timing_rows[-1]["returncode"]) or 1)
        if not args.skip_analyze:
            timing_rows.append(analyze_db(db_path, run_started_at=run_started_at, mode=args.mode))
            write_timing_csv(timing_csv, timing_rows)
    finally:
        write_timing_csv(timing_csv, timing_rows)


if __name__ == "__main__":
    main()
