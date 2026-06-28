#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
import time
from collections import Counter
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, normalize_string_list, resolve_path  # noqa: E402
from biotech_index.core.db import connect  # noqa: E402
from biotech_index.core.text_norm import normalize_ticker  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_CANDIDATES = PACKAGE_ROOT / "data" / "delisted_biotech_calibration_candidates.csv"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "output" / "biotech_index_reports" / "delisted_calibration_candidate_audit"

ALLOWED_CALIBRATION_COHORTS = frozenset(
    {
        "commercial_profitable_quality_or_mature",
        "commercial_turnaround_or_unprofitable_growth",
        "late_clinical_pivotal_or_registrational",
        "platform_partnered_modality_pipeline",
        "early_clinical_speculative_or_single_asset_pipeline",
    }
)

VALID_VERIFICATION_STATUSES = frozenset(
    {
        "pending_norgate_sec_price_identity",
        "norgate_price_identity_mapped_pending_final_review",
        "identity_conflict_do_not_promote",
        "active_stock_exception_do_not_promote",
    }
)

REQUIRED_CANDIDATE_COLUMNS = [
    "ticker",
    "company_name",
    "proposed_cohort",
    "exit_type",
    "acquirer_or_exit_event",
    "exit_year",
    "delisting_date",
    "price_start_date",
    "price_end_date",
    "cik",
    "cusip_or_figi",
    "cusip",
    "share_class_figi",
    "norgate_symbol",
    "norgate_security_name",
    "norgate_mapping_reason",
    "norgate_first_bar_date",
    "norgate_last_bar_date",
    "norgate_bar_count",
    "ticker_reuse_risk",
    "terminal_consideration",
    "terminal_date",
    "equity_recovery",
    "recovery_type",
    "cvr_handling",
    "drop_otc_tape",
    "terminal_value_source",
    "corporate_resolution_event",
    "verification_status",
    "include_in_calibration",
    "candidate_source",
    "stage_at_delisting",
    "notes",
]

AUDIT_FIELDS = [
    *REQUIRED_CANDIDATE_COLUMNS,
    "db_company_match_count",
    "db_company_name",
    "db_company_is_active",
    "db_company_universe_status",
    "db_company_listing_status",
    "db_name_similarity",
    "market_bar_sources",
    "trusted_price_source_count",
    "trusted_price_min_date",
    "trusted_price_max_date",
    "trusted_price_bar_count",
    "any_price_source_count",
    "any_price_min_date",
    "any_price_max_date",
    "any_price_bar_count",
    "already_in_active_calibration_cohorts",
    "candidate_blockers",
    "candidate_warnings",
    "recommended_next_action",
    "ready_for_manual_promotion",
]

PROMOTION_FIELDS = ["ticker", "biotech_calibration_cohort", "reason"]
NON_BLOCKING_PROMOTION_WARNINGS = frozenset(
    {
        "ticker_reuse_resolved_by_explicit_symbol_and_identity_keys",
    }
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Audit delisted biotech calibration candidates before they can be promoted "
            "into the official five-cohort calibration map."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--candidates", type=Path, default=DEFAULT_CANDIDATES)
    parser.add_argument("--cohort-csv", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--asof", type=str, default="")
    parser.add_argument(
        "--trusted-price-sources",
        type=str,
        default="norgate,norgate_adjusted,norgatedata,norgate_delisted,norgate_us_equities_total_return",
        help="Comma-separated market_bars_daily sources accepted for delisted calibration price verification.",
    )
    parser.add_argument("--min-trusted-bars", type=int, default=252)
    parser.add_argument(
        "--allow-current-active-symbol-collisions",
        action="store_true",
        help="Permit promotion-ready status even when the ticker currently belongs to an active DB company.",
    )
    return parser.parse_args()


def configure_logging() -> None:
    import logging

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    for handler in logging.getLogger().handlers:
        if handler.formatter is not None:
            handler.formatter.converter = time.gmtime


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_asof(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return date.today().isoformat()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return date.fromisoformat(text[:10]).isoformat()


def compact_date(raw: str) -> str:
    return parse_asof(raw).replace("-", "")


def as_bool(raw: object, default: bool = False) -> bool:
    text = str(raw or "").strip().lower()
    if not text:
        return default
    if text in {"1", "true", "t", "yes", "y", "on", "enabled"}:
        return True
    if text in {"0", "false", "f", "no", "n", "off", "disabled"}:
        return False
    return default


def parse_float(raw: object) -> float | None:
    text = str(raw or "").strip().replace("$", "").replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header: {path}")
        fields = [str(field or "").strip() for field in reader.fieldnames]
        missing = [field for field in REQUIRED_CANDIDATE_COLUMNS if field not in fields]
        if missing:
            raise ValueError(f"Candidate CSV missing required column(s) {missing}: {path}")
        rows: list[dict[str, str]] = []
        seen: set[str] = set()
        for line_no, row in enumerate(reader, start=2):
            clean = {field: str(row.get(field) or "").strip() for field in fields}
            ticker = normalize_ticker(clean.get("ticker"))
            if not ticker:
                raise ValueError(f"Candidate CSV row {line_no} missing ticker: {path}")
            if ticker in seen:
                raise ValueError(f"Duplicate delisted candidate ticker {ticker}: {path}")
            seen.add(ticker)
            clean["ticker"] = ticker
            rows.append(clean)
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def load_official_cohort_tickers(path: Path) -> dict[str, str]:
    if not path.exists():
        raise FileNotFoundError(f"Official calibration cohort CSV not found: {path}")
    out: dict[str, str] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"Official calibration cohort CSV has no header: {path}")
        fields = {str(field or "").strip() for field in reader.fieldnames}
        cohort_field = (
            "biotech_calibration_cohort"
            if "biotech_calibration_cohort" in fields
            else "official_cohort"
            if "official_cohort" in fields
            else "biotech_primary_cohort"
            if "biotech_primary_cohort" in fields
            else ""
        )
        if "ticker" not in fields or not cohort_field:
            raise ValueError(f"Official calibration cohort CSV missing ticker/cohort columns: {path}")
        for row in reader:
            ticker = normalize_ticker(row.get("ticker"))
            cohort = str(row.get(cohort_field) or "").strip()
            if ticker and cohort:
                out[ticker] = cohort
    return out


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def company_rows(conn: sqlite3.Connection, ticker: str) -> list[sqlite3.Row]:
    if not table_exists(conn, "companies"):
        return []
    return conn.execute(
        """
        SELECT ticker, company_name, is_active, universe_status, listing_status
        FROM companies
        WHERE ticker = ?
        ORDER BY is_active DESC, company_name
        """,
        (ticker,),
    ).fetchall()


def market_bar_rows(conn: sqlite3.Connection, ticker: str) -> list[sqlite3.Row]:
    if not table_exists(conn, "market_bars_daily"):
        return []
    return conn.execute(
        """
        SELECT
            source,
            COUNT(*) AS bar_count,
            MIN(bar_date) AS min_date,
            MAX(bar_date) AS max_date
        FROM market_bars_daily
        WHERE ticker = ?
        GROUP BY source
        ORDER BY source
        """,
        (ticker,),
    ).fetchall()


def name_similarity(left: str, right: str) -> float:
    left_clean = " ".join(str(left or "").upper().replace(",", " ").split())
    right_clean = " ".join(str(right or "").upper().replace(",", " ").split())
    if not left_clean or not right_clean:
        return 0.0
    return round(SequenceMatcher(None, left_clean, right_clean).ratio(), 4)


def aggregate_bars(rows: list[sqlite3.Row], *, trusted_sources: set[str]) -> dict[str, Any]:
    sources = []
    any_count = 0
    trusted_count = 0
    any_min = ""
    any_max = ""
    trusted_min = ""
    trusted_max = ""
    for row in rows:
        source = str(row["source"] or "").strip()
        source_key = source.lower()
        count = int(row["bar_count"] or 0)
        min_date = str(row["min_date"] or "")
        max_date = str(row["max_date"] or "")
        sources.append(f"{source}:{count}:{min_date}:{max_date}")
        any_count += count
        any_min = min([item for item in [any_min, min_date] if item] or [""])
        any_max = max([item for item in [any_max, max_date] if item] or [""])
        if source_key in trusted_sources:
            trusted_count += count
            trusted_min = min([item for item in [trusted_min, min_date] if item] or [""])
            trusted_max = max([item for item in [trusted_max, max_date] if item] or [""])
    return {
        "market_bar_sources": "|".join(sources),
        "trusted_price_source_count": len([row for row in rows if str(row["source"] or "").strip().lower() in trusted_sources]),
        "trusted_price_bar_count": trusted_count,
        "trusted_price_min_date": trusted_min,
        "trusted_price_max_date": trusted_max,
        "any_price_source_count": len(rows),
        "any_price_bar_count": any_count,
        "any_price_min_date": any_min,
        "any_price_max_date": any_max,
    }


def audit_candidate(
    row: dict[str, str],
    *,
    conn: sqlite3.Connection,
    official_cohorts: dict[str, str],
    trusted_sources: set[str],
    min_trusted_bars: int,
    allow_active_collisions: bool,
) -> dict[str, Any]:
    ticker = row["ticker"]
    out: dict[str, Any] = {field: row.get(field, "") for field in REQUIRED_CANDIDATE_COLUMNS}
    blockers: list[str] = []
    warnings: list[str] = []

    cohort = str(row.get("proposed_cohort") or "").strip()
    if cohort not in ALLOWED_CALIBRATION_COHORTS:
        blockers.append("invalid_proposed_cohort")

    if as_bool(row.get("include_in_calibration"), False):
        blockers.append("candidate_seed_row_must_not_be_pre_enabled")

    verification_status = str(row.get("verification_status") or "").strip()
    if verification_status not in VALID_VERIFICATION_STATUSES:
        blockers.append("unexpected_candidate_verification_status")
    if verification_status == "identity_conflict_do_not_promote":
        blockers.append("identity_conflict_do_not_promote")
    active_stock_exception = verification_status == "active_stock_exception_do_not_promote"
    if active_stock_exception:
        blockers.append("active_stock_exception_do_not_promote")

    if ticker in official_cohorts:
        blockers.append("already_in_active_calibration_cohorts")
    out["already_in_active_calibration_cohorts"] = official_cohorts.get(ticker, "")

    if not (row.get("cik") or row.get("cusip_or_figi") or row.get("cusip") or row.get("share_class_figi")):
        blockers.append("missing_cik_or_cusip_figi_identity_key")

    if not row.get("delisting_date"):
        blockers.append("missing_delisting_date")
    if not row.get("price_start_date"):
        blockers.append("missing_price_start_date")
    if not row.get("price_end_date"):
        blockers.append("missing_price_end_date")

    exit_type = str(row.get("exit_type") or "").strip().lower()
    if exit_type in {"strategic_acquisition", "merger"} and not row.get("terminal_consideration"):
        blockers.append("missing_terminal_consideration_for_transaction_exit")
    distress_text = " ".join(
        [
            exit_type,
            str(row.get("stage_at_delisting") or "").lower(),
            str(row.get("terminal_consideration") or "").lower(),
            str(row.get("corporate_resolution_event") or "").lower(),
        ]
    )
    bankruptcy_like = (
        not active_stock_exception
        and (
            exit_type.startswith("bankruptcy")
            or any(
                token in distress_text
                for token in (
                    "bankruptcy",
                    "chapter 11",
                    "chapter 7",
                    "liquidat",
                    "wind-down",
                    "wind down",
                    "distress",
                    "wipeout",
                    "restructur",
                )
            )
        )
    )
    if bankruptcy_like:
        recovery_type = str(row.get("recovery_type") or "").strip().lower()
        equity_recovery = parse_float(row.get("equity_recovery"))
        drop_otc_tape = as_bool(row.get("drop_otc_tape"), False)
        if not row.get("terminal_date"):
            blockers.append("missing_terminal_resolution_date")
        if recovery_type == "wipeout":
            if equity_recovery is None or abs(equity_recovery) > 1e-9:
                blockers.append("wipeout_recovery_must_be_hard_zero")
            if not drop_otc_tape:
                blockers.append("wipeout_requires_drop_otc_tape")
        elif recovery_type in {"settlement", "reorg_retained", "cash_cvr", "distressed_nonzero"}:
            if equity_recovery is None:
                warnings.append("nonzero_terminal_equity_recovery_amount_pending")
            if not row.get("cvr_handling"):
                warnings.append("nonzero_terminal_cvr_handling_pending")
            if not drop_otc_tape:
                warnings.append("nonzero_terminal_should_drop_otc_tape")
        elif recovery_type in {"likely_wipeout_unconfirmed", "wipeout_unconfirmed"}:
            blockers.append("wipeout_terminal_recovery_not_plan_verified")
            if not drop_otc_tape:
                warnings.append("unconfirmed_wipeout_should_drop_otc_tape")
        else:
            warnings.append("distress_or_bankruptcy_terminal_return_requires_special_handling")

    reuse = str(row.get("ticker_reuse_risk") or "").strip().lower()
    identity_key_count = sum(1 for key in ("cik", "cusip_or_figi", "cusip", "share_class_figi") if row.get(key))
    if reuse and reuse != "none_known" and not active_stock_exception:
        reuse_resolved = "resolved" in reuse and bool(row.get("norgate_symbol")) and identity_key_count >= 2
        if reuse_resolved:
            warnings.append("ticker_reuse_resolved_by_explicit_symbol_and_identity_keys")
        else:
            blockers.append("ticker_reuse_or_borderline_flag_requires_manual_identity_review")

    companies = company_rows(conn, ticker)
    out["db_company_match_count"] = len(companies)
    out["db_company_name"] = ""
    out["db_company_is_active"] = ""
    out["db_company_universe_status"] = ""
    out["db_company_listing_status"] = ""
    out["db_name_similarity"] = ""
    if companies:
        best = companies[0]
        db_name = str(best["company_name"] or "")
        out["db_company_name"] = db_name
        out["db_company_is_active"] = int(best["is_active"] or 0)
        out["db_company_universe_status"] = str(best["universe_status"] or "")
        out["db_company_listing_status"] = str(best["listing_status"] or "")
        out["db_name_similarity"] = name_similarity(row.get("company_name", ""), db_name)
        if int(best["is_active"] or 0) > 0 and not allow_active_collisions:
            blockers.append("ticker_currently_belongs_to_active_db_company")
        if float(out["db_name_similarity"] or 0.0) < 0.55:
            warnings.append("db_company_name_low_similarity")

    bars = aggregate_bars(market_bar_rows(conn, ticker), trusted_sources=trusted_sources)
    out.update(bars)
    if int(bars["trusted_price_bar_count"] or 0) < int(min_trusted_bars):
        blockers.append("insufficient_trusted_delisted_price_history")
    if int(bars["any_price_bar_count"] or 0) > 0 and int(bars["trusted_price_bar_count"] or 0) == 0:
        warnings.append("only_non_trusted_price_sources_found")

    blocking_warnings = [warning for warning in warnings if warning not in NON_BLOCKING_PROMOTION_WARNINGS]
    if blockers:
        action = "hold_pending_verification"
    elif blocking_warnings:
        action = "manual_review_before_promotion"
    else:
        action = "ready_for_manual_promotion"

    out["candidate_blockers"] = "|".join(dict.fromkeys(blockers))
    out["candidate_warnings"] = "|".join(dict.fromkeys(warnings))
    out["recommended_next_action"] = action
    out["ready_for_manual_promotion"] = 1 if action == "ready_for_manual_promotion" else 0
    return out


def main() -> int:
    configure_logging()
    args = parse_args()
    asof = parse_asof(args.asof)
    config = load_yaml(args.config)
    base_dir = args.config.resolve().parent
    db_path = (
        args.db.expanduser().resolve()
        if args.db
        else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    )
    cohort_settings = cfg_get(config, "biotech_scoring.calibration_cohorts", {}) or {}
    cohort_csv = (
        args.cohort_csv.expanduser().resolve()
        if args.cohort_csv
        else resolve_path(cohort_settings.get("csv", "data/biotech_calibration_cohorts.csv"), base_dir=base_dir)
    )
    candidates_path = args.candidates.expanduser().resolve()
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else DEFAULT_OUTPUT_ROOT / compact_date(asof)
    )
    trusted_sources = {item.strip().lower() for item in normalize_string_list(args.trusted_price_sources) if item.strip()}
    if not trusted_sources:
        raise ValueError("--trusted-price-sources cannot be empty")

    candidates = read_csv(candidates_path)
    official_cohorts = load_official_cohort_tickers(cohort_csv)

    with connect(db_path, timeout_sec=float(cfg_get(config, "sqlite_timeout_sec", 30.0))) as conn:
        audit_rows = [
            audit_candidate(
                row,
                conn=conn,
                official_cohorts=official_cohorts,
                trusted_sources=trusted_sources,
                min_trusted_bars=max(0, int(args.min_trusted_bars)),
                allow_active_collisions=bool(args.allow_current_active_symbol_collisions),
            )
            for row in candidates
        ]

    promotion_rows = [
        {
            "ticker": row["ticker"],
            "biotech_calibration_cohort": row["proposed_cohort"],
            "reason": (
                "verified delisted calibration candidate; "
                f"source={row.get('candidate_source', '')}; "
                f"exit_type={row.get('exit_type', '')}"
            ),
        }
        for row in audit_rows
        if int(row.get("ready_for_manual_promotion") or 0) > 0
    ]

    blockers = Counter()
    warnings = Counter()
    actions = Counter(str(row.get("recommended_next_action") or "") for row in audit_rows)
    for row in audit_rows:
        for item in str(row.get("candidate_blockers") or "").split("|"):
            if item:
                blockers[item] += 1
        for item in str(row.get("candidate_warnings") or "").split("|"):
            if item:
                warnings[item] += 1

    output_dir.mkdir(parents=True, exist_ok=True)
    audit_path = output_dir / "delisted_candidate_audit.csv"
    promotion_path = output_dir / "delisted_candidate_promotable_cohort_rows.csv"
    summary_path = output_dir / "delisted_candidate_audit_summary.json"
    write_csv(audit_path, audit_rows, AUDIT_FIELDS)
    write_csv(promotion_path, promotion_rows, PROMOTION_FIELDS)
    summary = {
        "created_at": utc_now(),
        "asof_date": asof,
        "candidate_csv": str(candidates_path),
        "official_cohort_csv": str(cohort_csv),
        "db_path": str(db_path),
        "trusted_price_sources": sorted(trusted_sources),
        "min_trusted_bars": int(args.min_trusted_bars),
        "candidate_count": len(audit_rows),
        "ready_for_manual_promotion_count": len(promotion_rows),
        "actions": dict(sorted(actions.items())),
        "blockers": dict(sorted(blockers.items())),
        "warnings": dict(sorted(warnings.items())),
        "audit_csv": str(audit_path),
        "promotable_cohort_rows_csv": str(promotion_path),
    }
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
