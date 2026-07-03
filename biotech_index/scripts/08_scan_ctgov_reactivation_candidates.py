#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
import importlib.util
import json
import logging
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.clients.ctgov_client import parse_sponsors, parse_study
from biotech_index.core.config import cfg_get, load_yaml, normalize_string_list, resolve_optional_path, resolve_path
from biotech_index.core.db import connect, finish_run, init_db, start_run, utc_now
from biotech_index.core.http_cache import HostThrottle
from biotech_index.core.logging_utils import configure_utc_logging
from biotech_index.core.pipeline_guards import validate_nonempty_selection, validate_requested_tickers
from biotech_index.core.text_norm import normalize_ticker


LOGGER = logging.getLogger("scan_ctgov_reactivation_candidates")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def load_script_module(filename: str, module_name: str) -> Any:
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load script module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise ImportError(f"Failed to load helper script {path}: {type(exc).__name__}: {exc}") from exc
    return module


# Reuse the current numbered pipeline scripts directly so reactivation review
# stays aligned with the production CTGov search, linking, and audit rules.
# Load lazily from main() so importing this module does not execute helper scripts.
SYNC_HELPERS: Any = None
LINK_HELPERS: Any = None
AUDIT_HELPERS: Any = None


def load_helper_modules() -> None:
    global SYNC_HELPERS, LINK_HELPERS, AUDIT_HELPERS
    SYNC_HELPERS = load_script_module("03_sync_ctgov_trials.py", "biotech_index_scripts_03_sync_ctgov_trials")
    LINK_HELPERS = load_script_module("04_link_trials_to_companies.py", "biotech_index_scripts_04_link_trials_to_companies")
    AUDIT_HELPERS = load_script_module("05_audit_ctgov_trial_links.py", "biotech_index_scripts_05_audit_ctgov_trial_links")


SCAN_FIELDS = [
    "ticker",
    "company_name",
    "current_universe_status",
    "current_scoring_status",
    "reactivation_status",
    "reactivation_priority",
    "reactivation_reason",
    "policy_override_applied",
    "policy_override_notes",
    "recommended_status",
    "review_bucket",
    "root_cause_category",
    "recommended_fix",
    "review_reason",
    "audit_score",
    "active_positive_score",
    "risk_penalty",
    "primary_nct",
    "primary_trial_title",
    "primary_trial_score",
    "days_since_last_update",
    "is_pivotal",
    "pipeline_density",
    "total_linked_trials",
    "active_trials",
    "qualifying_trial_count",
    "qualifying_active_trial_count",
    "verified_qualifying_active_trial_count",
    "active_any_lead_trials",
    "active_any_program_trials",
    "weak_active_lead_or_program_trials",
    "active_lead_sponsor_trials",
    "active_collaborator_trials",
    "active_program_override_trials",
    "active_nonqualifying_trials",
    "active_nonqualifying_device_trials",
    "active_nonqualifying_observational_trials",
    "active_weak_link_trials",
    "active_stale_qualifying_trials",
    "phase2_3_active_trials",
    "non_drug_device_diagnostic_trials",
    "completed_or_terminated_trials",
    "low_confidence_links",
    "weak_company_link_trials",
    "stale_active_trials",
    "company_diagnostic_like",
    "alias_count",
    "manual_alias_count",
    "manual_aliases",
    "search_count",
    "search_terms",
    "query_hit_count",
    "unique_study_count",
    "scan_error",
    "sample_ncts",
    "source_reason_codes",
]

EVIDENCE_FIELDS = [
    "ticker",
    "company_name",
    "current_universe_status",
    "current_scoring_status",
    "reactivation_status",
    "reactivation_priority",
    "policy_override_applied",
    "policy_override_notes",
    "nct_id",
    "brief_title",
    "overall_status",
    "phase_text",
    "phase_rank",
    "study_type",
    "primary_purpose",
    "match_roles",
    "match_methods",
    "strong_company_link",
    "max_confidence",
    "is_active_status",
    "is_pivotal",
    "is_qualifying_device",
    "is_therapeutic",
    "is_non_therapeutic",
    "qualifying_trial",
    "trial_score",
    "days_since_last_update",
    "last_update_post_date",
    "primary_completion_date",
    "intervention_types",
    "intervention_names",
    "therapeutic_keyword_hits",
    "non_therapeutic_keyword_hits",
    "qualifying_device_keyword_hits",
    "exclusion_reasons",
    "sponsors",
    "query_hit_search_terms",
    "query_hit_fields",
    "query_hit_sources",
    "query_hit_max_confidence",
    "outcome_override_applied",
    "outcome_override_status",
    "outcome_override_reason",
    "outcome_override_source_url",
    "outcome_override_manual_review",
]


@dataclass(frozen=True)
class ReactivationJob:
    audit_company: Any
    link_company: Any
    scoring_status: str
    source_manual_verdict: str
    all_aliases: tuple[str, ...]
    manual_aliases: tuple[str, ...]
    sync_job: Any


@dataclass(frozen=True)
class ReactivationPolicyOverride:
    ticker: str
    reactivation_status: str
    reactivation_priority: str
    reactivation_reason: str
    notes: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scan removed names for CTGov reactivation candidates.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--tickers", type=str, default="", help="Optional comma-separated ticker subset.")
    parser.add_argument("--max-workers", type=int, default=None)
    parser.add_argument("--max-companies", type=int, default=0, help="Optional limit for smoke tests. 0 means all.")
    parser.add_argument("--asof", type=str, default="", help="Review date in YYYY-MM-DD. Defaults to today.")
    parser.add_argument("--all-removed", action="store_true", help="Scan all removed companies instead of the audit review CSV.")
    parser.add_argument("--allow-partial", action="store_true", help="Return success even if one or more CTGov scan jobs fail.")
    return parser.parse_args()


def configure_logging() -> None:
    configure_utc_logging()


def to_int(raw: object) -> int:
    try:
        return int(float(str(raw or "").strip()))
    except (TypeError, ValueError):
        return 0


def to_float(raw: object) -> float:
    try:
        return float(str(raw or "").strip())
    except (TypeError, ValueError):
        return 0.0


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True), encoding="utf-8")


def as_bool(raw: object, default: bool = False) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in {"1", "true", "t", "yes", "y", "enabled", "on"}:
        return True
    if text in {"0", "false", "f", "no", "n", "disabled", "off"}:
        return False
    return default


def load_source_review_rows(path: Path, *, final_status_filter: set[str], ticker_filter: set[str]) -> dict[str, dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"CTGov source review CSV not found: {path}")
    out: dict[str, dict[str, str]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            return {}
        for row in reader:
            ticker = str(row.get("ticker") or "").strip().upper().replace(".", "-")
            if not ticker:
                continue
            if ticker_filter and ticker not in ticker_filter:
                continue
            final_status = str(row.get("final_status") or row.get("recommended_status") or "").strip().lower()
            if final_status_filter and final_status not in final_status_filter:
                continue
            out[ticker] = {str(k): str(v or "") for k, v in row.items()}
    return out


def load_policy_overrides(path: Path | None, *, ticker_filter: set[str]) -> dict[str, ReactivationPolicyOverride]:
    if path is None or not path.exists():
        return {}
    out: dict[str, ReactivationPolicyOverride] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            return {}
        for line_no, row in enumerate(reader, start=2):
            ticker = str(row.get("ticker") or "").strip().upper().replace(".", "-")
            if not ticker:
                continue
            if ticker_filter and ticker not in ticker_filter:
                continue
            if not as_bool(row.get("enabled", "true")):
                continue
            reactivation_status = str(row.get("reactivation_status") or "").strip()
            reactivation_priority = str(row.get("reactivation_priority") or "").strip()
            reactivation_reason = str(row.get("reactivation_reason") or "").strip()
            if not reactivation_status or not reactivation_priority or not reactivation_reason:
                LOGGER.warning("Ignoring invalid reactivation policy override at %s:%d", path, line_no)
                continue
            out[ticker] = ReactivationPolicyOverride(
                ticker=ticker,
                reactivation_status=reactivation_status,
                reactivation_priority=reactivation_priority,
                reactivation_reason=reactivation_reason,
                notes=str(row.get("notes") or "").strip(),
            )
    return out


def load_audit_companies_for_scan(
    conn: sqlite3.Connection,
    *,
    status_filter: set[str],
    ticker_filter: set[str],
    include_inactive: bool,
) -> list[Any]:
    if not include_inactive:
        return AUDIT_HELPERS.load_companies(conn, status_filter=status_filter, ticker_filter=ticker_filter)
    rows = conn.execute(
        """
        SELECT company_id, ticker, company_name, universe_status, industry, industry_aggregate, reason_codes
        FROM companies
        ORDER BY ticker
        """
    ).fetchall()
    companies: list[Any] = []
    for row in rows:
        ticker = str(row["ticker"] or "").upper()
        status = str(row["universe_status"] or "").lower()
        if status_filter and status not in status_filter:
            continue
        if ticker_filter and ticker not in ticker_filter:
            continue
        companies.append(
            AUDIT_HELPERS.Company(
                company_id=int(row["company_id"]),
                ticker=ticker,
                company_name=str(row["company_name"] or ""),
                universe_status=status,
                industry=str(row["industry"] or ""),
                industry_aggregate=str(row["industry_aggregate"] or ""),
                reason_codes=str(row["reason_codes"] or ""),
            )
        )
    return companies


def load_link_companies_for_scan(
    conn: sqlite3.Connection,
    *,
    status_filter: set[str],
    ticker_filter: set[str],
    include_inactive: bool,
) -> list[Any]:
    if not include_inactive:
        return LINK_HELPERS.load_companies(conn, status_filter=status_filter, ticker_filter=ticker_filter)
    rows = conn.execute(
        """
        SELECT company_id, ticker, company_name, universe_status
        FROM companies
        ORDER BY ticker
        """
    ).fetchall()
    companies: list[Any] = []
    for row in rows:
        ticker = str(row["ticker"] or "").upper()
        status = str(row["universe_status"] or "").lower()
        if status_filter and status not in status_filter:
            continue
        if ticker_filter and ticker not in ticker_filter:
            continue
        alias_rows = conn.execute(
            """
            SELECT alias_norm
            FROM company_aliases
            WHERE company_id = ?
            ORDER BY is_manual DESC, confidence DESC, LENGTH(alias_norm) DESC
            """,
            (int(row["company_id"]),),
        ).fetchall()
        alias_norms = {
            LINK_HELPERS.normalize_org_name(alias_row["alias_norm"])
            for alias_row in alias_rows
            if str(alias_row["alias_norm"] or "").strip()
        }
        company_name = str(row["company_name"] or "")
        alias_norms.add(LINK_HELPERS.normalize_org_name(company_name))
        alias_norms.add(LINK_HELPERS.strip_corporate_suffixes(LINK_HELPERS.normalize_org_name(company_name)))
        alias_norms = {alias for alias in alias_norms if alias}
        tokens = tuple(frozenset(value) for value in LINK_HELPERS.alias_token_sets(alias_norms))
        companies.append(
            LINK_HELPERS.CompanyAliases(
                company_id=int(row["company_id"]),
                ticker=ticker,
                company_name=company_name,
                alias_norms=frozenset(alias_norms),
                alias_tokens=tokens,
            )
        )
    return companies


def load_jobs(
    conn: sqlite3.Connection,
    *,
    status_filter: set[str],
    source_rows: dict[str, dict[str, str]],
    restrict_to_source_rows: bool,
    ticker_filter: set[str],
    max_companies: int,
    min_alias_length: int,
    max_aliases_per_company: int,
    query_fields: list[str],
    search_overrides: dict[str, list[Any]],
) -> list[ReactivationJob]:
    effective_ticker_filter = set(ticker_filter)
    if restrict_to_source_rows:
        effective_ticker_filter |= set(source_rows)
    effective_status_filter = set(status_filter)
    if restrict_to_source_rows:
        effective_status_filter = set()

    include_inactive = not restrict_to_source_rows
    audit_companies = load_audit_companies_for_scan(
        conn,
        status_filter=effective_status_filter,
        ticker_filter=effective_ticker_filter,
        include_inactive=include_inactive,
    )
    link_companies = {
        company.company_id: company
        for company in load_link_companies_for_scan(
            conn,
            status_filter=effective_status_filter,
            ticker_filter=effective_ticker_filter,
            include_inactive=include_inactive,
        )
    }
    jobs: list[ReactivationJob] = []
    for company in audit_companies:
        link_company = link_companies.get(company.company_id)
        if link_company is None:
            continue
        if restrict_to_source_rows and company.ticker not in source_rows:
            continue
        search_aliases = SYNC_HELPERS.load_company_aliases(
            conn,
            company_id=company.company_id,
            fallback_name=company.company_name,
            min_alias_length=min_alias_length,
            max_aliases=max_aliases_per_company,
        )
        all_aliases, manual_aliases = AUDIT_HELPERS.load_aliases(conn, company.company_id)
        searches = SYNC_HELPERS.build_company_searches(
            aliases=search_aliases,
            query_fields=query_fields,
            overrides=search_overrides.get(company.ticker, []),
        )
        jobs.append(
            ReactivationJob(
                audit_company=company,
                link_company=link_company,
                scoring_status=str(
                    source_rows.get(company.ticker, {}).get("final_status")
                    or source_rows.get(company.ticker, {}).get("recommended_status")
                    or ""
                ).strip().lower(),
                source_manual_verdict=str(source_rows.get(company.ticker, {}).get("manual_verdict") or "").strip().lower(),
                all_aliases=tuple(all_aliases),
                manual_aliases=tuple(manual_aliases),
                sync_job=SYNC_HELPERS.CompanyJob(
                    company_id=company.company_id,
                    ticker=company.ticker,
                    company_name=company.company_name,
                    aliases=tuple(search_aliases),
                    searches=tuple(searches),
                ),
            )
        )
        if max_companies > 0 and len(jobs) >= max_companies:
            break
    return jobs


def classify_reactivation(
    audit_row: dict[str, Any],
    *,
    scan_error: str,
    source_manual_verdict: str,
) -> tuple[str, str, str]:
    if scan_error:
        return ("scan_error", "high", "ctgov_scan_error")

    if source_manual_verdict == "manual_remove":
        return ("no_reactivation_signal", "none", "manual_remove_persisted")

    recommended_status = str(audit_row.get("recommended_status") or "").strip().lower()
    review_bucket = str(audit_row.get("review_bucket") or "").strip()
    root_cause_category = str(audit_row.get("root_cause_category") or "").strip()
    active_trials = to_int(audit_row.get("active_trials"))
    qualifying_active = to_int(audit_row.get("qualifying_active_trial_count"))
    active_any_lead = to_int(audit_row.get("active_any_lead_trials"))
    active_any_program = to_int(audit_row.get("active_any_program_trials"))
    weak_lead_program = to_int(audit_row.get("weak_active_lead_or_program_trials"))

    active_signal = any(
        value > 0 for value in (active_trials, qualifying_active, active_any_lead, active_any_program, weak_lead_program)
    )
    lead_program_signal = any(value > 0 for value in (active_any_lead, active_any_program, weak_lead_program))

    if recommended_status == "keep":
        return ("reactivation_candidate", "high", "keep_signal_from_removed_universe")

    if review_bucket == "collaborator_only_active" and not lead_program_signal:
        return ("no_reactivation_signal", "none", "collaborator_only_without_program_ownership")

    if review_bucket == "post_market_device_only":
        return ("no_reactivation_signal", "none", "post_market_device_only_default_exclusion")

    if review_bucket == "active_study_exists_but_not_qualifying":
        if root_cause_category == "active_but_nonqualifying_device":
            return ("reactivation_review", "low", "device_exception_review")
        return ("no_reactivation_signal", "none", "nonqualifying_active_default_exclusion")

    if review_bucket in {"lead_rows_exist_but_weak_alias_match", "active_study_missing_from_match"} and lead_program_signal:
        return ("reactivation_candidate", "high", "lead_or_program_signal_needs_link_fix")

    if active_signal:
        priority = "medium" if qualifying_active > 0 or lead_program_signal else "low"
        reason = review_bucket or "active_signal_needs_review"
        return ("reactivation_review", priority, reason)

    return ("no_reactivation_signal", "none", review_bucket or "no_reactivation_signal")


def apply_policy_override(
    ticker: str,
    *,
    reactivation_status: str,
    reactivation_priority: str,
    reactivation_reason: str,
    overrides: dict[str, ReactivationPolicyOverride],
) -> tuple[str, str, str, bool, str]:
    override = overrides.get(str(ticker or "").strip().upper().replace(".", "-"))
    if override is None:
        return (reactivation_status, reactivation_priority, reactivation_reason, False, "")
    return (
        override.reactivation_status,
        override.reactivation_priority,
        override.reactivation_reason,
        True,
        override.notes,
    )


def trial_row_from_study(study: dict[str, Any]) -> dict[str, Any]:
    parsed = parse_study(study)
    if parsed is None:
        return {}
    return {
        "nct_id": parsed.nct_id,
        "brief_title": parsed.brief_title,
        "study_type": parsed.study_type,
        "phase_text": parsed.phase_text,
        "overall_status": parsed.overall_status,
        "lead_sponsor": parsed.lead_sponsor,
        "last_update_post_date": parsed.last_update_post_date,
        "has_results": 1 if parsed.has_results else 0,
        "primary_completion_date": parsed.primary_completion_date,
        "enrollment_count": parsed.enrollment_count if parsed.enrollment_count is not None else "",
        "raw_hash": parsed.raw_hash,
        "raw_json": parsed.raw_json,
    }


def main() -> None:
    configure_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    load_helper_modules()
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else resolve_path(cfg_get(config, "ctgov_reactivation.output_dir", "../output/biotech_index_reports"), base_dir=base_dir)
    )
    asof_date = AUDIT_HELPERS.parse_date(args.asof) if args.asof else datetime.now(timezone.utc).date()
    if asof_date is None:
        raise ValueError(f"Invalid --asof date: {args.asof}")

    sqlite_timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))
    status_filter = {
        value.lower()
        for value in normalize_string_list(cfg_get(config, "ctgov_reactivation.status_filter"), ["remove"])
    }
    ticker_filter = {normalize_ticker(value) for value in args.tickers.split(",") if normalize_ticker(value)}
    if (ticker_filter or int(args.max_companies) > 0 or args.all_removed) and args.output_dir is None:
        raise ValueError(
            "--tickers, --max-companies, and --all-removed are subset/broad-review modes and must be paired with --output-dir "
            "so canonical CTGov reactivation outputs are not overwritten."
        )
    query_fields = normalize_string_list(cfg_get(config, "ctgov.query_fields"), ["query.spons", "query.lead"])
    override_default_query_fields = normalize_string_list(
        cfg_get(config, "ctgov.override_default_query_fields"), ["query.intr"]
    )
    min_alias_length = int(cfg_get(config, "ctgov.min_alias_length", 4))
    max_aliases_per_company = int(cfg_get(config, "ctgov.max_aliases_per_company", 4))
    studies_url = str(cfg_get(config, "ctgov.studies_url", "https://clinicaltrials.gov/api/v2/studies"))
    cache_dir = resolve_path(cfg_get(config, "ctgov.cache_dir", "../output/biotech_index_cache"), base_dir=base_dir)
    page_size = int(cfg_get(config, "ctgov.page_size", 100))
    max_pages = int(cfg_get(config, "ctgov.max_pages", 25))
    ttl_hours = float(cfg_get(config, "ctgov.json_ttl_hours", 168.0))
    sleep_sec = float(cfg_get(config, "ctgov.sleep_sec", 0.2))
    timeout_sec = float(cfg_get(config, "ctgov.timeout_sec", 45.0))
    max_retries = int(cfg_get(config, "ctgov.max_retries", 3))
    max_workers = int(args.max_workers if args.max_workers is not None else cfg_get(config, "ctgov.max_workers", 4))
    min_confidence = float(cfg_get(config, "trial_linking.min_confidence", 0.65))
    allow_single_token_match = str(cfg_get(config, "trial_linking.allow_single_token_match", False)).strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
    }
    allow_single_token_prefix_match = str(
        cfg_get(config, "trial_linking.allow_single_token_prefix_match", True)
    ).strip().lower() in {"1", "true", "yes", "y"}
    single_token_prefix_min_length = int(cfg_get(config, "trial_linking.single_token_prefix_min_length", 7))

    active_statuses = {
        value.upper()
        for value in normalize_string_list(
            cfg_get(config, "ctgov_audit.active_statuses"),
            ["RECRUITING", "ACTIVE_NOT_RECRUITING", "NOT_YET_RECRUITING", "ENROLLING_BY_INVITATION"],
        )
    }
    stale_days = int(cfg_get(config, "ctgov_audit.stale_days", 365))
    completed_stale_days = int(cfg_get(config, "ctgov_audit.completed_stale_days", 730))
    low_confidence_threshold = float(cfg_get(config, "ctgov_audit.low_confidence_threshold", 0.75))
    min_keep_score = float(cfg_get(config, "ctgov_audit.min_keep_score", 3.0))
    therapeutic_types = {
        value.upper() for value in normalize_string_list(cfg_get(config, "ctgov_audit.therapeutic_intervention_types"), [])
    }
    non_therapeutic_types = {
        value.upper() for value in normalize_string_list(cfg_get(config, "ctgov_audit.non_therapeutic_intervention_types"), [])
    }
    non_therapeutic_purposes = {
        value.upper() for value in normalize_string_list(cfg_get(config, "ctgov_audit.non_therapeutic_primary_purposes"), [])
    }
    therapeutic_keywords = normalize_string_list(cfg_get(config, "ctgov_audit.therapeutic_keywords"), [])
    non_therapeutic_keywords = normalize_string_list(cfg_get(config, "ctgov_audit.non_therapeutic_keywords"), [])
    qualifying_device_keywords = normalize_string_list(cfg_get(config, "ctgov_audit.qualifying_device_keywords"), [])
    diagnostic_keywords = normalize_string_list(cfg_get(config, "ctgov_audit.diagnostic_company_keywords"), [])

    search_overrides_path = resolve_optional_path(cfg_get(config, "ctgov.search_overrides_csv"), base_dir=base_dir)
    program_owner_overrides_path = resolve_optional_path(
        cfg_get(config, "trial_linking.program_owner_overrides_csv"),
        base_dir=base_dir,
    )
    policy_overrides_path = resolve_optional_path(
        cfg_get(config, "ctgov_reactivation.policy_overrides_csv"),
        base_dir=base_dir,
    )
    trial_status_overrides_csv = resolve_optional_path(cfg_get(config, "ctgov_audit.trial_status_overrides_csv"), base_dir=base_dir)
    source_review_csv = resolve_path(
        cfg_get(config, "ctgov_reactivation.source_review_csv", "../output/biotech_index_reports/ctgov_trial_link_review.csv"),
        base_dir=base_dir,
    )
    source_final_status_filter = {
        value.lower()
        for value in normalize_string_list(
            cfg_get(config, "ctgov_reactivation.source_final_status_filter"),
            ["review", "remove", "remove_candidate"],
        )
    }
    scan_csv = output_dir / str(cfg_get(config, "ctgov_reactivation.scan_csv", "ctgov_reactivation_scan.csv"))
    review_csv = output_dir / str(cfg_get(config, "ctgov_reactivation.review_csv", "ctgov_reactivation_review.csv"))
    evidence_csv = output_dir / str(cfg_get(config, "ctgov_reactivation.evidence_csv", "ctgov_reactivation_evidence.csv"))
    manifest_json = output_dir / str(cfg_get(config, "ctgov_reactivation.manifest_json", "ctgov_reactivation_manifest.json"))

    search_overrides = SYNC_HELPERS.load_ctgov_search_overrides(
        search_overrides_path,
        default_query_fields=override_default_query_fields,
    )
    source_rows = (
        {}
        if args.all_removed
        else load_source_review_rows(
            source_review_csv,
            final_status_filter=source_final_status_filter,
            ticker_filter=ticker_filter,
        )
    )
    selection_mode = "all_removed_status_filter" if args.all_removed else "source_review_rows"
    policy_overrides = load_policy_overrides(policy_overrides_path, ticker_filter=ticker_filter)
    trial_status_overrides = {
        (normalize_ticker(row.get("ticker")), str(row.get("nct_id") or "").strip().upper()): row
        for row in AUDIT_HELPERS.load_trial_status_overrides(trial_status_overrides_csv)
        if normalize_ticker(row.get("ticker")) and str(row.get("nct_id") or "").strip()
    }

    scan_rows: list[dict[str, Any]] = []
    review_rows: list[dict[str, Any]] = []
    evidence_rows: list[dict[str, Any]] = []

    run_id: int | None = None
    with connect(db_path, timeout_sec=sqlite_timeout_sec) as conn:
        init_db(conn)
        try:
            run_id = start_run(conn, run_type="scan_ctgov_reactivation_candidates", input_path=db_path)
            jobs = load_jobs(
                conn,
                status_filter=status_filter,
                source_rows=source_rows,
                restrict_to_source_rows=not args.all_removed,
                ticker_filter=ticker_filter,
                max_companies=args.max_companies,
                min_alias_length=min_alias_length,
                max_aliases_per_company=max_aliases_per_company,
                query_fields=query_fields,
                search_overrides=search_overrides,
            )
            LOGGER.info(
                "Loaded %d companies for reactivation scan mode=%s status_filter_applied=%s status_filter=%s source_rows=%d",
                len(jobs),
                selection_mode,
                bool(args.all_removed),
                sorted(status_filter),
                len(source_rows),
            )
            validate_nonempty_selection(
                count=len(jobs),
                context="CTGov reactivation scan",
                subset_mode=bool(ticker_filter) or int(args.max_companies) > 0,
            )
            validate_requested_tickers(
                requested_tickers=ticker_filter,
                loaded_tickers=[job.audit_company.ticker for job in jobs],
                context="CTGov reactivation scan",
            )

            throttle = HostThrottle()
            results: list[Any] = []
            if max_workers <= 1 or len(jobs) <= 1:
                for job in jobs:
                    results.append(
                        SYNC_HELPERS.sync_one_company(
                            job.sync_job,
                            cache_dir=cache_dir,
                            studies_url=studies_url,
                            query_fields=query_fields,
                            page_size=page_size,
                            max_pages=max_pages,
                            ttl_hours=ttl_hours,
                            sleep_sec=sleep_sec,
                            timeout_sec=timeout_sec,
                            max_retries=max_retries,
                            throttle=throttle,
                        )
                    )
            else:
                futures: dict[Any, ReactivationJob] = {}
                pending_raise: BaseException | None = None
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    for job in jobs:
                        future = executor.submit(
                            SYNC_HELPERS.sync_one_company,
                            job.sync_job,
                            cache_dir=cache_dir,
                            studies_url=studies_url,
                            query_fields=query_fields,
                            page_size=page_size,
                            max_pages=max_pages,
                            ttl_hours=ttl_hours,
                            sleep_sec=sleep_sec,
                            timeout_sec=timeout_sec,
                            max_retries=max_retries,
                            throttle=throttle,
                        )
                        futures[future] = job
                    for future in as_completed(futures):
                        try:
                            result = future.result()
                        except BaseException as exc:
                            if isinstance(exc, (SystemExit, KeyboardInterrupt, GeneratorExit)):
                                pending_raise = exc
                                for pending in futures:
                                    pending.cancel()
                                break
                            job = futures[future]
                            LOGGER.exception("Unexpected worker failure for %s: %s", job.sync_job.ticker, exc)
                            result = SYNC_HELPERS.SyncResult(
                                company_id=job.sync_job.company_id,
                                ticker=job.sync_job.ticker,
                                alias_count=len(job.sync_job.aliases),
                                search_count=len(job.sync_job.searches),
                                study_count=0,
                                studies={},
                                query_hits=(),
                                error=f"{type(exc).__name__}: {exc}",
                            )
                        results.append(result)
                if pending_raise is not None:
                    raise pending_raise

            job_by_company_id = {job.audit_company.company_id: job for job in jobs}
            scan_result_by_company_id = {int(result.company_id): result for result in results}

            studies_by_nct: dict[str, dict[str, Any]] = {}
            sponsor_rows: list[Any] = []
            query_hit_rows: list[Any] = []
            raw_query_hits_by_key: dict[tuple[int, str], list[Any]] = {}
            for result in results:
                for nct_id, study in result.studies.items():
                    studies_by_nct[nct_id] = study
                for sponsor in (parsed for study in result.studies.values() for parsed in parse_sponsors(study)):
                    sponsor_rows.append(
                        LINK_HELPERS.SponsorRow(
                            nct_id=sponsor.nct_id,
                            sponsor_name=sponsor.sponsor_name,
                            sponsor_name_norm=sponsor.sponsor_name_norm,
                            sponsor_role=sponsor.sponsor_role,
                        )
                    )
                for hit in result.query_hits:
                    query_hit_rows.append(
                        LINK_HELPERS.QueryHitRow(
                            nct_id=hit.nct_id,
                            company_id=hit.company_id,
                            query_field=hit.query_field,
                            source=hit.source,
                            confidence=hit.confidence,
                        )
                    )
                    raw_query_hits_by_key.setdefault((int(hit.company_id), str(hit.nct_id)), []).append(hit)

            link_companies = [job.link_company for job in jobs]
            job_ticker_filter = {job.audit_company.ticker for job in jobs}
            program_owner_overrides = LINK_HELPERS.load_program_owner_overrides(
                program_owner_overrides_path,
                companies=link_companies,
                ticker_filter=job_ticker_filter,
            )
            links = LINK_HELPERS.dedupe_links(
                [
                    *LINK_HELPERS.build_links(
                        sponsor_rows,
                        link_companies,
                        min_confidence=min_confidence,
                        allow_single_token_match=allow_single_token_match,
                        allow_single_token_prefix_match=allow_single_token_prefix_match,
                        single_token_prefix_min_length=single_token_prefix_min_length,
                    ),
                    *LINK_HELPERS.query_hit_links(query_hit_rows, min_confidence=min_confidence),
                    *LINK_HELPERS.program_owner_links(program_owner_overrides, min_confidence=min_confidence),
                ]
            )

            sponsor_text_by_nct: dict[str, str] = {}
            for sponsor in sponsor_rows:
                key = str(sponsor.nct_id)
                text = f"{sponsor.sponsor_name} [{sponsor.sponsor_role}]"
                sponsor_text_by_nct.setdefault(key, "")
                existing = sponsor_text_by_nct[key].split(";") if sponsor_text_by_nct[key] else []
                if text not in existing:
                    existing.append(text)
                    sponsor_text_by_nct[key] = ";".join(existing)

            trial_links_by_company: dict[int, dict[str, list[Any]]] = {}
            dropped_link_count = 0
            for link in links:
                if link.company_id not in job_by_company_id:
                    dropped_link_count += 1
                    continue
                trial_links_by_company.setdefault(int(link.company_id), {}).setdefault(str(link.nct_id), []).append(
                    AUDIT_HELPERS.TrialLink(
                        nct_id=str(link.nct_id),
                        match_role=str(link.match_role),
                        match_method=str(link.match_method),
                        confidence=float(link.confidence),
                    )
                )
            if dropped_link_count:
                LOGGER.debug("Dropped CTGov reactivation links outside scan job set: count=%d", dropped_link_count)

            for job in jobs:
                company = job.audit_company
                result = scan_result_by_company_id.get(company.company_id)
                company_evidence: list[dict[str, Any]] = []
                company_trial_links = trial_links_by_company.get(company.company_id, {})
                for nct_id, trial_links in company_trial_links.items():
                    study = studies_by_nct.get(nct_id)
                    if not isinstance(study, dict):
                        continue
                    row = trial_row_from_study(study)
                    if not row:
                        continue
                    evidence = AUDIT_HELPERS.classify_trial(
                        company=company,
                        row=row,
                        links=trial_links,
                        study=study,
                        asof_date=asof_date,
                        active_statuses=active_statuses,
                        stale_days=stale_days,
                        completed_stale_days=completed_stale_days,
                        therapeutic_types=therapeutic_types,
                        non_therapeutic_types=non_therapeutic_types,
                        non_therapeutic_purposes=non_therapeutic_purposes,
                        qualifying_device_keywords=qualifying_device_keywords,
                        therapeutic_keywords=therapeutic_keywords,
                        non_therapeutic_keywords=non_therapeutic_keywords,
                    )
                    evidence = AUDIT_HELPERS.apply_trial_status_override(evidence, trial_status_overrides)
                    key = (company.company_id, nct_id)
                    hits = raw_query_hits_by_key.get(key, [])
                    evidence["sponsors"] = sponsor_text_by_nct.get(nct_id, "")
                    evidence["query_hit_search_terms"] = ";".join(
                        sorted({str(hit.search_term or "") for hit in hits if str(hit.search_term or "").strip()})
                    )
                    evidence["query_hit_fields"] = ";".join(sorted({str(hit.query_field or "") for hit in hits if hit.query_field}))
                    evidence["query_hit_sources"] = ";".join(sorted({str(hit.source or "") for hit in hits if hit.source}))
                    evidence["query_hit_max_confidence"] = (
                        round(max((float(hit.confidence or 0.0) for hit in hits), default=0.0), 4) if hits else ""
                    )
                    company_evidence.append(evidence)

                diagnostic_like = AUDIT_HELPERS.company_is_diagnostic_like(company, diagnostic_keywords)
                audit_row = AUDIT_HELPERS.recommend_company(
                    company=company,
                    evidence_rows=company_evidence,
                    aliases=list(job.all_aliases),
                    manual_aliases=list(job.manual_aliases),
                    diagnostic_like=diagnostic_like,
                    min_keep_score=min_keep_score,
                    low_confidence_threshold=low_confidence_threshold,
                )
                scan_error = str(result.error or "") if result is not None else ""
                reactivation_status, reactivation_priority, reactivation_reason = classify_reactivation(
                    audit_row,
                    scan_error=scan_error,
                    source_manual_verdict=job.source_manual_verdict,
                )
                (
                    reactivation_status,
                    reactivation_priority,
                    reactivation_reason,
                    policy_override_applied,
                    policy_override_notes,
                ) = apply_policy_override(
                    company.ticker,
                    reactivation_status=reactivation_status,
                    reactivation_priority=reactivation_priority,
                    reactivation_reason=reactivation_reason,
                    overrides=policy_overrides,
                )
                for evidence in company_evidence:
                    evidence["current_universe_status"] = company.universe_status
                    evidence["current_scoring_status"] = job.scoring_status
                    evidence["reactivation_status"] = reactivation_status
                    evidence["reactivation_priority"] = reactivation_priority
                    evidence["policy_override_applied"] = policy_override_applied
                    evidence["policy_override_notes"] = policy_override_notes
                    evidence_rows.append(evidence)

                scan_row = {
                    "ticker": company.ticker,
                    "company_name": company.company_name,
                    "current_universe_status": company.universe_status,
                    "current_scoring_status": job.scoring_status,
                    "reactivation_status": reactivation_status,
                    "reactivation_priority": reactivation_priority,
                    "reactivation_reason": reactivation_reason,
                    "policy_override_applied": policy_override_applied,
                    "policy_override_notes": policy_override_notes,
                    "recommended_status": audit_row.get("recommended_status", ""),
                    "review_bucket": audit_row.get("review_bucket", ""),
                    "root_cause_category": audit_row.get("root_cause_category", ""),
                    "recommended_fix": audit_row.get("recommended_fix", ""),
                    "review_reason": audit_row.get("review_reason", ""),
                    "audit_score": audit_row.get("audit_score", ""),
                    "active_positive_score": audit_row.get("active_positive_score", ""),
                    "risk_penalty": audit_row.get("risk_penalty", ""),
                    "primary_nct": audit_row.get("primary_nct", ""),
                    "primary_trial_title": audit_row.get("primary_trial_title", ""),
                    "primary_trial_score": audit_row.get("primary_trial_score", ""),
                    "days_since_last_update": audit_row.get("days_since_last_update", ""),
                    "is_pivotal": audit_row.get("is_pivotal", ""),
                    "pipeline_density": audit_row.get("pipeline_density", ""),
                    "total_linked_trials": audit_row.get("total_linked_trials", ""),
                    "active_trials": audit_row.get("active_trials", ""),
                    "qualifying_trial_count": audit_row.get("qualifying_trial_count", ""),
                    "qualifying_active_trial_count": audit_row.get("qualifying_active_trial_count", ""),
                    "verified_qualifying_active_trial_count": audit_row.get("verified_qualifying_active_trial_count", ""),
                    "active_any_lead_trials": audit_row.get("active_any_lead_trials", ""),
                    "active_any_program_trials": audit_row.get("active_any_program_trials", ""),
                    "weak_active_lead_or_program_trials": audit_row.get("weak_active_lead_or_program_trials", ""),
                    "active_lead_sponsor_trials": audit_row.get("active_lead_sponsor_trials", ""),
                    "active_collaborator_trials": audit_row.get("active_collaborator_trials", ""),
                    "active_program_override_trials": audit_row.get("active_program_override_trials", ""),
                    "active_nonqualifying_trials": audit_row.get("active_nonqualifying_trials", ""),
                    "active_nonqualifying_device_trials": audit_row.get("active_nonqualifying_device_trials", ""),
                    "active_nonqualifying_observational_trials": audit_row.get("active_nonqualifying_observational_trials", ""),
                    "active_weak_link_trials": audit_row.get("active_weak_link_trials", ""),
                    "active_stale_qualifying_trials": audit_row.get("active_stale_qualifying_trials", ""),
                    "phase2_3_active_trials": audit_row.get("phase2_3_active_trials", ""),
                    "non_drug_device_diagnostic_trials": audit_row.get("non_drug_device_diagnostic_trials", ""),
                    "completed_or_terminated_trials": audit_row.get("completed_or_terminated_trials", ""),
                    "low_confidence_links": audit_row.get("low_confidence_links", ""),
                    "weak_company_link_trials": audit_row.get("weak_company_link_trials", ""),
                    "stale_active_trials": audit_row.get("stale_active_trials", ""),
                    "company_diagnostic_like": audit_row.get("company_diagnostic_like", ""),
                    "alias_count": len(job.sync_job.aliases),
                    "manual_alias_count": len(job.manual_aliases),
                    "manual_aliases": ";".join(job.manual_aliases[:10]),
                    "search_count": len(job.sync_job.searches),
                    "search_terms": ";".join(search.search_term for search in job.sync_job.searches[:12]),
                    "query_hit_count": len(result.query_hits) if result is not None else 0,
                    "unique_study_count": len(result.studies) if result is not None else 0,
                    "scan_error": scan_error,
                    "sample_ncts": audit_row.get("sample_ncts", ""),
                    "source_reason_codes": audit_row.get("source_reason_codes", ""),
                }
                scan_rows.append(scan_row)
                if reactivation_status in {"reactivation_candidate", "reactivation_review", "scan_error"}:
                    review_rows.append(scan_row)

            scan_rows.sort(
                key=lambda row: (
                    {"reactivation_candidate": 0, "reactivation_review": 1, "scan_error": 2, "no_reactivation_signal": 3}.get(
                        str(row["reactivation_status"]),
                        9,
                    ),
                    {"high": 0, "medium": 1, "low": 2, "none": 3}.get(str(row["reactivation_priority"]), 9),
                    -to_float(row["audit_score"]),
                    str(row["ticker"]),
                )
            )
            review_rows.sort(
                key=lambda row: (
                    {"reactivation_candidate": 0, "reactivation_review": 1, "scan_error": 2}.get(
                        str(row["reactivation_status"]),
                        9,
                    ),
                    {"high": 0, "medium": 1, "low": 2, "none": 3}.get(str(row["reactivation_priority"]), 9),
                    -to_float(row["audit_score"]),
                    str(row["ticker"]),
                )
            )
            evidence_rows.sort(
                key=lambda row: (
                    {"reactivation_candidate": 0, "reactivation_review": 1, "scan_error": 2, "no_reactivation_signal": 3}.get(
                        str(row["reactivation_status"]),
                        9,
                    ),
                    str(row["ticker"]),
                    -to_float(row["trial_score"]),
                    str(row["nct_id"]),
                )
            )

            write_csv(scan_csv, scan_rows, SCAN_FIELDS)
            write_csv(review_csv, review_rows, SCAN_FIELDS)
            write_csv(evidence_csv, evidence_rows, EVIDENCE_FIELDS)

            error_count = sum(1 for row in scan_rows if str(row["scan_error"]).strip())
            manifest = {
                "generated_at_utc": utc_now(),
                "asof_date": asof_date.isoformat(),
                "db_path": str(db_path),
                "db_signature": AUDIT_HELPERS.db_signature(conn),
                "selection_mode": selection_mode,
                "status_filter": sorted(status_filter),
                "all_removed_mode": bool(args.all_removed),
                "status_filter_active": bool(args.all_removed),
                "status_filter_applied": bool(args.all_removed),
                "source_review_csv": "" if args.all_removed else str(source_review_csv),
                "source_review_csv_used": not bool(args.all_removed),
                "source_final_status_filter": sorted(source_final_status_filter),
                "ticker_filter": sorted(ticker_filter),
                "company_count": len(jobs),
                "scan_result_count": len(scan_rows),
                "review_count": len(review_rows),
                "evidence_row_count": len(evidence_rows),
                "unique_study_count": len(studies_by_nct),
                "query_hit_count": len(query_hit_rows),
                "program_owner_override_count": len(program_owner_overrides),
                "policy_override_count": len(policy_overrides),
                "link_count": len(links),
                "error_count": error_count,
                "reactivation_status_counts": dict(Counter(str(row["reactivation_status"]) for row in scan_rows)),
                "reactivation_priority_counts": dict(Counter(str(row["reactivation_priority"]) for row in scan_rows)),
                "review_bucket_counts": dict(Counter(str(row["review_bucket"]) for row in scan_rows if row["review_bucket"])),
                "root_cause_counts": dict(
                    Counter(str(row["root_cause_category"]) for row in scan_rows if row["root_cause_category"])
                ),
                "output_files": {
                    "scan_csv": str(scan_csv),
                    "review_csv": str(review_csv),
                    "evidence_csv": str(evidence_csv),
                    "manifest_json": str(manifest_json),
                },
            }
            write_json(manifest_json, manifest)

            message = (
                f"companies={len(jobs)} candidates={sum(1 for row in scan_rows if row['reactivation_status'] == 'reactivation_candidate')} "
                f"reviews={sum(1 for row in scan_rows if row['reactivation_status'] == 'reactivation_review')} "
                f"errors={error_count} studies={len(studies_by_nct)}"
            )
            status = "failed" if jobs and error_count == len(jobs) else "partial" if error_count > 0 else "success"
            finish_run(conn, run_id=run_id, status=status, row_count=len(scan_rows), message=message)
            LOGGER.info("CTGov reactivation scan complete: %s", message)
            if error_count > 0 and not args.allow_partial:
                raise SystemExit(2)
        except BaseException as exc:
            if run_id is not None and not (isinstance(exc, SystemExit) and exc.code in (0, None)):
                finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()
