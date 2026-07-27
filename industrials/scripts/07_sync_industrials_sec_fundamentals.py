#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import html as html_lib
import json
import logging
import math
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, expand_env_vars, load_yaml, resolve_path  # noqa: E402
from industrials.core.db import connect, finish_run, init_db, start_run, utc_now  # noqa: E402
from industrials.core.logging_utils import configure_utc_logging  # noqa: E402
from industrials.core.reports import write_csv_atomic  # noqa: E402
from industrials.core.source_registry import load_source_registry, upsert_source_registry  # noqa: E402
from industrials.core.text_norm import normalize_cik, normalize_ticker  # noqa: E402
from industrials.machinery.disclosure_candidates import (  # noqa: E402
    PROSE_SOURCE_DETAIL,
    DisclosureCandidate,
    accepted_candidates,
    extract_machinery_prose_candidates,
    reconcile_machinery_disclosure_facts,
    resolve_machinery_disclosure_candidates,
    upsert_disclosure_candidates,
)
from industrials.machinery.disclosure_documents import (  # noqa: E402
    extract_document_text,
    filing_summary_document_name,
    filing_summary_report_documents,
)
from industrials.machinery.reporting_currency import apply_reporting_currencies  # noqa: E402


LOGGER = logging.getLogger("sync_industrials_sec_fundamentals")
_SEC_REQUEST_LOCK = threading.Lock()
_SEC_LAST_REQUEST_AT = 0.0
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
RUN_TYPE = "sync_industrials_sec_fundamentals"
RECENT_STUB_PROFILES = {"RECENT_IPO_DEVELOPMENT_STAGE", "RECENT_PUBLIC_STUB"}
FPI_HYBRID_PROFILES = {"FPI_HYBRID_STUB_LOADED", "FPI_HYBRID_LOADED"}
# Profiles that route through the SEC archive fallback and are allowed to carry
# intentionally low/partial coverage. Used both to decide whether to attempt the
# archive fallback and as the completeness whitelist for incremental resume.
ARCHIVE_FALLBACK_PROFILES = frozenset(
    {
        "SEC_RAW_ARCHIVE_REQUIRED",
        "RECENT_IPO_DEVELOPMENT_STAGE",
        "RECENT_PUBLIC_STUB",
        "SEC_20F_METADATA_ONLY",
        "FOREIGN_PRIVATE_ISSUER_ARCHIVE_REQUIRED",
        "FPI_HYBRID_STUB_LOADED",
        "FPI_HYBRID_LOADED",
    }
)
FULL_STATEMENT_ARCHIVE_HANDLING_TYPES = frozenset({"Ingestion_Gap_Pending"})
MACHINERY_EXTRA_CONCEPT_MAPPINGS = (
    {
        "taxonomy": "us-gaap",
        "concept_name": "ReceivablesNetCurrent",
        "canonical_metric": "accounts_receivable",
        "financial_statement": "balance_sheet",
        "period_type": "instant",
        "sign_policy": "as_reported",
        "priority": 15,
    },
    {
        "taxonomy": "us-gaap",
        "concept_name": "AccountsReceivableNet",
        "canonical_metric": "accounts_receivable",
        "financial_statement": "balance_sheet",
        "period_type": "instant",
        "sign_policy": "as_reported",
        "priority": 20,
    },
    {
        "taxonomy": "us-gaap",
        "concept_name": "AccountsNotesAndLoansReceivableNetCurrent",
        "canonical_metric": "accounts_receivable",
        "financial_statement": "balance_sheet",
        "period_type": "instant",
        "sign_policy": "as_reported",
        "priority": 25,
    },
    {
        "taxonomy": "us-gaap",
        "concept_name": "AccountsAndNotesReceivableNet",
        "canonical_metric": "accounts_receivable",
        "financial_statement": "balance_sheet",
        "period_type": "instant",
        "sign_policy": "as_reported",
        "priority": 30,
    },
    {
        "taxonomy": "us-gaap",
        "concept_name": "AccountsPayableTradeCurrent",
        "canonical_metric": "accounts_payable",
        "financial_statement": "balance_sheet",
        "period_type": "instant",
        "sign_policy": "as_reported",
        "priority": 15,
    },
    {
        "taxonomy": "us-gaap",
        "concept_name": "LongTermDebt",
        "canonical_metric": "debt_total",
        "financial_statement": "balance_sheet",
        "period_type": "instant",
        "sign_policy": "as_reported",
        "priority": 15,
    },
    {
        "taxonomy": "us-gaap",
        "concept_name": "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities",
        "canonical_metric": "debt_total",
        "financial_statement": "balance_sheet",
        "period_type": "instant",
        "sign_policy": "as_reported",
        "priority": 20,
    },
    {
        "taxonomy": "us-gaap",
        "concept_name": "LongTermDebtAndCapitalLeaseObligations",
        "canonical_metric": "debt_total",
        "financial_statement": "balance_sheet",
        "period_type": "instant",
        "sign_policy": "as_reported",
        "priority": 25,
    },
    {
        "taxonomy": "us-gaap",
        "concept_name": "PaymentsToAcquireProductiveAssets",
        "canonical_metric": "capex",
        "financial_statement": "cash_flow",
        "period_type": "duration",
        "sign_policy": "positive_abs",
        "priority": 20,
    },
    {
        "taxonomy": "us-gaap",
        "concept_name": "OtherDepreciationAndAmortization",
        "canonical_metric": "depreciation_and_amortization",
        "financial_statement": "cash_flow",
        "period_type": "duration",
        "sign_policy": "positive_abs",
        "priority": 20,
    },
    {
        "taxonomy": "us-gaap",
        "concept_name": "PaymentsToAcquireOtherPropertyPlantAndEquipment",
        "canonical_metric": "capex",
        "financial_statement": "cash_flow",
        "period_type": "duration",
        "sign_policy": "positive_abs",
        "priority": 25,
    },
    {
        "taxonomy": "us-gaap",
        "concept_name": "ProceedsFromIssuanceOfPrivatePlacement",
        "canonical_metric": "equity_issuance_proceeds",
        "financial_statement": "cash_flow",
        "period_type": "duration",
        "sign_policy": "positive_abs",
        "priority": 125,
    },
    {
        "taxonomy": "us-gaap",
        "concept_name": "ProceedsFromIssuanceOfRedeemableConvertiblePreferredStock",
        "canonical_metric": "equity_issuance_proceeds",
        "financial_statement": "cash_flow",
        "period_type": "duration",
        "sign_policy": "positive_abs",
        "priority": 130,
    },
    {
        "taxonomy": "us-gaap",
        "concept_name": "ProceedsFromIssuanceOfConvertiblePreferredStock",
        "canonical_metric": "equity_issuance_proceeds",
        "financial_statement": "cash_flow",
        "period_type": "duration",
        "sign_policy": "positive_abs",
        "priority": 135,
    },
    {
        "taxonomy": "us-gaap",
        "concept_name": "ProceedsFromIssuanceOfPreferredStockPreferenceStockAndWarrants",
        "canonical_metric": "equity_issuance_proceeds",
        "financial_statement": "cash_flow",
        "period_type": "duration",
        "sign_policy": "positive_abs",
        "priority": 140,
    },
    {
        "taxonomy": "us-gaap",
        "concept_name": "ProceedsFromIssuanceOfCommonLimitedPartnersUnits",
        "canonical_metric": "equity_issuance_proceeds",
        "financial_statement": "cash_flow",
        "period_type": "duration",
        "sign_policy": "positive_abs",
        "priority": 145,
    },
    {
        "taxonomy": "us-gaap",
        "concept_name": "ProceedsFromIssuanceOfPreferredLimitedPartnersUnits",
        "canonical_metric": "equity_issuance_proceeds",
        "financial_statement": "cash_flow",
        "period_type": "duration",
        "sign_policy": "positive_abs",
        "priority": 150,
    },
    {
        "taxonomy": "us-gaap",
        "concept_name": "ProceedsFromDebtMaturingInMoreThanThreeMonths",
        "canonical_metric": "debt_issuance_proceeds",
        "financial_statement": "cash_flow",
        "period_type": "duration",
        "sign_policy": "positive_abs",
        "priority": 185,
    },
    {
        "taxonomy": "us-gaap",
        "concept_name": "ProceedsFromShortTermDebtMaturingInMoreThanThreeMonths",
        "canonical_metric": "debt_issuance_proceeds",
        "financial_statement": "cash_flow",
        "period_type": "duration",
        "sign_policy": "positive_abs",
        "priority": 190,
    },
    {
        "taxonomy": "us-gaap",
        "concept_name": "ProceedsFromIssuanceOfLongTermDebtAndCapitalSecuritiesNet",
        "canonical_metric": "debt_issuance_proceeds",
        "financial_statement": "cash_flow",
        "period_type": "duration",
        "sign_policy": "positive_abs",
        "priority": 195,
    },
    {
        "taxonomy": "sec-text",
        "concept_name": "PretaxIncome",
        "canonical_metric": "pretax_income",
        "financial_statement": "income_statement",
        "period_type": "duration",
        "sign_policy": "as_reported",
        "priority": 200,
    },
    {
        "taxonomy": "sec-text",
        "concept_name": "IncomeTaxExpense",
        "canonical_metric": "income_tax_expense",
        "financial_statement": "income_statement",
        "period_type": "duration",
        "sign_policy": "as_reported",
        "priority": 200,
    },
    {
        "taxonomy": "sec-text",
        "concept_name": "Orders",
        "canonical_metric": "orders",
        "financial_statement": "orders",
        "period_type": "duration",
        "sign_policy": "positive_abs",
        "priority": 200,
    },
    {
        "taxonomy": "sec-text",
        "concept_name": "FundedBacklog",
        "canonical_metric": "funded_backlog",
        "financial_statement": "backlog",
        "period_type": "instant",
        "sign_policy": "positive_abs",
        "priority": 200,
    },
    {
        "taxonomy": "sec-text",
        "concept_name": "ReportedBacklog",
        "canonical_metric": "reported_backlog",
        "financial_statement": "backlog",
        "period_type": "instant",
        "sign_policy": "positive_abs",
        "priority": 200,
    },
    {
        "taxonomy": "sec-text",
        "concept_name": "RemainingPerformanceObligation",
        "canonical_metric": "remaining_performance_obligation",
        "financial_statement": "backlog",
        "period_type": "instant",
        "sign_policy": "positive_abs",
        "priority": 180,
    },
    {
        "taxonomy": "sec-footnote",
        "concept_name": "RemainingPerformanceObligation",
        "canonical_metric": "remaining_performance_obligation",
        "financial_statement": "backlog",
        "period_type": "instant",
        "sign_policy": "positive_abs",
        "priority": 40,
    },
    {
        "taxonomy": "sec-footnote",
        "concept_name": "RemainingPerformanceObligationCurrent",
        "canonical_metric": "rpo_current",
        "financial_statement": "backlog",
        "period_type": "instant",
        "sign_policy": "positive_abs",
        "priority": 40,
    },
    {
        "taxonomy": "sec-footnote",
        "concept_name": "ReportedBacklog",
        "canonical_metric": "reported_backlog",
        "financial_statement": "backlog",
        "period_type": "instant",
        "sign_policy": "positive_abs",
        "priority": 40,
    },
    {
        "taxonomy": "sec-footnote",
        "concept_name": "FundedBacklog",
        "canonical_metric": "funded_backlog",
        "financial_statement": "backlog",
        "period_type": "instant",
        "sign_policy": "positive_abs",
        "priority": 40,
    },
    {
        "taxonomy": "sec-footnote",
        "concept_name": "Orders",
        "canonical_metric": "orders",
        "financial_statement": "orders",
        "period_type": "duration",
        "sign_policy": "positive_abs",
        "priority": 40,
    },
)
REPORT_FIELDS = [
    "ticker",
    "cik",
    "company_name",
    "country",
    "status",
    "reporting_profile",
    "reporting_standard",
    "latest_filing_date",
    "latest_form_type",
    "filing_count",
    "raw_fact_count",
    "mapped_fact_count",
    "review_reason",
]
CACHE_HYDRATION_REPORT_FIELDS = [
    "ticker",
    "cik",
    "status",
    "archive_request_count",
    "cached_fact_candidate_count",
    "error",
]
FILING_CATALOG_REPORT_FIELDS = [
    "ticker",
    "cik",
    "status",
    "catalog_start_date",
    "catalog_end_date",
    "requested_forms",
    "cataloged_filing_count",
    "cataloged_form_counts_json",
    "relevant_history_file_count",
    "missing_history_cache_count",
    "missing_history_cache_files_json",
    "network_request_count",
    "error",
]

PROFILE_ACCEPTED_DATE_SQL = """
CASE
    WHEN COALESCE(accepted_at, '') GLOB '????-??-??*' THEN SUBSTR(accepted_at, 1, 10)
    WHEN COALESCE(accepted_at, '') GLOB '[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]*'
        THEN SUBSTR(accepted_at, 1, 4) || '-' || SUBSTR(accepted_at, 5, 2) || '-' || SUBSTR(accepted_at, 7, 2)
    ELSE COALESCE(NULLIF(filing_date, ''), '9999-12-31')
END
"""

XBRL_INSTANCE_NAMESPACE = "http://www.xbrl.org/2003/instance"
INLINE_XBRL_LOCAL_NAMES = {"nonfraction", "nonnumeric"}
ARCHIVE_EXCLUDED_SUFFIXES = (
    "_cal.xml",
    "_def.xml",
    "_lab.xml",
    "_pre.xml",
    ".xsd",
    "filingsummary.xml",
    "metalinks.json",
    "-index-headers.html",
    "-index.html",
)
ARCHIVE_ALLOWED_DOCUMENT_SUFFIXES = (".xml", ".xhtml", ".htm", ".html", ".txt")
ARCHIVE_PDF_SUFFIXES = (".pdf",)
TEXT_TABLE_SOURCE_DETAIL = "sec_archive_text_table"
XBRL_ARCHIVE_SOURCE_DETAIL = "sec_archive_xbrl"
FOOTNOTE_XBRL_SOURCE_DETAIL = "sec_archive_footnote_xbrl"
ARCHIVE_SOURCE_DETAILS = (
    XBRL_ARCHIVE_SOURCE_DETAIL,
    TEXT_TABLE_SOURCE_DETAIL,
    FOOTNOTE_XBRL_SOURCE_DETAIL,
    PROSE_SOURCE_DETAIL,
)


class SecRequestError(RuntimeError):
    def __init__(self, *, status_code: int, url: str, body: str) -> None:
        super().__init__(f"SEC request failed status={status_code} url={url} body={body[:200]}")
        self.status_code = status_code
        self.url = url
        self.body = body


@dataclass(frozen=True)
class ReportingOverride:
    ticker: str
    handling_type: str
    parent_ticker: str
    skip_sec_network: bool
    reporting_profile: str
    reporting_standard: str
    fallback_status: str
    financial_confidence: float
    usable_xbrl_flag: int
    review_reason: str
    notes: str


@dataclass(frozen=True)
class ArchiveFact:
    taxonomy: str
    concept_name: str
    unit: str
    value: float
    period_start: str
    period_end: str
    frame: str
    decimals: str
    payload_json: str
    source_detail: str = XBRL_ARCHIVE_SOURCE_DETAIL


def prose_candidate_facts(
    candidates: list[DisclosureCandidate],
    *,
    document_name: str,
) -> list[ArchiveFact]:
    return [
        ArchiveFact(
            taxonomy="sec-text",
            concept_name=candidate.concept_name,
            unit=candidate.unit,
            value=candidate.value,
            period_start=candidate.period_start,
            period_end=candidate.period_end,
            frame=(f"prose:{document_name}:{candidate.block_index}:{candidate.concept_name}:{candidate.period_end}"),
            decimals="",
            payload_json=candidate.payload_json(document_name=document_name),
            source_detail=PROSE_SOURCE_DETAIL,
        )
        for candidate in accepted_candidates(candidates)
    ]


@dataclass(frozen=True)
class ContextInfo:
    period_start: str
    period_end: str
    context_id: str
    dimensions: tuple[tuple[str, str], ...]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync SEC submissions and companyfacts for an industrials model family."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--model-family", default="", help="Industrials model family to sync, e.g. defense.")
    parser.add_argument("--tickers", default="", help="Optional comma-separated ticker filter.")
    parser.add_argument("--max-tickers", type=int, default=0, help="Optional cap for smoke tests.")
    parser.add_argument(
        "--include-historical", action="store_true", help="Also sync non-current historical/delisted members."
    )
    parser.add_argument("--force", action="store_true", help="Ignore cached JSON and refetch.")
    parser.add_argument(
        "--incremental",
        action="store_true",
        help=(
            "Daily refresh mode: refetch submissions metadata, then process "
            "companyfacts/archive only for tickers with new filings or no existing SEC financial state."
        ),
    )
    parser.add_argument(
        "--force-submissions",
        action="store_true",
        help="Refetch submissions metadata without forcing companyfacts/archive caches.",
    )
    parser.add_argument(
        "--force-companyfacts",
        action="store_true",
        help="Refetch companyfacts JSON without forcing archive document caches.",
    )
    parser.add_argument("--force-archive", action="store_true", help="Refetch SEC archive index/document caches.")
    parser.add_argument(
        "--archive-max-filings-per-ticker",
        type=int,
        default=None,
        help=("Override sec_archive.max_filings_per_ticker for this run; 0 means unlimited."),
    )
    parser.add_argument(
        "--archive-max-documents-per-filing",
        type=int,
        default=None,
        help=("Override sec_archive.max_documents_per_filing for this run; 0 means unlimited."),
    )
    parser.add_argument(
        "--archive-scan-all-documents",
        action="store_true",
        help=(
            "Do not stop an archive filing after the first document with "
            "mapped facts. Intended for explicit research hydration only."
        ),
    )
    parser.add_argument(
        "--archive-cache-only",
        action="store_true",
        help=(
            "Hydrate SEC archive files without replacing canonical/archive "
            "financial facts. Requires --archive-selected and --tickers."
        ),
    )
    parser.add_argument(
        "--filing-catalog-cache-only",
        action="store_true",
        help=(
            "Populate fact_sec_filing only from cached SEC submissions JSON. "
            "No network request or financial-fact write is allowed. Requires "
            "--tickers."
        ),
    )
    parser.add_argument(
        "--filing-catalog-forms",
        default="",
        help=("Comma-separated form types for --filing-catalog-cache-only; defaults to the configured SEC form list."),
    )
    parser.add_argument(
        "--filing-catalog-start-date",
        default="",
        help=("Inclusive filing-date lower bound for cache-only cataloging; defaults to sec_fundamentals.start_date."),
    )
    parser.add_argument(
        "--archive-cache-workers",
        type=int,
        default=1,
        help=(
            "Ticker-level workers for --archive-cache-only. Network requests "
            "remain governed by one process-wide SEC throttle."
        ),
    )
    parser.add_argument(
        "--filing-catalog-fetch-missing",
        action="store_true",
        help=(
            "Allow --filing-catalog-cache-only to fetch only missing root or "
            "history submissions JSON metadata. CompanyFacts and archive "
            "documents remain disabled."
        ),
    )
    parser.add_argument(
        "--archive-document-keywords",
        default="",
        help=("Comma-separated report-description keywords used to hydrate specialized-metric FilingSummary reports."),
    )
    parser.add_argument(
        "--archive-accession-scope-csv",
        type=Path,
        default=None,
        help=("Optional ticker/accession CSV restricting cache-only hydration to the exact missing accession set."),
    )
    parser.add_argument(
        "--archive-bootstrap",
        action="store_true",
        help="Process every configured family member through SEC archives while reusing valid cached documents.",
    )
    parser.add_argument(
        "--archive-selected",
        action="store_true",
        help=(
            "Process every explicitly --tickers-selected member through SEC "
            "archives without enabling a full-family daily archive refresh."
        ),
    )
    parser.add_argument(
        "--allow-partial", action="store_true", help="Finish with success when individual tickers fail."
    )
    parser.add_argument(
        "--asof",
        default="",
        help=(
            "Evaluation asof date (YYYY-MM-DD) used to select the effective reporting-override "
            "rows via their valid_from column; defaults to the current UTC date."
        ),
    )
    parser.add_argument(
        "--profiles-only",
        action="store_true",
        help="Rebuild dated reporting-profile snapshots from stored SEC filings/facts without network access.",
    )
    parser.add_argument(
        "--profiles-all-members",
        action="store_true",
        help=(
            "With --profiles-only --include-historical, rebuild stored-data coverage "
            "for every family member instead of only members alive on --asof."
        ),
    )
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--skip-source-registry", action="store_true")
    return parser.parse_args()


def parse_ticker_list(raw: object) -> list[str]:
    values = raw if isinstance(raw, list) else str(raw or "").split(",")
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        ticker = normalize_ticker(value)
        if ticker and ticker not in seen:
            out.append(ticker)
            seen.add(ticker)
    return out


def as_bool(raw: object) -> bool:
    text = str(raw or "").strip().lower()
    return text in {"1", "true", "t", "yes", "y"}


def sec_cik(raw: object) -> str:
    cik = normalize_cik(raw)
    if not cik or len(cik) != 10:
        raise ValueError(f"Invalid SEC CIK value: {raw!r}")
    return cik


def row_value(row: dict[str, str], key: str) -> str:
    return str(row.get(key) or "").strip()


def resolve_reporting_overrides_path(config: dict[str, Any], *, base_dir: Path, model_family: str) -> Path | None:
    """Resolve the per-family reporting-overrides CSV path (DR-2).

    scoring_policy.families.<model_family>.reporting_overrides_csv is
    authoritative; the legacy flat sec_fundamentals.reporting_overrides_csv key
    remains a defense-only fallback so other families never silently inherit
    the defense override rows. Returns None only when no key is set for the
    family (overrides are optional); a configured path that does not exist on
    disk raises in load_reporting_overrides instead of being treated as empty.
    """
    family_key = f"scoring_policy.families.{model_family}.reporting_overrides_csv"
    overrides_raw = str(cfg_get(config, family_key, "") or "").strip()
    if not overrides_raw and model_family == "defense":
        overrides_raw = str(cfg_get(config, "sec_fundamentals.reporting_overrides_csv", "") or "").strip()
    if not overrides_raw:
        return None
    return resolve_path(overrides_raw, base_dir=base_dir)


def resolve_reporting_graduations_path(config: dict[str, Any], *, base_dir: Path, model_family: str) -> Path | None:
    """Resolve the optional append-only profile-graduation decision ledger."""
    raw = str(
        cfg_get(
            config,
            f"scoring_policy.families.{model_family}.reporting_profile_graduations_csv",
            "",
        )
        or ""
    ).strip()
    return resolve_path(raw, base_dir=base_dir) if raw else None


def _load_reporting_override_versions(
    path: Path | None,
    *,
    asof: str,
) -> dict[tuple[str, str], ReportingOverride]:
    """Load every effective dated version from one override-format CSV."""
    if path is None:
        return {}
    if not path.exists():
        raise FileNotFoundError(f"Reporting overrides CSV not found: {path}")

    versions: dict[tuple[str, str], ReportingOverride] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = [str(name or "").strip() for name in (reader.fieldnames or [])]
        if "valid_from" not in fieldnames:
            raise ValueError(
                f"{path} is missing the required 'valid_from' column; reporting override rows "
                "must carry point-in-time metadata (valid_from, reviewed_at)."
            )
        for line_number, row in enumerate(reader, start=2):
            ticker = normalize_ticker(row_value(row, "ticker"))
            if not ticker:
                raise ValueError(f"{path}:{line_number} missing or invalid ticker")
            valid_from = parse_date(row_value(row, "valid_from"))
            if not valid_from:
                raise ValueError(f"{path}:{line_number} ticker={ticker} missing or invalid valid_from")
            key = (ticker, valid_from)
            if key in versions:
                raise ValueError(f"{path}:{line_number} duplicate override ticker={ticker} valid_from={valid_from}")
            confidence = as_float(row_value(row, "financial_confidence"))
            if confidence is None:
                raise ValueError(f"{path}:{line_number} ticker={ticker} missing financial_confidence")
            if valid_from > asof:
                continue
            versions[key] = ReportingOverride(
                ticker=ticker,
                handling_type=row_value(row, "handling_type"),
                parent_ticker=normalize_ticker(row_value(row, "parent_ticker")),
                skip_sec_network=as_bool(row_value(row, "skip_sec_network")),
                reporting_profile=row_value(row, "reporting_profile"),
                reporting_standard=row_value(row, "reporting_standard"),
                fallback_status=row_value(row, "fallback_status") or "review",
                financial_confidence=confidence,
                usable_xbrl_flag=1 if as_bool(row_value(row, "usable_xbrl_flag")) else 0,
                review_reason=row_value(row, "review_reason"),
                notes=row_value(row, "notes"),
            )
    return versions


def _select_effective_reporting_overrides(
    versions: dict[tuple[str, str], ReportingOverride],
) -> dict[str, ReportingOverride]:
    effective: dict[str, tuple[str, ReportingOverride]] = {}
    for (ticker, valid_from), override in versions.items():
        current = effective.get(ticker)
        if current is None or valid_from > current[0]:
            effective[ticker] = (valid_from, override)
    return {ticker: override for ticker, (_, override) in effective.items()}


def load_reporting_override_sources(
    paths: list[Path | None],
    *,
    asof: str,
) -> dict[str, ReportingOverride]:
    """Merge base overrides and dated graduation decisions without ambiguity."""
    merged: dict[tuple[str, str], ReportingOverride] = {}
    origins: dict[tuple[str, str], Path] = {}
    for path in paths:
        for key, override in _load_reporting_override_versions(path, asof=asof).items():
            existing = merged.get(key)
            if existing is not None and existing != override:
                raise ValueError(
                    "Conflicting reporting override versions for "
                    f"ticker={key[0]} valid_from={key[1]} in {origins[key]} and {path}"
                )
            merged[key] = override
            if path is not None:
                origins[key] = path
    return _select_effective_reporting_overrides(merged)


def load_reporting_overrides(path: Path | None, *, asof: str) -> dict[str, ReportingOverride]:
    """Load reporting overrides effective at the evaluation asof (EL-3).

    Rows carry point-in-time metadata: valid_from gates effectiveness (same-day
    inclusive at the evaluation asof); reviewed_at is provenance documentation
    only and is not interpreted. Multiple rows per ticker with distinct
    valid_from dates are versions; the latest row with valid_from <= asof wins.
    """
    return load_reporting_override_sources([path], asof=asof)


def parse_date(raw: object) -> str:
    text = str(raw or "").strip()[:10]
    if not text:
        return ""
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return ""


def as_float(raw: object) -> float | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value


def as_int(raw: object) -> int | None:
    text = str(raw or "").strip()
    return int(text) if text.isdigit() else None


def compact_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def normalized_name_text(raw: object) -> str:
    text = re.sub(r"[^A-Z0-9 ]+", " ", str(raw or "").upper())
    return " ".join(
        token
        for token in text.split()
        if token not in {"INC", "CORP", "CORPORATION", "CO", "COMPANY", "LTD", "LIMITED", "PLC", "SA", "THE"}
    )


def normalized_name_similarity(left: object, right: object) -> float:
    left_text = normalized_name_text(left)
    right_text = normalized_name_text(right)
    if not left_text or not right_text:
        return 0.0
    return SequenceMatcher(None, left_text, right_text).ratio()


def names_plausibly_match(left: object, right: object) -> bool:
    left_text = normalized_name_text(left)
    right_text = normalized_name_text(right)
    if not left_text or not right_text:
        return True
    if left_text in right_text or right_text in left_text:
        return True
    left_initials = "".join(token[0] for token in left_text.split() if token)
    right_initials = "".join(token[0] for token in right_text.split() if token)
    if len(left_text) <= 5 and left_text == right_initials:
        return True
    if len(right_text) <= 5 and right_text == left_initials:
        return True
    return normalized_name_similarity(left_text, right_text) >= 0.25


def payload_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def cache_path(cache_dir: Path, *, source_id: str, cik: str) -> Path:
    return cache_dir / source_id / f"CIK{cik}.json"


def named_cache_path(cache_dir: Path, *, source_id: str, name: str) -> Path:
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)
    return cache_dir / source_id / safe_name


def throttle_sec_request(minimum_interval_sec: float) -> None:
    """Apply one process-wide SEC request rate across hydration workers."""
    global _SEC_LAST_REQUEST_AT
    interval = max(0.0, float(minimum_interval_sec))
    with _SEC_REQUEST_LOCK:
        now = time.monotonic()
        wait_seconds = (_SEC_LAST_REQUEST_AT + interval) - now
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        _SEC_LAST_REQUEST_AT = time.monotonic()


def request_json(
    url: str, *, user_agent: str, timeout_sec: float, max_retries: int, sleep_sec: float
) -> tuple[int, dict[str, Any], str]:
    try:
        import requests  # type: ignore
    except ImportError as exc:
        raise RuntimeError("Package 'requests' is required for SEC sync.") from exc

    headers = {
        "User-Agent": user_agent,
        "Accept-Encoding": "gzip, deflate",
    }
    last_status = 0
    last_text = ""
    for attempt in range(max(1, max_retries)):
        throttle_sec_request(sleep_sec)
        response = requests.get(url, headers=headers, timeout=timeout_sec)
        last_status = int(response.status_code)
        last_text = response.text
        if response.status_code == 200:
            return last_status, response.json(), last_text
        if response.status_code not in {429, 500, 502, 503, 504}:
            break
        time.sleep(sleep_sec * (attempt + 1))
    raise SecRequestError(status_code=last_status, url=url, body=last_text)


def request_text(
    url: str, *, user_agent: str, timeout_sec: float, max_retries: int, sleep_sec: float
) -> tuple[int, str]:
    try:
        import requests  # type: ignore
    except ImportError as exc:
        raise RuntimeError("Package 'requests' is required for SEC archive sync.") from exc

    headers = {
        "User-Agent": user_agent,
        "Accept-Encoding": "gzip, deflate",
    }
    last_status = 0
    last_text = ""
    for attempt in range(max(1, max_retries)):
        throttle_sec_request(sleep_sec)
        response = requests.get(url, headers=headers, timeout=timeout_sec)
        last_status = int(response.status_code)
        last_text = response.text
        if response.status_code == 200:
            return last_status, last_text
        if response.status_code not in {429, 500, 502, 503, 504}:
            break
        time.sleep(sleep_sec * (attempt + 1))
    raise SecRequestError(status_code=last_status, url=url, body=last_text)


def request_bytes(
    url: str,
    *,
    user_agent: str,
    timeout_sec: float,
    max_retries: int,
    sleep_sec: float,
) -> tuple[int, bytes]:
    try:
        import requests  # type: ignore
    except ImportError as exc:
        raise RuntimeError("Package 'requests' is required for SEC archive sync.") from exc

    headers = {
        "User-Agent": user_agent,
        "Accept-Encoding": "gzip, deflate",
    }
    last_status = 0
    last_payload = b""
    for attempt in range(max(1, max_retries)):
        throttle_sec_request(sleep_sec)
        response = requests.get(url, headers=headers, timeout=timeout_sec)
        last_status = int(response.status_code)
        last_payload = bytes(response.content)
        if response.status_code == 200:
            return last_status, last_payload
        if response.status_code not in {429, 500, 502, 503, 504}:
            break
        time.sleep(sleep_sec * (attempt + 1))
    raise SecRequestError(
        status_code=last_status,
        url=url,
        body=last_payload[:1000].decode("utf-8", errors="replace"),
    )


def write_cache_atomic(cache_file: Path, text: str) -> None:
    """Write an HTTP cache file via tmp + os.replace so readers never see a truncated file (MK-10)."""
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_file = cache_file.with_name(f"{cache_file.name}.{os.getpid()}.{time.time_ns()}.tmp")
    tmp_file.write_text(text, encoding="utf-8")
    try:
        for attempt in range(8):
            try:
                os.replace(tmp_file, cache_file)
                return
            except OSError as exc:
                if getattr(exc, "winerror", None) not in {5, 32} and not isinstance(exc, PermissionError):
                    raise
                try:
                    if cache_file.exists() and cache_file.read_text(encoding="utf-8") == text:
                        return
                except OSError:
                    pass
                if attempt == 7:
                    raise
                time.sleep(0.1 * (attempt + 1))
    finally:
        try:
            tmp_file.unlink(missing_ok=True)
        except OSError:
            LOGGER.warning("Unable to remove SEC cache temp file: %s", tmp_file)


def write_binary_cache_atomic(cache_file: Path, payload: bytes) -> None:
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_file = cache_file.with_name(f"{cache_file.name}.{os.getpid()}.{time.time_ns()}.tmp")
    tmp_file.write_bytes(payload)
    try:
        os.replace(tmp_file, cache_file)
    finally:
        try:
            tmp_file.unlink(missing_ok=True)
        except OSError:
            LOGGER.warning("Unable to remove SEC binary cache temp file: %s", tmp_file)


def load_or_fetch_json(
    url: str,
    *,
    cache_file: Path,
    force: bool,
    user_agent: str,
    timeout_sec: float,
    max_retries: int,
    sleep_sec: float,
) -> tuple[int, dict[str, Any], str, str]:
    if cache_file.exists() and not force:
        text = cache_file.read_text(encoding="utf-8")
        try:
            return 200, json.loads(text), text, "cache"
        except json.JSONDecodeError:
            # MK-10: a corrupt cache must never poison the ticker forever.
            # Delete the bad file and fall through to a single refetch.
            LOGGER.warning("Deleting corrupt JSON cache and refetching once: %s", cache_file)
            cache_file.unlink()
    status, payload, text = request_json(
        url, user_agent=user_agent, timeout_sec=timeout_sec, max_retries=max_retries, sleep_sec=sleep_sec
    )
    write_cache_atomic(cache_file, text)
    return status, payload, text, "network"


def load_or_fetch_text(
    url: str,
    *,
    cache_file: Path,
    force: bool,
    user_agent: str,
    timeout_sec: float,
    max_retries: int,
    sleep_sec: float,
) -> tuple[int, str, str]:
    if cache_file.exists() and not force:
        text = cache_file.read_text(encoding="utf-8", errors="replace")
        return 200, text, "cache"
    status, text = request_text(
        url, user_agent=user_agent, timeout_sec=timeout_sec, max_retries=max_retries, sleep_sec=sleep_sec
    )
    write_cache_atomic(cache_file, text)
    return status, text, "network"


def load_or_fetch_bytes(
    url: str,
    *,
    cache_file: Path,
    force: bool,
    user_agent: str,
    timeout_sec: float,
    max_retries: int,
    sleep_sec: float,
) -> tuple[int, bytes, str]:
    if cache_file.exists() and not force:
        return 200, cache_file.read_bytes(), "cache"
    status, payload = request_bytes(
        url,
        user_agent=user_agent,
        timeout_sec=timeout_sec,
        max_retries=max_retries,
        sleep_sec=sleep_sec,
    )
    write_binary_cache_atomic(cache_file, payload)
    return status, payload, "network"


def resolve_sec_user_agent(config: dict[str, Any]) -> str:
    """Resolve and validate the SEC User-Agent after ${ENV:-default} expansion."""
    user_agent = expand_env_vars(str(cfg_get(config, "sec_fundamentals.user_agent", "") or "")).strip()
    if not user_agent or "@" not in user_agent or "${" in user_agent:
        raise ValueError(
            "sec_fundamentals.user_agent must resolve (after env expansion) to a non-empty value "
            f"containing a contact email address per SEC fair-access policy; current value={user_agent!r}"
        )
    return user_agent


def should_skip_incremental_companyfacts(
    *,
    incremental: bool,
    new_filing_keys: set[tuple[str, str, str]],
    force_companyfacts: bool,
    force_archive: bool,
    prior_sync_failed: bool,
    has_existing_state: bool,
) -> bool:
    """Return whether incremental mode can safely skip fact/archive processing.

    FN-1 guard: new submissions metadata is a hard stop for skipping; the ticker
    must re-enter companyfacts/archive processing so the new filing's facts can
    be fetched and mapped before a future run considers it current.
    """
    return (
        incremental
        and not new_filing_keys
        and not force_companyfacts
        and not force_archive
        and not prior_sync_failed
        and has_existing_state
    )


def should_force_companyfacts_payload_fetch(*, incremental: bool, force_companyfacts: bool) -> bool:
    """CompanyFacts is CIK-scoped mutable aggregate JSON; incremental processing refetches it."""
    return bool(force_companyfacts or incremental)


def add_issue(
    conn: Any,
    *,
    severity: str,
    ticker: str,
    model_family: str,
    source_id: str,
    issue_type: str,
    detail: str,
) -> None:
    now = utc_now()
    row = conn.execute("SELECT company_id FROM dim_company WHERE ticker = ?", (ticker,)).fetchone()
    company_id = int(row["company_id"]) if row is not None else None
    # SC-12: issues are family-scoped; every writer stamps the configured/CLI
    # model_family so one family's sync never masquerades as another's.
    conn.execute(
        """
        INSERT INTO data_quality_issues(
            detected_at, severity, stage, model_family, ticker, company_id, source_id, issue_type,
            issue_detail, resolution_status, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
        """,
        (now, severity, RUN_TYPE, model_family, ticker, company_id, source_id, issue_type, detail, now, now),
    )


def resolve_open_issue(
    conn: Any,
    *,
    ticker: str,
    model_family: str,
    source_id: str,
    issue_type: str,
    detail: str,
    resolution_status: str,
) -> None:
    now = utc_now()
    conn.execute(
        """
        UPDATE data_quality_issues
        SET resolution_status = ?,
            updated_at = ?
        WHERE stage = ?
          AND model_family = ?
          AND ticker = ?
          AND source_id = ?
          AND issue_type = ?
          AND issue_detail = ?
          AND resolution_status = 'open'
        """,
        (resolution_status, now, RUN_TYPE, model_family, ticker, source_id, issue_type, detail),
    )


def resolve_successful_sync_issues(
    conn: Any,
    *,
    ticker: str,
    model_family: str,
    source_id: str,
) -> int:
    now = utc_now()
    result = conn.execute(
        """
        UPDATE data_quality_issues
        SET resolution_status = 'resolved_by_successful_retry',
            updated_at = ?
        WHERE stage = ?
          AND model_family = ?
          AND ticker = ?
          AND source_id = ?
          AND issue_type IN (
                'sec_sync_failed',
                'sec_endpoint_not_available',
                'sec_archive_xbrl_unavailable',
                'sec_archive_xbrl_no_filing_metadata'
          )
          AND resolution_status = 'open'
        """,
        (now, RUN_TYPE, model_family, ticker, source_id),
    )
    return int(result.rowcount or 0)


def load_universe(
    conn: Any,
    *,
    model_family: str,
    ticker_filter: list[str],
    include_historical: bool,
    membership_asof: str = "",
    currency_asof: str = "",
) -> list[dict[str, Any]]:
    filter_sql = ""
    params: list[Any] = [model_family]
    membership_sql = ""
    if include_historical and membership_asof:
        membership_sql = "AND m.start_date <= ? AND COALESCE(NULLIF(m.end_date, ''), '9999-12-31') >= ?"
        params.extend([membership_asof, membership_asof])
    if ticker_filter:
        filter_sql = f"AND c.ticker IN ({','.join('?' for _ in ticker_filter)})"
        params.extend(ticker_filter)
    if include_historical:
        rows = conn.execute(
            f"""
            SELECT DISTINCT c.company_id, c.ticker, c.cik, c.company_name, c.country, c.currency, c.is_active
            FROM dim_company c
            JOIN dim_universe_membership m
              ON m.company_id = c.company_id
             AND m.model_family = ?
            WHERE 1 = 1
              {membership_sql}
              {filter_sql}
            ORDER BY c.ticker
            """,
            tuple(params),
        ).fetchall()
    else:
        rows = conn.execute(
            f"""
            SELECT DISTINCT c.company_id, c.ticker, c.cik, c.company_name, c.country, c.currency, c.is_active
            FROM dim_company c
            JOIN dim_industrials_taxonomy t
              ON t.company_id = c.company_id
             AND t.model_family = ?
            WHERE c.is_active = 1
              {filter_sql}
            ORDER BY c.ticker
            """,
            tuple(params),
        ).fetchall()
    output = [dict(row) for row in rows]
    return (
        apply_reporting_currencies(
            conn,
            output,
            model_family=model_family,
            asof=currency_asof,
        )
        if model_family == "machinery" and currency_asof
        else output
    )


def group_cache_hydration_items(
    items: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    """Group aliases/share classes so one worker owns each CIK cache path."""
    items_by_cik: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        group_key = normalize_cik(item.get("cik")) or (f"ticker:{normalize_ticker(item.get('ticker'))}")
        items_by_cik.setdefault(group_key, []).append(item)
    return list(items_by_cik.values())


def filing_keys(conn: Any, *, ticker: str, source_id: str) -> set[tuple[str, str, str]]:
    rows = conn.execute(
        """
        SELECT accession_number, filing_date, form_type
        FROM fact_sec_filing
        WHERE ticker = ?
          AND source_id = ?
        """,
        (ticker, source_id),
    ).fetchall()
    return {
        (
            str(row["accession_number"] or ""),
            str(row["filing_date"] or ""),
            str(row["form_type"] or ""),
        )
        for row in rows
    }


def has_filing_metadata(conn: Any, *, ticker: str, source_id: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM fact_sec_filing
        WHERE ticker = ?
          AND source_id = ?
        LIMIT 1
        """,
        (ticker, source_id),
    ).fetchone()
    return row is not None


def has_existing_sec_financial_state(
    conn: Any,
    *,
    ticker: str,
    model_family: str,
    source_id: str,
    override: ReportingOverride | None,
) -> bool:
    """Return True only when a prior sync completed for this ticker (XC-12).

    Completeness marker: the stored reporting profile row is written by
    classify_reporting_profile at the end of every completed per-ticker pass,
    and its latest_filing_date snapshots the filing metadata seen at that time.
    A bare fact row (partial/stale state) is never treated as complete, and
    filing metadata newer than the last classification forces a reprocess.
    """
    if override is not None and override.skip_sec_network:
        return True
    profile_row = conn.execute(
        """
        SELECT reporting_profile, usable_xbrl_flag, latest_filing_date
        FROM dim_issuer_reporting_profile
        WHERE ticker = ?
          AND model_family = ?
        """,
        (ticker, model_family),
    ).fetchone()
    if profile_row is None:
        # Never classified for this family: fact rows alone are not completeness evidence.
        return False
    newest_filing_row = conn.execute(
        "SELECT MAX(filing_date) FROM fact_sec_filing WHERE ticker = ?",
        (ticker,),
    ).fetchone()
    newest_filing = str(newest_filing_row[0] or "") if newest_filing_row is not None else ""
    profile_latest_filing = str(profile_row["latest_filing_date"] or "")
    if newest_filing and profile_latest_filing < newest_filing:
        # Filing metadata landed after the last completed classification pass
        # (e.g. a prior run crashed between the filings and facts transactions).
        return False
    if int(profile_row["usable_xbrl_flag"] or 0) == 1:
        fact_row = conn.execute(
            """
            SELECT 1
            FROM fact_sec_xbrl_fact
            WHERE ticker = ?
              AND source_id = ?
            LIMIT 1
            """,
            (ticker, source_id),
        ).fetchone()
        return fact_row is not None
    # usable_xbrl_flag = 0: only intentionally low/partial coverage profiles count
    # as complete state. Daily refreshes should not repeatedly grind the archive
    # fallback unless a new filing appears or the caller explicitly forces
    # companyfacts/archive processing.
    return (
        override is not None
        and str(profile_row["reporting_profile"] or "") in ARCHIVE_FALLBACK_PROFILES
        and override.reporting_profile in ARCHIVE_FALLBACK_PROFILES
    )


def sec_fact_counts(conn: Any, *, ticker: str, source_id: str) -> tuple[int, int]:
    raw_row = conn.execute(
        """
        SELECT COUNT(*)
        FROM fact_sec_xbrl_fact_raw
        WHERE ticker = ?
          AND source_id = ?
        """,
        (ticker, source_id),
    ).fetchone()
    mapped_row = conn.execute(
        """
        SELECT COUNT(*)
        FROM fact_sec_xbrl_fact
        WHERE ticker = ?
          AND source_id = ?
        """,
        (ticker, source_id),
    ).fetchone()
    return int(raw_row[0] or 0), int(mapped_row[0] or 0)


def clear_stage_issues(conn: Any, *, model_family: str, ticker_filter: list[str] | None = None) -> None:
    # SC-12: per-stage clears are family-scoped so a run for one model family
    # never wipes another family's open issues for the same stage.
    if ticker_filter:
        placeholders = ",".join("?" for _ in ticker_filter)
        conn.execute(
            f"DELETE FROM data_quality_issues WHERE stage = ? AND model_family = ? AND ticker IN ({placeholders})",
            (RUN_TYPE, model_family, *ticker_filter),
        )
    else:
        conn.execute("DELETE FROM data_quality_issues WHERE stage = ? AND model_family = ?", (RUN_TYPE, model_family))


def record_raw_response(
    conn: Any,
    *,
    source_id: str,
    endpoint: str,
    status: int,
    payload_text: str,
    asof_date: str,
    ingestion_run_id: int,
) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO raw_api_responses(
            source_id, endpoint, query_params_json, request_time_utc, response_status,
            response_hash, asof_date, payload_text, ingestion_run_id, created_at
        )
        VALUES (?, ?, '{}', ?, ?, ?, ?, ?, ?, ?)
        """,
        (source_id, endpoint, now, status, payload_hash(payload_text), asof_date, payload_text, ingestion_run_id, now),
    )


def upsert_filings(
    conn: Any,
    *,
    ticker: str,
    cik: str,
    source_id: str,
    payload: dict[str, Any],
    allowed_forms: set[str],
    start_date: str,
    end_date: str = "",
    source_detail: str = "sec_submissions_recent",
) -> int:
    recent_payload = (payload.get("filings") or {}).get("recent") if isinstance(payload.get("filings"), dict) else None
    rows_payload = recent_payload if isinstance(recent_payload, dict) else payload
    if not isinstance(rows_payload, dict):
        return 0
    forms = rows_payload.get("form") or []
    count = 0
    now = utc_now()
    keys = [
        "accessionNumber",
        "filingDate",
        "acceptanceDateTime",
        "reportDate",
        "form",
        "primaryDocument",
    ]
    for idx, form in enumerate(forms):
        form_type = str(form or "").strip().upper()
        if allowed_forms and form_type not in allowed_forms:
            continue
        values = {key: (rows_payload.get(key) or []) for key in keys}
        accession = str(values["accessionNumber"][idx] or "").strip() if idx < len(values["accessionNumber"]) else ""
        filing_date = parse_date(values["filingDate"][idx] if idx < len(values["filingDate"]) else "")
        if (
            not accession
            or not filing_date
            or (start_date and filing_date < start_date)
            or (end_date and filing_date > end_date)
        ):
            continue
        accepted_at = (
            str(values["acceptanceDateTime"][idx] or "").strip() if idx < len(values["acceptanceDateTime"]) else ""
        )
        report_date = parse_date(values["reportDate"][idx] if idx < len(values["reportDate"]) else "")
        primary_document = (
            str(values["primaryDocument"][idx] or "").strip() if idx < len(values["primaryDocument"]) else ""
        )
        # SEC submissions payloads carry no fiscal year/period columns (FN-12,
        # empirically verified): leave them honestly NULL/'' here. Per-fact
        # fy/fp arrive with the companyfacts payload instead.
        fiscal_year: int | None = None
        fiscal_period = ""
        accession_nodash = accession.replace("-", "")
        filing_url = (
            f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession_nodash}/{primary_document}"
            if primary_document
            else ""
        )
        conn.execute(
            """
            INSERT INTO fact_sec_filing(
                ticker, cik, source_id, accession_number, form_type, filing_date,
                accepted_at, report_date, fiscal_year, fiscal_period, primary_document,
                filing_url, source_detail, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, accession_number, source_id) DO UPDATE SET
                cik = excluded.cik,
                form_type = excluded.form_type,
                filing_date = excluded.filing_date,
                accepted_at = excluded.accepted_at,
                report_date = excluded.report_date,
                fiscal_year = excluded.fiscal_year,
                fiscal_period = excluded.fiscal_period,
                primary_document = excluded.primary_document,
                filing_url = excluded.filing_url,
                source_detail = excluded.source_detail,
                updated_at = excluded.updated_at
            """,
            (
                ticker,
                cik,
                source_id,
                accession,
                form_type,
                filing_date,
                accepted_at,
                report_date,
                fiscal_year,
                fiscal_period,
                primary_document,
                filing_url,
                source_detail,
                now,
                now,
            ),
        )
        count += 1
    return count


def submission_history_files(payload: dict[str, Any], *, max_files: int) -> list[str]:
    files = (payload.get("filings") or {}).get("files") if isinstance(payload.get("filings"), dict) else []
    if not isinstance(files, list):
        return []
    names: list[str] = []
    seen: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name or name in seen:
            continue
        names.append(name)
        seen.add(name)
        if max_files > 0 and len(names) >= max_files:
            break
    return names


def relevant_submission_history_files(
    payload: dict[str, Any],
    *,
    start_date: str,
    end_date: str,
    max_files: int,
) -> list[str]:
    files = (payload.get("filings") or {}).get("files") if isinstance(payload.get("filings"), dict) else []
    if not isinstance(files, list):
        return []
    names: list[str] = []
    seen: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        filing_from = parse_date(item.get("filingFrom"))
        filing_to = parse_date(item.get("filingTo"))
        if (
            not name
            or name in seen
            or (start_date and filing_to and filing_to < start_date)
            or (end_date and filing_from and filing_from > end_date)
        ):
            continue
        names.append(name)
        seen.add(name)
        if max_files > 0 and len(names) >= max_files:
            break
    return names


def read_cached_submission_payload(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Cached SEC submissions payload not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid cached SEC submissions JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Cached SEC submissions payload must be an object: {path}")
    return payload


def catalog_cached_submission_filings(
    conn: Any,
    *,
    items: list[dict[str, Any]],
    source_id: str,
    cache_dir: Path,
    allowed_forms: set[str],
    start_date: str,
    end_date: str,
    max_history_files: int,
    fetch_missing: bool = False,
    submissions_url_template: str = "",
    history_url_template: str = "",
    user_agent: str = "",
    timeout_sec: float = 30.0,
    max_retries: int = 3,
    sleep_sec: float = 0.12,
) -> list[dict[str, Any]]:
    """Catalog cached SEC filing metadata without network or financial writes."""
    if not allowed_forms:
        raise ValueError("Cached filing catalog requires at least one form")
    report_rows: list[dict[str, Any]] = []
    requested_forms = ",".join(sorted(allowed_forms))
    form_placeholders = ",".join("?" for _ in allowed_forms)
    for group in group_cache_hydration_items(items):
        cik = normalize_cik(group[0].get("cik")) if group else ""
        network_request_count = 0
        try:
            if not cik:
                raise ValueError("missing_or_invalid_cik")
            root_path = cache_path(cache_dir, source_id=source_id, cik=cik)
            if not root_path.exists() and fetch_missing:
                if not submissions_url_template or not user_agent:
                    raise ValueError("missing_catalog_network_configuration")
                _, _, _, fetch_mode = load_or_fetch_json(
                    submissions_url_template.format(cik=cik),
                    cache_file=root_path,
                    force=False,
                    user_agent=user_agent,
                    timeout_sec=timeout_sec,
                    max_retries=max_retries,
                    sleep_sec=sleep_sec,
                )
                network_request_count += int(fetch_mode == "network")
            root_payload = read_cached_submission_payload(root_path)
            payload_cik = normalize_cik(root_payload.get("cik"))
            if payload_cik and payload_cik != cik:
                raise ValueError(f"cached_submission_cik_mismatch:expected={cik};actual={payload_cik}")
            history_names = relevant_submission_history_files(
                root_payload,
                start_date=start_date,
                end_date=end_date,
                max_files=max_history_files,
            )
            history_payloads: list[tuple[str, dict[str, Any]]] = []
            missing_history: list[str] = []
            for name in history_names:
                history_path = named_cache_path(
                    cache_dir,
                    source_id=source_id,
                    name=name,
                )
                if not history_path.exists():
                    if fetch_missing:
                        if not history_url_template or not user_agent:
                            raise ValueError("missing_catalog_history_network_configuration")
                        try:
                            _, payload, _, fetch_mode = load_or_fetch_json(
                                history_url_template.format(file_name=name),
                                cache_file=history_path,
                                force=False,
                                user_agent=user_agent,
                                timeout_sec=timeout_sec,
                                max_retries=max_retries,
                                sleep_sec=sleep_sec,
                            )
                        except SecRequestError:
                            missing_history.append(name)
                            continue
                        network_request_count += int(fetch_mode == "network")
                        history_payloads.append((name, payload))
                        continue
                    missing_history.append(name)
                    continue
                history_payloads.append((name, read_cached_submission_payload(history_path)))
            with conn:
                for item in group:
                    ticker = normalize_ticker(item.get("ticker"))
                    if not ticker:
                        continue
                    upsert_filings(
                        conn,
                        ticker=ticker,
                        cik=cik,
                        source_id=source_id,
                        payload=root_payload,
                        allowed_forms=allowed_forms,
                        start_date=start_date,
                        end_date=end_date,
                        source_detail="sec_submissions_recent_cache_catalog",
                    )
                    for _, history_payload in history_payloads:
                        upsert_filings(
                            conn,
                            ticker=ticker,
                            cik=cik,
                            source_id=source_id,
                            payload=history_payload,
                            allowed_forms=allowed_forms,
                            start_date=start_date,
                            end_date=end_date,
                            source_detail=("sec_submissions_history_cache_catalog"),
                        )
            for item in group:
                ticker = normalize_ticker(item.get("ticker"))
                if not ticker:
                    continue
                form_counts = {
                    str(row["form_type"]): int(row["filing_count"])
                    for row in conn.execute(
                        f"""
                        SELECT UPPER(form_type) AS form_type,
                               COUNT(*) AS filing_count
                        FROM fact_sec_filing
                        WHERE ticker = ?
                          AND source_id = ?
                          AND filing_date >= ?
                          AND filing_date <= ?
                          AND UPPER(form_type) IN ({form_placeholders})
                        GROUP BY UPPER(form_type)
                        ORDER BY UPPER(form_type)
                        """,
                        (
                            ticker,
                            source_id,
                            start_date,
                            end_date,
                            *sorted(allowed_forms),
                        ),
                    )
                }
                report_rows.append(
                    {
                        "ticker": ticker,
                        "cik": cik,
                        "status": ("catalog_partial_history_cache" if missing_history else "cataloged"),
                        "catalog_start_date": start_date,
                        "catalog_end_date": end_date,
                        "requested_forms": requested_forms,
                        "cataloged_filing_count": sum(form_counts.values()),
                        "cataloged_form_counts_json": json.dumps(
                            form_counts,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        "relevant_history_file_count": len(history_names),
                        "missing_history_cache_count": len(missing_history),
                        "missing_history_cache_files_json": json.dumps(
                            missing_history,
                            separators=(",", ":"),
                        ),
                        "network_request_count": network_request_count,
                        "error": "",
                    }
                )
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            LOGGER.exception(
                "Cached SEC filing catalog failed cik=%s tickers=%s",
                cik,
                ",".join(normalize_ticker(item.get("ticker")) for item in group),
            )
            for item in group:
                report_rows.append(
                    {
                        "ticker": normalize_ticker(item.get("ticker")),
                        "cik": cik,
                        "status": "catalog_failed",
                        "catalog_start_date": start_date,
                        "catalog_end_date": end_date,
                        "requested_forms": requested_forms,
                        "cataloged_filing_count": 0,
                        "cataloged_form_counts_json": "{}",
                        "relevant_history_file_count": 0,
                        "missing_history_cache_count": 0,
                        "missing_history_cache_files_json": "[]",
                        "network_request_count": network_request_count,
                        "error": detail,
                    }
                )
    return sorted(report_rows, key=lambda row: str(row["ticker"]))


def sync_submission_history_files(
    conn: Any,
    *,
    ticker: str,
    cik: str,
    source_id: str,
    root_payload: dict[str, Any],
    cache_dir: Path,
    force: bool,
    user_agent: str,
    timeout_sec: float,
    max_retries: int,
    sleep_sec: float,
    allowed_forms: set[str],
    start_date: str,
    url_template: str,
    max_files: int,
    ingestion_run_id: int,
) -> tuple[int, int]:
    # Phase 1 (XC-23): fetch every history file before opening a write
    # transaction so the DB write lock is never held across network I/O.
    fetched: list[tuple[str, int, dict[str, Any], str, str]] = []
    failed: list[tuple[str, int, str]] = []
    request_count = 0
    for file_name in submission_history_files(root_payload, max_files=max_files):
        url = url_template.format(file_name=file_name)
        request_count += 1
        try:
            status, payload, text, fetch_mode = load_or_fetch_json(
                url,
                cache_file=named_cache_path(cache_dir, source_id=source_id, name=file_name),
                force=force,
                user_agent=user_agent,
                timeout_sec=timeout_sec,
                max_retries=max_retries,
                sleep_sec=sleep_sec,
            )
        except SecRequestError as exc:
            failed.append((exc.url, exc.status_code, exc.body))
            LOGGER.warning(
                "Skipping unavailable SEC submission history file ticker=%s url=%s status=%s",
                ticker,
                exc.url,
                exc.status_code,
            )
            continue
        fetched.append((url, status, payload, text, fetch_mode))
        if fetch_mode == "network":
            time.sleep(sleep_sec)
    # Phase 2: one short write transaction for provenance rows and filing upserts.
    filing_count = 0
    with conn:
        for endpoint, status_code, body in failed:
            record_raw_response(
                conn,
                source_id=source_id,
                endpoint=endpoint,
                status=status_code,
                payload_text=body,
                asof_date=datetime.now(timezone.utc).date().isoformat(),
                ingestion_run_id=ingestion_run_id,
            )
        for url, status, payload, text, fetch_mode in fetched:
            if fetch_mode == "network":
                # FN-8: cache hits are not new observations; only record network fetches.
                record_raw_response(
                    conn,
                    source_id=source_id,
                    endpoint=url,
                    status=status,
                    payload_text=text,
                    asof_date=datetime.now(timezone.utc).date().isoformat(),
                    ingestion_run_id=ingestion_run_id,
                )
            filing_count += upsert_filings(
                conn,
                ticker=ticker,
                cik=cik,
                source_id=source_id,
                payload=payload,
                allowed_forms=allowed_forms,
                start_date=start_date,
                source_detail="sec_submissions_history",
            )
    return filing_count, request_count


def parse_browse_atom_filings(atom_text: str, *, fallback_form_type: str) -> list[dict[str, str]]:
    try:
        root = ET.fromstring(atom_text.encode("utf-8"))
    except ET.ParseError:
        return []
    filings: list[dict[str, str]] = []
    for entry in root.iter():
        if local_name(entry.tag).lower() != "entry":
            continue
        values: dict[str, str] = {"form_type": fallback_form_type}
        link_href = ""
        for child in entry.iter():
            child_name = local_name(child.tag).lower()
            text = str(child.text or "").strip()
            if child_name == "link" and not link_href:
                link_href = str(child.attrib.get("href") or "").strip()
            elif child_name in {"accession-number", "accessionnumber"} and text:
                values["accession_number"] = text
            elif child_name in {"filing-date", "filingdate", "updated"} and text and "filing_date" not in values:
                values["filing_date"] = parse_date(text)
            elif child_name in {"filing-type", "filingtype", "category"} and text:
                values["form_type"] = text.upper()
            elif child_name == "title" and text and values.get("form_type") == fallback_form_type:
                values["form_type"] = text.split()[0].upper()
        if "accession_number" not in values and link_href:
            match = re.search(r"(\d{10}-\d{2}-\d{6})", link_href)
            if match:
                values["accession_number"] = match.group(1)
        if "filing_date" not in values and link_href:
            values["filing_date"] = ""
        if values.get("accession_number"):
            values["filing_url"] = link_href
            filings.append(values)
    return filings


def upsert_filing_stub(
    conn: Any,
    *,
    ticker: str,
    cik: str,
    source_id: str,
    accession: str,
    form_type: str,
    filing_date: str,
    filing_url: str,
) -> None:
    now = utc_now()
    accession_nodash = accession.replace("-", "")
    url = filing_url or f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession_nodash}/"
    conn.execute(
        """
        INSERT INTO fact_sec_filing(
            ticker, cik, source_id, accession_number, form_type, filing_date,
            accepted_at, report_date, fiscal_year, fiscal_period, primary_document,
            filing_url, source_detail, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, '', '', NULL, '', '', ?, 'sec_browse_edgar_atom', ?, ?)
        ON CONFLICT(ticker, accession_number, source_id) DO UPDATE SET
            cik = excluded.cik,
            form_type = excluded.form_type,
            filing_date = excluded.filing_date,
            filing_url = excluded.filing_url,
            -- SC-9: an atom stub must never downgrade richer provenance (e.g.
            -- sec_submissions_recent) already stored for this filing; only an
            -- empty or same-stub value is replaced.
            source_detail = CASE
                WHEN COALESCE(fact_sec_filing.source_detail, '') IN ('', 'sec_browse_edgar_atom')
                THEN excluded.source_detail
                ELSE fact_sec_filing.source_detail
            END,
            updated_at = excluded.updated_at
        """,
        (ticker, cik, source_id, accession, form_type, filing_date, url, now, now),
    )


def sync_browse_edgar_filings(
    conn: Any,
    *,
    ticker: str,
    cik: str,
    source_id: str,
    cache_dir: Path,
    force: bool,
    user_agent: str,
    timeout_sec: float,
    max_retries: int,
    sleep_sec: float,
    allowed_forms: set[str],
    start_date: str,
    url_template: str,
    ingestion_run_id: int,
) -> tuple[int, int]:
    forms = sorted(allowed_forms or {"10-K", "10-Q"})
    # Phase 1 (XC-23): fetch every browse feed before opening a write transaction.
    fetched: list[tuple[str, str, int, str, str]] = []
    failed: list[tuple[str, int, str]] = []
    request_count = 0
    for form_type in forms:
        url = url_template.format(cik=cik, form_type=form_type)
        cache = named_cache_path(cache_dir, source_id=source_id, name=f"CIK{cik}_browse_{form_type}.atom")
        request_count += 1
        try:
            status, text, fetch_mode = load_or_fetch_text(
                url,
                cache_file=cache,
                force=force,
                user_agent=user_agent,
                timeout_sec=timeout_sec,
                max_retries=max_retries,
                sleep_sec=sleep_sec,
            )
        except SecRequestError as exc:
            failed.append((exc.url, exc.status_code, exc.body))
            LOGGER.warning(
                "Skipping unavailable SEC browse feed ticker=%s form=%s status=%s", ticker, form_type, exc.status_code
            )
            continue
        fetched.append((form_type, url, status, text, fetch_mode))
        if fetch_mode == "network":
            time.sleep(sleep_sec)
    # Phase 2: one short write transaction for provenance rows and filing stubs.
    filing_count = 0
    with conn:
        for endpoint, status_code, body in failed:
            record_raw_response(
                conn,
                source_id=source_id,
                endpoint=endpoint,
                status=status_code,
                payload_text=body,
                asof_date=datetime.now(timezone.utc).date().isoformat(),
                ingestion_run_id=ingestion_run_id,
            )
        for form_type, url, status, text, fetch_mode in fetched:
            if fetch_mode == "network":
                # FN-8: cache hits are not new observations; only record network fetches.
                record_raw_response(
                    conn,
                    source_id=source_id,
                    endpoint=url,
                    status=status,
                    payload_text=text,
                    asof_date=datetime.now(timezone.utc).date().isoformat(),
                    ingestion_run_id=ingestion_run_id,
                )
            for filing in parse_browse_atom_filings(text, fallback_form_type=form_type):
                filing_date = parse_date(filing.get("filing_date"))
                if start_date and filing_date and filing_date < start_date:
                    continue
                accession = str(filing.get("accession_number") or "").strip()
                if not accession:
                    continue
                upsert_filing_stub(
                    conn,
                    ticker=ticker,
                    cik=cik,
                    source_id=source_id,
                    accession=accession,
                    form_type=str(filing.get("form_type") or form_type).upper(),
                    filing_date=filing_date,
                    filing_url=str(filing.get("filing_url") or ""),
                )
                filing_count += 1
    return filing_count, request_count


def load_concept_map(conn: Any) -> dict[tuple[str, str], list[dict[str, Any]]]:
    rows = conn.execute(
        """
        SELECT taxonomy, concept_name, canonical_metric, financial_statement,
               period_type, sign_policy, priority
        FROM dim_xbrl_concept_map
        WHERE active_flag = 1
        ORDER BY priority, canonical_metric
        """
    ).fetchall()
    out: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        out.setdefault((str(row["taxonomy"]), str(row["concept_name"])), []).append(dict(row))
    return out


def add_family_concept_mappings(
    concept_map: dict[tuple[str, str], list[dict[str, Any]]],
    *,
    model_family: str,
    config: dict[str, Any] | None = None,
    base_dir: Path | None = None,
) -> None:
    if model_family == "machinery":
        for mapping in MACHINERY_EXTRA_CONCEPT_MAPPINGS:
            key = (str(mapping["taxonomy"]), str(mapping["concept_name"]))
            family_mapping = dict(mapping)
            if family_mapping not in concept_map.setdefault(key, []):
                concept_map[key].append(family_mapping)
    if config is None or base_dir is None:
        return
    raw_path = str(
        cfg_get(
            config,
            f"model_families.{model_family}.financial.concept_aliases_csv",
            "",
        )
        or ""
    ).strip()
    if not raw_path:
        return
    path = resolve_path(raw_path, base_dir=base_dir)
    if not path.exists():
        raise FileNotFoundError(f"Family concept-alias CSV not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {
            "taxonomy",
            "concept_name",
            "canonical_metric",
            "financial_statement",
            "period_type",
            "sign_policy",
            "priority",
            "review_status",
        }
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"Family concept-alias CSV missing columns={sorted(missing)}")
        for line_number, row in enumerate(reader, start=2):
            if str(row.get("review_status") or "").strip().lower() != "reviewed":
                raise ValueError(f"{path}:{line_number} concept alias must be reviewed")
            mapping = {
                "taxonomy": str(row.get("taxonomy") or "").strip(),
                "concept_name": str(row.get("concept_name") or "").strip(),
                "canonical_metric": str(row.get("canonical_metric") or "").strip(),
                "financial_statement": str(row.get("financial_statement") or "").strip(),
                "period_type": str(row.get("period_type") or "").strip(),
                "sign_policy": str(row.get("sign_policy") or "").strip(),
                "priority": int(str(row.get("priority") or "")),
            }
            if not all(
                mapping[key]
                for key in (
                    "taxonomy",
                    "concept_name",
                    "canonical_metric",
                    "financial_statement",
                    "period_type",
                    "sign_policy",
                )
            ):
                raise ValueError(f"{path}:{line_number} concept alias has blank fields")
            key = (str(mapping["taxonomy"]), str(mapping["concept_name"]))
            if mapping not in concept_map.setdefault(key, []):
                concept_map[key].append(mapping)


def make_fact_key(*parts: object) -> str:
    text = "|".join(str(part or "") for part in parts)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def apply_sign(value: float | None, sign_policy: str) -> float | None:
    if value is None:
        return None
    if sign_policy in {"positive_abs", "abs"}:
        return abs(value)
    if sign_policy == "negative_abs":
        return -abs(value)
    if sign_policy == "expense_from_net":
        # Net income/expense lines report net expense as negative. Net income
        # (positive raw value) maps to 0 expense, never abs() into a phantom one.
        return max(-value, 0.0)
    return value


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def namespace_uri(tag: str) -> str:
    if tag.startswith("{") and "}" in tag:
        return tag[1:].split("}", 1)[0]
    return ""


def taxonomy_from_namespace(raw: str) -> str:
    text = str(raw or "").lower()
    if "us-gaap" in text:
        return "us-gaap"
    if "ifrs-full" in text:
        return "ifrs-full"
    return ""


def parse_float_text(raw: object) -> float | None:
    text = str(raw or "").strip()
    if not text:
        return None
    text = text.replace(",", "").replace("$", "").replace("\u00a0", "")
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    return as_float(text)


def parse_inline_numeric_value(element: ET.Element) -> float | None:
    text = "".join(element.itertext()).strip()
    value = parse_float_text(text)
    if value is None:
        return None
    scale = as_int(element.attrib.get("scale"))
    if scale is not None:
        value *= 10.0**scale
    if str(element.attrib.get("sign") or "").strip() == "-":
        value = -abs(value)
    return value


def parse_namespace_prefixes(document_text: str) -> dict[str, str]:
    prefixes: dict[str, str] = {}
    for match in re.finditer(r"xmlns:([A-Za-z0-9_\-]+)\s*=\s*['\"]([^'\"]+)['\"]", document_text[:200000]):
        prefixes[match.group(1)] = match.group(2)
    return prefixes


def xml_attribute(element: ET.Element, name: str) -> str:
    target = name.lower()
    for key, value in element.attrib.items():
        if local_name(key).lower() == target:
            return str(value or "").strip()
    return ""


def parse_xbrl_label_linkbase(document_text: str) -> dict[str, list[str]]:
    """Resolve extension-concept labels from a filing's XBRL label linkbase."""
    try:
        root = ET.fromstring(document_text.encode("utf-8"))
    except ET.ParseError:
        return {}
    locators: dict[str, str] = {}
    labels: dict[str, str] = {}
    arcs: list[tuple[str, str]] = []
    for element in root.iter():
        element_name = local_name(element.tag).lower()
        if element_name == "loc":
            locator_id = xml_attribute(element, "label")
            href = xml_attribute(element, "href")
            concept = href.rsplit("#", 1)[-1].strip()
            if locator_id and concept:
                locators[locator_id] = concept
        elif element_name == "label":
            label_id = xml_attribute(element, "label")
            text = " ".join("".join(element.itertext()).split())
            if label_id and text:
                labels[label_id] = text
        elif element_name == "labelarc":
            source = xml_attribute(element, "from")
            target = xml_attribute(element, "to")
            if source and target:
                arcs.append((source, target))
    output: dict[str, list[str]] = {}
    for source, target in arcs:
        concept = locators.get(source, "")
        label = labels.get(target, "")
        if not concept or not label:
            continue
        keys = {concept, concept.split("_", 1)[-1]}
        for key in keys:
            if label not in output.setdefault(key, []):
                output[key].append(label)
    return output


def read_context_details(root: ET.Element) -> dict[str, ContextInfo]:
    contexts: dict[str, ContextInfo] = {}
    for element in root.iter():
        if local_name(element.tag).lower() != "context":
            continue
        context_id = str(element.attrib.get("id") or "").strip()
        if not context_id:
            continue
        start = ""
        end = ""
        instant = ""
        dimensions: list[tuple[str, str]] = []
        for child in element.iter():
            child_name = local_name(child.tag).lower()
            child_text = parse_date(child.text)
            if child_name == "startdate":
                start = child_text
            elif child_name == "enddate":
                end = child_text
            elif child_name == "instant":
                instant = child_text
            elif child_name in {"explicitmember", "typedmember"}:
                axis = xml_attribute(child, "dimension")
                member = " ".join("".join(child.itertext()).split())
                dimensions.append((axis, member))
        period_end = end or instant
        if period_end:
            contexts[context_id] = ContextInfo(
                period_start=start,
                period_end=period_end,
                context_id=context_id,
                dimensions=tuple(dimensions),
            )
    return contexts


def humanize_xbrl_name(raw: str) -> str:
    text = str(raw or "").replace("_", " ").replace("-", " ")
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    return " ".join(text.lower().split())


def classify_machinery_footnote_concept(
    concept_name: str,
    *,
    labels: list[str],
    period_type: str,
) -> str:
    semantic = " ".join([humanize_xbrl_name(concept_name), *(label.lower() for label in labels)])
    semantic = " ".join(semantic.split())
    reject = {
        "abstract",
        "axis",
        "domain",
        "line items",
        "member",
        "percentage",
        "percent",
        "description",
        "table",
        "growth rate",
    }
    if any(token in semantic for token in reject):
        return ""
    if "remaining performance obligation" in semantic:
        if "practical expedient" in semantic or "expected timing" in semantic:
            return ""
        if any(token in semantic for token in ("next twelve months", "next 12 months", "within one year", "current")):
            return "RemainingPerformanceObligationCurrent"
        return "RemainingPerformanceObligation"
    if period_type == "instant" and "backlog" in semantic:
        if any(token in semantic for token in ("unfunded", "potential", "growth", "cancellation")):
            return ""
        if any(label in semantic for label in ("funded backlog", "authorized backlog", "appropriated backlog")):
            return "FundedBacklog"
        if re.search(r"\b(?:total\s+)?(?:order\s+)?backlog\b", semantic):
            return "ReportedBacklog"
    order_disclosure = bool(
        re.fullmatch(
            r"(?:(?:total|new|net|customer)\s+)?(?:orders|bookings)(?:\s+(?:booked|received))?",
            semantic,
        )
        or any(
            token in semantic
            for token in (
                "total orders",
                "new orders",
                "net orders",
                "orders booked",
                "orders received",
                "order intake",
                "order bookings",
                "bookings received",
                "customer bookings",
            )
        )
    )
    if period_type == "duration" and order_disclosure:
        if not any(
            token in semantic
            for token in (
                "backlog",
                "cancellation",
                "purchase order obligation",
                "purchase orders",
                "percentage",
                "growth",
            )
        ):
            return "Orders"
    return ""


def rpo_dimensions_only(dimensions: tuple[tuple[str, str], ...]) -> bool:
    return all(
        "remainingperformanceobligationexpectedtimingofsatisfaction" in re.sub(r"[^a-z]", "", axis.lower())
        or "rangeaxis" in re.sub(r"[^a-z]", "", axis.lower())
        for axis, _ in dimensions
    )


def consolidated_order_dimensions_only(dimensions: tuple[tuple[str, str], ...]) -> bool:
    """Accept only dimensions whose member explicitly denotes the whole issuer."""
    if not dimensions:
        return True
    total_member_markers = (
        "allsegments",
        "companywide",
        "consolidated",
        "totalcompany",
        "totaloperations",
    )
    allowed_axis_markers = ("business", "consolidation", "entity", "segment")
    for axis, member in dimensions:
        compact_axis = re.sub(r"[^a-z]", "", axis.lower())
        compact_member = re.sub(r"[^a-z]", "", member.lower())
        if not any(marker in compact_axis for marker in allowed_axis_markers):
            return False
        if not any(marker in compact_member for marker in total_member_markers):
            return False
    return True


def practical_expedient_evidence(document_text: str) -> str:
    plain_text = normalize_table_label(strip_html_cell(document_text))
    matches = (
        r"practical expedient.{0,500}(?:does not|do not|not required to) disclose.{0,250}remaining performance obligations?",
        r"remaining performance obligations?.{0,500}practical expedient.{0,250}(?:does not|do not|not required to) disclose",
    )
    for pattern in matches:
        match = re.search(pattern, plain_text, re.IGNORECASE)
        if match:
            return match.group(0)[:1000]
    return ""


def parse_iso_date_value(raw: object) -> date | None:
    parsed = parse_date(raw)
    if not parsed:
        return None
    try:
        return datetime.strptime(parsed, "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_xbrl_duration(raw: object) -> tuple[int, int] | None:
    text = " ".join(str(raw or "").strip().split())
    match = re.search(r"P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)D)?", text, re.IGNORECASE)
    if match and any(match.groups()):
        years = int(match.group(1) or 0)
        months = int(match.group(2) or 0)
        days = int(match.group(3) or 0)
        return years * 12 + months, days
    human = re.search(r"\b(\d+)\s*(years?|months?|days?)\b", text, re.IGNORECASE)
    if not human:
        return None
    amount = int(human.group(1))
    unit = human.group(2).lower()
    if unit.startswith("year"):
        return amount * 12, 0
    if unit.startswith("month"):
        return amount, 0
    return 0, amount


def add_calendar_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    month_ends = (
        31,
        29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
        31,
        30,
        31,
        30,
        31,
        31,
        30,
        31,
        30,
        31,
    )
    return value.replace(year=year, month=month, day=min(value.day, month_ends[month - 1]))


def apply_xbrl_duration(start: date, duration: tuple[int, int]) -> date:
    months, days = duration
    return add_calendar_months(start, months) + timedelta(days=days)


def rpo_timing_start(context: ContextInfo) -> date | None:
    for axis, member in context.dimensions:
        compact_axis = re.sub(r"[^a-z]", "", axis.lower())
        if "remainingperformanceobligationexpectedtimingofsatisfaction" not in compact_axis:
            continue
        match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", member)
        if match:
            return parse_iso_date_value(match.group(1))
    return None


def rpo_timing_percentage_from_text(document_text: str) -> float | None:
    plain = normalize_table_label(strip_html_cell(document_text))
    percentages: set[float] = set()
    anchors = list(re.finditer(r"remaining performance obligations?", plain, re.IGNORECASE))
    horizon = (
        r"(?:next|following|within)\s+(?:the\s+)?(?:12|twelve)\s+months"
        r"|(?:within|during|over)\s+(?:the\s+)?next\s+year"
    )
    for index, match in enumerate(anchors):
        prefix = plain[max(0, match.start() - 240) : match.start()]
        suffix_end = anchors[index + 1].start() if index + 1 < len(anchors) else match.end() + 600
        suffix = plain[match.end() : min(len(plain), suffix_end)]
        before = re.search(
            r"(?:expect(?:s|ed)?|anticipat(?:e|es|ed))\s+(?:to\s+)?recognize.{0,140}?"
            r"(\d{1,3}(?:\.\d+)?)\s*%\s+(?:of\s+(?:(?:our|the)\s+)?)?$",
            prefix,
            re.IGNORECASE,
        )
        if before and re.search(rf"^.{{0,220}}?(?:{horizon})", suffix, re.IGNORECASE):
            percentages.add(round(float(before.group(1)) / 100.0, 10))
        for after in re.finditer(
            rf"(\d{{1,3}}(?:\.\d+)?)\s*%.{{0,180}}?(?:{horizon})",
            suffix,
            re.IGNORECASE,
        ):
            percentages.add(round(float(after.group(1)) / 100.0, 10))
    percentages = {value for value in percentages if 0.0 <= value <= 1.0}
    if len(percentages) != 1:
        return None
    return percentages.pop()


def rpo_current_amount_from_text(document_text: str) -> float | None:
    """Return one explicitly disclosed next-12-month RPO amount, in base units.

    This lane deliberately requires an RPO anchor, recognition language, an
    explicit magnitude word, and a 12-month horizon in the same short passage.
    Conflicting amounts fail closed instead of selecting one heuristically.
    """
    plain = normalize_table_label(strip_html_cell(document_text))
    money = (
        r"(?<![\d.])"
        r"(?:\$|usd\s*)?"
        r"(\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)\s*"
        r"(thousand|million|billion)"
    )
    horizon = (
        r"(?:next|following|within)\s+(?:the\s+)?(?:12|twelve)\s+months"
        r"|(?:within|during|over)\s+(?:the\s+)?next\s+year"
    )
    recognize = (
        r"(?:expect(?:s|ed)?|anticipat(?:e|es|ed))\s+(?:approximately\s+|about\s+)?"
        r"(?:to\s+)?recognize"
    )
    expected = r"(?:is|are|will\s+be)\s+(?:expected|anticipated)\s+to\s+be\s+recognized"
    patterns = (
        rf"{recognize}.{{0,80}}?{money}.{{0,160}}?(?:{horizon})",
        rf"{money}\s*{expected}.{{0,160}}?(?:{horizon})",
        rf"(?:{horizon}).{{0,160}}?{recognize}.{{0,80}}?{money}",
    )
    scale = {"thousand": 1_000.0, "million": 1_000_000.0, "billion": 1_000_000_000.0}
    amounts: set[float] = set()
    for pattern in patterns:
        for match in re.finditer(pattern, plain, re.IGNORECASE):
            context = plain[max(0, match.start() - 260) : min(len(plain), match.end() + 260)]
            if not re.search(r"remaining performance obligations?", context, re.IGNORECASE):
                continue
            raw_value = float(match.group(1).replace(",", ""))
            amounts.add(round(raw_value * scale[match.group(2).lower()], 6))
    if len(amounts) != 1:
        return None
    return amounts.pop()


def rpo_bucket_is_current(
    *,
    report_end: date,
    bucket_start: date,
    bucket_end: date,
) -> bool:
    horizon = add_calendar_months(report_end, 12)
    return (
        report_end - timedelta(days=31) <= bucket_start <= horizon
        and bucket_start < bucket_end
        and bucket_end <= horizon + timedelta(days=7)
    )


def derived_rpo_current_fact(
    *,
    document_name: str,
    period_end: str,
    unit: str,
    value: float,
    method: str,
    evidence: dict[str, Any],
) -> ArchiveFact:
    return ArchiveFact(
        taxonomy="sec-footnote",
        concept_name="RemainingPerformanceObligationCurrent",
        unit=unit,
        value=value,
        period_start="",
        period_end=period_end,
        frame=f"footnote_derived:{document_name}:{period_end}:rpo_current:{method}",
        decimals="",
        payload_json=compact_json(
            {
                "document": document_name,
                "source": FOOTNOTE_XBRL_SOURCE_DETAIL,
                "derivation": method,
                **evidence,
            }
        ),
        source_detail=FOOTNOTE_XBRL_SOURCE_DETAIL,
    )


def derive_cross_source_rpo_current_facts(
    facts: list[ArchiveFact],
    *,
    document_text: str,
    document_name: str,
    filing: dict[str, Any],
    ticker: str,
) -> list[ArchiveFact]:
    """Derive current RPO from one explicit amount or percentage and a report-date total."""
    if str(ticker or "").strip().upper() == "OTIS":
        return facts
    percentage = rpo_timing_percentage_from_text(document_text)
    disclosed_amount = rpo_current_amount_from_text(document_text)
    report_end = parse_date(filing.get("report_date")) or parse_date(filing.get("filing_date"))
    if (percentage is None and disclosed_amount is None) or not report_end:
        return facts

    direct_current = {
        (fact.period_end, fact.unit.lower())
        for fact in facts
        if fact.concept_name == "RemainingPerformanceObligationCurrent"
    }
    totals: dict[tuple[str, str], list[ArchiveFact]] = {}
    for fact in facts:
        if fact.concept_name == "RemainingPerformanceObligation" and fact.period_end == report_end and fact.value >= 0:
            totals.setdefault((fact.period_end, fact.unit.lower()), []).append(fact)

    derived: list[ArchiveFact] = []
    for (period_end, normalized_unit), candidates in totals.items():
        if (period_end, normalized_unit) in direct_current:
            continue
        unique_totals = {round(candidate.value, 6) for candidate in candidates}
        if len(unique_totals) != 1:
            continue
        total = min(candidates, key=lambda candidate: (candidate.source_detail, candidate.frame))
        percentage_value = total.value * percentage if percentage is not None else None
        if (
            disclosed_amount is not None
            and percentage_value is not None
            and not math.isclose(
                disclosed_amount,
                percentage_value,
                rel_tol=0.02,
                abs_tol=1.0,
            )
        ):
            continue
        current_value = disclosed_amount if disclosed_amount is not None else percentage_value
        if current_value is None:
            continue
        if current_value < 0 or current_value > total.value * 1.000001:
            continue
        method = (
            "cross_source_text_disclosed_12_month_amount"
            if disclosed_amount is not None
            else "cross_source_text_disclosed_12_month_percentage"
        )
        derived.append(
            derived_rpo_current_fact(
                document_name=document_name,
                period_end=period_end,
                unit=total.unit,
                value=current_value,
                method=method,
                evidence={
                    "percentage": percentage,
                    "disclosed_amount": disclosed_amount,
                    "total_rpo": total.value,
                    "total_taxonomy": total.taxonomy,
                    "total_concept": total.concept_name,
                    "total_source_detail": total.source_detail,
                },
            )
        )
    return [*facts, *derived]


def parse_machinery_footnote_facts(
    document_text: str,
    *,
    document_name: str,
    filing: dict[str, Any],
    concept_labels: dict[str, list[str]] | None = None,
    ticker: str = "",
) -> list[ArchiveFact]:
    """Extract conservative machinery disclosures from standard/custom Inline XBRL."""
    labels_by_concept = concept_labels or {}
    filing_period_end = parse_date(filing.get("report_date")) or parse_date(filing.get("filing_date"))
    evidence = practical_expedient_evidence(document_text)
    evidence_facts: list[ArchiveFact] = []
    if evidence and filing_period_end:
        evidence_facts.append(
            ArchiveFact(
                taxonomy="sec-footnote",
                concept_name="RPOPracticalExpedient",
                unit="boolean",
                value=1.0,
                period_start="",
                period_end=filing_period_end,
                frame=f"footnote_evidence:{document_name}:rpo_practical_expedient",
                decimals="",
                payload_json=compact_json(
                    {
                        "document": document_name,
                        "source": FOOTNOTE_XBRL_SOURCE_DETAIL,
                        "evidence": evidence,
                        "availability_hint": "EXEMPT",
                    }
                ),
                source_detail=FOOTNOTE_XBRL_SOURCE_DETAIL,
            )
        )
    try:
        root = ET.fromstring(document_text.encode("utf-8"))
    except ET.ParseError:
        cleaned = re.sub(r"<\?xml[^>]*\?>", "", document_text, count=1).strip()
        try:
            root = ET.fromstring(cleaned.encode("utf-8"))
        except ET.ParseError:
            return evidence_facts
    contexts = read_context_details(root)
    units = read_units(root)
    prefix_map = parse_namespace_prefixes(document_text)
    timing_durations: dict[str, tuple[int, int]] = {}
    timing_percentages: dict[str, float] = {}
    for element in root.iter():
        context_ref = xml_attribute(element, "contextref")
        context = contexts.get(context_ref)
        if context is None or not context.dimensions or not rpo_dimensions_only(context.dimensions):
            continue
        raw_name = xml_attribute(element, "name") or local_name(element.tag)
        concept_name = raw_name.split(":", 1)[-1]
        labels = labels_by_concept.get(concept_name, [])
        semantic = " ".join([humanize_xbrl_name(concept_name), *(label.lower() for label in labels)])
        compact_semantic = re.sub(r"[^a-z]", "", semantic.lower())
        if "remainingperformanceobligationexpectedtimingofsatisfactionperiod" in compact_semantic:
            duration = parse_xbrl_duration(" ".join(element.itertext()))
            if duration is not None:
                timing_durations[context_ref] = duration
            continue
        if "remainingperformanceobligation" not in compact_semantic or not any(
            token in compact_semantic for token in ("percentage", "percent")
        ):
            continue
        value = (
            parse_inline_numeric_value(element)
            if local_name(element.tag).lower() in INLINE_XBRL_LOCAL_NAMES
            else parse_float_text(element.text)
        )
        if value is None:
            continue
        fraction = value / 100.0 if 1.0 < value <= 100.0 else value
        if 0.0 <= fraction <= 1.0:
            timing_percentages[context_ref] = fraction

    candidates: dict[tuple[str, str, str, str], list[tuple[int, ArchiveFact]]] = {}
    timing_buckets: list[tuple[ContextInfo, ArchiveFact]] = []
    for element in root.iter():
        element_local = local_name(element.tag)
        element_local_lower = element_local.lower()
        context_ref = xml_attribute(element, "contextref")
        context = contexts.get(context_ref)
        if context is None:
            continue
        unit_ref = xml_attribute(element, "unitref")
        unit = units.get(unit_ref, unit_ref).upper()
        if not unit or unit in {"PURE", "SHARES", "PERCENT"} or "PER" in unit:
            continue
        original_taxonomy = ""
        original_concept = element_local
        value: float | None
        if element_local_lower in INLINE_XBRL_LOCAL_NAMES and xml_attribute(element, "name"):
            raw_name = xml_attribute(element, "name")
            if ":" in raw_name:
                prefix, original_concept = raw_name.split(":", 1)
                original_taxonomy = taxonomy_from_namespace(prefix_map.get(prefix, prefix)) or prefix
            else:
                original_concept = raw_name
            value = parse_inline_numeric_value(element)
        else:
            original_taxonomy = taxonomy_from_namespace(namespace_uri(element.tag)) or namespace_uri(element.tag)
            value = parse_float_text(element.text)
        if value is None:
            continue
        period_type = "duration" if context.period_start else "instant"
        concept_labels_for_fact = [
            *labels_by_concept.get(original_concept, []),
            *labels_by_concept.get(f"{str(original_taxonomy).split(':')[-1]}_{original_concept}", []),
        ]
        if original_concept == "RevenueRemainingPerformanceObligation":
            synthetic_concept = "RemainingPerformanceObligation"
        else:
            synthetic_concept = classify_machinery_footnote_concept(
                original_concept,
                labels=concept_labels_for_fact,
                period_type=period_type,
            )
        if not synthetic_concept:
            continue
        if context.dimensions:
            if synthetic_concept.startswith("RemainingPerformanceObligation"):
                if not rpo_dimensions_only(context.dimensions):
                    continue
            elif synthetic_concept == "Orders":
                if not consolidated_order_dimensions_only(context.dimensions):
                    continue
            else:
                continue
        payload = {
            "document": document_name,
            "source": FOOTNOTE_XBRL_SOURCE_DETAIL,
            "original_taxonomy": original_taxonomy,
            "original_concept": original_concept,
            "labels": concept_labels_for_fact,
            "contextRef": context_ref,
            "dimensions": list(context.dimensions),
            "unitRef": unit_ref,
        }
        fact = ArchiveFact(
            taxonomy="sec-footnote",
            concept_name=synthetic_concept,
            unit=unit,
            value=value,
            period_start=context.period_start,
            period_end=context.period_end,
            frame=f"footnote:{document_name}:{context_ref}:{original_concept}",
            decimals=xml_attribute(element, "decimals"),
            payload_json=compact_json(payload),
            source_detail=FOOTNOTE_XBRL_SOURCE_DETAIL,
        )
        if context.dimensions and synthetic_concept.startswith("RemainingPerformanceObligation"):
            timing_buckets.append((context, fact))
            continue
        key = (synthetic_concept, context.period_start, context.period_end, unit)
        candidates.setdefault(key, []).append((len(context.dimensions), fact))
    facts = list(evidence_facts)
    for (synthetic_concept, _, _, _), grouped in candidates.items():
        grouped.sort(key=lambda item: (item[0], item[1].frame))
        nondimensional = [item for item in grouped if item[0] == 0]
        if nondimensional:
            facts.append(nondimensional[0][1])
            continue
        facts.append(grouped[0][1])

    direct_current_periods = {
        (fact.period_end, fact.unit) for fact in facts if fact.concept_name == "RemainingPerformanceObligationCurrent"
    }
    totals = {
        (fact.period_end, fact.unit): fact for fact in facts if fact.concept_name == "RemainingPerformanceObligation"
    }
    buckets_by_period: dict[tuple[str, str], list[tuple[ContextInfo, ArchiveFact]]] = {}
    for context, fact in timing_buckets:
        buckets_by_period.setdefault((fact.period_end, fact.unit), []).append((context, fact))

    for period_key in sorted(set(buckets_by_period) | set(totals)):
        if period_key in direct_current_periods:
            continue
        period_end, unit = period_key
        period_buckets = buckets_by_period.get(period_key, [])
        report_end = parse_iso_date_value(period_end)
        if report_end is None:
            continue
        starts: dict[date, tuple[ContextInfo, ArchiveFact | None]] = {}
        for context, fact in period_buckets:
            start = rpo_timing_start(context)
            if start is not None:
                current = starts.get(start)
                if current is None or current[1] is None or fact.frame < current[1].frame:
                    starts[start] = (context, fact)
        for context in contexts.values():
            if context.period_end != period_end or (
                context.context_id not in timing_durations and context.context_id not in timing_percentages
            ):
                continue
            start = rpo_timing_start(context)
            if start is not None and start not in starts:
                starts[start] = (context, None)
        ordered = sorted(starts.items())
        current_values: list[float] = []
        current_contexts: list[str] = []
        for index, (start, (context, fact)) in enumerate(ordered):
            if fact is None:
                continue
            duration = timing_durations.get(context.context_id)
            if duration is not None:
                bucket_end = apply_xbrl_duration(start, duration)
            elif index + 1 < len(ordered):
                bucket_end = ordered[index + 1][0]
            else:
                continue
            if rpo_bucket_is_current(report_end=report_end, bucket_start=start, bucket_end=bucket_end):
                current_values.append(fact.value)
                current_contexts.append(context.context_id)
        current_value = sum(current_values) if current_values else None
        method = "timing_bucket_amounts"
        derivation_evidence: dict[str, Any] = {"contexts": current_contexts}
        total = totals.get(period_key)
        if current_value is None and total is not None:
            current_percentages: list[float] = []
            percentage_contexts: list[str] = []
            for index, (start, (context, _)) in enumerate(ordered):
                percentage = timing_percentages.get(context.context_id)
                if percentage is None:
                    continue
                duration = timing_durations.get(context.context_id)
                if duration is not None:
                    bucket_end = apply_xbrl_duration(start, duration)
                elif index + 1 < len(ordered):
                    bucket_end = ordered[index + 1][0]
                else:
                    continue
                if rpo_bucket_is_current(report_end=report_end, bucket_start=start, bucket_end=bucket_end):
                    current_percentages.append(percentage)
                    percentage_contexts.append(context.context_id)
            if current_percentages and sum(current_percentages) <= 1.000001:
                current_value = total.value * sum(current_percentages)
                method = "timing_bucket_percentage"
                derivation_evidence = {
                    "contexts": percentage_contexts,
                    "percentage": sum(current_percentages),
                    "total_rpo": total.value,
                }
        if current_value is None and total is not None and filing_period_end and period_end == filing_period_end:
            text_percentage = rpo_timing_percentage_from_text(document_text)
            disclosed_amount = rpo_current_amount_from_text(document_text)
            percentage_value = total.value * text_percentage if text_percentage is not None else None
            amount_percentage_conflict = (
                disclosed_amount is not None
                and percentage_value is not None
                and not math.isclose(
                    disclosed_amount,
                    percentage_value,
                    rel_tol=0.02,
                    abs_tol=1.0,
                )
            )
            if disclosed_amount is not None and not amount_percentage_conflict:
                current_value = disclosed_amount
                method = "text_disclosed_12_month_amount"
                derivation_evidence = {
                    "disclosed_amount": disclosed_amount,
                    "percentage": text_percentage,
                    "total_rpo": total.value,
                }
            elif percentage_value is not None and not amount_percentage_conflict:
                current_value = percentage_value
                method = "text_disclosed_12_month_percentage"
                derivation_evidence = {
                    "percentage": text_percentage,
                    "total_rpo": total.value,
                }
        if current_value is None or current_value < 0:
            continue
        if total is not None and current_value > total.value * 1.02:
            continue
        facts.append(
            derived_rpo_current_fact(
                document_name=document_name,
                period_end=period_end,
                unit=unit,
                value=current_value,
                method=method,
                evidence=derivation_evidence,
            )
        )
    if str(ticker or "").strip().upper() == "OTIS":
        facts = [
            fact
            for fact in facts
            if not (
                fact.concept_name == "RemainingPerformanceObligationCurrent"
                and fact.frame.startswith("footnote_derived:")
            )
        ]
    return facts


def read_context_periods(root: ET.Element) -> dict[str, tuple[str, str, str]]:
    contexts: dict[str, tuple[str, str, str]] = {}
    for element in root.iter():
        if local_name(element.tag).lower() != "context":
            continue
        if context_has_dimensional_qualifier(element):
            continue
        context_id = str(element.attrib.get("id") or "").strip()
        if not context_id:
            continue
        start = ""
        end = ""
        instant = ""
        for child in element.iter():
            child_name = local_name(child.tag).lower()
            child_text = parse_date(child.text)
            if child_name == "startdate":
                start = child_text
            elif child_name == "enddate":
                end = child_text
            elif child_name == "instant":
                instant = child_text
        period_end = end or instant
        if period_end:
            contexts[context_id] = (start, period_end, context_id)
    return contexts


def element_has_dimension_attribute(element: ET.Element) -> bool:
    return any(local_name(key).lower() == "dimension" for key in element.attrib)


def context_has_dimensional_qualifier(context: ET.Element) -> bool:
    """Return True when a context is segment/scenario-specific rather than consolidated.

    Archive inline-XBRL fallback must ingest only consolidated facts. Segment or
    scenario contexts often carry business-unit, geographic, product, or legal
    entity dimensions that share concept/period/unit values with consolidated
    facts and can otherwise collide during canonical projection.
    """
    for element in context.iter():
        if element is context:
            continue
        element_name = local_name(element.tag).lower()
        if element_name in {"explicitmember", "typedmember"}:
            return True
        if element_has_dimension_attribute(element):
            return True
        if element_name in {"segment", "scenario"}:
            if str(element.text or "").strip() or element.attrib:
                return True
            if any(descendant is not element for descendant in element.iter()):
                return True
    return False


def read_units(root: ET.Element) -> dict[str, str]:
    units: dict[str, str] = {}
    for element in root.iter():
        if local_name(element.tag).lower() != "unit":
            continue
        unit_id = str(element.attrib.get("id") or "").strip()
        if not unit_id:
            continue
        measures: list[str] = []
        for child in element.iter():
            if local_name(child.tag).lower() == "measure" and child.text:
                measures.append(child.text.strip().split(":")[-1].upper())
        if measures:
            units[unit_id] = "*".join(measures)
    return units


def parse_archive_facts(
    document_text: str, *, document_name: str, concept_map: dict[tuple[str, str], list[dict[str, Any]]]
) -> list[ArchiveFact]:
    try:
        root = ET.fromstring(document_text.encode("utf-8"))
    except ET.ParseError:
        cleaned = re.sub(r"<\?xml[^>]*\?>", "", document_text, count=1).strip()
        try:
            root = ET.fromstring(cleaned.encode("utf-8"))
        except ET.ParseError:
            return []
    context_periods = read_context_periods(root)
    units = read_units(root)
    prefix_map = parse_namespace_prefixes(document_text)
    facts: list[ArchiveFact] = []
    seen: set[tuple[str, str, str, str, str, float]] = set()

    for element in root.iter():
        element_local_name = local_name(element.tag)
        element_local_lower = element_local_name.lower()
        context_ref = str(element.attrib.get("contextRef") or element.attrib.get("contextref") or "").strip()
        if not context_ref or context_ref not in context_periods:
            continue
        unit_ref = str(element.attrib.get("unitRef") or element.attrib.get("unitref") or "").strip()
        decimals = str(element.attrib.get("decimals") or "").strip()
        taxonomy = ""
        concept_name = ""
        value: float | None = None

        if element_local_lower in INLINE_XBRL_LOCAL_NAMES and "name" in element.attrib:
            raw_name = str(element.attrib.get("name") or "").strip()
            if ":" in raw_name:
                prefix, concept_name = raw_name.split(":", 1)
                taxonomy = taxonomy_from_namespace(prefix_map.get(prefix, prefix))
            else:
                concept_name = raw_name
            value = parse_inline_numeric_value(element)
        else:
            taxonomy = taxonomy_from_namespace(namespace_uri(element.tag))
            concept_name = element_local_name
            value = parse_float_text(element.text)

        if not taxonomy or not concept_name or value is None:
            continue
        if (taxonomy, concept_name) not in concept_map:
            continue
        period_start, period_end, frame = context_periods[context_ref]
        if not period_end:
            continue
        unit = units.get(unit_ref, unit_ref or "")
        key = (taxonomy, concept_name, unit, period_start, period_end, value)
        if key in seen:
            continue
        seen.add(key)
        facts.append(
            ArchiveFact(
                taxonomy=taxonomy,
                concept_name=concept_name,
                unit=unit,
                value=value,
                period_start=period_start,
                period_end=period_end,
                frame=frame,
                decimals=decimals,
                payload_json=compact_json({"document": document_name, "contextRef": context_ref, "unitRef": unit_ref}),
            )
        )
    return facts


TEXT_TABLE_LABELS: list[tuple[str, str, tuple[str, ...], tuple[str, ...]]] = [
    (
        "Revenue",
        "duration",
        (r"^(?:total\s+)?(?:net\s+)?(?:sales|revenues?|revenue)\b",),
        (r"backlog", r"deferred", r"remaining", r"per\s+share", r"%"),
    ),
    ("CostOfRevenue", "duration", (r"^cost\s+of\s+(?:sales|revenues?|revenue|goods\s+sold)\b",), (r"%",)),
    ("GrossProfit", "duration", (r"^gross\s+profit\b",), (r"%",)),
    (
        "OperatingIncomeLoss",
        "duration",
        (r"^(?:income|loss)\s+from\s+operations\b", r"^operating\s+(?:income|loss)\b"),
        (r"%",),
    ),
    ("NetIncomeLoss", "duration", (r"^net\s+(?:income|loss|earnings)\b",), (r"per\s+share", r"attributable", r"%")),
    ("Assets", "instant", (r"^total\s+assets\b",), ()),
    ("Liabilities", "instant", (r"^total\s+liabilities\b",), (r"and\s+(?:stockholders|shareholders|equity)",)),
    (
        "Equity",
        "instant",
        (r"^total\s+(?:stockholders|shareholders|members|owners).{0,30}equity\b", r"^total\s+equity\b"),
        (r"liabilities",),
    ),
    ("CashAndCashEquivalents", "instant", (r"^cash\s+and\s+cash\s+equivalents\b",), (r"restricted", r"cash\s+flows?")),
    ("Inventory", "instant", (r"^inventor(?:y|ies)\b",), ()),
    ("AccountsReceivable", "instant", (r"^accounts\s+receivable\b", r"^trade\s+receivables\b"), ()),
    ("AccountsPayable", "instant", (r"^accounts\s+payable\b", r"^trade\s+payables\b"), ()),
    (
        "OperatingCashFlow",
        "duration",
        (r"^net\s+cash\s+(?:provided\s+by|used\s+in|provided\s+by\s+\(used\s+in\))\s+operating\s+activities\b",),
        (),
    ),
    (
        "Capex",
        "duration",
        (
            r"^(?:purchases?|payments?)\s+(?:of|to\s+acquire)\s+(?:property|plant|equipment)",
            r"^(?:additions?|acquisitions?)\s+(?:of|to)\s+(?:property|plant|equipment)",
            r"^acquired\s+(?:property|plant|equipment)",
            r"^capital\s+expenditures\b",
        ),
        (),
    ),
    (
        "DepreciationAndAmortization",
        "duration",
        (r"^(?:depreciation\s+and\s+amortization|depreciation\s*&\s*amortization)\b",),
        (r"accumulated", r"%"),
    ),
    ("InterestExpense", "duration", (r"^interest\s+expense\b",), (r"income", r"%")),
    (
        "Orders",
        "duration",
        (
            r"^(?:(?:total|new|net|customer)\s+)?(?:orders|bookings)(?:\s+(?:booked|received))?\b",
            r"^(?:total\s+)?order\s+intake\b",
        ),
        (r"backlog", r"cancell", r"growth", r"purchase", r"%"),
    ),
    (
        "FundedBacklog",
        "instant",
        (r"^(?:funded|authorized|appropriated)\s+(?:order\s+)?backlog\b",),
        (r"unfunded", r"potential", r"remaining\s+performance", r"%"),
    ),
    (
        "ReportedBacklog",
        "instant",
        (
            r"^(?:(?:total|firm)\s+)?(?:order\s+)?backlog"
            r"(?:\s+at\s+(?:period|year)\s+end)?$",
        ),
        (r"funded", r"unfunded", r"potential", r"remaining\s+performance", r"growth", r"%"),
    ),
    (
        "RemainingPerformanceObligation",
        "instant",
        (
            r"^(?:total\s+)?remaining\s+performance\s+obligations?$",
            r"^transaction\s+price\s+allocated\s+to\s+remaining\s+performance\s+obligations?$",
        ),
        (r"expected", r"percentage", r"timing", r"recognized", r"%"),
    ),
    ("ResearchAndDevelopment", "duration", (r"^(?:research\s+and\s+development|r\s*&\s*d)\b",), (r"%",)),
    ("DilutedShares", "duration", (r"^weighted\s+average.{0,35}diluted\s+shares\b",), (r"per\s+share",)),
    ("DebtTotal", "instant", (r"^total\s+(?:debt|borrowings)\b",), ()),
]

# Registration statements frequently embed audited predecessor statements even
# when CompanyFacts has no usable issuer taxonomy. This stricter label set is
# used only for explicitly reviewed ingestion-gap issuers; it avoids mapping
# segment rows, margins, growth rates, and cash-flow working-capital changes as
# consolidated statement values.
REGISTRATION_TEXT_TABLE_LABELS: list[tuple[str, str, tuple[str, ...], tuple[str, ...]]] = [
    (
        "Revenue",
        "duration",
        (
            r"^(?:total\s+)?(?:net\s+)?(?:sales|revenues?|revenue)$",
            r"^operating\s+revenues?$",
        ),
        (),
    ),
    (
        "CostOfRevenue",
        "duration",
        (r"^cost\s+of\s+(?:sales|revenues?|revenue|goods\s+sold)$",),
        (),
    ),
    ("GrossProfit", "duration", (r"^gross\s+profit$",), ()),
    (
        "OperatingIncomeLoss",
        "duration",
        (r"^(?:income|loss)\s+from\s+operations$", r"^operating\s+(?:income|loss)$"),
        (),
    ),
    (
        "PretaxIncome",
        "duration",
        (
            r"^(?:income(?:\s+\(?loss\)?)?|loss)\s+from\s+continuing\s+operations\s+before\s+income\s+taxes$",
            r"^(?:income(?:\s+\(?loss\)?)?|loss)\s+before\s+income\s+taxes$",
        ),
        (),
    ),
    (
        "IncomeTaxExpense",
        "duration",
        (r"^income\s+tax\s+expense(?:\s+\(?benefit\)?)?$",),
        (),
    ),
    (
        "NetIncomeLoss",
        "duration",
        (r"^net\s+(?:income|loss|earnings)(?:\s+\(?loss\)?)?$",),
        (),
    ),
    ("Assets", "instant", (r"^total\s+assets$",), ()),
    ("Liabilities", "instant", (r"^total\s+liabilities$",), ()),
    (
        "Equity",
        "instant",
        (
            r"^total\s+(?:stockholder(?:s|')?|shareholder(?:s|')?|members?|owners?)\s+equity(?:\s+\(?deficit\)?)?$",
            r"^total\s+equity(?:\s+\(?deficit\)?)?$",
        ),
        (r"liabilities",),
    ),
    (
        "CashAndCashEquivalents",
        "instant",
        (r"^cash\s+and\s+cash\s+equivalents$",),
        (r"restricted", r"cash\s+flows?"),
    ),
    (
        "Inventory",
        "instant",
        (r"^inventor(?:y|ies)(?:\s*\(?notes?\s+\d+\)?)?$",),
        (),
    ),
    (
        "AccountsReceivable",
        "instant",
        (r"^accounts\s+receivable(?:\s*-?\s*net)?$", r"^trade\s+receivables(?:\s*-?\s*net)?$"),
        (),
    ),
    ("AccountsPayable", "instant", (r"^accounts\s+payable$", r"^trade\s+payables$"), ()),
    (
        "OperatingCashFlow",
        "duration",
        (
            r"^net\s+cash\s+flows?\s+(?:provided\s+by|used\s+in|provided\s+by\s+\(?used\s+in\)?)\s+operating\s+activities(?:\s*-?\s*continuing\s+operations)?$",
            r"^net\s+cash\s+(?:provided\s+by|used\s+in|provided\s+by\s+\(?used\s+in\)?)\s+operating\s+activities(?:\s*-?\s*continuing\s+operations)?$",
        ),
        (r"discontinued", r"conversion"),
    ),
    (
        "Capex",
        "duration",
        (
            r"^(?:purchases?|payments?)\s+(?:of|to\s+acquire)\s+property\s+plant(?:\s+and)?\s+equipment$",
            r"^(?:additions?|acquisitions?)\s+(?:of|to)\s+property\s+plant(?:\s+and)?\s+equipment$",
            r"^capital\s+expenditures$",
        ),
        (),
    ),
    (
        "DepreciationAndAmortization",
        "duration",
        (r"^(?:depreciation\s+and\s+amortization|depreciation\s*&\s*amortization)$",),
        (),
    ),
    (
        "DepreciationComponent",
        "duration",
        (r"^depreciation(?:\s+of\s+property\s+plant\s+and\s+equipment)?$",),
        (),
    ),
    (
        "AmortizationComponent",
        "duration",
        (r"^amortization\s+of\s+intangible\s+assets$",),
        (),
    ),
    (
        "InterestExpense",
        "duration",
        (
            r"^interest\s+expense(?:\s+and\s+related\s+financing\s+costs)?(?:\s*-?\s*net)?$",
            r"^interest\s+and\s+financing\s+expenses?(?:\s+net)?$",
        ),
        (r"income",),
    ),
    (
        "Orders",
        "duration",
        (
            r"^(?:(?:total|new|net|customer)\s+)?(?:orders|bookings)(?:\s+(?:booked|received))?$",
            r"^(?:(?:total|equipment)\s+)?order\s+intake$",
        ),
        (r"backlog", r"cancell", r"growth", r"purchase"),
    ),
    (
        "FundedBacklog",
        "instant",
        (r"^(?:funded|firm)\s+(?:order\s+)?backlog$",),
        (r"unfunded", r"potential", r"remaining\s+performance"),
    ),
    (
        "ResearchAndDevelopment",
        "duration",
        (r"^(?:research\s+and\s+development|r\s*&\s*d)(?:\s+expenses?)?$",),
        (),
    ),
    (
        "DilutedShares",
        "duration",
        (r"^weighted\s+average.{0,35}diluted\s+shares(?:\s+outstanding)?$",),
        (r"per\s+share",),
    ),
    ("DebtTotal", "instant", (r"^total\s+(?:debt|borrowings)$",), ()),
    (
        "DebtTotal",
        "instant",
        (r"^long-term\s+debt\s+\(?including\s+current\s+portion\)?$",),
        (),
    ),
    (
        "DebtCurrentComponent",
        "instant",
        (r"^current\s+maturities\s+of\s+long-term\s+debt$",),
        (),
    ),
    (
        "DebtNoncurrentComponent",
        "instant",
        (r"^long-term\s+debt(?:\s*-?\s*net)?$",),
        (),
    ),
]

REGISTRATION_STATEMENT_TYPES: dict[str, set[str]] = {
    "Revenue": {"income_statement"},
    "CostOfRevenue": {"income_statement"},
    "GrossProfit": {"income_statement"},
    "OperatingIncomeLoss": {"income_statement"},
    "NetIncomeLoss": {"income_statement"},
    "PretaxIncome": {"income_statement"},
    "IncomeTaxExpense": {"income_statement"},
    "InterestExpense": {"income_statement"},
    "ResearchAndDevelopment": {"income_statement"},
    "DilutedShares": {"income_statement"},
    "Assets": {"balance_sheet"},
    "Liabilities": {"balance_sheet"},
    "Equity": {"balance_sheet"},
    "CashAndCashEquivalents": {"balance_sheet"},
    "Inventory": {"balance_sheet"},
    "AccountsReceivable": {"balance_sheet"},
    "AccountsPayable": {"balance_sheet"},
    "DebtTotal": {"balance_sheet"},
    "DebtCurrentComponent": {"balance_sheet"},
    "DebtNoncurrentComponent": {"balance_sheet"},
    "OperatingCashFlow": {"cash_flow"},
    "Capex": {"cash_flow"},
    "DepreciationAndAmortization": {"cash_flow", "income_statement"},
    "DepreciationComponent": {"cash_flow"},
    "AmortizationComponent": {"cash_flow"},
}

# The statement-semantic guard may only reject a concept when the table carries
# a POSITIVELY recognized statement heading that conflicts with the concept's
# home statement. "financial_summary" is the default returned by
# text_table_statement_provenance when NO formal heading is detected (e.g. IFRS
# foreign private issuers whose income statement is titled "Statement of
# Comprehensive Income"/"Statement of Earnings"). Treating that default as a
# conflict silently dropped every income-statement concept for those filers, so
# it must never trigger the guard.
RECOGNIZED_STATEMENT_TYPES: frozenset[str] = frozenset({"income_statement", "balance_sheet", "cash_flow"})


def strip_html_cell(raw: str) -> str:
    text = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", " ", raw)
    text = re.sub(r"(?is)<br\s*/?>", " ", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = html_lib.unescape(text).replace("\xa0", " ").replace("\u200b", " ").replace("\ufeff", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def html_table_row_items(table_html: str) -> list[tuple[list[str], str]]:
    rows: list[tuple[list[str], str]] = []
    row_matches = list(re.finditer(r"(?is)<tr\b[^>]*>(.*?)</tr>", table_html))
    if not row_matches:
        return rows
    for row_match in row_matches:
        cells = [
            strip_html_cell(match.group(1))
            for match in re.finditer(r"(?is)<t[dh]\b[^>]*>(.*?)</t[dh]>", row_match.group(1))
        ]
        cells = [cell for cell in cells if cell]
        if cells:
            rows.append((cells, row_match.group(0)))
    return rows


def html_table_rows(table_html: str) -> list[list[str]]:
    return [cells for cells, _ in html_table_row_items(table_html)]


def special_metric_row_has_conflicting_xbrl_concept(
    row_html: str,
    concept_name: str,
) -> bool:
    tagged_concepts = re.findall(
        r"\bname\s*=\s*[\"']([^\"']+)[\"']",
        row_html,
        re.IGNORECASE,
    )
    if not tagged_concepts:
        return False
    expected_fragments = {
        "Orders": ("order", "booking"),
        "FundedBacklog": ("fundedbacklog", "authorizedbacklog", "appropriatedbacklog"),
        "ReportedBacklog": ("backlog",),
        "RemainingPerformanceObligation": (
            "remainingperformanceobligation",
            "transactionpriceallocatedtoremainingperformanceobligation",
        ),
    }.get(concept_name, ())
    normalized = [re.sub(r"[^a-z0-9]", "", tagged.split(":")[-1].lower()) for tagged in tagged_concepts]
    return bool(expected_fragments) and not any(
        fragment in tagged for tagged in normalized for fragment in expected_fragments
    )


BACKLOG_ORDER_TEXT_CONCEPTS = {
    "Orders",
    "FundedBacklog",
    "ReportedBacklog",
    "RemainingPerformanceObligation",
}
# Backlog/orders aggregates below this are noise for this universe: verified
# junk captures ($200K "backlog" at a $3.7B-revenue filer, raw "21.0" from an
# undetected millions scale) all sit far below any genuine disclosure.
BACKLOG_ORDER_MIN_PLAUSIBLE_USD = 1_000_000.0


def table_scale(text: str) -> float:
    return table_scale_info(text)[0]


def table_scale_info(text: str) -> tuple[float, str, str]:
    normalized = re.sub(r"\s+", " ", text.lower())
    scale_patterns: tuple[tuple[float, str], ...] = (
        (
            1_000_000_000.0,
            r"(?:in|amounts in|presented in|expressed in|stated in|\$\s*in)\s+billions|\bbillions\b|\$000000000\b",
        ),
        (
            1_000_000.0,
            r"(?:in|amounts in|presented in|expressed in|stated in|\$\s*in)\s+millions|\bmillions\b|\$000000\b",
        ),
        (1_000.0, r"(?:in|amounts in|presented in|expressed in|stated in|\$\s*in)\s+thousands|\bthousands\b|\$000\b"),
    )
    for scale, pattern in scale_patterns:
        match = re.search(pattern, normalized)
        if match:
            return scale, match.group(0), "high"
    return 1.0, "not_detected_default_units", "low"


def document_default_scale_info(document_text: str) -> tuple[float, str, str]:
    """Read only explicit document-wide amount conventions, not narrative magnitudes."""
    normalized = re.sub(
        r"\s+",
        " ",
        strip_html_cell(document_text[:1_000_000]).lower(),
    )
    scale_patterns: tuple[tuple[float, str], ...] = (
        (
            1_000_000_000.0,
            r"\b(?:dollar(?:\s+and\s+share)?\s+amounts?|amounts?)\s+(?:are\s+)?in\s+billions\b",
        ),
        (
            1_000_000.0,
            r"\b(?:dollar(?:\s+and\s+share)?\s+amounts?|amounts?)\s+(?:are\s+)?in\s+millions\b",
        ),
        (
            1_000.0,
            r"\b(?:dollar(?:\s+and\s+share)?\s+amounts?|amounts?)\s+(?:are\s+)?in\s+thousands\b",
        ),
    )
    for scale, pattern in scale_patterns:
        match = re.search(pattern, normalized)
        if match:
            return scale, f"document_default:{match.group(0)}", "high"
    return 1.0, "document_default_not_detected", "low"


def detect_text_currency(text: str, *, allow_symbol_only: bool) -> str:
    """Return the positively identified currency code, or "" when none is detected (FN-5)."""
    normalized = re.sub(r"\s+", " ", strip_html_cell(text).lower())
    currency_patterns: tuple[tuple[str, tuple[str, ...]], ...] = (
        (
            "CAD",
            (
                r"\b(?:in|expressed in|presented in|stated in|reported in)\s+(?:thousands|millions)?(?:\s+of)?\s+(?:canadian dollars|cad|cdn dollars)\b",
                r"\b(?:cad|cdn)\s?\$",
                r"\bc\$\s?(?:000|millions?|thousands?)?\b",
            ),
        ),
        (
            "GBP",
            (
                r"\b(?:in|expressed in|presented in|stated in|reported in)\s+(?:thousands|millions)?(?:\s+of)?\s+(?:pounds sterling|gbp|pounds)\b",
                r"\bpounds sterling\b",
                r"\bgbp\b",
                r"£\s?(?:000|millions?|thousands?)?\b",
            ),
        ),
        (
            "EUR",
            (
                r"\b(?:in|expressed in|presented in|stated in|reported in)\s+(?:thousands|millions)?(?:\s+of)?\s+(?:euros?|eur)\b",
                r"\b(?:eur)\s?(?:000|millions?|thousands?)\b",
                r"€\s?(?:000|millions?|thousands?)?\b",
            ),
        ),
        (
            "ILS",
            (
                r"\b(?:in|expressed in|presented in|stated in|reported in)\s+(?:thousands|millions)?(?:\s+of)?\s+(?:new israeli shekels?|israeli shekels?|shekels?|nis|ils)\b",
                r"\bnew israeli shekels?\b",
                r"\bnis\s?(?:000|millions?|thousands?)\b",
                r"₪\s?(?:000|millions?|thousands?)?\b",
            ),
        ),
        (
            "JPY",
            (
                r"\b(?:in|expressed in|presented in|stated in|reported in)\s+(?:thousands|millions|billions)?(?:\s+of)?\s+(?:japanese yen|yen|jpy)\b",
                r"\bjapanese yen\b",
                r"\bjpy\b",
                r"¥\s?(?:000|millions?|thousands?)?\b",
            ),
        ),
        (
            "CHF",
            (
                r"\b(?:in|expressed in|presented in|stated in|reported in)\s+(?:thousands|millions)?(?:\s+of)?\s+(?:swiss francs?|chf)\b",
                r"\bswiss francs?\b",
                r"\bchf\s?(?:000|millions?|thousands?)\b",
            ),
        ),
        (
            "AUD",
            (
                r"\b(?:in|expressed in|presented in|stated in|reported in)\s+(?:thousands|millions)?(?:\s+of)?\s+(?:australian dollars|aud)\b",
                r"\baud\s?(?:000|millions?|thousands?)\b",
                r"\ba\$\s?(?:000|millions?|thousands?)\b",
            ),
        ),
        (
            "SEK",
            (
                r"\b(?:in|expressed in|presented in|stated in|reported in)\s+(?:thousands|millions)?(?:\s+of)?\s+(?:swedish kronor|swedish krona|sek)\b",
                r"\bswedish kronor\b",
                r"\bsek\s?(?:000|millions?|thousands?)\b",
            ),
        ),
    )
    for currency, patterns in currency_patterns:
        for pattern in patterns:
            if re.search(pattern, normalized) and (allow_symbol_only or not pattern.startswith(("£", "€", "₪", "¥"))):
                return currency
    return ""


def text_table_unit(document_text: str, context_text: str = "", *, company_currency: str = "") -> tuple[str, str]:
    """Return (currency_unit, currency_confidence) for a text-table extraction (FN-5).

    Confidence is "high" when the document positively identifies the currency
    (or the issuer is a known USD filer), "low" when no in-document evidence was
    found and we fell back to dim_company.currency for a non-USD filer.
    """
    context_currency = detect_text_currency(context_text, allow_symbol_only=True)
    if context_currency:
        return context_currency, "high"
    intro_currency = detect_text_currency(document_text[:50000], allow_symbol_only=False)
    if intro_currency:
        return intro_currency, "high"
    company = str(company_currency or "").strip().upper()
    if company and company != "USD":
        # Non-US filer whose document did not positively identify a currency:
        # cross-check against dim_company.currency instead of failing open to
        # USD (a shekel/yen filing stored as USD would silently misstate values).
        return company, "low"
    return "USD", "high"


def parse_table_number(raw: str) -> float | None:
    text = html_lib.unescape(str(raw or "")).replace("\xa0", " ").strip()
    if not text or "%" in text:
        return None
    text = (
        text.replace("$", "").replace("£", "").replace("€", "").replace(",", "").replace("*", "").replace("\u200b", "")
    )
    text = re.sub(r"\[[^\]]*\]|\([a-zA-Z]\)|\b[a-zA-Z]\b", "", text).strip()
    if text in {"-", "--", "---", "N/A", "n/a"}:
        return None
    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1].strip()
    match = re.fullmatch(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    value = float(text)
    return -abs(value) if negative else value


def normalize_table_label(raw: str) -> str:
    text = html_lib.unescape(str(raw or "")).replace("\xa0", " ")
    text = re.sub(r"\[[^\]]*\]|\([a-zA-Z]\)|\b[a-zA-Z]\b", " ", text)
    text = re.sub(r"[^A-Za-z0-9%&()'/ -]+", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def text_table_label_concept(label: str) -> tuple[str, str] | None:
    normalized = normalize_table_label(label)
    if not normalized or len(normalized) > 180:
        return None
    for concept_name, period_type, allow_patterns, reject_patterns in TEXT_TABLE_LABELS:
        if any(re.search(pattern, normalized) for pattern in reject_patterns):
            continue
        if any(re.search(pattern, normalized) for pattern in allow_patterns):
            return concept_name, period_type
    return None


def registration_text_table_label_concept(label: str) -> tuple[str, str] | None:
    normalized = normalize_table_label(label)
    normalized = re.sub(r"\s*\((?:note\s*)?\d+\)\s*$", "", normalized)
    if not normalized or len(normalized) > 180:
        return None
    for concept_name, period_type, allow_patterns, reject_patterns in REGISTRATION_TEXT_TABLE_LABELS:
        if any(re.search(pattern, normalized) for pattern in reject_patterns):
            continue
        if any(re.search(pattern, normalized) for pattern in allow_patterns):
            return concept_name, period_type
    return None


def row_values(cells: list[str]) -> list[float]:
    values: list[float] = []
    value_cells = cells[1:]
    idx = 0
    while idx < len(value_cells):
        cell = value_cells[idx].strip()
        if cell in {"$", "US$", "USD", "C$", "CAD", "Â£", "â‚¬", "Â¥"}:
            idx += 1
            continue
        combined = cell
        if cell.startswith("(") and not cell.endswith(")") and idx + 1 < len(value_cells):
            closing = value_cells[idx + 1].strip()
            if closing == ")":
                combined = f"{cell})"
                idx += 1
        value = parse_table_number(combined)
        if value is not None:
            values.append(value)
        idx += 1
    return values


def text_table_statement_provenance(context_text: str, table_text: str) -> tuple[str, int, int]:
    """Classify a table without treating forecasts/pro-forma tables as history."""
    normalized = normalize_table_label(f"{context_text} {table_text[:1200]}")
    statement_patterns = {
        "cash_flow": r"\bsta\s*tements?\s+of\s+cash\s+flows?\b",
        "balance_sheet": r"\b(?:balance\s+sheets?|sta\s*tements?\s+of\s+financial\s+position)\b",
        "income_statement": r"\bsta\s*tements?\s+of\s+(?:operations|income)(?:\s+loss)?\b",
    }
    statement_matches = [
        (match.start(), statement_type)
        for statement_type, pattern in statement_patterns.items()
        for match in re.finditer(pattern, normalized)
    ]
    latest_statement_position = max((position for position, _ in statement_matches), default=-1)
    statement_type = max(statement_matches, key=lambda item: item[0])[1] if statement_matches else "financial_summary"
    projection_matches = list(
        re.finditer(
            r"\b(?:unaudited\s+pro\s+forma|pro\s+forma|projected|projection|forecast|estimated\s+financial)\b",
            normalized,
        )
    )
    latest_projection_position = max((match.start() for match in projection_matches), default=-1)
    # A pro-forma footnote can immediately precede the next historical
    # statement. The later formal statement heading resets that context.
    projection_flag = int(
        latest_projection_position >= 0
        and (
            latest_projection_position > latest_statement_position
            or latest_statement_position - latest_projection_position <= 100
        )
    )
    formal_statement = bool(
        re.search(
            r"\b(?:condensed\s+)?(?:c\s*o\s*n\s*s\s*o\s*l\s*i\s*d\s*a\s*t\s*e\s*d|onsolidated|combined)\s+"
            r"(?:balance\s+sheets?|sta\s*tements?\s+of\s+(?:operations|income(?:\s+loss)?|cash\s+flows?|financial\s+position))\b",
            normalized,
        )
    )
    historical_statement_flag = int(formal_statement and projection_flag == 0)
    return statement_type, projection_flag, historical_statement_flag


MONTH_LOOKUP = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


def month_number(raw: str) -> int | None:
    token = re.sub(r"[^A-Za-z]", "", raw).lower()
    return MONTH_LOOKUP.get(token)


def period_iso(year: int, month: int, day: int) -> str:
    try:
        return datetime(year, month, day).date().isoformat()
    except ValueError:
        return ""


def month_day_pairs(text: str) -> list[tuple[int, int, str]]:
    pairs: list[tuple[int, int, str]] = []
    month_names = "|".join(sorted(MONTH_LOOKUP, key=len, reverse=True))
    for match in re.finditer(rf"\b({month_names})\.?\s+(\d{{1,2}})(?:,|\b)", text, flags=re.IGNORECASE):
        month = month_number(match.group(1))
        day = int(match.group(2))
        if month is not None:
            pairs.append((month, day, match.group(0)))
    return pairs


def full_text_dates(text: str) -> list[tuple[str, str]]:
    dates: list[tuple[str, str]] = []
    month_names = "|".join(sorted(MONTH_LOOKUP, key=len, reverse=True))
    for match in re.finditer(
        rf"\b({month_names})\.?\s+(\d{{1,2}}),?\s+((?:19|20)\d{{2}})\b", text, flags=re.IGNORECASE
    ):
        month = month_number(match.group(1))
        if month is None:
            continue
        value = period_iso(int(match.group(3)), month, int(match.group(2)))
        if value:
            dates.append((value, match.group(0)))
    return dates


def standalone_years(text: str) -> list[int]:
    return [int(match.group(1)) for match in re.finditer(r"(?<![\d.])((?:19|20)\d{2})(?![\d.])", text)]


def duration_days_from_table_context(text: str) -> int:
    normalized = re.sub(r"\s+", " ", text.lower())
    if re.search(
        r"\bthree[- ]months?(?:\s+period)?\s+ended\b|\bquarter(?:ly)?\s+ended\b",
        normalized,
    ):
        return 90
    if "six months ended" in normalized:
        return 181
    if "nine months ended" in normalized:
        return 273
    return 364


def duration_days_for_period_evidence(
    *, month: int | None, day: int | None, combined_text: str, default_days: int, form_type: str = ""
) -> int:
    normalized = re.sub(r"\s+", " ", combined_text.lower())
    form = str(form_type or "").upper()
    if month == 12 and day in {30, 31} and re.search(r"\byears?\s+ended\b|\bfiscal\s+years?\b", normalized):
        return 364
    if re.search(
        r"\bthree[- ]months?(?:\s+period)?\s+ended\b|\bquarter(?:ly)?\s+ended\b",
        normalized,
    ) and not (month == 12 and day in {30, 31}):
        return 90
    if re.search(r"\bsix\s+months?\s+ended\b", normalized):
        return 181
    if re.search(r"\bnine\s+months?\s+ended\b", normalized):
        return 273
    if form == "6-K" and not re.search(r"\byears?\s+ended\b|\bfiscal\s+years?\b", normalized):
        if month == 3 and day in {30, 31}:
            return 90
        if month == 6 and day in {29, 30}:
            return 181
        if month == 9 and day in {29, 30}:
            return 273
    return default_days


def infer_text_table_periods(
    header_rows: list[list[str]],
    *,
    value_count: int,
    fallback_period_end: str,
    context_text: str,
    form_type: str = "",
    strict_mixed_periods: bool = False,
) -> list[tuple[str, int, str, str]]:
    header_text = " ".join(cell for row in header_rows for cell in row if cell)
    combined = re.sub(r"\s+", " ", f"{header_text} {strip_html_cell(context_text[:1200])}").strip()
    duration_days = duration_days_from_table_context(combined)
    full_dates = full_text_dates(header_text)
    if len(full_dates) >= value_count:
        inferred_full: list[tuple[str, int, str, str]] = []
        for period_end, evidence in full_dates[:value_count]:
            parsed = datetime.strptime(period_end, "%Y-%m-%d").date()
            inferred_full.append(
                (
                    period_end,
                    duration_days_for_period_evidence(
                        month=parsed.month,
                        day=parsed.day,
                        combined_text=combined,
                        default_days=duration_days,
                        form_type=form_type,
                    ),
                    "table_column_date",
                    evidence,
                )
            )
        return inferred_full

    pairs = month_day_pairs(header_text)
    years = standalone_years(header_text)
    if strict_mixed_periods and years and len(pairs) >= 2:
        duplicate_boundary = next(
            (idx + 1 for idx in range(len(years) - 1) if years[idx] == years[idx + 1]),
            None,
        )
        if duplicate_boundary is not None and duplicate_boundary < value_count:
            grouped_pairs = [pairs[0]] * duplicate_boundary + [pairs[1]] * (value_count - duplicate_boundary)
            inferred_mixed: list[tuple[str, int, str, str]] = []
            for year, (month, day, pair_evidence) in zip(years[:value_count], grouped_pairs, strict=False):
                period_end = period_iso(year, month, day)
                if period_end:
                    inferred_mixed.append(
                        (
                            period_end,
                            duration_days_for_period_evidence(
                                month=month,
                                day=day,
                                combined_text=combined,
                                default_days=duration_days,
                                form_type=form_type,
                            ),
                            "table_column_mixed_period_group",
                            f"{pair_evidence} {year}",
                        )
                    )
            if len(inferred_mixed) == value_count:
                return inferred_mixed
    if strict_mixed_periods and years and not pairs:
        context_pairs = month_day_pairs(strip_html_cell(context_text[-1200:]))
        if context_pairs:
            pairs = [context_pairs[-1]]
    if years and pairs:
        inferred: list[tuple[str, int, str, str]] = []
        if len(pairs) == 1:
            month, day, pair_evidence = pairs[0]
            for year in years[:value_count]:
                period_end = period_iso(year, month, day)
                if period_end:
                    inferred.append(
                        (
                            period_end,
                            duration_days_for_period_evidence(
                                month=month,
                                day=day,
                                combined_text=combined,
                                default_days=duration_days,
                                form_type=form_type,
                            ),
                            "table_column_month_day_year",
                            f"{pair_evidence} {year}",
                        )
                    )
        else:
            for idx, year in enumerate(years[:value_count]):
                month, day, pair_evidence = pairs[min(idx, len(pairs) - 1)]
                period_end = period_iso(year, month, day)
                if period_end:
                    inferred.append(
                        (
                            period_end,
                            duration_days_for_period_evidence(
                                month=month,
                                day=day,
                                combined_text=combined,
                                default_days=duration_days,
                                form_type=form_type,
                            ),
                            "table_column_month_day_year",
                            f"{pair_evidence} {year}",
                        )
                    )
        if len(inferred) == value_count:
            return inferred

    if years and re.search(r"\byears?\s+ended\b|\bfiscal\s+years?\b", combined, re.IGNORECASE):
        inferred = [
            (period_iso(year, 12, 31), 364, "table_column_year_default_dec31", str(year))
            for year in years[:value_count]
        ]
        inferred = [item for item in inferred if item[0]]
        if len(inferred) == value_count:
            return inferred

    return [
        (fallback_period_end, duration_days, "fallback_filing_or_report_date", fallback_period_end)
        for _ in range(value_count)
    ]


def text_table_period_count(header_rows: list[list[str]]) -> int:
    header_text = " ".join(cell for row in header_rows for cell in row if cell)
    dates = full_text_dates(header_text)
    years = standalone_years(header_text)
    return max(len(dates), len(years))


def registration_actual_period_count(header_rows: list[list[str]]) -> int:
    expected = text_table_period_count(header_rows)
    header_text = normalize_table_label(" ".join(cell for row in header_rows for cell in row))
    if "pro forma" not in header_text:
        return expected
    years = standalone_years(header_text)
    for idx in range(1, len(years)):
        if years[idx] >= years[idx - 1]:
            return idx
    return expected


def align_registration_table_values(cells: list[str], header_rows: list[list[str]]) -> list[float]:
    """Align statement values to dated columns and discard notes/change columns."""
    values = row_values(cells)
    expected = registration_actual_period_count(header_rows)
    if expected <= 0 or len(values) < expected:
        return []
    if len(values) > expected:
        header_text = normalize_table_label(" ".join(cell for row in header_rows for cell in row))
        excess = len(values) - expected
        if "note" in header_text and excess > 0:
            # Statement note references are leading small integers. Remove no
            # more than the number of excess numeric cells, preserving values.
            while excess > 0 and values and values[0].is_integer() and 0 < values[0] < 100:
                values.pop(0)
                excess -= 1
        if len(values) > expected:
            values = values[:expected]
    return values if len(values) == expected else []


def align_operating_table_values(
    cells: list[str],
    header_rows: list[list[str]],
) -> list[float]:
    """Keep dated monetary columns and discard adjacent percent/change columns."""
    expected = text_table_period_count(header_rows)
    if expected <= 0:
        return row_values(cells)
    currency_markers = {"$", "US$", "USD", "C$", "CAD", "GBP", "EUR"}
    marked_values: list[float] = []
    value_cells = cells[1:]
    for index, cell in enumerate(value_cells[:-1]):
        if cell.strip() not in currency_markers:
            continue
        for candidate in value_cells[index + 1 :]:
            if candidate.strip() in currency_markers:
                break
            value = parse_table_number(candidate)
            if value is not None:
                marked_values.append(value)
                break
    if len(marked_values) >= expected:
        return marked_values[:expected]
    values = row_values(cells)
    return values[:expected] if len(values) > expected else values


def consolidated_dimension_column_index(rows: list[list[str]]) -> int | None:
    """Return the numeric value index for an explicit consolidated matrix column."""
    if not rows or len(rows[0]) < 3 or text_table_period_count([rows[0]]) > 0:
        return None
    headers = [normalize_table_label(cell) for cell in rows[0][1:]]
    if headers[-1] not in {"consolidated", "total company", "company total"}:
        return None
    if not any("segment" in header for header in headers[:-1]):
        return None
    return len(headers) - 1


def period_start_for_text_fact(period_end: str, period_type: str, duration_days: int) -> str:
    if period_type == "instant":
        return ""
    parsed = parse_date(period_end)
    if not parsed:
        return ""
    end = datetime.strptime(parsed, "%Y-%m-%d").date()
    return (end - timedelta(days=duration_days)).isoformat()


def prefer_explicit_total_order_facts(facts: list[ArchiveFact]) -> list[ArchiveFact]:
    """Discard segment order rows when the same document reports an explicit total."""
    groups: dict[tuple[str, str, str], list[ArchiveFact]] = {}
    for fact in facts:
        if fact.concept_name != "Orders":
            continue
        groups.setdefault((fact.period_start, fact.period_end, fact.unit), []).append(fact)

    preferred_ids: set[int] = set()
    suppressed_ids: set[int] = set()
    for grouped in groups.values():
        explicit_totals: list[ArchiveFact] = []
        for fact in grouped:
            try:
                label = normalize_table_label(json.loads(fact.payload_json).get("label", ""))
            except (json.JSONDecodeError, TypeError):
                label = ""
            if re.match(r"^(?:total|consolidated|companywide|company wide)\s+(?:orders|bookings)\b", label):
                explicit_totals.append(fact)
        if not explicit_totals:
            continue
        preferred_ids.update(id(fact) for fact in explicit_totals)
        suppressed_ids.update(id(fact) for fact in grouped if fact not in explicit_totals)

    return [fact for fact in facts if id(fact) not in suppressed_ids or id(fact) in preferred_ids]


def parse_archive_text_table_facts(
    document_text: str,
    *,
    document_name: str,
    filing: dict[str, Any],
    company_currency: str = "",
    special_metrics_only: bool = False,
    strict_registration_statements: bool = False,
) -> list[ArchiveFact]:
    lower_document_name = document_name.lower()
    if lower_document_name.endswith(("-index.html", "-index-headers.html")):
        return []
    period_end = parse_date(filing.get("report_date")) or parse_date(filing.get("filing_date"))
    form_type = str(filing.get("form_type") or "").strip().upper()
    if not period_end:
        return []
    document_default_scale = document_default_scale_info(document_text)
    facts: list[ArchiveFact] = []
    seen: set[tuple[str, str, str, float]] = set()
    for table_index, match in enumerate(re.finditer(r"(?is)<table\b[^>]*>.*?</table>", document_text), start=1):
        table_html = match.group(0)
        if special_metrics_only and not re.search(
            r"(?i)\b(?:orders?|bookings?|backlog|remaining\s+performance\s+obligations?)\b",
            table_html,
        ):
            continue
        row_items = html_table_row_items(table_html)
        rows = [cells for cells, _ in row_items]
        label_resolver = (
            registration_text_table_label_concept if strict_registration_statements else text_table_label_concept
        )
        concept_rows = [label_resolver(row[0]) if row else None for row in rows]
        if special_metrics_only:
            concept_rows = [
                concept
                if concept is not None
                and concept[0]
                in {
                    "Orders",
                    "FundedBacklog",
                    "ReportedBacklog",
                    "RemainingPerformanceObligation",
                }
                else None
                for concept in concept_rows
            ]
        concept_row_flags = [concept is not None for concept in concept_rows]
        special_operating_rows = [
            concept
            for concept in concept_rows
            if concept is not None
            and concept[0]
            in {
                "Orders",
                "FundedBacklog",
                "ReportedBacklog",
                "RemainingPerformanceObligation",
            }
        ]
        minimum_concept_rows = 1 if strict_registration_statements else 2
        if sum(1 for flag in concept_row_flags if flag) < minimum_concept_rows and not special_operating_rows:
            continue
        table_text = strip_html_cell(table_html)
        scale_context = document_text[max(0, match.start() - 2500) : min(len(document_text), match.end() + 500)]
        # Registration statements often place the formal "Consolidated ..."
        # heading more than 1,200 characters before a table. Keep the context
        # window aligned with scale detection so historical statement tables
        # are not mistaken for unaudited prospectus summaries.
        provenance_context = document_text[max(0, match.start() - 2500) : match.start()]
        statement_type, projection_flag, historical_statement_flag = text_table_statement_provenance(
            strip_html_cell(provenance_context),
            table_text,
        )
        currency_context = (
            f"{table_text[:1200]} {strip_html_cell(provenance_context)[-500:]}"
            if strict_registration_statements
            else scale_context
        )
        if (
            strict_registration_statements
            and str(company_currency or "").strip().upper() == "USD"
            and "$" in table_text[:1200]
        ):
            unit, currency_confidence = "USD", "high"
        else:
            unit, currency_confidence = text_table_unit(
                document_text,
                currency_context,
                company_currency=company_currency,
            )
        normalized_table_text = f"{strip_html_cell(scale_context)} {table_text}".lower()
        financial_table = any(
            marker in normalized_table_text
            for marker in (
                "consolidated",
                "summary financial",
                "statement of operations",
                "statement of income",
                "balance sheet",
                "cash flows",
                "financial position",
            )
        )
        operating_table = bool(special_operating_rows) and any(
            marker in normalized_table_text
            for marker in ("orders", "bookings", "backlog", "remaining performance obligation")
        )
        has_actual_columns = "actual" in normalize_table_label(table_text[:500])
        if strict_registration_statements and projection_flag and not has_actual_columns:
            continue
        if (
            strict_registration_statements
            and not historical_statement_flag
            and not (projection_flag and has_actual_columns)
            and not operating_table
        ):
            continue
        if not financial_table and not operating_table:
            continue
        scale, scale_source, scale_confidence = table_scale_info(f"{scale_context} {table_text[:1000]}")
        if scale_confidence == "low" and document_default_scale[2] == "high":
            scale, scale_source, scale_confidence = document_default_scale
        if currency_confidence == "low":
            # FN-5: currency not positively identified for a non-US filer;
            # the scale evidence cannot be trusted more than the currency.
            scale_confidence = "low"
        first_concept_idx = next((idx for idx, flag in enumerate(concept_row_flags) if flag), 0)
        table_header_rows = rows[:first_concept_idx]
        consolidated_value_index = consolidated_dimension_column_index(rows)
        component_facts: dict[tuple[str, str], dict[str, ArchiveFact]] = {}
        for row_index, cells in enumerate(rows):
            if len(cells) < 2:
                continue
            label_result = label_resolver(cells[0])
            if label_result is None:
                continue
            concept_name, period_type = label_result
            if special_metrics_only and concept_name not in BACKLOG_ORDER_TEXT_CONCEPTS:
                continue
            if concept_name in BACKLOG_ORDER_TEXT_CONCEPTS and special_metric_row_has_conflicting_xbrl_concept(
                row_items[row_index][1],
                concept_name,
            ):
                continue
            allowed_statement_types = REGISTRATION_STATEMENT_TYPES.get(concept_name)
            if (
                not strict_registration_statements
                and allowed_statement_types is not None
                and statement_type in RECOGNIZED_STATEMENT_TYPES
                and statement_type not in allowed_statement_types
            ):
                # A row named "Inventories" on the cash-flow statement is a
                # working-capital change, not an inventory balance. Apply the
                # same statement-semantic guard used for prospectuses whenever
                # a normal filing table has a POSITIVELY recognized statement
                # heading. When the heading is unrecognized (financial_summary),
                # there is no conflicting statement context, so the concept must
                # be kept — otherwise IFRS foreign private issuers ("Statement of
                # Comprehensive Income"/"Earnings") lose every income-statement
                # concept and fail the FPI-hybrid completeness gate.
                continue
            nearby_header_rows = [
                row for row in rows[max(0, row_index - 8) : row_index] if not (row and label_resolver(row[0]))
            ]
            # Long KPI tables can place an Orders row more than eight rows
            # below its date heading. Always retain the table-level header,
            # then add any local subheader rows without duplicating it.
            local_header_rows = [
                *table_header_rows,
                *(row for row in nearby_header_rows if row not in table_header_rows),
            ] or nearby_header_rows
            if strict_registration_statements:
                # Formal statement headers define the dated columns for the
                # entire table. Nearby component rows can otherwise push a
                # multi-row header outside the local lookback window.
                local_header_rows = table_header_rows
                if allowed_statement_types is not None and statement_type not in allowed_statement_types:
                    continue
                preceding_labels = normalize_table_label(
                    " ".join(row[0] for row in rows[max(0, row_index - 6) : row_index] if row)
                )
                if concept_name == "NetIncomeLoss" and "per share" in preceding_labels:
                    continue
                values = align_registration_table_values(cells, local_header_rows)
                if not values and local_header_rows != table_header_rows:
                    local_header_rows = table_header_rows
                    values = align_registration_table_values(cells, local_header_rows)
            else:
                if concept_name in BACKLOG_ORDER_TEXT_CONCEPTS and consolidated_value_index is not None:
                    dimension_values = row_values(cells)
                    values = (
                        [dimension_values[consolidated_value_index]]
                        if len(dimension_values) > consolidated_value_index
                        else []
                    )
                else:
                    values = (
                        align_operating_table_values(cells, local_header_rows)
                        if concept_name in BACKLOG_ORDER_TEXT_CONCEPTS
                        else row_values(cells)
                    )
            if not values:
                continue
            period_context = provenance_context if strict_registration_statements else scale_context
            if consolidated_value_index is not None:
                duration_days = duration_days_from_table_context(f"{period_context} {table_text[:1200]}")
                periods = [
                    (
                        period_end,
                        duration_days,
                        "explicit_consolidated_dimension_column",
                        rows[0][-1],
                    )
                ]
            else:
                periods = infer_text_table_periods(
                    local_header_rows,
                    value_count=len(values),
                    fallback_period_end=period_end,
                    context_text=period_context,
                    form_type=form_type,
                    strict_mixed_periods=strict_registration_statements,
                )
            if table_header_rows and all(item[2] == "fallback_filing_or_report_date" for item in periods):
                periods = infer_text_table_periods(
                    table_header_rows,
                    value_count=len(values),
                    fallback_period_end=period_end,
                    context_text=period_context,
                    form_type=form_type,
                    strict_mixed_periods=strict_registration_statements,
                )
            for value_index, raw_value in enumerate(values):
                fact_period_end, duration_days, period_confidence, period_evidence = periods[
                    min(value_index, len(periods) - 1)
                ]
                if period_type == "duration" and period_confidence == "fallback_filing_or_report_date":
                    continue
                value = raw_value * scale
                if concept_name in BACKLOG_ORDER_TEXT_CONCEPTS and (
                    period_confidence == "fallback_filing_or_report_date"
                    or abs(value) < BACKLOG_ORDER_MIN_PLAUSIBLE_USD
                    or (scale_confidence == "low" and abs(value) < BACKLOG_ORDER_MIN_PLAUSIBLE_USD * 1000.0)
                ):
                    # Prose/table backlog captures have produced mass mis-scaled
                    # junk (1,443 sub-$1M facts). These aggregates need a
                    # resolved period date and a plausible magnitude; when the
                    # table scale was NOT confidently detected, additionally
                    # require the value to already be in full-dollar range
                    # (>= $1B) — a full-dollar table needs no scaling, while a
                    # mis-scaled millions capture lands far below that.
                    continue
                key = (concept_name, fact_period_end, unit, value)
                if key in seen:
                    continue
                seen.add(key)
                fact = ArchiveFact(
                    taxonomy="sec-text",
                    concept_name=concept_name,
                    unit=unit,
                    value=value,
                    period_start=period_start_for_text_fact(fact_period_end, period_type, duration_days),
                    period_end=fact_period_end,
                    frame=f"text_table:{document_name}:{table_index}:{row_index}:{value_index}",
                    decimals="",
                    payload_json=compact_json(
                        {
                            "document": document_name,
                            "label": cells[0],
                            "source": "sec_archive_text_table",
                            "scale": scale,
                            "scale_source": scale_source,
                            "scale_confidence": scale_confidence,
                            "currency_confidence": currency_confidence,
                            "period_confidence": period_confidence,
                            "period_evidence": period_evidence,
                            "column_index": value_index,
                            "column_count": len(values),
                            "statement_type": statement_type,
                            "projection_flag": projection_flag,
                            "historical_statement_flag": historical_statement_flag,
                        }
                    ),
                    source_detail=TEXT_TABLE_SOURCE_DETAIL,
                )
                if strict_registration_statements and concept_name in {
                    "DepreciationComponent",
                    "AmortizationComponent",
                    "DebtCurrentComponent",
                    "DebtNoncurrentComponent",
                }:
                    component_facts.setdefault((fact_period_end, unit), {})[concept_name] = fact
                else:
                    facts.append(fact)
        if strict_registration_statements:
            for (component_period_end, component_unit), components in component_facts.items():
                depreciation = components.get("DepreciationComponent")
                amortization = components.get("AmortizationComponent")
                if depreciation is not None and amortization is not None:
                    payload = json.loads(depreciation.payload_json)
                    payload.update(
                        {
                            "label": "Depreciation plus amortization",
                            "derived_from": ["DepreciationComponent", "AmortizationComponent"],
                        }
                    )
                    combined_value = depreciation.value + amortization.value
                    combined_key = (
                        "DepreciationAndAmortization",
                        component_period_end,
                        component_unit,
                        combined_value,
                    )
                    if combined_key not in seen:
                        seen.add(combined_key)
                        facts.append(
                            ArchiveFact(
                                taxonomy="sec-text",
                                concept_name="DepreciationAndAmortization",
                                unit=component_unit,
                                value=combined_value,
                                period_start=depreciation.period_start,
                                period_end=component_period_end,
                                frame=f"text_table:{document_name}:{table_index}:derived_d_and_a",
                                decimals="",
                                payload_json=compact_json(payload),
                                source_detail=TEXT_TABLE_SOURCE_DETAIL,
                            )
                        )

                debt_components = [
                    components[component]
                    for component in ("DebtCurrentComponent", "DebtNoncurrentComponent")
                    if component in components
                ]
                if debt_components:
                    debt_payload = json.loads(debt_components[0].payload_json)
                    debt_payload.update(
                        {
                            "label": "Total debt derived from balance-sheet debt components",
                            "derived_from": [fact.concept_name for fact in debt_components],
                        }
                    )
                    debt_value = sum(fact.value for fact in debt_components)
                    debt_key = ("DebtTotal", component_period_end, component_unit, debt_value)
                    if debt_key not in seen:
                        seen.add(debt_key)
                        facts.append(
                            ArchiveFact(
                                taxonomy="sec-text",
                                concept_name="DebtTotal",
                                unit=component_unit,
                                value=debt_value,
                                period_start="",
                                period_end=component_period_end,
                                frame=f"text_table:{document_name}:{table_index}:derived_total_debt",
                                decimals="",
                                payload_json=compact_json(debt_payload),
                                source_detail=TEXT_TABLE_SOURCE_DETAIL,
                            )
                        )
    if not strict_registration_statements:
        return prefer_explicit_total_order_facts(facts)

    # Registration statements can contain the issuer, acquired subsidiaries,
    # and repeated summaries under one accession. The canonical table's key is
    # one metric per period/accession, so choose deterministically here. Prefer
    # annual duration, consolidated totals over continuing-only subtotals, and
    # the earlier issuer summary before later acquired-company appendices.
    best_facts: dict[tuple[str, str, str], ArchiveFact] = {}

    def registration_fact_rank(fact: ArchiveFact) -> tuple[int, int, int, int]:
        start = parse_date(fact.period_start)
        end = parse_date(fact.period_end)
        duration_days = 0
        if start and end:
            duration_days = (
                datetime.strptime(end, "%Y-%m-%d").date() - datetime.strptime(start, "%Y-%m-%d").date()
            ).days
        payload = json.loads(fact.payload_json)
        label = normalize_table_label(str(payload.get("label") or ""))
        table_match = re.search(r":(\d+):", fact.frame)
        table_index = int(table_match.group(1)) if table_match else 1_000_000
        equity_scope_rank = int(fact.concept_name == "Equity" and not label.startswith("total equity"))
        return (
            -duration_days,
            equity_scope_rank,
            int("continuing operations" in label),
            table_index,
        )

    for fact in facts:
        key = (fact.concept_name, fact.period_end, fact.unit)
        current = best_facts.get(key)
        if current is None or registration_fact_rank(fact) < registration_fact_rank(current):
            best_facts[key] = fact
    return list(best_facts.values())


LEGACY_ASCII_TABLE_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9.])(?:\$\s*)?(\(?-?\d[\d,]*(?:\.\d+)?\)?)(?![A-Za-z0-9.])")


def parse_archive_legacy_ascii_table_facts(
    document_text: str,
    *,
    document_name: str,
    filing: dict[str, Any],
    company_currency: str,
) -> list[ArchiveFact]:
    """Parse pre-XBRL SEC ``<TABLE>`` blocks that contain fixed-width text.

    Older complete-submission files use SGML table wrappers but no HTML
    ``<TR>/<TD>`` cells. The normal HTML parser therefore sees no rows. This
    parser is deliberately limited to formal consolidated statement blocks,
    reviewed labels, explicit column dates, and explicit scale declarations.
    """
    facts: list[ArchiveFact] = []
    seen: set[tuple[str, str, str, float]] = set()
    fallback_period_end = parse_date(filing.get("report_date") or filing.get("filing_date"))
    form_type = str(filing.get("form_type") or "").strip().upper()
    for table_index, match in enumerate(
        re.finditer(r"(?is)<table\b[^>]*>(.*?)</table>", document_text),
        start=1,
    ):
        table_html = match.group(1)
        if re.search(r"(?is)<tr\b", table_html):
            continue
        plain = html_lib.unescape(table_html)
        plain = re.sub(r"(?is)<(?:caption|s|c)\b[^>]*>", "\n", plain)
        plain = re.sub(r"(?is)</?(?:text|page)\b[^>]*>", "\n", plain)
        plain = re.sub(r"(?is)<[^>]+>", " ", plain)
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in plain.replace("\r", "\n").split("\n")]
        lines = [line for line in lines if line]
        if not lines:
            continue
        table_text = "\n".join(lines)
        statement_type, projection_flag, historical_statement_flag = text_table_statement_provenance(
            table_text[:2000], table_text
        )
        if statement_type not in RECOGNIZED_STATEMENT_TYPES or projection_flag or not historical_statement_flag:
            continue
        first_data_index: int | None = None
        for line_index, line in enumerate(lines):
            numeric_matches = list(LEGACY_ASCII_TABLE_NUMBER_RE.finditer(line))
            if not numeric_matches:
                continue
            label = line[: numeric_matches[0].start()].strip(" $:-")
            if re.search(r"[A-Za-z]{3}", label) and not month_day_pairs(line) and not full_text_dates(line):
                first_data_index = line_index
                break
        if first_data_index is None:
            continue
        header_rows = [[line] for line in lines[:first_data_index]]
        period_count = text_table_period_count(header_rows)
        if period_count <= 0 or period_count > 8:
            continue
        scale, scale_source, scale_confidence = table_scale_info(" ".join(lines[: min(first_data_index + 1, 25)]))
        if scale_confidence != "high":
            continue
        unit, currency_confidence = text_table_unit(
            document_text,
            table_text[:2000],
            company_currency=company_currency,
        )
        has_broader_revenue_row = any(
            normalize_table_label(line[: matches[0].start()].strip(" $:-"))
            in {"revenue", "revenues", "total revenue", "total revenues"}
            for line in lines[first_data_index:]
            if (matches := list(LEGACY_ASCII_TABLE_NUMBER_RE.finditer(line)))
        )
        for row_index, line in enumerate(lines[first_data_index:], start=first_data_index):
            numeric_matches = list(LEGACY_ASCII_TABLE_NUMBER_RE.finditer(line))
            if len(numeric_matches) < period_count:
                continue
            selected = numeric_matches[-period_count:]
            label = line[: selected[0].start()].strip(" $:-")
            label_result = text_table_label_concept(label)
            if label_result is None:
                continue
            concept_name, period_type = label_result
            if (
                concept_name == "Revenue"
                and normalize_table_label(label).startswith("net revenue")
                and has_broader_revenue_row
            ):
                continue
            if statement_type == "balance_sheet" and period_type != "instant":
                continue
            if statement_type == "income_statement" and period_type != "duration":
                continue
            if statement_type == "cash_flow" and concept_name not in {
                "OperatingCashFlow",
                "Capex",
                "DepreciationAndAmortization",
            }:
                continue
            values = [parse_table_number(number_match.group(1)) for number_match in selected]
            if any(value is None for value in values):
                continue
            periods = infer_text_table_periods(
                header_rows,
                value_count=period_count,
                fallback_period_end=fallback_period_end,
                context_text=table_text[:2000],
                form_type=form_type,
            )
            for value_index, parsed_value in enumerate(values):
                if parsed_value is None:
                    continue
                period_end, duration_days, period_confidence, period_evidence = periods[
                    min(value_index, len(periods) - 1)
                ]
                if not period_end or (
                    period_type == "duration" and period_confidence == "fallback_filing_or_report_date"
                ):
                    continue
                value = parsed_value * scale
                key = (concept_name, period_end, unit, value)
                if key in seen:
                    continue
                seen.add(key)
                facts.append(
                    ArchiveFact(
                        taxonomy="sec-text",
                        concept_name=concept_name,
                        unit=unit,
                        value=value,
                        period_start=period_start_for_text_fact(
                            period_end,
                            period_type,
                            duration_days,
                        ),
                        period_end=period_end,
                        frame=(f"legacy_ascii:{document_name}:{table_index}:{row_index}:{value_index}"),
                        decimals="",
                        payload_json=compact_json(
                            {
                                "document": document_name,
                                "label": label,
                                "source": "sec_archive_legacy_ascii_table",
                                "scale": scale,
                                "scale_source": scale_source,
                                "scale_confidence": scale_confidence,
                                "currency_confidence": currency_confidence,
                                "period_confidence": period_confidence,
                                "period_evidence": period_evidence,
                                "statement_type": statement_type,
                                "projection_flag": projection_flag,
                                "historical_statement_flag": historical_statement_flag,
                            }
                        ),
                        source_detail=TEXT_TABLE_SOURCE_DETAIL,
                    )
                )
    return facts


def archive_document_candidates(
    index_payload: dict[str, Any],
    *,
    primary_document: str,
    max_documents: int,
    text_tables_only: bool = False,
    include_pdf: bool = False,
    targeted_report_documents: set[str] | None = None,
    machinery_targeted: bool = False,
    research_targeted: bool = False,
    event_filing: bool = False,
    event_exhibits_only: bool = False,
) -> list[str]:
    raw_items = (index_payload.get("directory") or {}).get("item") or []
    candidates: list[str] = []
    event_research_documents: set[str] = set()
    primary = str(primary_document or "").strip()
    text_table_suffixes = (".xhtml", ".htm", ".html")
    allowed_suffixes = (
        text_table_suffixes + (ARCHIVE_PDF_SUFFIXES if include_pdf else ())
        if text_tables_only
        else ARCHIVE_ALLOWED_DOCUMENT_SUFFIXES + (ARCHIVE_PDF_SUFFIXES if include_pdf else ())
    )
    if primary and primary.lower().endswith(allowed_suffixes):
        candidates.append(primary)
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        lower = name.lower()
        if not name or name in candidates:
            continue
        if not lower.endswith(allowed_suffixes):
            continue
        if any(lower.endswith(suffix) for suffix in ARCHIVE_EXCLUDED_SUFFIXES):
            continue
        item_metadata = " ".join(
            (
                str(item.get("type") or ""),
                str(item.get("description") or ""),
            )
        )
        is_event_exhibit = event_filing and is_event_research_document(
            name,
            metadata=item_metadata,
        )
        if is_event_exhibit:
            event_research_documents.add(name)
        if (machinery_targeted or research_targeted) and name != primary:
            is_targeted_report = name in (targeted_report_documents or set())
            is_instance_xml = lower.endswith(".xml")
            is_event_pdf = event_filing and lower.endswith(ARCHIVE_PDF_SUFFIXES)
            if not (is_targeted_report or is_instance_xml or is_event_exhibit or is_event_pdf):
                continue
        candidates.append(name)
    if event_filing and event_exhibits_only:
        event_documents = [name for name in candidates if name != primary and name in event_research_documents]
        candidates = event_documents or ([primary] if primary in candidates else [])
    candidates.sort(
        key=lambda name: (
            0 if name == primary else 1 if re.search(r"ex(?:hibit)?99|ex99", name, re.IGNORECASE) else 2,
            1 if name.lower().endswith(".txt") else 0,
            name.lower(),
        )
    )
    if max_documents > 0:
        return candidates[:max_documents]
    return candidates


def is_event_research_document(
    document_name: object,
    *,
    metadata: object = "",
) -> bool:
    searchable = " ".join(
        (
            str(document_name or "").strip(),
            str(metadata or "").strip(),
        )
    ).lower()
    return bool(
        re.search(
            r"(?:ex(?:hibit)?[-_ ]?99|earnings|presentation|release)",
            searchable,
        )
    )


def archive_raw_submission_document_name(accession_number: object) -> str:
    """Return EDGAR's canonical complete-submission text filename.

    Legacy filing indexes can list split document names that now return 404
    even though the complete submission remains available at
    ``{accession-number}.txt`` in the same accession directory.
    """
    accession = str(accession_number or "").strip()
    if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession):
        return ""
    return f"{accession}.txt"


def should_stop_archive_document_scan(
    *,
    model_family: str,
    form_type: str,
    mapped_estimate: int,
    special_metric_count: int,
    parse_all_documents: bool,
    scan_all_documents: bool = False,
) -> bool:
    """Keep scanning machinery event filings until an operating exhibit is found."""
    if parse_all_documents or scan_all_documents or mapped_estimate <= 0:
        return False
    if model_family == "machinery" and form_type.strip().upper() in {"8-K", "8-K/A"}:
        # Separate EX-99 exhibits often contain different metrics. Parse all
        # eligible event-filing documents and deduplicate after ticker staging.
        return False
    return True


def archive_label_linkbase_candidates(index_payload: dict[str, Any]) -> list[str]:
    raw_items = (index_payload.get("directory") or {}).get("item") or []
    candidates = {
        str(item.get("name") or "").strip()
        for item in raw_items
        if isinstance(item, dict) and str(item.get("name") or "").strip().lower().endswith("_lab.xml")
    }
    return sorted(name for name in candidates if name)


def archive_cache_file(cache_dir: Path, *, cik: str, accession: str, document_name: str) -> Path:
    safe_document = re.sub(r"[^A-Za-z0-9_.-]+", "_", document_name)
    return cache_dir / "sec_archive_xbrl" / f"CIK{cik}" / accession.replace("-", "") / safe_document


def upsert_archive_facts(
    conn: Any,
    *,
    ticker: str,
    cik: str,
    source_id: str,
    filing: dict[str, Any],
    document_name: str,
    facts: list[ArchiveFact],
    concept_map: dict[tuple[str, str], list[dict[str, Any]]],
    start_date: str,
) -> tuple[int, int]:
    now = utc_now()
    raw_count = 0
    mapped_count = 0
    accession = str(filing.get("accession_number") or "").strip()
    form_type = str(filing.get("form_type") or "").strip().upper()
    filing_date = parse_date(filing.get("filing_date"))
    accepted_at = str(filing.get("accepted_at") or "").strip()
    fiscal_year = as_int(filing.get("fiscal_year"))
    fiscal_period = str(filing.get("fiscal_period") or "").strip()
    for fact in facts:
        if start_date and filing_date and filing_date < start_date:
            continue
        # SC-2: normalize unit case to lowercase at write time so the archive
        # path ('SHARES'/'USD' from read_units) and the companyfacts path agree;
        # readers compare case-insensitively for legacy rows.
        unit_text = str(fact.unit or "").strip().lower()
        fact_key = make_fact_key(
            ticker,
            source_id,
            accession,
            fact.taxonomy,
            fact.concept_name,
            unit_text,
            fact.period_start,
            fact.period_end,
            fact.frame,
            document_name,
        )
        conn.execute(
            """
            INSERT INTO fact_sec_xbrl_fact_raw(
                fact_key, ticker, cik, source_id, accession_number, form_type,
                filing_date, accepted_at, fiscal_year, fiscal_period, period_start,
                period_end, frame, taxonomy, concept_name, unit, raw_value, decimals,
                source_detail, payload_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(fact_key) DO UPDATE SET
                filing_date = excluded.filing_date,
                accepted_at = excluded.accepted_at,
                fiscal_year = excluded.fiscal_year,
                fiscal_period = excluded.fiscal_period,
                raw_value = excluded.raw_value,
                decimals = excluded.decimals,
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            (
                fact_key,
                ticker,
                cik,
                source_id,
                accession,
                form_type,
                filing_date,
                accepted_at,
                fiscal_year,
                fiscal_period,
                fact.period_start,
                fact.period_end,
                fact.frame,
                fact.taxonomy,
                fact.concept_name,
                unit_text,
                fact.value,
                fact.decimals,
                fact.source_detail,
                fact.payload_json,
                now,
                now,
            ),
        )
        raw_row = conn.execute(
            "SELECT raw_fact_id FROM fact_sec_xbrl_fact_raw WHERE fact_key = ?", (fact_key,)
        ).fetchone()
        raw_fact_id = int(raw_row["raw_fact_id"]) if raw_row is not None else None
        raw_count += 1
        for mapping in concept_map.get((fact.taxonomy, fact.concept_name), []):
            mapped_value = apply_sign(fact.value, str(mapping["sign_policy"]))
            conn.execute(
                """
                INSERT INTO fact_sec_xbrl_fact(
                    raw_fact_id, ticker, cik, source_id, accession_number,
                    form_type, filing_date, accepted_at, fiscal_year, fiscal_period,
                    period_start, period_end, frame, taxonomy, concept_name,
                    canonical_metric, financial_statement, period_type, unit,
                    value, sign_policy, source_priority, source_detail,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker, source_id, accession_number, taxonomy, concept_name, canonical_metric, unit, period_start, period_end, frame)
                DO UPDATE SET
                    raw_fact_id = excluded.raw_fact_id,
                    filing_date = excluded.filing_date,
                    accepted_at = excluded.accepted_at,
                    fiscal_year = excluded.fiscal_year,
                    fiscal_period = excluded.fiscal_period,
                    value = excluded.value,
                    sign_policy = excluded.sign_policy,
                    source_priority = excluded.source_priority,
                    source_detail = excluded.source_detail,
                    updated_at = excluded.updated_at
                """,
                (
                    raw_fact_id,
                    ticker,
                    cik,
                    source_id,
                    accession,
                    form_type,
                    filing_date,
                    accepted_at,
                    fiscal_year,
                    fiscal_period,
                    fact.period_start,
                    fact.period_end,
                    fact.frame,
                    fact.taxonomy,
                    fact.concept_name,
                    str(mapping["canonical_metric"]),
                    str(mapping["financial_statement"]),
                    str(mapping["period_type"]),
                    unit_text,
                    mapped_value,
                    str(mapping["sign_policy"]),
                    int(mapping["priority"]),
                    f"{fact.source_detail}_mapped",
                    now,
                    now,
                ),
            )
            mapped_count += 1
    return raw_count, mapped_count


def purge_archive_xbrl_facts(conn: Any, *, ticker: str, source_id: str, model_family: str) -> None:
    conn.execute(
        """
        DELETE FROM fact_financial_statement_canonical
        WHERE ticker = ?
          AND source_id = ?
          AND model_family = ?
          AND EXISTS (
                SELECT 1
                FROM fact_sec_xbrl_fact f
                WHERE f.ticker = fact_financial_statement_canonical.ticker
                  AND f.source_id = fact_financial_statement_canonical.source_id
                  AND f.canonical_metric = fact_financial_statement_canonical.canonical_metric
                  AND f.period_end = fact_financial_statement_canonical.period_end
                  AND COALESCE(f.accession_number, '') = COALESCE(fact_financial_statement_canonical.accession_number, '')
                  AND COALESCE(f.unit, '') = COALESCE(fact_financial_statement_canonical.unit, '')
                  AND (
                        f.source_detail IN ('sec_archive_xbrl_mapped', 'sec_archive_text_table_mapped', 'sec_archive_footnote_xbrl_mapped', 'sec_archive_prose_metric_mapped')
                     OR f.raw_fact_id IN (
                            SELECT raw_fact_id
                            FROM fact_sec_xbrl_fact_raw r
                            WHERE r.ticker = ?
                              AND r.source_id = ?
                              AND r.source_detail IN ('sec_archive_xbrl', 'sec_archive_text_table', 'sec_archive_footnote_xbrl', 'sec_archive_prose_metric')
                        )
                  )
          )
        """,
        (ticker, source_id, model_family, ticker, source_id),
    )
    conn.execute(
        """
        DELETE FROM fact_sec_xbrl_fact
        WHERE ticker = ?
          AND source_id = ?
          AND (
                source_detail IN ('sec_archive_xbrl_mapped', 'sec_archive_text_table_mapped', 'sec_archive_footnote_xbrl_mapped', 'sec_archive_prose_metric_mapped')
             OR raw_fact_id IN (
                    SELECT raw_fact_id
                    FROM fact_sec_xbrl_fact_raw
                    WHERE ticker = ?
                      AND source_id = ?
                      AND source_detail IN ('sec_archive_xbrl', 'sec_archive_text_table', 'sec_archive_footnote_xbrl', 'sec_archive_prose_metric')
                )
          )
        """,
        (ticker, source_id, ticker, source_id),
    )
    conn.execute(
        """
        DELETE FROM fact_sec_xbrl_fact_raw
        WHERE ticker = ?
          AND source_id = ?
          AND source_detail IN ('sec_archive_xbrl', 'sec_archive_text_table', 'sec_archive_footnote_xbrl', 'sec_archive_prose_metric')
        """,
        (ticker, source_id),
    )


def purge_archive_text_table_facts(conn: Any, *, ticker: str, source_id: str, model_family: str) -> None:
    conn.execute(
        """
        DELETE FROM fact_financial_statement_canonical
        WHERE ticker = ?
          AND source_id = ?
          AND model_family = ?
          AND EXISTS (
                SELECT 1
                FROM fact_sec_xbrl_fact f
                WHERE f.ticker = fact_financial_statement_canonical.ticker
                  AND f.source_id = fact_financial_statement_canonical.source_id
                  AND f.canonical_metric = fact_financial_statement_canonical.canonical_metric
                  AND f.period_end = fact_financial_statement_canonical.period_end
                  AND COALESCE(f.accession_number, '') = COALESCE(fact_financial_statement_canonical.accession_number, '')
                  AND COALESCE(f.unit, '') = COALESCE(fact_financial_statement_canonical.unit, '')
                  AND f.source_detail IN ('sec_archive_text_table_mapped', 'sec_archive_footnote_xbrl_mapped', 'sec_archive_prose_metric_mapped')
          )
        """,
        (ticker, source_id, model_family),
    )
    conn.execute(
        """
        DELETE FROM fact_sec_xbrl_fact
        WHERE ticker = ?
          AND source_id = ?
          AND (
                source_detail IN ('sec_archive_text_table_mapped', 'sec_archive_footnote_xbrl_mapped', 'sec_archive_prose_metric_mapped')
             OR raw_fact_id IN (
                    SELECT raw_fact_id
                    FROM fact_sec_xbrl_fact_raw
                    WHERE ticker = ?
                      AND source_id = ?
                      AND source_detail IN ('sec_archive_text_table', 'sec_archive_footnote_xbrl', 'sec_archive_prose_metric')
                )
          )
        """,
        (ticker, source_id, ticker, source_id),
    )
    conn.execute(
        """
        DELETE FROM fact_sec_xbrl_fact_raw
        WHERE ticker = ?
          AND source_id = ?
          AND source_detail IN ('sec_archive_text_table', 'sec_archive_footnote_xbrl', 'sec_archive_prose_metric')
        """,
        (ticker, source_id),
    )


def should_attempt_archive(override: ReportingOverride | None) -> bool:
    if override is None:
        return False
    return override.reporting_profile in ARCHIVE_FALLBACK_PROFILES


def select_archive_filing_rows(
    filing_rows: list[Any],
    *,
    max_filings: int,
    supplemental_forms: set[str],
    max_supplemental_filings: int,
) -> list[Any]:
    """Cap event filings separately so they cannot displace periodic history."""
    if not supplemental_forms:
        return filing_rows[:max_filings] if max_filings > 0 else filing_rows
    core = [row for row in filing_rows if str(row["form_type"] or "").strip().upper() not in supplemental_forms]
    supplemental = [row for row in filing_rows if str(row["form_type"] or "").strip().upper() in supplemental_forms]
    if max_filings > 0:
        core = core[:max_filings]
    if max_supplemental_filings >= 0:
        supplemental = supplemental[:max_supplemental_filings]
    selected = [*core, *supplemental]
    return sorted(
        selected,
        key=lambda row: (
            str(row["filing_date"] or ""),
            str(row["accession_number"] or ""),
        ),
        reverse=True,
    )


def sync_archive_xbrl(
    conn: Any,
    *,
    ticker: str,
    cik: str,
    source_id: str,
    submissions_source_id: str,
    model_family: str,
    cache_dir: Path,
    force: bool,
    user_agent: str,
    timeout_sec: float,
    max_retries: int,
    sleep_sec: float,
    concept_map: dict[tuple[str, str], list[dict[str, Any]]],
    start_date: str,
    index_url_template: str,
    document_url_template: str,
    max_filings: int,
    supplemental_forms: set[str],
    max_supplemental_filings: int,
    max_documents: int,
    include_pdf_documents: bool = False,
    pdf_ocr_enabled: bool = False,
    max_pdf_pages: int = 250,
    max_pdf_bytes: int = 25_000_000,
    pdf_extraction_timeout_sec: float = 30.0,
    parse_all_documents: bool = False,
    text_tables_only: bool = False,
    strict_registration_statements: bool = False,
    company_currency: str = "",
    min_refetch_fact_fraction: float = 0.5,
    ingestion_run_id: int,
    scan_all_documents: bool = False,
    cache_only: bool = False,
    document_keywords: tuple[str, ...] = (),
    accession_filter: set[str] | None = None,
) -> tuple[int, int, int]:
    """Refresh archive-derived XBRL facts with stage-then-swap semantics (FN-2, XC-23).

    Phase 1 fetches and parses every filing document with no write transaction
    open. Phase 2 opens one short transaction that purges the prior archive
    facts and swaps in the staged ones — and refuses (raises, rolling back the
    purge) when document fetch failures leave an implausibly small fraction of
    the previously stored facts. A complete reparse may legitimately remove
    facts after a mapping or parser-policy correction.
    """
    filing_rows = conn.execute(
        """
        SELECT accession_number, form_type, filing_date, accepted_at, report_date, fiscal_year,
               fiscal_period, primary_document
        FROM fact_sec_filing
        WHERE ticker = ?
          AND source_id = ?
        ORDER BY filing_date DESC, accession_number DESC
        """,
        (ticker, submissions_source_id),
    ).fetchall()
    if not filing_rows:
        return 0, 0, 0
    if accession_filter is not None:
        filing_rows = [row for row in filing_rows if str(row["accession_number"] or "") in accession_filter]
        if not filing_rows:
            return 0, 0, 0
    filing_rows = select_archive_filing_rows(
        list(filing_rows),
        max_filings=max_filings,
        supplemental_forms=supplemental_forms,
        max_supplemental_filings=max_supplemental_filings,
    )

    # Phase 1: network fetch + parse, staging facts in memory.
    staged: list[tuple[dict[str, Any], str, list[ArchiveFact], list[DisclosureCandidate]]] = []
    raw_responses: list[tuple[str, int, str]] = []
    document_parse_warnings: list[str] = []
    fetch_failures = 0
    requests = 0
    for row in filing_rows:
        filing = dict(row)
        accession = str(filing.get("accession_number") or "")
        accession_nodash = accession.replace("-", "")
        if not accession_nodash:
            continue
        cik_int = str(int(cik))
        index_url = index_url_template.format(cik_int=cik_int, accession_nodash=accession_nodash)
        index_cache = archive_cache_file(cache_dir, cik=cik, accession=accession, document_name="index.json")
        requests += 1
        try:
            status, index_payload, index_text, index_fetch_mode = load_or_fetch_json(
                index_url,
                cache_file=index_cache,
                force=force,
                user_agent=user_agent,
                timeout_sec=timeout_sec,
                max_retries=max_retries,
                sleep_sec=sleep_sec,
            )
        except SecRequestError as exc:
            raw_responses.append((exc.url, exc.status_code, exc.body))
            fetch_failures += 1
            LOGGER.warning(
                "Unavailable SEC archive index ticker=%s accession=%s status=%s", ticker, accession, exc.status_code
            )
            continue
        if index_fetch_mode == "network":
            # FN-8: cache hits are not new observations; only record network fetches.
            raw_responses.append((index_url, status, index_text))
        concept_labels: dict[str, list[str]] = {}
        if model_family == "machinery":
            for label_document in archive_label_linkbase_candidates(index_payload):
                label_url = document_url_template.format(
                    cik_int=cik_int,
                    accession_nodash=accession_nodash,
                    document_name=label_document,
                )
                label_cache = archive_cache_file(
                    cache_dir,
                    cik=cik,
                    accession=accession,
                    document_name=label_document,
                )
                requests += 1
                try:
                    label_status, label_text, label_fetch_mode = load_or_fetch_text(
                        label_url,
                        cache_file=label_cache,
                        force=force,
                        user_agent=user_agent,
                        timeout_sec=timeout_sec,
                        max_retries=max_retries,
                        sleep_sec=sleep_sec,
                    )
                except SecRequestError as exc:
                    raw_responses.append((exc.url, exc.status_code, exc.body))
                    fetch_failures += 1
                    LOGGER.warning(
                        "Unavailable SEC archive label linkbase ticker=%s accession=%s document=%s status=%s",
                        ticker,
                        accession,
                        label_document,
                        exc.status_code,
                    )
                    continue
                for concept, labels in parse_xbrl_label_linkbase(label_text).items():
                    for label in labels:
                        if label not in concept_labels.setdefault(concept, []):
                            concept_labels[concept].append(label)
                if label_fetch_mode == "network":
                    raw_responses.append((label_url, label_status, label_text))
                    time.sleep(sleep_sec)
        form_type = str(filing.get("form_type") or "").strip().upper()
        event_filing = form_type in {"8-K", "8-K/A", "6-K", "6-K/A"}
        targeted_report_documents: set[str] = set()
        if (model_family == "machinery" or document_keywords) and not event_filing:
            summary_document = filing_summary_document_name(index_payload)
            if summary_document:
                summary_url = document_url_template.format(
                    cik_int=cik_int,
                    accession_nodash=accession_nodash,
                    document_name=summary_document,
                )
                summary_cache = archive_cache_file(
                    cache_dir,
                    cik=cik,
                    accession=accession,
                    document_name=summary_document,
                )
                requests += 1
                try:
                    summary_status, summary_text, summary_fetch_mode = load_or_fetch_text(
                        summary_url,
                        cache_file=summary_cache,
                        force=force,
                        user_agent=user_agent,
                        timeout_sec=timeout_sec,
                        max_retries=max_retries,
                        sleep_sec=sleep_sec,
                    )
                except SecRequestError as exc:
                    raw_responses.append((exc.url, exc.status_code, exc.body))
                    fetch_failures += 1
                    LOGGER.warning(
                        "Unavailable SEC FilingSummary ticker=%s accession=%s status=%s",
                        ticker,
                        accession,
                        exc.status_code,
                    )
                else:
                    targeted_report_documents = filing_summary_report_documents(
                        summary_text,
                        keywords=(
                            document_keywords
                            if document_keywords
                            else (
                                "backlog",
                                "booking",
                                "contract",
                                "customer",
                                "inventory",
                                "order",
                                "performance obligation",
                                "remaining performance",
                                "revenue",
                                "segment",
                            )
                        ),
                    )
                    if summary_fetch_mode == "network":
                        raw_responses.append((summary_url, summary_status, summary_text))
                        time.sleep(sleep_sec)
        document_candidates = archive_document_candidates(
            index_payload,
            primary_document=str(filing.get("primary_document") or ""),
            max_documents=max_documents,
            text_tables_only=text_tables_only,
            include_pdf=(include_pdf_documents and event_filing),
            targeted_report_documents=targeted_report_documents,
            machinery_targeted=model_family == "machinery",
            research_targeted=bool(document_keywords),
            event_filing=event_filing,
            event_exhibits_only=(model_family == "defense" and bool(document_keywords) and event_filing),
        )
        # SEC's complete-submission text file is the authoritative fallback
        # for pre-XBRL filings whose legacy split-document links return 404.
        # Keep it last so a working primary/instance document wins, and do not
        # add it to FPI "parse every document" runs where it would duplicate
        # hundreds of already-parsed exhibits.
        raw_submission_name = archive_raw_submission_document_name(accession)
        defense_targeted_event = model_family == "defense" and bool(document_keywords) and event_filing
        if (
            raw_submission_name
            and not parse_all_documents
            and not defense_targeted_event
            and raw_submission_name not in document_candidates
        ):
            document_candidates.append(raw_submission_name)
        for document_name in document_candidates:
            document_url = document_url_template.format(
                cik_int=cik_int, accession_nodash=accession_nodash, document_name=document_name
            )
            document_cache = archive_cache_file(cache_dir, cik=cik, accession=accession, document_name=document_name)
            requests += 1
            try:
                if document_name.lower().endswith(ARCHIVE_PDF_SUFFIXES):
                    document_status, document_payload, document_fetch_mode = load_or_fetch_bytes(
                        document_url,
                        cache_file=document_cache,
                        force=force,
                        user_agent=user_agent,
                        timeout_sec=timeout_sec,
                        max_retries=max_retries,
                        sleep_sec=sleep_sec,
                    )
                    extracted = extract_document_text(
                        document_payload,
                        document_name=document_name,
                        content_type="application/pdf",
                        enable_pdf_ocr=pdf_ocr_enabled,
                        max_pdf_pages=max_pdf_pages,
                        max_pdf_bytes=max_pdf_bytes,
                        pdf_extraction_timeout_sec=pdf_extraction_timeout_sec,
                    )
                    document_text = extracted.text
                    if extracted.warning:
                        document_parse_warnings.append(f"{accession}:{document_name}:{extracted.warning}")
                    if document_fetch_mode == "network":
                        raw_responses.append(
                            (
                                document_url,
                                document_status,
                                json.dumps(
                                    {
                                        "content_sha256": hashlib.sha256(document_payload).hexdigest(),
                                        "document_name": document_name,
                                        "extraction_method": extracted.extraction_method,
                                        "page_count": extracted.page_count,
                                        "ocr_used": extracted.ocr_used,
                                    },
                                    sort_keys=True,
                                ),
                            )
                        )
                    if not document_text.strip():
                        continue
                else:
                    _, document_text, document_fetch_mode = load_or_fetch_text(
                        document_url,
                        cache_file=document_cache,
                        force=force,
                        user_agent=user_agent,
                        timeout_sec=timeout_sec,
                        max_retries=max_retries,
                        sleep_sec=sleep_sec,
                    )
            except SecRequestError as exc:
                raw_responses.append((exc.url, exc.status_code, exc.body))
                fetch_failures += 1
                LOGGER.warning(
                    "Unavailable SEC archive document ticker=%s accession=%s document=%s status=%s",
                    ticker,
                    accession,
                    document_name,
                    exc.status_code,
                )
                continue
            facts = parse_archive_text_table_facts(
                document_text,
                document_name=document_name,
                filing=filing,
                company_currency=company_currency,
                special_metrics_only=text_tables_only,
                strict_registration_statements=strict_registration_statements,
            )
            if document_name.lower().endswith(".txt"):
                facts = [
                    *facts,
                    *parse_archive_legacy_ascii_table_facts(
                        document_text,
                        document_name=document_name,
                        filing=filing,
                        company_currency=company_currency,
                    ),
                ]
            prose_candidates: list[DisclosureCandidate] = []
            if model_family == "machinery":
                prose_candidates = extract_machinery_prose_candidates(
                    document_text,
                    filing=filing,
                    company_currency=company_currency,
                )
                prose_candidates = resolve_machinery_disclosure_candidates(
                    prose_candidates,
                    ticker=ticker,
                    filing=filing,
                )
                facts = [
                    *facts,
                    *prose_candidate_facts(
                        prose_candidates,
                        document_name=document_name,
                    ),
                    *parse_machinery_footnote_facts(
                        document_text,
                        document_name=document_name,
                        filing=filing,
                        concept_labels=concept_labels,
                        ticker=ticker,
                    ),
                ]
            if not text_tables_only:
                facts = [
                    *parse_archive_facts(document_text, document_name=document_name, concept_map=concept_map),
                    *facts,
                ]
            if model_family == "machinery":
                facts = derive_cross_source_rpo_current_facts(
                    facts,
                    document_text=document_text,
                    document_name=document_name,
                    filing=filing,
                    ticker=ticker,
                )
            staged.append((filing, document_name, facts, prose_candidates))
            mapped_estimate = sum(len(concept_map.get((fact.taxonomy, fact.concept_name), [])) for fact in facts)
            special_metric_count = sum(fact.concept_name in BACKLOG_ORDER_TEXT_CONCEPTS for fact in facts)
            if should_stop_archive_document_scan(
                model_family=model_family,
                form_type=str(filing.get("form_type") or ""),
                mapped_estimate=mapped_estimate,
                special_metric_count=special_metric_count,
                parse_all_documents=parse_all_documents,
                scan_all_documents=scan_all_documents,
            ):
                break
            if document_fetch_mode == "network":
                time.sleep(sleep_sec)
        if index_fetch_mode == "network":
            time.sleep(sleep_sec)

    staged_fact_count = sum(len(facts) for _, _, facts, _ in staged)
    low_currency_documents = sorted(
        {
            document_name
            for _, document_name, facts, _ in staged
            for fact in facts
            if fact.source_detail == TEXT_TABLE_SOURCE_DETAIL
            and json.loads(fact.payload_json).get("currency_confidence") == "low"
        }
    )

    # Phase 2: one short write transaction — provenance, purge guard, swap.
    if cache_only:
        # Hydration supplies the dedicated parser's shadow cache. It must not
        # replace canonical/archive financial facts as a side effect.
        return staged_fact_count, 0, requests

    raw_total = 0
    mapped_total = 0
    with conn:
        for endpoint, status_code, payload_text in raw_responses:
            record_raw_response(
                conn,
                source_id=source_id,
                endpoint=endpoint,
                status=status_code,
                payload_text=payload_text,
                asof_date=datetime.now(timezone.utc).date().isoformat(),
                ingestion_run_id=ingestion_run_id,
            )
        source_filter = (
            "source_detail IN ('sec_archive_text_table', 'sec_archive_footnote_xbrl', 'sec_archive_prose_metric')"
            if text_tables_only
            else (
                "source_detail IN ('sec_archive_xbrl', 'sec_archive_text_table', 'sec_archive_footnote_xbrl', 'sec_archive_prose_metric')"
            )
        )
        prior_row = conn.execute(
            f"""
            SELECT COUNT(*)
            FROM fact_sec_xbrl_fact_raw
            WHERE ticker = ?
              AND source_id = ?
              AND {source_filter}
            """,
            (ticker, source_id),
        ).fetchone()
        prior_fact_count = int(prior_row[0] or 0)
        if (
            prior_fact_count > 0
            and fetch_failures > 0
            and staged_fact_count < prior_fact_count * min_refetch_fact_fraction
        ):
            raise RuntimeError(
                f"SEC archive refetch for ticker={ticker} staged only {staged_fact_count} facts vs "
                f"{prior_fact_count} previously stored (fetch_failures={fetch_failures}, "
                f"min_refetch_fact_fraction={min_refetch_fact_fraction}); refusing to purge existing archive facts."
            )
        if text_tables_only:
            purge_archive_text_table_facts(conn, ticker=ticker, source_id=source_id, model_family=model_family)
        else:
            purge_archive_xbrl_facts(conn, ticker=ticker, source_id=source_id, model_family=model_family)
        conn.execute(
            """
            DELETE FROM fact_sec_metric_disclosure_candidate
            WHERE ticker = ? AND source_id = ? AND model_family = ?
            """,
            (ticker, source_id, model_family),
        )
        for filing, document_name, facts, prose_candidates in staged:
            upsert_disclosure_candidates(
                conn,
                ticker=ticker,
                cik=cik,
                source_id=source_id,
                model_family=model_family,
                filing=filing,
                document_name=document_name,
                candidates=prose_candidates,
                now=utc_now(),
            )
            raw_count, mapped_count = upsert_archive_facts(
                conn,
                ticker=ticker,
                cik=cik,
                source_id=source_id,
                filing=filing,
                document_name=document_name,
                facts=facts,
                concept_map=concept_map,
                start_date=start_date,
            )
            raw_total += raw_count
            mapped_total += mapped_count
        if low_currency_documents:
            add_issue(
                conn,
                severity="warning",
                ticker=ticker,
                model_family=model_family,
                source_id=source_id,
                issue_type="text_table_currency_unconfirmed",
                detail=(
                    "Text-table currency could not be positively identified for a non-US filer; "
                    f"fell back to dim_company currency={str(company_currency or '').strip().upper() or 'UNKNOWN'} "
                    f"documents={','.join(low_currency_documents[:5])}"
                ),
            )
        if document_parse_warnings:
            add_issue(
                conn,
                severity="warning",
                ticker=ticker,
                model_family=model_family,
                source_id=source_id,
                issue_type="sec_archive_pdf_extraction_warning",
                detail=";".join(document_parse_warnings[:10]),
            )
        if model_family == "machinery":
            reconciliation = reconcile_machinery_disclosure_facts(
                conn,
                ticker=ticker,
                source_id=source_id,
                model_family=model_family,
                now=utc_now(),
            )
            raw_total -= reconciliation["raw_facts_deleted"]
            mapped_total -= reconciliation["mapped_facts_deleted"]
    return raw_total, mapped_total, requests


def upsert_companyfacts(
    conn: Any,
    *,
    ticker: str,
    cik: str,
    source_id: str,
    payload: dict[str, Any],
    concept_map: dict[tuple[str, str], list[dict[str, Any]]],
    start_date: str,
) -> tuple[int, int]:
    facts = payload.get("facts") or {}
    if not isinstance(facts, dict):
        return 0, 0
    now = utc_now()
    raw_count = 0
    mapped_count = 0
    for taxonomy, concepts in facts.items():
        if not isinstance(concepts, dict):
            continue
        taxonomy_text = str(taxonomy)
        for concept_name, concept_payload in concepts.items():
            if not isinstance(concept_payload, dict):
                continue
            units = concept_payload.get("units") or {}
            if not isinstance(units, dict):
                continue
            mappings = concept_map.get((taxonomy_text, str(concept_name)), [])
            for unit, fact_rows in units.items():
                if not isinstance(fact_rows, list):
                    continue
                # SC-2: normalize unit case to lowercase at write time so both
                # XBRL fact writers agree; readers compare case-insensitively
                # for legacy rows.
                unit_text = str(unit).strip().lower()
                for fact in fact_rows:
                    if not isinstance(fact, dict):
                        continue
                    period_end = parse_date(fact.get("end"))
                    filing_date = parse_date(fact.get("filed"))
                    if not period_end or (start_date and filing_date and filing_date < start_date):
                        continue
                    value = as_float(fact.get("val"))
                    accession = str(fact.get("accn") or "").strip()
                    form_type = str(fact.get("form") or "").strip().upper()
                    fiscal_year_raw = fact.get("fy")
                    fiscal_year = as_int(fiscal_year_raw)
                    fiscal_period = str(fact.get("fp") or "").strip()
                    period_start = parse_date(fact.get("start"))
                    frame = str(fact.get("frame") or "").strip()
                    fact_key = make_fact_key(
                        ticker,
                        source_id,
                        accession,
                        taxonomy_text,
                        concept_name,
                        unit_text,
                        period_start,
                        period_end,
                        frame,
                    )
                    conn.execute(
                        """
                        INSERT INTO fact_sec_xbrl_fact_raw(
                            fact_key, ticker, cik, source_id, accession_number, form_type,
                            filing_date, fiscal_year, fiscal_period, period_start, period_end,
                            frame, taxonomy, concept_name, unit, raw_value, decimals,
                            source_detail, payload_json, created_at, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'sec_companyfacts', ?, ?, ?)
                        ON CONFLICT(fact_key) DO UPDATE SET
                            filing_date = excluded.filing_date,
                            fiscal_year = excluded.fiscal_year,
                            fiscal_period = excluded.fiscal_period,
                            raw_value = excluded.raw_value,
                            decimals = excluded.decimals,
                            payload_json = excluded.payload_json,
                            updated_at = excluded.updated_at
                        """,
                        (
                            fact_key,
                            ticker,
                            cik,
                            source_id,
                            accession,
                            form_type,
                            filing_date,
                            fiscal_year,
                            fiscal_period,
                            period_start,
                            period_end,
                            frame,
                            taxonomy_text,
                            str(concept_name),
                            unit_text,
                            value,
                            str(fact.get("decimals") or ""),
                            compact_json(fact),
                            now,
                            now,
                        ),
                    )
                    raw_row = conn.execute(
                        "SELECT raw_fact_id FROM fact_sec_xbrl_fact_raw WHERE fact_key = ?", (fact_key,)
                    ).fetchone()
                    raw_fact_id = int(raw_row["raw_fact_id"]) if raw_row is not None else None
                    raw_count += 1
                    for mapping in mappings:
                        mapped_value = apply_sign(value, str(mapping["sign_policy"]))
                        conn.execute(
                            """
                            INSERT INTO fact_sec_xbrl_fact(
                                raw_fact_id, ticker, cik, source_id, accession_number,
                                form_type, filing_date, fiscal_year, fiscal_period,
                                period_start, period_end, frame, taxonomy, concept_name,
                                canonical_metric, financial_statement, period_type, unit,
                                value, sign_policy, source_priority, source_detail,
                                created_at, updated_at
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'sec_companyfacts_mapped', ?, ?)
                            ON CONFLICT(ticker, source_id, accession_number, taxonomy, concept_name, canonical_metric, unit, period_start, period_end, frame)
                            DO UPDATE SET
                                raw_fact_id = excluded.raw_fact_id,
                                filing_date = excluded.filing_date,
                                fiscal_year = excluded.fiscal_year,
                                fiscal_period = excluded.fiscal_period,
                                value = excluded.value,
                                sign_policy = excluded.sign_policy,
                                source_priority = excluded.source_priority,
                                updated_at = excluded.updated_at
                            """,
                            (
                                raw_fact_id,
                                ticker,
                                cik,
                                source_id,
                                accession,
                                form_type,
                                filing_date,
                                fiscal_year,
                                fiscal_period,
                                period_start,
                                period_end,
                                frame,
                                taxonomy_text,
                                str(concept_name),
                                str(mapping["canonical_metric"]),
                                str(mapping["financial_statement"]),
                                str(mapping["period_type"]),
                                unit_text,
                                mapped_value,
                                str(mapping["sign_policy"]),
                                int(mapping["priority"]),
                                now,
                                now,
                            ),
                        )
                        mapped_count += 1
    return raw_count, mapped_count


def classify_reporting_profile(
    conn: Any,
    *,
    ticker: str,
    cik: str,
    country: str,
    model_family: str,
    source_id: str,
    asof: str,
    override: ReportingOverride | None = None,
) -> dict[str, Any]:
    profile_asof = parse_date(asof)
    if not profile_asof:
        raise ValueError(f"Invalid reporting-profile asof={asof!r}; expected YYYY-MM-DD")
    latest = conn.execute(
        f"""
        SELECT accession_number, filing_date, form_type
        FROM fact_sec_filing
        WHERE ticker = ?
          AND (? = '' OR cik = ?)
          AND ({PROFILE_ACCEPTED_DATE_SQL}) <= ?
        ORDER BY ({PROFILE_ACCEPTED_DATE_SQL}) DESC, filing_date DESC, accession_number DESC
        LIMIT 1
        """,
        (ticker, cik, cik, profile_asof),
    ).fetchone()
    tax_rows = conn.execute(
        f"""
        SELECT taxonomy, COUNT(*) AS n, MAX(COALESCE(period_end, '')) AS latest_period_end
        FROM fact_sec_xbrl_fact
        WHERE ticker = ?
          AND (? = '' OR cik = ?)
          AND ({PROFILE_ACCEPTED_DATE_SQL}) <= ?
        GROUP BY taxonomy
        """,
        (ticker, cik, cik, profile_asof),
    ).fetchall()
    taxonomy_metric_rows = conn.execute(
        f"""
        SELECT taxonomy, canonical_metric
        FROM fact_sec_xbrl_fact
        WHERE ticker = ?
          AND (? = '' OR cik = ?)
          AND ({PROFILE_ACCEPTED_DATE_SQL}) <= ?
        GROUP BY taxonomy, canonical_metric
        """,
        (ticker, cik, cik, profile_asof),
    ).fetchall()
    metrics_by_taxonomy: dict[str, set[str]] = {}
    for row in taxonomy_metric_rows:
        metrics_by_taxonomy.setdefault(str(row["taxonomy"]), set()).add(str(row["canonical_metric"]))
    metrics = {
        str(row["canonical_metric"])
        for row in conn.execute(
            f"""
            SELECT DISTINCT canonical_metric
            FROM fact_sec_xbrl_fact
            WHERE ticker = ?
              AND (? = '' OR cik = ?)
              AND ({PROFILE_ACCEPTED_DATE_SQL}) <= ?
            """,
            (ticker, cik, cik, profile_asof),
        ).fetchall()
    }
    taxonomies = {str(row["taxonomy"]): int(row["n"] or 0) for row in tax_rows}
    taxonomy_latest_period = {str(row["taxonomy"]): str(row["latest_period_end"] or "") for row in tax_rows}
    core_metrics = {"revenue", "assets"}

    def primary_xbrl_taxonomy(*, require_core: bool) -> str:
        candidates: list[str] = []
        for taxonomy in ("us-gaap", "ifrs-full"):
            taxonomy_metrics = metrics_by_taxonomy.get(taxonomy, set())
            has_taxonomy_core = core_metrics <= taxonomy_metrics
            has_taxonomy_partial = (
                "assets" in taxonomy_metrics
                and bool(
                    taxonomy_metrics.intersection(
                        {
                            "cash_and_equivalents",
                            "liabilities",
                            "equity",
                        }
                    )
                )
            ) or bool(
                taxonomy_metrics.intersection(
                    {
                        "operating_income",
                        "net_income",
                        "operating_cash_flow",
                        "capex",
                        "research_and_development",
                    }
                )
            )
            if has_taxonomy_core if require_core else has_taxonomy_partial:
                candidates.append(taxonomy)
        if not candidates:
            return ""
        # Mixed-taxonomy issuers can retain a handful of stale facts from an
        # earlier reporting basis. Prefer the taxonomy with the most recent
        # mapped period, then the largest mapped-fact population. The final
        # name tie-break preserves the historic us-gaap precedence.
        return max(
            candidates,
            key=lambda taxonomy: (
                taxonomy_latest_period.get(taxonomy, ""),
                taxonomies.get(taxonomy, 0),
                taxonomy,
            ),
        )

    primary_core_taxonomy = primary_xbrl_taxonomy(require_core=True)
    primary_partial_taxonomy = primary_xbrl_taxonomy(require_core=False)
    has_core = {"revenue", "assets"} <= metrics
    has_core_sec_text = {"revenue", "assets"} <= metrics_by_taxonomy.get("sec-text", set())
    has_balance_sheet = "assets" in metrics and bool(
        metrics.intersection({"cash_and_equivalents", "liabilities", "equity"})
    )
    has_operating_or_income = bool(
        metrics.intersection(
            {"operating_income", "net_income", "operating_cash_flow", "capex", "research_and_development"}
        )
    )
    has_partial_xbrl = has_balance_sheet or has_operating_or_income
    latest_form = str(latest["form_type"]) if latest is not None else ""
    latest_filing = str(latest["filing_date"]) if latest is not None else ""
    latest_accession = str(latest["accession_number"]) if latest is not None else ""
    country_text = str(country or "").strip()

    raw_archive_override = override is not None and override.reporting_profile == "SEC_RAW_ARCHIVE_REQUIRED"
    archive_attempt_override = should_attempt_archive(override)
    fallback_only_override = archive_attempt_override
    if override is not None and override.reporting_profile and not fallback_only_override:
        profile = override.reporting_profile
        standard = override.reporting_standard or "override"
        primary_taxonomy = ",".join(sorted(taxonomies))
        fallback = override.fallback_status
        confidence = override.financial_confidence
        usable_xbrl = override.usable_xbrl_flag
        reason = override.review_reason or override.handling_type
    elif override is not None and override.reporting_profile in RECENT_STUB_PROFILES and (has_core or has_partial_xbrl):
        profile = override.reporting_profile
        if taxonomies.get("us-gaap", 0) > 0:
            standard = "US_GAAP_STUB"
            primary_taxonomy = "us-gaap"
        elif taxonomies.get("ifrs-full", 0) > 0:
            standard = "IFRS_STUB"
            primary_taxonomy = "ifrs-full"
        elif taxonomies.get("sec-text", 0) > 0:
            standard = "sec_archive_text_table_stub"
            primary_taxonomy = "sec-text"
        else:
            standard = override.reporting_standard or "recent_public_stub"
            primary_taxonomy = ",".join(sorted(taxonomies))
        fallback = "recent_public_stub_period_limited"
        confidence = max(override.financial_confidence, 0.55 if has_core else 0.35)
        usable_xbrl = 1
        reason = override.review_reason or "recent_public_stub_limited_annual_history"
    elif override is not None and override.reporting_profile in FPI_HYBRID_PROFILES and (has_core or has_partial_xbrl):
        profile = override.reporting_profile
        if taxonomies.get("ifrs-full", 0) > 0:
            standard = "IFRS_FPI_HYBRID"
            primary_taxonomy = "ifrs-full"
        elif taxonomies.get("sec-text", 0) > 0:
            standard = "sec_archive_text_table_fpi_hybrid"
            primary_taxonomy = "sec-text"
        elif taxonomies.get("us-gaap", 0) > 0:
            standard = "US_GAAP_FPI_HYBRID"
            primary_taxonomy = "us-gaap"
        else:
            standard = override.reporting_standard or "fpi_hybrid"
            primary_taxonomy = ",".join(sorted(taxonomies))
        if profile == "FPI_HYBRID_LOADED":
            fallback = "fpi_hybrid_loaded"
            reason = ""
        else:
            fallback = "fpi_hybrid_stub_period_limited"
            reason = override.review_reason or "fpi_hybrid_stub_loaded_not_rank_ready"
        confidence = max(override.financial_confidence, 0.55 if has_core else 0.35)
        usable_xbrl = 1
    elif override is not None and override.handling_type in FULL_STATEMENT_ARCHIVE_HANDLING_TYPES and has_core_sec_text:
        profile = "SEC_ARCHIVE_TEXT_TABLE"
        standard = "sec_archive_text_table"
        primary_taxonomy = "sec-text"
        fallback = "text_table_extracted"
        confidence = 0.55
        usable_xbrl = 1
        reason = ""
    elif primary_core_taxonomy == "us-gaap":
        profile = "SEC_XBRL_US_GAAP"
        standard = "US_GAAP"
        primary_taxonomy = "us-gaap"
        fallback = "none"
        confidence = 0.9
        usable_xbrl = 1
        reason = ""
    elif primary_core_taxonomy == "ifrs-full":
        profile = "SEC_XBRL_IFRS"
        standard = "IFRS"
        primary_taxonomy = "ifrs-full"
        fallback = "none"
        confidence = 0.75
        usable_xbrl = 1
        reason = ""
    elif (
        has_core
        and taxonomies.get("sec-text", 0) > 0
        and override is not None
        and override.reporting_profile == "RECENT_IPO_DEVELOPMENT_STAGE"
    ):
        profile = "RECENT_IPO_DEVELOPMENT_STAGE"
        standard = "sec_archive_text_table"
        primary_taxonomy = "sec-text"
        fallback = "text_table_extracted_lifecycle_limited"
        confidence = max(override.financial_confidence, 0.45)
        usable_xbrl = 1
        reason = override.review_reason or "recent_ipo_limited_public_filing_history"
    elif has_core and taxonomies.get("sec-text", 0) > 0:
        profile = "SEC_ARCHIVE_TEXT_TABLE"
        standard = "sec_archive_text_table"
        primary_taxonomy = "sec-text"
        fallback = "text_table_extracted"
        confidence = 0.55
        usable_xbrl = 1
        reason = ""
    elif primary_partial_taxonomy == "us-gaap":
        profile = "SEC_XBRL_US_GAAP_PARTIAL"
        standard = "US_GAAP_PARTIAL"
        primary_taxonomy = "us-gaap"
        fallback = "component_limited"
        confidence = 0.55
        usable_xbrl = 1
        reason = "partial_xbrl_missing_core_revenue_or_assets"
    elif primary_partial_taxonomy == "ifrs-full":
        profile = "SEC_XBRL_IFRS_PARTIAL"
        standard = "IFRS_PARTIAL"
        primary_taxonomy = "ifrs-full"
        fallback = "component_limited"
        confidence = 0.45
        usable_xbrl = 1
        reason = "partial_ifrs_xbrl_missing_core_revenue_or_assets"
    elif (
        has_partial_xbrl
        and taxonomies.get("sec-text", 0) > 0
        and override is not None
        and override.reporting_profile == "RECENT_IPO_DEVELOPMENT_STAGE"
    ):
        profile = "RECENT_IPO_DEVELOPMENT_STAGE"
        standard = "sec_archive_text_table_partial"
        primary_taxonomy = "sec-text"
        fallback = "text_table_partial_lifecycle_limited"
        confidence = max(override.financial_confidence, 0.35)
        usable_xbrl = 1
        reason = override.review_reason or "recent_ipo_limited_public_filing_history"
    elif has_partial_xbrl and taxonomies.get("sec-text", 0) > 0:
        profile = "SEC_ARCHIVE_TEXT_TABLE_PARTIAL"
        standard = "sec_archive_text_table_partial"
        primary_taxonomy = "sec-text"
        fallback = "text_table_partial"
        confidence = 0.35
        usable_xbrl = 1
        reason = "text_table_partial_missing_core_revenue_or_assets"
    elif latest_form in {"20-F", "40-F", "6-K"}:
        profile = "SEC_20F_METADATA_ONLY"
        standard = "foreign_private_issuer_metadata"
        primary_taxonomy = ",".join(sorted(taxonomies))
        fallback = "neutral_low_confidence"
        confidence = 0.35
        usable_xbrl = 0
        reason = f"foreign_issuer_without_mapped_core_xbrl form={latest_form}"
    elif country_text and country_text.upper() not in {"UNITED STATES", "USA", "US"}:
        profile = "FOREIGN_NEUTRAL_LOW_CONFIDENCE"
        standard = "foreign_no_sec_xbrl"
        primary_taxonomy = ",".join(sorted(taxonomies))
        fallback = "neutral_low_confidence"
        confidence = 0.25
        usable_xbrl = 0
        reason = "foreign_issuer_no_usable_sec_xbrl"
    elif raw_archive_override or archive_attempt_override:
        profile = (
            override.reporting_profile
            if override is not None and override.reporting_profile
            else "SEC_RAW_ARCHIVE_REQUIRED"
        )
        standard = override.reporting_standard if override is not None else "legacy_sec_archive"
        primary_taxonomy = ",".join(sorted(taxonomies))
        fallback = override.fallback_status if override is not None else "raw_archive_required"
        confidence = override.financial_confidence if override is not None else 0.2
        usable_xbrl = 0
        reason = override.review_reason if override is not None else "legacy_or_delisted_sec_archive_required"
    elif latest is None:
        profile = "NO_FINANCIALS_REVIEW"
        standard = "unavailable"
        primary_taxonomy = ""
        fallback = "review"
        confidence = 0.0
        usable_xbrl = 0
        reason = "no_sec_filings_loaded"
    else:
        profile = "NO_FINANCIALS_REVIEW"
        standard = "sec_metadata_no_mapped_core_xbrl"
        primary_taxonomy = ",".join(sorted(taxonomies))
        fallback = "review"
        confidence = 0.2
        usable_xbrl = 0
        reason = "sec_filing_loaded_without_mapped_core_xbrl"

    now = utc_now()
    conn.execute(
        """
        INSERT INTO dim_issuer_reporting_profile(
            ticker, model_family, cik, country, reporting_profile, reporting_standard,
            primary_taxonomy, latest_filing_date, latest_form_type, latest_accession_number,
            fallback_status, financial_confidence, usable_xbrl_flag, source_id,
            review_reason, profile_asof_date, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker, model_family) DO UPDATE SET
            cik = excluded.cik,
            country = excluded.country,
            reporting_profile = excluded.reporting_profile,
            reporting_standard = excluded.reporting_standard,
            primary_taxonomy = excluded.primary_taxonomy,
            latest_filing_date = excluded.latest_filing_date,
            latest_form_type = excluded.latest_form_type,
            latest_accession_number = excluded.latest_accession_number,
            fallback_status = excluded.fallback_status,
            financial_confidence = excluded.financial_confidence,
            usable_xbrl_flag = excluded.usable_xbrl_flag,
            source_id = excluded.source_id,
            review_reason = excluded.review_reason,
            profile_asof_date = excluded.profile_asof_date,
            updated_at = excluded.updated_at
        WHERE COALESCE(dim_issuer_reporting_profile.profile_asof_date, '') <= excluded.profile_asof_date
        """,
        (
            ticker,
            model_family,
            cik,
            country_text,
            profile,
            standard,
            primary_taxonomy,
            latest_filing,
            latest_form,
            latest_accession,
            fallback,
            confidence,
            usable_xbrl,
            source_id,
            reason,
            profile_asof,
            now,
            now,
        ),
    )
    conn.execute(
        """
        INSERT INTO dim_issuer_reporting_profile_history(
            ticker, model_family, profile_asof_date, cik, country, reporting_profile,
            reporting_standard, primary_taxonomy, latest_filing_date, latest_form_type,
            latest_accession_number, fallback_status, financial_confidence,
            usable_xbrl_flag, source_id, review_reason, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker, model_family, profile_asof_date) DO UPDATE SET
            cik = excluded.cik,
            country = excluded.country,
            reporting_profile = excluded.reporting_profile,
            reporting_standard = excluded.reporting_standard,
            primary_taxonomy = excluded.primary_taxonomy,
            latest_filing_date = excluded.latest_filing_date,
            latest_form_type = excluded.latest_form_type,
            latest_accession_number = excluded.latest_accession_number,
            fallback_status = excluded.fallback_status,
            financial_confidence = excluded.financial_confidence,
            usable_xbrl_flag = excluded.usable_xbrl_flag,
            source_id = excluded.source_id,
            review_reason = excluded.review_reason,
            updated_at = excluded.updated_at
        """,
        (
            ticker,
            model_family,
            profile_asof,
            cik,
            country_text,
            profile,
            standard,
            primary_taxonomy,
            latest_filing,
            latest_form,
            latest_accession,
            fallback,
            confidence,
            usable_xbrl,
            source_id,
            reason,
            now,
            now,
        ),
    )
    return {
        "reporting_profile": profile,
        "reporting_standard": standard,
        "latest_filing_date": latest_filing,
        "latest_form_type": latest_form,
        "financial_confidence": confidence,
        "review_reason": reason,
        "profile_asof_date": profile_asof,
    }


def write_report(
    path: Path,
    rows: list[dict[str, Any]],
    *,
    preserve_existing_tickers: bool = False,
) -> None:
    merged = {str(row.get("ticker") or ""): row for row in rows}
    if preserve_existing_tickers and path.exists():
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                ticker = str(row.get("ticker") or "").strip()
                if ticker and ticker not in merged:
                    merged[ticker] = dict(row)
    write_csv_atomic(path, REPORT_FIELDS, [merged[ticker] for ticker in sorted(merged)])


def source_status(conn: Any, source_id: str) -> str:
    row = conn.execute("SELECT status FROM source_registry WHERE source_id = ?", (source_id,)).fetchone()
    return str(row["status"]) if row is not None else ""


def start_ingestion_run(conn: Any, *, source_id: str) -> int:
    now = utc_now()
    with conn:
        cur = conn.execute(
            """
            INSERT INTO ingestion_runs(source_id, started_at, status, created_at)
            VALUES (?, ?, 'running', ?)
            """,
            (source_id, now, now),
        )
    if cur.lastrowid is None:
        raise RuntimeError(f"Failed to create ingestion run for {source_id}")
    return int(cur.lastrowid)


def finish_ingestion_run(
    conn: Any, *, ingestion_run_id: int, status: str, request_count: int, row_count: int, message: str
) -> None:
    with conn:
        conn.execute(
            """
            UPDATE ingestion_runs
            SET completed_at = ?, status = ?, request_count = ?, row_count = ?, message = ?
            WHERE ingestion_run_id = ?
            """,
            (utc_now(), status, int(request_count), int(row_count), str(message or ""), int(ingestion_run_id)),
        )


def purge_stale_cik_artifacts(
    conn: Any,
    *,
    ticker: str,
    cik: str,
    submissions_source_id: str,
    companyfacts_source_id: str,
    model_family: str,
) -> None:
    if not cik:
        return
    stale_cik_found = any(
        conn.execute(sql, params).fetchone() is not None
        for sql, params in (
            (
                "SELECT 1 FROM fact_sec_xbrl_fact WHERE ticker = ? AND source_id = ? AND COALESCE(cik, '') <> ? LIMIT 1",
                (ticker, companyfacts_source_id, cik),
            ),
            (
                "SELECT 1 FROM fact_sec_xbrl_fact_raw WHERE ticker = ? AND source_id = ? AND COALESCE(cik, '') <> ? LIMIT 1",
                (ticker, companyfacts_source_id, cik),
            ),
            (
                "SELECT 1 FROM fact_sec_filing WHERE ticker = ? AND source_id IN (?, ?) AND COALESCE(cik, '') <> ? LIMIT 1",
                (ticker, submissions_source_id, companyfacts_source_id, cik),
            ),
            (
                "SELECT 1 FROM dim_issuer_reporting_profile WHERE ticker = ? AND model_family = ? AND COALESCE(cik, '') NOT IN ('', ?) LIMIT 1",
                (ticker, model_family, cik),
            ),
        )
    )
    if stale_cik_found:
        conn.execute(
            """
            DELETE FROM fact_financial_statement_canonical
            WHERE ticker = ? AND source_id = ? AND model_family = ?
            """,
            (ticker, companyfacts_source_id, model_family),
        )
        conn.execute(
            """
            DELETE FROM feature_financial_statement
            WHERE ticker = ? AND source_id = ? AND model_family = ?
            """,
            (ticker, companyfacts_source_id, model_family),
        )
        conn.execute(
            "DELETE FROM feature_financial_metric_availability WHERE ticker = ? AND model_family = ?",
            (ticker, model_family),
        )
    conn.execute(
        """
        DELETE FROM fact_sec_xbrl_fact
        WHERE ticker = ?
          AND source_id = ?
          AND COALESCE(cik, '') <> ?
        """,
        (ticker, companyfacts_source_id, cik),
    )
    conn.execute(
        """
        DELETE FROM fact_sec_xbrl_fact_raw
        WHERE ticker = ?
          AND source_id = ?
          AND COALESCE(cik, '') <> ?
        """,
        (ticker, companyfacts_source_id, cik),
    )
    conn.execute(
        """
        DELETE FROM fact_sec_filing
        WHERE ticker = ?
          AND source_id IN (?, ?)
          AND COALESCE(cik, '') <> ?
        """,
        (ticker, submissions_source_id, companyfacts_source_id, cik),
    )
    conn.execute(
        """
        DELETE FROM dim_issuer_reporting_profile
        WHERE ticker = ?
          AND model_family = ?
          AND COALESCE(cik, '') NOT IN ('', ?)
        """,
        (ticker, model_family, cik),
    )
    conn.execute(
        """
        DELETE FROM dim_issuer_reporting_profile_history
        WHERE ticker = ?
          AND model_family = ?
          AND COALESCE(cik, '') NOT IN ('', ?)
        """,
        (ticker, model_family, cik),
    )


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
    model_family = str(
        args.model_family or cfg_get(config, "industrials_universe.initial_subsector", "defense") or "defense"
    ).strip()
    submissions_source_id = str(
        cfg_get(config, "sec_fundamentals.submissions_source_id", "sec_submissions") or "sec_submissions"
    )
    companyfacts_source_id = str(
        cfg_get(config, "sec_fundamentals.companyfacts_source_id", "sec_companyfacts") or "sec_companyfacts"
    )
    submissions_template = str(
        cfg_get(config, "sec_fundamentals.submissions_url_template") or "https://data.sec.gov/submissions/CIK{cik}.json"
    )
    companyfacts_template = str(
        cfg_get(config, "sec_fundamentals.companyfacts_url_template")
        or "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    )
    # CF-1: expand env-var templates before use, and fail loudly when the
    # expanded value is empty or carries no contact address — SEC fair-access
    # policy requires a contact in every User-Agent, and a literal "${...}"
    # template must never be sent upstream.
    user_agent = (
        ""
        if args.profiles_only or (args.filing_catalog_cache_only and not args.filing_catalog_fetch_missing)
        else resolve_sec_user_agent(config)
    )
    timeout_sec = float(cfg_get(config, "sec_fundamentals.timeout_sec", 30.0))
    max_retries = int(cfg_get(config, "sec_fundamentals.max_retries", 3))
    sleep_sec = float(cfg_get(config, "sec_fundamentals.request_sleep_sec", 0.12))
    start_date = parse_date(cfg_get(config, "sec_fundamentals.start_date", "2015-01-01"))
    allowed_forms = {str(form).upper() for form in (cfg_get(config, "sec_fundamentals.forms", []) or [])}
    cache_dir = resolve_path(cfg_get(config, "sec_fundamentals.cache_dir"), base_dir=base_dir)
    archive_enabled = as_bool(cfg_get(config, "sec_archive.enabled", True))
    configured_archive_all_family_members = as_bool(cfg_get(config, "sec_archive.all_family_members", False))
    archive_core_metric_recovery_tickers = {
        normalize_ticker(item)
        for item in (cfg_get(config, "sec_archive.core_metric_recovery_tickers", []) or [])
        if normalize_ticker(item)
    }
    archive_index_template = str(
        cfg_get(config, "sec_archive.index_url_template")
        or "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_nodash}/index.json"
    )
    archive_document_template = str(
        cfg_get(config, "sec_archive.document_url_template")
        or "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_nodash}/{document_name}"
    )
    archive_submission_file_template = str(
        cfg_get(config, "sec_archive.submission_file_url_template") or "https://data.sec.gov/submissions/{file_name}"
    )
    archive_browse_edgar_template = str(
        cfg_get(config, "sec_archive.browse_edgar_atom_url_template")
        or "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type={form_type}&owner=exclude&count=100&output=atom"
    )
    archive_max_submission_files = int(cfg_get(config, "sec_archive.max_submission_history_files", 0) or 0)
    configured_archive_max_filings = int(cfg_get(config, "sec_archive.max_filings_per_ticker", 0) or 0)
    archive_max_filings = (
        configured_archive_max_filings
        if args.archive_max_filings_per_ticker is None
        else int(args.archive_max_filings_per_ticker)
    )
    archive_supplemental_forms = {
        str(form).strip().upper()
        for form in (cfg_get(config, "sec_archive.supplemental_forms", []) or [])
        if str(form).strip()
    }
    archive_max_supplemental_filings = int(cfg_get(config, "sec_archive.max_supplemental_filings_per_ticker", 0) or 0)
    archive_max_documents_raw = cfg_get(
        config,
        "sec_archive.max_documents_per_filing",
        5,
    )
    configured_archive_max_documents = int(archive_max_documents_raw) if archive_max_documents_raw is not None else 5
    archive_max_documents = (
        configured_archive_max_documents
        if args.archive_max_documents_per_filing is None
        else int(args.archive_max_documents_per_filing)
    )
    if archive_max_filings < 0 or archive_max_documents < 0:
        raise ValueError("Archive filing/document limits must be zero or positive")
    archive_include_pdf_documents = bool(cfg_get(config, "sec_archive.include_pdf_documents", False))
    archive_pdf_ocr_enabled = bool(cfg_get(config, "sec_archive.pdf_ocr_enabled", False))
    archive_max_pdf_pages = int(cfg_get(config, "sec_archive.max_pdf_pages", 250) or 250)
    archive_max_pdf_bytes = int(cfg_get(config, "sec_archive.max_pdf_bytes", 25_000_000) or 25_000_000)
    archive_pdf_extraction_timeout_sec = float(cfg_get(config, "sec_archive.pdf_extraction_timeout_sec", 30.0) or 30.0)
    archive_document_keywords = tuple(
        sorted(
            {value.strip().lower() for value in str(args.archive_document_keywords or "").split(",") if value.strip()}
        )
    )
    archive_accession_scope: dict[str, set[str]] = {}
    if args.archive_accession_scope_csv is not None:
        if not args.archive_cache_only:
            raise ValueError("--archive-accession-scope-csv requires --archive-cache-only")
        scope_path = args.archive_accession_scope_csv.expanduser().resolve()
        if not scope_path.is_file():
            raise ValueError(f"Archive accession scope CSV not found: {scope_path}")
        with scope_path.open(
            "r",
            encoding="utf-8-sig",
            newline="",
        ) as handle:
            for line_number, row in enumerate(
                csv.DictReader(handle),
                start=2,
            ):
                ticker = normalize_ticker(row.get("ticker"))
                accession = str(row.get("accession_number") or "").strip()
                if not ticker or not accession:
                    raise ValueError(f"{scope_path}:{line_number} requires ticker and accession_number")
                archive_accession_scope.setdefault(ticker, set()).add(accession)
    # FN-2: minimum fraction of previously stored archive facts a refetch must
    # reproduce before the purge-and-swap is allowed to destroy existing state.
    archive_min_refetch_fraction = float(cfg_get(config, "sec_archive.min_refetch_fact_fraction", 0.5))
    # FN-8: retention window for raw_api_responses payloads (days; <= 0 disables pruning).
    raw_response_retention_days = int(cfg_get(config, "sec_fundamentals.raw_response_retention_days", 90) or 0)
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(cfg_get(config, "sec_fundamentals.sync_output_csv"), base_dir=base_dir)
    )
    include_historical = bool(
        args.include_historical or cfg_get(config, "sec_fundamentals.include_historical_members", False)
    )
    # EL-3: reporting overrides are point-in-time; select the rows effective at
    # the evaluation asof (defaults to the sync run's UTC date, never implicitly
    # a different wall-clock date for historical replays).
    asof_raw = str(args.asof or "").strip()
    if asof_raw:
        evaluation_asof = parse_date(asof_raw)
        if not evaluation_asof:
            raise ValueError(f"Invalid --asof value: {args.asof!r} (expected YYYY-MM-DD)")
    else:
        evaluation_asof = datetime.now(timezone.utc).date().isoformat()
    # DR-2: per-family CSV resolution with a defense-only legacy fallback;
    # a configured-but-missing file raises instead of loading zero overrides.
    reporting_overrides_path = resolve_reporting_overrides_path(config, base_dir=base_dir, model_family=model_family)
    reporting_graduations_path = resolve_reporting_graduations_path(
        config, base_dir=base_dir, model_family=model_family
    )
    reporting_overrides = load_reporting_override_sources(
        [reporting_overrides_path, reporting_graduations_path],
        asof=evaluation_asof,
    )
    ticker_filter = parse_ticker_list(args.tickers)
    catalog_forms = {
        str(form).strip().upper() for form in str(args.filing_catalog_forms or "").split(",") if str(form).strip()
    } or set(allowed_forms)
    catalog_start_date = parse_date(args.filing_catalog_start_date or start_date)
    if args.filing_catalog_start_date and not catalog_start_date:
        raise ValueError(f"Invalid --filing-catalog-start-date value: {args.filing_catalog_start_date!r}")
    unsupported_catalog_forms = catalog_forms - allowed_forms
    if unsupported_catalog_forms:
        raise ValueError(
            "--filing-catalog-forms contains forms not enabled by "
            "sec_fundamentals.forms: " + ",".join(sorted(unsupported_catalog_forms))
        )
    if args.filing_catalog_cache_only and not ticker_filter:
        raise ValueError("--filing-catalog-cache-only requires an explicit --tickers filter")
    if args.filing_catalog_cache_only and args.archive_cache_only:
        raise ValueError("--filing-catalog-cache-only cannot be combined with --archive-cache-only")
    if args.filing_catalog_fetch_missing and not args.filing_catalog_cache_only:
        raise ValueError("--filing-catalog-fetch-missing requires --filing-catalog-cache-only")
    if args.archive_selected and not ticker_filter:
        raise ValueError("--archive-selected requires an explicit --tickers filter")
    if args.archive_cache_only and not args.archive_selected:
        raise ValueError("--archive-cache-only requires --archive-selected --tickers")
    if args.archive_scan_all_documents and not args.archive_selected:
        raise ValueError("--archive-scan-all-documents requires --archive-selected --tickers")
    if args.archive_cache_only and any(
        (
            args.incremental,
            args.force,
            args.force_submissions,
            args.force_companyfacts,
            args.profiles_only,
        )
    ):
        raise ValueError(
            "--archive-cache-only cannot be combined with incremental, "
            "companyfacts/submissions force, or profile-only modes"
        )
    if args.archive_cache_workers < 1:
        raise ValueError("--archive-cache-workers must be at least 1")
    if args.profiles_only and any(
        (
            args.incremental,
            args.force,
            args.force_submissions,
            args.force_companyfacts,
            args.force_archive,
            args.archive_bootstrap,
            args.archive_selected,
            args.archive_cache_only,
            args.filing_catalog_cache_only,
            args.archive_scan_all_documents,
        )
    ):
        raise ValueError("--profiles-only cannot be combined with SEC fetch/force options")
    if args.profiles_all_members and not (args.profiles_only and args.include_historical):
        raise ValueError("--profiles-all-members requires --profiles-only --include-historical")
    if args.incremental and args.force:
        raise ValueError(
            "--incremental cannot be combined with --force; use --force-submissions, --force-companyfacts, or --force-archive."
        )
    force_submissions = bool(args.force or args.force_submissions or args.incremental)
    force_submission_history = bool(args.force or args.force_submissions)
    force_companyfacts = bool(args.force or args.force_companyfacts)
    force_archive = bool(args.force or args.force_archive)
    archive_bootstrap = bool(args.archive_bootstrap)
    archive_all_family_members = bool(configured_archive_all_family_members or args.archive_selected)
    if archive_bootstrap and not archive_all_family_members:
        raise ValueError(
            "--archive-bootstrap requires sec_archive.all_family_members=true "
            "or an explicit --archive-selected --tickers scope"
        )

    with closing(connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0)))) as conn:
        init_db(conn)
        if not args.skip_source_registry:
            registry_path = resolve_path(cfg_get(config, "source_registry.path"), base_dir=base_dir)
            with conn:
                upsert_source_registry(conn, load_source_registry(registry_path))
        if source_status(conn, submissions_source_id) != "active":
            raise ValueError(f"Source {submissions_source_id} must be active in source_registry.")
        if not args.filing_catalog_cache_only and source_status(conn, companyfacts_source_id) != "active":
            raise ValueError(f"Source {companyfacts_source_id} must be active in source_registry.")

        tickers = load_universe(
            conn,
            model_family=model_family,
            ticker_filter=ticker_filter,
            include_historical=include_historical,
            membership_asof=(evaluation_asof if args.profiles_only and not args.profiles_all_members else ""),
            currency_asof=evaluation_asof,
        )
        if args.max_tickers > 0:
            tickers = tickers[: args.max_tickers]
        if not tickers:
            raise ValueError(f"No tickers found for model_family={model_family}")
        LOGGER.info(
            "SEC fundamentals sync mode: tickers=%d incremental=%s include_historical=%s force_submissions=%s force_companyfacts=%s force_archive=%s archive_bootstrap=%s",
            len(tickers),
            bool(args.incremental),
            include_historical,
            force_submissions,
            force_companyfacts,
            force_archive,
            archive_bootstrap,
        )

        if args.filing_catalog_cache_only:
            catalog_rows = catalog_cached_submission_filings(
                conn,
                items=tickers,
                source_id=submissions_source_id,
                cache_dir=cache_dir,
                allowed_forms=catalog_forms,
                start_date=catalog_start_date,
                end_date=evaluation_asof,
                max_history_files=archive_max_submission_files,
                fetch_missing=bool(args.filing_catalog_fetch_missing),
                submissions_url_template=submissions_template,
                history_url_template=archive_submission_file_template,
                user_agent=user_agent,
                timeout_sec=timeout_sec,
                max_retries=max_retries,
                sleep_sec=sleep_sec,
            )
            write_csv_atomic(
                output_csv,
                FILING_CATALOG_REPORT_FIELDS,
                catalog_rows,
            )
            catalog_failures = [
                row for row in catalog_rows if row["status"] in {"catalog_failed", "catalog_partial_history_cache"}
            ]
            if catalog_failures and not args.allow_partial:
                raise RuntimeError(
                    "Cached filing catalog failures: "
                    + "; ".join(f"{row['ticker']}:{row['status']}:{row['error']}" for row in catalog_failures[:20])
                )
            LOGGER.info(
                "Cached filing catalog complete: tickers=%d filings=%d failures=%d output=%s",
                len(catalog_rows),
                sum(int(row["cataloged_filing_count"] or 0) for row in catalog_rows),
                len(catalog_failures),
                output_csv,
            )
            return

        concept_map = load_concept_map(conn)
        add_family_concept_mappings(
            concept_map,
            model_family=model_family,
            config=config,
            base_dir=base_dir,
        )
        if args.archive_cache_only:
            if not archive_enabled:
                raise ValueError("--archive-cache-only requires sec_archive.enabled=true")

            def hydrate_cache_item(
                item: dict[str, Any],
            ) -> dict[str, Any]:
                ticker = normalize_ticker(item.get("ticker"))
                cik = normalize_cik(item.get("cik"))
                if not ticker or not cik:
                    return {
                        "ticker": ticker,
                        "cik": cik,
                        "status": "source_unavailable",
                        "archive_request_count": 0,
                        "cached_fact_candidate_count": 0,
                        "error": "missing_ticker_or_cik",
                    }
                reporting_override = reporting_overrides.get(ticker)
                try:
                    with closing(
                        connect(
                            db_path,
                            timeout_sec=float(
                                cfg_get(
                                    config,
                                    "runtime.sqlite_timeout_sec",
                                    30.0,
                                )
                            ),
                        )
                    ) as cache_conn:
                        staged_count, _, request_count = sync_archive_xbrl(
                            cache_conn,
                            ticker=ticker,
                            cik=cik,
                            source_id=companyfacts_source_id,
                            submissions_source_id=submissions_source_id,
                            model_family=model_family,
                            cache_dir=cache_dir,
                            force=force_archive,
                            user_agent=user_agent,
                            timeout_sec=timeout_sec,
                            max_retries=max_retries,
                            sleep_sec=sleep_sec,
                            concept_map=concept_map,
                            start_date=start_date,
                            index_url_template=archive_index_template,
                            document_url_template=archive_document_template,
                            max_filings=archive_max_filings,
                            supplemental_forms=archive_supplemental_forms,
                            max_supplemental_filings=(archive_max_supplemental_filings),
                            max_documents=archive_max_documents,
                            include_pdf_documents=(archive_include_pdf_documents),
                            pdf_ocr_enabled=archive_pdf_ocr_enabled,
                            max_pdf_pages=archive_max_pdf_pages,
                            max_pdf_bytes=archive_max_pdf_bytes,
                            pdf_extraction_timeout_sec=(archive_pdf_extraction_timeout_sec),
                            parse_all_documents=bool(
                                reporting_override is not None
                                and reporting_override.reporting_profile in FPI_HYBRID_PROFILES
                            ),
                            text_tables_only=not bool(args.archive_scan_all_documents),
                            strict_registration_statements=bool(
                                reporting_override is not None
                                and reporting_override.handling_type in FULL_STATEMENT_ARCHIVE_HANDLING_TYPES
                            ),
                            company_currency=str(item.get("currency") or ""),
                            min_refetch_fact_fraction=(archive_min_refetch_fraction),
                            ingestion_run_id=0,
                            scan_all_documents=bool(args.archive_scan_all_documents),
                            cache_only=True,
                            document_keywords=archive_document_keywords,
                            accession_filter=(archive_accession_scope.get(ticker) if archive_accession_scope else None),
                        )
                    return {
                        "ticker": ticker,
                        "cik": cik,
                        "status": "cache_hydrated",
                        "archive_request_count": request_count,
                        "cached_fact_candidate_count": staged_count,
                        "error": "",
                    }
                except Exception as exc:
                    detail = f"{type(exc).__name__}: {exc}"
                    LOGGER.exception(
                        "Archive cache hydration failed ticker=%s",
                        ticker,
                    )
                    return {
                        "ticker": ticker,
                        "cik": cik,
                        "status": "cache_hydration_failed",
                        "archive_request_count": 0,
                        "cached_fact_candidate_count": 0,
                        "error": detail,
                    }

            def hydrate_cache_group(
                items: list[dict[str, Any]],
            ) -> list[dict[str, Any]]:
                # Share classes and aliases can use the same CIK/cache path.
                # Process them serially within one worker to prevent concurrent
                # writes to the same accession documents.
                return [hydrate_cache_item(item) for item in items]

            hydration_groups = group_cache_hydration_items(tickers)
            indexed_results: list[tuple[int, list[dict[str, Any]]]] = []
            with ThreadPoolExecutor(
                max_workers=args.archive_cache_workers,
                thread_name_prefix="sec-cache",
            ) as executor:
                futures = {
                    executor.submit(hydrate_cache_group, group): index for index, group in enumerate(hydration_groups)
                }
                for completed_count, future in enumerate(
                    as_completed(futures),
                    start=1,
                ):
                    indexed_results.append((futures[future], future.result()))
                    if completed_count % 10 == 0 or completed_count == len(futures):
                        LOGGER.info(
                            "Archive cache hydration progress: CIK groups=%d/%d",
                            completed_count,
                            len(futures),
                        )
            grouped_results = [group_rows for _, group_rows in sorted(indexed_results)]
            cache_report_rows = [row for group_rows in grouped_results for row in group_rows]
            cache_failures = [
                f"{row['ticker']}: {row['error']}"
                for row in cache_report_rows
                if row["status"] == "cache_hydration_failed"
            ]
            write_csv_atomic(
                output_csv,
                CACHE_HYDRATION_REPORT_FIELDS,
                cache_report_rows,
            )
            if cache_failures and not args.allow_partial:
                raise RuntimeError("Archive cache hydration failures: " + "; ".join(cache_failures[:20]))
            LOGGER.info(
                "Archive cache-only hydration complete: tickers=%d failures=%d output=%s",
                len(cache_report_rows),
                len(cache_failures),
                output_csv,
            )
            return
        if args.profiles_only:
            report_rows: list[dict[str, Any]] = []
            with conn:
                for item in tickers:
                    ticker = normalize_ticker(item.get("ticker"))
                    if not ticker:
                        continue
                    cik = normalize_cik(item.get("cik"))
                    purge_stale_cik_artifacts(
                        conn,
                        ticker=ticker,
                        cik=cik,
                        submissions_source_id=submissions_source_id,
                        companyfacts_source_id=companyfacts_source_id,
                        model_family=model_family,
                    )
                    profile = classify_reporting_profile(
                        conn,
                        ticker=ticker,
                        cik=cik,
                        country=str(item.get("country") or ""),
                        model_family=model_family,
                        source_id=companyfacts_source_id,
                        asof=evaluation_asof,
                        override=reporting_overrides.get(ticker),
                    )
                    raw_fact_count, mapped_fact_count = sec_fact_counts(
                        conn,
                        ticker=ticker,
                        source_id=companyfacts_source_id,
                    )
                    report_rows.append(
                        {
                            "ticker": ticker,
                            "cik": cik,
                            "company_name": str(item.get("company_name") or ""),
                            "country": str(item.get("country") or ""),
                            "status": "review" if profile["review_reason"] else "success",
                            "reporting_profile": profile["reporting_profile"],
                            "reporting_standard": profile["reporting_standard"],
                            "latest_filing_date": profile["latest_filing_date"],
                            "latest_form_type": profile["latest_form_type"],
                            "filing_count": len(
                                filing_keys(
                                    conn,
                                    ticker=ticker,
                                    source_id=submissions_source_id,
                                )
                            ),
                            "raw_fact_count": raw_fact_count,
                            "mapped_fact_count": mapped_fact_count,
                            "review_reason": profile["review_reason"],
                        }
                    )
            write_report(
                output_csv,
                report_rows,
                preserve_existing_tickers=bool(ticker_filter),
            )
            LOGGER.info(
                "Rebuilt reporting-profile snapshots: model_family=%s asof=%s rows=%d output=%s",
                model_family,
                evaluation_asof,
                len(report_rows),
                output_csv,
            )
            return
        run_id = start_run(conn, run_type=RUN_TYPE, input_path=config_path)
        submissions_run_id = start_ingestion_run(conn, source_id=submissions_source_id)
        companyfacts_run_id = start_ingestion_run(conn, source_id=companyfacts_source_id)
        report_rows: list[dict[str, Any]] = []
        failures: list[str] = []
        submissions_requests = 0
        companyfacts_requests = 0
        try:
            with conn:
                if not args.incremental:
                    clear_stage_issues(conn, model_family=model_family, ticker_filter=ticker_filter or None)

            for item in tickers:
                ticker = normalize_ticker(item.get("ticker"))
                cik = ""
                company_name = str(item.get("company_name") or "")
                country = str(item.get("country") or "")
                company_currency = str(item.get("currency") or "")
                reporting_override = reporting_overrides.get(ticker)
                filing_count = 0
                raw_count = 0
                mapped_count = 0
                status = "success"
                review_reason = ""
                if not ticker:
                    continue
                try:
                    # XC-21: validate the CIK inside the per-ticker try so one bad
                    # seed value fails this ticker (honoring --allow-partial)
                    # instead of aborting the whole run.
                    cik = sec_cik(item.get("cik")) if normalize_cik(item.get("cik")) else ""
                    # XC-12: remember whether the previous run left an open sync
                    # failure before FN-11's per-ticker clear removes the marker.
                    prior_sync_failed = (
                        args.incremental
                        and conn.execute(
                            """
                            SELECT 1
                            FROM data_quality_issues
                            WHERE stage = ?
                              AND model_family = ?
                              AND ticker = ?
                              AND issue_type = 'sec_sync_failed'
                              AND resolution_status = 'open'
                            LIMIT 1
                            """,
                            (RUN_TYPE, model_family, ticker),
                        ).fetchone()
                        is not None
                    )
                    with conn:
                        if args.incremental:
                            # FN-11: clear this ticker's stage issues up front so
                            # every branch re-emits at most one open copy per
                            # condition instead of accumulating daily duplicates.
                            clear_stage_issues(conn, model_family=model_family, ticker_filter=[ticker])
                        purge_stale_cik_artifacts(
                            conn,
                            ticker=ticker,
                            cik=cik,
                            submissions_source_id=submissions_source_id,
                            companyfacts_source_id=companyfacts_source_id,
                            model_family=model_family,
                        )
                    if reporting_override is not None and reporting_override.skip_sec_network:
                        status = "review"
                        review_reason = reporting_override.review_reason or reporting_override.handling_type
                        with conn:
                            add_issue(
                                conn,
                                severity="warning",
                                ticker=ticker,
                                model_family=model_family,
                                source_id=companyfacts_source_id,
                                issue_type="sec_reporting_override",
                                detail=f"{reporting_override.handling_type}; parent={reporting_override.parent_ticker}",
                            )
                            profile = classify_reporting_profile(
                                conn,
                                ticker=ticker,
                                cik=cik,
                                country=country,
                                model_family=model_family,
                                source_id=companyfacts_source_id,
                                asof=evaluation_asof,
                                override=reporting_override,
                            )
                    elif not cik:
                        status = "review"
                        review_reason = "missing_cik"
                        with conn:
                            add_issue(
                                conn,
                                severity="error",
                                ticker=ticker,
                                model_family=model_family,
                                source_id=submissions_source_id,
                                issue_type="missing_cik",
                                detail="Ticker has no CIK; SEC financial sync skipped.",
                            )
                            profile = classify_reporting_profile(
                                conn,
                                ticker=ticker,
                                cik="",
                                country=country,
                                model_family=model_family,
                                source_id=submissions_source_id,
                                asof=evaluation_asof,
                                override=reporting_override,
                            )
                    else:
                        existing_filing_keys = (
                            filing_keys(conn, ticker=ticker, source_id=submissions_source_id)
                            if args.incremental
                            else set()
                        )
                        existing_archive_metadata = has_filing_metadata(
                            conn, ticker=ticker, source_id=submissions_source_id
                        )
                        submissions_url = submissions_template.format(cik=cik)
                        submissions_cache = cache_path(cache_dir, source_id=submissions_source_id, cik=cik)
                        status_code, submissions_payload, submissions_text, submissions_fetch_mode = load_or_fetch_json(
                            submissions_url,
                            cache_file=submissions_cache,
                            force=force_submissions,
                            user_agent=user_agent,
                            timeout_sec=timeout_sec,
                            max_retries=max_retries,
                            sleep_sec=sleep_sec,
                        )
                        submissions_requests += 1
                        name_mismatch = False
                        with conn:
                            if submissions_fetch_mode == "network":
                                # FN-8: cache hits are not new observations; only
                                # record network fetches as raw responses.
                                record_raw_response(
                                    conn,
                                    source_id=submissions_source_id,
                                    endpoint=submissions_url,
                                    status=status_code,
                                    payload_text=submissions_text,
                                    asof_date=datetime.now(timezone.utc).date().isoformat(),
                                    ingestion_run_id=submissions_run_id,
                                )
                            sec_conformed_name = str(submissions_payload.get("name") or "").strip()
                            name_score = normalized_name_similarity(company_name, sec_conformed_name)
                            if (
                                company_name
                                and sec_conformed_name
                                and not names_plausibly_match(company_name, sec_conformed_name)
                            ):
                                status = "review"
                                review_reason = f"sec_cik_company_name_mismatch:{sec_conformed_name}"
                                name_mismatch = True
                                add_issue(
                                    conn,
                                    severity="error",
                                    ticker=ticker,
                                    model_family=model_family,
                                    source_id=submissions_source_id,
                                    issue_type="sec_cik_company_name_mismatch",
                                    detail=f"ticker_company={company_name}; sec_name={sec_conformed_name}; score={name_score:.3f}; cik={cik}",
                                )
                                profile = classify_reporting_profile(
                                    conn,
                                    ticker=ticker,
                                    cik=cik,
                                    country=country,
                                    model_family=model_family,
                                    source_id=submissions_source_id,
                                    asof=evaluation_asof,
                                    override=reporting_override,
                                )
                            else:
                                filing_count = upsert_filings(
                                    conn,
                                    ticker=ticker,
                                    cik=cik,
                                    source_id=submissions_source_id,
                                    payload=submissions_payload,
                                    allowed_forms=allowed_forms,
                                    start_date=start_date,
                                )
                        if name_mismatch:
                            report_rows.append(
                                {
                                    "ticker": ticker,
                                    "cik": cik,
                                    "company_name": company_name,
                                    "country": country,
                                    "status": status,
                                    "reporting_profile": profile.get("reporting_profile", ""),
                                    "reporting_standard": profile.get("reporting_standard", ""),
                                    "latest_filing_date": profile.get("latest_filing_date", ""),
                                    "latest_form_type": profile.get("latest_form_type", ""),
                                    "filing_count": 0,
                                    "raw_fact_count": 0,
                                    "mapped_fact_count": 0,
                                    "review_reason": review_reason,
                                }
                            )
                            continue
                        # XC-23: the history/browse helpers fetch before writing and
                        # manage their own short transactions, so they must run
                        # outside any open write transaction.
                        if (
                            archive_enabled
                            and (archive_all_family_members or should_attempt_archive(reporting_override))
                            and (not args.incremental or not existing_archive_metadata or force_submission_history)
                        ):
                            extra_filing_count, extra_submission_requests = sync_submission_history_files(
                                conn,
                                ticker=ticker,
                                cik=cik,
                                source_id=submissions_source_id,
                                root_payload=submissions_payload,
                                cache_dir=cache_dir,
                                force=force_submission_history,
                                user_agent=user_agent,
                                timeout_sec=timeout_sec,
                                max_retries=max_retries,
                                sleep_sec=sleep_sec,
                                allowed_forms=allowed_forms,
                                start_date=start_date,
                                url_template=archive_submission_file_template,
                                max_files=archive_max_submission_files,
                                ingestion_run_id=submissions_run_id,
                            )
                            filing_count += extra_filing_count
                            submissions_requests += extra_submission_requests
                            if filing_count == 0:
                                browse_filing_count, browse_requests = sync_browse_edgar_filings(
                                    conn,
                                    ticker=ticker,
                                    cik=cik,
                                    source_id=submissions_source_id,
                                    cache_dir=cache_dir,
                                    force=force_submission_history,
                                    user_agent=user_agent,
                                    timeout_sec=timeout_sec,
                                    max_retries=max_retries,
                                    sleep_sec=sleep_sec,
                                    allowed_forms=allowed_forms,
                                    start_date=start_date,
                                    url_template=archive_browse_edgar_template,
                                    ingestion_run_id=submissions_run_id,
                                )
                                filing_count += browse_filing_count
                                submissions_requests += browse_requests

                        new_filing_keys = (
                            filing_keys(conn, ticker=ticker, source_id=submissions_source_id) - existing_filing_keys
                            if args.incremental
                            else set()
                        )
                        has_existing_state = has_existing_sec_financial_state(
                            conn,
                            ticker=ticker,
                            model_family=model_family,
                            source_id=companyfacts_source_id,
                            override=reporting_override,
                        )
                        if should_skip_incremental_companyfacts(
                            incremental=bool(args.incremental),
                            new_filing_keys=new_filing_keys,
                            force_companyfacts=force_companyfacts,
                            force_archive=force_archive or archive_bootstrap,
                            prior_sync_failed=prior_sync_failed,
                            has_existing_state=has_existing_state,
                        ):
                            with conn:
                                profile = classify_reporting_profile(
                                    conn,
                                    ticker=ticker,
                                    cik=cik,
                                    country=country,
                                    model_family=model_family,
                                    source_id=companyfacts_source_id,
                                    asof=evaluation_asof,
                                    override=reporting_override,
                                )
                                if profile.get("review_reason"):
                                    # FN-11: the per-ticker clear at the top of the
                                    # try block removed the prior copy; re-emit the
                                    # standing review condition for skipped tickers
                                    # so the validator's issue-parity check holds.
                                    add_issue(
                                        conn,
                                        severity="warning",
                                        ticker=ticker,
                                        model_family=model_family,
                                        source_id=companyfacts_source_id,
                                        issue_type="financial_reporting_profile_review",
                                        detail=str(profile["review_reason"]),
                                    )
                                raw_count, mapped_count = sec_fact_counts(
                                    conn,
                                    ticker=ticker,
                                    source_id=companyfacts_source_id,
                                )
                            status = "skipped_current"
                            review_reason = str(profile.get("review_reason", "") or "")
                            report_rows.append(
                                {
                                    "ticker": ticker,
                                    "cik": cik,
                                    "company_name": company_name,
                                    "country": country,
                                    "status": status,
                                    "reporting_profile": profile.get("reporting_profile", ""),
                                    "reporting_standard": profile.get("reporting_standard", ""),
                                    "latest_filing_date": profile.get("latest_filing_date", ""),
                                    "latest_form_type": profile.get("latest_form_type", ""),
                                    "filing_count": filing_count,
                                    "raw_fact_count": raw_count,
                                    "mapped_fact_count": mapped_count,
                                    "review_reason": review_reason,
                                }
                            )
                            continue

                        companyfacts_url = companyfacts_template.format(cik=cik)
                        companyfacts_cache = cache_path(cache_dir, source_id=companyfacts_source_id, cik=cik)
                        # FN-1: the per-CIK companyfacts cache is a mutable
                        # aggregate keyed only by CIK. Any ticker that reaches
                        # this fetch in incremental mode (new filings detected,
                        # or no complete existing state) must not trust the
                        # cached payload, or the new filing's facts are never
                        # ingested and the ticker is skipped forever after.
                        status_code, companyfacts_payload, companyfacts_text, companyfacts_fetch_mode = (
                            load_or_fetch_json(
                                companyfacts_url,
                                cache_file=companyfacts_cache,
                                force=should_force_companyfacts_payload_fetch(
                                    incremental=bool(args.incremental),
                                    force_companyfacts=force_companyfacts,
                                ),
                                user_agent=user_agent,
                                timeout_sec=timeout_sec,
                                max_retries=max_retries,
                                sleep_sec=sleep_sec,
                            )
                        )
                        companyfacts_requests += 1
                        with conn:
                            if companyfacts_fetch_mode == "network":
                                # FN-8: only network fetches are new observations.
                                record_raw_response(
                                    conn,
                                    source_id=companyfacts_source_id,
                                    endpoint=companyfacts_url,
                                    status=status_code,
                                    payload_text=companyfacts_text,
                                    asof_date=datetime.now(timezone.utc).date().isoformat(),
                                    ingestion_run_id=companyfacts_run_id,
                                )
                            raw_count, mapped_count = upsert_companyfacts(
                                conn,
                                ticker=ticker,
                                cik=cik,
                                source_id=companyfacts_source_id,
                                payload=companyfacts_payload,
                                concept_map=concept_map,
                                start_date=start_date,
                            )
                        archive_requests = 0
                        archive_mapped_count = 0
                        archive_attempted = False
                        if archive_enabled and (
                            archive_all_family_members or should_attempt_archive(reporting_override)
                        ):
                            # XC-23/FN-2: fetches outside write transactions with
                            # stage-then-swap purge protection inside the helper.
                            archive_attempted = True
                            strict_registration_statements = bool(
                                reporting_override is not None
                                and reporting_override.handling_type in FULL_STATEMENT_ARCHIVE_HANDLING_TYPES
                            )
                            archive_raw_count, archive_mapped_count, archive_requests = sync_archive_xbrl(
                                conn,
                                ticker=ticker,
                                cik=cik,
                                source_id=companyfacts_source_id,
                                submissions_source_id=submissions_source_id,
                                model_family=model_family,
                                cache_dir=cache_dir,
                                force=force_archive,
                                user_agent=user_agent,
                                timeout_sec=timeout_sec,
                                max_retries=max_retries,
                                sleep_sec=sleep_sec,
                                concept_map=concept_map,
                                start_date=start_date,
                                index_url_template=archive_index_template,
                                document_url_template=archive_document_template,
                                max_filings=archive_max_filings,
                                supplemental_forms=archive_supplemental_forms,
                                max_supplemental_filings=archive_max_supplemental_filings,
                                max_documents=archive_max_documents,
                                include_pdf_documents=archive_include_pdf_documents,
                                pdf_ocr_enabled=archive_pdf_ocr_enabled,
                                max_pdf_pages=archive_max_pdf_pages,
                                max_pdf_bytes=archive_max_pdf_bytes,
                                pdf_extraction_timeout_sec=archive_pdf_extraction_timeout_sec,
                                parse_all_documents=(
                                    reporting_override is not None
                                    and reporting_override.reporting_profile in FPI_HYBRID_PROFILES
                                ),
                                text_tables_only=(
                                    archive_all_family_members
                                    and not should_attempt_archive(reporting_override)
                                    and ticker not in archive_core_metric_recovery_tickers
                                ),
                                strict_registration_statements=strict_registration_statements,
                                company_currency=company_currency,
                                min_refetch_fact_fraction=archive_min_refetch_fraction,
                                ingestion_run_id=companyfacts_run_id,
                            )
                            raw_count += archive_raw_count
                            mapped_count += archive_mapped_count
                            companyfacts_requests += archive_requests
                        with conn:
                            if archive_attempted and archive_requests == 0:
                                add_issue(
                                    conn,
                                    severity="warning",
                                    ticker=ticker,
                                    model_family=model_family,
                                    source_id=companyfacts_source_id,
                                    issue_type="sec_archive_xbrl_no_filing_metadata",
                                    detail="Archive fallback could not run because SEC submissions metadata had no filing rows.",
                                )
                            elif archive_attempted and archive_mapped_count == 0:
                                add_issue(
                                    conn,
                                    severity="warning",
                                    ticker=ticker,
                                    model_family=model_family,
                                    source_id=companyfacts_source_id,
                                    issue_type="sec_archive_xbrl_no_mapped_facts",
                                    detail="Archive index/documents fetched but no mapped XBRL facts were extracted.",
                                )
                            profile = classify_reporting_profile(
                                conn,
                                ticker=ticker,
                                cik=cik,
                                country=country,
                                model_family=model_family,
                                source_id=companyfacts_source_id,
                                asof=evaluation_asof,
                                override=reporting_override,
                            )
                            if profile["review_reason"]:
                                add_issue(
                                    conn,
                                    severity="warning",
                                    ticker=ticker,
                                    model_family=model_family,
                                    source_id=companyfacts_source_id,
                                    issue_type="financial_reporting_profile_review",
                                    detail=str(profile["review_reason"]),
                                )
                                status = "review"
                                review_reason = str(profile["review_reason"])
                        time.sleep(sleep_sec)
                except SecRequestError as exc:
                    if exc.status_code == 404:
                        status = "review"
                        endpoint_source_id = (
                            companyfacts_source_id if "/companyfacts/" in exc.url else submissions_source_id
                        )
                        endpoint_run_id = (
                            companyfacts_run_id if endpoint_source_id == companyfacts_source_id else submissions_run_id
                        )
                        review_reason = f"sec_endpoint_404:{endpoint_source_id}"
                        with conn:
                            record_raw_response(
                                conn,
                                source_id=endpoint_source_id,
                                endpoint=exc.url,
                                status=exc.status_code,
                                payload_text=exc.body,
                                asof_date=datetime.now(timezone.utc).date().isoformat(),
                                ingestion_run_id=endpoint_run_id,
                            )
                            add_issue(
                                conn,
                                severity="warning",
                                ticker=ticker,
                                model_family=model_family,
                                source_id=endpoint_source_id,
                                issue_type="sec_endpoint_not_available",
                                detail=review_reason,
                            )
                        if (
                            archive_enabled
                            and endpoint_source_id == companyfacts_source_id
                            and cik
                            and (archive_all_family_members or should_attempt_archive(reporting_override))
                        ):
                            # XC-23: the archive helper fetches before writing and
                            # manages its own short transactions, so it must run
                            # outside any open write transaction. FN-2: it raises
                            # RuntimeError (rolling back its purge) when the
                            # refetch is implausibly small.
                            try:
                                strict_registration_statements = bool(
                                    reporting_override is not None
                                    and reporting_override.handling_type in FULL_STATEMENT_ARCHIVE_HANDLING_TYPES
                                )
                                archive_raw_count, archive_mapped_count, archive_requests = sync_archive_xbrl(
                                    conn,
                                    ticker=ticker,
                                    cik=cik,
                                    source_id=companyfacts_source_id,
                                    submissions_source_id=submissions_source_id,
                                    model_family=model_family,
                                    cache_dir=cache_dir,
                                    force=force_archive,
                                    user_agent=user_agent,
                                    timeout_sec=timeout_sec,
                                    max_retries=max_retries,
                                    sleep_sec=sleep_sec,
                                    concept_map=concept_map,
                                    start_date=start_date,
                                    index_url_template=archive_index_template,
                                    document_url_template=archive_document_template,
                                    max_filings=archive_max_filings,
                                    supplemental_forms=archive_supplemental_forms,
                                    max_supplemental_filings=archive_max_supplemental_filings,
                                    max_documents=archive_max_documents,
                                    include_pdf_documents=archive_include_pdf_documents,
                                    pdf_ocr_enabled=archive_pdf_ocr_enabled,
                                    max_pdf_pages=archive_max_pdf_pages,
                                    max_pdf_bytes=archive_max_pdf_bytes,
                                    pdf_extraction_timeout_sec=archive_pdf_extraction_timeout_sec,
                                    parse_all_documents=(
                                        reporting_override is not None
                                        and reporting_override.reporting_profile in FPI_HYBRID_PROFILES
                                    ),
                                    text_tables_only=(
                                        archive_all_family_members
                                        and not should_attempt_archive(reporting_override)
                                        and ticker not in archive_core_metric_recovery_tickers
                                    ),
                                    strict_registration_statements=strict_registration_statements,
                                    company_currency=company_currency,
                                    min_refetch_fact_fraction=archive_min_refetch_fraction,
                                    ingestion_run_id=companyfacts_run_id,
                                )
                                raw_count += archive_raw_count
                                mapped_count += archive_mapped_count
                                companyfacts_requests += archive_requests
                                with conn:
                                    if archive_mapped_count > 0:
                                        resolve_open_issue(
                                            conn,
                                            ticker=ticker,
                                            model_family=model_family,
                                            source_id=endpoint_source_id,
                                            issue_type="sec_endpoint_not_available",
                                            detail=review_reason,
                                            resolution_status="resolved_by_archive_fallback",
                                        )
                                        review_reason = ""
                                    elif archive_requests == 0:
                                        add_issue(
                                            conn,
                                            severity="warning",
                                            ticker=ticker,
                                            model_family=model_family,
                                            source_id=companyfacts_source_id,
                                            issue_type="sec_archive_xbrl_no_filing_metadata",
                                            detail="CompanyFacts 404 and archive fallback could not run because SEC submissions metadata had no filing rows.",
                                        )
                                    else:
                                        add_issue(
                                            conn,
                                            severity="warning",
                                            ticker=ticker,
                                            model_family=model_family,
                                            source_id=companyfacts_source_id,
                                            issue_type="sec_archive_xbrl_no_mapped_facts",
                                            detail="CompanyFacts 404; archive documents did not produce mapped facts.",
                                        )
                            except (SecRequestError, RuntimeError) as archive_exc:
                                with conn:
                                    add_issue(
                                        conn,
                                        severity="warning",
                                        ticker=ticker,
                                        model_family=model_family,
                                        source_id=companyfacts_source_id,
                                        issue_type="sec_archive_xbrl_unavailable",
                                        detail=f"CompanyFacts 404 and archive fallback failed: {type(archive_exc).__name__}: {archive_exc}",
                                    )
                        with conn:
                            profile = classify_reporting_profile(
                                conn,
                                ticker=ticker,
                                cik=cik,
                                country=country,
                                model_family=model_family,
                                source_id=endpoint_source_id,
                                asof=evaluation_asof,
                                override=reporting_override,
                            )
                    else:
                        status = "failed"
                        review_reason = f"{type(exc).__name__}: {exc}"
                        failures.append(f"{ticker}: {review_reason}")
                        with conn:
                            add_issue(
                                conn,
                                severity="error",
                                ticker=ticker,
                                model_family=model_family,
                                source_id=companyfacts_source_id,
                                issue_type="sec_sync_failed",
                                detail=review_reason,
                            )
                            profile = classify_reporting_profile(
                                conn,
                                ticker=ticker,
                                cik=cik,
                                country=country,
                                model_family=model_family,
                                source_id=companyfacts_source_id,
                                asof=evaluation_asof,
                                override=reporting_override,
                            )
                        if not args.allow_partial:
                            raise
                except Exception as exc:
                    status = "failed"
                    review_reason = f"{type(exc).__name__}: {exc}"
                    failures.append(f"{ticker}: {review_reason}")
                    with conn:
                        add_issue(
                            conn,
                            severity="error",
                            ticker=ticker,
                            model_family=model_family,
                            source_id=companyfacts_source_id,
                            issue_type="sec_sync_failed",
                            detail=review_reason,
                        )
                        profile = classify_reporting_profile(
                            conn,
                            ticker=ticker,
                            cik=cik,
                            country=country,
                            model_family=model_family,
                            source_id=companyfacts_source_id,
                            asof=evaluation_asof,
                            override=reporting_override,
                        )
                    if not args.allow_partial:
                        raise

                if status != "failed":
                    with conn:
                        resolve_successful_sync_issues(
                            conn,
                            ticker=ticker,
                            model_family=model_family,
                            source_id=companyfacts_source_id,
                        )

                report_rows.append(
                    {
                        "ticker": ticker,
                        "cik": cik,
                        "company_name": company_name,
                        "country": country,
                        "status": status,
                        "reporting_profile": profile.get("reporting_profile", ""),
                        "reporting_standard": profile.get("reporting_standard", ""),
                        "latest_filing_date": profile.get("latest_filing_date", ""),
                        "latest_form_type": profile.get("latest_form_type", ""),
                        "filing_count": filing_count,
                        "raw_fact_count": raw_count,
                        "mapped_fact_count": mapped_count,
                        "review_reason": review_reason or profile.get("review_reason", ""),
                    }
                )

            if raw_response_retention_days > 0:
                # FN-8: config-driven retention so raw_api_responses cannot grow
                # without bound under daily incremental refreshes.
                prune_cutoff = (
                    (datetime.now(timezone.utc) - timedelta(days=raw_response_retention_days)).date().isoformat()
                )
                with conn:
                    pruned = conn.execute(
                        "DELETE FROM raw_api_responses WHERE source_id IN (?, ?) AND asof_date < ?",
                        (submissions_source_id, companyfacts_source_id, prune_cutoff),
                    ).rowcount
                if pruned:
                    LOGGER.info("Pruned %d raw_api_responses rows older than %s for SEC sources.", pruned, prune_cutoff)
            write_report(
                output_csv,
                report_rows,
                preserve_existing_tickers=bool(ticker_filter),
            )
            # XC-18: failures with allow_partial disabled re-raise inside the
            # loop, so reaching this point with failures implies --allow-partial.
            status = "success_with_failures" if failures else "success"
            finish_run(
                conn,
                run_id=run_id,
                status=status,
                row_count=len(report_rows),
                message=f"rows={len(report_rows)} failures={len(failures)} output={output_csv}",
            )
            finish_ingestion_run(
                conn,
                ingestion_run_id=submissions_run_id,
                status=status,
                request_count=submissions_requests,
                row_count=sum(int(row.get("filing_count") or 0) for row in report_rows),
                message=f"tickers={len(report_rows)}",
            )
            finish_ingestion_run(
                conn,
                ingestion_run_id=companyfacts_run_id,
                status=status,
                request_count=companyfacts_requests,
                row_count=sum(int(row.get("mapped_fact_count") or 0) for row in report_rows),
                message=f"tickers={len(report_rows)}",
            )
            LOGGER.info("Wrote SEC fundamentals coverage report: %s", output_csv)
            LOGGER.info("SEC fundamentals sync complete: rows=%d failures=%d", len(report_rows), len(failures))
        except BaseException as exc:
            finish_run(
                conn, run_id=run_id, status="failed", row_count=len(report_rows), message=f"{type(exc).__name__}: {exc}"
            )
            finish_ingestion_run(
                conn,
                ingestion_run_id=submissions_run_id,
                status="failed",
                request_count=submissions_requests,
                row_count=0,
                message=f"{type(exc).__name__}: {exc}",
            )
            finish_ingestion_run(
                conn,
                ingestion_run_id=companyfacts_run_id,
                status="failed",
                request_count=companyfacts_requests,
                row_count=0,
                message=f"{type(exc).__name__}: {exc}",
            )
            raise


if __name__ == "__main__":
    main()
