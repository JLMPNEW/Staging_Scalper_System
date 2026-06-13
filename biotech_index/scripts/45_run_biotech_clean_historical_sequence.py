#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import logging
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from biotech_index.core.logging_utils import configure_utc_logging  # noqa: E402


LOGGER = logging.getLogger("run_biotech_clean_historical_sequence")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "output" / "biotech_index_reports" / "clean_historical_sequence"
DEFAULT_OPTUNA_SCRIPT = PACKAGE_ROOT / "scripts" / "46_optuna_biotech_candidate_optimizer.py"

ALLOWED_CALIBRATION_COHORTS = frozenset(
    {
        "commercial_profitable_quality_or_mature",
        "commercial_turnaround_or_unprofitable_growth",
        "late_clinical_pivotal_or_registrational",
        "platform_partnered_modality_pipeline",
        "early_clinical_speculative_or_single_asset_pipeline",
    }
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the clean biotech historical recomputation sequence: derived restatement, "
            "historical QA, feature IC/monotonicity, and baseline Phase-1 backtest."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--start-asof", type=str, default="")
    parser.add_argument("--end-asof", type=str, default="")
    parser.add_argument(
        "--target-weekly-date-count",
        type=int,
        default=0,
        help="Build an explicit Friday date grid ending at --end-asof with this many dates, e.g. 250 for Optuna prep.",
    )
    parser.add_argument(
        "--market-history-start-asof",
        type=str,
        default="",
        help="Earliest market-bar date to use/fetch. Defaults to 420 days before the first scoring date.",
    )
    parser.add_argument("--history-date-source", choices=["daily_scores", "daily_features"], default="daily_scores")
    parser.add_argument("--history-fridays-only", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--horizons", type=str, default="20,60,120")
    parser.add_argument("--top-n", type=str, default="10,20")
    parser.add_argument("--bootstrap-iterations", type=int, default=250)
    parser.add_argument(
        "--min-short-pct-coverage",
        type=float,
        default=0.90,
        help="Hard minimum per-date short_interest_pct_float availability ratio for clean-panel QA.",
    )
    parser.add_argument("--skip-recompute", action="store_true")
    parser.add_argument("--skip-market-bar-backfill", action="store_true")
    parser.add_argument("--skip-qa", action="store_true")
    parser.add_argument("--skip-ic", action="store_true")
    parser.add_argument("--skip-baseline-backtest", action="store_true")
    parser.add_argument("--run-candidate-calibration", action="store_true")
    parser.add_argument("--candidate-name-filter", type=str, default="")
    parser.add_argument("--policy-name-filter", type=str, default="")
    parser.add_argument(
        "--calibration-max-workers",
        type=int,
        default=0,
        help="Optional --max-workers value passed through to script 28 candidate calibration.",
    )
    parser.add_argument(
        "--candidate-grid-executor",
        choices=["thread", "process"],
        default="",
        help="Optional script 28 candidate-grid executor override; use process for CPU-bound full runs.",
    )
    parser.add_argument(
        "--run-optuna",
        action="store_true",
        help=(
            "Run the gated Optuna optimizer after panel rebuild, QA, IC, baseline backtest, "
            "and candidate calibration. Requires an optimizer script and --run-candidate-calibration."
        ),
    )
    parser.add_argument("--optuna-script", type=Path, default=DEFAULT_OPTUNA_SCRIPT)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def compact_date(raw: str) -> str:
    parsed = parse_date(raw)
    return parsed.strftime("%Y%m%d") if parsed else str(raw).replace("-", "")


def previous_or_same_friday(day: date) -> date:
    return day - timedelta(days=(day.weekday() - 4) % 7)


def friday_grid(*, start_asof: str, end_asof: str, target_count: int = 0) -> list[str]:
    end = parse_date(end_asof)
    if end is None:
        raise ValueError(f"Invalid weekly date-grid end date: {end_asof!r}")
    end = previous_or_same_friday(end)
    start = parse_date(start_asof)
    if target_count > 0:
        dates = [end - timedelta(days=7 * idx) for idx in range(target_count)]
        return [item.isoformat() for item in reversed(dates)]
    if start is None:
        raise ValueError("A weekly date grid requires either --start-asof or --target-weekly-date-count.")
    start = previous_or_same_friday(start)
    dates: list[str] = []
    current = start
    while current <= end:
        dates.append(current.isoformat())
        current += timedelta(days=7)
    if not dates:
        raise RuntimeError("Weekly date grid is empty.")
    return dates


def default_market_history_start(first_scoring_asof: str) -> str:
    first = parse_date(first_scoring_asof)
    if first is None:
        raise ValueError(f"Invalid first scoring date: {first_scoring_asof!r}")
    return (first - timedelta(days=420)).isoformat()


def as_bool(raw: object, default: bool = False) -> bool:
    if raw is None:
        return default
    text = str(raw).strip().lower()
    if not text:
        return default
    if text in {"1", "true", "t", "yes", "y", "enabled", "on"}:
        return True
    if text in {"0", "false", "f", "no", "n", "disabled", "off"}:
        return False
    return default


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        keys: list[str] = []
        seen: set[str] = set()
        for row in rows:
            for key in row:
                if key not in seen:
                    keys.append(key)
                    seen.add(key)
        fieldnames = keys
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def connect_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def path_is_staging(path: Path) -> bool:
    parts = {part.upper() for part in path.resolve().parts}
    text = str(path.resolve()).upper()
    return "STAGING" in parts and "PROD" not in text and "PRODUCTION" not in text


def validate_staging_paths(config: dict[str, Any], *, config_path: Path, db_path: Path) -> dict[str, str]:
    base_dir = config_path.parent
    market_db = resolve_path(cfg_get(config, "market_positioning.database_path"), base_dir=base_dir)
    form4_db = resolve_path(cfg_get(config, "governance_events.form4_db_path"), base_dir=base_dir)
    paths = {
        "biotech_db": db_path.resolve(),
        "market_positioning_db": market_db.resolve(),
        "form4_db": form4_db.resolve(),
    }
    failures = [f"{label}={path}" for label, path in paths.items() if not path_is_staging(path)]
    if failures:
        raise RuntimeError("Clean historical sequence requires staging DB paths only: " + " | ".join(failures))
    return {label: str(path) for label, path in paths.items()}


def load_history_bounds(db_path: Path, *, source_table: str, start_asof: str, end_asof: str) -> tuple[str, str, list[str]]:
    start = parse_date(start_asof)
    end = parse_date(end_asof)
    with closing(connect_readonly(db_path)) as conn:
        rows = conn.execute(
            f"""
            SELECT DISTINCT asof_date
            FROM {source_table}
            WHERE asof_date IS NOT NULL
            ORDER BY asof_date
            """
        ).fetchall()
    dates: list[str] = []
    for row in rows:
        parsed = parse_date(row["asof_date"])
        if parsed is None:
            continue
        if start is not None and parsed < start:
            continue
        if end is not None and parsed > end:
            continue
        dates.append(parsed.isoformat())
    if not dates:
        raise RuntimeError(f"No historical dates found in {source_table} for requested range.")
    return dates[0], dates[-1], dates


def read_expected_tickers(config: dict[str, Any], *, config_path: Path) -> set[str]:
    universe_path = resolve_path(cfg_get(config, "biotech_features.final_scoring_universe_csv"), base_dir=config_path.parent)
    if not universe_path.exists():
        raise FileNotFoundError(universe_path)
    out: set[str] = set()
    with universe_path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if as_bool(row.get("scoring_include"), False):
                ticker = str(row.get("ticker") or "").strip().upper()
                if ticker:
                    out.add(ticker)
    if not out:
        raise RuntimeError(f"No scoring_include tickers found in {universe_path}")
    return out


def run_command(
    *,
    label: str,
    command: list[str],
    output_dir: Path,
    dry_run: bool,
    timing_rows: list[dict[str, Any]],
) -> None:
    start = time.monotonic()
    LOGGER.info("Starting %s: %s", label, " ".join(command))
    row = {
        "step": label,
        "command": " ".join(command),
        "status": "dry_run" if dry_run else "running",
        "elapsed_sec": "",
        "returncode": "",
    }
    timing_rows.append(row)
    write_csv(output_dir / "clean_historical_sequence_timing.csv", timing_rows)
    if dry_run:
        return
    result = subprocess.run(command, cwd=PROJECT_ROOT)
    row["elapsed_sec"] = round(time.monotonic() - start, 3)
    row["returncode"] = result.returncode
    row["status"] = "success" if result.returncode == 0 else "failed"
    write_csv(output_dir / "clean_historical_sequence_timing.csv", timing_rows)
    if result.returncode != 0:
        raise RuntimeError(f"{label} failed with returncode={result.returncode}")


def source_table_qa(market_db_path: Path, *, end_asof: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with closing(connect_readonly(market_db_path)) as conn:
        checks = [
            (
                "finra_short_interest_publication_dates",
                """
                SELECT COUNT(*) AS n
                FROM short_interest_snapshots
                WHERE asof_date IS NULL
                   OR publication_date IS NULL
                   OR settlement_date IS NULL
                   OR date(asof_date) < date(settlement_date)
                """,
                "FINRA short-interest snapshots must be dated by public/as-of date, not future settlement leakage.",
            ),
            (
                "sec13f_filing_lag",
                """
                SELECT COUNT(*) AS n
                FROM institutional_13f_ownership_snapshots
                WHERE asof_date IS NULL
                   OR period_of_report IS NULL
                   OR date(asof_date) < date(period_of_report)
                """,
                "13F ownership snapshots must not be available before the report period date.",
            ),
        ]
        for check, sql, details in checks:
            bad = int(conn.execute(sql).fetchone()["n"] or 0)
            rows.append(
                {
                    "check": check,
                    "status": "PASS" if bad == 0 else "FAIL",
                    "value": bad,
                    "details": details,
                }
            )
        for table, date_column in [
            ("short_interest_snapshots", "asof_date"),
            ("institutional_13f_ownership_snapshots", "asof_date"),
            ("ibkr_borrow_fee_rate_daily", "asof_date"),
            ("float_shares_snapshots", "asof_date"),
        ]:
            row = conn.execute(f"SELECT MAX({date_column}) AS max_date, COUNT(*) AS n FROM {table}").fetchone()
            max_date = str(row["max_date"] or "")
            rows.append(
                {
                    "check": f"{table}_loaded",
                    "status": "PASS" if int(row["n"] or 0) > 0 else "FAIL",
                    "value": int(row["n"] or 0),
                    "details": f"max_{date_column}={max_date}; recompute_end_asof={end_asof}",
                }
            )
    return rows


def config_guardrail_qa(config: dict[str, Any]) -> list[dict[str, Any]]:
    routing = cfg_get(config, "biotech_scoring.risk_mode_routing", {}) or {}
    if not isinstance(routing, dict):
        routing = {}
    ctgov = cfg_get(config, "biotech_features.forward_catalyst_ctgov", {}) or {}
    if not isinstance(ctgov, dict):
        ctgov = {}
    cohort_modes = routing.get("cohort_modes", {}) if isinstance(routing.get("cohort_modes"), dict) else {}
    old_keys = sorted(set(str(key) for key in cohort_modes) - ALLOWED_CALIBRATION_COHORTS)
    rows = [
        {
            "check": "production_score_source_allocation_only",
            "status": "PASS" if str(routing.get("production_score_source") or "opportunity_score") == "opportunity_score" else "FAIL",
            "value": str(routing.get("production_score_source") or ""),
            "details": "Production rank must use allocation opportunity score.",
        },
        {
            "check": "discovery_not_allowed_as_production_rank",
            "status": "PASS" if not as_bool(routing.get("allow_discovery_as_production_rank"), False) else "FAIL",
            "value": as_bool(routing.get("allow_discovery_as_production_rank"), False),
            "details": "Discovery score must not silently become allocation rank.",
        },
        {
            "check": "ctgov_shadow_only",
            "status": "PASS" if not as_bool(ctgov.get("include_in_primary_score"), False) else "FAIL",
            "value": as_bool(ctgov.get("include_in_primary_score"), False),
            "details": "CTGov forward catalyst signal remains shadow-only until source-aware IC validates.",
        },
        {
            "check": "risk_routing_uses_five_cohorts",
            "status": "PASS" if not old_keys else "FAIL",
            "value": "|".join(old_keys),
            "details": "Risk routing cohort keys must use the five calibration cohorts only.",
        },
    ]
    return rows


def panel_qa(
    db_path: Path,
    *,
    expected_tickers: set[str],
    start_asof: str,
    end_asof: str,
    expected_dates: list[str] | None = None,
    min_short_pct_coverage: float = 0.90,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    expected_count = len(expected_tickers)
    start = parse_date(start_asof)
    end = parse_date(end_asof)
    with closing(connect_readonly(db_path)) as conn:
        if expected_dates is not None:
            dates = [str(item) for item in expected_dates if parse_date(item) is not None]
        else:
            date_rows = conn.execute(
                """
                SELECT asof_date
                FROM daily_scores
                WHERE asof_date IS NOT NULL
                GROUP BY asof_date
                ORDER BY asof_date
                """
            ).fetchall()
            dates = []
            for row in date_rows:
                parsed = parse_date(row["asof_date"])
                if parsed is None:
                    continue
                if start is not None and parsed < start:
                    continue
                if end is not None and parsed > end:
                    continue
                dates.append(parsed.isoformat())

        summary_rows: list[dict[str, Any]] = []
        detail_rows: list[dict[str, Any]] = []
        for asof in dates:
            feature = conn.execute(
                """
                SELECT
                    COUNT(*) AS n,
                    SUM(CASE WHEN COALESCE(short_interest_pct_float_available_flag, 0.0) > 0.0 THEN 1 ELSE 0 END) AS short_pct_available,
                    SUM(CASE WHEN COALESCE(short_interest_pct_float, 0.0) > 0.0 THEN 1 ELSE 0 END) AS short_pct_positive,
                    SUM(CASE WHEN COALESCE(borrow_fee_data_available_flag, 0.0) > 0.0 THEN 1 ELSE 0 END) AS borrow_fee_available,
                    SUM(CASE WHEN COALESCE(borrow_fee_stale_flag, 0.0) > 0.0 THEN 1 ELSE 0 END) AS borrow_fee_stale,
                    SUM(CASE WHEN COALESCE(shortable_data_available_flag, 0.0) > 0.0 THEN 1 ELSE 0 END) AS shortable_available,
                    SUM(CASE WHEN institutional_accumulation_score IS NOT NULL THEN 1 ELSE 0 END) AS institutional_score_present,
                    SUM(CASE WHEN insider_accumulation_score IS NOT NULL THEN 1 ELSE 0 END) AS insider_score_present,
                    SUM(CASE WHEN ctgov_forward_catalyst_score IS NOT NULL THEN 1 ELSE 0 END) AS ctgov_score_present
                FROM daily_features
                WHERE asof_date = ?
                """,
                (asof,),
            ).fetchone()
            score = conn.execute(
                """
                SELECT
                    COUNT(*) AS n,
                    SUM(CASE WHEN biotech_primary_cohort IN (
                        'commercial_profitable_quality_or_mature',
                        'commercial_turnaround_or_unprofitable_growth',
                        'late_clinical_pivotal_or_registrational',
                        'platform_partnered_modality_pipeline',
                        'early_clinical_speculative_or_single_asset_pipeline'
                    ) THEN 1 ELSE 0 END) AS five_cohort_rows,
                    SUM(CASE WHEN production_score_source = 'legacy_allocation' THEN 1 ELSE 0 END) AS legacy_source_rows,
                    SUM(CASE WHEN production_rank_score_field = 'opportunity_score' THEN 1 ELSE 0 END) AS allocation_rank_field_rows,
                    SUM(CASE WHEN COALESCE(discovery_opportunity_score, 0.0) > 0.0 THEN 1 ELSE 0 END) AS discovery_score_rows
                FROM daily_scores
                WHERE asof_date = ?
                """,
                (asof,),
            ).fetchone()
            row = {
                "asof_date": asof,
                "expected_tickers": expected_count,
                "daily_features_rows": int(feature["n"] or 0),
                "daily_scores_rows": int(score["n"] or 0),
                "short_pct_available_rows": int(feature["short_pct_available"] or 0),
                "short_pct_positive_rows": int(feature["short_pct_positive"] or 0),
                "borrow_fee_available_rows": int(feature["borrow_fee_available"] or 0),
                "borrow_fee_stale_rows": int(feature["borrow_fee_stale"] or 0),
                "shortable_available_rows": int(feature["shortable_available"] or 0),
                "institutional_score_present_rows": int(feature["institutional_score_present"] or 0),
                "insider_score_present_rows": int(feature["insider_score_present"] or 0),
                "ctgov_score_present_rows": int(feature["ctgov_score_present"] or 0),
                "five_cohort_rows": int(score["five_cohort_rows"] or 0),
                "legacy_source_rows": int(score["legacy_source_rows"] or 0),
                "allocation_rank_field_rows": int(score["allocation_rank_field_rows"] or 0),
                "discovery_score_rows": int(score["discovery_score_rows"] or 0),
            }
            short_pct_min = int(round(expected_count * min_short_pct_coverage))
            failures: list[str] = []
            warnings: list[str] = []
            if row["daily_features_rows"] != expected_count:
                failures.append("daily_features_row_count")
            if row["daily_scores_rows"] != expected_count:
                failures.append("daily_scores_row_count")
            if row["short_pct_available_rows"] < short_pct_min:
                warnings.append("short_interest_pct_float_coverage")
            if row["five_cohort_rows"] != expected_count:
                failures.append("five_cohort_assignment")
            if row["legacy_source_rows"] != expected_count or row["allocation_rank_field_rows"] != expected_count:
                failures.append("allocation_rank_source")
            row["status"] = "PASS" if not failures else "FAIL"
            row["failures"] = "|".join(failures)
            row["warnings"] = "|".join(warnings)
            detail_rows.append(row)

        old_cohort_rows = conn.execute(
            """
            SELECT biotech_primary_cohort, COUNT(*) AS n
            FROM daily_scores
            WHERE asof_date BETWEEN ? AND ?
              AND biotech_primary_cohort NOT IN (
                'commercial_profitable_quality_or_mature',
                'commercial_turnaround_or_unprofitable_growth',
                'late_clinical_pivotal_or_registrational',
                'platform_partnered_modality_pipeline',
                'early_clinical_speculative_or_single_asset_pipeline'
              )
            GROUP BY biotech_primary_cohort
            ORDER BY n DESC
            """,
            (start_asof, end_asof),
        ).fetchall()
        summary_rows.append(
            {
                "check": "historical_panel_dates",
                "status": "PASS" if dates else "FAIL",
                "value": len(dates),
                "details": (
                    f"start={start_asof}; end={end_asof}; "
                    f"expected_dates={'explicit_grid' if expected_dates is not None else 'db_range'}"
                ),
            }
        )
        hard_failure_count = sum(1 for row in detail_rows if row["status"] == "FAIL")
        warning_count = sum(1 for row in detail_rows if row.get("warnings"))
        summary_rows.append(
            {
                "check": "core_panel_row_count_and_ranking_integrity",
                "status": "PASS" if hard_failure_count == 0 else "FAIL",
                "value": hard_failure_count,
                "details": f"expected_tickers={expected_count}",
            }
        )
        summary_rows.append(
            {
                "check": "short_interest_pct_float_coverage_advisory",
                "status": "PASS" if warning_count == 0 else "WARN",
                "value": warning_count,
                "details": (
                    f"expected_tickers={expected_count}; min_short_pct_coverage={min_short_pct_coverage:.2f}; "
                    "coverage gaps do not block the clean panel because IC classifies coverage-limited factors."
                ),
            }
        )
        summary_rows.append(
            {
                "check": "no_old_primary_cohorts",
                "status": "PASS" if not old_cohort_rows else "FAIL",
                "value": "|".join(f"{row['biotech_primary_cohort']}:{row['n']}" for row in old_cohort_rows),
                "details": "daily_scores.biotech_primary_cohort must use only the five calibration cohorts.",
            }
        )
    return summary_rows, detail_rows


def fail_if_qa_failed(rows: list[dict[str, Any]]) -> None:
    failures = [row for row in rows if str(row.get("status")) == "FAIL"]
    if failures:
        sample = "; ".join(f"{row.get('check')}={row.get('value')}" for row in failures[:10])
        raise RuntimeError("Historical clean-panel QA failed: " + sample)


def planned_sequence(args: argparse.Namespace) -> list[str]:
    steps: list[str] = []
    if not args.skip_recompute:
        if not args.skip_market_bar_backfill:
            steps.append("market_bar_backfill")
        steps.append("historical_restatement")
    if not args.skip_qa:
        steps.append("clean_panel_qa")
    if not args.skip_ic:
        steps.append("feature_ic_monitor")
    if not args.skip_baseline_backtest:
        steps.append("phase1_baseline_backtest")
    if args.run_candidate_calibration:
        steps.append("candidate_calibration")
    if args.run_optuna:
        steps.append("gated_optuna")
    return steps


def validate_optuna_gate(args: argparse.Namespace) -> Path:
    if not args.run_candidate_calibration:
        raise RuntimeError("Optuna is gated behind candidate calibration. Re-run with --run-candidate-calibration before --run-optuna.")
    script_path = args.optuna_script.expanduser().resolve()
    if not script_path.exists():
        raise FileNotFoundError(
            f"Optuna optimizer script is not implemented yet: {script_path}. "
            "Run the clean panel, QA, IC, and candidate calibration first; then add the optimizer against surviving structures."
        )
    return script_path


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=config_path.parent)
    if int(args.target_weekly_date_count or 0) > 0 or args.start_asof:
        inferred_end = args.end_asof
        if not inferred_end:
            _, inferred_end, _ = load_history_bounds(db_path, source_table=args.history_date_source, start_asof="", end_asof="")
        history_dates = friday_grid(
            start_asof=args.start_asof,
            end_asof=inferred_end,
            target_count=max(0, int(args.target_weekly_date_count or 0)),
        )
        start_asof = history_dates[0]
        end_asof = history_dates[-1]
    else:
        start_asof, end_asof, history_dates = load_history_bounds(
            db_path,
            source_table=args.history_date_source,
            start_asof=args.start_asof,
            end_asof=args.end_asof,
        )
    market_history_start_asof = args.market_history_start_asof or default_market_history_start(start_asof)
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else DEFAULT_OUTPUT_ROOT / f"{compact_date(start_asof)}_{compact_date(end_asof)}"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    staging_paths = validate_staging_paths(config, config_path=config_path, db_path=db_path)
    expected_tickers = read_expected_tickers(config, config_path=config_path)
    timing_rows: list[dict[str, Any]] = []
    data_rules = {
        "created_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "start_asof": start_asof,
        "end_asof": end_asof,
        "history_date_count": len(history_dates),
        "target_weekly_date_count": int(args.target_weekly_date_count or 0),
        "market_history_start_asof": market_history_start_asof,
        "expected_ticker_count": len(expected_tickers),
        "staging_paths": staging_paths,
        "point_in_time_rules": {
            "finra_short_interest": "short_interest_snapshots.asof_date is the public publication/as-of date.",
            "sec_13f": "13F ownership snapshots are available by filing/as-of date, not quarter-end alone.",
            "form4": "governance_events.form4_db_path must point to the staging Form 4 DB and use filing dates.",
            "ibkr_borrow": "borrow fee rows are dated by IBKR borrow-fee as-of date.",
            "ctgov": "CTGov forward catalyst dates remain shadow-only unless source-aware IC validates promotion.",
        },
    }
    write_json(output_dir / "clean_historical_data_rules_manifest.json", data_rules)

    if not args.skip_recompute:
        if not args.skip_market_bar_backfill:
            cmd = [
                sys.executable,
                str(PACKAGE_ROOT / "scripts" / "17_sync_market_data_yahoo_adjusted.py"),
                "--config",
                str(config_path),
                "--db",
                str(db_path),
                "--asof",
                end_asof,
                "--start-date",
                market_history_start_asof,
                "--allow-partial",
            ]
            run_command(label="market_bar_backfill", command=cmd, output_dir=output_dir, dry_run=args.dry_run, timing_rows=timing_rows)
        cmd = [
            sys.executable,
            str(PACKAGE_ROOT / "scripts" / "24_run_biotech_refresh_pipeline.py"),
            "--config",
            str(config_path),
            "--db",
            str(db_path),
            "--asof",
            end_asof,
            "--history-restatement",
            "--history-date-source",
            args.history_date_source,
            "--history-start-asof",
            start_asof,
            "--history-end-asof",
            end_asof,
            "--history-market-start-asof",
            market_history_start_asof,
        ]
        if int(args.target_weekly_date_count or 0) > 0 or args.start_asof:
            cmd.extend(["--history-dates", ",".join(history_dates)])
        if args.history_fridays_only:
            cmd.append("--history-fridays-only")
        run_command(label="historical_restatement", command=cmd, output_dir=output_dir, dry_run=args.dry_run, timing_rows=timing_rows)
        if args.dry_run:
            write_json(
                output_dir / "clean_historical_sequence_manifest.json",
                {
                    "output_dir": str(output_dir),
                    "start_asof": start_asof,
                    "end_asof": end_asof,
                    "history_date_count": len(history_dates),
                    "planned_full_sequence": planned_sequence(args),
                    "steps": [row["step"] for row in timing_rows],
                    "dry_run": True,
                },
            )
            LOGGER.info("Clean historical sequence dry-run complete: output_dir=%s", output_dir)
            return

    if not args.skip_qa:
        qa_rows = config_guardrail_qa(config)
        source_rows = source_table_qa(Path(staging_paths["market_positioning_db"]), end_asof=end_asof)
        panel_summary, panel_detail = panel_qa(
            db_path,
            expected_tickers=expected_tickers,
            start_asof=start_asof,
            end_asof=end_asof,
            expected_dates=history_dates,
            min_short_pct_coverage=max(0.0, min(1.0, float(args.min_short_pct_coverage))),
        )
        qa_summary = qa_rows + source_rows + panel_summary
        write_csv(output_dir / "clean_historical_qa_summary.csv", qa_summary)
        write_csv(output_dir / "clean_historical_panel_qa_by_asof.csv", panel_detail)
        fail_if_qa_failed(qa_summary)

    if not args.skip_ic:
        cmd = [
            sys.executable,
            str(PACKAGE_ROOT / "scripts" / "43_validate_biotech_feature_ic_monotonicity.py"),
            "--config",
            str(config_path),
            "--db",
            str(db_path),
            "--output-dir",
            str(output_dir / "feature_ic_monitor"),
            "--start-asof",
            start_asof,
            "--end-asof",
            end_asof,
            "--horizons",
            args.horizons,
            "--strict-feature-lag",
            "--next-bar-entry",
            "--resume",
        ]
        run_command(label="feature_ic_monitor", command=cmd, output_dir=output_dir, dry_run=args.dry_run, timing_rows=timing_rows)

    if not args.skip_baseline_backtest:
        cmd = [
            sys.executable,
            str(PACKAGE_ROOT / "scripts" / "27_calibration_phase1_backtest.py"),
            "--config",
            str(config_path),
            "--db",
            str(db_path),
            "--output-dir",
            str(output_dir / "phase1_baseline"),
            "--start-asof",
            start_asof,
            "--end-asof",
            end_asof,
            "--horizons",
            args.horizons,
            "--top-n",
            args.top_n,
            "--bootstrap-iterations",
            str(max(0, int(args.bootstrap_iterations))),
            "--next-bar-entry",
        ]
        run_command(label="phase1_baseline_backtest", command=cmd, output_dir=output_dir, dry_run=args.dry_run, timing_rows=timing_rows)

    if args.run_candidate_calibration:
        cmd = [
            sys.executable,
            str(PACKAGE_ROOT / "scripts" / "28_calibrate_biotech_opportunity.py"),
            "--config",
            str(config_path),
            "--db",
            str(db_path),
            "--output-dir",
            str(output_dir / "candidate_calibration"),
            "--start-asof",
            start_asof,
            "--end-asof",
            end_asof,
            "--horizons",
            args.horizons,
            "--top-n",
            args.top_n,
            "--bootstrap-iterations",
            str(max(0, int(args.bootstrap_iterations))),
            "--bootstrap-top-k",
            "5",
            "--holdout-top-k",
            "10",
            "--strict-feature-lag",
            "--next-bar-entry",
        ]
        if args.candidate_name_filter:
            cmd.extend(["--candidate-name-filter", args.candidate_name_filter])
        if args.policy_name_filter:
            cmd.extend(["--policy-name-filter", args.policy_name_filter])
        if int(args.calibration_max_workers or 0) > 0:
            cmd.extend(["--max-workers", str(int(args.calibration_max_workers))])
        if args.candidate_grid_executor:
            cmd.extend(["--candidate-grid-executor", args.candidate_grid_executor])
        run_command(label="candidate_calibration", command=cmd, output_dir=output_dir, dry_run=args.dry_run, timing_rows=timing_rows)

    optuna_status = "not_requested"
    if args.run_optuna:
        optuna_script = validate_optuna_gate(args)
        cmd = [
            sys.executable,
            str(optuna_script),
            "--config",
            str(config_path),
            "--db",
            str(db_path),
            "--input-dir",
            str(output_dir / "candidate_calibration"),
            "--feature-ic-dir",
            str(output_dir / "feature_ic_monitor"),
            "--output-dir",
            str(output_dir / "optuna"),
            "--start-asof",
            start_asof,
            "--end-asof",
            end_asof,
            "--horizons",
            args.horizons,
            "--top-n",
            args.top_n,
        ]
        run_command(label="gated_optuna", command=cmd, output_dir=output_dir, dry_run=args.dry_run, timing_rows=timing_rows)
        optuna_status = "success"

    final_manifest = {
        "output_dir": str(output_dir),
        "start_asof": start_asof,
        "end_asof": end_asof,
        "history_date_count": len(history_dates),
        "planned_full_sequence": planned_sequence(args),
        "steps": [row["step"] for row in timing_rows],
        "candidate_calibration_run": bool(args.run_candidate_calibration),
        "optuna_status": optuna_status,
    }
    write_json(output_dir / "clean_historical_sequence_manifest.json", final_manifest)
    LOGGER.info("Clean historical sequence complete: output_dir=%s", output_dir)


if __name__ == "__main__":
    main()
