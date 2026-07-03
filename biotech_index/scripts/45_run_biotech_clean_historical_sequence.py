#!/usr/bin/env python3
from __future__ import annotations

import argparse
import bisect
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
    parser.add_argument(
        "--no-snap-weekly-to-market-days",
        action="store_true",
        help="Keep requested weekly Friday dates exactly instead of snapping market holidays to the prior market bar date.",
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
    parser.add_argument(
        "--step-timeout-sec",
        type=int,
        default=0,
        help=(
            "Optional timeout applied to each child step. Defaults to "
            "biotech_historical_sequence.step_timeout_sec; 0 disables the timeout."
        ),
    )
    parser.add_argument("--skip-recompute", action="store_true")
    parser.add_argument("--skip-market-bar-backfill", action="store_true")
    parser.add_argument("--skip-qa", action="store_true")
    parser.add_argument(
        "--skip-historical-score-csvs",
        action="store_true",
        help=(
            "Skip generation/validation of output/biotech_index_reports/<YYYYMMDD>/biotech_daily_scores.csv "
            "for each historical calibration date."
        ),
    )
    parser.add_argument(
        "--historical-score-csv-overwrite",
        action="store_true",
        help="Overwrite existing historical biotech_daily_scores.csv files even when --skip-recompute is used.",
    )
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


def snap_to_available_market_dates(db_path: Path, dates: list[str]) -> list[str]:
    """Snap calendar date-grid entries to the latest available market-bar date.

    Weekly calibration grids are expressed as Fridays, but some Fridays are market
    holidays.  Using the prior market day preserves point-in-time ordering and
    avoids trying to score a non-trading as-of date.
    """
    parsed_dates = [parse_date(item) for item in dates]
    valid_dates = [item for item in parsed_dates if item is not None]
    if not valid_dates:
        return dates
    min_query = (min(valid_dates) - timedelta(days=10)).isoformat()
    max_query = max(valid_dates).isoformat()
    with closing(connect_readonly(db_path)) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT bar_date
            FROM market_bars_daily
            WHERE bar_date BETWEEN ? AND ?
            ORDER BY bar_date
            """,
            (min_query, max_query),
        ).fetchall()
    market_dates = [parse_date(row["bar_date"]) for row in rows]
    market_dates = [item for item in market_dates if item is not None]
    if not market_dates:
        raise RuntimeError("Cannot snap weekly grid because market_bars_daily has no dates in the requested range.")

    snapped: list[str] = []
    for raw, parsed in zip(dates, parsed_dates):
        if parsed is None:
            continue
        idx = bisect.bisect_right(market_dates, parsed) - 1
        if idx < 0:
            raise RuntimeError(f"No market date available on or before requested historical as-of {raw}")
        snapped.append(market_dates[idx].isoformat())
    snapped_unique = list(dict.fromkeys(snapped))
    if len(snapped_unique) < len(snapped):
        LOGGER.warning(
            "Snapping the weekly grid to market days collapsed duplicate dates: "
            "%d requested dates -> %d unique grid dates.",
            len(snapped),
            len(snapped_unique),
        )
    return snapped_unique


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


def read_calibration_tickers(config: dict[str, Any], *, config_path: Path) -> set[str]:
    settings = cfg_get(config, "biotech_scoring.calibration_cohorts", {}) or {}
    if not isinstance(settings, dict):
        settings = {}
    csv_path = resolve_path(settings.get("csv", "data/biotech_calibration_cohorts.csv"), base_dir=config_path.parent)
    out: set[str] = set()
    for path, columns in (
        (csv_path, ("ticker",)),
        (config_path.parent / "data" / "delisted_biotech_calibration_universe.csv", ("ticker", "calibration_company_ticker")),
    ):
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                for column in columns:
                    ticker = str(row.get(column) or "").strip().upper()
                    if ticker:
                        out.add(ticker)
    return out


def read_dated_expected_tickers(
    *,
    base_output_dir: Path,
    configured_universe_name: str,
    asof: str,
    fallback_tickers: set[str],
) -> tuple[set[str], str]:
    dated_path = base_output_dir / compact_date(asof) / configured_universe_name
    if not dated_path.exists():
        LOGGER.warning(
            "Dated universe CSV is missing for %s; falling back to the CURRENT universe: %s",
            asof,
            dated_path,
        )
        return set(fallback_tickers), "current_universe_fallback"
    out: set[str] = set()
    with dated_path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if as_bool(row.get("scoring_include"), False):
                ticker = str(row.get("ticker") or "").strip().upper()
                if ticker:
                    out.add(ticker)
    if out:
        return out, "dated_universe"
    LOGGER.warning(
        "Dated universe CSV has no scoring_include tickers for %s; falling back to the CURRENT universe: %s",
        asof,
        dated_path,
    )
    return set(fallback_tickers), "current_universe_fallback"


def run_command(
    *,
    label: str,
    command: list[str],
    output_dir: Path,
    dry_run: bool,
    timeout_sec: int,
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
        "timeout_sec": max(0, int(timeout_sec or 0)),
    }
    timing_rows.append(row)
    write_csv(output_dir / "clean_historical_sequence_timing.csv", timing_rows)
    if dry_run:
        return
    try:
        result = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            timeout=max(0, int(timeout_sec or 0)) or None,
        )
    except subprocess.TimeoutExpired as exc:
        row["elapsed_sec"] = round(time.monotonic() - start, 3)
        row["returncode"] = "timeout"
        row["status"] = "timeout"
        write_csv(output_dir / "clean_historical_sequence_timing.csv", timing_rows)
        raise TimeoutError(f"{label} exceeded timeout_sec={max(0, int(timeout_sec or 0))}") from exc
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
    production_score_source = str(routing.get("production_score_source") or "opportunity_score").strip().lower()
    allocation_source_aliases = {"legacy", "legacy_allocation", "allocation", "opportunity", "opportunity_score"}
    rows = [
        {
            "check": "production_score_source_allocation_only",
            "status": "PASS" if production_score_source in allocation_source_aliases else "FAIL",
            "value": production_score_source,
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
    calibration_tickers: set[str],
    base_output_dir: Path,
    configured_universe_name: str,
    start_asof: str,
    end_asof: str,
    expected_dates: list[str] | None = None,
    min_short_pct_coverage: float = 0.90,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
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
        calibration_ticker_values = sorted(ticker for ticker in calibration_tickers if ticker)
        calibration_placeholders = ", ".join("?" for _ in calibration_ticker_values)
        calibration_filter = (
            f"(c.universe_status = 'delisted_calibration' OR UPPER(c.ticker) IN ({calibration_placeholders}))"
            if calibration_ticker_values
            else "1 = 1"
        )
        for asof in dates:
            dated_expected_tickers, expected_ticker_source = read_dated_expected_tickers(
                base_output_dir=base_output_dir,
                configured_universe_name=configured_universe_name,
                asof=asof,
                fallback_tickers=expected_tickers,
            )
            feature = conn.execute(
                f"""
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
                FROM daily_features f
                JOIN companies c ON c.company_id = f.company_id
                WHERE f.asof_date = ?
                  AND {calibration_filter}
                """,
                (asof, *calibration_ticker_values),
            ).fetchone()
            score = conn.execute(
                f"""
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
                    SUM(CASE WHEN production_score_source IN (
                        'legacy',
                        'legacy_allocation',
                        'allocation',
                        'opportunity',
                        'opportunity_score'
                    ) OR production_score_source LIKE 'legacy_allocation|%' THEN 1 ELSE 0 END) AS allocation_source_rows,
                    SUM(CASE WHEN production_score_source LIKE '%discovery%'
                              OR production_rank_score_field = 'discovery_opportunity_score'
                             THEN 1 ELSE 0 END) AS discovery_rank_source_rows,
                    SUM(CASE WHEN production_rank_score_field = 'opportunity_score' THEN 1 ELSE 0 END) AS allocation_rank_field_rows,
                    SUM(CASE WHEN COALESCE(discovery_opportunity_score, 0.0) > 0.0 THEN 1 ELSE 0 END) AS discovery_score_rows
                FROM daily_scores s
                JOIN companies c ON c.company_id = s.company_id
                WHERE s.asof_date = ?
                  AND {calibration_filter}
                """,
                (asof, *calibration_ticker_values),
            ).fetchone()
            feature_count = int(feature["n"] or 0)
            score_count = int(score["n"] or 0)
            has_dated_universe = expected_ticker_source == "dated_universe"
            expected_count = len(dated_expected_tickers) if has_dated_universe else max(feature_count, score_count)
            row = {
                "asof_date": asof,
                "expected_tickers": expected_count,
                "expected_ticker_source": expected_ticker_source,
                "daily_features_rows": feature_count,
                "daily_scores_rows": score_count,
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
                "allocation_source_rows": int(score["allocation_source_rows"] or 0),
                "discovery_rank_source_rows": int(score["discovery_rank_source_rows"] or 0),
                "allocation_rank_field_rows": int(score["allocation_rank_field_rows"] or 0),
                "discovery_score_rows": int(score["discovery_score_rows"] or 0),
            }
            short_pct_min = int(round(expected_count * min_short_pct_coverage))
            failures: list[str] = []
            warnings: list[str] = []
            if expected_dates is not None and not has_dated_universe:
                # An explicit historical grid must have a per-date PIT universe;
                # silently downgrading to current-universe parity hides
                # survivorship gaps, so treat it as a hard panel-QA failure.
                failures.append("missing_dated_universe")
            if has_dated_universe:
                if row["daily_features_rows"] != expected_count:
                    failures.append("daily_features_row_count")
                if row["daily_scores_rows"] != expected_count:
                    failures.append("daily_scores_row_count")
            elif row["daily_features_rows"] != row["daily_scores_rows"]:
                failures.append("daily_features_scores_row_count_mismatch")
            if row["short_pct_available_rows"] < short_pct_min:
                warnings.append("short_interest_pct_float_coverage")
            if row["five_cohort_rows"] != row["daily_scores_rows"]:
                failures.append("five_cohort_assignment")
            # Discovery scores can be present as diagnostics on rows whose
            # production rank is still allocation-based, so discovery_score_rows
            # is not a mutually exclusive source bucket.  Production output is
            # clean only when every row's rank source is allocation and no row
            # routes discovery into production ranking.
            if (
                row["allocation_source_rows"] != row["daily_scores_rows"]
                or row["allocation_rank_field_rows"] != row["daily_scores_rows"]
                or row["discovery_rank_source_rows"] > 0
            ):
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
                "details": "expected_tickers=dated_universe_per_asof",
            }
        )
        summary_rows.append(
            {
                "check": "short_interest_pct_float_coverage_advisory",
                "status": "PASS" if warning_count == 0 else "WARN",
                "value": warning_count,
                "details": (
                    f"expected_tickers=dated_universe_per_asof; min_short_pct_coverage={min_short_pct_coverage:.2f}; "
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
    if not args.skip_historical_score_csvs:
        steps.append("historical_score_csv_generation")
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
    step_timeout_sec = (
        int(args.step_timeout_sec)
        if int(args.step_timeout_sec or 0) > 0
        else int(cfg_get(config, "biotech_historical_sequence.step_timeout_sec", 0) or 0)
    )
    if int(args.target_weekly_date_count or 0) > 0 or args.start_asof:
        inferred_end = args.end_asof
        if not inferred_end:
            _, inferred_end, _ = load_history_bounds(db_path, source_table=args.history_date_source, start_asof="", end_asof="")
        history_dates = friday_grid(
            start_asof=args.start_asof,
            end_asof=inferred_end,
            target_count=max(0, int(args.target_weekly_date_count or 0)),
        )
        if not args.no_snap_weekly_to_market_days:
            history_dates = snap_to_available_market_dates(db_path, history_dates)
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
    calibration_tickers = read_calibration_tickers(config, config_path=config_path)
    timing_rows: list[dict[str, Any]] = []
    data_rules = {
        "created_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "start_asof": start_asof,
        "end_asof": end_asof,
        "history_date_count": len(history_dates),
        "target_weekly_date_count": int(args.target_weekly_date_count or 0),
        "weekly_dates_snapped_to_market_days": (
            bool(int(args.target_weekly_date_count or 0) > 0 or args.start_asof)
            and not bool(args.no_snap_weekly_to_market_days)
        ),
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

    if args.dry_run and args.skip_recompute:
        planned_steps = planned_sequence(args)
        timing_rows.extend(
            {
                "step": planned_step,
                "command": "planned_dry_run",
                "status": "dry_run",
                "elapsed_sec": "",
                "returncode": "",
                "timeout_sec": max(0, int(step_timeout_sec or 0)),
            }
            for planned_step in planned_steps
        )
        write_csv(output_dir / "clean_historical_sequence_timing.csv", timing_rows)
        write_json(
            output_dir / "clean_historical_sequence_manifest.json",
            {
                "output_dir": str(output_dir),
                "start_asof": start_asof,
                "end_asof": end_asof,
                "history_date_count": len(history_dates),
                "planned_full_sequence": planned_steps,
                "steps": planned_steps,
                "dry_run": True,
            },
        )
        LOGGER.info("Clean historical sequence dry-run complete: output_dir=%s", output_dir)
        return

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
            run_command(label="market_bar_backfill", command=cmd, output_dir=output_dir, dry_run=args.dry_run, timeout_sec=step_timeout_sec, timing_rows=timing_rows)
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
        run_command(label="historical_restatement", command=cmd, output_dir=output_dir, dry_run=args.dry_run, timeout_sec=step_timeout_sec, timing_rows=timing_rows)
        if args.dry_run:
            planned_steps = planned_sequence(args)
            recorded_steps = {str(row.get("step") or "") for row in timing_rows}
            for planned_step in planned_steps:
                if planned_step in recorded_steps:
                    continue
                timing_rows.append(
                    {
                        "step": planned_step,
                        "command": "planned_after_recompute_boundary",
                        "status": "dry_run",
                        "elapsed_sec": "",
                        "returncode": "",
                        "timeout_sec": max(0, int(step_timeout_sec or 0)),
                    }
                )
            write_csv(output_dir / "clean_historical_sequence_timing.csv", timing_rows)
            write_json(
                output_dir / "clean_historical_sequence_manifest.json",
                {
                    "output_dir": str(output_dir),
                    "start_asof": start_asof,
                    "end_asof": end_asof,
                    "history_date_count": len(history_dates),
                    "planned_full_sequence": planned_steps,
                    "steps": planned_steps,
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
            calibration_tickers=calibration_tickers,
            base_output_dir=resolve_path(cfg_get(config, "biotech_scoring.output_dir", "../output/biotech_index_reports"), base_dir=config_path.parent),
            configured_universe_name=Path(str(cfg_get(config, "biotech_features.final_scoring_universe_csv"))).name,
            start_asof=start_asof,
            end_asof=end_asof,
            expected_dates=history_dates,
            min_short_pct_coverage=max(0.0, min(1.0, float(args.min_short_pct_coverage))),
        )
        qa_summary = qa_rows + source_rows + panel_summary
        write_csv(output_dir / "clean_historical_qa_summary.csv", qa_summary)
        write_csv(output_dir / "clean_historical_panel_qa_by_asof.csv", panel_detail)
        fail_if_qa_failed(qa_summary)

    if not args.skip_historical_score_csvs:
        cmd = [
            sys.executable,
            str(PACKAGE_ROOT / "scripts" / "56_generate_historical_biotech_score_csvs.py"),
            "--config",
            str(config_path),
            "--db",
            str(db_path),
            "--dates",
            ",".join(history_dates),
            "--summary-csv",
            str(output_dir / "historical_score_csv_generation_summary.csv"),
            "--manifest-json",
            str(output_dir / "historical_score_csv_generation_manifest.json"),
        ]
        if not args.skip_recompute or args.historical_score_csv_overwrite:
            cmd.append("--overwrite")
        run_command(label="historical_score_csv_generation", command=cmd, output_dir=output_dir, dry_run=args.dry_run, timeout_sec=step_timeout_sec, timing_rows=timing_rows)

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
        run_command(label="feature_ic_monitor", command=cmd, output_dir=output_dir, dry_run=args.dry_run, timeout_sec=step_timeout_sec, timing_rows=timing_rows)

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
        run_command(label="phase1_baseline_backtest", command=cmd, output_dir=output_dir, dry_run=args.dry_run, timeout_sec=step_timeout_sec, timing_rows=timing_rows)

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
        run_command(label="candidate_calibration", command=cmd, output_dir=output_dir, dry_run=args.dry_run, timeout_sec=step_timeout_sec, timing_rows=timing_rows)

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
        run_command(label="gated_optuna", command=cmd, output_dir=output_dir, dry_run=args.dry_run, timeout_sec=step_timeout_sec, timing_rows=timing_rows)
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
