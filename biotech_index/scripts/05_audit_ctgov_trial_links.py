#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter
import csv
import hashlib
import json
import logging
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.clients.ctgov_client import as_list, get_nested
from biotech_index.core.config import cfg_get, load_yaml, normalize_string_list, resolve_optional_path, resolve_path
from biotech_index.core.db import connect, finish_run, init_db, start_run, utc_now
from biotech_index.core.logging_utils import configure_utc_logging
from biotech_index.core.pipeline_guards import validate_nonempty_selection, validate_requested_tickers


LOGGER = logging.getLogger("audit_ctgov_trial_links")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
SQLITE_IN_CLAUSE_CHUNK_SIZE = 800
_RUN_CONTEXT: dict[str, Any] = {
    "db_path": None,
    "timeout_sec": 30.0,
    "run_id": None,
    "finished": False,
}
MANUAL_VERDICTS = {"manual_keep", "manual_remove", "manual_review"}
ROOT_CAUSE_CATEGORIES = {
    "entity_mapping_issue",
    "sponsor_alias_missing",
    "intervention_name_missing",
    "status_stale_on_ctgov",
    "weak_company_link_false_positive",
    "active_but_nonqualifying_observational",
    "active_but_nonqualifying_device",
    "collaborator_only_not_lead",
    "true_historical_only",
    "out_of_strategy_large_cap",
    "manual_remove_override",
    "mixed_review_case",
}
STRONG_LINK_METHODS = {"exact_norm", "suffix_stripped_exact", "ticker_token_match"}
STRONG_LINK_METHOD_PREFIXES = ("manual_search_term:", "program_owner_override:")


@dataclass(frozen=True)
class Company:
    company_id: int
    ticker: str
    company_name: str
    universe_status: str
    industry: str
    industry_aggregate: str
    reason_codes: str


@dataclass(frozen=True)
class TrialLink:
    nct_id: str
    match_role: str
    match_method: str
    confidence: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit CTGov trial links and build a locked clean biotech universe.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--tickers", type=str, default="", help="Optional comma-separated ticker subset.")
    parser.add_argument("--asof", type=str, default="", help="Audit date in YYYY-MM-DD. Defaults to today.")
    return parser.parse_args()


def configure_logging() -> None:
    configure_utc_logging()


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            if fmt == "%Y-%m":
                return datetime.strptime(f"{text}-01", "%Y-%m-%d").date()
            if fmt == "%Y":
                return datetime.strptime(f"{text}-01-01", "%Y-%m-%d").date()
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def days_since(raw: object, *, asof_date: date) -> int | None:
    parsed = parse_date(raw)
    if parsed is None:
        return None
    return (asof_date - parsed).days


def split_codes(raw: object) -> list[str]:
    return [piece.strip() for piece in str(raw or "").split(";") if piece.strip()]


def as_bool(raw: object) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "t", "yes", "y"}


def should_include_in_scoring(row: dict[str, Any]) -> bool:
    """Keep CTGov review as a warning for source-valid companies, not a hard universe drop."""
    final_status = str(row.get("final_status") or row.get("recommended_status") or "").strip().lower()
    universe_status = str(row.get("universe_status") or "").strip().lower()
    if final_status == "keep":
        return True
    if final_status == "review" and universe_status == "keep":
        return True
    return False


def contains_any(text: str, keywords: Iterable[str]) -> list[str]:
    haystack = text.lower()
    hits: list[str] = []
    for keyword in keywords:
        needle = str(keyword or "").strip().lower()
        if needle and needle in haystack:
            hits.append(needle)
    return hits


def is_strong_company_link_method(method: str) -> bool:
    method = str(method or "").strip()
    return method in STRONG_LINK_METHODS or any(method.startswith(prefix) for prefix in STRONG_LINK_METHOD_PREFIXES)


def phase_rank(phase_text: str) -> int:
    text = str(phase_text or "").upper()
    if "PHASE3" in text or "PHASE 3" in text:
        return 3
    if "PHASE2" in text or "PHASE 2" in text:
        return 2
    if "PHASE1" in text or "PHASE 1" in text or "EARLY_PHASE1" in text:
        return 1
    if "PHASE4" in text or "PHASE 4" in text:
        return 4
    return 0


def extract_trial_payload(raw_json: str) -> dict[str, Any]:
    if not raw_json:
        return {}
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def extract_interventions(study: dict[str, Any]) -> tuple[list[str], list[str]]:
    names: list[str] = []
    types: list[str] = []
    interventions = get_nested(study, ["protocolSection", "armsInterventionsModule", "interventions"], [])
    for item in as_list(interventions):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        raw_type = str(item.get("type") or "").strip().upper()
        if name:
            names.append(name)
        if raw_type:
            types.append(raw_type)
        for other in as_list(item.get("otherNames")):
            if str(other or "").strip():
                names.append(str(other).strip())
    return sorted(set(names)), sorted(set(types))


def load_trial_status_overrides(path: Path | None) -> list[dict[str, str]]:
    if path is None or not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            LOGGER.warning("Trial status override CSV has no header; ignoring file: %s", path)
            return []
        if not any(str(field or "").strip() for field in reader.fieldnames):
            raise ValueError(f"Trial status override CSV has an empty header row: {path}")
        return [{str(k): str(v or "") for k, v in row.items()} for row in reader]


def apply_trial_status_override(evidence: dict[str, Any], overrides: dict[tuple[str, str], dict[str, str]]) -> dict[str, Any]:
    ticker = str(evidence.get("ticker") or "").strip().upper()
    nct_id = str(evidence.get("nct_id") or "").strip().upper()
    override = overrides.get((ticker, nct_id))
    evidence["outcome_override_applied"] = False
    evidence["outcome_override_status"] = ""
    evidence["outcome_override_reason"] = ""
    evidence["outcome_override_source_url"] = ""
    evidence["outcome_override_manual_review"] = False
    if not override or not as_bool(override.get("enabled", "true")):
        return evidence

    status = str(override.get("override_status") or "").strip()
    reason = str(override.get("override_reason") or "").strip()
    source_url = str(override.get("source_url") or "").strip()
    evidence["outcome_override_applied"] = True
    evidence["outcome_override_status"] = status
    evidence["outcome_override_reason"] = reason
    evidence["outcome_override_source_url"] = source_url
    evidence["outcome_override_manual_review"] = as_bool(override.get("manual_review"))
    if as_bool(override.get("exclude_from_scoring")):
        evidence["is_active_status"] = False
        evidence["is_therapeutic"] = False
        evidence["qualifying_trial"] = False
        evidence["trial_score"] = 0.0
        suffix = f"outcome_override:{status}" if status else "outcome_override"
        reasons = split_codes(evidence.get("exclusion_reasons"))
        if suffix not in reasons:
            reasons.append(suffix)
        evidence["exclusion_reasons"] = ";".join(reasons)
    return evidence


def trial_text(study: dict[str, Any], row: sqlite3.Row, intervention_names: list[str]) -> str:
    parts: list[str] = [
        str(row["brief_title"] or ""),
        str(get_nested(study, ["protocolSection", "identificationModule", "officialTitle"], "") or ""),
        str(get_nested(study, ["protocolSection", "descriptionModule", "briefSummary"], "") or ""),
        str(get_nested(study, ["protocolSection", "descriptionModule", "detailedDescription"], "") or ""),
        str(get_nested(study, ["protocolSection", "designModule", "designInfo", "primaryPurpose"], "") or ""),
    ]
    parts.extend(str(x) for x in as_list(get_nested(study, ["protocolSection", "conditionsModule", "conditions"], [])))
    parts.extend(str(x) for x in as_list(get_nested(study, ["protocolSection", "conditionsModule", "keywords"], [])))
    parts.extend(intervention_names)
    return " ".join(part for part in parts if part).lower()


def load_companies(conn: sqlite3.Connection, *, status_filter: set[str], ticker_filter: set[str]) -> list[Company]:
    rows = conn.execute(
        """
        SELECT company_id, ticker, company_name, universe_status, industry, industry_aggregate, reason_codes
        FROM companies
        WHERE is_active = 1
        ORDER BY ticker
        """
    ).fetchall()
    companies: list[Company] = []
    for row in rows:
        ticker = str(row["ticker"] or "").upper()
        status = str(row["universe_status"] or "").lower()
        if status_filter and status not in status_filter:
            continue
        if ticker_filter and ticker not in ticker_filter:
            continue
        companies.append(
            Company(
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


def load_aliases(conn: sqlite3.Connection, company_id: int) -> tuple[list[str], list[str]]:
    rows = conn.execute(
        """
        SELECT alias_raw, alias_norm, source, is_manual
        FROM company_aliases
        WHERE company_id = ?
        ORDER BY is_manual DESC, confidence DESC, source ASC, LENGTH(alias_raw) DESC
        """,
        (company_id,),
    ).fetchall()
    all_aliases: list[str] = []
    manual_aliases: list[str] = []
    for row in rows:
        alias = str(row["alias_raw"] or "").strip()
        if not alias:
            continue
        all_aliases.append(alias)
        if int(row["is_manual"] or 0) == 1:
            manual_aliases.append(alias)
    return all_aliases, manual_aliases


def load_aliases_by_company(
    conn: sqlite3.Connection,
    company_ids: set[int] | None = None,
) -> dict[int, tuple[list[str], list[str]]]:
    def fetch(where: str = "", params: tuple[int, ...] = ()) -> list[sqlite3.Row]:
        return conn.execute(
            f"""
            SELECT company_id, alias_raw, alias_norm, source, is_manual
            FROM company_aliases
            {where}
            ORDER BY company_id, is_manual DESC, confidence DESC, source ASC, LENGTH(alias_raw) DESC
            """,
            params,
        ).fetchall()

    if company_ids is None:
        rows = fetch()
    elif not company_ids:
        rows = []
    else:
        rows = []
        sorted_ids = sorted(company_ids)
        for idx in range(0, len(sorted_ids), SQLITE_IN_CLAUSE_CHUNK_SIZE):
            chunk = tuple(sorted_ids[idx : idx + SQLITE_IN_CLAUSE_CHUNK_SIZE])
            placeholders = ",".join("?" for _ in chunk)
            rows.extend(fetch(f"WHERE company_id IN ({placeholders})", chunk))
    out: dict[int, tuple[list[str], list[str]]] = {}
    for row in rows:
        company_id = int(row["company_id"])
        alias = str(row["alias_raw"] or "").strip()
        if not alias:
            continue
        all_aliases, manual_aliases = out.setdefault(company_id, ([], []))
        all_aliases.append(alias)
        if int(row["is_manual"] or 0) == 1:
            manual_aliases.append(alias)
    return out


def load_trial_rows(conn: sqlite3.Connection, company_id: int, *, asof_date: date) -> dict[str, tuple[sqlite3.Row, list[TrialLink]]]:
    rows = conn.execute(
        """
        WITH latest_snapshot AS (
            SELECT s.*
            FROM trial_snapshot_daily s
            JOIN (
                SELECT nct_id, MAX(asof_date) AS max_asof
                FROM trial_snapshot_daily
                WHERE asof_date <= ?
                GROUP BY nct_id
            ) latest
              ON latest.nct_id = s.nct_id
             AND latest.max_asof = s.asof_date
        )
        SELECT
            t.nct_id, t.brief_title, t.study_type,
            COALESCE(s.phase_text, t.phase_text) AS phase_text,
            COALESCE(s.overall_status, t.overall_status) AS overall_status,
            t.lead_sponsor, t.last_update_post_date,
            COALESCE(s.has_results, t.has_results) AS has_results,
            s.primary_completion_date AS primary_completion_date,
            s.enrollment_count AS enrollment_count,
            '' AS raw_hash,
            t.raw_json,
            l.match_role, l.match_method, l.confidence
        FROM trial_company_links l
        JOIN trials t ON t.nct_id = l.nct_id
        LEFT JOIN latest_snapshot s ON s.nct_id = t.nct_id
        WHERE l.company_id = ?
        ORDER BY t.nct_id, l.confidence DESC
        """,
        (asof_date.isoformat(), company_id),
    ).fetchall()
    grouped: dict[str, tuple[sqlite3.Row, list[TrialLink]]] = {}
    for row in rows:
        nct_id = str(row["nct_id"] or "")
        link = TrialLink(
            nct_id=nct_id,
            match_role=str(row["match_role"] or ""),
            match_method=str(row["match_method"] or ""),
            confidence=float(row["confidence"] or 0.0),
        )
        if nct_id not in grouped:
            grouped[nct_id] = (row, [link])
        else:
            grouped[nct_id][1].append(link)
    return grouped


def load_sponsors(conn: sqlite3.Connection, nct_id: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT sponsor_name, sponsor_role
        FROM trial_sponsors
        WHERE nct_id = ?
        ORDER BY sponsor_role, sponsor_name
        """,
        (nct_id,),
    ).fetchall()
    return [f"{row['sponsor_name']} [{row['sponsor_role']}]" for row in rows]


def load_sponsors_by_nct(conn: sqlite3.Connection) -> dict[str, list[str]]:
    rows = conn.execute(
        """
        SELECT nct_id, sponsor_name, sponsor_role
        FROM trial_sponsors
        ORDER BY nct_id, sponsor_role, sponsor_name
        """
    ).fetchall()
    out: dict[str, list[str]] = {}
    for row in rows:
        nct_id = str(row["nct_id"] or "")
        if not nct_id:
            continue
        out.setdefault(nct_id, []).append(f"{row['sponsor_name']} [{row['sponsor_role']}]")
    return out


def company_is_diagnostic_like(company: Company, diagnostic_keywords: list[str]) -> bool:
    text = f"{company.company_name} {company.industry} {company.industry_aggregate}".lower()
    return bool(contains_any(text, diagnostic_keywords))


def classify_trial(
    *,
    company: Company,
    row: sqlite3.Row,
    links: list[TrialLink],
    study: dict[str, Any],
    asof_date: date,
    active_statuses: set[str],
    stale_days: int,
    completed_stale_days: int,
    therapeutic_types: set[str],
    non_therapeutic_types: set[str],
    non_therapeutic_purposes: set[str],
    qualifying_device_keywords: list[str],
    therapeutic_keywords: list[str],
    non_therapeutic_keywords: list[str],
) -> dict[str, Any]:
    intervention_names, intervention_types = extract_interventions(study)
    text = trial_text(study, row, intervention_names)
    purpose = str(get_nested(study, ["protocolSection", "designModule", "designInfo", "primaryPurpose"], "") or "").upper()
    status = str(row["overall_status"] or "").upper()
    phase_text = str(row["phase_text"] or "")
    rank = phase_rank(phase_text)
    roles = sorted({link.match_role for link in links if link.match_role})
    methods = sorted({link.match_method for link in links if link.match_method})
    max_conf = max((link.confidence for link in links), default=0.0)
    strong_company_link = any(is_strong_company_link_method(method) for method in methods)
    has_lead = "lead" in roles
    has_program = "program" in roles
    has_collab = "collaborator" in roles
    is_active = status in active_statuses
    last_update_days = days_since(row["last_update_post_date"], asof_date=asof_date)
    completed_stale = status == "COMPLETED" and last_update_days is not None and last_update_days > completed_stale_days
    active_stale = is_active and last_update_days is not None and last_update_days > stale_days
    is_interventional = str(row["study_type"] or "").upper() == "INTERVENTIONAL"
    pivotal = rank == 3 or bool(contains_any(text, ["pivotal", "registrational", "phase 2/3", "phase ii/iii"]))
    therapeutic_type_hit = bool(set(intervention_types) & therapeutic_types)
    device_type_hit = "DEVICE" in set(intervention_types)
    therapeutic_hits = contains_any(text, therapeutic_keywords)
    non_therapeutic_hits = contains_any(text, non_therapeutic_keywords)
    qualifying_device_hits = contains_any(text, qualifying_device_keywords)
    qualifying_device_role = has_lead or has_program
    therapeutic_device_purpose = purpose in {"TREATMENT", "PREVENTION", "SUPPORTIVE_CARE"}
    narrow_non_therapeutic_device_purpose = purpose in {"DIAGNOSTIC", "SCREENING", "DEVICE_FEASIBILITY"}
    qualifying_device = (
        is_interventional
        and device_type_hit
        and not therapeutic_type_hit
        and (
            (therapeutic_device_purpose and (bool(qualifying_device_hits) or pivotal or rank >= 2))
            or (narrow_non_therapeutic_device_purpose and qualifying_device_role and bool(qualifying_device_hits))
        )
    )
    non_therapeutic_type_only = (
        bool(intervention_types)
        and not therapeutic_type_hit
        and bool(set(intervention_types) & non_therapeutic_types)
        and not qualifying_device
    )
    non_therapeutic_purpose = purpose in non_therapeutic_purposes
    hard_non_therapeutic = (
        non_therapeutic_type_only
        or (non_therapeutic_purpose and not therapeutic_type_hit and not qualifying_device)
        or (bool(non_therapeutic_hits) and not therapeutic_type_hit and rank == 0 and not qualifying_device)
    )
    therapeutic = (
        is_interventional
        and not hard_non_therapeutic
        and (
            therapeutic_type_hit
            or qualifying_device
            or bool(therapeutic_hits)
            or (rank > 0 and purpose in {"TREATMENT", "PREVENTION"})
        )
    )

    score = 0.0
    if not is_interventional:
        score -= 10.0
    if hard_non_therapeutic:
        score -= 8.0
    if is_active and therapeutic:
        if has_lead:
            score += {3: 10.0, 2: 8.0, 1: 5.0, 4: 3.0}.get(rank, 3.0)
        elif has_program:
            score += {3: 9.0, 2: 7.0, 1: 5.0, 4: 3.0}.get(rank, 3.0)
        elif has_collab:
            score += {3: 2.0, 2: 1.0, 1: 0.5, 4: 0.5}.get(rank, 0.5)
        if status in {"RECRUITING", "NOT_YET_RECRUITING"}:
            score += 1.0
    if status in {"TERMINATED", "WITHDRAWN", "SUSPENDED"}:
        score -= 3.0
    if completed_stale:
        score -= 2.0
    if active_stale:
        score -= 1.0

    exclusion_reasons: list[str] = []
    if hard_non_therapeutic:
        exclusion_reasons.append("non_therapeutic")
    if non_therapeutic_type_only:
        exclusion_reasons.append("non_therapeutic_intervention_type")
    if non_therapeutic_purpose and not therapeutic_type_hit and not qualifying_device:
        exclusion_reasons.append(f"primary_purpose:{purpose}")
    if non_therapeutic_hits and hard_non_therapeutic:
        exclusion_reasons.append("keyword:" + "|".join(non_therapeutic_hits[:3]))
    if completed_stale:
        exclusion_reasons.append("completed_stale")
    if active_stale:
        exclusion_reasons.append("active_stale")
    if not strong_company_link:
        exclusion_reasons.append("weak_company_link")

    return {
        "ticker": company.ticker,
        "company_name": company.company_name,
        "nct_id": str(row["nct_id"] or ""),
        "brief_title": str(row["brief_title"] or ""),
        "overall_status": status,
        "phase_text": phase_text,
        "phase_rank": rank,
        "study_type": str(row["study_type"] or ""),
        "primary_purpose": purpose,
        "match_roles": ";".join(roles),
        "match_methods": ";".join(methods),
        "strong_company_link": strong_company_link,
        "max_confidence": round(max_conf, 4),
        "is_active_status": is_active,
        "is_pivotal": pivotal,
        "is_qualifying_device": qualifying_device,
        "is_therapeutic": therapeutic,
        "is_non_therapeutic": hard_non_therapeutic,
        "qualifying_trial": therapeutic,
        "trial_score": round(score, 4),
        "days_since_last_update": last_update_days if last_update_days is not None else "",
        "last_update_post_date": str(row["last_update_post_date"] or ""),
        "primary_completion_date": str(row["primary_completion_date"] or ""),
        "intervention_types": ";".join(intervention_types),
        "intervention_names": ";".join(intervention_names[:12]),
        "therapeutic_keyword_hits": "|".join(therapeutic_hits[:8]),
        "non_therapeutic_keyword_hits": "|".join(non_therapeutic_hits[:8]),
        "qualifying_device_keyword_hits": "|".join(qualifying_device_hits[:8]),
        "exclusion_reasons": ";".join(exclusion_reasons),
    }


def recommend_company(
    *,
    company: Company,
    evidence_rows: list[dict[str, Any]],
    aliases: list[str],
    manual_aliases: list[str],
    diagnostic_like: bool,
    min_keep_score: float,
    low_confidence_threshold: float,
) -> dict[str, Any]:
    total = len(evidence_rows)
    active = [row for row in evidence_rows if row["is_active_status"]]
    qualifying = [row for row in evidence_rows if row["qualifying_trial"]]
    qualifying_active = [row for row in qualifying if row["is_active_status"]]
    strong_qualifying = [row for row in qualifying if row["strong_company_link"]]
    strong_qualifying_active = [row for row in qualifying_active if row["strong_company_link"]]
    qualifying_active_phase23 = [row for row in strong_qualifying_active if int(row["phase_rank"] or 0) in {2, 3}]
    qualifying_active_any_lead = [row for row in qualifying_active if "lead" in str(row["match_roles"]).split(";")]
    qualifying_active_any_program = [row for row in qualifying_active if "program" in str(row["match_roles"]).split(";")]
    weak_qualifying_active_lead_program = [
        row
        for row in qualifying_active
        if not row["strong_company_link"]
        and ("lead" in str(row["match_roles"]).split(";") or "program" in str(row["match_roles"]).split(";"))
    ]
    qualifying_active_lead = [row for row in strong_qualifying_active if "lead" in str(row["match_roles"]).split(";")]
    qualifying_active_program = [row for row in strong_qualifying_active if "program" in str(row["match_roles"]).split(";")]
    qualifying_active_collab = [row for row in strong_qualifying_active if "collaborator" in str(row["match_roles"]).split(";")]
    active_nonqualifying = [row for row in active if not row["qualifying_trial"]]
    active_weak_link_only = [row for row in qualifying_active if not row["strong_company_link"]]
    non_therapeutic = [row for row in evidence_rows if row["is_non_therapeutic"]]
    low_conf = [row for row in evidence_rows if float(row["max_confidence"] or 0.0) < low_confidence_threshold]
    stale = [row for row in evidence_rows if "active_stale" in str(row["exclusion_reasons"])]
    stale_qualifying_active = [row for row in qualifying_active if "active_stale" in str(row["exclusion_reasons"])]
    weak_company_links = [row for row in evidence_rows if not row["strong_company_link"]]
    active_nonqualifying_device = [
        row
        for row in active_nonqualifying
        if "DEVICE" in set(split_codes(row["intervention_types"])) or bool(row["is_qualifying_device"])
    ]
    active_nonqualifying_observational = [row for row in active_nonqualifying if row not in active_nonqualifying_device]
    post_market_device_only = bool(strong_qualifying_active) and all(
        bool(row["is_qualifying_device"]) and int(row["phase_rank"] or 0) >= 4 for row in strong_qualifying_active
    )
    completed_or_terminal = [
        row
        for row in evidence_rows
        if str(row["overall_status"]) in {"COMPLETED", "TERMINATED", "WITHDRAWN", "SUSPENDED"}
    ]
    positive_active_score = round(
        sum(max(float(row["trial_score"] or 0.0), 0.0) for row in strong_qualifying_active),
        4,
    )
    risk_penalty = round(
        min(
            10.0,
            (len(non_therapeutic) * 0.50)
            + (len(completed_or_terminal) * 0.25)
            + (len(stale) * 0.50)
            + (len(low_conf) * 0.50),
        ),
        4,
    )
    score = round(positive_active_score - risk_penalty, 4)
    primary = sorted(
        evidence_rows,
        key=lambda row: (
            float(row["trial_score"] or 0.0),
            1 if row["is_active_status"] else 0,
            int(row["phase_rank"] or 0),
            str(row["last_update_post_date"] or ""),
        ),
        reverse=True,
    )
    primary_row = primary[0] if primary else {}
    reasons: list[str] = []
    if total == 0:
        reasons.append("no_linked_trials")
    if diagnostic_like and not (qualifying_active_lead or qualifying_active_program):
        reasons.append("diagnostic_or_platform_only_without_lead_program")
    if total > 0 and len(non_therapeutic) == total:
        reasons.append("all_linked_trials_non_therapeutic")
    if qualifying and not qualifying_active:
        reasons.append("qualifying_trials_historical_only")
    if active and not strong_qualifying_active:
        reasons.append("active_links_not_qualifying_therapeutic")
    if qualifying_active and not strong_qualifying_active:
        reasons.append("weak_company_link_only")
    if strong_qualifying_active and not (qualifying_active_lead or qualifying_active_program):
        reasons.append("qualifying_active_trials_collaborator_only")
    if post_market_device_only:
        reasons.append("post_market_device_only")
    if low_conf:
        reasons.append("low_confidence_links")
    if stale:
        reasons.append("stale_active_trial_updates")
    if not qualifying:
        reasons.append("no_qualifying_therapeutic_trials")

    keep_signal = strong_qualifying_active and positive_active_score >= min_keep_score
    if keep_signal and not (
        diagnostic_like and not (qualifying_active_lead or qualifying_active_program)
    ) and not (
        strong_qualifying_active and not (qualifying_active_lead or qualifying_active_program)
    ) and not (
        post_market_device_only
    ):
        recommended = "keep"
    elif not strong_qualifying or (total > 0 and len(non_therapeutic) == total):
        recommended = "remove_candidate"
    else:
        recommended = "review"

    pipeline_density: float | None = None
    if active:
        pipeline_density = round(len(qualifying_active_phase23) / len(active), 4)

    review_bucket = "keep_candidate"
    root_cause_category = ""
    recommended_fix = "none"
    all_weak_links = total > 0 and len(weak_company_links) == total

    if total == 0:
        review_bucket = "no_linked_trials"
        root_cause_category = "entity_mapping_issue"
        recommended_fix = "add_company_alias_override_or_ctgov_search_override"
    elif all_weak_links and not strong_qualifying_active:
        review_bucket = "weak_company_link_false_positive"
        root_cause_category = "weak_company_link_false_positive"
        recommended_fix = "manual_remove_or_ignore_generic_match"
    elif weak_qualifying_active_lead_program:
        review_bucket = "lead_rows_exist_but_weak_alias_match"
        root_cause_category = "sponsor_alias_missing"
        recommended_fix = "add_company_alias_override_or_ctgov_search_override"
    elif strong_qualifying_active and not (qualifying_active_lead or qualifying_active_program):
        review_bucket = "collaborator_only_active"
        root_cause_category = "collaborator_only_not_lead"
        recommended_fix = "manual_review_collaborator_scope"
    elif qualifying_active and not strong_qualifying_active:
        review_bucket = "active_study_missing_from_match"
        root_cause_category = "sponsor_alias_missing"
        recommended_fix = "add_company_alias_override_or_ctgov_search_override"
    elif stale and active and not strong_qualifying_active:
        review_bucket = "stale_active_record_only"
        root_cause_category = "status_stale_on_ctgov"
        recommended_fix = "verify_ctgov_status_or_add_trial_status_override"
    elif active and not qualifying_active:
        review_bucket = "active_study_exists_but_not_qualifying"
        if active_nonqualifying_device or (diagnostic_like and not active_nonqualifying_observational):
            root_cause_category = "active_but_nonqualifying_device"
            recommended_fix = "manual_review_device_scope_or_manual_keep"
        else:
            root_cause_category = "active_but_nonqualifying_observational"
            recommended_fix = "manual_review_non_therapeutic_scope"
    elif qualifying and not qualifying_active:
        review_bucket = "true_historical_only"
        root_cause_category = "true_historical_only"
        recommended_fix = "none_true_historical_only"
    elif post_market_device_only:
        review_bucket = "post_market_device_only"
        root_cause_category = "active_but_nonqualifying_device"
        recommended_fix = "manual_review_device_scope_or_manual_keep"
    elif low_conf and not strong_qualifying_active:
        review_bucket = "active_study_missing_from_match"
        root_cause_category = "sponsor_alias_missing"
        recommended_fix = "add_company_alias_override_or_ctgov_search_override"
    elif recommended != "keep":
        review_bucket = "mixed_review_case"
        root_cause_category = "mixed_review_case"
        recommended_fix = "manual_review"

    return {
        "ticker": company.ticker,
        "company_name": company.company_name,
        "universe_status": company.universe_status,
        "recommended_status": recommended,
        "review_reason": ";".join(dict.fromkeys(reasons)),
        "review_bucket": review_bucket,
        "root_cause_category": root_cause_category,
        "recommended_fix": recommended_fix,
        "audit_score": score,
        "active_positive_score": positive_active_score,
        "risk_penalty": risk_penalty,
        "primary_nct": primary_row.get("nct_id", ""),
        "primary_trial_title": primary_row.get("brief_title", ""),
        "primary_trial_score": primary_row.get("trial_score", ""),
        "days_since_last_update": primary_row.get("days_since_last_update", ""),
        "is_pivotal": bool(primary_row.get("is_pivotal", False)),
        "pipeline_density": pipeline_density,
        "total_linked_trials": total,
        "active_trials": len(active),
        "qualifying_trial_count": len(qualifying),
        "qualifying_active_trial_count": len(qualifying_active),
        "verified_qualifying_active_trial_count": len(strong_qualifying_active),
        "active_any_lead_trials": len(qualifying_active_any_lead),
        "active_any_program_trials": len(qualifying_active_any_program),
        "weak_active_lead_or_program_trials": len(weak_qualifying_active_lead_program),
        "active_lead_sponsor_trials": len(qualifying_active_lead),
        "active_collaborator_trials": len(qualifying_active_collab),
        "active_program_override_trials": len(qualifying_active_program),
        "active_nonqualifying_trials": len(active_nonqualifying),
        "active_nonqualifying_device_trials": len(active_nonqualifying_device),
        "active_nonqualifying_observational_trials": len(active_nonqualifying_observational),
        "active_weak_link_trials": len(active_weak_link_only),
        "active_stale_qualifying_trials": len(stale_qualifying_active),
        "phase2_3_active_trials": len(qualifying_active_phase23),
        "non_drug_device_diagnostic_trials": len(non_therapeutic),
        "completed_or_terminated_trials": len(completed_or_terminal),
        "low_confidence_links": len(low_conf),
        "weak_company_link_trials": len(weak_company_links),
        "stale_active_trials": len(stale),
        "company_diagnostic_like": diagnostic_like,
        "manual_alias_count": len(manual_aliases),
        "manual_aliases": ";".join(manual_aliases[:10]),
        "sample_ncts": ";".join(str(row["nct_id"]) for row in primary[:12]),
        "source_reason_codes": company.reason_codes,
    }


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True), encoding="utf-8")


def mark_current_run_failed(exc: BaseException, conn: Any | None = None) -> None:
    run_id = _RUN_CONTEXT.get("run_id")
    db_path = _RUN_CONTEXT.get("db_path")
    if not run_id or not db_path or _RUN_CONTEXT.get("finished"):
        return
    try:
        if conn is not None:
            finish_run(
                conn,
                run_id=int(run_id),
                status="failed",
                row_count=0,
                message=f"{type(exc).__name__}: {exc}",
            )
        else:
            with connect(Path(str(db_path)), timeout_sec=float(_RUN_CONTEXT.get("timeout_sec") or 30.0)) as failure_conn:
                finish_run(
                    failure_conn,
                    run_id=int(run_id),
                    status="failed",
                    row_count=0,
                    message=f"{type(exc).__name__}: {exc}",
                )
        _RUN_CONTEXT["finished"] = True
    except BaseException:
        LOGGER.exception("Could not mark audit run %s as failed", run_id)


def write_output_with_run_failure(writer: Any, *args: Any, **kwargs: Any) -> None:
    try:
        writer(*args, **kwargs)
    except Exception as exc:
        mark_current_run_failed(exc)
        raise


MANUAL_VERIFICATION_FIELDS = [
    "ticker",
    "company_name",
    "recommended_status",
    "review_bucket",
    "root_cause_category",
    "recommended_fix",
    "review_reason",
    "audit_score",
    "primary_nct",
    "primary_trial_title",
    "verified_qualifying_active_trial_count",
    "qualifying_active_trial_count",
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
    "weak_company_link_trials",
    "phase2_3_active_trials",
    "total_linked_trials",
    "active_trials",
    "sample_ncts",
    "source_reason_codes",
    "manual_root_cause",
    "manual_verified_active_study",
    "manual_verdict",
    "manual_verified_nct",
    "manual_verified_status",
    "manual_verified_phase",
    "manual_verified_study_type",
    "manual_lead_vs_collab",
    "manual_notes",
    "manual_reviewer",
    "manual_verified_date",
]


def load_manual_decisions(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            return {}
        out: dict[str, dict[str, str]] = {}
        for row in reader:
            ticker = str(row.get("ticker") or "").strip().upper().replace(".", "-")
            if not ticker:
                continue
            out[ticker] = {str(k): str(v or "") for k, v in row.items()}
        return out


def build_manual_verification_rows(
    rows: list[dict[str, Any]],
    manual_decisions: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    verification_rows: list[dict[str, Any]] = []
    seen_tickers: set[str] = set()
    for row in rows:
        ticker = str(row.get("ticker") or "").strip().upper().replace(".", "-")
        if not ticker:
            continue
        seen_tickers.add(ticker)
        manual = manual_decisions.get(ticker, {})
        manual_verdict = str(manual.get("manual_verdict") or row.get("manual_verdict") or "").strip().lower()
        final_status = str(row.get("final_status") or row.get("recommended_status") or "").strip().lower()
        if final_status == "keep" and manual_verdict not in MANUAL_VERDICTS:
            continue
        out = {
            "ticker": row.get("ticker", ""),
            "company_name": row.get("company_name", ""),
            "recommended_status": row.get("recommended_status", ""),
            "review_bucket": row.get("review_bucket", ""),
            "root_cause_category": row.get("root_cause_category", ""),
            "recommended_fix": row.get("recommended_fix", ""),
            "review_reason": row.get("review_reason", ""),
            "audit_score": row.get("audit_score", ""),
            "primary_nct": row.get("primary_nct", ""),
            "primary_trial_title": row.get("primary_trial_title", ""),
            "verified_qualifying_active_trial_count": row.get("verified_qualifying_active_trial_count", ""),
            "qualifying_active_trial_count": row.get("qualifying_active_trial_count", ""),
            "active_any_lead_trials": row.get("active_any_lead_trials", ""),
            "active_any_program_trials": row.get("active_any_program_trials", ""),
            "weak_active_lead_or_program_trials": row.get("weak_active_lead_or_program_trials", ""),
            "active_lead_sponsor_trials": row.get("active_lead_sponsor_trials", ""),
            "active_collaborator_trials": row.get("active_collaborator_trials", ""),
            "active_program_override_trials": row.get("active_program_override_trials", ""),
            "active_nonqualifying_trials": row.get("active_nonqualifying_trials", ""),
            "active_nonqualifying_device_trials": row.get("active_nonqualifying_device_trials", ""),
            "active_nonqualifying_observational_trials": row.get("active_nonqualifying_observational_trials", ""),
            "active_weak_link_trials": row.get("active_weak_link_trials", ""),
            "active_stale_qualifying_trials": row.get("active_stale_qualifying_trials", ""),
            "weak_company_link_trials": row.get("weak_company_link_trials", ""),
            "phase2_3_active_trials": row.get("phase2_3_active_trials", ""),
            "total_linked_trials": row.get("total_linked_trials", ""),
            "active_trials": row.get("active_trials", ""),
            "sample_ncts": row.get("sample_ncts", ""),
            "source_reason_codes": row.get("source_reason_codes", ""),
            "manual_root_cause": manual.get("manual_root_cause", ""),
            "manual_verified_active_study": manual.get("manual_verified_active_study", ""),
            "manual_verdict": manual.get("manual_verdict", ""),
            "manual_verified_nct": manual.get("manual_verified_nct", ""),
            "manual_verified_status": manual.get("manual_verified_status", ""),
            "manual_verified_phase": manual.get("manual_verified_phase", ""),
            "manual_verified_study_type": manual.get("manual_verified_study_type", ""),
            "manual_lead_vs_collab": manual.get("manual_lead_vs_collab", ""),
            "manual_notes": manual.get("manual_notes", ""),
            "manual_reviewer": manual.get("manual_reviewer", ""),
            "manual_verified_date": manual.get("manual_verified_date", ""),
        }
        verification_rows.append(out)
    for ticker, manual in sorted(manual_decisions.items()):
        if ticker in seen_tickers:
            continue
        manual_verdict = str(manual.get("manual_verdict") or "").strip().lower()
        if manual_verdict not in MANUAL_VERDICTS:
            continue
        verification_rows.append({field: manual.get(field, "") for field in MANUAL_VERIFICATION_FIELDS})
    verification_rows.sort(key=lambda item: (str(item["recommended_status"]), str(item["ticker"])))
    return verification_rows


def apply_manual_decisions(rows: list[dict[str, Any]], manual_decisions: dict[str, dict[str, str]]) -> None:
    for row in rows:
        ticker = str(row.get("ticker") or "").strip().upper().replace(".", "-")
        manual = manual_decisions.get(ticker, {})
        verdict = str(manual.get("manual_verdict") or "").strip().lower()
        final_status = str(row.get("recommended_status") or "")
        final_reason = str(row.get("review_reason") or "")
        root_cause_category = str(row.get("root_cause_category") or "")
        manual_applied = False
        if verdict == "manual_keep":
            final_status = "keep"
            final_reason = "manual_keep"
            manual_applied = True
        elif verdict == "manual_remove":
            final_status = "remove"
            final_reason = "manual_remove"
            manual_applied = True
        elif verdict == "manual_review":
            final_status = "review"
            final_reason = "manual_review"
            manual_applied = True
        manual_root_cause = str(manual.get("manual_root_cause") or "").strip()
        if manual_root_cause in ROOT_CAUSE_CATEGORIES:
            root_cause_category = manual_root_cause

        row["manual_verdict"] = verdict
        row["manual_override_applied"] = manual_applied
        row["manual_root_cause"] = manual_root_cause
        row["manual_verified_active_study"] = manual.get("manual_verified_active_study", "")
        row["manual_verified_nct"] = manual.get("manual_verified_nct", "")
        row["manual_verified_status"] = manual.get("manual_verified_status", "")
        row["manual_verified_phase"] = manual.get("manual_verified_phase", "")
        row["manual_verified_study_type"] = manual.get("manual_verified_study_type", "")
        row["manual_lead_vs_collab"] = manual.get("manual_lead_vs_collab", "")
        row["manual_notes"] = manual.get("manual_notes", "")
        row["manual_reviewer"] = manual.get("manual_reviewer", "")
        row["manual_verified_date"] = manual.get("manual_verified_date", "")
        row["root_cause_category"] = root_cause_category
        row["final_status"] = final_status
        row["final_status_reason"] = final_reason
        row["scoring_include"] = should_include_in_scoring(row)


def db_signature(conn: sqlite3.Connection) -> str:
    pieces: list[str] = []
    for table in ("companies", "trials", "trial_company_links", "ctgov_query_hits"):
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        pieces.append(f"{table}:{count}")
        columns = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}
        for timestamp_col in ("updated_at", "created_at"):
            if timestamp_col in columns:
                latest = conn.execute(f"SELECT MAX({timestamp_col}) FROM {table}").fetchone()[0]
                pieces.append(f"{table}.{timestamp_col}:{latest or ''}")
                break
        if table == "companies":
            active = conn.execute("SELECT COUNT(*) FROM companies WHERE is_active = 1").fetchone()[0]
            pieces.append(f"{table}.active:{active}")
    return hashlib.sha1("|".join(pieces).encode("utf-8")).hexdigest()


def main() -> None:
    configure_logging()
    _RUN_CONTEXT.clear()
    _RUN_CONTEXT.update({"db_path": None, "timeout_sec": 30.0, "run_id": None, "finished": False})
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else resolve_path(cfg_get(config, "ctgov_audit.output_dir", "../output/biotech_index_reports"), base_dir=base_dir)
    )
    asof_date = parse_date(args.asof) if args.asof else datetime.now(timezone.utc).date()
    if asof_date is None:
        raise ValueError(f"Invalid --asof date: {args.asof}")
    status_filter = {
        value.lower()
        for value in normalize_string_list(cfg_get(config, "ctgov_audit.status_filter"), ["keep", "review"])
    }
    ticker_filter = {value.strip().upper().replace(".", "-") for value in args.tickers.split(",") if value.strip()}
    if ticker_filter and args.output_dir is None:
        raise ValueError(
            "--tickers is a subset/smoke-test mode and must be paired with --output-dir so canonical CTGov outputs are not overwritten."
        )
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
    therapeutic_types = {value.upper() for value in normalize_string_list(cfg_get(config, "ctgov_audit.therapeutic_intervention_types"), [])}
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
    trial_status_overrides_csv = resolve_optional_path(cfg_get(config, "ctgov_audit.trial_status_overrides_csv"), base_dir=base_dir)
    sqlite_timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))
    trial_status_overrides = {
        (str(row.get("ticker") or "").strip().upper(), str(row.get("nct_id") or "").strip().upper()): row
        for row in load_trial_status_overrides(trial_status_overrides_csv)
        if str(row.get("ticker") or "").strip() and str(row.get("nct_id") or "").strip()
    }

    audit_rows: list[dict[str, Any]] = []
    review_rows: list[dict[str, Any]] = []
    evidence_rows: list[dict[str, Any]] = []
    clean_rows: list[dict[str, Any]] = []
    run_id: int | None = None

    with connect(db_path, timeout_sec=sqlite_timeout_sec) as conn:
        init_db(conn)
        run_id = start_run(conn, run_type="audit_ctgov_trial_links", input_path=db_path)
        _RUN_CONTEXT.update({"db_path": db_path, "timeout_sec": sqlite_timeout_sec, "run_id": run_id, "finished": False})
        companies = load_companies(conn, status_filter=status_filter, ticker_filter=ticker_filter)
        validate_nonempty_selection(
            count=len(companies),
            context="CTGov audit",
            subset_mode=bool(ticker_filter),
        )
        validate_requested_tickers(
            requested_tickers=ticker_filter,
            loaded_tickers=[company.ticker for company in companies],
            context="CTGov audit",
        )
        company_ids = {company.company_id for company in companies}
        aliases_by_company = load_aliases_by_company(conn, company_ids=company_ids)
        sponsors_by_nct = load_sponsors_by_nct(conn)
        LOGGER.info("Loaded %d active companies for CTGov audit", len(companies))
        for idx, company in enumerate(companies, start=1):
            aliases, manual_aliases = aliases_by_company.get(company.company_id, ([], []))
            trial_rows = load_trial_rows(conn, company.company_id, asof_date=asof_date)
            company_evidence: list[dict[str, Any]] = []
            for nct_id, (row, links) in trial_rows.items():
                study = extract_trial_payload(str(row["raw_json"] or ""))
                evidence = classify_trial(
                    company=company,
                    row=row,
                    links=links,
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
                evidence = apply_trial_status_override(evidence, trial_status_overrides)
                evidence["sponsors"] = ";".join(sponsors_by_nct.get(nct_id, [])[:12])
                company_evidence.append(evidence)
                evidence_rows.append(evidence)
            diagnostic_like = company_is_diagnostic_like(company, diagnostic_keywords)
            audit = recommend_company(
                company=company,
                evidence_rows=company_evidence,
                aliases=aliases,
                manual_aliases=manual_aliases,
                diagnostic_like=diagnostic_like,
                min_keep_score=min_keep_score,
                low_confidence_threshold=low_confidence_threshold,
            )
            audit_rows.append(audit)
            if idx % 50 == 0:
                LOGGER.info("Audited %d/%d companies", idx, len(companies))
        signature = db_signature(conn)

    audit_fields = [
        "ticker",
        "company_name",
        "universe_status",
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
        "manual_alias_count",
        "manual_aliases",
        "sample_ncts",
        "source_reason_codes",
        "manual_root_cause",
        "manual_verified_active_study",
        "manual_verdict",
        "manual_override_applied",
        "manual_verified_nct",
        "manual_verified_status",
        "manual_verified_phase",
        "manual_verified_study_type",
        "manual_lead_vs_collab",
        "manual_notes",
        "manual_reviewer",
        "manual_verified_date",
        "final_status",
        "final_status_reason",
        "scoring_include",
    ]
    evidence_fields = [
        "ticker",
        "company_name",
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
        "outcome_override_applied",
        "outcome_override_status",
        "outcome_override_reason",
        "outcome_override_source_url",
        "outcome_override_manual_review",
        "sponsors",
    ]

    audit_csv = output_dir / str(cfg_get(config, "ctgov_audit.audit_csv", "ctgov_trial_link_audit.csv"))
    review_csv = output_dir / str(cfg_get(config, "ctgov_audit.review_csv", "ctgov_trial_link_review.csv"))
    manual_verification_csv = output_dir / str(
        cfg_get(config, "ctgov_audit.manual_verification_csv", "ctgov_manual_verification_queue.csv")
    )
    final_scoring_universe_csv = output_dir / str(
        cfg_get(config, "ctgov_audit.final_scoring_universe_csv", "ctgov_final_scoring_universe.csv")
    )
    evidence_csv = output_dir / str(cfg_get(config, "ctgov_audit.evidence_csv", "ctgov_trial_evidence.csv"))
    clean_csv = output_dir / str(cfg_get(config, "ctgov_audit.locked_clean_universe_csv", "ctgov_clean_universe_locked.csv"))
    clean_json = output_dir / str(cfg_get(config, "ctgov_audit.locked_clean_universe_json", "ctgov_clean_universe_locked.json"))
    manifest_json = output_dir / str(cfg_get(config, "ctgov_audit.manifest_json", "ctgov_audit_manifest.json"))
    manual_decisions = load_manual_decisions(manual_verification_csv)
    manual_verdict_count = sum(
        1
        for manual in manual_decisions.values()
        if str(manual.get("manual_verdict") or "").strip().lower() in MANUAL_VERDICTS
    )
    apply_manual_decisions(audit_rows, manual_decisions)
    manual_verification_rows = build_manual_verification_rows(audit_rows, manual_decisions)
    final_scoring_rows = [row for row in audit_rows if bool(row.get("scoring_include"))]
    clean_rows = [row for row in audit_rows if str(row.get("final_status") or "").lower() == "keep"]
    review_rows = [row for row in audit_rows if str(row.get("final_status") or "").lower() != "keep"]

    audit_rows.sort(key=lambda row: (str(row["final_status"]), str(row["recommended_status"]), str(row["ticker"])))
    review_rows.sort(key=lambda row: (str(row["final_status"]), str(row["recommended_status"]), str(row["ticker"])))
    clean_rows.sort(key=lambda row: str(row["ticker"]))
    final_scoring_rows.sort(key=lambda row: str(row["ticker"]))
    evidence_rows.sort(key=lambda row: (str(row["ticker"]), str(row["nct_id"])))

    write_output_with_run_failure(write_csv, audit_csv, audit_rows, audit_fields)
    write_output_with_run_failure(write_csv, review_csv, review_rows, audit_fields)
    write_output_with_run_failure(write_csv, manual_verification_csv, manual_verification_rows, MANUAL_VERIFICATION_FIELDS)
    write_output_with_run_failure(write_csv, evidence_csv, evidence_rows, evidence_fields)
    write_output_with_run_failure(write_csv, clean_csv, clean_rows, audit_fields)
    write_output_with_run_failure(write_csv, final_scoring_universe_csv, final_scoring_rows, audit_fields)
    write_output_with_run_failure(write_json, clean_json, clean_rows)
    manifest = {
        "created_at": utc_now(),
        "asof_date": asof_date.isoformat(),
        "db_path": str(db_path),
        "db_signature": signature,
        "company_count": len(audit_rows),
        "clean_keep_count": len(clean_rows),
        "review_count": len(review_rows),
        "manual_review_row_count": len(manual_decisions),
        "manual_decision_count": manual_verdict_count,
        "final_scoring_count": len(final_scoring_rows),
        "evidence_count": len(evidence_rows),
        "recommended_status_counts": dict(sorted(Counter(str(row["recommended_status"]) for row in audit_rows).items())),
        "final_status_counts": dict(sorted(Counter(str(row["final_status"]) for row in audit_rows).items())),
        "review_bucket_counts": dict(sorted(Counter(str(row["review_bucket"]) for row in audit_rows if row.get("review_bucket")).items())),
        "root_cause_counts": dict(sorted(Counter(str(row["root_cause_category"]) for row in audit_rows if row.get("root_cause_category")).items())),
        "outputs": {
            "audit_csv": str(audit_csv),
            "review_csv": str(review_csv),
            "manual_verification_csv": str(manual_verification_csv),
            "final_scoring_universe_csv": str(final_scoring_universe_csv),
            "evidence_csv": str(evidence_csv),
            "trial_status_overrides_csv": str(trial_status_overrides_csv),
            "clean_csv": str(clean_csv),
            "clean_json": str(clean_json),
        },
        "config": cfg_get(config, "ctgov_audit", {}),
    }
    write_output_with_run_failure(write_json, manifest_json, manifest)
    if run_id is not None:
        with connect(db_path, timeout_sec=sqlite_timeout_sec) as conn:
            finish_run(
                conn,
                run_id=run_id,
                status="success",
                row_count=len(audit_rows),
                message=(
                    f"clean={len(clean_rows)} review={len(review_rows)} "
                    f"final_scoring={len(final_scoring_rows)} manual_decisions={manual_verdict_count} "
                    f"evidence={len(evidence_rows)}"
                ),
            )
        _RUN_CONTEXT["finished"] = True
    LOGGER.info(
        "CTGov audit complete: companies=%d clean=%d review=%d final_scoring=%d manual_decisions=%d evidence=%d output=%s",
        len(audit_rows),
        len(clean_rows),
        len(review_rows),
        len(final_scoring_rows),
        manual_verdict_count,
        len(evidence_rows),
        output_dir,
    )


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:
        if not (isinstance(exc, SystemExit) and exc.code in (0, None)):
            mark_current_run_failed(exc)
        raise
