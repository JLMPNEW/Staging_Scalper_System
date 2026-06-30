#!/usr/bin/env python3
"""Backfill dated technology dashboard/report snapshots from local data only.

This runner intentionally excludes upstream refresh steps. It does not call SEC,
FINRA, IBKR, Yahoo, or Norgate. For each target as-of date it rebuilds local
market and positioning feature snapshots, Stage 6A scoring inputs, Stage 7
scores, and Stage 10 dashboard snapshots.

PowerShell treats unquoted comma-separated native-command arguments in ways
that can interact badly with sandbox/approval wrappers. Use repeatable --family
and --date arguments for selective runs, or quote comma-separated values.
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import json
import runpy
import sqlite3
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from technology.core.oos_provenance import parse_iso_date, validate_oos_rank_rows  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_START_DATE = "2019-01-04"
DEFAULT_CALENDAR_TICKER = "QQQ"
MARKET_SOURCE_ID = "yahoo_finance_adjusted"
RUNPY_TRAMPOLINE = "import runpy, sys; script=sys.argv[1]; sys.argv=[script]+sys.argv[2:]; runpy.run_path(script, run_name='__main__')"


@dataclass(frozen=True)
class StepSpec:
    step_id: str
    script: Path
    extra_args: tuple[str, ...] = ()
    pass_db: bool = True
    pass_config: bool = True
    pass_asof: bool = True
    historical_mode: bool = False
    output_dir_from_snapshot: bool = False


@dataclass(frozen=True)
class FamilySpec:
    family: str
    aliases: tuple[str, ...]
    dashboard_dir_key: str
    dashboard_dir_default: str
    rank_filename: str
    manifest_filename: str
    steps: tuple[StepSpec, ...]
    restore_steps: tuple[StepSpec, ...]


def script(relative: str) -> Path:
    return PROJECT_ROOT / relative


FAMILIES: dict[str, FamilySpec] = {}


def register(spec: FamilySpec) -> None:
    for alias in (spec.family, *spec.aliases):
        FAMILIES[alias.lower()] = spec


SEMICONDUCTOR_STEPS = (
    StepSpec(
        "05_build_market_features",
        script("technology/scripts/05_build_technology_market_features.py"),
        ("--model-family", "semiconductors", "--benchmark-tickers", "SMH,SOXX,QQQ,SPY"),
    ),
    StepSpec("09_import_positioning", script("technology/scripts/09_import_technology_positioning.py"), ("--model-family", "semiconductors")),
    StepSpec("06a_build_scoring_contract", script("technology/semiconductors/scripts/06a_build_semiconductor_scoring_features.py")),
    StepSpec("06a_validate_scoring_contract", script("technology/semiconductors/scripts/06a_validate_semiconductor_scoring_features.py"), historical_mode=True),
    StepSpec("06b_build_sector_cycle", script("technology/semiconductors/scripts/06b_build_sector_cycle_features.py")),
    StepSpec("06b_build_big_tech_capex", script("technology/semiconductors/scripts/06b_build_big_tech_capex_features.py")),
    StepSpec("06b_apply_overlays", script("technology/semiconductors/scripts/06b_apply_semiconductor_overlay_scores.py")),
    StepSpec("06b_validate_overlays", script("technology/semiconductors/scripts/06b_validate_semiconductor_overlays.py")),
    StepSpec("10_build_stage7_scores", script("technology/semiconductors/scripts/10_build_semiconductor_calibrated_scores.py")),
    StepSpec("10_validate_stage7_scores", script("technology/semiconductors/scripts/10_validate_semiconductor_calibrated_scores.py"), historical_mode=True),
    StepSpec(
        "10b_publish_dashboard",
        script("technology/semiconductors/scripts/10b_publish_semiconductor_dashboard_reports.py"),
        historical_mode=True,
    ),
    StepSpec(
        "10b_validate_dashboard_snapshot",
        script("technology/semiconductors/scripts/10b_validate_semiconductor_dashboard_reports.py"),
        historical_mode=True,
        pass_db=False,
        output_dir_from_snapshot=True,
    ),
)

SEMICONDUCTOR_RESTORE_STEPS = (
    StepSpec("10b_restore_current_dashboard", script("technology/semiconductors/scripts/10b_publish_semiconductor_dashboard_reports.py")),
    StepSpec("10b_validate_current_dashboard", script("technology/semiconductors/scripts/10b_validate_semiconductor_dashboard_reports.py"), pass_db=False),
)

HARDWARE_STEPS = (
    StepSpec("05_build_market_features", script("technology/technology_hardware/scripts/05_build_technology_hardware_market_features.py")),
    StepSpec("09_import_positioning", script("technology/technology_hardware/scripts/09_import_technology_hardware_positioning.py")),
    StepSpec("06a_build_scoring_contract", script("technology/technology_hardware/scripts/06a_build_technology_hardware_scoring_features.py")),
    StepSpec("06a_validate_scoring_contract", script("technology/technology_hardware/scripts/06a_validate_technology_hardware_scoring_features.py"), historical_mode=True),
    StepSpec("06b_validate_overlay_closure", script("technology/technology_hardware/scripts/06b_validate_technology_hardware_overlay_closure.py")),
    StepSpec("10_build_stage7_scores", script("technology/technology_hardware/scripts/10_build_technology_hardware_calibrated_scores.py")),
    StepSpec("10_validate_stage7_scores", script("technology/technology_hardware/scripts/10_validate_technology_hardware_calibrated_scores.py"), historical_mode=True),
    StepSpec("10_build_stage7_challenger", script("technology/technology_hardware/scripts/10_build_technology_hardware_stage7_challenger_scores.py")),
    StepSpec("10_validate_stage7_challenger", script("technology/technology_hardware/scripts/10_validate_technology_hardware_stage7_challenger_scores.py"), historical_mode=True),
    StepSpec(
        "10b_publish_dashboard",
        script("technology/technology_hardware/scripts/10b_publish_technology_hardware_dashboard_reports.py"),
        historical_mode=True,
    ),
    StepSpec(
        "10b_validate_dashboard_snapshot",
        script("technology/technology_hardware/scripts/10b_validate_technology_hardware_dashboard_reports.py"),
        historical_mode=True,
        pass_db=False,
        output_dir_from_snapshot=True,
    ),
)

HARDWARE_RESTORE_STEPS = (
    StepSpec("10b_restore_current_dashboard", script("technology/technology_hardware/scripts/10b_publish_technology_hardware_dashboard_reports.py")),
    StepSpec("10b_validate_current_dashboard", script("technology/technology_hardware/scripts/10b_validate_technology_hardware_dashboard_reports.py"), pass_db=False),
)

SOFTWARE_STEPS = (
    StepSpec("05_build_market_features", script("technology/software_infrastructure/scripts/05_build_software_infrastructure_market_features.py")),
    StepSpec("09_import_positioning", script("technology/software_infrastructure/scripts/09_import_software_infrastructure_positioning.py")),
    StepSpec("06a_build_scoring_contract", script("technology/software_infrastructure/scripts/06a_build_software_infrastructure_scoring_features.py")),
    StepSpec("06a_validate_scoring_contract", script("technology/software_infrastructure/scripts/06a_validate_software_infrastructure_scoring_features.py"), historical_mode=True),
    StepSpec("06b_validate_overlay_closure", script("technology/software_infrastructure/scripts/06b_validate_software_infrastructure_overlay_closure.py")),
    StepSpec("10_build_stage7_scores", script("technology/software_infrastructure/scripts/10_build_software_infrastructure_calibrated_scores.py")),
    StepSpec("10_validate_stage7_scores", script("technology/software_infrastructure/scripts/10_validate_software_infrastructure_calibrated_scores.py"), historical_mode=True),
    StepSpec("10_build_stage7_challenger", script("technology/software_infrastructure/scripts/10_build_software_infrastructure_stage7_challenger_scores.py")),
    StepSpec("10_validate_stage7_challenger", script("technology/software_infrastructure/scripts/10_validate_software_infrastructure_stage7_challenger_scores.py"), historical_mode=True),
    StepSpec(
        "10b_publish_dashboard",
        script("technology/software_infrastructure/scripts/10b_publish_software_infrastructure_dashboard_reports.py"),
        historical_mode=True,
    ),
    StepSpec(
        "10b_validate_dashboard_snapshot",
        script("technology/software_infrastructure/scripts/10b_validate_software_infrastructure_dashboard_reports.py"),
        historical_mode=True,
        pass_db=False,
        output_dir_from_snapshot=True,
    ),
)

SOFTWARE_RESTORE_STEPS = (
    StepSpec("10b_restore_current_dashboard", script("technology/software_infrastructure/scripts/10b_publish_software_infrastructure_dashboard_reports.py")),
    StepSpec("10b_validate_current_dashboard", script("technology/software_infrastructure/scripts/10b_validate_software_infrastructure_dashboard_reports.py"), pass_db=False),
)


register(
    FamilySpec(
        family="semiconductors",
        aliases=("semis", "semi", "semiconductor"),
        dashboard_dir_key="semiconductor_dashboard_reports.output_dir",
        dashboard_dir_default="../output/technology_reports/semi_dashboard",
        rank_filename="semiconductor_final_rank_table.csv",
        manifest_filename="semiconductor_dashboard_manifest.json",
        steps=SEMICONDUCTOR_STEPS,
        restore_steps=SEMICONDUCTOR_RESTORE_STEPS,
    )
)
register(
    FamilySpec(
        family="technology_hardware",
        aliases=("hardware", "tech_hardware"),
        dashboard_dir_key="technology_hardware_dashboard_reports.output_dir",
        dashboard_dir_default="../output/technology_reports/technology_hardware/dashboard",
        rank_filename="technology_hardware_final_rank_table.csv",
        manifest_filename="technology_hardware_dashboard_manifest.json",
        steps=HARDWARE_STEPS,
        restore_steps=HARDWARE_RESTORE_STEPS,
    )
)
register(
    FamilySpec(
        family="software_infrastructure",
        aliases=("software", "software_infra", "software-infrastructure"),
        dashboard_dir_key="software_infrastructure_dashboard_reports.output_dir",
        dashboard_dir_default="../output/technology_reports/software_infrastructure/dashboard",
        rank_filename="software_infrastructure_final_rank_table.csv",
        manifest_filename="software_infrastructure_dashboard_manifest.json",
        steps=SOFTWARE_STEPS,
        restore_steps=SOFTWARE_RESTORE_STEPS,
    )
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill historical technology dashboard snapshots from local data only.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--start-date", default=DEFAULT_START_DATE)
    parser.add_argument("--end-date", default="", help="Defaults to the latest available calendar-ticker price date.")
    parser.add_argument("--dates", default="", help="Comma-separated explicit as-of dates. Overrides start/end/frequency.")
    parser.add_argument("--date", action="append", default=[], help="Repeatable explicit as-of date. Avoids shell comma ambiguity.")
    parser.add_argument("--frequency", choices=("panel21", "daily"), default="panel21")
    parser.add_argument(
        "--families",
        default="semiconductors,technology_hardware,software_infrastructure",
        help="Comma-separated families: semiconductors, technology_hardware, software_infrastructure.",
    )
    parser.add_argument("--family", action="append", default=[], help="Repeatable family selector. Avoids shell comma ambiguity.")
    parser.add_argument("--calendar-ticker", default=DEFAULT_CALENDAR_TICKER)
    parser.add_argument("--step-timeout-sec", type=int, default=1800)
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument(
        "--require-oos-score-valid",
        action="store_true",
        help=(
            "Require strict OOS scores for dates on/after each family's production start. "
            "Pre-production dates remain PIT calibration inputs and log a strict-check skip."
        ),
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-restore-latest-root", action="store_true")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Backfill run manifest directory. Defaults to output/technology_reports/historical_backfill.",
    )
    parser.add_argument("--log-file", type=Path, default=None, help="Write runner console output to this file without shell redirection.")
    return parser.parse_args()


def parse_date_text(raw: str) -> str:
    text = str(raw or "").strip()[:10]
    datetime.strptime(text, "%Y-%m-%d")
    return text


def split_cli_values(raw: str | list[str] | tuple[str, ...]) -> list[str]:
    chunks = list(raw) if isinstance(raw, (list, tuple)) else [str(raw or "")]
    return [item.strip() for chunk in chunks for item in str(chunk or "").split(",") if item.strip()]


def unique_specs(raw: str | list[str] | tuple[str, ...]) -> list[FamilySpec]:
    out: list[FamilySpec] = []
    seen: set[str] = set()
    for item in split_cli_values(raw):
        key = item.lower()
        if key not in FAMILIES:
            raise ValueError(f"Unknown family {item!r}; valid families are semiconductors, technology_hardware, software_infrastructure")
        spec = FAMILIES[key]
        if spec.family not in seen:
            out.append(spec)
            seen.add(spec.family)
    if not out:
        raise ValueError("At least one family is required.")
    return out


def connect_ro(db_path: Path) -> sqlite3.Connection:
    last_error: sqlite3.OperationalError | None = None
    for attempt in range(3):
        try:
            conn = sqlite3.connect(str(db_path.expanduser().resolve()), timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            return conn
        except sqlite3.OperationalError as exc:
            last_error = exc
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"Unable to open technology database for read-only calendar lookup: {db_path}") from last_error


def calendar_dates(db_path: Path, *, ticker: str, start_date: str, end_date: str, frequency: str) -> list[str]:
    with connect_ro(db_path) as conn:
        if not end_date:
            row = conn.execute(
                "SELECT MAX(bar_date) FROM fact_price_ohlcv WHERE source_id = ? AND ticker = ?",
                (MARKET_SOURCE_ID, ticker),
            ).fetchone()
            end_date = str(row[0] or "")
        if not end_date:
            raise ValueError(f"No calendar prices found for ticker={ticker}")
        start_date = parse_date_text(start_date)
        end_date = parse_date_text(end_date)
        rows = conn.execute(
            """
            SELECT DISTINCT bar_date
            FROM fact_price_ohlcv
            WHERE source_id = ?
              AND ticker = ?
              AND bar_date BETWEEN ? AND ?
            ORDER BY bar_date
            """,
            (MARKET_SOURCE_ID, ticker, start_date, end_date),
        ).fetchall()
    dates = [str(row[0]) for row in rows]
    if not dates:
        raise ValueError(f"No trading dates found for {ticker} between {start_date} and {end_date}")
    if frequency == "daily":
        return dates
    selected = dates[::21]
    if selected[-1] != dates[-1]:
        selected.append(dates[-1])
    return selected


def explicit_dates(raw: str | list[str] | tuple[str, ...]) -> list[str]:
    dates = [parse_date_text(item) for item in split_cli_values(raw)]
    return sorted(dict.fromkeys(dates))


def dashboard_dir(config: dict[str, Any], base_dir: Path, spec: FamilySpec) -> Path:
    return resolve_path(cfg_get(config, spec.dashboard_dir_key, spec.dashboard_dir_default), base_dir=base_dir)


def latest_current_asof(db_path: Path, spec: FamilySpec) -> str:
    source_by_family = {
        "semiconductors": "semiconductor_calibrated_score_v1",
        "technology_hardware": "technology_hardware_calibrated_score_v1",
        "software_infrastructure": "software_infrastructure_calibrated_score_v1",
    }
    with connect_ro(db_path) as conn:
        row = conn.execute(
            """
            SELECT MAX(asof_date)
            FROM feature_scoring_model_output
            WHERE model_family = ? AND source_id = ?
            """,
            (spec.family, source_by_family[spec.family]),
        ).fetchone()
    return str(row[0] or "")


def command_for_step(
    *,
    python_exe: str,
    step: StepSpec,
    config_path: Path,
    db_path: Path | None,
    asof: str,
    snapshot_dir: Path | None = None,
) -> list[str]:
    cmd = [python_exe, "-c", RUNPY_TRAMPOLINE, str(step.script)]
    if step.pass_config:
        cmd.extend(["--config", str(config_path)])
    if step.pass_db and db_path is not None:
        cmd.extend(["--db", str(db_path)])
    if step.pass_asof:
        cmd.extend(["--asof", asof])
    if step.historical_mode:
        cmd.append("--historical-mode")
    if step.output_dir_from_snapshot:
        if snapshot_dir is None:
            raise ValueError(f"Step {step.step_id} requires snapshot_dir")
        cmd.extend(["--output-dir", str(snapshot_dir)])
    cmd.extend(step.extra_args)
    return cmd


def run_command(cmd: list[str], *, timeout_sec: int, dry_run: bool) -> tuple[int, str, str]:
    if dry_run:
        print("DRY-RUN", " ".join(cmd))
        return 0, "", ""
    if len(cmd) >= 4 and cmd[1] == "-c" and cmd[2] == RUNPY_TRAMPOLINE:
        script_path = cmd[3]
        old_argv = sys.argv[:]
        try:
            sys.argv = [script_path, *cmd[4:]]
            runpy.run_path(script_path, run_name="__main__")
            return 0, "", ""
        except SystemExit as exc:
            code = exc.code
            if code in (None, 0):
                return 0, "", ""
            if isinstance(code, int):
                return code, "", ""
            return 1, "", str(code)
        except BaseException:  # noqa: BLE001 - runner records the full child failure
            return 1, "", traceback.format_exc()
        finally:
            sys.argv = old_argv
    proc = subprocess.run(
        cmd,
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        timeout=timeout_sec,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def tail(text: str, lines: int = 20) -> str:
    parts = [line for line in str(text or "").splitlines() if line.strip()]
    return "\n".join(parts[-lines:])


def validate_snapshot_files(
    snapshot_dir: Path,
    spec: FamilySpec,
    asof: str,
    *,
    historical_mode: bool,
    require_oos_score: bool = False,
) -> None:
    manifest_path = snapshot_dir / spec.manifest_filename
    rank_path = snapshot_dir / spec.rank_filename
    if not manifest_path.exists():
        raise RuntimeError(f"Missing snapshot manifest: {manifest_path}")
    if not rank_path.exists() or rank_path.stat().st_size == 0:
        raise RuntimeError(f"Missing or empty snapshot rank table: {rank_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if str(manifest.get("asof_date") or "") != asof:
        raise RuntimeError(f"{spec.family} manifest asof mismatch: {manifest.get('asof_date')} vs {asof}")
    if historical_mode and str(manifest.get("report_mode") or "") != "historical":
        raise RuntimeError(f"{spec.family} manifest report_mode mismatch: {manifest.get('report_mode')} vs historical")
    if historical_mode:
        if str(manifest.get("non_point_in_time_sections") or "") != "omitted":
            raise RuntimeError(f"{spec.family} historical manifest did not omit non-PIT sections.")
        if str(manifest.get("calibration_input_valid_flag") or "") != "1":
            raise RuntimeError(f"{spec.family} historical manifest calibration_input_valid_flag is not 1.")
        if not str(manifest.get("oos_assertion_basis") or "").strip():
            raise RuntimeError(f"{spec.family} historical manifest missing oos_assertion_basis.")
        effective_require_oos_score = require_oos_score
        asof_date = parse_iso_date(asof)
        production_start = parse_iso_date(manifest.get("calibration_production_start_date"))
        if require_oos_score and asof_date is not None and production_start is not None and asof_date < production_start:
            effective_require_oos_score = False
            print(
                f"[{asof}][{spec.family}][oos] strict OOS score check skipped before "
                f"production_start={production_start.isoformat()}; PIT calibration-input validation still enforced."
            )
        if effective_require_oos_score and str(manifest.get("oos_score_valid_flag") or "") != "1":
            raise RuntimeError(f"{spec.family} historical manifest oos_score_valid_flag is not 1: {manifest.get('oos_invalid_reason')}")
    else:
        effective_require_oos_score = require_oos_score
    with rank_path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise RuntimeError(f"Snapshot rank table has no rows: {rank_path}")
    bad_dates = sorted({str(row.get("asof_date") or "") for row in rows if str(row.get("asof_date") or "") != asof})
    if bad_dates:
        raise RuntimeError(f"{spec.family} rank table contains rows outside {asof}: {bad_dates[:5]}")
    oos_errors = validate_oos_rank_rows(rows, asof=asof, historical_mode=historical_mode, require_oos_score=effective_require_oos_score)
    if oos_errors:
        raise RuntimeError(f"{spec.family} OOS snapshot validation failed: {'; '.join(oos_errors)}")


def write_run_report(output_dir: Path, rows: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = output_dir / f"technology_historical_dashboard_backfill_{stamp}.json"
    csv_path = output_dir / f"technology_historical_dashboard_backfill_{stamp}.csv"
    json_path.write_text(json.dumps({"summary": summary, "steps": rows}, indent=2, sort_keys=True), encoding="utf-8")
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields or ["empty"], extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps({"summary": summary, "json_report": str(json_path), "csv_report": str(csv_path)}, indent=2, sort_keys=True))


def run_with_args(args: argparse.Namespace) -> int:
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    command_db_path = args.db.expanduser().resolve() if args.db else None
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else PROJECT_ROOT / "output" / "technology_reports" / "historical_backfill"
    families = unique_specs(args.family if args.family else args.families)
    explicit_date_values = args.date if args.date else args.dates
    dates = explicit_dates(explicit_date_values) if explicit_date_values else calendar_dates(
        db_path,
        ticker=str(args.calendar_ticker).strip().upper(),
        start_date=args.start_date,
        end_date=args.end_date,
        frequency=args.frequency,
    )
    if not dates:
        raise ValueError("No target dates selected.")

    rows: list[dict[str, Any]] = []
    failures = 0
    print(
        f"Historical dashboard backfill: dates={len(dates)} {dates[0]}..{dates[-1]} "
        f"frequency={args.frequency} families={','.join(spec.family for spec in families)}"
    )
    for asof in dates:
        for spec in families:
            root_dir = dashboard_dir(config, base_dir, spec)
            snapshot_dir = root_dir / asof
            for step in spec.steps:
                cmd = command_for_step(
                    python_exe=sys.executable,
                    step=step,
                    config_path=config_path,
                    db_path=command_db_path,
                    asof=asof,
                    snapshot_dir=snapshot_dir if step.output_dir_from_snapshot else None,
                )
                print(f"[{asof}][{spec.family}][{step.step_id}]")
                try:
                    code, stdout, stderr = run_command(cmd, timeout_sec=args.step_timeout_sec, dry_run=bool(args.dry_run))
                except subprocess.TimeoutExpired as exc:
                    code, stdout, stderr = 124, str(exc.stdout or ""), str(exc.stderr or f"Timed out after {args.step_timeout_sec}s")
                row = {
                    "asof_date": asof,
                    "family": spec.family,
                    "step_id": step.step_id,
                    "returncode": code,
                    "command": " ".join(cmd),
                    "stdout_tail": tail(stdout),
                    "stderr_tail": tail(stderr),
                }
                rows.append(row)
                if code != 0:
                    failures += 1
                    print(row["stderr_tail"] or row["stdout_tail"])
                    if not args.continue_on_error:
                        summary = {"status": "FAIL", "target_dates": len(dates), "families": [s.family for s in families], "failures": failures}
                        write_run_report(output_dir, rows, summary)
                        return 1
            if not args.dry_run:
                try:
                    validate_snapshot_files(
                        snapshot_dir,
                        spec,
                        asof,
                        historical_mode=True,
                        require_oos_score=bool(args.require_oos_score_valid),
                    )
                    rows.append({"asof_date": asof, "family": spec.family, "step_id": "snapshot_file_validation", "returncode": 0})
                except Exception as exc:  # noqa: BLE001 - report and optionally continue
                    failures += 1
                    rows.append(
                        {
                            "asof_date": asof,
                            "family": spec.family,
                            "step_id": "snapshot_file_validation",
                            "returncode": 1,
                            "stderr_tail": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    print(f"{type(exc).__name__}: {exc}")
                    if not args.continue_on_error:
                        summary = {"status": "FAIL", "target_dates": len(dates), "families": [s.family for s in families], "failures": failures}
                        write_run_report(output_dir, rows, summary)
                        return 1

    if not args.no_restore_latest_root:
        for spec in families:
            current_asof = latest_current_asof(db_path, spec)
            if not current_asof:
                continue
            root_dir = dashboard_dir(config, base_dir, spec)
            for step in spec.restore_steps:
                cmd = command_for_step(
                    python_exe=sys.executable,
                    step=step,
                    config_path=config_path,
                    db_path=command_db_path,
                    asof=current_asof,
                    snapshot_dir=None,
                )
                print(f"[restore-current-root][{spec.family}][{step.step_id}][{current_asof}]")
                try:
                    code, stdout, stderr = run_command(cmd, timeout_sec=args.step_timeout_sec, dry_run=bool(args.dry_run))
                except subprocess.TimeoutExpired as exc:
                    code, stdout, stderr = 124, str(exc.stdout or ""), str(exc.stderr or f"Timed out after {args.step_timeout_sec}s")
                rows.append(
                    {
                        "asof_date": current_asof,
                        "family": spec.family,
                        "step_id": step.step_id,
                        "returncode": code,
                        "command": " ".join(cmd),
                        "stdout_tail": tail(stdout),
                        "stderr_tail": tail(stderr),
                    }
                )
                if code != 0:
                    failures += 1
                    print(tail(stderr) or tail(stdout))
                    if not args.continue_on_error:
                        summary = {"status": "FAIL", "target_dates": len(dates), "families": [s.family for s in families], "failures": failures}
                        write_run_report(output_dir, rows, summary)
                        return 1
            if not args.dry_run:
                validate_snapshot_files(root_dir / current_asof, spec, current_asof, historical_mode=False)

    summary = {
        "status": "PASS" if failures == 0 else "FAIL",
        "target_dates": len(dates),
        "date_range": [dates[0], dates[-1]],
        "frequency": args.frequency,
        "families": [spec.family for spec in families],
        "failures": failures,
        "dry_run": bool(args.dry_run),
    }
    write_run_report(output_dir, rows, summary)
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    parsed_args = parse_args()
    if parsed_args.log_file is not None:
        log_path = parsed_args.log_file.expanduser().resolve()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log_handle:
            with contextlib.redirect_stdout(log_handle), contextlib.redirect_stderr(log_handle):
                raise SystemExit(run_with_args(parsed_args))
    raise SystemExit(run_with_args(parsed_args))
