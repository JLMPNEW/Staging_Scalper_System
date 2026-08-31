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
import os
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
    stage11_prefix: str
    pre_steps: tuple[StepSpec, ...]
    steps: tuple[StepSpec, ...]
    restore_steps: tuple[StepSpec, ...]


def script(relative: str) -> Path:
    return PROJECT_ROOT / relative


FAMILIES: dict[str, FamilySpec] = {}


def register(spec: FamilySpec) -> None:
    for alias in (spec.family, *spec.aliases):
        FAMILIES[alias.lower()] = spec


def financial_feature_pre_step(model_family: str) -> StepSpec:
    return StepSpec(
        "08_rebuild_financial_features",
        script("technology/scripts/08_build_technology_financial_features_batched.py"),
        (
            "--model-family",
            model_family,
            "--batch-size",
            "8",
            "--batch-timeout-sec",
            "900",
        ),
        pass_asof=False,
    )


SEMICONDUCTOR_STEPS = (
    StepSpec(
        "05_build_market_features",
        script("technology/scripts/05_build_technology_market_features.py"),
        ("--model-family", "semiconductors", "--benchmark-tickers", "SMH,SOXX,QQQ,SPY"),
    ),
    StepSpec("09_import_positioning", script("technology/scripts/09_import_technology_positioning.py"), ("--model-family", "semiconductors", "--features-only")),
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
        "10c_financial_lineage_shadow",
        script("technology/scripts/10c_build_technology_financial_lineage_shadow.py"),
        (
            "--family",
            "semiconductors",
            "--policy-context",
            "production",
            "--retrospective-source-discovery-max-days",
            "7",
        ),
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
    StepSpec("09_import_positioning", script("technology/technology_hardware/scripts/09_import_technology_hardware_positioning.py"), ("--features-only",)),
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
        "10c_financial_lineage_shadow",
        script("technology/scripts/10c_build_technology_financial_lineage_shadow.py"),
        (
            "--family",
            "technology_hardware",
            "--policy-context",
            "production",
            "--retrospective-source-discovery-max-days",
            "7",
        ),
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
    StepSpec("09_import_positioning", script("technology/software_infrastructure/scripts/09_import_software_infrastructure_positioning.py"), ("--features-only",)),
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
        "10c_financial_lineage_shadow",
        script("technology/scripts/10c_build_technology_financial_lineage_shadow.py"),
        (
            "--family",
            "software_infrastructure",
            "--policy-context",
            "production",
            "--retrospective-source-discovery-max-days",
            "7",
        ),
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
        stage11_prefix="semiconductor",
        pre_steps=(financial_feature_pre_step("semiconductors"),),
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
        stage11_prefix="technology_hardware",
        pre_steps=(financial_feature_pre_step("technology_hardware"),),
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
        stage11_prefix="software_infrastructure",
        pre_steps=(financial_feature_pre_step("software_infrastructure"),),
        steps=SOFTWARE_STEPS,
        restore_steps=SOFTWARE_RESTORE_STEPS,
    )
)

STAGE11_EXPORT_SCRIPT = script("technology/scripts/19_export_stage11_survivorship_calibration_panel.py")


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
    parser.add_argument(
        "--restatement-reason",
        default="",
        help=(
            "Force selected historical snapshots to research/PIT status rather than strict OOS, "
            "and stamp this reason into row and manifest provenance."
        ),
    )
    parser.add_argument(
        "--include-stage11-survivorship-panel",
        action="store_true",
        help="Also write the survivorship-correct Stage 11 calibration sidecar into each dated dashboard folder.",
    )
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


def command_for_stage11_sidecar(
    *,
    python_exe: str,
    config_path: Path,
    db_path: Path | None,
    spec: FamilySpec,
    dates: list[str],
) -> list[str]:
    if not dates:
        raise ValueError("Stage 11 sidecar export requires at least one date.")
    cmd = [
        python_exe,
        "-c",
        RUNPY_TRAMPOLINE,
        str(STAGE11_EXPORT_SCRIPT),
        "--config",
        str(config_path),
        "--family",
        spec.family,
        "--dates",
        ",".join(dates),
        "--output-layout",
        "dashboard_snapshot",
    ]
    if db_path is not None:
        cmd.extend(["--db", str(db_path)])
    return cmd


def run_command(cmd: list[str], *, timeout_sec: int, dry_run: bool) -> tuple[int, str, str]:
    if dry_run:
        print("DRY-RUN", " ".join(cmd))
        return 0, "", ""
    # Always execute steps in a subprocess so --step-timeout-sec is enforced;
    # the former in-process runpy branch could never be timed out.
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


def validate_stage11_sidecar(snapshot_dir: Path, spec: FamilySpec, asof: str) -> None:
    path = snapshot_dir / f"{spec.stage11_prefix}_stage11_survivorship_calibration_panel.csv"
    if not path.exists() or path.stat().st_size == 0:
        raise RuntimeError(f"Missing or empty Stage 11 survivorship sidecar: {path}")
    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise RuntimeError(f"Stage 11 survivorship sidecar has no rows: {path}")
    required = {
        "ticker",
        "asof_date",
        "final_score",
        "survivorship_corrected_panel_flag",
        "stage11_calibration_panel_source",
        "stage11_calibration_input_eligible_flag",
        "stage11_calibration_input_reason",
        "stage11_exclusion_reason",
        "price_available_on_asof_flag",
        "forward_21d_join_ready_flag",
        "forward_63d_join_ready_flag",
    }
    missing = sorted(required.difference(rows[0]))
    if missing:
        raise RuntimeError(f"Stage 11 sidecar missing required fields: {missing}")
    bad_dates = sorted({str(row.get("asof_date") or "") for row in rows if str(row.get("asof_date") or "") != asof})
    if bad_dates:
        raise RuntimeError(f"Stage 11 sidecar contains rows outside {asof}: {bad_dates[:5]}")
    bad_survivorship = [
        str(row.get("ticker") or "")
        for row in rows
        if str(row.get("survivorship_corrected_panel_flag") or "") != "1"
        or str(row.get("stage11_calibration_panel_source") or "") == "dashboard_rank_snapshot_current_universe_replay"
    ]
    if bad_survivorship:
        raise RuntimeError(f"Stage 11 sidecar is not survivorship-correct for rows: {bad_survivorship[:10]}")
    eligible = sum(1 for row in rows if str(row.get("stage11_calibration_input_eligible_flag") or "") == "1")
    if eligible <= 0:
        raise RuntimeError(f"Stage 11 sidecar has no eligible calibration rows: {path}")


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


class _BackfillAbort(Exception):
    """Internal control-flow signal: stop the backfill, run root restore, write the report."""


def run_with_args(args: argparse.Namespace) -> int:
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    restatement_reason = str(args.restatement_reason or "").strip()
    if restatement_reason:
        os.environ["TECHNOLOGY_HISTORICAL_RESTATEMENT_REASON"] = restatement_reason
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
    historical_published = False
    print(
        f"Historical dashboard backfill: dates={len(dates)} {dates[0]}..{dates[-1]} "
        f"frequency={args.frequency} families={','.join(spec.family for spec in families)}"
    )
    try:
        date_range_label = f"{dates[0]}..{dates[-1]}"
        for spec in families:
            for step in spec.pre_steps:
                cmd = command_for_step(
                    python_exe=sys.executable,
                    step=step,
                    config_path=config_path,
                    db_path=command_db_path,
                    asof=dates[-1],
                    snapshot_dir=None,
                )
                print(f"[{date_range_label}][{spec.family}][{step.step_id}]")
                try:
                    code, stdout, stderr = run_command(
                        cmd,
                        timeout_sec=args.step_timeout_sec,
                        dry_run=bool(args.dry_run),
                    )
                except subprocess.TimeoutExpired as exc:
                    code = 124
                    stdout = str(exc.stdout or "")
                    stderr = str(
                        exc.stderr or f"Timed out after {args.step_timeout_sec}s"
                    )
                rows.append(
                    {
                        "asof_date": date_range_label,
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
                        raise _BackfillAbort
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
                    if not args.dry_run and step.step_id == "10b_publish_dashboard":
                        historical_published = True
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
                            raise _BackfillAbort
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
                            raise _BackfillAbort from exc

        if args.include_stage11_survivorship_panel:
            for spec in families:
                cmd = command_for_stage11_sidecar(
                    python_exe=sys.executable,
                    config_path=config_path,
                    db_path=command_db_path,
                    spec=spec,
                    dates=dates,
                )
                date_range_label = f"{dates[0]}..{dates[-1]}"
                print(f"[{date_range_label}][{spec.family}][stage11_survivorship_sidecar_batch]")
                try:
                    code, stdout, stderr = run_command(cmd, timeout_sec=args.step_timeout_sec, dry_run=bool(args.dry_run))
                except subprocess.TimeoutExpired as exc:
                    code, stdout, stderr = 124, str(exc.stdout or ""), str(exc.stderr or f"Timed out after {args.step_timeout_sec}s")
                rows.append(
                    {
                        "asof_date": date_range_label,
                        "family": spec.family,
                        "step_id": "stage11_survivorship_sidecar_batch",
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
                        raise _BackfillAbort
                if not args.dry_run:
                    root_dir = dashboard_dir(config, base_dir, spec)
                    for asof in dates:
                        try:
                            validate_stage11_sidecar(root_dir / asof, spec, asof)
                            rows.append({"asof_date": asof, "family": spec.family, "step_id": "stage11_sidecar_validation", "returncode": 0})
                        except Exception as exc:  # noqa: BLE001 - report and optionally continue
                            failures += 1
                            rows.append(
                                {
                                    "asof_date": asof,
                                    "family": spec.family,
                                    "step_id": "stage11_sidecar_validation",
                                    "returncode": 1,
                                    "stderr_tail": f"{type(exc).__name__}: {exc}",
                                }
                            )
                            print(f"{type(exc).__name__}: {exc}")
                            if not args.continue_on_error:
                                raise _BackfillAbort from exc
    except _BackfillAbort:
        pass
    finally:
        # Restore the production dashboard root whenever historical publishing began,
        # including the abort paths above; a failed run must not leave a historical
        # snapshot in the current-dashboard root.
        if historical_published and not args.dry_run and not args.no_restore_latest_root:
            preserved_historical_snapshots: dict[tuple[str, str], tuple[Path, dict[Path, bytes]]] = {}
            for spec in families:
                current_asof = latest_current_asof(db_path, spec)
                snapshot_dir = dashboard_dir(config, base_dir, spec) / current_asof
                if current_asof in dates and snapshot_dir.exists():
                    preserved_historical_snapshots[(spec.family, current_asof)] = (
                        snapshot_dir,
                        {
                            path.relative_to(snapshot_dir): path.read_bytes()
                            for path in snapshot_dir.rglob("*")
                            if path.is_file()
                        },
                    )
            restore_aborted = False
            for spec in families:
                if restore_aborted:
                    break
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
                        code, stdout, stderr = run_command(cmd, timeout_sec=args.step_timeout_sec, dry_run=False)
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
                            restore_aborted = True
                            break
                else:
                    try:
                        validate_snapshot_files(root_dir / current_asof, spec, current_asof, historical_mode=False)
                        rows.append({"asof_date": current_asof, "family": spec.family, "step_id": "restore_snapshot_validation", "returncode": 0})
                    except Exception as exc:  # noqa: BLE001 - restore validation must not mask the run report
                        failures += 1
                        rows.append(
                            {
                                "asof_date": current_asof,
                                "family": spec.family,
                                "step_id": "restore_snapshot_validation",
                                "returncode": 1,
                                "stderr_tail": f"{type(exc).__name__}: {exc}",
                            }
                        )
                        print(f"{type(exc).__name__}: {exc}")

            # Current-root publishers also update their dated current-asof folder.
            # Restore any selected historical snapshot from memory so root restoration
            # cannot silently replace a corrected PIT artifact with current-mode output.
            for spec in families:
                current_asof = latest_current_asof(db_path, spec)
                preserved = preserved_historical_snapshots.get((spec.family, current_asof))
                if preserved is None:
                    continue
                snapshot_dir, files = preserved
                try:
                    for relative_path, payload in files.items():
                        destination = snapshot_dir / relative_path
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination.write_bytes(payload)
                    validate_snapshot_files(snapshot_dir, spec, current_asof, historical_mode=True)
                    if args.include_stage11_survivorship_panel:
                        validate_stage11_sidecar(snapshot_dir, spec, current_asof)
                    rows.append(
                        {
                            "asof_date": current_asof,
                            "family": spec.family,
                            "step_id": "restore_target_historical_snapshot",
                            "returncode": 0,
                        }
                    )
                except Exception as exc:  # noqa: BLE001 - report restoration failure in the run manifest
                    failures += 1
                    rows.append(
                        {
                            "asof_date": current_asof,
                            "family": spec.family,
                            "step_id": "restore_target_historical_snapshot",
                            "returncode": 1,
                            "stderr_tail": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    print(f"{type(exc).__name__}: {exc}")

    summary = {
        "status": "PASS" if failures == 0 else "FAIL",
        "target_dates": len(dates),
        "date_range": [dates[0], dates[-1]],
        "frequency": args.frequency,
        "families": [spec.family for spec in families],
        "failures": failures,
        "dry_run": bool(args.dry_run),
        "restatement_reason": restatement_reason,
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
