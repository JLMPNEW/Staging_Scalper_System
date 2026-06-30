#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.db import connect, finish_run, init_db, quote_identifier, start_run, utc_now  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402
from med_devices.core.point_in_time import parse_iso_date, row_is_effective_asof  # noqa: E402
from med_devices.core.text_norm import as_bool, normalize_code, normalize_org_name, normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("link_med_device_reimbursement_to_companies")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
CODE_TOKEN_RE = re.compile(r"\b(?:[A-Z]\d{4}|\d{5})\b")
CORPORATE_SUFFIXES = {
    "INC",
    "INCORPORATED",
    "CORP",
    "CORPORATION",
    "PLC",
    "LTD",
    "LIMITED",
    "LLC",
    "NV",
    "SA",
    "AG",
    "CO",
    "COMPANY",
    "HOLDING",
    "HOLDINGS",
    "GROUP",
}
STOP_TERMS = {
    "MEDICAL",
    "DEVICE",
    "DEVICES",
    "TECHNOLOGY",
    "TECHNOLOGIES",
    "HEALTH",
    "HEALTHCARE",
    "SYSTEM",
    "SYSTEMS",
    "SURGICAL",
    "DIAGNOSTIC",
    "DIAGNOSTICS",
}
FIELDNAMES = [
    "company_id",
    "ticker",
    "company_name",
    "reimbursement_policy_id",
    "policy_type",
    "policy_id",
    "reimbursement_code_id",
    "reimbursement_code",
    "confidence",
    "mapping_method",
    "matched_term",
    "title",
]


@dataclass(frozen=True)
class LinkPolicy:
    source_ids: list[str]
    min_auto_confidence: float
    exact_alias_confidence: float
    core_alias_confidence: float
    ticker_confidence: float
    min_term_length: int
    max_policy_rows: int
    code_source_ids: list[str] | None = None
    override_csv: str = ""
    resolved_classification_csv: str = ""
    manual_rate_csv: str = ""
    manual_rate_audit_csv: str = ""
    manual_rate_validation_tolerance_pct: float = 5.0
    unmapped_output_csv: str = ""
    enable_descriptor_matching: bool = True
    descriptor_confidence: float = 70.0
    descriptor_min_term_length: int = 8
    descriptor_min_token_count: int = 2
    descriptor_max_code_matches_per_term: int = 50
    descriptor_require_rate_rows: bool = True
    replace_existing_mappings: bool = True


@dataclass(frozen=True)
class CompanyAlias:
    company_id: int
    ticker: str
    company_name: str
    term: str
    method: str
    confidence: float


@dataclass(frozen=True)
class PolicyRow:
    reimbursement_policy_id: int
    policy_type: str
    policy_id: str
    title: str
    related_codes: str
    source_id: str
    search_text: str


@dataclass(frozen=True)
class ReimbursementCodeRow:
    reimbursement_code_id: int
    code: str
    short_description: str
    long_description: str
    source_id: str
    search_text: str
    rate_row_count: int


@dataclass(frozen=True)
class MatchRow:
    company_id: int
    ticker: str
    company_name: str
    reimbursement_policy_id: int | None
    policy_type: str
    policy_id: str
    reimbursement_code_id: int | None
    reimbursement_code: str
    confidence: float
    mapping_method: str
    matched_term: str
    title: str
    source_id: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Map CMS reimbursement policies and HCPCS codes to med-device companies.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--unmapped-output-csv", type=Path, default=None)
    parser.add_argument("--manual-rate-audit-csv", type=Path, default=None)
    parser.add_argument("--tickers", type=str, default="")
    parser.add_argument("--asof", type=str, default="")
    parser.add_argument("--max-policies", type=int, default=0)
    parser.add_argument("--min-confidence", type=float, default=0.0)
    return parser.parse_args()


def allow_missing_static_pit_metadata(config: dict[str, Any]) -> bool:
    return str(cfg_get(config, "historical_backfill.allow_missing_static_pit_metadata", True)).strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
        "on",
    }


def resolve_asof(raw: str) -> str:
    text = str(raw or "").strip() or datetime.now(timezone.utc).date().isoformat()
    parsed = parse_iso_date(text)
    if parsed is None:
        raise ValueError(f"Invalid as-of date: {text}")
    return parsed.isoformat()


def table_exists(conn: Any, table_name: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table_name,)).fetchone()
    return row is not None


def table_columns(conn: Any, table_name: str) -> set[str]:
    if not table_exists(conn, table_name):
        return set()
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({quote_identifier(table_name)})").fetchall()}


def to_float(raw: object) -> float | None:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def link_policy(config: dict[str, Any]) -> LinkPolicy:
    raw_source_ids = cfg_get(config, "reimbursement_entity_linking.source_ids", ["cms_coverage_api"])
    source_ids = [str(value).strip() for value in raw_source_ids] if isinstance(raw_source_ids, list) else ["cms_coverage_api"]
    raw_code_source_ids = cfg_get(config, "reimbursement_entity_linking.code_source_ids", source_ids)
    code_source_ids = (
        [str(value).strip() for value in raw_code_source_ids]
        if isinstance(raw_code_source_ids, list)
        else list(source_ids)
    )
    return LinkPolicy(
        source_ids=[source_id for source_id in source_ids if source_id],
        min_auto_confidence=float(cfg_get(config, "reimbursement_entity_linking.min_auto_confidence", 65.0)),
        exact_alias_confidence=float(cfg_get(config, "reimbursement_entity_linking.exact_alias_confidence", 92.0)),
        core_alias_confidence=float(cfg_get(config, "reimbursement_entity_linking.core_alias_confidence", 82.0)),
        ticker_confidence=float(cfg_get(config, "reimbursement_entity_linking.ticker_confidence", 60.0)),
        min_term_length=max(3, int(cfg_get(config, "reimbursement_entity_linking.min_term_length", 5))),
        max_policy_rows=max(0, int(cfg_get(config, "reimbursement_entity_linking.max_policy_rows", 0))),
        code_source_ids=[source_id for source_id in code_source_ids if source_id],
        override_csv=str(cfg_get(config, "reimbursement_entity_linking.override_csv", "") or "").strip(),
        resolved_classification_csv=str(
            cfg_get(
                config,
                "reimbursement_entity_linking.resolved_classification_csv",
                cfg_get(config, "reimbursement_features.company_classification_csv", ""),
            )
            or ""
        ).strip(),
        manual_rate_csv=str(cfg_get(config, "reimbursement_entity_linking.manual_rate_csv", "") or "").strip(),
        manual_rate_audit_csv=str(cfg_get(config, "reimbursement_entity_linking.manual_rate_audit_csv", "") or "").strip(),
        manual_rate_validation_tolerance_pct=float(
            cfg_get(config, "reimbursement_entity_linking.manual_rate_validation_tolerance_pct", 5.0)
        ),
        unmapped_output_csv=str(cfg_get(config, "reimbursement_entity_linking.unmapped_output_csv", "") or "").strip(),
        enable_descriptor_matching=as_bool(
            cfg_get(config, "reimbursement_entity_linking.enable_descriptor_matching", True),
            default=True,
        ),
        descriptor_confidence=float(cfg_get(config, "reimbursement_entity_linking.descriptor_confidence", 70.0)),
        descriptor_min_term_length=max(3, int(cfg_get(config, "reimbursement_entity_linking.descriptor_min_term_length", 8))),
        descriptor_min_token_count=max(1, int(cfg_get(config, "reimbursement_entity_linking.descriptor_min_token_count", 2))),
        descriptor_max_code_matches_per_term=max(1, int(cfg_get(config, "reimbursement_entity_linking.descriptor_max_code_matches_per_term", 50))),
        descriptor_require_rate_rows=as_bool(
            cfg_get(config, "reimbursement_entity_linking.descriptor_require_rate_rows", True),
            default=True,
        ),
        replace_existing_mappings=as_bool(
            cfg_get(config, "reimbursement_entity_linking.replace_existing_mappings", True),
            default=True,
        ),
    )


def strip_suffixes(norm_name: str) -> str:
    tokens = [token for token in norm_name.split() if token and token not in CORPORATE_SUFFIXES]
    return " ".join(tokens).strip()


def term_is_usable(term: str, *, min_length: int) -> bool:
    if len(term) < min_length:
        return False
    if term in STOP_TERMS:
        return False
    return True


def add_alias(
    aliases: dict[tuple[int, str], CompanyAlias],
    *,
    company_id: int,
    ticker: str,
    company_name: str,
    term: str,
    method: str,
    confidence: float,
    min_length: int,
) -> None:
    norm = normalize_org_name(term)
    if not term_is_usable(norm, min_length=min_length):
        return
    key = (company_id, norm)
    existing = aliases.get(key)
    if existing is None or confidence > existing.confidence:
        aliases[key] = CompanyAlias(company_id, ticker, company_name, norm, method, confidence)


def alias_confidence_score(raw: object) -> float:
    value = to_float(raw)
    if value is None:
        return 100.0
    if 0.0 <= value <= 1.0:
        return value * 100.0
    return value


def build_aliases(
    conn: Any,
    *,
    ticker_filter: set[str],
    policy: LinkPolicy,
    asof: str | None = None,
) -> list[CompanyAlias]:
    if asof:
        rows = conn.execute(
            """
            SELECT c.company_id, c.ticker, c.company_name
            FROM dim_company c
            WHERE EXISTS (
                SELECT 1
                FROM dim_universe_membership m
                WHERE m.company_id = c.company_id
                  AND m.model_family = 'med_devices'
                  AND m.point_in_time_flag = 1
                  AND m.start_date <= ?
                  AND (m.end_date IS NULL OR m.end_date >= ?)
            )
            ORDER BY c.ticker
            """,
            (asof, asof),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT company_id, ticker, company_name
            FROM dim_company
            WHERE is_active = 1
            ORDER BY ticker
            """
        ).fetchall()
    aliases: dict[tuple[int, str], CompanyAlias] = {}
    company_ids: list[int] = []
    meta: dict[int, tuple[str, str]] = {}
    for row in rows:
        ticker = normalize_ticker(row["ticker"])
        if ticker_filter and ticker not in ticker_filter:
            continue
        company_id = int(row["company_id"])
        company_name = str(row["company_name"] or "")
        company_ids.append(company_id)
        meta[company_id] = (ticker, company_name)
        normalized = normalize_org_name(company_name)
        core = strip_suffixes(normalized)
        add_alias(
            aliases,
            company_id=company_id,
            ticker=ticker,
            company_name=company_name,
            term=normalized,
            method="company_name_exact",
            confidence=policy.exact_alias_confidence,
            min_length=policy.min_term_length,
        )
        add_alias(
            aliases,
            company_id=company_id,
            ticker=ticker,
            company_name=company_name,
            term=core,
            method="company_name_core",
            confidence=policy.core_alias_confidence,
            min_length=policy.min_term_length,
        )
        if len(ticker) >= max(4, policy.min_term_length):
            add_alias(
                aliases,
                company_id=company_id,
                ticker=ticker,
                company_name=company_name,
                term=ticker,
                method="ticker",
                confidence=policy.ticker_confidence,
                min_length=policy.min_term_length,
            )
    if company_ids:
        placeholders = ",".join("?" for _ in company_ids)
        alias_rows = conn.execute(
            f"""
            SELECT company_id, alias_norm, alias_raw, confidence, is_manual
            FROM dim_company_alias
            WHERE company_id IN ({placeholders})
            """,
            company_ids,
        ).fetchall()
        alias_companies: dict[str, set[int]] = {}
        for row in alias_rows:
            if int(row["is_manual"] or 0):
                continue
            term = normalize_org_name(str(row["alias_norm"] or row["alias_raw"] or ""))
            if term:
                alias_companies.setdefault(term, set()).add(int(row["company_id"]))
        ambiguous_terms = {term for term, term_company_ids in alias_companies.items() if len(term_company_ids) > 1}
        for row in alias_rows:
            company_id = int(row["company_id"])
            ticker, company_name = meta[company_id]
            is_manual = int(row["is_manual"] or 0)
            term = str(row["alias_norm"] or row["alias_raw"] or "")
            if not is_manual and normalize_org_name(term) in ambiguous_terms:
                continue
            confidence = min(98.0, max(0.0, alias_confidence_score(row["confidence"])))
            method = "manual_alias" if is_manual else "company_alias"
            add_alias(
                aliases,
                company_id=company_id,
                ticker=ticker,
                company_name=company_name,
                term=term,
                method=method,
                confidence=confidence,
                min_length=policy.min_term_length,
            )
    return sorted(aliases.values(), key=lambda item: (-item.confidence, item.ticker, item.term))


def load_policy_rows(conn: Any, *, policy: LinkPolicy, max_rows: int, asof: str | None = None) -> list[PolicyRow]:
    if not policy.source_ids:
        return []
    placeholders = ",".join("?" for _ in policy.source_ids)
    limit = max_rows or policy.max_policy_rows
    limit_sql = " LIMIT ?" if limit > 0 else ""
    params: list[Any] = [*policy.source_ids, asof, asof, asof, asof]
    if limit > 0:
        params.append(limit)
    rows = conn.execute(
        f"""
        SELECT reimbursement_policy_id, policy_type, policy_id, title, related_codes, source_id, payload_json
        FROM fact_reimbursement_policy
        WHERE source_id IN ({placeholders})
          AND (? IS NULL OR NULLIF(effective_date, '') IS NULL OR SUBSTR(effective_date, 1, 10) <= ?)
          AND (? IS NULL OR NULLIF(retirement_date, '') IS NULL OR SUBSTR(retirement_date, 1, 10) >= ?)
        ORDER BY reimbursement_policy_id
        {limit_sql}
        """,
        params,
    ).fetchall()
    out: list[PolicyRow] = []
    for row in rows:
        payload_text = str(row["payload_json"] or "")
        title = str(row["title"] or "")
        search_text = extract_policy_search_text(title, payload_text)
        out.append(
            PolicyRow(
                reimbursement_policy_id=int(row["reimbursement_policy_id"]),
                policy_type=str(row["policy_type"] or ""),
                policy_id=str(row["policy_id"] or ""),
                title=title,
                related_codes=str(row["related_codes"] or ""),
                source_id=str(row["source_id"] or ""),
                search_text=search_text,
            )
        )
    return out


def extract_policy_search_text(title: str, payload_text: str) -> str:
    parts = [title]
    try:
        payload = json.loads(payload_text) if payload_text else {}
    except json.JSONDecodeError:
        payload = {}
    wanted = (
        "title",
        "contractor",
        "jurisdiction",
        "description",
        "narrative",
        "summary",
        "coverage",
        "indication",
        "billing",
        "article",
        "lcd",
        "ncd",
    )

    def visit(value: Any, key_hint: str = "", depth: int = 0) -> None:
        if depth > 8:
            return
        if isinstance(value, dict):
            for key, child in value.items():
                visit(child, str(key).lower(), depth + 1)
        elif isinstance(value, list):
            for child in value:
                visit(child, key_hint, depth + 1)
        elif isinstance(value, str) and len(value.strip()) > 10 and any(token in key_hint for token in wanted):
            parts.append(value)

    visit(payload)
    return normalize_org_name(" ".join(parts))


def term_in_text(term: str, text: str) -> bool:
    if not term or not text:
        return False
    return re.search(rf"(?<![A-Z0-9]){re.escape(term)}(?![A-Z0-9])", text) is not None


def parse_related_codes(raw: object) -> set[str]:
    text = str(raw or "").upper()
    return {match.group(0) for match in CODE_TOKEN_RE.finditer(text)}


def reimbursement_code_ids(
    conn: Any,
    codes: set[str],
    *,
    source_ids: list[str] | None = None,
    asof: str | None = None,
) -> dict[str, list[int]]:
    if not codes:
        return {}
    placeholders = ",".join("?" for _ in codes)
    source_ids = [source_id for source_id in (source_ids or []) if source_id]
    source_sql = ""
    params: list[Any] = [*sorted(codes), asof, asof, asof, asof]
    if source_ids:
        source_placeholders = ",".join("?" for _ in source_ids)
        source_sql = f" AND source_id IN ({source_placeholders})"
        params.extend(source_ids)
    rows = conn.execute(
        f"""
        SELECT reimbursement_code_id, code
        FROM dim_reimbursement_code
        WHERE code IN ({placeholders})
          AND (? IS NULL OR NULLIF(effective_date, '') IS NULL OR SUBSTR(effective_date, 1, 10) <= ?)
          AND (? IS NULL OR NULLIF(termination_date, '') IS NULL OR SUBSTR(termination_date, 1, 10) >= ?)
        {source_sql}
        """,
        params,
    ).fetchall()
    out: dict[str, list[int]] = {}
    for row in rows:
        out.setdefault(str(row["code"] or ""), []).append(int(row["reimbursement_code_id"]))
    return out


def ensure_manual_reimbursement_code(
    conn: Any,
    *,
    code: str,
    code_type: str,
    source_id: str,
    short_description: str,
    long_description: str,
) -> int:
    now = utc_now()
    normalized_type = normalize_org_name(code_type or "HCPCS").replace(" ", "_")[:24] or "HCPCS"
    existing = conn.execute(
        """
        SELECT reimbursement_code_id
        FROM dim_reimbursement_code
        WHERE code_type = ?
          AND code = ?
          AND COALESCE(effective_date, '') = ''
        """,
        (normalized_type, code),
    ).fetchone()
    if existing is not None:
        reimbursement_code_id = int(existing["reimbursement_code_id"])
        conn.execute(
            """
            UPDATE dim_reimbursement_code
            SET short_description = COALESCE(NULLIF(?, ''), short_description),
                long_description = COALESCE(NULLIF(?, ''), long_description),
                source_id = COALESCE(NULLIF(?, ''), source_id),
                updated_at = ?
            WHERE reimbursement_code_id = ?
            """,
            (short_description, long_description, source_id, now, reimbursement_code_id),
        )
        return reimbursement_code_id
    cursor = conn.execute(
        """
        INSERT INTO dim_reimbursement_code(
            code_type, code, short_description, long_description, source_id, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (normalized_type, code, short_description or None, long_description or None, source_id or None, now, now),
    )
    return int(cursor.lastrowid)


def upsert_manual_reimbursement_rate(
    conn: Any,
    *,
    reimbursement_code_id: int,
    payment_system: str,
    effective_date: str,
    locality: str,
    payment_rate: float | None,
    status_indicator: str,
    apc: str,
    drg: str,
    source_id: str,
    payload: dict[str, Any],
) -> None:
    now = utc_now()
    existing = conn.execute(
        """
        SELECT reimbursement_rate_id
        FROM fact_reimbursement_rate
        WHERE reimbursement_code_id = ?
          AND payment_system = ?
          AND COALESCE(effective_date, '') = COALESCE(?, '')
          AND COALESCE(locality, '') = COALESCE(?, '')
          AND COALESCE(source_id, '') = COALESCE(?, '')
        """,
        (reimbursement_code_id, payment_system, effective_date or None, locality or None, source_id),
    ).fetchone()
    payload_json = json.dumps(payload, ensure_ascii=True, sort_keys=True)
    values = (
        reimbursement_code_id,
        payment_system,
        effective_date or None,
        locality or None,
        apc or None,
        drg or None,
        payment_rate,
        status_indicator or None,
        source_id,
        payload_json,
        now,
    )
    if existing is not None:
        conn.execute(
            """
            UPDATE fact_reimbursement_rate
            SET apc = ?, drg = ?, payment_rate = ?, status_indicator = ?,
                payload_json = ?, updated_at = ?
            WHERE reimbursement_rate_id = ?
            """,
            (apc or None, drg or None, payment_rate, status_indicator or None, payload_json, now, int(existing["reimbursement_rate_id"])),
        )
        return
    conn.execute(
        """
        INSERT INTO fact_reimbursement_rate(
            reimbursement_code_id, payment_system, effective_date, locality, apc, drg,
            payment_rate, status_indicator, source_id, payload_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (*values, now),
    )


def official_rate_validation(
    conn: Any,
    *,
    reimbursement_code_id: int,
    manual_payment_rate: float | None,
    tolerance_pct: float,
    asof: str | None = None,
) -> dict[str, Any]:
    if manual_payment_rate is None:
        return {
            "validation_status": "non_flat_rate_status",
            "official_rate_count": 0,
            "nearest_official_rate": "",
            "official_rate_delta_pct": "",
            "official_payment_system": "",
            "official_locality": "",
        }
    rows = conn.execute(
        """
        SELECT payment_system, locality, effective_date, payment_rate
        FROM fact_reimbursement_rate
        WHERE reimbursement_code_id = ?
          AND payment_rate IS NOT NULL
          AND LOWER(payment_system) NOT LIKE '%manual%'
          AND LOWER(payment_system) NOT LIKE '%benchmark%'
          AND LOWER(payment_system) NOT LIKE '%override%'
          AND (? IS NULL OR NULLIF(effective_date, '') IS NULL OR SUBSTR(effective_date, 1, 10) <= ?)
        """,
        (reimbursement_code_id, asof, asof),
    ).fetchall()
    if not rows:
        return {
            "validation_status": "manual_benchmark_unverified",
            "official_rate_count": 0,
            "nearest_official_rate": "",
            "official_rate_delta_pct": "",
            "official_payment_system": "",
            "official_locality": "",
        }
    nearest = min(rows, key=lambda item: abs(float(item["payment_rate"] or 0.0) - manual_payment_rate))
    nearest_rate = float(nearest["payment_rate"] or 0.0)
    if abs(nearest_rate) <= 1e-12:
        delta_pct = 0.0 if abs(manual_payment_rate) <= 1e-12 else 100.0
    else:
        delta_pct = abs(manual_payment_rate - nearest_rate) / abs(nearest_rate) * 100.0
    status = "official_rate_match" if delta_pct <= tolerance_pct else "official_rate_conflict"
    return {
        "validation_status": status,
        "official_rate_count": len(rows),
        "nearest_official_rate": round(nearest_rate, 4),
        "official_rate_delta_pct": round(delta_pct, 4),
        "official_payment_system": str(nearest["payment_system"] or ""),
        "official_locality": str(nearest["locality"] or ""),
    }


def load_manual_rate_rows(
    conn: Any,
    path: Path,
    *,
    policy: LinkPolicy,
    asof: str | None = None,
    include_missing_pit_metadata: bool = True,
    audit_rows: list[dict[str, Any]] | None = None,
) -> int:
    if not path.exists():
        return 0
    count = 0
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for raw_row in reader:
            if not row_is_effective_asof(raw_row, asof, include_missing=include_missing_pit_metadata):
                continue
            code = normalize_code(row_get(raw_row, "code", "reimbursement_code", "hcpcs", "cpt"))
            if not code:
                continue
            code_type = row_get(raw_row, "code_type", "code_system") or "HCPCS"
            source_id = row_get(raw_row, "source_id") or ((policy.code_source_ids or policy.source_ids or ["cms_payment_files"])[0])
            reimbursement_code_id = ensure_manual_reimbursement_code(
                conn,
                code=code,
                code_type=code_type,
                source_id=source_id,
                short_description=row_get(raw_row, "short_description", "description"),
                long_description=row_get(raw_row, "long_description", "notes"),
            )
            payment_rate = to_float(row_get(raw_row, "payment_rate", "rate", "payment_amount"))
            status_indicator = row_get(raw_row, "status_indicator", "status")
            if payment_rate is None and not status_indicator:
                continue
            validation = official_rate_validation(
                conn,
                reimbursement_code_id=reimbursement_code_id,
                manual_payment_rate=payment_rate,
                tolerance_pct=policy.manual_rate_validation_tolerance_pct,
                asof=asof,
            )
            upsert_manual_reimbursement_rate(
                conn,
                reimbursement_code_id=reimbursement_code_id,
                payment_system=row_get(raw_row, "payment_system") or "manual_benchmark",
                effective_date=row_get(raw_row, "effective_date"),
                locality=row_get(raw_row, "locality") or "national",
                payment_rate=payment_rate,
                status_indicator=status_indicator,
                apc=row_get(raw_row, "apc"),
                drg=row_get(raw_row, "drg"),
                source_id=source_id,
                payload={
                    "manual_rate_row": {str(key): str(value or "") for key, value in raw_row.items()},
                    "rate_basis": row_get(raw_row, "rate_basis"),
                    "notes": row_get(raw_row, "notes"),
                    "official_rate_validation": validation,
                },
            )
            if audit_rows is not None:
                audit_rows.append(
                    {
                        "code": code,
                        "code_type": code_type,
                        "payment_system": row_get(raw_row, "payment_system") or "manual_benchmark",
                        "effective_date": row_get(raw_row, "effective_date"),
                        "locality": row_get(raw_row, "locality") or "national",
                        "payment_rate": "" if payment_rate is None else payment_rate,
                        "status_indicator": status_indicator,
                        "validation_status": validation["validation_status"],
                        "official_rate_count": validation["official_rate_count"],
                        "nearest_official_rate": validation["nearest_official_rate"],
                        "official_rate_delta_pct": validation["official_rate_delta_pct"],
                        "official_payment_system": validation["official_payment_system"],
                        "official_locality": validation["official_locality"],
                        "rate_basis": row_get(raw_row, "rate_basis"),
                        "notes": row_get(raw_row, "notes"),
                    }
                )
            count += 1
    return count


def build_matches(
    conn: Any,
    aliases: list[CompanyAlias],
    policies: list[PolicyRow],
    *,
    min_confidence: float,
    asof: str | None = None,
) -> list[MatchRow]:
    matches: list[MatchRow] = []
    eligible_aliases = [alias for alias in aliases if alias.confidence >= min_confidence]
    term_to_aliases: dict[str, list[CompanyAlias]] = {}
    for alias in eligible_aliases:
        term_to_aliases.setdefault(alias.term, []).append(alias)
    alias_pattern = (
        re.compile(
            r"(?<![A-Z0-9])(" + "|".join(re.escape(term) for term in sorted(term_to_aliases, key=len, reverse=True)) + r")(?![A-Z0-9])"
        )
        if term_to_aliases
        else None
    )
    all_codes: set[str] = set()
    policy_codes: dict[int, set[str]] = {}
    for policy_row in policies:
        codes = parse_related_codes(policy_row.related_codes)
        policy_codes[policy_row.reimbursement_policy_id] = codes
        all_codes.update(codes)
    all_code_ids = reimbursement_code_ids(conn, all_codes, asof=asof)
    for policy_row in policies:
        codes = policy_codes.get(policy_row.reimbursement_policy_id, set())
        best_by_company: dict[int, CompanyAlias] = {}
        if alias_pattern is not None:
            for match in alias_pattern.finditer(policy_row.search_text):
                for alias in term_to_aliases.get(match.group(1), []):
                    existing = best_by_company.get(alias.company_id)
                    if existing is None or alias.confidence > existing.confidence:
                        best_by_company[alias.company_id] = alias
        for alias in best_by_company.values():
            if codes:
                for code in sorted(codes):
                    ids = all_code_ids.get(code, [])
                    if not ids:
                        matches.append(
                            MatchRow(
                                alias.company_id,
                                alias.ticker,
                                alias.company_name,
                                policy_row.reimbursement_policy_id,
                                policy_row.policy_type,
                                policy_row.policy_id,
                                None,
                                code,
                                alias.confidence,
                                alias.method,
                                alias.term,
                                policy_row.title,
                                policy_row.source_id,
                            )
                        )
                    for code_id in ids:
                        matches.append(
                            MatchRow(
                                alias.company_id,
                                alias.ticker,
                                alias.company_name,
                                policy_row.reimbursement_policy_id,
                                policy_row.policy_type,
                                policy_row.policy_id,
                                code_id,
                                code,
                                alias.confidence,
                                alias.method,
                                alias.term,
                                policy_row.title,
                                policy_row.source_id,
                            )
                        )
            else:
                matches.append(
                    MatchRow(
                        alias.company_id,
                        alias.ticker,
                        alias.company_name,
                        policy_row.reimbursement_policy_id,
                        policy_row.policy_type,
                        policy_row.policy_id,
                        None,
                        "",
                        alias.confidence,
                        alias.method,
                        alias.term,
                        policy_row.title,
                        policy_row.source_id,
                    )
                )
    return matches


def load_company_meta(conn: Any, *, ticker_filter: set[str], asof: str | None = None) -> dict[int, tuple[str, str]]:
    if asof:
        rows = conn.execute(
            """
            SELECT c.company_id, c.ticker, c.company_name
            FROM dim_company c
            WHERE EXISTS (
                SELECT 1
                FROM dim_universe_membership m
                WHERE m.company_id = c.company_id
                  AND m.model_family = 'med_devices'
                  AND m.point_in_time_flag = 1
                  AND m.start_date <= ?
                  AND (m.end_date IS NULL OR m.end_date >= ?)
            )
            ORDER BY c.ticker
            """,
            (asof, asof),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT company_id, ticker, company_name
            FROM dim_company
            WHERE is_active = 1
            ORDER BY ticker
            """
        ).fetchall()
    out: dict[int, tuple[str, str]] = {}
    for row in rows:
        ticker = normalize_ticker(row["ticker"])
        if ticker_filter and ticker not in ticker_filter:
            continue
        out[int(row["company_id"])] = (ticker, str(row["company_name"] or ""))
    return out


FDA_DESCRIPTOR_DATE_COLUMNS = {
    "fact_fda_approval": ("decision_date", "receipt_date"),
    "fact_fda_recall": ("recall_initiation_date", "center_classification_date", "termination_date"),
    "fact_fda_adverse_event": (
        "event_date",
        "date_of_event",
        "report_date",
        "mdr_report_date",
        "date_received",
        "report_received_date",
    ),
}
FDA_DESCRIPTOR_PRODUCT_CODE_TABLES = ("fact_fda_recall", "fact_fda_adverse_event")


def fda_descriptor_date_expr(available_columns: set[str], table: str) -> str:
    date_columns = [column for column in FDA_DESCRIPTOR_DATE_COLUMNS.get(table, ()) if column in available_columns]
    if not date_columns:
        return ""
    quoted = [quote_identifier(column) for column in date_columns]
    return quoted[0] if len(quoted) == 1 else f"COALESCE({', '.join(quoted)})"


def load_company_descriptor_terms(
    conn: Any,
    company_meta: dict[int, tuple[str, str]],
    *,
    policy: LinkPolicy,
    asof: str | None = None,
) -> dict[int, set[str]]:
    if not company_meta:
        return {}
    company_ids = sorted(company_meta)
    placeholders = ",".join("?" for _ in company_ids)
    out: dict[int, set[str]] = {company_id: set() for company_id in company_ids}
    approval_columns = table_columns(conn, "fact_fda_approval")
    if {"company_id", "device_name"}.issubset(approval_columns):
        date_expr = fda_descriptor_date_expr(approval_columns, "fact_fda_approval")
        if date_expr:
            rows = conn.execute(
                f"""
                SELECT company_id, device_name
                FROM fact_fda_approval
                WHERE company_id IN ({placeholders})
                  AND COALESCE(device_name, '') != ''
                  AND (? IS NULL OR NULLIF({date_expr}, '') IS NULL OR SUBSTR({date_expr}, 1, 10) <= ?)
                """,
                [*company_ids, asof, asof],
            ).fetchall()
            for row in rows:
                company_id = int(row["company_id"])
                for term in descriptor_terms(row["device_name"], policy=policy):
                    out.setdefault(company_id, set()).add(term)
        else:
            LOGGER.warning("Skipping FDA approval descriptor terms without PIT date column.")

    product_code_columns = table_columns(conn, "dim_fda_product_code")
    if {"product_code", "device_name"}.issubset(product_code_columns):
        for table in FDA_DESCRIPTOR_PRODUCT_CODE_TABLES:
            columns = table_columns(conn, table)
            if not {"company_id", "product_code"}.issubset(columns):
                continue
            date_expr = fda_descriptor_date_expr(columns, table)
            if not date_expr:
                LOGGER.warning("Skipping FDA product-code descriptor terms without PIT date column: table=%s", table)
                continue
            rows = conn.execute(
                f"""
                SELECT DISTINCT f.company_id, p.device_name
                FROM {quote_identifier(table)} f
                JOIN dim_fda_product_code p
                  ON p.product_code = f.product_code
                WHERE f.company_id IN ({placeholders})
                  AND COALESCE(f.product_code, '') != ''
                  AND COALESCE(p.device_name, '') != ''
                  AND (? IS NULL OR NULLIF({date_expr}, '') IS NULL OR SUBSTR({date_expr}, 1, 10) <= ?)
                """,
                [*company_ids, asof, asof],
            ).fetchall()
            for row in rows:
                company_id = int(row["company_id"])
                for term in descriptor_terms(row["device_name"], policy=policy):
                    out.setdefault(company_id, set()).add(term)
    return out


def descriptor_terms(raw: object, *, policy: LinkPolicy) -> set[str]:
    norm = normalize_org_name(raw)
    if not norm:
        return set()
    tokens = [
        token
        for token in norm.split()
        if token
        and token not in STOP_TERMS
        and token not in CORPORATE_SUFFIXES
        and not token.isdigit()
    ]
    out: set[str] = set()
    min_tokens = policy.descriptor_min_token_count
    min_length = policy.descriptor_min_term_length
    if len(tokens) >= min_tokens:
        full = " ".join(tokens)
        if term_is_usable(full, min_length=min_length):
            out.add(full)
    max_window = min(5, len(tokens))
    for size in range(max(min_tokens, 2), max_window + 1):
        for idx in range(0, len(tokens) - size + 1):
            term = " ".join(tokens[idx : idx + size])
            if term_is_usable(term, min_length=min_length):
                out.add(term)
    return out


def load_reimbursement_code_rows(conn: Any, *, policy: LinkPolicy, asof: str | None = None) -> list[ReimbursementCodeRow]:
    source_ids = [source_id for source_id in (policy.code_source_ids or policy.source_ids) if source_id]
    where_parts = [
        "(? IS NULL OR NULLIF(c.effective_date, '') IS NULL OR SUBSTR(c.effective_date, 1, 10) <= ?)",
        "(? IS NULL OR NULLIF(c.termination_date, '') IS NULL OR SUBSTR(c.termination_date, 1, 10) >= ?)",
    ]
    params: list[Any] = [asof, asof, asof, asof]
    if source_ids:
        placeholders = ",".join("?" for _ in source_ids)
        where_parts.append(f"c.source_id IN ({placeholders})")
        params.extend(source_ids)
    where_sql = "WHERE " + " AND ".join(where_parts)
    rows = conn.execute(
        f"""
        SELECT c.reimbursement_code_id, c.code, c.short_description, c.long_description, c.source_id,
               COUNT(r.reimbursement_rate_id) AS rate_row_count
        FROM dim_reimbursement_code c
        LEFT JOIN fact_reimbursement_rate r
          ON r.reimbursement_code_id = c.reimbursement_code_id
         AND (? IS NULL OR NULLIF(r.effective_date, '') IS NULL OR SUBSTR(r.effective_date, 1, 10) <= ?)
        {where_sql}
        GROUP BY c.reimbursement_code_id, c.code, c.short_description, c.long_description, c.source_id
        """,
        [asof, asof, *params],
    ).fetchall()
    out: list[ReimbursementCodeRow] = []
    for row in rows:
        short_description = str(row["short_description"] or "")
        long_description = str(row["long_description"] or "")
        search_text = normalize_org_name(" ".join([short_description, long_description]))
        if not search_text:
            continue
        rate_row_count = int(row["rate_row_count"] or 0)
        if policy.descriptor_require_rate_rows and rate_row_count <= 0:
            continue
        out.append(
            ReimbursementCodeRow(
                reimbursement_code_id=int(row["reimbursement_code_id"]),
                code=str(row["code"] or ""),
                short_description=short_description,
                long_description=long_description,
                source_id=str(row["source_id"] or ""),
                search_text=search_text,
                rate_row_count=rate_row_count,
            )
        )
    return out


def text_tokens(text: str) -> set[str]:
    return {
        token
        for token in str(text or "").split()
        if token and token not in STOP_TERMS and len(token) >= 3
    }


def build_descriptor_code_matches(
    conn: Any,
    company_meta: dict[int, tuple[str, str]],
    *,
    policy: LinkPolicy,
    min_confidence: float,
    asof: str | None = None,
) -> list[MatchRow]:
    if not policy.enable_descriptor_matching or policy.descriptor_confidence < min_confidence:
        return []
    company_terms = load_company_descriptor_terms(conn, company_meta, policy=policy, asof=asof)
    code_rows = load_reimbursement_code_rows(conn, policy=policy, asof=asof)
    token_index: dict[str, set[int]] = {}
    for idx, code_row in enumerate(code_rows):
        for token in text_tokens(code_row.search_text):
            token_index.setdefault(token, set()).add(idx)
    matches: list[MatchRow] = []
    for company_id, terms in company_terms.items():
        if not terms:
            continue
        ticker, company_name = company_meta[company_id]
        per_term_count: dict[str, int] = {}
        seen_codes: set[int] = set()
        for term in sorted(terms, key=len, reverse=True):
            term_tokens = text_tokens(term)
            if not term_tokens:
                continue
            candidate_ids: set[int] | None = None
            for token in term_tokens:
                ids = token_index.get(token, set())
                candidate_ids = set(ids) if candidate_ids is None else candidate_ids & ids
                if not candidate_ids:
                    break
            if not candidate_ids:
                continue
            for code_idx in sorted(candidate_ids):
                code_row = code_rows[code_idx]
                if not term_in_text(term, code_row.search_text):
                    continue
                per_term_count[term] = per_term_count.get(term, 0) + 1
                if per_term_count[term] > policy.descriptor_max_code_matches_per_term:
                    break
                if code_row.reimbursement_code_id in seen_codes:
                    continue
                seen_codes.add(code_row.reimbursement_code_id)
                matches.append(
                    MatchRow(
                        company_id,
                        ticker,
                        company_name,
                        None,
                        "code_descriptor",
                        "",
                        code_row.reimbursement_code_id,
                        code_row.code,
                        policy.descriptor_confidence,
                        "fda_device_descriptor",
                        term,
                        code_row.short_description or code_row.long_description,
                        code_row.source_id,
                    )
                )
    return matches


def row_get(row: dict[str, str], *names: str) -> str:
    lowered = {str(key).strip().lower(): str(value or "").strip() for key, value in row.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value:
            return value
    return ""


def load_override_matches(
    conn: Any,
    path: Path,
    company_meta: dict[int, tuple[str, str]],
    *,
    policy: LinkPolicy,
    min_confidence: float,
    asof: str | None = None,
    include_missing_pit_metadata: bool = True,
) -> list[MatchRow]:
    if not path.exists():
        return []
    ticker_to_company = {ticker: (company_id, company_name) for company_id, (ticker, company_name) in company_meta.items()}
    matches: list[MatchRow] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for raw_row in reader:
            if not row_is_effective_asof(raw_row, asof, include_missing=include_missing_pit_metadata):
                continue
            active = row_get(raw_row, "active", "enabled")
            if active and active.lower() in {"0", "false", "no", "n"}:
                continue
            ticker = normalize_ticker(row_get(raw_row, "ticker", "symbol"))
            company = ticker_to_company.get(ticker)
            if company is None:
                continue
            code = normalize_code(row_get(raw_row, "reimbursement_code", "hcpcs", "hcpcs_code", "code"))
            if not code:
                continue
            confidence = to_float(row_get(raw_row, "confidence")) or 99.0
            if confidence < min_confidence:
                continue
            source_id = row_get(raw_row, "source_id") or ((policy.code_source_ids or policy.source_ids or ["cms_payment_files"])[0])
            code_type = row_get(raw_row, "code_type", "code_system") or "HCPCS"
            short_description = row_get(raw_row, "short_description", "description", "product_name")
            long_description = row_get(raw_row, "long_description", "notes")
            manual_code_id = ensure_manual_reimbursement_code(
                conn,
                code=code,
                code_type=code_type,
                source_id=source_id,
                short_description=short_description,
                long_description=long_description,
            )
            code_ids = reimbursement_code_ids(
                conn,
                {code},
                source_ids=policy.code_source_ids or policy.source_ids,
                asof=asof,
            )
            if not code_ids.get(code):
                code_ids = {code: [manual_code_id]}
            company_id, company_name = company
            for code_id in code_ids[code]:
                matches.append(
                    MatchRow(
                        company_id,
                        ticker,
                        company_name,
                        None,
                        "manual_override",
                        "",
                        code_id,
                        code,
                        confidence,
                        row_get(raw_row, "mapping_method") or "manual_override",
                        row_get(raw_row, "matched_term", "product_name", "notes"),
                        row_get(raw_row, "notes", "product_name"),
                        source_id,
                    )
                )
    return matches


RESOLVED_NO_CODE_PAYMENT_STATUSES = {
    "bundled_ipps",
    "bundled_opps",
    "bundled_opps_ipps",
    "cash_fee_sched",
    "cash_pay_or_out_of_pocket",
    "commercial_contract_no_cms",
    "commercial_vision_or_cash_pay",
    "component_pricing_no_direct_cms",
    "cpt_category_iii",
    "dental_or_cash_pay",
    "developmental_premarket_no_active_billing",
    "enterprise_saas_no_clinical_code",
    "esrd_bundle",
    "external_report_mismatch_guard",
    "facility_budget",
    "hospital_overhead_budget",
    "imaging_provider_mpfs_rates",
    "jurisdictional_mac_priced",
    "laboratory_overhead_no_direct_code",
    "large_lab_clfs_array",
    "not_applicable_ruo_b2b",
    "ntap_add_on_payment",
    "opo_cost_pass_through",
    "opps_asp_passthrough",
    "packaged_apc_asc",
    "packaged_status_n",
    "pharma_overhead",
    "pharmacy_benefit_or_ncpdp",
    "procedure_rate_mpfs",
    "state_public_health_bundle",
    "system_budget",
    "upstream_b2b_no_clinical_code",
    "veterinary_no_cms",
}


def load_resolved_no_code_company_ids(
    path: Path | None,
    company_meta: dict[int, tuple[str, str]],
    *,
    asof: str | None = None,
    include_missing_pit_metadata: bool = True,
) -> set[int]:
    if path is None or not path.exists():
        return set()
    ticker_to_company_id = {ticker: company_id for company_id, (ticker, _) in company_meta.items()}
    resolved: set[int] = set()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if not row_is_effective_asof(row, asof, include_missing=include_missing_pit_metadata):
                continue
            ticker = normalize_ticker(row_get(row, "ticker", "symbol"))
            if not ticker:
                continue
            status = normalize_org_name(row_get(row, "payment_rate_status", "payment_status")).lower().replace(" ", "_")
            if status in RESOLVED_NO_CODE_PAYMENT_STATUSES:
                company_id = ticker_to_company_id.get(ticker)
                if company_id is not None:
                    resolved.add(company_id)
    return resolved


def upsert_policy_mapping(conn: Any, match: MatchRow) -> None:
    if match.reimbursement_policy_id is None:
        return
    now = utc_now()
    payload = {
        "ticker": match.ticker,
        "policy_id": match.policy_id,
        "policy_type": match.policy_type,
        "title": match.title,
    }
    conn.execute(
        """
        INSERT INTO map_company_reimbursement_policy(
            company_id, reimbursement_policy_id, confidence, mapping_method, matched_term,
            source_id, payload_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(company_id, reimbursement_policy_id) DO UPDATE SET
            confidence = excluded.confidence,
            mapping_method = excluded.mapping_method,
            matched_term = excluded.matched_term,
            source_id = excluded.source_id,
            payload_json = excluded.payload_json,
            updated_at = excluded.updated_at
        """,
        (
            match.company_id,
            match.reimbursement_policy_id,
            match.confidence,
            match.mapping_method,
            match.matched_term,
            match.source_id,
            json.dumps(payload, ensure_ascii=True, sort_keys=True),
            now,
            now,
        ),
    )


def upsert_code_mapping(conn: Any, match: MatchRow) -> None:
    if match.reimbursement_code_id is None:
        return
    now = utc_now()
    payload = {
        "ticker": match.ticker,
        "policy_id": match.policy_id,
        "policy_type": match.policy_type,
        "code": match.reimbursement_code,
        "title": match.title,
    }
    existing = conn.execute(
        """
        SELECT company_reimbursement_code_id
        FROM map_company_reimbursement_code
        WHERE company_id = ?
          AND reimbursement_code_id = ?
          AND COALESCE(reimbursement_policy_id, -1) = COALESCE(?, -1)
        """,
        (match.company_id, match.reimbursement_code_id, match.reimbursement_policy_id),
    ).fetchone()
    values = (
        match.company_id,
        match.reimbursement_code_id,
        match.reimbursement_policy_id,
        match.confidence,
        match.mapping_method,
        match.matched_term,
        match.source_id,
        json.dumps(payload, ensure_ascii=True, sort_keys=True),
        now,
    )
    if existing is not None:
        conn.execute(
            """
            UPDATE map_company_reimbursement_code
            SET confidence = ?, mapping_method = ?, matched_term = ?, source_id = ?,
                payload_json = ?, updated_at = ?
            WHERE company_reimbursement_code_id = ?
            """,
            (
                match.confidence,
                match.mapping_method,
                match.matched_term,
                match.source_id,
                json.dumps(payload, ensure_ascii=True, sort_keys=True),
                now,
                int(existing["company_reimbursement_code_id"]),
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO map_company_reimbursement_code(
                company_id, reimbursement_code_id, reimbursement_policy_id, confidence,
                mapping_method, matched_term, source_id, payload_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (*values, now),
        )


def upsert_matches(conn: Any, matches: list[MatchRow]) -> tuple[int, int]:
    policy_seen: set[tuple[int, int]] = set()
    code_count = 0
    for match in matches:
        if match.reimbursement_policy_id is not None:
            policy_key = (match.company_id, match.reimbursement_policy_id)
        else:
            policy_key = None
        if policy_key is not None and policy_key not in policy_seen:
            upsert_policy_mapping(conn, match)
            policy_seen.add(policy_key)
        if match.reimbursement_code_id is not None:
            upsert_code_mapping(conn, match)
            code_count += 1
    return len(policy_seen), code_count


def row_to_dict(match: MatchRow) -> dict[str, Any]:
    return {field: getattr(match, field) for field in FIELDNAMES if hasattr(match, field)}


def dedupe_matches(matches: list[MatchRow]) -> list[MatchRow]:
    best: dict[tuple[int, int | None, int | None, str], MatchRow] = {}
    for match in matches:
        key = (
            match.company_id,
            match.reimbursement_policy_id,
            match.reimbursement_code_id,
            match.reimbursement_code,
        )
        existing = best.get(key)
        if existing is None or match.confidence > existing.confidence:
            best[key] = match
    return sorted(best.values(), key=lambda item: (item.ticker, item.policy_type, item.policy_id, item.reimbursement_code))


def write_csv(path: Path, rows: list[MatchRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(row_to_dict(row) for row in rows)


def clear_existing_mappings(conn: Any, company_ids: list[int], *, policy: LinkPolicy) -> None:
    if not company_ids:
        return
    source_ids = sorted({*(policy.source_ids or []), *((policy.code_source_ids or []) or [])})
    if not source_ids:
        return
    company_placeholders = ",".join("?" for _ in company_ids)
    source_placeholders = ",".join("?" for _ in source_ids)
    conn.execute(
        f"""
        DELETE FROM map_company_reimbursement_code
        WHERE company_id IN ({company_placeholders})
          AND source_id IN ({source_placeholders})
        """,
        [*company_ids, *source_ids],
    )
    conn.execute(
        f"""
        DELETE FROM map_company_reimbursement_policy
        WHERE company_id IN ({company_placeholders})
          AND source_id IN ({source_placeholders})
        """,
        [*company_ids, *source_ids],
    )


def load_mapped_company_ids(conn: Any, company_ids: list[int], *, policy: LinkPolicy) -> set[int]:
    if not company_ids:
        return set()
    source_ids = sorted({*(policy.source_ids or []), *((policy.code_source_ids or []) or [])})
    if not source_ids:
        return set()
    company_placeholders = ",".join("?" for _ in company_ids)
    source_placeholders = ",".join("?" for _ in source_ids)
    rows = conn.execute(
        f"""
        SELECT DISTINCT company_id
        FROM map_company_reimbursement_code
        WHERE company_id IN ({company_placeholders})
          AND source_id IN ({source_placeholders})
        """,
        [*company_ids, *source_ids],
    ).fetchall()
    return {int(row["company_id"]) for row in rows}


def write_unmapped_csv(
    path: Path,
    company_meta: dict[int, tuple[str, str]],
    mapped_company_ids: set[int],
    *,
    resolved_no_code_company_ids: set[int] | None = None,
) -> None:
    fieldnames = ["company_id", "ticker", "company_name", "review_reason"]
    resolved_no_code_company_ids = resolved_no_code_company_ids or set()
    rows = [
        {
            "company_id": company_id,
            "ticker": ticker,
            "company_name": company_name,
            "review_reason": "no_reimbursement_code_mapping",
        }
        for company_id, (ticker, company_name) in sorted(company_meta.items(), key=lambda item: item[1][0])
        if company_id not in mapped_company_ids and company_id not in resolved_no_code_company_ids
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_manual_rate_audit_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "code",
        "code_type",
        "payment_system",
        "effective_date",
        "locality",
        "payment_rate",
        "status_indicator",
        "validation_status",
        "official_rate_count",
        "nearest_official_rate",
        "official_rate_delta_pct",
        "official_payment_system",
        "official_locality",
        "rate_basis",
        "notes",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(
            cfg_get(config, "reimbursement_entity_linking.output_csv", "../output/med_devices_reports/med_device_reimbursement_entity_mapping.csv"),
            base_dir=base_dir,
        )
    )
    unmapped_output_csv = (
        args.unmapped_output_csv.expanduser().resolve()
        if args.unmapped_output_csv
        else resolve_path(
            cfg_get(
                config,
                "reimbursement_entity_linking.unmapped_output_csv",
                "../output/med_devices_reports/med_device_reimbursement_unmapped_companies.csv",
            ),
            base_dir=base_dir,
        )
    )
    policy = link_policy(config)
    include_missing_pit_metadata = allow_missing_static_pit_metadata(config)
    manual_rate_audit_csv = (
        args.manual_rate_audit_csv.expanduser().resolve()
        if args.manual_rate_audit_csv
        else resolve_path(policy.manual_rate_audit_csv, base_dir=base_dir)
        if policy.manual_rate_audit_csv
        else None
    )
    min_confidence = float(args.min_confidence or policy.min_auto_confidence)
    ticker_filter = {normalize_ticker(value) for value in str(args.tickers or "").split(",") if normalize_ticker(value)}
    asof = resolve_asof(args.asof)
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        run_id = start_run(conn, run_type="link_med_device_reimbursement_to_companies", input_path=config_path)
        try:
            company_meta = load_company_meta(conn, ticker_filter=ticker_filter, asof=asof)
            aliases = build_aliases(conn, ticker_filter=ticker_filter, policy=policy, asof=asof)
            policies = load_policy_rows(conn, policy=policy, max_rows=int(args.max_policies), asof=asof)
            matches = build_matches(conn, aliases, policies, min_confidence=min_confidence, asof=asof)
            matches.extend(
                build_descriptor_code_matches(
                    conn,
                    company_meta,
                    policy=policy,
                    min_confidence=min_confidence,
                    asof=asof,
                )
            )
            if policy.override_csv:
                matches.extend(
                    load_override_matches(
                        conn,
                        resolve_path(policy.override_csv, base_dir=base_dir),
                        company_meta,
                        policy=policy,
                        min_confidence=min_confidence,
                        asof=asof,
                        include_missing_pit_metadata=include_missing_pit_metadata,
                    )
            )
            manual_rate_count = 0
            manual_rate_audit_rows: list[dict[str, Any]] = []
            if policy.manual_rate_csv:
                manual_rate_count = load_manual_rate_rows(
                    conn,
                    resolve_path(policy.manual_rate_csv, base_dir=base_dir),
                    policy=policy,
                    asof=asof,
                    include_missing_pit_metadata=include_missing_pit_metadata,
                    audit_rows=manual_rate_audit_rows,
                )
            matches = dedupe_matches(matches)
            if policy.replace_existing_mappings:
                with conn:
                    clear_existing_mappings(conn, sorted(company_meta), policy=policy)
                    policy_count, code_count = upsert_matches(conn, matches)
            else:
                with conn:
                    policy_count, code_count = upsert_matches(conn, matches)
            write_csv(output_csv, matches)
            mapped_company_ids = load_mapped_company_ids(conn, sorted(company_meta), policy=policy)
            resolved_classification_csv = (
                resolve_path(policy.resolved_classification_csv, base_dir=base_dir)
                if policy.resolved_classification_csv
                else None
            )
            resolved_no_code_company_ids = load_resolved_no_code_company_ids(
                resolved_classification_csv,
                company_meta,
                asof=asof,
                include_missing_pit_metadata=include_missing_pit_metadata,
            )
            write_unmapped_csv(
                unmapped_output_csv,
                company_meta,
                mapped_company_ids,
                resolved_no_code_company_ids=resolved_no_code_company_ids,
            )
            if manual_rate_audit_csv is not None:
                write_manual_rate_audit_csv(manual_rate_audit_csv, manual_rate_audit_rows)
            message = (
                f"policies_scanned={len(policies)} aliases={len(aliases)} matches={len(matches)} "
                f"policy_maps={policy_count} code_maps={code_count} mapped_companies={len(mapped_company_ids)} output={output_csv} "
                f"manual_rates={manual_rate_count} manual_rate_audit={manual_rate_audit_csv or ''} "
                f"resolved_no_code={len(resolved_no_code_company_ids)} unmapped_output={unmapped_output_csv}"
            )
            finish_run(conn, run_id=run_id, status="success", row_count=len(matches), message=message)
            LOGGER.info("Reimbursement linking complete: %s", message)
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()
