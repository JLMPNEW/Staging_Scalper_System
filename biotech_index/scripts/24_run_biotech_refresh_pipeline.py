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
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, resolve_path
from biotech_index.core.db import connect, quote_identifier
from biotech_index.core.logging_utils import configure_utc_logging
from biotech_index.core.pipeline_guards import format_ticker_sample, read_final_scoring_tickers, universe_coverage


LOGGER = logging.getLogger("run_biotech_refresh_pipeline")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"

BIOTECH_SCORE_REQUIRED_COLUMNS = [
    "tier1_selection_gate_score",
    "data_quality_confidence_multiplier",
    "clinical_risk_drag",
    "investment_risk_drag",
]

MULTIBAGGER_SCORE_BASE_REQUIRED_COLUMNS = [
    "base_multibagger_score",
    "orthogonal_alpha_score",
    "distinctive_acceleration_score",
    "tier1_available",
    "tier1_interaction_reason",
]

MULTIBAGGER_SCORE_TIER1_REQUIRED_COLUMNS = [
    "tier1_opportunity_score",
    "tier1_risk_score",
    "tier1_bucket",
    "tier1_gate_score",
    "tier1_gate_multiplier",
]


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
    parser.add_argument("--skip-yahoo", action="store_true", help="Skip the Yahoo adjusted market-data step.")
    parser.add_argument("--skip-analyze", action="store_true", help="Skip SQLite ANALYZE at the end.")
    parser.add_argument("--skip-final-validation", action="store_true", help="Skip final as-of/coverage validation after a full pipeline run.")
    parser.add_argument("--reuse-unchanged-historical", action="store_true", help="Reuse exact-signature governance rows for historical snapshot runs.")
    return parser.parse_args()


def configure_logging() -> None:
    configure_utc_logging()


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def as_bool(raw: object) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "y"}


def parse_clock_time(raw: object, default: str = "16:15") -> dt_time:
    text = str(raw or default).strip()
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    raise ValueError(f"Invalid market close time: {raw}")


def previous_business_day(day: date) -> date:
    out = day - timedelta(days=1)
    while out.weekday() >= 5:
        out -= timedelta(days=1)
    return out


def default_pipeline_asof(config: dict[str, Any]) -> date:
    market_timezone = str(cfg_get(config, "ib_market_data.market_timezone", "America/New_York"))
    market_close_time = parse_clock_time(cfg_get(config, "ib_market_data.market_close_time", "16:15"))
    guard_enabled = as_bool(cfg_get(config, "ib_market_data.market_close_guard", True))
    now_local = datetime.now(timezone.utc).astimezone(ZoneInfo(market_timezone))
    local_today = now_local.date()
    if guard_enabled and (local_today.weekday() >= 5 or now_local.time() < market_close_time):
        return previous_business_day(local_today)
    return local_today


def pipeline_steps(mode: str, *, skip_ctgov: bool, skip_ib: bool, skip_yahoo: bool, reuse_unchanged_historical: bool = False) -> list[Step]:
    sec_event_args: tuple[str, ...] = ("--full-rescan",) if mode in {"weekly_reconcile", "full_backfill"} else ()
    companyfacts_args: tuple[str, ...] = ("--full-refresh",) if mode == "full_backfill" else ()
    forward_args: tuple[str, ...] = ("--run-mode", mode)
    governance_reuse = reuse_unchanged_historical or mode == "weekly_reconcile"
    governance_args: tuple[str, ...] = ("--reuse-unchanged-historical",) if governance_reuse else ()
    ib_args: tuple[str, ...] = ("--allow-partial",) if mode in {"weekly_reconcile", "full_backfill"} else ()
    yahoo_args: tuple[str, ...] = ("--allow-partial",) if mode in {"weekly_reconcile", "full_backfill"} else ()
    commercial_args: tuple[str, ...] = ("--allow-missing-market",) if mode in {"weekly_reconcile", "full_backfill"} else ()
    multibagger_feature_args: tuple[str, ...] = ("--allow-missing-market",) if mode in {"weekly_reconcile", "full_backfill"} else ()
    # 08_scan_ctgov_reactivation_candidates.py is an audit/discovery utility, not a deterministic refresh step.
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
        steps.append(Step("ib_market", "17_sync_market_data_ib.py", ib_args))
    if not skip_yahoo:
        steps.append(Step("yahoo_market_adjusted", "17_sync_market_data_yahoo_adjusted.py", yahoo_args))
    steps.extend(
        [
            Step("commercial_value", "18_build_commercial_value_features.py", commercial_args),
            Step("forward_guidance", "19_parse_forward_guidance.py", forward_args),
            Step("governance_events", "20_build_governance_event_features.py", governance_args),
            Step("biotech_features", "10_build_biotech_features.py"),
            Step("biotech_scores", "11_score_biotech_index.py"),
            Step("biotech_reports", "12_publish_biotech_reports.py"),
            Step("multibagger_features", "21_build_multibagger_features.py", multibagger_feature_args),
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
    timeout_sec: float | None = None,
) -> dict[str, Any]:
    start = time.monotonic()
    LOGGER.info("Starting %s", step.name)
    try:
        completed = subprocess.run(command, cwd=str(PROJECT_ROOT), timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        elapsed = round(time.monotonic() - start, 3)
        LOGGER.error("Step %s timed out after %.3fs", step.name, elapsed)
        return {
            "run_started_at": run_started_at,
            "mode": mode,
            "step": step.name,
            "status": "failed",
            "elapsed_sec": elapsed,
            "returncode": -1,
            "command": " ".join(command),
        }
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
    LOGGER.info("Starting sqlite_optimize")
    with connect(db_path) as conn:
        conn.execute("PRAGMA optimize")
    elapsed = round(time.monotonic() - start, 3)
    LOGGER.info("Finished sqlite_optimize status=success elapsed=%.3fs", elapsed)
    return {
        "run_started_at": run_started_at,
        "mode": mode,
        "step": "sqlite_optimize",
        "status": "success",
        "elapsed_sec": elapsed,
        "returncode": 0,
        "command": f"PRAGMA optimize {db_path}",
    }


def observed_table_tickers(conn: sqlite3.Connection, table: str, *, asof: str, source: str = "") -> list[str]:
    table_sql = quote_identifier(table)
    source_clause = " AND t.source = ?" if source else ""
    params: tuple[Any, ...] = (asof, source) if source else (asof,)
    rows = conn.execute(
        f"""
        SELECT c.ticker
        FROM {table_sql} t
        JOIN companies c ON c.company_id = t.company_id
        WHERE t.asof_date = ?{source_clause}
        """,
        params,
    ).fetchall()
    return [str(row["ticker"] or "") for row in rows]


def validate_table_coverage(
    conn: sqlite3.Connection,
    *,
    table: str,
    asof: str,
    expected_tickers: set[str],
    source: str = "",
    allow_extra: bool = False,
) -> None:
    observed = observed_table_tickers(conn, table, asof=asof, source=source)
    coverage = universe_coverage(expected_tickers, observed)
    failures: list[str] = []
    label = f"{table}{':' + source if source else ''}"
    if coverage.missing_tickers:
        failures.append(f"missing {len(coverage.missing_tickers)}: {format_ticker_sample(coverage.missing_tickers)}")
    if coverage.extra_tickers and not allow_extra:
        failures.append(f"extra {len(coverage.extra_tickers)}: {format_ticker_sample(coverage.extra_tickers)}")
    if failures:
        raise RuntimeError(f"{label} coverage failed for asof={asof}: " + " | ".join(failures))

def is_blank(raw: object) -> bool:
    return raw is None or str(raw).strip() == ""


def validate_table_required_columns(
    conn: sqlite3.Connection,
    *,
    table: str,
    asof: str,
    required_columns: list[str],
) -> None:
    table_sql = quote_identifier(table)
    columns = {
        str(row["name"])
        for row in conn.execute(f"PRAGMA table_info({table_sql})").fetchall()
    }
    failures: list[str] = []
    missing_columns = [column for column in required_columns if column not in columns]
    if missing_columns:
        failures.append("missing required columns: " + ",".join(missing_columns))

    for column in required_columns:
        if column not in columns:
            continue
        column_sql = quote_identifier(column)
        blank_condition = f"t.{column_sql} IS NULL OR TRIM(CAST(t.{column_sql} AS TEXT)) = ''"
        count_row = conn.execute(
            f"""
            SELECT COUNT(*) AS blank_count
            FROM {table_sql} t
            WHERE t.asof_date = ? AND ({blank_condition})
            """,
            (asof,),
        ).fetchone()
        blank_count = int(count_row["blank_count"] or 0) if count_row else 0
        if blank_count:
            sample_rows = conn.execute(
                f"""
                SELECT c.ticker
                FROM {table_sql} t
                JOIN companies c ON c.company_id = t.company_id
                WHERE t.asof_date = ? AND ({blank_condition})
                ORDER BY c.ticker
                LIMIT 10
                """,
                (asof,),
            ).fetchall()
            sample = ",".join(str(row["ticker"] or "") for row in sample_rows)
            failures.append(f"{column} blank for {blank_count} row(s): {sample}")

    if failures:
        raise RuntimeError(f"{table} required column validation failed for asof={asof}: " + " | ".join(failures))


def validate_paired_score_dates(conn: sqlite3.Connection) -> None:
    daily_dates = {
        str(row["asof_date"] or "")
        for row in conn.execute("SELECT DISTINCT asof_date FROM daily_scores WHERE asof_date IS NOT NULL").fetchall()
    }
    multibagger_dates = {
        str(row["asof_date"] or "")
        for row in conn.execute("SELECT DISTINCT asof_date FROM multibagger_scores_daily WHERE asof_date IS NOT NULL").fetchall()
    }
    daily_only = sorted(daily_dates - multibagger_dates)
    multibagger_only = sorted(multibagger_dates - daily_dates)
    failures: list[str] = []
    if daily_only:
        sample = ",".join(daily_only[:10])
        failures.append(f"daily_scores only {len(daily_only)} date(s): {sample}")
    if multibagger_only:
        sample = ",".join(multibagger_only[:10])
        failures.append(f"multibagger_scores_daily only {len(multibagger_only)} date(s): {sample}")
    if failures:
        raise RuntimeError("Paired score date validation failed: " + " | ".join(failures))


def validate_score_csv(
    path: Path,
    *,
    asof: str,
    expected_tickers: set[str],
    required_columns: list[str],
) -> None:
    if not path.exists():
        raise FileNotFoundError(f"Required pipeline output CSV not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        fieldnames = [str(field or "") for field in (reader.fieldnames or [])]
    observed_tickers = [str(row.get("ticker") or "") for row in rows]
    coverage = universe_coverage(expected_tickers, observed_tickers)
    asof_values = {str(row.get("asof_date") or "") for row in rows}
    failures: list[str] = []
    missing_columns = [column for column in required_columns if column not in fieldnames]
    if missing_columns:
        failures.append("missing required columns: " + ",".join(missing_columns))
    if coverage.missing_tickers:
        failures.append(f"missing {len(coverage.missing_tickers)}: {format_ticker_sample(coverage.missing_tickers)}")
    if coverage.extra_tickers:
        failures.append(f"extra {len(coverage.extra_tickers)}: {format_ticker_sample(coverage.extra_tickers)}")
    if asof_values != {asof}:
        sample = ",".join(sorted(asof_values)[:5])
        failures.append(f"asof_date values are {sample or '<blank>'}, expected {asof}")
    for column in required_columns:
        if column not in fieldnames:
            continue
        blank_tickers = [str(row.get("ticker") or "") for row in rows if is_blank(row.get(column))]
        if blank_tickers:
            sample = ",".join(sorted(blank_tickers)[:10])
            failures.append(f"{column} blank for {len(blank_tickers)} row(s): {sample}")
    if failures:
        raise RuntimeError(f"{path} validation failed: " + " | ".join(failures))


def validate_required_csv_columns(path: Path, *, asof: str, required_columns: list[str]) -> None:
    if not path.exists():
        raise FileNotFoundError(f"Required pipeline output CSV not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        fieldnames = [str(field or "") for field in (reader.fieldnames or [])]

    failures: list[str] = []
    missing_columns = [column for column in required_columns if column not in fieldnames]
    if missing_columns:
        failures.append("missing required columns: " + ",".join(missing_columns))
    if rows:
        asof_values = {str(row.get("asof_date") or "") for row in rows}
        if asof_values != {asof}:
            sample = ",".join(sorted(asof_values)[:5])
            failures.append(f"asof_date values are {sample or '<blank>'}, expected {asof}")
        for column in required_columns:
            if column not in fieldnames:
                continue
            blank_tickers = [str(row.get("ticker") or "") for row in rows if is_blank(row.get(column))]
            if blank_tickers:
                sample = ",".join(sorted(blank_tickers)[:10])
                failures.append(f"{column} blank for {len(blank_tickers)} row(s): {sample}")
    if failures:
        raise RuntimeError(f"{path} required column validation failed: " + " | ".join(failures))


def validate_final_outputs(
    config: dict[str, Any],
    *,
    base_dir: Path,
    db_path: Path,
    asof: str,
    run_started_at: str,
    mode: str,
) -> dict[str, Any]:
    start = time.monotonic()
    LOGGER.info("Starting final_output_validation")
    universe_csv = resolve_path(
        cfg_get(config, "biotech_features.final_scoring_universe_csv", "../output/biotech_index_reports/ctgov_final_scoring_universe.csv"),
        base_dir=base_dir,
    )
    expected_tickers = read_final_scoring_tickers(universe_csv)
    biotech_output_dir = resolve_path(cfg_get(config, "biotech_scoring.output_dir", "../output/biotech_index_reports"), base_dir=base_dir)
    biotech_reports_output_dir = resolve_path(cfg_get(config, "biotech_reports.output_dir", "../output/biotech_index_reports"), base_dir=base_dir)
    multibagger_output_dir = resolve_path(cfg_get(config, "multibagger.output_dir", "../output/biotech_index_reports"), base_dir=base_dir)
    biotech_scores_csv = biotech_output_dir / str(cfg_get(config, "biotech_scoring.output_csv", "biotech_daily_scores.csv"))
    biotech_top_candidates_csv = biotech_reports_output_dir / str(cfg_get(config, "biotech_reports.top_candidates_csv", "biotech_top_candidates.csv"))
    multibagger_scores_csv = multibagger_output_dir / str(cfg_get(config, "multibagger.scores_csv", "biotech_multibagger_scores.csv"))
    multibagger_candidates_csv = multibagger_output_dir / str(cfg_get(config, "multibagger.candidates_csv", "biotech_multibagger_candidates.csv"))
    preferred_market_sources = {
        str(cfg_get(config, "commercial_value.preferred_market_source", "interactive_brokers") or "interactive_brokers"),
        str(cfg_get(config, "multibagger.preferred_market_source", "interactive_brokers") or "interactive_brokers"),
    }
    multibagger_required_columns = list(MULTIBAGGER_SCORE_BASE_REQUIRED_COLUMNS)
    if as_bool(cfg_get(config, "multibagger.tier1_interaction.enabled", False)):
        multibagger_required_columns.extend(MULTIBAGGER_SCORE_TIER1_REQUIRED_COLUMNS)
    with connect(db_path) as conn:
        for table in (
            "financial_survival_features",
            "commercial_value_features_daily",
            "forward_guidance_features_daily",
            "governance_event_features_daily",
            "daily_features",
            "daily_scores",
            "multibagger_features_daily",
            "multibagger_scores_daily",
        ):
            validate_table_coverage(conn, table=table, asof=asof, expected_tickers=expected_tickers)
        for source in sorted(source for source in preferred_market_sources if source):
            # Market features can include extra symbols from the vendor cache; downstream layers filter to the final universe.
            validate_table_coverage(
                conn,
                table="market_features_daily",
                asof=asof,
                expected_tickers=expected_tickers,
                source=source,
                allow_extra=True,
            )
        validate_table_required_columns(
            conn,
            table="daily_scores",
            asof=asof,
            required_columns=BIOTECH_SCORE_REQUIRED_COLUMNS,
        )
        validate_table_required_columns(
            conn,
            table="multibagger_scores_daily",
            asof=asof,
            required_columns=multibagger_required_columns,
        )
        validate_paired_score_dates(conn)
    validate_score_csv(
        biotech_scores_csv,
        asof=asof,
        expected_tickers=expected_tickers,
        required_columns=BIOTECH_SCORE_REQUIRED_COLUMNS,
    )
    validate_required_csv_columns(
        biotech_top_candidates_csv,
        asof=asof,
        required_columns=BIOTECH_SCORE_REQUIRED_COLUMNS,
    )
    validate_score_csv(
        multibagger_scores_csv,
        asof=asof,
        expected_tickers=expected_tickers,
        required_columns=multibagger_required_columns,
    )
    validate_required_csv_columns(
        multibagger_candidates_csv,
        asof=asof,
        required_columns=multibagger_required_columns,
    )
    elapsed = round(time.monotonic() - start, 3)
    LOGGER.info("Finished final_output_validation status=success elapsed=%.3fs expected_tickers=%d", elapsed, len(expected_tickers))
    return {
        "run_started_at": run_started_at,
        "mode": mode,
        "step": "final_output_validation",
        "status": "success",
        "elapsed_sec": elapsed,
        "returncode": 0,
        "command": f"validate outputs asof={asof}",
    }


def main() -> None:
    configure_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    if args.asof:
        parsed_asof = parse_date(args.asof)
        if parsed_asof is None:
            raise ValueError(f"Invalid --asof date: {args.asof}")
        asof = parsed_asof.isoformat()
    else:
        asof = default_pipeline_asof(config).isoformat()
    LOGGER.info("Pipeline as-of date: %s", asof)
    timing_csv = resolve_path(
        cfg_get(config, "biotech_refresh.timing_csv", "../output/biotech_index_reports/biotech_refresh_timing.csv"),
        base_dir=base_dir,
    )
    raw_timeout_value = cfg_get(config, "biotech_refresh.step_timeout_sec", 7200.0)
    try:
        raw_timeout = float(raw_timeout_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid biotech_refresh.step_timeout_sec value: {raw_timeout_value!r}") from exc
    step_timeout_sec = raw_timeout if raw_timeout > 0 else None
    selected_steps = {step.strip() for step in args.steps.split(",") if step.strip()}
    all_steps = pipeline_steps(
        args.mode,
        skip_ctgov=args.skip_ctgov,
        skip_ib=args.skip_ib,
        skip_yahoo=args.skip_yahoo,
        reuse_unchanged_historical=args.reuse_unchanged_historical,
    )
    if selected_steps:
        known = {step.name for step in all_steps}
        unknown = sorted(selected_steps - known)
        if unknown:
            raise ValueError(f"Unknown pipeline step(s): {', '.join(unknown)}")
    steps = [step for step in all_steps if not selected_steps or step.name in selected_steps]
    final_validation_enabled = as_bool(cfg_get(config, "biotech_refresh.validate_final_outputs", True))

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
            timing_rows[-1] = run_step(
                step,
                command=command,
                mode=args.mode,
                run_started_at=run_started_at,
                timeout_sec=step_timeout_sec,
            )
            write_timing_csv(timing_csv, timing_rows)
            if timing_rows[-1]["status"] != "success":
                raise SystemExit(int(timing_rows[-1]["returncode"]))
        if not args.skip_analyze:
            timing_rows.append(analyze_db(db_path, run_started_at=run_started_at, mode=args.mode))
            write_timing_csv(timing_csv, timing_rows)
        if not selected_steps and not args.skip_final_validation and final_validation_enabled:
            validation_start = time.monotonic()
            timing_rows.append(
                {
                    "run_started_at": run_started_at,
                    "mode": args.mode,
                    "step": "final_output_validation",
                    "status": "running",
                    "elapsed_sec": "",
                    "returncode": "",
                    "command": f"validate outputs asof={asof}",
                }
            )
            write_timing_csv(timing_csv, timing_rows)
            try:
                timing_rows[-1] = validate_final_outputs(
                    config,
                    base_dir=base_dir,
                    db_path=db_path,
                    asof=asof,
                    run_started_at=run_started_at,
                    mode=args.mode,
                )
            except Exception as exc:
                timing_rows[-1] = {
                    "run_started_at": run_started_at,
                    "mode": args.mode,
                    "step": "final_output_validation",
                    "status": "failed",
                    "elapsed_sec": round(time.monotonic() - validation_start, 3),
                    "returncode": 1,
                    "command": f"validate outputs asof={asof}: {type(exc).__name__}: {exc}",
                }
                write_timing_csv(timing_csv, timing_rows)
                raise
            write_timing_csv(timing_csv, timing_rows)
        elif selected_steps:
            LOGGER.warning("Final output validation skipped because --steps was used.")
        elif args.skip_final_validation:
            LOGGER.warning("Final output validation skipped via --skip-final-validation.")
        elif not final_validation_enabled:
            LOGGER.warning("Final output validation skipped because biotech_refresh.validate_final_outputs=false.")
    finally:
        write_timing_csv(timing_csv, timing_rows)


if __name__ == "__main__":
    main()
