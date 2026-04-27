#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter
import csv
import json
import logging
import sqlite3
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, normalize_string_list, resolve_optional_path, resolve_path
from biotech_index.core.db import utc_now
from biotech_index.core.text_norm import normalize_ticker


LOGGER = logging.getLogger("audit_inactive_removed_universe")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"

AUDIT_FIELDS = [
    "ticker",
    "company_name",
    "cik",
    "old_decision",
    "screen_decision",
    "db_universe_status",
    "db_is_active",
    "listing_status",
    "manual_include",
    "manual_exclude",
    "manual_review",
    "reason_codes",
    "hard_blocker",
    "hard_blocker_reasons",
    "soft_rescreen_reasons",
    "google_reverse_split_status",
    "google_reverse_split_confirmed",
    "google_going_concern_status",
    "google_going_concern_confirmed",
    "ctgov_has_interventional_trial_match",
    "ctgov_interventional_match_count",
    "ctgov_lead_sponsor_match_count",
    "ctgov_active_interventional_match_count",
    "ctgov_matched_ncts",
    "median_addv20",
    "liquidity_status",
    "liquid_for_screen",
    "hard_blocker_policy_review_signal",
    "conditional_exclusion",
    "conditional_exclusion_notes",
    "policy_inclusion",
    "policy_inclusion_notes",
    "ctgov_reactivation_status",
    "ctgov_reactivation_priority",
    "ctgov_reactivation_reason",
    "ctgov_recommended_status",
    "ctgov_review_bucket",
    "rescreen_decision",
    "rescreen_reason_codes",
    "decision_change",
    "include_in_rescreen_tickers",
    "recommended_action",
    "action_priority",
    "candidate_override_decision",
    "candidate_override_reason_codes",
    "candidate_override_notes",
]

OVERRIDE_FIELDS = [
    "ticker",
    "decision",
    "listing_status",
    "manual_include",
    "manual_exclude",
    "manual_review",
    "reason_codes",
    "notes",
]

DEFAULT_HARD_BLOCK_REASON_CODES = [
    "manual_exclude",
    "confirmed_going_concern",
    "reverse_split",
    "acquired_delisted_non_public",
]

DEFAULT_SOFT_RESCREEN_REASON_CODES = [
    "ctgov_fetch_error",
    "sec_fetch_error",
    "no_interventional_ctgov_match",
    "ctgov_match_missing_needs_alias_review",
    "no_recent_10k_10q_8k_2y",
    "biotech_name_no_trials_no_rd_no_pipeline",
    "extremely_illiquid",
    "possible_going_concern",
    "possible_reverse_split",
]

DEFAULT_POLICY_REVIEW_HARD_REASON_CODES = [
    "reverse_split",
    "google_confirmed_reverse_split",
]

DEFAULT_ABSOLUTE_HARD_REASON_CODES = [
    "manual_exclude",
    "confirmed_going_concern",
    "google_confirmed_going_concern",
    "acquired_delisted_non_public",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Audit inactive/removed companies before scoring and produce controlled re-screen "
            "and remove-to-review candidate files."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--screen-results-csv", type=Path, default=None)
    parser.add_argument("--rescreen-results-csv", type=Path, default=None)
    parser.add_argument("--reactivation-scan-csv", type=Path, default=None)
    parser.add_argument("--tickers", type=str, default="", help="Optional comma-separated ticker subset.")
    parser.add_argument("--asof", type=str, default="", help="Audit date in YYYY-MM-DD. Defaults to UTC today.")
    return parser.parse_args()


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    for handler in logging.getLogger().handlers:
        if handler.formatter is not None:
            handler.formatter.converter = time.gmtime


def read_csv_flexible(path: Path) -> list[dict[str, str]]:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                reader = csv.DictReader(handle)
                if reader.fieldnames is None:
                    raise ValueError(f"CSV has no header: {path}")
                return [{str(k): str(v or "") for k, v in row.items()} for row in reader]
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
    raise ValueError(f"Could not decode CSV {path}: {last_error}")


def row_get(row: dict[str, Any], *keys: str) -> str:
    lowered = {str(k).strip().lower(): v for k, v in row.items()}
    for key in keys:
        raw = row.get(key)
        if raw is not None and str(raw).strip():
            return str(raw).strip()
        raw = lowered.get(key.lower())
        if raw is not None and str(raw).strip():
            return str(raw).strip()
    return ""


def parse_boolish(raw: Any) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "t", "yes", "y"}


def parse_float(raw: Any) -> float:
    try:
        return float(str(raw or "").strip())
    except (TypeError, ValueError):
        return 0.0


def parse_int(raw: Any) -> int:
    try:
        return int(float(str(raw or "").strip()))
    except (TypeError, ValueError):
        return 0


def split_reason_codes(raw: Any) -> set[str]:
    return {part.strip().lower() for part in str(raw or "").split(";") if part.strip()}


def join_codes(values: Iterable[str]) -> str:
    return ";".join(sorted({str(value).strip() for value in values if str(value).strip()}))


def normalize_keyed_rows(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for row in rows:
        ticker = normalize_ticker(row_get(row, "ticker", "Ticker", "Tickers", "symbol", "Symbol"))
        if not ticker:
            continue
        out[ticker] = {str(k): str(v or "") for k, v in row.items()}
    return out


def read_keyed_csv(path: Path | None) -> dict[str, dict[str, str]]:
    if path is None or not path.exists():
        return {}
    return normalize_keyed_rows(read_csv_flexible(path))


def load_conditional_exclusions(path: Path | None) -> dict[str, dict[str, str]]:
    rows = read_keyed_csv(path)
    out: dict[str, dict[str, str]] = {}
    for ticker, row in rows.items():
        enabled_raw = row_get(row, "enabled")
        if enabled_raw and not parse_boolish(enabled_raw):
            continue
        out[ticker] = row
    return out


def load_policy_inclusions(path: Path | None) -> dict[str, dict[str, str]]:
    rows = read_keyed_csv(path)
    out: dict[str, dict[str, str]] = {}
    for ticker, row in rows.items():
        enabled_raw = row_get(row, "enabled")
        if enabled_raw and not parse_boolish(enabled_raw):
            continue
        out[ticker] = row
    return out


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def latest_matching_file(output_dir: Path, filename: str) -> Path | None:
    if not output_dir.exists():
        return None
    matches = [path for path in output_dir.rglob(filename) if path.is_file()]
    if not matches:
        return None
    return max(matches, key=lambda path: path.stat().st_mtime)


def resolve_existing_csv(
    *,
    explicit: Path | None,
    configured_raw: Any,
    base_dir: Path,
    output_dir: Path,
    default_filename: str,
) -> Path | None:
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(explicit.expanduser().resolve())
    configured = resolve_optional_path(configured_raw, base_dir=base_dir)
    if configured is not None:
        candidates.append(configured)
    candidates.append(output_dir / default_filename)
    for path in candidates:
        if path.exists():
            return path
    return latest_matching_file(output_dir, default_filename)


def load_db_companies(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            ticker, cik, company_name, listing_status, manual_include, manual_exclude,
            manual_review, universe_status, is_active, source_screen_decision, reason_codes
        FROM companies
        """
    ).fetchall()
    return {str(row["ticker"]).strip().upper(): dict(row) for row in rows}


def connect_readonly(db_path: Path, *, timeout_sec: float) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(f"SQLite database not found: {db_path}")
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=timeout_sec)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def screen_value(screen_row: dict[str, str], db_row: dict[str, Any], key: str, *aliases: str) -> str:
    value = row_get(screen_row, key, *aliases)
    if value:
        return value
    raw = db_row.get(key)
    return str(raw or "").strip()


def classify_row(
    *,
    asof_date: date,
    ticker: str,
    screen_row: dict[str, str],
    db_row: dict[str, Any],
    rescreen_row: dict[str, str],
    reactivation_row: dict[str, str],
    conditional_exclusion_row: dict[str, str],
    policy_inclusion_row: dict[str, str],
    active_listing_statuses: set[str],
    hard_block_reason_codes: set[str],
    policy_review_hard_reason_codes: set[str],
    absolute_hard_reason_codes: set[str],
    soft_rescreen_reason_codes: set[str],
    min_liquidity_addv20: float,
) -> dict[str, Any]:
    screen_decision = row_get(screen_row, "decision").lower()
    db_universe_status = str(db_row.get("universe_status") or "").strip().lower()
    db_is_active = str(db_row.get("is_active") if db_row.get("is_active") is not None else "").strip()
    if db_universe_status == "remove" or db_is_active == "0":
        old_decision = db_universe_status or str(db_row.get("source_screen_decision") or screen_decision or "").strip().lower()
    else:
        old_decision = screen_decision or str(db_row.get("source_screen_decision") or db_universe_status or "").strip().lower()
    reason_codes = split_reason_codes(row_get(screen_row, "reason_codes") or db_row.get("reason_codes"))
    listing_status = screen_value(screen_row, db_row, "listing_status", "ListingStatus", "status")
    manual_include = screen_value(screen_row, db_row, "manual_include", "ManualInclude")
    manual_exclude = screen_value(screen_row, db_row, "manual_exclude", "ManualExclude")
    manual_review = screen_value(screen_row, db_row, "manual_review", "ManualReview")
    google_reverse_status = row_get(screen_row, "google_reverse_split_status")
    google_reverse_confirmed = row_get(screen_row, "google_reverse_split_confirmed")
    google_gc_status = row_get(screen_row, "google_going_concern_status")
    google_gc_confirmed = row_get(screen_row, "google_going_concern_confirmed")
    has_interventional = parse_boolish(row_get(screen_row, "has_interventional_trial_match"))
    interventional_count = parse_int(row_get(screen_row, "ctgov_interventional_match_count"))
    lead_sponsor_count = parse_int(row_get(screen_row, "ctgov_lead_sponsor_match_count"))
    active_interventional_count = parse_int(row_get(screen_row, "ctgov_active_interventional_match_count"))
    matched_ncts = row_get(screen_row, "ctgov_matched_ncts")
    median_addv20 = parse_float(row_get(screen_row, "median_addv20"))
    liquid_for_screen = median_addv20 >= float(min_liquidity_addv20)

    hard_reasons: set[str] = set()
    if parse_boolish(manual_exclude):
        hard_reasons.add("manual_exclude")
    normalized_listing = listing_status.strip().lower()
    if normalized_listing and active_listing_statuses and normalized_listing not in active_listing_statuses:
        hard_reasons.add(f"non_active_listing:{normalized_listing}")
    hard_reasons |= reason_codes.intersection(hard_block_reason_codes)
    if parse_boolish(google_reverse_confirmed) or google_reverse_status.strip().lower() == "confirmed":
        hard_reasons.add("google_confirmed_reverse_split")
    if parse_boolish(google_gc_confirmed) or google_gc_status.strip().lower() == "confirmed":
        hard_reasons.add("google_confirmed_going_concern")

    soft_reasons = reason_codes.intersection(soft_rescreen_reason_codes)
    reactivation_status = row_get(reactivation_row, "reactivation_status")
    reactivation_priority = row_get(reactivation_row, "reactivation_priority")
    reactivation_reason = row_get(reactivation_row, "reactivation_reason")
    ctgov_recommended_status = row_get(reactivation_row, "recommended_status")
    ctgov_review_bucket = row_get(reactivation_row, "review_bucket")

    rescreen_decision = row_get(rescreen_row, "decision").lower()
    rescreen_reason_codes = row_get(rescreen_row, "reason_codes")
    decision_change = ""
    if rescreen_decision:
        decision_change = "changed" if rescreen_decision != old_decision else "unchanged"

    hard_blocker = bool(hard_reasons)
    hard_policy_review_signal = bool(
        hard_reasons.intersection(policy_review_hard_reason_codes)
        and liquid_for_screen
        and has_interventional
        and lead_sponsor_count > 0
        and active_interventional_count > 0
    )
    reactivation_candidate = reactivation_status == "reactivation_candidate"
    reactivation_review = reactivation_status == "reactivation_review"
    rescreen_promotes = rescreen_decision in {"keep", "review"}
    manual_include_promotes = parse_boolish(manual_include) and old_decision == "remove"
    conditional_exclusion = bool(conditional_exclusion_row)
    conditional_exclusion_notes = row_get(conditional_exclusion_row, "notes")
    policy_inclusion = bool(policy_inclusion_row)
    policy_inclusion_notes = row_get(policy_inclusion_row, "notes")
    absolute_hard_blocker = bool(hard_reasons.intersection(absolute_hard_reason_codes)) or any(
        reason.startswith("non_active_listing:") for reason in hard_reasons
    )

    include_in_rescreen = False
    recommended_action = "keep_removed"
    action_priority = "none"
    candidate_override_decision = ""
    candidate_override_reason_codes = ""
    candidate_override_notes = ""

    if policy_inclusion and not absolute_hard_blocker and not conditional_exclusion:
        recommended_action = "candidate_promote_to_review"
        action_priority = row_get(policy_inclusion_row, "priority") or "medium"
        include_in_rescreen = False
        candidate_override_decision = "review"
        candidate_override_reason_codes = join_codes(
            [
                "manual_reactivation_review",
                "policy_inclusion",
                f"source_decision:{old_decision}" if old_decision else "",
                f"screen_reason:{join_codes(reason_codes)}" if reason_codes else "",
            ]
        )
        candidate_override_notes = (
            f"Inactive audit recommends review as of {asof_date.isoformat()} from policy inclusion; "
            f"{policy_inclusion_notes}; median_addv20={median_addv20:g}; "
            f"screen_ctgov_matches={interventional_count}; old_reason_codes={join_codes(reason_codes)}"
        )
    elif hard_blocker:
        if absolute_hard_blocker:
            recommended_action = "keep_removed_hard_blocker"
        elif conditional_exclusion and not reactivation_candidate:
            recommended_action = "conditional_exclusion_pending_reactivation_scan"
            action_priority = "none"
        elif hard_policy_review_signal or reactivation_candidate or rescreen_promotes or manual_include_promotes:
            recommended_action = "candidate_promote_to_review"
            action_priority = "high"
            candidate_override_decision = "review"
            candidate_override_reason_codes = join_codes(
                [
                    "manual_reactivation_review",
                    "reverse_split_scoring_penalty",
                    f"source_decision:{old_decision}" if old_decision else "",
                    f"ctgov:{reactivation_reason}" if reactivation_reason else "",
                    "screen_active_lead_signal" if hard_policy_review_signal else "",
                    "screen_rerun_promoted" if rescreen_promotes else "",
                    "manual_include_override" if manual_include_promotes else "",
                ]
            )
            candidate_override_notes = (
                f"Inactive audit recommends review as of {asof_date.isoformat()}; reverse split should remain a scoring penalty; "
                f"ctgov_status={reactivation_status or 'none'}; "
                f"active_interventional={active_interventional_count}; lead_matches={lead_sponsor_count}; "
                f"median_addv20={median_addv20:g}; old_reason_codes={join_codes(reason_codes)}"
            )
        else:
            recommended_action = "keep_removed_hard_blocker"
    elif reactivation_candidate or rescreen_promotes or manual_include_promotes:
        recommended_action = "candidate_promote_to_review"
        action_priority = "high" if reactivation_candidate or rescreen_decision == "keep" else "medium"
        candidate_override_decision = "review"
        candidate_override_reason_codes = join_codes(
            [
                "manual_reactivation_review",
                f"source_decision:{old_decision}" if old_decision else "",
                f"ctgov:{reactivation_reason}" if reactivation_reason else "",
                "screen_rerun_promoted" if rescreen_promotes else "",
                "manual_include_override" if manual_include_promotes else "",
            ]
        )
        candidate_override_notes = (
            f"Inactive audit recommends review as of {asof_date.isoformat()}; "
            f"ctgov_status={reactivation_status or 'none'}; "
            f"rescreen_decision={rescreen_decision or 'not_run'}; "
            f"old_reason_codes={join_codes(reason_codes)}"
        )
    elif reactivation_review:
        recommended_action = "manual_reactivation_review"
        action_priority = reactivation_priority or "medium"
        include_in_rescreen = True
    elif soft_reasons:
        recommended_action = "targeted_rescreen"
        action_priority = "medium" if "extremely_illiquid" not in soft_reasons else "low"
        include_in_rescreen = True
    else:
        recommended_action = "keep_removed_no_signal"

    if not hard_blocker and (reactivation_candidate or reactivation_review or soft_reasons):
        include_in_rescreen = True
    if rescreen_decision:
        include_in_rescreen = False

    return {
        "ticker": ticker,
        "company_name": screen_value(screen_row, db_row, "company_name", "CompanyName", "company"),
        "cik": screen_value(screen_row, db_row, "cik", "CIK"),
        "old_decision": old_decision,
        "screen_decision": screen_decision,
        "db_universe_status": db_universe_status,
        "db_is_active": db_is_active,
        "listing_status": listing_status,
        "manual_include": manual_include,
        "manual_exclude": manual_exclude,
        "manual_review": manual_review,
        "reason_codes": join_codes(reason_codes),
        "hard_blocker": int(hard_blocker),
        "hard_blocker_reasons": join_codes(hard_reasons),
        "soft_rescreen_reasons": join_codes(soft_reasons),
        "google_reverse_split_status": google_reverse_status,
        "google_reverse_split_confirmed": google_reverse_confirmed,
        "google_going_concern_status": google_gc_status,
        "google_going_concern_confirmed": google_gc_confirmed,
        "ctgov_has_interventional_trial_match": int(has_interventional),
        "ctgov_interventional_match_count": interventional_count,
        "ctgov_lead_sponsor_match_count": lead_sponsor_count,
        "ctgov_active_interventional_match_count": active_interventional_count,
        "ctgov_matched_ncts": matched_ncts,
        "median_addv20": row_get(screen_row, "median_addv20"),
        "liquidity_status": row_get(screen_row, "liquidity_status"),
        "liquid_for_screen": int(liquid_for_screen),
        "hard_blocker_policy_review_signal": int(hard_policy_review_signal),
        "conditional_exclusion": int(conditional_exclusion),
        "conditional_exclusion_notes": conditional_exclusion_notes,
        "policy_inclusion": int(policy_inclusion),
        "policy_inclusion_notes": policy_inclusion_notes,
        "ctgov_reactivation_status": reactivation_status,
        "ctgov_reactivation_priority": reactivation_priority,
        "ctgov_reactivation_reason": reactivation_reason,
        "ctgov_recommended_status": ctgov_recommended_status,
        "ctgov_review_bucket": ctgov_review_bucket,
        "rescreen_decision": rescreen_decision,
        "rescreen_reason_codes": rescreen_reason_codes,
        "decision_change": decision_change,
        "include_in_rescreen_tickers": int(include_in_rescreen),
        "recommended_action": recommended_action,
        "action_priority": action_priority,
        "candidate_override_decision": candidate_override_decision,
        "candidate_override_reason_codes": candidate_override_reason_codes,
        "candidate_override_notes": candidate_override_notes,
    }


def build_override_rows(audit_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in audit_rows:
        if str(row.get("candidate_override_decision") or "") != "review":
            continue
        rows.append(
            {
                "ticker": row["ticker"],
                "decision": "review",
                "listing_status": row.get("listing_status", ""),
                "manual_include": "true",
                "manual_exclude": "",
                "manual_review": "true",
                "reason_codes": row.get("candidate_override_reason_codes", ""),
                "notes": row.get("candidate_override_notes", ""),
            }
        )
    return rows


def main() -> None:
    configure_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else resolve_path(cfg_get(config, "inactive_removed_audit.output_dir", "../output/biotech_index_reports"), base_dir=base_dir)
    )
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    screen_path = (
        args.screen_results_csv.expanduser().resolve()
        if args.screen_results_csv
        else resolve_path(
            cfg_get(
                config,
                "inactive_removed_audit.screen_results_csv",
                cfg_get(config, "paths.screen_results_csv"),
            ),
            base_dir=base_dir,
        )
    )
    rescreen_path = (
        args.rescreen_results_csv.expanduser().resolve()
        if args.rescreen_results_csv
        else resolve_optional_path(cfg_get(config, "inactive_removed_audit.rescreen_results_csv"), base_dir=base_dir)
    )
    reactivation_scan_path = resolve_existing_csv(
        explicit=args.reactivation_scan_csv,
        configured_raw=cfg_get(config, "inactive_removed_audit.reactivation_scan_csv"),
        base_dir=base_dir,
        output_dir=output_dir,
        default_filename=str(cfg_get(config, "ctgov_reactivation.scan_csv", "ctgov_reactivation_scan.csv")),
    )
    asof = args.asof or datetime.now(timezone.utc).date().isoformat()
    ticker_filter = {normalize_ticker(value) for value in args.tickers.split(",") if normalize_ticker(value)}

    active_listing_statuses = {
        value.strip().lower()
        for value in normalize_string_list(cfg_get(config, "inactive_removed_audit.active_listing_statuses"), ["active"])
        if value.strip()
    }
    hard_block_reason_codes = {
        value.strip().lower()
        for value in normalize_string_list(
            cfg_get(config, "inactive_removed_audit.hard_block_reason_codes"),
            DEFAULT_HARD_BLOCK_REASON_CODES,
        )
        if value.strip()
    }
    policy_review_hard_reason_codes = {
        value.strip().lower()
        for value in normalize_string_list(
            cfg_get(config, "inactive_removed_audit.policy_review_hard_reason_codes"),
            DEFAULT_POLICY_REVIEW_HARD_REASON_CODES,
        )
        if value.strip()
    }
    absolute_hard_reason_codes = {
        value.strip().lower()
        for value in normalize_string_list(
            cfg_get(config, "inactive_removed_audit.absolute_hard_reason_codes"),
            DEFAULT_ABSOLUTE_HARD_REASON_CODES,
        )
        if value.strip()
    }
    soft_rescreen_reason_codes = {
        value.strip().lower()
        for value in normalize_string_list(
            cfg_get(config, "inactive_removed_audit.soft_rescreen_reason_codes"),
            DEFAULT_SOFT_RESCREEN_REASON_CODES,
        )
        if value.strip()
    }
    min_liquidity_addv20 = float(
        cfg_get(
            config,
            "inactive_removed_audit.min_liquidity_addv20",
            cfg_get(config, "biotech_features.min_liquidity_addv20", 1_000_000),
        )
    )

    if not screen_path.exists():
        raise FileNotFoundError(f"Screen results CSV not found: {screen_path}")

    screen_rows = read_keyed_csv(screen_path)
    rescreen_rows = read_keyed_csv(rescreen_path)
    reactivation_rows = read_keyed_csv(reactivation_scan_path)
    conditional_exclusions = load_conditional_exclusions(
        resolve_optional_path(cfg_get(config, "inactive_removed_audit.conditional_exclusions_csv"), base_dir=base_dir)
    )
    policy_inclusions = load_policy_inclusions(
        resolve_optional_path(cfg_get(config, "inactive_removed_audit.policy_inclusions_csv"), base_dir=base_dir)
    )

    sqlite_timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))
    with connect_readonly(db_path, timeout_sec=sqlite_timeout_sec) as conn:
        db_rows = load_db_companies(conn)

    tickers = set()
    for ticker, row in screen_rows.items():
        if row_get(row, "decision").lower() == "remove":
            tickers.add(ticker)
    for ticker, row in db_rows.items():
        db_active = int(row.get("is_active") or 0)
        db_status = str(row.get("universe_status") or "").strip().lower()
        if db_active == 0 or db_status == "remove":
            tickers.add(ticker)
    if ticker_filter:
        tickers &= ticker_filter

    audit_rows = [
        classify_row(
            asof_date=asof_date,
            ticker=ticker,
            screen_row=screen_rows.get(ticker, {}),
            db_row=db_rows.get(ticker, {}),
            rescreen_row=rescreen_rows.get(ticker, {}),
            reactivation_row=reactivation_rows.get(ticker, {}),
            conditional_exclusion_row=conditional_exclusions.get(ticker, {}),
            policy_inclusion_row=policy_inclusions.get(ticker, {}),
            active_listing_statuses=active_listing_statuses,
            hard_block_reason_codes=hard_block_reason_codes,
            policy_review_hard_reason_codes=policy_review_hard_reason_codes,
            absolute_hard_reason_codes=absolute_hard_reason_codes,
            soft_rescreen_reason_codes=soft_rescreen_reason_codes,
            min_liquidity_addv20=min_liquidity_addv20,
        )
        for ticker in sorted(tickers)
    ]
    audit_rows.sort(
        key=lambda row: (
            {
                "candidate_promote_to_review": 0,
                "manual_reactivation_review": 1,
                "targeted_rescreen": 2,
                "conditional_exclusion_pending_reactivation_scan": 3,
            }.get(
                str(row.get("recommended_action")),
                9,
            ),
            str(row.get("ticker")),
        )
    )

    audit_csv = output_dir / str(cfg_get(config, "inactive_removed_audit.audit_csv", "inactive_removed_audit.csv"))
    tickers_csv = output_dir / str(
        cfg_get(config, "inactive_removed_audit.rescreen_tickers_csv", "inactive_removed_rescreen_tickers.csv")
    )
    overrides_csv = output_dir / str(
        cfg_get(config, "inactive_removed_audit.status_override_candidates_csv", "inactive_removed_status_override_candidates.csv")
    )
    manifest_json = output_dir / str(cfg_get(config, "inactive_removed_audit.manifest_json", "inactive_removed_audit_manifest.json"))
    screen_config_path = resolve_path(
        cfg_get(config, "inactive_removed_audit.screen_config_yaml", "screen_biotech_universe_config.yaml"),
        base_dir=base_dir,
    )
    rescreen_output_dir = resolve_path(
        cfg_get(config, "inactive_removed_audit.rescreen_output_dir", "../output/biotech_index_reports/inactive_removed_rescreen"),
        base_dir=base_dir,
    )
    rescreen_output_file = str(
        cfg_get(config, "inactive_removed_audit.rescreen_output_file", "inactive_removed_rescreen_results.csv")
    )
    rescreen_command = [
        sys.executable,
        str(PACKAGE_ROOT / "screen_biotech_universe.py"),
        "--config",
        str(screen_config_path),
        "--tickers-csv",
        str(tickers_csv),
        "--output-dir",
        str(rescreen_output_dir),
        "--output-file",
        rescreen_output_file,
    ]

    write_csv(audit_csv, audit_rows, AUDIT_FIELDS)
    rescreen_tickers = [{"ticker": row["ticker"]} for row in audit_rows if int(row.get("include_in_rescreen_tickers") or 0) == 1]
    write_csv(tickers_csv, rescreen_tickers, ["ticker"])
    override_rows = build_override_rows(audit_rows)
    write_csv(overrides_csv, override_rows, OVERRIDE_FIELDS)

    manifest = {
        "asof_date": asof,
        "created_at": utc_now(),
        "screen_results_csv": str(screen_path),
        "rescreen_results_csv": str(rescreen_path) if rescreen_path else "",
        "reactivation_scan_csv": str(reactivation_scan_path) if reactivation_scan_path else "",
        "conditional_exclusion_count": len(conditional_exclusions),
        "policy_inclusion_count": len(policy_inclusions),
        "database_path": str(db_path),
        "audit_csv": str(audit_csv),
        "rescreen_tickers_csv": str(tickers_csv),
        "status_override_candidates_csv": str(overrides_csv),
        "suggested_rescreen_command": " ".join(f'"{part}"' if " " in part else part for part in rescreen_command),
        "suggested_rescreen_results_csv": str(rescreen_output_dir / rescreen_output_file),
        "suggested_second_pass_command": " ".join(
            f'"{part}"' if " " in part else part
            for part in [
                sys.executable,
                str(Path(__file__).resolve()),
                "--config",
                str(config_path),
                "--rescreen-results-csv",
                str(rescreen_output_dir / rescreen_output_file),
            ]
        ),
        "inactive_or_removed_count": len(audit_rows),
        "rescreen_ticker_count": len(rescreen_tickers),
        "override_candidate_count": len(override_rows),
        "recommended_action_counts": dict(Counter(str(row["recommended_action"]) for row in audit_rows)),
        "hard_blocker_count": sum(1 for row in audit_rows if int(row.get("hard_blocker") or 0) == 1),
        "hard_blocker_policy_review_signal_count": sum(
            1 for row in audit_rows if int(row.get("hard_blocker_policy_review_signal") or 0) == 1
        ),
    }
    manifest_json.parent.mkdir(parents=True, exist_ok=True)
    manifest_json.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

    LOGGER.info(
        "Inactive audit complete: rows=%d rescreen=%d override_candidates=%d audit_csv=%s",
        len(audit_rows),
        len(rescreen_tickers),
        len(override_rows),
        audit_csv,
    )


if __name__ == "__main__":
    main()
