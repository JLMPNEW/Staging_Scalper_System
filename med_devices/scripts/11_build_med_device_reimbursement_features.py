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
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.db import connect, finish_run, init_db, quote_identifier, start_run, utc_now  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402
from med_devices.core.point_in_time import row_is_effective_asof, warn_pit_invariant_violations  # noqa: E402
from med_devices.core.text_norm import normalize_org_name, normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("build_med_device_reimbursement_features")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
FIELDNAMES = [
    "asof_date",
    "company_id",
    "ticker",
    "company_name",
    "score",
    "coverage_clarity_score",
    "payment_adequacy_score",
    "policy_evidence_count",
    "company_mention_count",
    "mapped_product_code_count",
    "reimbursement_code_count",
    "rate_row_count",
    "billing_category",
    "payment_rate_status",
    "primary_payment_file",
    "regional_mac_name",
    "regional_payment_rate",
    "regional_rate_status",
    "reimbursement_status",
    "direct_code_evidence",
    "payment_rate_evidence",
    "coverage_policy_evidence",
    "procedure_bundled_flag",
    "capital_equipment_flag",
    "diagnostics_lab_flag",
    "unknown_reimbursement_flag",
    "hard_red_flag",
    "hard_red_flag_reasons",
    "review_reason",
]


@dataclass(frozen=True)
class Company:
    company_id: int
    ticker: str
    company_name: str


@dataclass(frozen=True)
class ReimbursementPolicy:
    source_ids: list[str]
    no_data_score: float
    no_data_coverage_clarity_score: float
    no_data_payment_adequacy_score: float
    company_mention_score: float
    policy_evidence_score: float
    rate_evidence_score: float
    coverage_weight: float
    payment_weight: float
    mention_count_boost_per_hit: float
    mention_count_boost_cap: float
    low_confidence_hard_flag: bool
    use_fallback_policy_scan_when_unmapped: bool
    valid_no_rate_statuses: set[str]
    zip_mac_csv: str = ""
    billing_zip: str = ""


@dataclass
class ReimbursementFeatureRow:
    asof_date: str
    company_id: int
    ticker: str
    company_name: str
    score: float = 0.0
    coverage_clarity_score: float = 0.0
    payment_adequacy_score: float = 0.0
    policy_evidence_count: int = 0
    company_mention_count: int = 0
    mapped_product_code_count: int = 0
    reimbursement_code_count: int = 0
    rate_row_count: int = 0
    billing_category: str = ""
    payment_rate_status: str = ""
    primary_payment_file: str = ""
    regional_mac_name: str = ""
    regional_payment_rate: float | None = None
    regional_rate_status: str = ""
    reimbursement_status: str = "unknown"
    direct_code_evidence: int = 0
    payment_rate_evidence: int = 0
    coverage_policy_evidence: int = 0
    procedure_bundled_flag: int = 0
    capital_equipment_flag: int = 0
    diagnostics_lab_flag: int = 0
    unknown_reimbursement_flag: int = 1
    hard_red_flag: int = 0
    hard_red_flag_reasons: list[str] | None = None
    review_reason: str = ""
    payload: dict[str, Any] | None = None


@dataclass(frozen=True)
class PolicySearchRow:
    policy_id: str
    policy_type: str
    title: str
    related_codes: str
    payload_json: str


@dataclass(frozen=True)
class CompanyReimbursementEvidence:
    policy_evidence_count: int
    company_mention_count: int
    reimbursement_code_count: int
    rate_row_count: int
    matched_codes: set[str]
    matched_policy_ids: list[str]


@dataclass(frozen=True)
class ReimbursementClassification:
    ticker: str
    billing_category: str
    payment_rate_status: str
    primary_payment_file: str
    coverage_score: float | None = None
    payment_score: float | None = None
    review_reason: str = ""
    notes: str = ""


@dataclass(frozen=True)
class ZipMacRule:
    zip3_start: int
    zip3_end: int
    mac_name: str
    source: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build med-device reimbursement and market-access feature rows.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--asof", type=str, default="")
    parser.add_argument("--tickers", type=str, default="")
    parser.add_argument("--max-tickers", type=int, default=0)
    parser.add_argument("--billing-zip", type=str, default="")
    parser.add_argument("--include-historical-members", action="store_true")
    return parser.parse_args()


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def to_float(raw: object) -> float | None:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def cfg_float(config: dict[str, Any], dotted_key: str, default: float) -> float:
    value = to_float(cfg_get(config, dotted_key, default))
    if value is None:
        raise ValueError(f"Config value must be numeric: {dotted_key}")
    return value


def cfg_bool(config: dict[str, Any], dotted_key: str, default: bool) -> bool:
    raw = cfg_get(config, dotted_key, default)
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def reimbursement_policy(config: dict[str, Any], *, billing_zip_override: str = "") -> ReimbursementPolicy:
    source_ids_raw = cfg_get(config, "reimbursement_features.source_ids", ["cms_coverage_api", "cms_payment_files"])
    source_ids = (
        [str(value).strip() for value in source_ids_raw]
        if isinstance(source_ids_raw, list)
        else ["cms_coverage_api", "cms_payment_files"]
    )
    coverage_weight = cfg_float(config, "reimbursement_features.coverage_weight", 0.60)
    payment_weight = cfg_float(config, "reimbursement_features.payment_weight", 0.40)
    if abs((coverage_weight + payment_weight) - 1.0) > 0.0001:
        raise ValueError(
            f"reimbursement_features.coverage_weight ({coverage_weight}) + "
            f"payment_weight ({payment_weight}) must sum to 1.0, got {coverage_weight + payment_weight:.4f}"
        )
    return ReimbursementPolicy(
        source_ids=[source_id for source_id in source_ids if source_id],
        no_data_score=cfg_float(config, "reimbursement_features.no_data_score", 25.0),
        no_data_coverage_clarity_score=cfg_float(config, "reimbursement_features.no_data_coverage_clarity_score", 25.0),
        no_data_payment_adequacy_score=cfg_float(config, "reimbursement_features.no_data_payment_adequacy_score", 25.0),
        company_mention_score=cfg_float(config, "reimbursement_features.company_mention_score", 45.0),
        policy_evidence_score=cfg_float(config, "reimbursement_features.policy_evidence_score", 60.0),
        rate_evidence_score=cfg_float(config, "reimbursement_features.rate_evidence_score", 65.0),
        coverage_weight=coverage_weight,
        payment_weight=payment_weight,
        mention_count_boost_per_hit=cfg_float(config, "reimbursement_features.mention_count_boost_per_hit", 0.5),
        mention_count_boost_cap=cfg_float(config, "reimbursement_features.mention_count_boost_cap", 8.0),
        low_confidence_hard_flag=cfg_bool(config, "reimbursement_features.low_confidence_hard_flag", False),
        use_fallback_policy_scan_when_unmapped=cfg_bool(
            config,
            "reimbursement_features.use_fallback_policy_scan_when_unmapped",
            True,
        ),
        valid_no_rate_statuses={
            str(value).strip().lower()
            for value in cfg_get(config, "reimbursement_features.valid_no_rate_statuses", [])
            if str(value).strip()
        },
        zip_mac_csv=str(cfg_get(config, "reimbursement_features.zip_mac_csv", "") or "").strip(),
        billing_zip=billing_zip_override.strip()
        or str(cfg_get(config, "reimbursement_features.default_billing_zip", "") or "").strip(),
    )


def latest_asof(conn: Any) -> str:
    row = conn.execute("SELECT MAX(asof_date) AS asof_date FROM feature_financial_valuation").fetchone()
    asof = str(row["asof_date"] or "") if row is not None else ""
    return asof or datetime.now(timezone.utc).date().isoformat()


def allow_missing_static_pit_metadata(config: dict[str, Any]) -> bool:
    return str(cfg_get(config, "historical_backfill.allow_missing_static_pit_metadata", False)).strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
        "on",
    }


def table_exists(conn: Any, table_name: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table_name,)).fetchone()
    return row is not None


def table_columns(conn: Any, table_name: str) -> set[str]:
    if not table_exists(conn, table_name):
        return set()
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({quote_identifier(table_name)})").fetchall()}


def load_companies(
    conn: Any,
    *,
    asof: str,
    ticker_filter: set[str],
    max_tickers: int,
    include_historical_members: bool,
) -> list[Company]:
    rows = conn.execute(
        """
        SELECT company_id, ticker, company_name
        FROM dim_company c
        WHERE (c.is_active = 1 AND EXISTS (
                SELECT 1
                FROM dim_company_model_taxonomy t
                WHERE t.company_id = c.company_id
                  AND t.model_family = 'med_devices'
                  AND (
                        (NULLIF(t.valid_from, '') IS NOT NULL AND SUBSTR(t.valid_from, 1, 10) <= ?)
                        OR (
                            NULLIF(t.valid_from, '') IS NULL
                            AND (NULLIF(t.reviewed_at, '') IS NULL OR SUBSTR(t.reviewed_at, 1, 10) < ?)
                        )
                  )
                  AND (NULLIF(t.valid_to, '') IS NULL OR SUBSTR(t.valid_to, 1, 10) >= ?)
           ))
           OR (? = 1 AND EXISTS (
                SELECT 1
                FROM dim_universe_membership m
                WHERE m.company_id = c.company_id
                  AND m.model_family = 'med_devices'
                  AND m.point_in_time_flag = 1
                  AND m.start_date <= ?
                  AND (m.end_date IS NULL OR m.end_date >= ?)
           ))
        ORDER BY ticker
        """,
        (asof, asof, asof, 1 if include_historical_members else 0, asof, asof),
    ).fetchall()
    out: list[Company] = []
    for row in rows:
        ticker = normalize_ticker(row["ticker"])
        if ticker_filter and ticker not in ticker_filter:
            continue
        out.append(Company(int(row["company_id"]), ticker, str(row["company_name"] or "")))
        if max_tickers > 0 and len(out) >= max_tickers:
            break
    return out


def row_get(row: dict[str, str], *keys: str) -> str:
    lowered = {str(key).strip().lower(): str(value or "") for key, value in row.items()}
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
        value = lowered.get(key.lower())
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def load_company_classifications(
    path: Path | None,
    *,
    asof: date | str | None = None,
    include_missing_pit_metadata: bool = False,
) -> dict[str, ReimbursementClassification]:
    if path is None or not path.exists():
        return {}
    out: dict[str, ReimbursementClassification] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            warn_pit_invariant_violations(
                row, context="reimbursement_classification_csv", logger=LOGGER, require_reviewed_at=True
            )
            if not row_is_effective_asof(row, asof, include_missing=include_missing_pit_metadata):
                continue
            ticker = normalize_ticker(row_get(row, "ticker", "symbol"))
            if not ticker:
                continue
            if ticker in out:
                LOGGER.warning("Duplicate ticker in classification CSV; later row overwrites earlier: %s", ticker)
            out[ticker] = ReimbursementClassification(
                ticker=ticker,
                billing_category=row_get(row, "billing_category", "category"),
                payment_rate_status=row_get(row, "payment_rate_status", "payment_status"),
                primary_payment_file=row_get(row, "primary_payment_file", "payment_file", "cms_file"),
                coverage_score=to_float(row_get(row, "coverage_score")),
                payment_score=to_float(row_get(row, "payment_score")),
                review_reason=row_get(row, "review_reason"),
                notes=row_get(row, "notes", "note"),
            )
    return out


def parse_zip3(raw: object) -> int | None:
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    if len(digits) < 3:
        return None
    return int(digits[:3])


def load_zip_mac_rules(path: Path | None) -> list[ZipMacRule]:
    if path is None or not path.exists():
        return []
    rules: list[ZipMacRule] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            start = parse_zip3(row_get(row, "zip3_start", "zip_prefix", "zip3"))
            end = parse_zip3(row_get(row, "zip3_end", "zip_prefix", "zip3"))
            mac_name = row_get(row, "mac_name", "mac")
            if start is None or end is None or not mac_name:
                continue
            rules.append(
                ZipMacRule(
                    zip3_start=min(start, end),
                    zip3_end=max(start, end),
                    mac_name=mac_name,
                    source=row_get(row, "source"),
                )
            )
    return rules


def mac_for_zip(zip_code: str, rules: list[ZipMacRule]) -> str:
    zip3 = parse_zip3(zip_code)
    if zip3 is None:
        return ""
    for rule in rules:
        if rule.zip3_start <= zip3 <= rule.zip3_end:
            return rule.mac_name
    return ""


def regional_rate_for_codes(
    conn: Any,
    *,
    source_ids: list[str],
    codes: set[str],
    billing_zip: str,
    zip_mac_rules: list[ZipMacRule],
    asof: str,
) -> dict[str, Any]:
    if not billing_zip:
        return {}
    mac_name = mac_for_zip(billing_zip, zip_mac_rules)
    if not mac_name:
        return {"regional_rate_status": "zip_mac_not_found", "billing_zip": billing_zip}
    if not source_ids or not codes:
        return {"regional_rate_status": "no_matched_codes", "billing_zip": billing_zip, "regional_mac_name": mac_name}
    source_placeholders = ",".join("?" for _ in source_ids)
    code_placeholders = ",".join("?" for _ in codes)
    rows = conn.execute(
        f"""
        SELECT c.code, r.payment_rate, r.payment_system, r.effective_date, r.locality
        FROM fact_reimbursement_rate r
        JOIN dim_reimbursement_code c
          ON c.reimbursement_code_id = r.reimbursement_code_id
        WHERE r.source_id IN ({source_placeholders})
          AND c.code IN ({code_placeholders})
          AND (
                NULLIF(c.effective_date, '') IS NULL
                OR SUBSTR(c.effective_date, 1, 10) <= ?
              )
          AND (
                NULLIF(c.termination_date, '') IS NULL
                OR SUBSTR(c.termination_date, 1, 10) >= ?
              )
          AND LOWER(COALESCE(r.locality, '')) = LOWER(?)
          AND r.payment_rate IS NOT NULL
          AND (
                NULLIF(r.effective_date, '') IS NULL
                OR SUBSTR(r.effective_date, 1, 10) <= ?
              )
        ORDER BY COALESCE(r.effective_date, '') DESC, r.reimbursement_rate_id DESC
        """,
        [*source_ids, *sorted(codes), asof, asof, mac_name, asof],
    ).fetchall()
    if not rows:
        return {
            "regional_rate_status": "local_mac_rate_not_found",
            "billing_zip": billing_zip,
            "regional_mac_name": mac_name,
        }
    row = rows[0]
    return {
        "regional_rate_status": "local_mac_rate_found",
        "billing_zip": billing_zip,
        "regional_mac_name": mac_name,
        "regional_payment_rate": to_float(row["payment_rate"]),
        "regional_code": str(row["code"] or ""),
        "regional_payment_system": str(row["payment_system"] or ""),
        "regional_effective_date": str(row["effective_date"] or ""),
    }


def source_row_counts(conn: Any, source_ids: list[str], *, asof: str | None = None) -> tuple[int, int, int]:
    if not source_ids:
        return 0, 0, 0
    placeholders = ",".join("?" for _ in source_ids)
    policies = conn.execute(
        f"""
        SELECT COUNT(*) AS n
        FROM fact_reimbursement_policy
        WHERE source_id IN ({placeholders})
          AND (? IS NULL OR NULLIF(effective_date, '') IS NULL OR SUBSTR(effective_date, 1, 10) <= ?)
          AND (? IS NULL OR NULLIF(retirement_date, '') IS NULL OR SUBSTR(retirement_date, 1, 10) >= ?)
        """,
        [*source_ids, asof, asof, asof, asof],
    ).fetchone()
    codes = conn.execute(
        f"""
        SELECT COUNT(*) AS n
        FROM dim_reimbursement_code
        WHERE source_id IN ({placeholders})
          AND (? IS NULL OR NULLIF(effective_date, '') IS NULL OR SUBSTR(effective_date, 1, 10) <= ?)
          AND (? IS NULL OR NULLIF(termination_date, '') IS NULL OR SUBSTR(termination_date, 1, 10) >= ?)
        """,
        [*source_ids, asof, asof, asof, asof],
    ).fetchone()
    rates = conn.execute(
        f"""
        SELECT COUNT(*) AS n
        FROM fact_reimbursement_rate
        WHERE source_id IN ({placeholders})
          AND (? IS NULL OR NULLIF(effective_date, '') IS NULL OR SUBSTR(effective_date, 1, 10) <= ?)
        """,
        [*source_ids, asof, asof],
    ).fetchone()
    return int(policies["n"] or 0), int(codes["n"] or 0), int(rates["n"] or 0)


def mapped_reimbursement_row_count(conn: Any, source_ids: list[str]) -> int:
    if not source_ids:
        return 0
    placeholders = ",".join("?" for _ in source_ids)
    policy_row = conn.execute(
        f"SELECT COUNT(*) AS n FROM map_company_reimbursement_policy WHERE source_id IN ({placeholders})",
        source_ids,
    ).fetchone()
    code_row = conn.execute(
        f"SELECT COUNT(*) AS n FROM map_company_reimbursement_code WHERE source_id IN ({placeholders})",
        source_ids,
    ).fetchone()
    return int(policy_row["n"] or 0) + int(code_row["n"] or 0)


def preflight_reimbursement_links(
    conn: Any, policy: ReimbursementPolicy, *, require_links: bool, asof: str | None = None
) -> None:
    if not require_links:
        return
    policy_count, code_count, rate_count = source_row_counts(conn, policy.source_ids, asof=asof)
    if policy_count + code_count + rate_count <= 0:
        return
    mapping_count = mapped_reimbursement_row_count(conn, policy.source_ids)
    if mapping_count <= 0:
        raise RuntimeError(
            "CMS reimbursement source rows exist but company reimbursement mappings are empty; run scripts 14 and 15 before script 11."
        )


def load_policy_search_rows(conn: Any, source_ids: list[str], *, asof: str | None = None) -> list[PolicySearchRow]:
    if not source_ids:
        return []
    placeholders = ",".join("?" for _ in source_ids)
    rows = conn.execute(
        f"""
        SELECT policy_id, policy_type, title, related_codes, payload_json
        FROM fact_reimbursement_policy
        WHERE source_id IN ({placeholders})
          AND (? IS NULL OR NULLIF(effective_date, '') IS NULL OR SUBSTR(effective_date, 1, 10) <= ?)
          AND (? IS NULL OR NULLIF(retirement_date, '') IS NULL OR SUBSTR(retirement_date, 1, 10) >= ?)
        """,
        [*source_ids, asof, asof, asof, asof],
    ).fetchall()
    return [
        PolicySearchRow(
            policy_id=str(row["policy_id"] or ""),
            policy_type=str(row["policy_type"] or ""),
            title=str(row["title"] or ""),
            related_codes=str(row["related_codes"] or ""),
            payload_json=str(row["payload_json"] or ""),
        )
        for row in rows
    ]


def company_terms(company: Company) -> list[str]:
    norm = normalize_org_name(company.company_name)
    terms = [norm]
    ticker = normalize_ticker(company.ticker)
    if ticker:
        terms.append(ticker)
    stripped = re.sub(
        r"\b(INC|INCORPORATED|CORP|CORPORATION|PLC|LTD|LIMITED|LLC|NV|SA|AG|HOLDINGS|HOLDING|GROUP)\b",
        "",
        norm,
    )
    stripped = re.sub(r"\s+", " ", stripped).strip()
    if stripped and stripped not in terms:
        terms.append(stripped)
    return [term for term in terms if len(term) >= 3]


FDA_PRODUCT_CODE_DATE_COLUMNS = {
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


def fda_product_code_date_expr(available_columns: set[str], table: str) -> str:
    date_columns = [column for column in FDA_PRODUCT_CODE_DATE_COLUMNS.get(table, ()) if column in available_columns]
    if not date_columns:
        return ""
    quoted = [quote_identifier(column) for column in date_columns]
    return quoted[0] if len(quoted) == 1 else f"COALESCE({', '.join(quoted)})"


def mapped_product_codes(conn: Any, company_id: int, *, asof: str) -> set[str]:
    codes: set[str] = set()
    for table in FDA_PRODUCT_CODE_DATE_COLUMNS:
        columns = table_columns(conn, table)
        if "product_code" not in columns:
            continue
        date_expr = fda_product_code_date_expr(columns, table)
        if not date_expr:
            LOGGER.warning("Skipping FDA product-code scan without PIT date column: table=%s", table)
            continue
        rows = conn.execute(
            f"""
            SELECT DISTINCT product_code
            FROM {quote_identifier(table)}
            WHERE company_id = ?
              AND COALESCE(product_code, '') != ''
              AND (
                    NULLIF({date_expr}, '') IS NULL
                    OR SUBSTR({date_expr}, 1, 10) <= ?
                  )
            """,
            (company_id, asof),
        ).fetchall()
        codes.update(str(row["product_code"] or "").strip() for row in rows if str(row["product_code"] or "").strip())
    return codes


CODE_TOKEN_RE = re.compile(r"\b(?:[A-Z]\d{4}|\d{5})\b")


def parse_related_codes(raw: object) -> set[str]:
    text = str(raw or "").upper()
    return {match.group(0) for match in CODE_TOKEN_RE.finditer(text)}


def policy_evidence(
    policy_rows: list[PolicySearchRow], company: Company, product_codes: set[str]
) -> tuple[int, int, set[str], list[str]]:
    if not policy_rows:
        return 0, 0, set(), []
    terms = [term.lower() for term in company_terms(company)]
    product_terms = [code.lower() for code in product_codes]
    mention_count = 0
    evidence_count = 0
    matched_codes: set[str] = set()
    matched_policy_ids: list[str] = []
    for row in policy_rows:
        haystack = " ".join([row.title, row.related_codes, row.payload_json]).lower()
        company_hit = any(term and term in haystack for term in terms)
        product_hit = any(term and term in haystack for term in product_terms)
        if company_hit:
            mention_count += 1
        if product_hit:
            evidence_count += 1
            matched_codes.update(parse_related_codes(row.related_codes))
            matched_policy_ids.append(row.policy_id or f"{row.policy_type}:{row.title}"[:120])
    return evidence_count, mention_count, matched_codes, matched_policy_ids


def rate_count_for_codes(conn: Any, source_ids: list[str], codes: set[str], *, asof: str) -> int:
    if not source_ids or not codes:
        return 0
    source_placeholders = ",".join("?" for _ in source_ids)
    code_placeholders = ",".join("?" for _ in codes)
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS n
        FROM fact_reimbursement_rate r
        JOIN dim_reimbursement_code c
          ON c.reimbursement_code_id = r.reimbursement_code_id
        WHERE r.source_id IN ({source_placeholders})
          AND c.code IN ({code_placeholders})
          AND (
                NULLIF(c.effective_date, '') IS NULL
                OR SUBSTR(c.effective_date, 1, 10) <= ?
              )
          AND (
                NULLIF(c.termination_date, '') IS NULL
                OR SUBSTR(c.termination_date, 1, 10) >= ?
              )
          AND (
                NULLIF(r.effective_date, '') IS NULL
                OR SUBSTR(r.effective_date, 1, 10) <= ?
              )
        """,
        [*source_ids, *sorted(codes), asof, asof, asof],
    ).fetchone()
    return int(row["n"] or 0) if row is not None else 0


def load_mapped_reimbursement_evidence(
    conn: Any,
    source_ids: list[str],
    *,
    asof: str,
) -> dict[int, CompanyReimbursementEvidence]:
    """Load current company-to-reimbursement mappings and PIT-filter effective-dated rate rows.

    The map_company_reimbursement_* tables are current-state link tables today; they do not
    carry valid_from/created_at metadata. Historical OOS protection therefore depends on
    rebuilding those mappings from PIT-filtered static CSVs before a strict backfill.
    """
    if not source_ids:
        return {}
    source_placeholders = ",".join("?" for _ in source_ids)
    policy_rows = conn.execute(
        f"""
        SELECT m.company_id, p.policy_id, p.policy_type, p.title, m.mapping_method
        FROM map_company_reimbursement_policy m
        JOIN fact_reimbursement_policy p
          ON p.reimbursement_policy_id = m.reimbursement_policy_id
         AND (
                NULLIF(p.effective_date, '') IS NULL
                OR SUBSTR(p.effective_date, 1, 10) <= ?
             )
         AND (
                NULLIF(p.retirement_date, '') IS NULL
                OR SUBSTR(p.retirement_date, 1, 10) >= ?
             )
        WHERE m.source_id IN ({source_placeholders})
        """,
        [asof, asof, *source_ids],
    ).fetchall()
    code_rows = conn.execute(
        f"""
        SELECT m.company_id, m.reimbursement_code_id, c.code
        FROM map_company_reimbursement_code m
        JOIN dim_reimbursement_code c
          ON c.reimbursement_code_id = m.reimbursement_code_id
         AND (
                NULLIF(c.effective_date, '') IS NULL
                OR SUBSTR(c.effective_date, 1, 10) <= ?
             )
         AND (
                NULLIF(c.termination_date, '') IS NULL
                OR SUBSTR(c.termination_date, 1, 10) >= ?
             )
        WHERE m.source_id IN ({source_placeholders})
        """,
        [asof, asof, *source_ids],
    ).fetchall()
    code_ids = sorted({int(row["reimbursement_code_id"]) for row in code_rows})
    rate_counts: dict[int, int] = {}
    if code_ids:
        code_placeholders = ",".join("?" for _ in code_ids)
        rate_rows = conn.execute(
            f"""
            SELECT reimbursement_code_id, COUNT(*) AS n
            FROM fact_reimbursement_rate
            WHERE source_id IN ({source_placeholders})
              AND reimbursement_code_id IN ({code_placeholders})
              AND (
                    NULLIF(effective_date, '') IS NULL
                    OR SUBSTR(effective_date, 1, 10) <= ?
                  )
            GROUP BY reimbursement_code_id
            """,
            [*source_ids, *code_ids, asof],
        ).fetchall()
        rate_counts = {int(row["reimbursement_code_id"]): int(row["n"] or 0) for row in rate_rows}
    by_company: dict[int, dict[str, Any]] = {}
    for row in policy_rows:
        company_id = int(row["company_id"])
        item = by_company.setdefault(
            company_id,
            {
                "policy_ids": set(),
                "mention_count": 0,
                "code_ids": set(),
                "codes": set(),
            },
        )
        policy_id = str(row["policy_id"] or f"{row['policy_type']}:{row['title']}"[:120])
        item["policy_ids"].add(policy_id)
        item["mention_count"] += 1
    for row in code_rows:
        company_id = int(row["company_id"])
        item = by_company.setdefault(
            company_id,
            {
                "policy_ids": set(),
                "mention_count": 0,
                "code_ids": set(),
                "codes": set(),
            },
        )
        code_id = int(row["reimbursement_code_id"])
        item["code_ids"].add(code_id)
        item["codes"].add(str(row["code"] or ""))
    out: dict[int, CompanyReimbursementEvidence] = {}
    for company_id, item in by_company.items():
        code_id_set = {int(value) for value in item["code_ids"]}
        out[company_id] = CompanyReimbursementEvidence(
            policy_evidence_count=len(item["policy_ids"]),
            company_mention_count=int(item["mention_count"]),
            reimbursement_code_count=len(item["codes"]),
            rate_row_count=sum(rate_counts.get(code_id, 0) for code_id in code_id_set),
            matched_codes={str(value) for value in item["codes"] if str(value)},
            matched_policy_ids=sorted(str(value) for value in item["policy_ids"])[:50],
        )
    return out


def blended_score(coverage_score: float, payment_score: float, *, policy: ReimbursementPolicy) -> float:
    total = max(1e-12, policy.coverage_weight + policy.payment_weight)
    return round((coverage_score * policy.coverage_weight + payment_score * policy.payment_weight) / total, 2)


RECOGNIZED_BUNDLED_PAYMENT_STATUSES = {
    "bundled_ipps",
    "bundled_opps",
    "bundled_opps_ipps",
    "cash_fee_sched",
    "cash_pay_or_out_of_pocket",
    "commercial_contract_no_cms",
    "commercial_vision_or_cash_pay",
    "component_pricing_no_direct_cms",
    "cpt_category_iii",
    "developmental_premarket_no_active_billing",
    "direct_hcpcs_and_bundled_hospital",
    "dental_or_cash_pay",
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
    "pharmacy_benefit_or_ncpdp",
    "pharma_overhead",
    "packaged_apc_asc",
    "packaged_status_n",
    "procedure_rate_mpfs",
    "state_public_health_bundle",
    "system_budget",
    "upstream_b2b_no_clinical_code",
    "veterinary_no_cms",
}
PROCEDURE_INDIRECT_PAYMENT_STATUSES = {
    "bundled_ipps",
    "bundled_opps",
    "bundled_opps_ipps",
    "cash_fee_sched",
    "cash_pay_or_out_of_pocket",
    "cpt_category_iii",
    "direct_hcpcs_and_bundled_hospital",
    "dental_or_cash_pay",
    "ntap_add_on_payment",
    "opo_cost_pass_through",
    "opps_asp_passthrough",
    "packaged_apc_asc",
    "packaged_status_n",
    "procedure_rate_mpfs",
}
CAPITAL_EQUIPMENT_PAYMENT_STATUSES = {
    "facility_budget",
    "hospital_overhead_budget",
    "imaging_provider_mpfs_rates",
    "system_budget",
}
DIAGNOSTICS_LAB_PAYMENT_STATUSES = {
    "laboratory_overhead_no_direct_code",
    "large_lab_clfs_array",
}
UPSTREAM_B2B_PAYMENT_STATUSES = {
    "component_pricing_no_direct_cms",
    "not_applicable_ruo_b2b",
    "upstream_b2b_no_clinical_code",
}
CONTRACTED_INDIRECT_PAYMENT_STATUSES = {
    "commercial_contract_no_cms",
    "commercial_vision_or_cash_pay",
    "enterprise_saas_no_clinical_code",
    "external_report_mismatch_guard",
    "jurisdictional_mac_priced",
    "pharmacy_benefit_or_ncpdp",
    "pharma_overhead",
    "state_public_health_bundle",
    "veterinary_no_cms",
}
DEVELOPMENTAL_NO_ACTIVE_BILLING_PAYMENT_STATUSES = {
    "developmental_premarket_no_active_billing",
}
PROCEDURE_BUNDLED_STATUS_TOKENS = {
    "bundled",
    "packaged",
    "procedure",
    "pass_through",
    "pass-through",
    "drg",
    "ipps",
    "ntap",
    "opo",
    "opps",
    "apc",
    "asc",
}
CAPITAL_EQUIPMENT_STATUS_TOKENS = {
    "capital",
    "capex",
    "equipment",
    "facility",
    "imaging",
    "overhead",
    "system",
    "utilization",
}
DIAGNOSTICS_LAB_STATUS_TOKENS = {
    "assay",
    "clfs",
    "cytopathology",
    "diagnostic",
    "diagnostics",
    "genetic",
    "lab",
    "laboratory",
    "molecular",
    "pathology",
    "sequencing",
}
RATE_EVIDENCE_RESOLVED_REVIEW_REASONS = {
    "code_mapping_without_payment_rate",
    "payment_rate_missing_clfs",
    "payment_rate_missing_dmepos",
    "payment_rate_missing_mpfs",
    "payment_rate_missing_opps",
}
REIMBURSEMENT_DQ_REASONS = {"cms_reimbursement_data_not_loaded", "code_mapping_without_payment_rate"}
PRODUCT_CODE_REVIEW_REASON_REPLACEMENTS = {
    "product_code_missing_clfs": "payment_rate_missing_clfs",
    "product_code_missing_dmepos": "payment_rate_missing_dmepos",
    "product_code_missing_mpfs": "payment_rate_missing_mpfs",
    "product_code_missing_opps": "payment_rate_missing_opps",
}


def has_any_token(text: str, tokens: set[str]) -> bool:
    normalized = text.lower()
    return any(token in normalized for token in tokens)


def finalize_reimbursement_evidence(row: ReimbursementFeatureRow) -> None:
    descriptor = " ".join(
        [
            row.billing_category,
            row.payment_rate_status,
            row.primary_payment_file,
            row.review_reason,
            row.regional_rate_status,
        ]
    ).lower()
    status = row.payment_rate_status.strip().lower()
    row.direct_code_evidence = int(row.reimbursement_code_count > 0)
    row.payment_rate_evidence = int(row.rate_row_count > 0 or row.regional_rate_status == "local_mac_rate_found")
    row.coverage_policy_evidence = int(row.policy_evidence_count > 0)
    row.procedure_bundled_flag = int(
        status in PROCEDURE_INDIRECT_PAYMENT_STATUSES or has_any_token(descriptor, PROCEDURE_BUNDLED_STATUS_TOKENS)
    )
    row.capital_equipment_flag = int(
        status in CAPITAL_EQUIPMENT_PAYMENT_STATUSES or has_any_token(descriptor, CAPITAL_EQUIPMENT_STATUS_TOKENS)
    )
    row.diagnostics_lab_flag = int(
        status in DIAGNOSTICS_LAB_PAYMENT_STATUSES or has_any_token(descriptor, DIAGNOSTICS_LAB_STATUS_TOKENS)
    )
    if row.review_reason == "cms_reimbursement_data_not_loaded":
        row.reimbursement_status = "cms_data_not_loaded"
    elif row.payment_rate_evidence:
        row.reimbursement_status = "direct_payment_evidence"
    elif row.diagnostics_lab_flag:
        row.reimbursement_status = "diagnostics_lab_pathway"
    elif row.capital_equipment_flag:
        row.reimbursement_status = "capital_equipment_indirect"
    elif status in UPSTREAM_B2B_PAYMENT_STATUSES:
        row.reimbursement_status = "upstream_b2b_or_not_direct"
    elif status in DEVELOPMENTAL_NO_ACTIVE_BILLING_PAYMENT_STATUSES:
        row.reimbursement_status = "developmental_or_premarket_no_active_billing"
    elif status in CONTRACTED_INDIRECT_PAYMENT_STATUSES:
        row.reimbursement_status = "contracted_or_indirect"
    elif row.procedure_bundled_flag:
        row.reimbursement_status = "procedure_bundled_or_indirect"
    elif row.direct_code_evidence:
        row.reimbursement_status = "direct_code_no_payment_rate"
    elif row.coverage_policy_evidence:
        row.reimbursement_status = "coverage_policy_only"
    elif row.company_mention_count > 0:
        row.reimbursement_status = "company_mention_only"
    else:
        row.reimbursement_status = "unknown"
    row.unknown_reimbursement_flag = int(row.reimbursement_status in {"cms_data_not_loaded", "unknown"})


def adjusted_classification_review_reason(
    row: ReimbursementFeatureRow,
    classification: ReimbursementClassification,
) -> str:
    reason = classification.review_reason.strip()
    normalized = reason.lower()
    if not normalized:
        return ""
    if row.rate_row_count > 0 and normalized in RATE_EVIDENCE_RESOLVED_REVIEW_REASONS:
        return ""
    if row.reimbursement_code_count > 0 and normalized in PRODUCT_CODE_REVIEW_REASON_REPLACEMENTS:
        if row.rate_row_count > 0:
            return ""
        return PRODUCT_CODE_REVIEW_REASON_REPLACEMENTS[normalized]
    return reason


def apply_company_classification(
    row: ReimbursementFeatureRow,
    classification: ReimbursementClassification | None,
    *,
    policy: ReimbursementPolicy,
) -> None:
    if classification is None:
        return
    row.billing_category = classification.billing_category
    row.payment_rate_status = classification.payment_rate_status
    row.primary_payment_file = classification.primary_payment_file
    status = classification.payment_rate_status.strip().lower()
    review_reason = adjusted_classification_review_reason(row, classification)
    missing_rate_reason = any(
        reason.strip() in RATE_EVIDENCE_RESOLVED_REVIEW_REASONS for reason in row.review_reason.split(";")
    )
    valid_no_rate_status = status in policy.valid_no_rate_statuses
    if status in RECOGNIZED_BUNDLED_PAYMENT_STATUSES and row.rate_row_count <= 0:
        if classification.coverage_score is not None:
            row.coverage_clarity_score = max(row.coverage_clarity_score, classification.coverage_score)
        if classification.payment_score is not None:
            row.payment_adequacy_score = max(row.payment_adequacy_score, classification.payment_score)
        row.score = blended_score(row.coverage_clarity_score, row.payment_adequacy_score, policy=policy)
        row.review_reason = review_reason
    elif valid_no_rate_status and missing_rate_reason:
        if classification.coverage_score is not None:
            row.coverage_clarity_score = max(row.coverage_clarity_score, classification.coverage_score)
        if classification.payment_score is not None:
            row.payment_adequacy_score = max(row.payment_adequacy_score, classification.payment_score)
        row.score = blended_score(row.coverage_clarity_score, row.payment_adequacy_score, policy=policy)
        row.review_reason = review_reason
    elif review_reason:
        row.review_reason = review_reason
        if classification.coverage_score is not None:
            row.coverage_clarity_score = max(row.coverage_clarity_score, classification.coverage_score)
        if classification.payment_score is not None:
            row.payment_adequacy_score = max(row.payment_adequacy_score, classification.payment_score)
        row.score = blended_score(row.coverage_clarity_score, row.payment_adequacy_score, policy=policy)


def build_rows(
    conn: Any,
    companies: list[Company],
    *,
    asof: str,
    policy: ReimbursementPolicy,
    classifications: dict[str, ReimbursementClassification] | None = None,
    zip_mac_rules: list[ZipMacRule] | None = None,
) -> list[ReimbursementFeatureRow]:
    policy_count, global_code_count, global_rate_count = source_row_counts(conn, policy.source_ids, asof=asof)
    policy_rows = load_policy_search_rows(conn, policy.source_ids, asof=asof)
    mapped_evidence = load_mapped_reimbursement_evidence(conn, policy.source_ids, asof=asof)
    use_mapped_evidence = bool(mapped_evidence)
    classifications = classifications or {}
    zip_mac_rules = zip_mac_rules or []
    rows: list[ReimbursementFeatureRow] = []
    for company in companies:
        product_codes = mapped_product_codes(conn, company.company_id, asof=asof)
        if company.company_id in mapped_evidence:
            evidence = mapped_evidence.get(
                company.company_id,
                CompanyReimbursementEvidence(0, 0, 0, 0, set(), []),
            )
            evidence_count = evidence.policy_evidence_count
            mention_count = evidence.company_mention_count
            matched_codes = evidence.matched_codes
            matched_policy_ids = evidence.matched_policy_ids
            matched_rate_count = evidence.rate_row_count
        elif not use_mapped_evidence or policy.use_fallback_policy_scan_when_unmapped:
            evidence_count, mention_count, matched_codes, matched_policy_ids = policy_evidence(
                policy_rows, company, product_codes
            )
            matched_rate_count = rate_count_for_codes(conn, policy.source_ids, matched_codes, asof=asof)
        else:
            evidence_count = 0
            mention_count = 0
            matched_codes = set()
            matched_policy_ids = []
            matched_rate_count = 0
        row = ReimbursementFeatureRow(
            asof_date=asof,
            company_id=company.company_id,
            ticker=company.ticker,
            company_name=company.company_name,
            policy_evidence_count=evidence_count,
            company_mention_count=mention_count,
            mapped_product_code_count=len(product_codes),
            reimbursement_code_count=len(matched_codes),
            rate_row_count=matched_rate_count,
        )
        reasons: list[str] = []
        if policy_count == 0 and global_code_count == 0 and global_rate_count == 0:
            row.coverage_clarity_score = policy.no_data_coverage_clarity_score
            row.payment_adequacy_score = policy.no_data_payment_adequacy_score
            row.score = policy.no_data_score
            row.review_reason = "cms_reimbursement_data_not_loaded"
        elif evidence_count > 0 and matched_codes:
            row.coverage_clarity_score = policy.policy_evidence_score
            row.payment_adequacy_score = (
                policy.rate_evidence_score if matched_rate_count > 0 else policy.no_data_payment_adequacy_score
            )
            row.score = blended_score(row.coverage_clarity_score, row.payment_adequacy_score, policy=policy)
        elif evidence_count > 0 or mention_count > 0:
            mention_boost = min(policy.mention_count_boost_cap, mention_count * policy.mention_count_boost_per_hit)
            row.coverage_clarity_score = min(policy.policy_evidence_score, policy.company_mention_score + mention_boost)
            row.payment_adequacy_score = policy.no_data_payment_adequacy_score
            row.score = blended_score(row.coverage_clarity_score, row.payment_adequacy_score, policy=policy)
            row.review_reason = "company_mentioned_without_product_code_mapping"
        elif matched_codes:
            row.coverage_clarity_score = policy.company_mention_score
            row.payment_adequacy_score = (
                policy.rate_evidence_score if matched_rate_count > 0 else policy.no_data_payment_adequacy_score
            )
            row.score = blended_score(row.coverage_clarity_score, row.payment_adequacy_score, policy=policy)
            if matched_rate_count <= 0:
                row.review_reason = "code_mapping_without_payment_rate"
        else:
            row.coverage_clarity_score = policy.no_data_coverage_clarity_score
            row.payment_adequacy_score = policy.no_data_payment_adequacy_score
            row.score = policy.no_data_score
            row.review_reason = "no_company_reimbursement_mapping"
        classification = classifications.get(company.ticker)
        apply_company_classification(row, classification, policy=policy)
        regional_rate = regional_rate_for_codes(
            conn,
            source_ids=policy.source_ids,
            codes=matched_codes,
            billing_zip=policy.billing_zip,
            zip_mac_rules=zip_mac_rules,
            asof=asof,
        )
        if regional_rate.get("regional_mac_name"):
            row.regional_mac_name = str(regional_rate.get("regional_mac_name") or "")
        row.regional_payment_rate = to_float(regional_rate.get("regional_payment_rate"))
        row.regional_rate_status = str(regional_rate.get("regional_rate_status") or "")
        if row.regional_rate_status == "local_mac_rate_found":
            row.payment_adequacy_score = max(row.payment_adequacy_score, policy.rate_evidence_score)
            row.score = blended_score(row.coverage_clarity_score, row.payment_adequacy_score, policy=policy)
        finalize_reimbursement_evidence(row)
        if row.review_reason:
            reasons.append(row.review_reason)
        row.hard_red_flag = 1 if policy.low_confidence_hard_flag and reasons else 0
        row.hard_red_flag_reasons = reasons if row.hard_red_flag else []
        row.payload = {
            "source": "reimbursement_feature_baseline",
            "source_ids": policy.source_ids,
            "mapping_source": "map_company_reimbursement" if use_mapped_evidence else "fallback_policy_scan",
            "source_row_counts": {
                "fact_reimbursement_policy": policy_count,
                "global_dim_reimbursement_code": global_code_count,
                "global_fact_reimbursement_rate": global_rate_count,
            },
            "mapped_product_codes": sorted(product_codes),
            "matched_reimbursement_codes": sorted(matched_codes),
            "matched_policy_ids": matched_policy_ids[:50],
            "evidence": {
                "policy_evidence_count": evidence_count,
                "company_mention_count": mention_count,
                "matched_rate_row_count": matched_rate_count,
            },
            "regional_rate_routing": regional_rate,
            "review_reason": row.review_reason,
            "company_reimbursement_classification": {
                "billing_category": row.billing_category,
                "payment_rate_status": row.payment_rate_status,
                "primary_payment_file": row.primary_payment_file,
                "notes": classification.notes if classification else "",
            },
            "reimbursement_evidence": {
                "reimbursement_status": row.reimbursement_status,
                "direct_code_evidence": row.direct_code_evidence,
                "payment_rate_evidence": row.payment_rate_evidence,
                "coverage_policy_evidence": row.coverage_policy_evidence,
                "procedure_bundled_flag": row.procedure_bundled_flag,
                "capital_equipment_flag": row.capital_equipment_flag,
                "diagnostics_lab_flag": row.diagnostics_lab_flag,
                "unknown_reimbursement_flag": row.unknown_reimbursement_flag,
            },
            "score_weights": {
                "coverage": policy.coverage_weight,
                "payment": policy.payment_weight,
                "mention_count_boost_per_hit": policy.mention_count_boost_per_hit,
                "mention_count_boost_cap": policy.mention_count_boost_cap,
            },
        }
        rows.append(row)
    return rows


def upsert_feature_rows(conn: Any, rows: list[ReimbursementFeatureRow]) -> int:
    if not rows:
        return 0
    now = utc_now()
    conn.executemany(
        """
        INSERT INTO feature_reimbursement(
            asof_date, company_id, ticker, company_name, score,
            coverage_clarity_score, payment_adequacy_score, policy_evidence_count,
            company_mention_count, mapped_product_code_count, reimbursement_code_count,
            rate_row_count, billing_category, payment_rate_status, primary_payment_file,
            regional_mac_name, regional_payment_rate, regional_rate_status,
            reimbursement_status, direct_code_evidence, payment_rate_evidence,
            coverage_policy_evidence, procedure_bundled_flag, capital_equipment_flag,
            diagnostics_lab_flag, unknown_reimbursement_flag,
            hard_red_flag, hard_red_flag_reasons, review_reason,
            payload_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(asof_date, company_id) DO UPDATE SET
            ticker = excluded.ticker,
            company_name = excluded.company_name,
            score = excluded.score,
            coverage_clarity_score = excluded.coverage_clarity_score,
            payment_adequacy_score = excluded.payment_adequacy_score,
            policy_evidence_count = excluded.policy_evidence_count,
            company_mention_count = excluded.company_mention_count,
            mapped_product_code_count = excluded.mapped_product_code_count,
            reimbursement_code_count = excluded.reimbursement_code_count,
            rate_row_count = excluded.rate_row_count,
            billing_category = excluded.billing_category,
            payment_rate_status = excluded.payment_rate_status,
            primary_payment_file = excluded.primary_payment_file,
            regional_mac_name = excluded.regional_mac_name,
            regional_payment_rate = excluded.regional_payment_rate,
            regional_rate_status = excluded.regional_rate_status,
            reimbursement_status = excluded.reimbursement_status,
            direct_code_evidence = excluded.direct_code_evidence,
            payment_rate_evidence = excluded.payment_rate_evidence,
            coverage_policy_evidence = excluded.coverage_policy_evidence,
            procedure_bundled_flag = excluded.procedure_bundled_flag,
            capital_equipment_flag = excluded.capital_equipment_flag,
            diagnostics_lab_flag = excluded.diagnostics_lab_flag,
            unknown_reimbursement_flag = excluded.unknown_reimbursement_flag,
            hard_red_flag = excluded.hard_red_flag,
            hard_red_flag_reasons = excluded.hard_red_flag_reasons,
            review_reason = excluded.review_reason,
            payload_json = excluded.payload_json,
            updated_at = excluded.updated_at
        """,
        [
            (
                row.asof_date,
                row.company_id,
                row.ticker,
                row.company_name,
                row.score,
                row.coverage_clarity_score,
                row.payment_adequacy_score,
                row.policy_evidence_count,
                row.company_mention_count,
                row.mapped_product_code_count,
                row.reimbursement_code_count,
                row.rate_row_count,
                row.billing_category,
                row.payment_rate_status,
                row.primary_payment_file,
                row.regional_mac_name,
                row.regional_payment_rate,
                row.regional_rate_status,
                row.reimbursement_status,
                row.direct_code_evidence,
                row.payment_rate_evidence,
                row.coverage_policy_evidence,
                row.procedure_bundled_flag,
                row.capital_equipment_flag,
                row.diagnostics_lab_flag,
                row.unknown_reimbursement_flag,
                row.hard_red_flag,
                ";".join(row.hard_red_flag_reasons or []),
                row.review_reason,
                json.dumps(row.payload or {}, ensure_ascii=True, sort_keys=True),
                now,
                now,
            )
            for row in rows
        ],
    )
    return len(rows)


def replace_data_quality_issues(conn: Any, rows: list[ReimbursementFeatureRow], *, asof: str) -> int:
    conn.execute(
        "DELETE FROM data_quality_issues WHERE table_name = ? AND asof_date = ?",
        ("feature_reimbursement", asof),
    )
    issue_rows: list[tuple[Any, ...]] = []
    now = utc_now()
    for row in rows:
        if not row.review_reason:
            continue
        if row.review_reason not in REIMBURSEMENT_DQ_REASONS:
            continue
        issue_rows.append(
            (
                asof,
                row.company_id,
                None,
                "feature_reimbursement",
                "score",
                row.review_reason,
                "warning",
                f"{row.ticker}: {row.review_reason}",
                now,
            )
        )
    if issue_rows:
        conn.executemany(
            """
            INSERT INTO data_quality_issues(
                asof_date, company_id, source_id, table_name, field_name, issue_type,
                severity, message, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            issue_rows,
        )
    return len(issue_rows)


def row_to_dict(row: ReimbursementFeatureRow) -> dict[str, Any]:
    out = {field: getattr(row, field) for field in FIELDNAMES if hasattr(row, field)}
    out["hard_red_flag_reasons"] = ";".join(row.hard_red_flag_reasons or [])
    return out


def write_csv(path: Path, rows: list[ReimbursementFeatureRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(row_to_dict(row) for row in rows)


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = (
        args.db.expanduser().resolve()
        if args.db
        else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    )
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(
            cfg_get(
                config,
                "reimbursement_features.output_csv",
                "../output/med_devices_reports/med_device_reimbursement_features.csv",
            ),
            base_dir=base_dir,
        )
    )
    policy = reimbursement_policy(config, billing_zip_override=args.billing_zip)
    include_missing_pit_metadata = allow_missing_static_pit_metadata(config)
    classification_raw = str(cfg_get(config, "reimbursement_features.company_classification_csv", "") or "").strip()
    zip_mac_rules = load_zip_mac_rules(
        resolve_path(policy.zip_mac_csv, base_dir=base_dir) if policy.zip_mac_csv else None
    )
    ticker_filter = {normalize_ticker(value) for value in str(args.tickers or "").split(",") if normalize_ticker(value)}
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        parsed_asof = parse_date(args.asof) if args.asof else parse_date(latest_asof(conn))
        if parsed_asof is None:
            raise ValueError(f"Invalid as-of date: {args.asof}")
        asof = parsed_asof.isoformat()
        classifications = load_company_classifications(
            resolve_path(classification_raw, base_dir=base_dir) if classification_raw else None,
            asof=asof,
            include_missing_pit_metadata=include_missing_pit_metadata,
        )
        companies = load_companies(
            conn,
            asof=asof,
            ticker_filter=ticker_filter,
            max_tickers=int(args.max_tickers),
            include_historical_members=bool(args.include_historical_members),
        )
        if not companies:
            raise ValueError("No active or point-in-time historical companies selected")
        run_id = start_run(conn, run_type="build_med_device_reimbursement_features", input_path=config_path)
        try:
            preflight_reimbursement_links(
                conn,
                policy,
                require_links=str(
                    cfg_get(config, "reimbursement_features.require_entity_linking_when_cms_loaded", True)
                )
                .strip()
                .lower()
                in {"1", "true", "yes", "y", "on"},
                asof=asof,
            )
            rows = build_rows(
                conn,
                companies,
                asof=asof,
                policy=policy,
                classifications=classifications,
                zip_mac_rules=zip_mac_rules,
            )
            upserted = upsert_feature_rows(conn, rows)
            issue_count = replace_data_quality_issues(conn, rows, asof=asof)
            write_csv(output_csv, rows)
            flagged = sum(1 for row in rows if row.hard_red_flag)
            message = f"asof={asof} rows={upserted} flagged={flagged} issues={issue_count} output={output_csv}"
            finish_run(conn, run_id=run_id, status="success", row_count=upserted, message=message)
            LOGGER.info("Reimbursement features complete: %s", message)
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    raise SystemExit(main())
