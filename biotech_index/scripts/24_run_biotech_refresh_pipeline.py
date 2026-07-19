#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import signal
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, expand_env_vars, load_yaml, resolve_path  # noqa: E402
from biotech_index.core.db import connect, quote_identifier  # noqa: E402
from biotech_index.core.logging_utils import configure_utc_logging  # noqa: E402
from biotech_index.core.market_policy import scoring_market_sources  # noqa: E402
from biotech_index.core.pipeline_guards import format_ticker_sample, read_final_scoring_tickers, universe_coverage  # noqa: E402


LOGGER = logging.getLogger("run_biotech_refresh_pipeline")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_FORM4_RAW_FILING_DATE_SOURCES = (
    ("sec_ownership_submission", "filing_date"),
    ("sec_form4_daily_ingest_log", "filing_date"),
    ("form4_events_tier1", "filing_date"),
    ("form4_buy_events_v1", "filing_date"),
)

BIOTECH_SCORE_REQUIRED_COLUMNS = [
    "tier1_selection_gate_score",
    "data_quality_confidence_multiplier",
    "clinical_risk_drag",
    "investment_risk_drag",
]

BIOTECH_SCORE_CSV_REQUIRED_COLUMNS = [
    *BIOTECH_SCORE_REQUIRED_COLUMNS,
    "tier1_primary_horizon_trading_days",
    "tier1_production_score_model",
    "tier1_selection_policy",
    "alpha_multibagger_role",
    "core_structural_veto_flag",
    "rank_demoted_by_core_veto",
    "effective_total_risk_drag",
    "portfolio_candidate_gate",
    "portfolio_candidate_score",
    "portfolio_candidate_status",
    "portfolio_candidate_reason",
    "calibration_eligible_flag",
    "native_score_field",
    "native_score_value",
    "score_zero_is_missing_flag",
    "research_calibration_input_eligible_flag",
    "research_calibration_status",
    "research_calibration_reason",
    "calibration_sample_role",
    "oos_score_valid_flag",
    "stage11_calibration_input_eligible_flag",
    "stage11_calibration_input_reason",
    "stage11_calibration_panel_source",
    "survivorship_corrected_panel_flag",
]

BIOTECH_SCORE_CSV_PRESENT_COLUMNS = [
    "core_structural_veto_reasons",
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
    parser.add_argument(
        "--skip-market-positioning",
        action="store_true",
        help="Skip FINRA short-interest, SEC 13F, and IBKR borrow positioning refresh/export.",
    )
    parser.add_argument("--skip-analyze", action="store_true", help="Skip SQLite ANALYZE at the end.")
    parser.add_argument("--skip-final-validation", action="store_true", help="Skip final as-of/coverage validation after a full pipeline run.")
    parser.add_argument("--skip-form4-refresh", action="store_true", help="Skip the staging Form 4 refresh step before preflight.")
    parser.add_argument("--skip-form4-preflight", action="store_true", help="Skip the staging Form 4 database freshness preflight.")
    parser.add_argument("--reuse-unchanged-historical", action="store_true", help="Reuse exact-signature governance rows for historical snapshot runs.")
    parser.add_argument(
        "--history-restatement",
        action="store_true",
        help=(
            "Restate derived historical feature/score layers over a date grid after raw source tables are current. "
            "This does not run raw upstream sync steps."
        ),
    )
    parser.add_argument("--history-start-asof", type=str, default="", help="Earliest YYYY-MM-DD date for --history-restatement.")
    parser.add_argument("--history-end-asof", type=str, default="", help="Latest YYYY-MM-DD date for --history-restatement. Defaults to --asof.")
    parser.add_argument(
        "--history-market-start-asof",
        type=str,
        default="",
        help=(
            "Earliest market-bar date to use when building historical market feature snapshots. "
            "Use a date at least one year before --history-start-asof for 200d/52w metrics."
        ),
    )
    parser.add_argument(
        "--history-dates",
        type=str,
        default="",
        help="Optional comma-separated YYYY-MM-DD date grid for --history-restatement. Overrides source-table date loading.",
    )
    parser.add_argument(
        "--history-date-source",
        choices=["daily_scores", "daily_features"],
        default="daily_scores",
        help="Source table for --history-restatement date grid when --history-dates is omitted.",
    )
    parser.add_argument(
        "--history-fridays-only",
        action="store_true",
        help="Restrict --history-restatement source-table date grid to Fridays.",
    )
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


def parse_db_date(raw: object) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    candidates = [text]
    if "T" in text:
        candidates.append(text.split("T", 1)[0])
    elif len(text) > 10:
        candidates.append(text[:10])
    for candidate in candidates:
        parsed = parse_date(candidate)
        if parsed is not None:
            return parsed
    for fmt in ("%Y%m%d", "%Y/%m/%d", "%d-%b-%Y", "%d-%B-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def compact_asof(asof: str) -> str:
    parsed = parse_date(asof)
    return parsed.strftime("%Y%m%d") if parsed is not None else str(asof).replace("-", "")


def dated_output_file(base_dir: Path, asof: str, filename: str) -> Path:
    dated_dir = base_dir if base_dir.name == compact_asof(asof) else base_dir / compact_asof(asof)
    return dated_dir / filename


def output_file_with_dated_fallback(base_dir: Path, asof: str, filename: str) -> Path:
    root_file = base_dir / filename
    return root_file if root_file.exists() else dated_output_file(base_dir, asof, filename)


def as_bool(raw: object, default: bool = False) -> bool:
    if raw is None:
        return default
    text = str(raw).strip().lower()
    if text in {"1", "true", "t", "yes", "y", "enabled", "on"}:
        return True
    if text in {"0", "false", "f", "no", "n", "disabled", "off"}:
        return False
    return default


def to_float(raw: object, default: float = 0.0) -> float:
    try:
        text = str(raw if raw is not None else "").strip().replace(",", "")
        return float(text) if text else default
    except (TypeError, ValueError):
        return default


def parse_string_list(raw: object, default: list[str] | None = None) -> list[str]:
    if raw is None:
        return list(default or [])
    if isinstance(raw, str):
        parts = raw.replace(";", ",").replace("|", ",").split(",")
    elif isinstance(raw, (list, tuple, set)):
        parts = [str(item) for item in raw]
    else:
        parts = [str(raw)]
    values = [part.strip() for part in parts if part.strip()]
    return values or list(default or [])


def parse_table_column_sources(
    raw: object,
    default: tuple[tuple[str, str], ...],
) -> list[tuple[str, str]]:
    if raw is None:
        return list(default)
    values: list[tuple[str, str]] = []
    if isinstance(raw, dict):
        raw_items: list[object] = [raw]
    elif isinstance(raw, (list, tuple)):
        raw_items = list(raw)
    else:
        raw_items = [str(raw)]
    for item in raw_items:
        if isinstance(item, dict):
            table = str(item.get("table") or "").strip()
            column = str(item.get("column") or "").strip()
        else:
            text = str(item or "").strip()
            if "." not in text:
                raise ValueError(
                    "Form 4 raw filing date sources must be mappings with table/column "
                    f"or strings in table.column form; got {item!r}"
                )
            table, column = (part.strip() for part in text.split(".", 1))
        if not table or not column:
            raise ValueError(f"Invalid Form 4 raw filing date source: {item!r}")
        values.append((table, column))
    return values or list(default)


def resolved_path_text(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def path_has_forbidden_marker(path: Path, forbidden_markers: list[str]) -> bool:
    text = resolved_path_text(path).replace("\\", "/").lower()
    for marker in forbidden_markers:
        clean = str(marker or "").strip()
        if clean and clean.replace("\\", "/").lower() in text:
            return True
    return False


def same_path(left: Path, right: Path) -> bool:
    return resolved_path_text(left).lower() == resolved_path_text(right).lower()


def form4_forbidden_path_markers(config: dict[str, Any]) -> list[str]:
    return parse_string_list(
        cfg_get(
            config,
            "biotech_refresh.form4_refresh.forbid_path_markers",
            ["PROD_Scalper_System", "/PROD/", "\\PROD\\"],
        ),
        ["PROD_Scalper_System", "/PROD/", "\\PROD\\"],
    )


def validate_form4_staging_boundary(
    config: dict[str, Any],
    *,
    base_dir: Path,
    db_path: Path | None = None,
    extra_paths: list[tuple[str, Path | None]] | None = None,
) -> Path:
    """Validate that Form 4 refresh/read paths stay inside the staging boundary."""
    governance_db_path = resolve_path(cfg_get(config, "governance_events.form4_db_path"), base_dir=base_dir)
    expected_raw = cfg_get(config, "biotech_refresh.form4_refresh.expected_db_path", None)
    expected_db_path = resolve_path(expected_raw, base_dir=base_dir) if expected_raw else governance_db_path
    active_db_path = db_path or governance_db_path
    forbidden = form4_forbidden_path_markers(config)

    for label, path in [
        ("governance_events.form4_db_path", governance_db_path),
        ("biotech_refresh.form4_refresh.expected_db_path", expected_db_path),
        ("active_form4_db_path", active_db_path),
    ]:
        if path_has_forbidden_marker(path, forbidden):
            raise RuntimeError(f"Form 4 {label} is outside the staging boundary: {path}")

    if not same_path(governance_db_path, expected_db_path):
        raise RuntimeError(
            "Form 4 staging boundary mismatch: governance_events.form4_db_path="
            f"{governance_db_path} expected_db_path={expected_db_path}"
        )
    if not same_path(active_db_path, governance_db_path):
        raise RuntimeError(
            f"Form 4 refresh DB path must match governance_events.form4_db_path: "
            f"refresh_db={active_db_path} governance_db={governance_db_path}"
        )

    for label, path in extra_paths or []:
        if path is not None and path_has_forbidden_marker(path, forbidden):
            raise RuntimeError(f"Form 4 {label} points outside staging boundary: {path}")
    return governance_db_path


def validate_config(config: dict[str, Any]) -> None:
    optional_defaults = {
        "governance_events.form4_required": True,
        "governance_events.reuse_unchanged_historical": False,
        "biotech_refresh.step_timeout_sec": 14400.0,
        "calibration.exclude_tickers": [],
    }
    for key, default in optional_defaults.items():
        if cfg_get(config, key, None) is None:
            LOGGER.warning("Config key %s is missing; defaulting to %r", key, default)

    biotech_buckets = cfg_get(config, "biotech_scoring.buckets", {}) or {}
    if biotech_buckets:
        watch = float(biotech_buckets.get("watchlist_min", 60.0))
        speculative = float(biotech_buckets.get("speculative_min", 45.0))
        if watch <= speculative:
            raise RuntimeError(
                "Invalid biotech_scoring.buckets thresholds: watchlist_min must exceed speculative_min."
            )

    multibagger = cfg_get(config, "multibagger", {}) or {}
    if multibagger:
        weights = multibagger.get("weights", {}) if isinstance(multibagger.get("weights", {}), dict) else {}
        positive_weight_keys = [
            "commercial_acceleration",
            "upside_capacity",
            "cash_flow_acceleration",
            "survival_quality",
            "governance_event",
            "market_confirmation",
            "catalyst_quality",
        ]
        positive_total = sum(float(weights.get(key, 0.0)) for key in positive_weight_keys)
        if weights and abs(positive_total - 1.0) > 1e-6:
            LOGGER.warning(
                "multibagger positive component weights sum to %.6f; scoring normalizes them to 1.0 at runtime.",
                positive_total,
            )
        max_spec = float(multibagger.get("max_speculative_risk", 75.0))
        avoid = float(multibagger.get("avoid_risk_min", 80.0))
        if avoid <= max_spec:
            LOGGER.warning(
                "multibagger.avoid_risk_min (%s) is <= max_speculative_risk (%s); "
                "boundary-risk names may fall to avoid by default.",
                avoid,
                max_spec,
            )


def table_row_count(db_path: Path, table: str) -> int:
    if not db_path.exists():
        return 0
    try:
        with connect(db_path) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if not row or int(row["n"]) <= 0:
                return 0
            count_row = conn.execute(f"SELECT COUNT(*) AS n FROM {quote_identifier(table)}").fetchone()
            return int(count_row["n"] if count_row else 0)
    except sqlite3.Error as exc:
        LOGGER.warning("Could not count table %s in %s: %s", table, db_path, exc)
        return 0


def maybe_skip_company_master(
    steps: list[Step],
    *,
    config: dict[str, Any],
    base_dir: Path,
    db_path: Path,
    mode: str,
    selected_steps: set[str],
) -> list[Step]:
    if selected_steps or not any(step.name == "company_master" for step in steps):
        return steps
    if mode != "daily_delta":
        return steps
    enabled = as_bool(
        cfg_get(config, "biotech_refresh.company_master.reuse_existing_if_screen_missing", True),
        True,
    )
    if not enabled:
        return steps
    screen_path = resolve_path(cfg_get(config, "paths.screen_results_csv"), base_dir=base_dir)
    if screen_path.exists():
        return steps
    company_rows = table_row_count(db_path, "companies")
    min_existing = int(cfg_get(config, "biotech_refresh.company_master.min_existing_companies", 100))
    if company_rows < min_existing:
        raise FileNotFoundError(
            f"Screen results CSV not found and existing companies table is too small to reuse: "
            f"screen={screen_path} companies={company_rows} min_existing={min_existing}"
        )
    LOGGER.warning(
        "Skipping company_master in daily_delta because screen CSV is missing but existing companies table is populated: "
        "screen=%s companies=%d",
        screen_path,
        company_rows,
    )
    return [step for step in steps if step.name != "company_master"]


def ensure_final_scoring_universe_for_skip_ctgov(config: dict[str, Any], *, base_dir: Path) -> None:
    """--skip-ctgov relies on a previously generated final scoring universe CSV.

    Downstream steps (e.g. sec_filings) and final validation read this CSV, which
    is normally refreshed by the CTGov steps; fail fast with a clear error instead
    of letting a later step crash on the missing file.
    """
    universe_csv = resolve_path(
        cfg_get(
            config,
            "sec_filings.final_scoring_universe_csv",
            "../output/biotech_index_reports/ctgov_final_scoring_universe.csv",
        ),
        base_dir=base_dir,
    )
    if not universe_csv.exists():
        raise FileNotFoundError(
            "--skip-ctgov requires an existing final scoring universe CSV from a prior CTGov run: "
            f"missing {universe_csv}"
        )


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


def business_day_age(start: date, end: date) -> int:
    """Return weekday-count age from start exclusive to end inclusive."""
    if end < start:
        return -business_day_age(end, start)
    age = 0
    current = start + timedelta(days=1)
    while current <= end:
        if current.weekday() < 5:
            age += 1
        current += timedelta(days=1)
    return age


def snap_asof_to_latest_trading_date(
    candidate: date,
    *,
    db_path: Path,
    config: dict[str, Any],
    max_snap_gap_days: int = 4,
) -> date:
    """Snap a weekday-derived asof candidate to the latest available market bar date.

    Sat/Sun-only weekday logic resolves a weekday market holiday to a non-trading
    day, which fails downstream coverage validation and inflates Form 4 staleness.
    Prefer the primary scoring source's bars; fall back to any source. Keep the
    weekday-based candidate (with a warning) when the DB has no usable bars yet
    (fresh install) or the newest bar is more than max_snap_gap_days behind the
    candidate (stale market data; this run will sync the missing dates).
    """
    if not db_path.exists():
        LOGGER.warning(
            "Cannot snap asof %s to a trading date because the database does not exist yet: %s",
            candidate.isoformat(),
            db_path,
        )
        return candidate
    latest: date | None = None
    try:
        with connect(db_path) as conn:
            table_row = conn.execute(
                "SELECT COUNT(*) AS n FROM sqlite_master WHERE type='table' AND name='market_bars_daily'"
            ).fetchone()
            if not table_row or int(table_row["n"]) <= 0:
                LOGGER.warning(
                    "Cannot snap asof %s to a trading date because market_bars_daily does not exist yet (fresh install?).",
                    candidate.isoformat(),
                )
                return candidate
            for source in scoring_market_sources(config):
                row = conn.execute(
                    "SELECT MAX(bar_date) AS latest FROM market_bars_daily WHERE source = ? AND bar_date <= ?",
                    (source, candidate.isoformat()),
                ).fetchone()
                latest = parse_db_date(row["latest"] if row else None)
                if latest is not None:
                    break
            if latest is None:
                row = conn.execute(
                    "SELECT MAX(bar_date) AS latest FROM market_bars_daily WHERE bar_date <= ?",
                    (candidate.isoformat(),),
                ).fetchone()
                latest = parse_db_date(row["latest"] if row else None)
    except sqlite3.Error as exc:
        LOGGER.warning("Cannot snap asof %s to a trading date: %s", candidate.isoformat(), exc)
        return candidate
    if latest is None:
        LOGGER.warning(
            "No market bars found at or before %s; keeping weekday-based asof (fresh install?).",
            candidate.isoformat(),
        )
        return candidate
    if latest == candidate:
        return candidate
    gap_days = (candidate - latest).days
    if gap_days > max_snap_gap_days:
        LOGGER.warning(
            "Newest market bar %s is %d calendar day(s) before candidate asof %s; keeping weekday-based asof "
            "because the market data looks stale rather than the candidate being a holiday.",
            latest.isoformat(),
            gap_days,
            candidate.isoformat(),
        )
        return candidate
    LOGGER.warning(
        "Snapped pipeline asof from weekday-based %s to latest available trading date %s (weekday market holiday).",
        candidate.isoformat(),
        latest.isoformat(),
    )
    return latest


def default_pipeline_asof(config: dict[str, Any], db_path: Path | None = None) -> date:
    market_timezone = str(cfg_get(config, "ib_market_data.market_timezone", "America/New_York"))
    market_close_time = parse_clock_time(cfg_get(config, "ib_market_data.market_close_time", "16:15"))
    guard_enabled = as_bool(cfg_get(config, "ib_market_data.market_close_guard", True))
    now_local = datetime.now(timezone.utc).astimezone(ZoneInfo(market_timezone))
    local_today = now_local.date()
    if guard_enabled and (local_today.weekday() >= 5 or now_local.time() < market_close_time):
        candidate = previous_business_day(local_today)
    else:
        candidate = local_today
    # Weekday logic alone treats a weekday market holiday as a trading day. A past
    # candidate's bars must already be synced if it was a trading day, so snap it to
    # the latest available bar date. A same-day candidate is left as-is because this
    # run has not synced today's bars yet, so absence of bars proves nothing.
    if db_path is not None and candidate < local_today:
        candidate = snap_asof_to_latest_trading_date(candidate, db_path=db_path, config=config)
    return candidate


def pipeline_steps(
    mode: str,
    *,
    skip_ctgov: bool,
    skip_ib: bool,
    skip_yahoo: bool,
    skip_market_positioning: bool,
    reuse_unchanged_historical: bool = False,
) -> list[Step]:
    # Operational refreshes should parse new/changed SEC text only.  Full SEC
    # event corpus reparsing, including parser-signature refreshes, is reserved
    # for explicit backfills.
    sec_event_args: tuple[str, ...] = (
        ("--full-rescan",) if mode == "full_backfill" else ("--skip-parser-signature-reparse",)
    )
    sec_filings_args: tuple[str, ...] = ("--allow-partial",) if mode == "daily_delta" else ()
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
            Step("sec_filings", "06_sync_sec_filings.py", sec_filings_args),
            Step("sec_events", "07_parse_sec_biotech_events.py", sec_event_args),
            Step("forward_catalyst_calendar", "09_build_forward_catalyst_calendar.py"),
            Step("sec_companyfacts", "15_sync_sec_companyfacts_history.py", companyfacts_args),
            Step("financial_survival", "16_build_financial_survival_features.py"),
        ]
    )
    if not skip_ib:
        steps.append(Step("ib_market", "17_sync_market_data_ib.py", ib_args))
    if not skip_yahoo:
        steps.append(Step("yahoo_market_adjusted", "17_sync_market_data_yahoo_adjusted.py", yahoo_args))
    if not (skip_ib and skip_yahoo):
        steps.append(Step("market_policy_audit", "31_audit_market_data_policy.py"))
    steps.extend(
        [
            Step("commercial_value", "18_build_commercial_value_features.py", commercial_args),
            Step("forward_guidance", "19_parse_forward_guidance.py", forward_args),
            Step("governance_events", "20_build_governance_event_features.py", governance_args),
        ]
    )
    if not skip_market_positioning:
        steps.append(Step("market_positioning", "25_update_market_positioning.py"))
    steps.extend(
        [
            Step("fda_adcom_calendar", "14_sync_fda_adcom_calendar.py"),
            Step("biotech_features", "10_build_biotech_features.py"),
            Step("biotech_scores", "11_score_biotech_index.py"),
            Step("biotech_reports", "12_publish_biotech_reports.py"),
            Step("multibagger_features", "21_build_multibagger_features.py", multibagger_feature_args),
            Step("multibagger_scores", "22_score_multibagger_candidates.py"),
            Step("multibagger_reports", "23_publish_multibagger_report.py"),
            Step("universe_coverage_audit", "32_audit_biotech_universe_coverage.py"),
        ]
    )
    return steps


def historical_restatement_steps(
    *,
    reuse_unchanged_historical: bool = True,
    market_start_asof: str = "",
) -> list[Step]:
    """Derived layers that can be safely restated from already-synced source tables."""
    governance_args: tuple[str, ...] = ("--reuse-unchanged-historical",) if reuse_unchanged_historical else ()
    market_args: tuple[str, ...] = ("--offline-existing-bars", "--allow-partial")
    if str(market_start_asof or "").strip():
        market_args = (*market_args, "--start-date", str(market_start_asof).strip())
    norgate_args = (
        "--offline-existing-bars",
        "--allow-partial",
        "--source",
        "norgate_us_equities_total_return",
    )
    if str(market_start_asof or "").strip():
        norgate_args = (*norgate_args, "--start-date", str(market_start_asof).strip())
    return [
        Step("ctgov_audit", "05_audit_ctgov_trial_links.py"),
        Step("historical_scoring_universe", "57_build_historical_scoring_universe.py"),
        Step("yahoo_market_adjusted", "17_sync_market_data_yahoo_adjusted.py", market_args),
        Step("norgate_market_features", "17_sync_market_data_yahoo_adjusted.py", norgate_args),
        Step("market_positioning_export", "25_update_market_positioning.py", ("--skip-download",)),
        Step("forward_catalyst_calendar", "09_build_forward_catalyst_calendar.py"),
        Step("financial_survival", "16_build_financial_survival_features.py"),
        Step("commercial_value", "18_build_commercial_value_features.py", ("--allow-missing-market", "--allow-stale-market")),
        Step("forward_guidance", "19_parse_forward_guidance.py", ("--run-mode", "weekly_reconcile")),
        Step("governance_events", "20_build_governance_event_features.py", governance_args),
        Step("fda_adcom_calendar", "14_sync_fda_adcom_calendar.py"),
        Step("biotech_features", "10_build_biotech_features.py"),
        Step("biotech_scores", "11_score_biotech_index.py"),
        Step("multibagger_features", "21_build_multibagger_features.py", ("--allow-missing-market", "--allow-stale-market")),
        Step("multibagger_scores", "22_score_multibagger_candidates.py"),
    ]


def parse_history_dates(raw: str) -> list[str]:
    dates: list[str] = []
    seen: set[str] = set()
    for part in str(raw or "").replace(";", ",").replace("|", ",").split(","):
        parsed = parse_date(part)
        if parsed is None:
            if str(part or "").strip():
                raise ValueError(f"Invalid historical restatement date: {part!r}")
            continue
        text = parsed.isoformat()
        if text not in seen:
            dates.append(text)
            seen.add(text)
    return dates


def load_history_date_grid(
    conn: sqlite3.Connection,
    *,
    source_table: str,
    explicit_dates: str,
    start_asof: str,
    end_asof: str,
    default_end_asof: str,
    fridays_only: bool,
) -> list[str]:
    dates = parse_history_dates(explicit_dates)
    start_date = parse_date(start_asof)
    if start_asof and start_date is None:
        raise ValueError(f"Invalid --history-start-asof date: {start_asof}")
    end_date = parse_date(end_asof) or parse_date(default_end_asof)
    if (end_asof or default_end_asof) and end_date is None:
        raise ValueError(f"Invalid historical restatement end date: {end_asof or default_end_asof}")
    if not dates:
        table_sql = quote_identifier(source_table)
        rows = conn.execute(f"SELECT DISTINCT asof_date FROM {table_sql} WHERE asof_date IS NOT NULL ORDER BY asof_date").fetchall()
        for row in rows:
            parsed = parse_date(row["asof_date"])
            if parsed is None:
                continue
            if start_date is not None and parsed < start_date:
                continue
            if end_date is not None and parsed > end_date:
                continue
            if fridays_only and parsed.weekday() != 4:
                continue
            dates.append(parsed.isoformat())
    else:
        filtered: list[str] = []
        for text in dates:
            parsed = parse_date(text)
            if parsed is None:
                continue
            if start_date is not None and parsed < start_date:
                continue
            if end_date is not None and parsed > end_date:
                continue
            if fridays_only and parsed.weekday() != 4:
                continue
            filtered.append(text)
        dates = filtered
    if not dates:
        raise RuntimeError("Historical restatement date grid is empty.")
    return dates


def write_timing_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["run_started_at", "mode", "step", "status", "elapsed_sec", "returncode", "command", "stdout_tail", "stderr_tail"]
    payload = [{field: row.get(field, "") for field in fieldnames} for row in rows]

    def write_path(target: Path) -> None:
        with target.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
            writer.writeheader()
            writer.writerows(payload)

    try:
        write_path(path)
    except PermissionError as exc:
        fallback = path.with_name(f"{path.stem}_unlocked{path.suffix}")
        LOGGER.warning("Timing CSV is locked; writing fallback timing file instead: %s error=%s", fallback, exc)
        try:
            write_path(fallback)
        except OSError as fallback_exc:
            LOGGER.warning("Unable to write fallback timing CSV %s: %s", fallback, fallback_exc)


def text_tail(raw: str, limit: int = 4000) -> str:
    text = str(raw or "").strip()
    return text[-limit:] if len(text) > limit else text


def terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        else:
            pgid = os.getpgid(process.pid)
            os.killpg(pgid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(pgid, signal.SIGKILL)
    except Exception as exc:
        LOGGER.warning("Failed to terminate process tree for pid=%s: %s", process.pid, exc)
    try:
        process.kill()
    except Exception:
        pass


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_direct_output_files(
    config: dict[str, Any],
    *,
    base_dir: Path,
    asof: str,
    run_started_at: str,
    mode: str,
    selected_steps: set[str],
) -> dict[str, Any]:
    start = time.monotonic()
    source_dir = resolve_path(
        cfg_get(config, "biotech_refresh.snapshot_outputs.source_dir", cfg_get(config, "biotech_reports.output_dir", "../output/biotech_index_reports")),
        base_dir=base_dir,
    )
    snapshot_root = resolve_path(
        cfg_get(config, "biotech_refresh.snapshot_outputs.root_dir", str(source_dir)),
        base_dir=base_dir,
    )
    include_extensions = {
        value.lower() if str(value).startswith(".") else f".{str(value).lower()}"
        for value in parse_string_list(
            cfg_get(config, "biotech_refresh.snapshot_outputs.include_extensions", [".csv", ".json"]),
            [".csv", ".json"],
        )
    }
    snapshot_dir = snapshot_root / asof.replace("-", "")
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    run_start = datetime.fromisoformat(run_started_at.replace("Z", "+00:00"))
    if run_start.tzinfo is None:
        run_start = run_start.replace(tzinfo=timezone.utc)
    copy_only_refreshed = as_bool(
        cfg_get(config, "biotech_refresh.snapshot_outputs.copy_only_refreshed_since_run_start", True),
        True,
    )
    mtime_tolerance_sec = max(
        0.0,
        float(cfg_get(config, "biotech_refresh.snapshot_outputs.mtime_tolerance_sec", 5.0)),
    )
    minimum_source_mtime = run_start.timestamp() - mtime_tolerance_sec
    manifest_path = snapshot_dir / "snapshot_manifest.json"
    previous_snapshot_names: set[str] = set()
    if manifest_path.exists():
        try:
            previous_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            previous_manifest = {}
        if isinstance(previous_manifest, dict):
            for item in previous_manifest.get("files", []):
                if isinstance(item, dict) and str(item.get("name") or "").strip():
                    previous_snapshot_names.add(str(item["name"]))
    candidate_source_files = [
        source
        for source in sorted(list(source_dir.iterdir()), key=lambda item: item.name.lower())
        if source.is_file() and source.suffix.lower() in include_extensions
    ]
    skipped_stale_sources = [
        source.name
        for source in candidate_source_files
        if copy_only_refreshed and source.stat().st_mtime < minimum_source_mtime
    ]
    source_files = [
        source
        for source in candidate_source_files
        if not copy_only_refreshed or source.stat().st_mtime >= minimum_source_mtime
    ]
    source_names = {source.name for source in source_files}

    copied_files: list[dict[str, Any]] = []
    for stale in list(snapshot_dir.iterdir()):
        if stale.is_file() and stale.name == "snapshot_manifest.json":
            stale.unlink()
            continue
        if (
            stale.is_file()
            and stale.suffix.lower() in include_extensions
            and stale.name in previous_snapshot_names
            and stale.name not in source_names
            and datetime.fromtimestamp(stale.stat().st_mtime, timezone.utc) < run_start
        ):
            LOGGER.info("Removing stale dated snapshot file not refreshed in this run: %s", stale)
            stale.unlink()

    for source in source_files:
        target = snapshot_dir / source.name
        action = "copied"
        if target.exists() and target.stat().st_mtime >= source.stat().st_mtime:
            action = "kept_existing_newer_or_equal"
        else:
            shutil.copy2(source, target)
        try:
            stat = target.stat()
            digest = file_sha256(target)
        except FileNotFoundError:
            LOGGER.warning("Snapshot target disappeared before hashing: %s", target)
            continue
        copied_files.append(
            {
                "name": target.name,
                "size_bytes": stat.st_size,
                "sha256": digest,
                "last_write_time_utc": datetime.fromtimestamp(stat.st_mtime, timezone.utc).replace(microsecond=0).isoformat(),
                "snapshot_action": action,
            }
        )

    manifest = {
        "asof_date": asof,
        "created_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "run_started_at_utc": run_started_at,
        "mode": mode,
        "selected_steps": sorted(selected_steps),
        "source_dir": str(source_dir),
        "snapshot_dir": str(snapshot_dir),
        "include_extensions": sorted(include_extensions),
        "copy_only_refreshed_since_run_start": copy_only_refreshed,
        "mtime_tolerance_sec": mtime_tolerance_sec,
        "skipped_stale_source_file_count": len(skipped_stale_sources),
        "skipped_stale_source_files": skipped_stale_sources,
        "file_count": len(copied_files),
        "files": copied_files,
        "notes": [
            "Snapshot folder name is derived from the data as-of date, not the wall-clock run date.",
            "Only direct files in source_dir are copied; subdirectories and calibration folders are not copied.",
            "By default, direct files older than this pipeline run are excluded so stale audit artifacts cannot be relabeled as current.",
            "Only files listed in the previous snapshot manifest are eligible for stale cleanup.",
        ],
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True), encoding="utf-8")
    stat = manifest_path.stat()
    copied_files.append(
        {
            "name": manifest_path.name,
            "size_bytes": stat.st_size,
            "sha256": file_sha256(manifest_path),
            "last_write_time_utc": datetime.fromtimestamp(stat.st_mtime, timezone.utc).replace(microsecond=0).isoformat(),
        }
    )
    max_history = int(cfg_get(config, "biotech_refresh.max_snapshot_history", 0) or 0)
    prune_enabled = as_bool(
        cfg_get(config, "biotech_refresh.snapshot_outputs.prune_old_snapshot_dirs", False),
        False,
    )
    shared_report_root = source_dir.resolve() == snapshot_root.resolve()
    if max_history > 0 and prune_enabled and shared_report_root:
        LOGGER.warning(
            "Skipping snapshot pruning because source_dir and snapshot_root are the same shared Stage 11 report root: %s",
            snapshot_root,
        )
    if max_history > 0 and prune_enabled and not shared_report_root:
        dated_dirs = sorted(
            [
                path
                for path in snapshot_root.iterdir()
                if path.is_dir()
                and path.name.isdigit()
                and len(path.name) == 8
                and (path / "snapshot_manifest.json").exists()
            ],
            key=lambda path: path.name,
            reverse=True,
        )
        prune_failures: list[tuple[Path, str]] = []
        pruned_count = 0
        for old_dir in dated_dirs[max_history:]:
            try:
                shutil.rmtree(old_dir)
                pruned_count += 1
            except OSError as exc:
                prune_failures.append((old_dir, str(exc)))
        if pruned_count:
            LOGGER.info("Pruned old biotech snapshot directories: count=%d", pruned_count)
        if prune_failures:
            sample = "; ".join(f"{path} ({error})" for path, error in prune_failures[:5])
            suffix = f"; ...(+{len(prune_failures) - 5})" if len(prune_failures) > 5 else ""
            LOGGER.warning(
                "Could not prune %d old biotech snapshot director%s; continuing because current outputs are already written. sample=%s%s",
                len(prune_failures),
                "y" if len(prune_failures) == 1 else "ies",
                sample,
                suffix,
            )
    elapsed = round(time.monotonic() - start, 3)
    LOGGER.info(
        "Snapshot outputs written: asof=%s files=%d snapshot_dir=%s elapsed=%.3fs",
        asof,
        len(copied_files),
        snapshot_dir,
        elapsed,
    )
    return {
        "run_started_at": run_started_at,
        "mode": mode,
        "step": "snapshot_outputs",
        "status": "success",
        "elapsed_sec": elapsed,
        "returncode": 0,
        "command": f"snapshot outputs asof={asof} dir={snapshot_dir}",
    }


def historical_universe_requires_norgate(
    config: dict[str, Any],
    *,
    base_dir: Path,
    asof: str,
) -> bool:
    """Return whether the dated PIT universe contains calibration-only members."""
    output_root = resolve_path(
        cfg_get(config, "biotech_scoring.output_dir", "../output/biotech_index_reports"),
        base_dir=base_dir,
    )
    universe_path = output_root / asof.replace("-", "") / "ctgov_final_scoring_universe.csv"
    if not universe_path.exists():
        raise FileNotFoundError(
            f"Historical scoring universe must exist before Norgate routing: {universe_path}"
        )
    with universe_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "calibration_only" not in reader.fieldnames:
            raise ValueError(
                "Historical scoring universe is missing required calibration_only column: "
                f"{universe_path}"
            )
        return any(as_bool(row.get("calibration_only"), False) for row in reader)


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
    LOGGER.info("Command for %s: %s", step.name, " ".join(command))
    stdout_text = ""
    stderr_text = ""
    try:
        process = subprocess.Popen(
            command,
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=sys.platform != "win32",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
        )
        try:
            stdout_text, stderr_text = process.communicate(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            terminate_process_tree(process)
            try:
                stdout_text, stderr_text = process.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                terminate_process_tree(process)
                stdout_text = stdout_text or ""
                stderr_text = (stderr_text or "") + "\nTimed out waiting for process tree termination."
            raise
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
            "stdout_tail": text_tail(stdout_text),
            "stderr_tail": text_tail(stderr_text),
        }
    except Exception as exc:
        # Spawn/communicate failures (e.g. FileNotFoundError, OSError) must be
        # recorded as a failed timing row instead of leaving a row stuck at
        # status="running"; the caller treats the failed row like any other step failure.
        elapsed = round(time.monotonic() - start, 3)
        LOGGER.exception("Step %s failed to launch or complete: %s", step.name, exc)
        return {
            "run_started_at": run_started_at,
            "mode": mode,
            "step": step.name,
            "status": "failed",
            "elapsed_sec": elapsed,
            "returncode": -1,
            "command": " ".join(command),
            "stdout_tail": text_tail(stdout_text),
            "stderr_tail": text_tail((stderr_text or "") + f"\n{type(exc).__name__}: {exc}"),
        }
    elapsed = round(time.monotonic() - start, 3)
    returncode = int(process.returncode if process.returncode is not None else -1)
    status = "success" if returncode == 0 else "failed"
    if stderr_text.strip():
        log_func = LOGGER.warning if status == "success" else LOGGER.error
        log_func("Step %s stderr tail: %s", step.name, text_tail(stderr_text, 1200))
    LOGGER.info("Finished %s status=%s elapsed=%.3fs", step.name, status, elapsed)
    return {
        "run_started_at": run_started_at,
        "mode": mode,
        "step": step.name,
        "status": status,
        "elapsed_sec": elapsed,
        "returncode": returncode,
        "command": " ".join(command),
        "stdout_tail": text_tail(stdout_text),
        "stderr_tail": text_tail(stderr_text),
    }


def maybe_load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        return {}
    try:
        return load_yaml(path)
    except Exception as exc:
        LOGGER.warning("Could not parse optional YAML for path validation: path=%s error=%s", path, exc)
        return {}


def nested_cfg(raw: dict[str, Any], dotted_key: str, default: Any = None) -> Any:
    cur: Any = raw
    for part in dotted_key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def validate_form4_refresh_config_paths(
    config: dict[str, Any],
    *,
    base_dir: Path,
    script_path: Path,
    refresh_config_path: Path | None,
    expected_db_path: Path,
) -> None:
    """Reject updater configs that would write to PROD or any DB besides staging."""
    validate_form4_staging_boundary(
        config,
        base_dir=base_dir,
        db_path=expected_db_path,
        extra_paths=[("refresh_script_path", script_path), ("refresh_config_path", refresh_config_path)],
    )
    if refresh_config_path is None or not refresh_config_path.exists():
        return

    refresh_cfg = maybe_load_yaml(refresh_config_path)
    if not refresh_cfg:
        return

    candidate_db_values: list[tuple[str, Any, Path]] = []
    direct_form4_db = nested_cfg(refresh_cfg, "sec_form4.db_path")
    if direct_form4_db:
        candidate_db_values.append(("sec_form4.db_path", direct_form4_db, refresh_config_path.parent))

    orchestrator_root = refresh_cfg.get("sec_form4_orchestrator", refresh_cfg)
    if isinstance(orchestrator_root, dict):
        form4_config_raw = nested_cfg(orchestrator_root, "form4.config_path")
        if form4_config_raw:
            repo_root = script_path.resolve().parent.parent
            form4_config_path = Path(str(form4_config_raw)).expanduser()
            if not form4_config_path.is_absolute():
                form4_config_path = (repo_root / form4_config_path).resolve()
            validate_form4_staging_boundary(
                config,
                base_dir=base_dir,
                db_path=expected_db_path,
                extra_paths=[("nested_form4_config_path", form4_config_path)],
            )
            nested_form4_cfg = maybe_load_yaml(form4_config_path)
            nested_db = nested_cfg(nested_form4_cfg, "sec_form4.db_path")
            if nested_db:
                candidate_db_values.append(("nested sec_form4.db_path", nested_db, form4_config_path.parent))

    for label, raw_db, relative_base in candidate_db_values:
        resolved = Path(expand_env_vars(raw_db)).expanduser()
        if not resolved.is_absolute():
            resolved = (relative_base / resolved).resolve()
        validate_form4_staging_boundary(config, base_dir=base_dir, db_path=resolved)
        if not same_path(resolved, expected_db_path):
            raise RuntimeError(f"Form 4 refresh config {label} must write to staging DB {expected_db_path}, got {resolved}")


def build_form4_refresh_command(
    config: dict[str, Any],
    *,
    base_dir: Path,
    asof: str,
) -> tuple[list[str], Path, Path | None]:
    refresh_cfg = cfg_get(config, "biotech_refresh.form4_refresh", {}) or {}
    if not isinstance(refresh_cfg, dict):
        raise ValueError("biotech_refresh.form4_refresh must be a mapping when enabled")
    python_executable = str(expand_env_vars(refresh_cfg.get("python_executable") or sys.executable)).strip() or sys.executable
    script_raw = refresh_cfg.get("script_path")
    if not script_raw:
        raise ValueError("biotech_refresh.form4_refresh.script_path is required when Form 4 refresh is enabled")
    script_path = resolve_path(script_raw, base_dir=base_dir)
    config_raw = refresh_cfg.get("config_path")
    refresh_config_path = resolve_path(config_raw, base_dir=base_dir) if config_raw else None
    expected_db_path = validate_form4_staging_boundary(config, base_dir=base_dir)
    validate_form4_refresh_config_paths(
        config,
        base_dir=base_dir,
        script_path=script_path,
        refresh_config_path=refresh_config_path,
        expected_db_path=expected_db_path,
    )
    if not script_path.is_file():
        raise FileNotFoundError(
            "Configured staging Form 4 refresh script is missing. "
            f"Expected a staging-local script, not PROD: {script_path}"
        )
    if refresh_config_path is not None and not refresh_config_path.is_file():
        raise FileNotFoundError(f"Configured staging Form 4 refresh config is missing: {refresh_config_path}")

    command = [python_executable, str(script_path)]
    if refresh_config_path is not None:
        command.extend(["--config", str(refresh_config_path)])
    target = str(refresh_cfg.get("target") or "form4").strip()
    profile = str(refresh_cfg.get("profile") or "daily").strip()
    asof_arg = str(refresh_cfg.get("asof_arg") or "--as-of-date").strip()
    if target:
        command.extend(["--target", target])
    if profile:
        command.extend(["--profile", profile])
    if asof_arg:
        command.extend([asof_arg, asof])
    extra_args = parse_string_list(refresh_cfg.get("extra_args"), [])
    command.extend(extra_args)
    return command, script_path, refresh_config_path


def run_form4_refresh(
    config: dict[str, Any],
    *,
    base_dir: Path,
    asof: str,
    run_started_at: str,
    mode: str,
) -> dict[str, Any]:
    start = time.monotonic()
    command, script_path, refresh_config_path = build_form4_refresh_command(config, base_dir=base_dir, asof=asof)
    refresh_cfg = cfg_get(config, "biotech_refresh.form4_refresh", {}) or {}
    timeout_raw = refresh_cfg.get("timeout_sec", refresh_cfg.get("max_runtime_sec", cfg_get(config, "biotech_refresh.step_timeout_sec", 14400.0)))
    try:
        timeout_sec = float(timeout_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid biotech_refresh.form4_refresh.timeout_sec value: {timeout_raw!r}") from exc
    timeout_value = timeout_sec if timeout_sec > 0 else None
    step = Step("form4_refresh", str(script_path), tuple(command[2:]), supports_asof=False)
    LOGGER.info(
        "Starting form4_refresh staging_db=%s script=%s config=%s",
        resolve_path(cfg_get(config, "governance_events.form4_db_path"), base_dir=base_dir),
        script_path,
        refresh_config_path or "",
    )
    row = run_step(step, command=command, mode=mode, run_started_at=run_started_at, timeout_sec=timeout_value)
    row["step"] = "form4_refresh"
    row["elapsed_sec"] = round(time.monotonic() - start, 3)
    return row


def connect_form4_readonly(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def read_form4_snapshot_date(conn: sqlite3.Connection, snapshot_table: str) -> tuple[str, str]:
    sources: list[tuple[str, str]] = []
    if snapshot_table:
        sources.append((snapshot_table, "last_index_date"))
        sources.append((snapshot_table, "as_of_date"))
    sources.extend(
        [
            ("stock_signal_snapshot_tier1", "as_of_date"),
            ("sec_form4_daily_state", "last_index_date"),
            ("form4_events_tier1", "filing_date"),
            ("form4_buy_events_v1", "filing_date"),
        ]
    )
    best_raw = ""
    best_source = ""
    best_date: date | None = None
    for table, field in sources:
        try:
            table_sql = quote_identifier(table)
            field_sql = quote_identifier(field)
            row = conn.execute(f"SELECT MAX({field_sql}) AS snapshot_date FROM {table_sql}").fetchone()
        except (sqlite3.Error, ValueError) as exc:
            LOGGER.debug("Form 4 snapshot probe skipped source=%s.%s error=%s", table, field, exc)
            continue
        if row and row["snapshot_date"]:
            raw = str(row["snapshot_date"])
            parsed = parse_db_date(raw)
            if parsed is not None and (best_date is None or parsed > best_date):
                best_raw = raw
                best_source = f"{table}.{field}"
                best_date = parsed
    return best_raw, best_source


def form4_raw_filing_tables_present(
    conn: sqlite3.Connection,
    sources: list[tuple[str, str]],
) -> bool:
    for table, _field in sources:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if row is not None:
            return True
    return False


def read_form4_raw_filing_date(
    conn: sqlite3.Connection,
    sources: list[tuple[str, str]],
) -> tuple[str, str]:
    best_raw = ""
    best_source = ""
    best_date: date | None = None
    for table, field in sources:
        try:
            table_sql = quote_identifier(table)
            field_sql = quote_identifier(field)
            rows = conn.execute(
                f"SELECT DISTINCT {field_sql} AS filing_date FROM {table_sql} "
                f"WHERE {field_sql} IS NOT NULL AND {field_sql} <> ''"
            ).fetchall()
        except (sqlite3.Error, ValueError) as exc:
            LOGGER.debug("Form 4 raw filing probe skipped source=%s.%s error=%s", table, field, exc)
            continue
        for row in rows:
            raw = str(row["filing_date"] or "").strip()
            parsed = parse_db_date(raw)
            if parsed is not None and (best_date is None or parsed > best_date):
                best_raw = raw
                best_source = f"{table}.{field}"
                best_date = parsed
    return best_raw, best_source


def validate_form4_preflight(
    config: dict[str, Any],
    *,
    base_dir: Path,
    asof: str,
    run_started_at: str,
    mode: str,
) -> dict[str, Any]:
    start = time.monotonic()
    LOGGER.info("Starting form4_preflight")
    target_date = parse_date(asof)
    if target_date is None:
        raise ValueError(f"Invalid pipeline as-of date for Form 4 preflight: {asof}")

    form4_db_path = resolve_path(cfg_get(config, "governance_events.form4_db_path"), base_dir=base_dir)
    validate_form4_staging_boundary(config, base_dir=base_dir, db_path=form4_db_path)
    snapshot_table = str(cfg_get(config, "governance_events.form4_snapshot_table", "sec_form4_daily_state") or "")
    max_staleness_days = int(
        cfg_get(
            config,
            "biotech_refresh.form4_preflight.max_staleness_days",
            cfg_get(config, "biotech_refresh.max_upstream_staleness_days", 2),
        )
    )
    staleness_day_basis = str(cfg_get(config, "biotech_refresh.form4_preflight.staleness_day_basis", "calendar")).strip().lower()
    if staleness_day_basis not in {"calendar", "business"}:
        raise ValueError(
            "biotech_refresh.form4_preflight.staleness_day_basis must be 'calendar' or 'business', "
            f"got {staleness_day_basis!r}"
        )
    required = as_bool(
        cfg_get(
            config,
            "biotech_refresh.form4_preflight.required",
            cfg_get(config, "governance_events.form4_required", True),
        ),
        True,
    )
    warn_only = as_bool(cfg_get(config, "biotech_refresh.form4_preflight.warn_only", False), False)
    raw_filing_check_enabled = as_bool(
        cfg_get(config, "biotech_refresh.form4_preflight.raw_filing_freshness_enabled", True),
        True,
    )
    raw_filing_required = as_bool(
        cfg_get(config, "biotech_refresh.form4_preflight.raw_filing_required", required),
        required,
    )
    max_raw_filing_lag_days = int(
        cfg_get(config, "biotech_refresh.form4_preflight.max_raw_filing_lag_days", 5)
    )
    raw_filing_lag_day_basis = str(
        cfg_get(config, "biotech_refresh.form4_preflight.raw_filing_lag_day_basis", staleness_day_basis)
    ).strip().lower()
    if raw_filing_lag_day_basis not in {"calendar", "business"}:
        raise ValueError(
            "biotech_refresh.form4_preflight.raw_filing_lag_day_basis must be 'calendar' or 'business', "
            f"got {raw_filing_lag_day_basis!r}"
        )
    raw_filing_sources = parse_table_column_sources(
        cfg_get(config, "biotech_refresh.form4_preflight.raw_filing_date_sources", None),
        DEFAULT_FORM4_RAW_FILING_DATE_SOURCES,
    )

    failures: list[str] = []
    warnings: list[str] = []
    snapshot_raw = ""
    snapshot_source = ""
    age_days: int | str = ""
    raw_filing_raw = ""
    raw_filing_source = ""
    raw_filing_age_days: int | str = ""
    raw_filing_tables_exist = False

    if max_staleness_days < 0:
        raise ValueError("biotech_refresh.form4_preflight.max_staleness_days must be >= 0")
    if max_raw_filing_lag_days < 0:
        raise ValueError("biotech_refresh.form4_preflight.max_raw_filing_lag_days must be >= 0")
    if not form4_db_path.exists():
        failures.append(f"Form 4 database not found: {form4_db_path}")
    else:
        try:
            with closing(connect_form4_readonly(form4_db_path)) as conn:
                snapshot_raw, snapshot_source = read_form4_snapshot_date(conn, snapshot_table)
                if raw_filing_check_enabled:
                    raw_filing_raw, raw_filing_source = read_form4_raw_filing_date(conn, raw_filing_sources)
                    raw_filing_tables_exist = form4_raw_filing_tables_present(conn, raw_filing_sources)
        except sqlite3.Error as exc:
            failures.append(f"Form 4 database cannot be opened read-only: {form4_db_path} ({type(exc).__name__}: {exc})")

    if not failures:
        snapshot_date = parse_db_date(snapshot_raw)
        if snapshot_date is None:
            failures.append(f"Form 4 snapshot date is unavailable in {form4_db_path}")
        else:
            age_days = (
                business_day_age(snapshot_date, target_date)
                if staleness_day_basis == "business"
                else (target_date - snapshot_date).days
            )
            if isinstance(age_days, int) and age_days < 0:
                failures.append(
                    f"Form 4 snapshot is future-dated: snapshot_date={snapshot_date.isoformat()} "
                    f"asof={target_date.isoformat()} age_days={age_days} basis={staleness_day_basis}"
                )
            elif isinstance(age_days, int) and age_days > max_staleness_days:
                failures.append(
                    f"Form 4 snapshot is stale: snapshot_date={snapshot_date.isoformat()} "
                    f"asof={target_date.isoformat()} age_days={age_days} basis={staleness_day_basis} "
                    f"max_staleness_days={max_staleness_days}"
                )
        if raw_filing_check_enabled:
            raw_filing_date = parse_db_date(raw_filing_raw)
            if raw_filing_date is None:
                if not raw_filing_tables_exist:
                    # A minimal staging copy may carry only the snapshot state table;
                    # the raw-filing freshness check is inapplicable, not a failure.
                    LOGGER.info(
                        "Form 4 raw filing tables absent in %s; skipping raw filing freshness check.",
                        form4_db_path,
                    )
                else:
                    message = (
                        f"Form 4 raw filing date is unavailable in {form4_db_path}; "
                        f"checked sources={raw_filing_sources}"
                    )
                    if raw_filing_required:
                        failures.append(message)
                    else:
                        warnings.append(message)
            else:
                raw_filing_age_days = (
                    business_day_age(raw_filing_date, target_date)
                    if raw_filing_lag_day_basis == "business"
                    else (target_date - raw_filing_date).days
                )
                if isinstance(raw_filing_age_days, int) and raw_filing_age_days < 0:
                    failures.append(
                        f"Form 4 raw filing date is future-dated: raw_filing_date={raw_filing_date.isoformat()} "
                        f"asof={target_date.isoformat()} age_days={raw_filing_age_days} basis={raw_filing_lag_day_basis}"
                    )
                elif isinstance(raw_filing_age_days, int) and raw_filing_age_days > max_raw_filing_lag_days:
                    failures.append(
                        f"Form 4 raw filings are stale: raw_filing_date={raw_filing_date.isoformat()} "
                        f"asof={target_date.isoformat()} age_days={raw_filing_age_days} "
                        f"basis={raw_filing_lag_day_basis} max_raw_filing_lag_days={max_raw_filing_lag_days}"
                    )

    if failures and (required and not warn_only):
        raise RuntimeError("Form 4 preflight failed: " + " | ".join(failures))
    if failures:
        warnings.extend(failures)

    elapsed = round(time.monotonic() - start, 3)
    if warnings:
        LOGGER.warning("Finished form4_preflight status=warning elapsed=%.3fs warnings=%s", elapsed, " | ".join(warnings))
        status = "warning"
    else:
        LOGGER.info(
            "Finished form4_preflight status=success elapsed=%.3fs db=%s snapshot_date=%s source=%s "
            "age_days=%s raw_filing_date=%s raw_source=%s raw_age_days=%s",
            elapsed,
            form4_db_path,
            snapshot_raw,
            snapshot_source,
            age_days,
            raw_filing_raw,
            raw_filing_source,
            raw_filing_age_days,
        )
        status = "success"
    return {
        "run_started_at": run_started_at,
        "mode": mode,
        "step": "form4_preflight",
        "status": status,
        "elapsed_sec": elapsed,
        "returncode": 0,
        "command": (
            f"validate Form 4 db={form4_db_path} asof={asof} snapshot_date={snapshot_raw or '<missing>'} "
            f"source={snapshot_source or '<missing>'} age_days={age_days} max_staleness_days={max_staleness_days} "
            f"raw_filing_date={raw_filing_raw or '<missing>'} raw_source={raw_filing_source or '<missing>'} "
            f"raw_age_days={raw_filing_age_days} max_raw_filing_lag_days={max_raw_filing_lag_days}"
        ),
    }


def ibkr_preflight_targets(config: dict[str, Any], steps: list[Step]) -> list[tuple[str, str, int]]:
    """Return unique IBKR TCP endpoints required by the selected pipeline steps."""
    step_names = {step.name for step in steps}
    targets: list[tuple[str, str, int]] = []
    if "ib_market" in step_names:
        targets.append(
            (
                "ib_market_data",
                str(cfg_get(config, "ib_market_data.host", "127.0.0.1")),
                int(float(cfg_get(config, "ib_market_data.port", 7497))),
            )
        )
    if "market_positioning" in step_names and as_bool(
        cfg_get(config, "market_positioning.ibkr_borrow.enabled", False),
        False,
    ):
        targets.append(
            (
                "market_positioning.ibkr_borrow",
                str(cfg_get(config, "market_positioning.ibkr_borrow.host", "127.0.0.1")),
                int(float(cfg_get(config, "market_positioning.ibkr_borrow.port", 7497))),
            )
        )
    seen: set[tuple[str, int]] = set()
    unique: list[tuple[str, str, int]] = []
    for label, host, port in targets:
        key = (host, port)
        if key in seen:
            continue
        seen.add(key)
        unique.append((label, host, port))
    return unique


def validate_ibkr_preflight(
    config: dict[str, Any],
    *,
    steps: list[Step],
    run_started_at: str,
    mode: str,
) -> dict[str, Any]:
    """Fail early when selected steps require IBKR/TWS but the TCP port is unavailable."""
    start = time.monotonic()
    targets = ibkr_preflight_targets(config, steps)
    timeout_sec = float(cfg_get(config, "biotech_refresh.ibkr_preflight.timeout_sec", 5.0))
    required = as_bool(cfg_get(config, "biotech_refresh.ibkr_preflight.required", True), True)
    failures: list[str] = []
    successes: list[str] = []
    for label, host, port in targets:
        try:
            with socket.create_connection((host, port), timeout=timeout_sec):
                successes.append(f"{label}={host}:{port}")
        except OSError as exc:
            failures.append(f"{label}={host}:{port} unavailable ({type(exc).__name__}: {exc})")
    elapsed = round(time.monotonic() - start, 3)
    if failures and required:
        raise RuntimeError(
            "IBKR preflight failed; TWS/IBGateway must be running before IB-dependent biotech steps: "
            + " | ".join(failures)
        )
    if failures:
        LOGGER.warning("Finished ibkr_preflight status=warning elapsed=%.3fs warnings=%s", elapsed, " | ".join(failures))
        status = "warning"
        returncode = 0
    else:
        LOGGER.info(
            "Finished ibkr_preflight status=success elapsed=%.3fs targets=%s",
            elapsed,
            ",".join(successes) if successes else "<none>",
        )
        status = "success"
        returncode = 0
    return {
        "run_started_at": run_started_at,
        "mode": mode,
        "step": "ibkr_preflight",
        "status": status,
        "elapsed_sec": elapsed,
        "returncode": returncode,
        "command": (
            "validate IBKR/TWS TCP connectivity targets="
            + (",".join(f"{label}={host}:{port}" for label, host, port in targets) if targets else "<none>")
        ),
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


def validate_paired_score_dates(conn: sqlite3.Connection, *, asof: str) -> None:
    daily_dates = {
        str(row["asof_date"] or "")
        for row in conn.execute(
            "SELECT DISTINCT asof_date FROM daily_scores WHERE asof_date = ?",
            (asof,),
        ).fetchall()
    }
    multibagger_dates = {
        str(row["asof_date"] or "")
        for row in conn.execute(
            "SELECT DISTINCT asof_date FROM multibagger_scores_daily WHERE asof_date = ?",
            (asof,),
        ).fetchall()
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
        raise RuntimeError(f"Paired score date validation failed for asof={asof}: " + " | ".join(failures))


def validate_score_csv(
    path: Path,
    *,
    asof: str,
    expected_tickers: set[str],
    required_columns: list[str],
    present_columns: list[str] | None = None,
) -> None:
    if not path.exists():
        raise FileNotFoundError(f"Required pipeline output CSV not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        fieldnames = [str(field or "") for field in (reader.fieldnames or [])]
    if not rows:
        raise RuntimeError(f"{path} validation failed: score CSV has no data rows")
    observed_tickers = [str(row.get("ticker") or "") for row in rows]
    coverage = universe_coverage(expected_tickers, observed_tickers)
    asof_values = {str(row.get("asof_date") or "") for row in rows}
    failures: list[str] = []
    presence_columns = list(present_columns or [])
    missing_columns = [column for column in [*required_columns, *presence_columns] if column not in fieldnames]
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
    portfolio_contract_fields = {
        "portfolio_candidate_gate",
        "portfolio_candidate_score",
        "portfolio_candidate_status",
        "score_zero_is_missing_flag",
        "calibration_sample_role",
        "oos_score_valid_flag",
    }
    if portfolio_contract_fields.issubset(fieldnames):
        bad_gate_status: list[str] = []
        bad_gate_score: list[str] = []
        bad_missing_gate: list[str] = []
        bad_oos_role: list[str] = []
        for row in rows:
            ticker = str(row.get("ticker") or "")
            gate = to_float(row.get("portfolio_candidate_gate"), 0.0) > 0.0
            score = to_float(row.get("portfolio_candidate_score"), 0.0)
            missing = to_float(row.get("score_zero_is_missing_flag"), 0.0) > 0.0
            role = str(row.get("calibration_sample_role") or "").strip()
            oos = to_float(row.get("oos_score_valid_flag"), 0.0) > 0.0
            if gate and str(row.get("portfolio_candidate_status") or "").strip() != "eligible":
                bad_gate_status.append(ticker)
            if gate and score <= 0.0:
                bad_gate_score.append(ticker)
            if gate and missing:
                bad_missing_gate.append(ticker)
            if (role == "strict_oos") != oos:
                bad_oos_role.append(ticker)
        for label, tickers in (
            ("gate_true_status_not_eligible", bad_gate_status),
            ("gate_true_nonpositive_score", bad_gate_score),
            ("gate_true_missing_score", bad_missing_gate),
            ("strict_oos_role_flag_mismatch", bad_oos_role),
        ):
            if tickers:
                failures.append(f"{label} for {len(tickers)} row(s): {format_ticker_sample(tickers)}")
    if failures:
        raise RuntimeError(f"{path} validation failed: " + " | ".join(failures))


def validate_required_csv_columns(
    path: Path,
    *,
    asof: str,
    required_columns: list[str],
    allow_empty_rows: bool = False,
) -> None:
    validate_required_csv_columns_with_presence(
        path,
        asof=asof,
        required_columns=required_columns,
        allow_empty_rows=allow_empty_rows,
    )


def validate_required_csv_columns_with_presence(
    path: Path,
    *,
    asof: str,
    required_columns: list[str],
    present_columns: list[str] | None = None,
    allow_empty_rows: bool = False,
) -> None:
    if not path.exists():
        raise FileNotFoundError(f"Required pipeline output CSV not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        fieldnames = [str(field or "") for field in (reader.fieldnames or [])]

    failures: list[str] = []
    if not rows and not allow_empty_rows:
        failures.append("CSV has no data rows")
    presence_columns = list(present_columns or [])
    missing_columns = [column for column in [*required_columns, *presence_columns] if column not in fieldnames]
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
    skipped_market_sources: set[str] | None = None,
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
    biotech_scores_csv = output_file_with_dated_fallback(
        biotech_output_dir,
        asof,
        str(cfg_get(config, "biotech_scoring.output_csv", "biotech_daily_scores.csv")),
    )
    biotech_top_candidates_csv = output_file_with_dated_fallback(
        biotech_reports_output_dir,
        asof,
        str(cfg_get(config, "biotech_reports.top_candidates_csv", "biotech_top_candidates.csv")),
    )
    multibagger_scores_csv = multibagger_output_dir / str(cfg_get(config, "multibagger.scores_csv", "biotech_multibagger_scores.csv"))
    multibagger_candidates_csv = multibagger_output_dir / str(cfg_get(config, "multibagger.candidates_csv", "biotech_multibagger_candidates.csv"))
    scoring_sources = [source for source in scoring_market_sources(config) if source]
    # Only require coverage from sources this run could actually have refreshed:
    # with --skip-yahoo the primary source is legitimately absent for the asof and
    # scoring used the configured fallback (e.g. interactive_brokers).
    skipped = skipped_market_sources or set()
    candidate_market_sources = [source for source in scoring_sources if source not in skipped]
    if not candidate_market_sources:
        candidate_market_sources = scoring_sources
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
        market_coverage_failures: list[str] = []
        for source in candidate_market_sources:
            try:
                # Market features can include extra symbols from the vendor cache; downstream layers filter to the final universe.
                validate_table_coverage(
                    conn,
                    table="market_features_daily",
                    asof=asof,
                    expected_tickers=expected_tickers,
                    source=source,
                    allow_extra=True,
                )
            except RuntimeError as exc:
                market_coverage_failures.append(str(exc))
                continue
            LOGGER.info("market_features_daily coverage satisfied by source=%s for asof=%s", source, asof)
            break
        else:
            if candidate_market_sources:
                raise RuntimeError(
                    "market_features_daily coverage failed for every candidate scoring source "
                    f"({','.join(candidate_market_sources)}): " + " || ".join(market_coverage_failures)
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
        validate_paired_score_dates(conn, asof=asof)
    validate_score_csv(
        biotech_scores_csv,
        asof=asof,
        expected_tickers=expected_tickers,
        required_columns=BIOTECH_SCORE_CSV_REQUIRED_COLUMNS,
        present_columns=BIOTECH_SCORE_CSV_PRESENT_COLUMNS,
    )
    validate_required_csv_columns_with_presence(
        biotech_top_candidates_csv,
        asof=asof,
        required_columns=BIOTECH_SCORE_CSV_REQUIRED_COLUMNS,
        present_columns=BIOTECH_SCORE_CSV_PRESENT_COLUMNS,
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
        allow_empty_rows=True,
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
    validate_config(config)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    if args.asof:
        parsed_asof = parse_date(args.asof)
        if parsed_asof is None:
            raise ValueError(f"Invalid --asof date: {args.asof}")
        asof = parsed_asof.isoformat()
    else:
        asof = default_pipeline_asof(config, db_path=db_path).isoformat()
    LOGGER.info("Pipeline as-of date: %s", asof)
    timing_csv = resolve_path(
        cfg_get(config, "biotech_refresh.timing_csv", "../output/biotech_index_reports/biotech_refresh_timing.csv"),
        base_dir=base_dir,
    )
    raw_timeout_value = cfg_get(config, "biotech_refresh.step_timeout_sec", 14400.0)
    try:
        raw_timeout = float(raw_timeout_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid biotech_refresh.step_timeout_sec value: {raw_timeout_value!r}") from exc
    step_timeout_sec = raw_timeout if raw_timeout > 0 else None
    selected_steps = {step.strip() for step in args.steps.split(",") if step.strip()}
    all_steps = (
        historical_restatement_steps(
            reuse_unchanged_historical=True,
            market_start_asof=args.history_market_start_asof,
        )
        if args.history_restatement
        else pipeline_steps(
            args.mode,
            skip_ctgov=args.skip_ctgov,
            skip_ib=args.skip_ib,
            skip_yahoo=args.skip_yahoo,
            skip_market_positioning=args.skip_market_positioning,
            reuse_unchanged_historical=args.reuse_unchanged_historical,
        )
    )
    if selected_steps:
        known = {step.name for step in all_steps}
        unknown = sorted(selected_steps - known)
        if unknown:
            raise ValueError(f"Unknown pipeline step(s): {', '.join(unknown)}")
    steps = [step for step in all_steps if not selected_steps or step.name in selected_steps]
    steps = maybe_skip_company_master(
        steps,
        config=config,
        base_dir=base_dir,
        db_path=db_path,
        mode=args.mode,
        selected_steps=selected_steps,
    )
    if args.skip_ctgov and not args.history_restatement and any(step.name == "sec_filings" for step in steps):
        ensure_final_scoring_universe_for_skip_ctgov(config, base_dir=base_dir)
    history_dates: list[str] = []
    if args.history_restatement:
        with connect(db_path) as conn:
            history_dates = load_history_date_grid(
                conn,
                source_table=args.history_date_source,
                explicit_dates=args.history_dates,
                start_asof=args.history_start_asof,
                end_asof=args.history_end_asof,
                default_end_asof=asof,
                fridays_only=args.history_fridays_only,
            )
        LOGGER.info(
            "Historical restatement date grid: dates=%d first=%s last=%s source=%s",
            len(history_dates),
            history_dates[0],
            history_dates[-1],
            "explicit" if args.history_dates else args.history_date_source,
        )
    final_validation_enabled = as_bool(cfg_get(config, "biotech_refresh.validate_final_outputs", True))
    snapshot_outputs_enabled = as_bool(cfg_get(config, "biotech_refresh.snapshot_outputs.enabled", True))
    form4_preflight_enabled = as_bool(cfg_get(config, "biotech_refresh.form4_preflight.enabled", True), True)
    form4_refresh_enabled = as_bool(cfg_get(config, "biotech_refresh.form4_refresh.enabled", False), False)
    form4_refresh_required = as_bool(cfg_get(config, "biotech_refresh.form4_refresh.required", True), True)
    form4_warning_is_fatal = as_bool(cfg_get(config, "biotech_refresh.form4_preflight.warning_is_fatal", True), True)
    form4_preflight_needed = any(step.name == "governance_events" for step in steps)
    ibkr_preflight_enabled = as_bool(cfg_get(config, "biotech_refresh.ibkr_preflight.enabled", True), True)
    ibkr_preflight_needed = bool(ibkr_preflight_targets(config, steps))

    run_started_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    timing_rows: list[dict[str, Any]] = []
    try:
        if ibkr_preflight_needed and ibkr_preflight_enabled:
            timing_rows.append(
                {
                    "run_started_at": run_started_at,
                    "mode": args.mode,
                    "step": "ibkr_preflight",
                    "status": "running",
                    "elapsed_sec": "",
                    "returncode": "",
                    "command": "validate IBKR/TWS TCP connectivity",
                }
            )
            write_timing_csv(timing_csv, timing_rows)
            try:
                timing_rows[-1] = validate_ibkr_preflight(
                    config,
                    steps=steps,
                    run_started_at=run_started_at,
                    mode=args.mode,
                )
            except Exception as exc:
                timing_rows[-1] = {
                    "run_started_at": run_started_at,
                    "mode": args.mode,
                    "step": "ibkr_preflight",
                    "status": "failed",
                    "elapsed_sec": "",
                    "returncode": 1,
                    "command": f"validate IBKR/TWS TCP connectivity: {type(exc).__name__}: {exc}",
                }
                write_timing_csv(timing_csv, timing_rows)
                raise
            write_timing_csv(timing_csv, timing_rows)
        elif ibkr_preflight_needed and not ibkr_preflight_enabled:
            LOGGER.warning("IBKR preflight skipped because biotech_refresh.ibkr_preflight.enabled=false.")

        if (
            form4_preflight_needed
            and form4_refresh_enabled
            and not args.skip_form4_refresh
            and not args.history_restatement
        ):
            timing_rows.append(
                {
                    "run_started_at": run_started_at,
                    "mode": args.mode,
                    "step": "form4_refresh",
                    "status": "running",
                    "elapsed_sec": "",
                    "returncode": "",
                    "command": f"refresh staging Form 4 db asof={asof}",
                }
            )
            write_timing_csv(timing_csv, timing_rows)
            try:
                timing_rows[-1] = run_form4_refresh(
                    config,
                    base_dir=base_dir,
                    asof=asof,
                    run_started_at=run_started_at,
                    mode=args.mode,
                )
            except Exception as exc:
                timing_rows[-1] = {
                    "run_started_at": run_started_at,
                    "mode": args.mode,
                    "step": "form4_refresh",
                    "status": "failed",
                    "elapsed_sec": "",
                    "returncode": 1,
                    "command": f"refresh staging Form 4 db asof={asof}: {type(exc).__name__}: {exc}",
                }
                write_timing_csv(timing_csv, timing_rows)
                if form4_refresh_required:
                    raise
                LOGGER.warning("Form 4 refresh failed but required=false: %s", exc)
            write_timing_csv(timing_csv, timing_rows)
            if timing_rows[-1]["status"] != "success" and form4_refresh_required:
                raise SystemExit(int(timing_rows[-1].get("returncode") or 1))
        elif form4_preflight_needed and args.skip_form4_refresh:
            LOGGER.warning("Form 4 refresh skipped via --skip-form4-refresh.")
        elif form4_preflight_needed and args.history_restatement:
            LOGGER.info("Form 4 refresh skipped for historical restatement; source DB freshness is validated separately.")
        elif form4_preflight_needed and not form4_refresh_enabled:
            LOGGER.warning("Form 4 refresh skipped because biotech_refresh.form4_refresh.enabled=false.")

        if form4_preflight_needed and form4_preflight_enabled and not args.skip_form4_preflight and not args.history_restatement:
            preflight_start = time.monotonic()
            timing_rows.append(
                {
                    "run_started_at": run_started_at,
                    "mode": args.mode,
                    "step": "form4_preflight",
                    "status": "running",
                    "elapsed_sec": "",
                    "returncode": "",
                    "command": f"validate Form 4 db freshness asof={asof}",
                }
            )
            write_timing_csv(timing_csv, timing_rows)
            try:
                timing_rows[-1] = validate_form4_preflight(
                    config,
                    base_dir=base_dir,
                    asof=asof,
                    run_started_at=run_started_at,
                    mode=args.mode,
                )
            except Exception as exc:
                timing_rows[-1] = {
                    "run_started_at": run_started_at,
                    "mode": args.mode,
                    "step": "form4_preflight",
                    "status": "failed",
                    "elapsed_sec": round(time.monotonic() - preflight_start, 3),
                    "returncode": 1,
                    "command": f"validate Form 4 db freshness asof={asof}: {type(exc).__name__}: {exc}",
                }
                write_timing_csv(timing_csv, timing_rows)
                raise
            if timing_rows[-1]["status"] == "warning" and form4_warning_is_fatal:
                timing_rows[-1] = {
                    **timing_rows[-1],
                    "status": "failed",
                    "returncode": 1,
                    "command": str(timing_rows[-1].get("command") or "") + " warning_is_fatal=true",
                }
                write_timing_csv(timing_csv, timing_rows)
                raise SystemExit(1)
            write_timing_csv(timing_csv, timing_rows)
        elif form4_preflight_needed and args.history_restatement:
            LOGGER.info(
                "Form 4 preflight skipped for historical restatement; downstream layers filter by filing/transaction date."
            )
        elif form4_preflight_needed and args.skip_form4_preflight:
            LOGGER.warning("Form 4 preflight skipped via --skip-form4-preflight.")
        elif form4_preflight_needed and not form4_preflight_enabled:
            LOGGER.warning("Form 4 preflight skipped because biotech_refresh.form4_preflight.enabled=false.")
        run_dates = history_dates if args.history_restatement else [asof]
        effective_mode = "history_restatement" if args.history_restatement else args.mode
        for run_asof in run_dates:
            for step in steps:
                command_step = step
                timing_step = step
                if args.history_restatement:
                    timing_step = Step(f"{step.name}@{run_asof}", step.script, step.args, step.supports_asof)
                if (
                    args.history_restatement
                    and step.name == "norgate_market_features"
                    and not historical_universe_requires_norgate(
                        config,
                        base_dir=base_dir,
                        asof=run_asof,
                    )
                ):
                    LOGGER.info(
                        "Skipping %s: dated PIT universe contains no calibration-only/delisted members",
                        timing_step.name,
                    )
                    timing_rows.append(
                        {
                            "run_started_at": run_started_at,
                            "mode": effective_mode,
                            "step": timing_step.name,
                            "status": "success",
                            "elapsed_sec": 0.0,
                            "returncode": 0,
                            "command": "skip Norgate market features: no calibration-only/delisted members",
                        }
                    )
                    write_timing_csv(timing_csv, timing_rows)
                    continue
                command = build_step_command(command_step, config_path=config_path, db_path=db_path, asof=run_asof)
                if args.history_restatement and step.name == "ctgov_audit":
                    historical_output_root = resolve_path(
                        cfg_get(config, "biotech_scoring.output_dir", "../output/biotech_index_reports"),
                        base_dir=base_dir,
                    )
                    command.extend(["--output-dir", str(historical_output_root / run_asof.replace("-", ""))])
                row = {
                    "run_started_at": run_started_at,
                    "mode": effective_mode,
                    "step": timing_step.name,
                    "status": "running",
                    "elapsed_sec": "",
                    "returncode": "",
                    "command": " ".join(command),
                }
                timing_rows.append(row)
                write_timing_csv(timing_csv, timing_rows)
                timing_rows[-1] = run_step(
                    timing_step,
                    command=command,
                    mode=effective_mode,
                    run_started_at=run_started_at,
                    timeout_sec=step_timeout_sec,
                )
                write_timing_csv(timing_csv, timing_rows)
                if timing_rows[-1]["status"] != "success":
                    try:
                        step_returncode = int(timing_rows[-1].get("returncode") or 1)
                    except (TypeError, ValueError):
                        step_returncode = 1
                    raise SystemExit(step_returncode if step_returncode > 0 else 1)
        if not args.skip_analyze:
            timing_rows.append(analyze_db(db_path, run_started_at=run_started_at, mode=effective_mode))
            write_timing_csv(timing_csv, timing_rows)
        if (
            not args.history_restatement
            and not selected_steps
            and not args.skip_final_validation
            and final_validation_enabled
        ):
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
                skipped_market_sources: set[str] = set()
                if args.skip_yahoo:
                    skipped_market_sources.add("yahoo_adjusted")
                if args.skip_ib:
                    skipped_market_sources.add("interactive_brokers")
                timing_rows[-1] = validate_final_outputs(
                    config,
                    base_dir=base_dir,
                    db_path=db_path,
                    asof=asof,
                    run_started_at=run_started_at,
                    mode=args.mode,
                    skipped_market_sources=skipped_market_sources,
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
            LOGGER.warning("Final output validation SKIPPED via --skip-final-validation; output may be unvalidated.")
        elif not final_validation_enabled:
            LOGGER.warning("Final output validation skipped because biotech_refresh.validate_final_outputs=false.")
        if snapshot_outputs_enabled and not args.history_restatement and not selected_steps:
            timing_rows.append(
                snapshot_direct_output_files(
                    config,
                    base_dir=base_dir,
                    asof=asof,
                    run_started_at=run_started_at,
                    mode=args.mode,
                    selected_steps=selected_steps,
                )
            )
            write_timing_csv(timing_csv, timing_rows)
        elif snapshot_outputs_enabled and args.history_restatement:
            LOGGER.warning("Snapshot output copy skipped during historical restatement.")
        elif snapshot_outputs_enabled and selected_steps:
            LOGGER.warning("Snapshot output copy skipped because --steps was used.")
    finally:
        write_timing_csv(timing_csv, timing_rows)


if __name__ == "__main__":
    main()
