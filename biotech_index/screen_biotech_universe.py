#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import math
import os
import re
import threading
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, cast
from urllib.parse import urlencode, urlparse

import pandas as pd
import requests

from google_screener import GoogleScreenerConfig, confirm_candidates


LOGGER = logging.getLogger("screen_biotech_universe")

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_MAPPING_CSV = SCRIPT_DIR.parent / "ticker_mapping" / "All_tickers_biotech_enriched.csv"
DEFAULT_TICKERS_CSV: Path | None = None
DEFAULT_CONFIG_YAML = SCRIPT_DIR / "screen_biotech_universe_config.yaml"
DEFAULT_OUTPUT_DIR = SCRIPT_DIR.parent / "output" / "preload_biotech_screen"
DEFAULT_OUTPUT_FILE = "biotech_screen_results_all.csv"
DEFAULT_CACHE_DIR = SCRIPT_DIR.parent / "output" / "preload_biotech_screen_cache"
DEFAULT_GOOGLE_CONFIRMATION_CACHE = DEFAULT_OUTPUT_DIR / "google_confirmation_cache.csv"
DEFAULT_GOOGLE_CONFIRMATION_RAW = DEFAULT_OUTPUT_DIR / "google_confirmation_raw.json"
GOOGLE_CONFIRMATION_CLASSIFICATIONS = ("reverse_split", "going_concern")
OPTIONAL_IDENTITY_COLUMNS = {
    "security_type": ("SecurityType", "security_type", "quoteType", "QuoteType"),
    "is_primary_listing": ("IsPrimaryListing", "is_primary_listing", "PrimaryListing", "primary_listing"),
    "listing_status": ("ListingStatus", "listing_status", "Status", "status"),
    "country": ("Country", "country"),
    "currency": ("Currency", "currency"),
    "manual_include": ("ManualInclude", "manual_include"),
    "manual_exclude": ("ManualExclude", "manual_exclude"),
    "manual_review": ("ManualReview", "manual_review"),
    "notes": ("Notes", "notes"),
    "identity_data_sources": ("IdentityDataSources", "identity_data_sources"),
    "missing_identity_fields": ("MissingIdentityFields", "missing_identity_fields"),
}
REQUIRED_IDENTITY_INPUT_COLUMNS = {
    "SecurityType": ("SecurityType", "security_type", "quoteType", "QuoteType"),
    "IsPrimaryListing": ("IsPrimaryListing", "is_primary_listing", "PrimaryListing", "primary_listing"),
    "ListingStatus": ("ListingStatus", "listing_status", "Status", "status"),
    "Country": ("Country", "country"),
    "Currency": ("Currency", "currency"),
    "ManualInclude": ("ManualInclude", "manual_include"),
    "ManualExclude": ("ManualExclude", "manual_exclude"),
    "ManualReview": ("ManualReview", "manual_review"),
    "Notes": ("Notes", "notes"),
    "IdentityDataSources": ("IdentityDataSources", "identity_data_sources"),
    "MissingIdentityFields": ("MissingIdentityFields", "missing_identity_fields"),
}
DEFAULT_TARGET_SECURITY_TYPES = {"Common Stock", "Ordinary Shares", "ADR/ADS"}
DEFAULT_ALLOWED_LISTING_STATUSES = {
    "active",
    "active_financial_status_D",
    "active_financial_status_E",
}

CTG_STUDIES_URL = "https://clinicaltrials.gov/api/v2/studies"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"
SEC_COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json"
SEC_ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"
DEFAULT_USER_AGENT = os.getenv("SEC_USER_AGENT", "").strip() or "JL, Independent Research, jm.357@hotmail.com"

REQUIRED_SEC_FORMS = {
    "10-K",
    "10-K/A",
    "10-Q",
    "10-Q/A",
    "10-QT",
    "10-QT/A",
    "8-K",
    "8-K/A",
    "20-F",
    "20-F/A",
    "40-F",
    "40-F/A",
    "6-K",
    "6-K/A",
}
CURRENT_REPORT_FORMS = {
    "8-K",
    "8-K/A",
    "6-K",
    "6-K/A",
}
PERIODIC_FORMS = {
    "10-K",
    "10-K/A",
    "10-Q",
    "10-Q/A",
    "10-QT",
    "10-QT/A",
    "20-F",
    "20-F/A",
    "40-F",
    "40-F/A",
}
TEXT_SCAN_FORMS = {
    "10-K",
    "10-K/A",
    "10-Q",
    "10-Q/A",
    "20-F",
    "20-F/A",
    "40-F",
    "40-F/A",
    "8-K",
    "8-K/A",
    "6-K",
    "6-K/A",
    "DEF 14A",
    "DEFA14A",
    "PRE 14A",
    "PRE14A",
    "S-1",
    "S-1/A",
    "S-3",
    "S-3/A",
    "424B1",
    "424B2",
    "424B3",
    "424B4",
    "424B5",
    "424B7",
}
SOFT_RISK_TEXT_FORMS = {
    "6-K",
    "6-K/A",
}
NT_LATE_FILING_FORMS = {
    "NT 10-K",
    "NT 10-K/A",
    "NT 10-Q",
    "NT 10-Q/A",
    "NT 10-QT",
    "NT 10-QT/A",
    "NT 20-F",
    "NT 20-F/A",
    "NT 40-F",
    "NT 40-F/A",
}

GOING_CONCERN_PATTERNS = (
    re.compile(r"substantial doubt.{0,220}continue as a going concern", re.IGNORECASE | re.DOTALL),
    re.compile(r"substantial doubt.{0,220}ability to continue.{0,80}going concern", re.IGNORECASE | re.DOTALL),
    re.compile(r"going concern.{0,220}substantial doubt", re.IGNORECASE | re.DOTALL),
)
GOING_CONCERN_SOFT_PATTERNS = (
    re.compile(r"\bnegative working capital\b", re.IGNORECASE),
    re.compile(r"will require additional capital", re.IGNORECASE),
    re.compile(r"will need additional capital", re.IGNORECASE),
    re.compile(r"existing cash(?:\s+and\s+cash\s+equivalents)? resources.{0,160}(?:will not|may not|not be) sufficient", re.IGNORECASE | re.DOTALL),
    re.compile(r"cash.{0,160}(?:will not|may not|not be) sufficient.{0,160}(?:12 months|one year|next year)", re.IGNORECASE | re.DOTALL),
)
GOING_CONCERN_ALLEVIATED_PATTERNS = (
    re.compile(r"substantial doubt.{0,180}(?:has been|was|is)\s+alleviated", re.IGNORECASE | re.DOTALL),
    re.compile(r"substantial doubt.{0,220}(?:have|has|had)\s+been\s+(?:resolved|removed)", re.IGNORECASE | re.DOTALL),
    re.compile(r"substantial doubt.{0,220}(?:conditions|events).{0,220}(?:resolved|removed)", re.IGNORECASE | re.DOTALL),
    re.compile(r"(?:conditions|events).{0,220}substantial doubt.{0,220}(?:resolved|removed)", re.IGNORECASE | re.DOTALL),
    re.compile(r"(?:alleviate|alleviated|alleviates).{0,180}substantial doubt", re.IGNORECASE | re.DOTALL),
    re.compile(r"(?:resolved|removed).{0,220}substantial doubt", re.IGNORECASE | re.DOTALL),
)
GOING_CONCERN_NOT_ALLEVIATED_PATTERNS = (
    re.compile(r"(?:do not|does not|did not|cannot|not expected to).{0,80}(?:alleviate|alleviated|alleviates).{0,180}substantial doubt", re.IGNORECASE | re.DOTALL),
    re.compile(r"(?:do not|does not|did not|cannot|not expected to).{0,80}(?:resolve|resolved|removes|removed).{0,180}substantial doubt", re.IGNORECASE | re.DOTALL),
    re.compile(r"substantial doubt.{0,180}(?:not|cannot).{0,80}(?:alleviate|alleviated|alleviates|resolve|resolved|removes|removed)", re.IGNORECASE | re.DOTALL),
)
GOING_CONCERN_CONDITIONAL_PATTERNS = (
    re.compile(r"absent.{0,120}(?:net\s+)?proceeds.{0,240}substantial doubt", re.IGNORECASE | re.DOTALL),
    re.compile(r"without.{0,120}(?:net\s+)?proceeds.{0,240}substantial doubt", re.IGNORECASE | re.DOTALL),
)
GOING_CONCERN_RESOLUTION_PATTERNS = (
    re.compile(r"\bcash runway\b.{0,160}\b(?:through|into)\s+(?:[A-Z][a-z]+\s+)?20\d{2}\b", re.IGNORECASE | re.DOTALL),
    re.compile(
        r"\bcash(?:,\s*cash equivalents)?\b.{0,220}\b(?:sufficient|adequate|expected)\b.{0,180}\b(?:fund|support)\b.{0,120}\b(?:operations|operating expenses|capital requirements)\b.{0,160}\b(?:for at least\s+(?:12 months|one year)|through|into)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\b(?:closed|completed|consummated)\b.{0,120}\b(?:public offering|private placement|registered direct|financing|offering)\b.{0,160}\b(?:gross|net)?\s*proceeds\b.{0,80}\$?\d+(?:\.\d+)?\s*(?:million|m)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\b(?:gross|net)?\s*proceeds\b.{0,100}\$?\d+(?:\.\d+)?\s*(?:million|m)\b.{0,160}\b(?:public offering|private placement|registered direct|financing|offering)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\b(?:upfront|initial)\s+(?:payment|proceeds)\b.{0,120}\$?\d+(?:\.\d+)?\s*(?:million|m)\b.{0,160}\b(?:license|licensing|collaboration|partnership)\b",
        re.IGNORECASE | re.DOTALL,
    ),
)
REVERSE_SPLIT_PATTERNS = (
    re.compile(r"\breverse stock split\b", re.IGNORECASE),
    re.compile(r"\breverse split\b", re.IGNORECASE),
)
REVERSE_SPLIT_CONFIRMED_PATTERNS = (
    re.compile(
        r"\b(reverse stock split|reverse split)\b.{0,180}\b(effective|effected|implemented|completed|became effective)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\b(effective|effected|implemented|completed|became effective)\b.{0,120}\b(reverse stock split|reverse split)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(r"\b\d+\s*[- ]for[- ]\s*\d+(?:\.\d+)?\b.{0,120}\b(effective|effected|implemented|completed|became effective)\b.{0,120}\b(reverse stock split|reverse split)\b", re.IGNORECASE | re.DOTALL),
    re.compile(r"\b(reverse stock split|reverse split)\b.{0,120}\b\d+\s*[- ]for[- ]\s*\d+(?:\.\d+)?\b.{0,120}\b(effective|effected|implemented|completed|became effective)\b", re.IGNORECASE | re.DOTALL),
)
REVERSE_SPLIT_NEGATION_PATTERNS = (
    re.compile(
        r"\b(proposal|proposed|proposing|authorize|authorized|authorization|approval|approved|stockholder approval|shareholder approval|may effect|may implement|could effect|could implement|intend to effect|intend to implement|plan to effect|plan to implement|considering)\b",
        re.IGNORECASE,
    ),
)
REVERSE_SPLIT_BOILERPLATE_PATTERNS = (
    re.compile(r"takes into account stock dividends,\s*splits and reverse splits", re.IGNORECASE),
    re.compile(r"accounting standards codification.{0,160}topic\s*260", re.IGNORECASE | re.DOTALL),
    re.compile(r"\bFASB\b.{0,180}\breverse splits\b", re.IGNORECASE | re.DOTALL),
    re.compile(r"\bus-gaap\b.{0,220}\breverse splits\b", re.IGNORECASE | re.DOTALL),
    re.compile(r"\bdisclosureRef\b.{0,220}\breverse splits\b", re.IGNORECASE | re.DOTALL),
    re.compile(r"stock split,\s*reverse stock split,\s*stock dividend,\s*combination or reclassification", re.IGNORECASE),
    re.compile(r"(?:subject to adjustment|proportionate adjustments|proportionately adjusted|adjusted to reflect).{0,260}\breverse stock split\b", re.IGNORECASE | re.DOTALL),
    re.compile(r"\breverse stock split\b.{0,260}(?:subject to adjustment|proportionate adjustments|proportionately adjusted|adjusted to reflect)", re.IGNORECASE | re.DOTALL),
    re.compile(r"(?:equity plan|incentive plan|purchase plan|warrant|award|option|rsu|restricted stock).{0,280}\breverse stock split\b", re.IGNORECASE | re.DOTALL),
    re.compile(r"\breverse stock split\b.{0,280}(?:equity plan|incentive plan|purchase plan|warrant|award|option|rsu|restricted stock)", re.IGNORECASE | re.DOTALL),
    re.compile(r"(?:if|in the event).{0,160}(?:consolidation|combination|recapitalization|reclassification|merger).{0,220}\breverse stock split\b", re.IGNORECASE | re.DOTALL),
    re.compile(r"merger,\s*consolidation,\s*recapitalization,\s*or\s*reorganization.{0,220}\breverse stock split\b", re.IGNORECASE | re.DOTALL),
    re.compile(r"\b(?:we|company|registrant)\s+held\b.{0,180}\bshares\b.{0,260}\breverse stock split\b", re.IGNORECASE | re.DOTALL),
    re.compile(r"\b[A-Z][A-Za-z0-9&., -]{2,80}\s+effected\b.{0,80}\breverse stock split\b.{0,140}\b(?:we|company|registrant)\s+held\b", re.IGNORECASE | re.DOTALL),
    re.compile(r"\bfollowing the reverse stock split\b.{0,220}\b(?:private placement|selling stockholders|conversion|beneficially own)\b", re.IGNORECASE | re.DOTALL),
)
REVERSE_SPLIT_RECAPITALIZATION_PATTERNS = (
    re.compile(r"(?:prior to|in connection with|pursuant to|immediately prior to).{0,260}(?:merger|business combination|initial public offering|ipo).{0,260}\breverse stock split\b", re.IGNORECASE | re.DOTALL),
    re.compile(r"\breverse stock split\b.{0,260}(?:prior to|in connection with|pursuant to|immediately prior to).{0,260}(?:merger|business combination|initial public offering|ipo)", re.IGNORECASE | re.DOTALL),
    re.compile(r"all share and per share amounts.{0,180}retrospectively adjusted.{0,180}\breverse stock split\b", re.IGNORECASE | re.DOTALL),
    re.compile(r"\breverse stock split\b.{0,220}all share and per share amounts.{0,180}retrospectively adjusted", re.IGNORECASE | re.DOTALL),
)
REVERSE_SPLIT_STRONG_CONFIRM_WORDS = re.compile(
    r"\b(effective|effected|implemented|completed|became effective)\b",
    re.IGNORECASE,
)
PIPELINE_PATTERNS = (
    re.compile(r"\bpipeline\b", re.IGNORECASE),
    re.compile(r"\bproduct candidate(?:s)?\b", re.IGNORECASE),
    re.compile(r"\bclinical[- ]stage\b", re.IGNORECASE),
    re.compile(r"\bphase\s*(?:[1-4][a-b]?|I{1,3}[a-b]?|IV[a-b]?)\b", re.IGNORECASE),
    re.compile(r"\bIND\b"),
    re.compile(r"\binvestigational new drug\b", re.IGNORECASE),
    re.compile(r"\bbla\b", re.IGNORECASE),
    re.compile(r"\bnda\b", re.IGNORECASE),
    re.compile(r"\bNCT\d{8}\b", re.IGNORECASE),
)
RND_KEYWORD_PATTERNS = (
    re.compile(r"research and development", re.IGNORECASE),
    re.compile(r"\bR&D\b", re.IGNORECASE),
)
BIOTECH_NAME_PATTERN = re.compile(r"\bbiotech(?:nology)?\b", re.IGNORECASE)
LIKELY_BIOTECH_NAME_PATTERN = re.compile(
    r"\b(biotech(?:nology)?|biopharma(?:ceuticals?)?|biologics?|biosciences?|therapeutics?|pharmaceuticals?|pharma|medicines?|oncology|genomics?|genetics?)\b",
    re.IGNORECASE,
)

CORPORATE_SUFFIXES = {
    "INC",
    "INCORPORATED",
    "CORP",
    "CORPORATION",
    "COMPANY",
    "CO",
    "LTD",
    "LIMITED",
    "PLC",
    "AG",
    "NV",
    "N V",
    "SA",
    "S A",
    "SPA",
    "S P A",
    "SE",
    "LP",
    "LLC",
    "HOLDINGS",
    "HOLDING",
    "GROUP",
}
LISTING_SUFFIXES = {
    "ADR",
    "ADS",
    "SPON",
    "SPONS",
    "SPONSORED",
    "SPONSORED ADR",
    "SPONSORED ADS",
    "UNSPONSORED",
    "UNSP",
    "DEPOSITARY",
    "RECEIPT",
    "RECEIPTS",
}
TRAILING_NAME_NOISE_TOKENS = {
    "CLASS",
    "CL",
    "COMMON",
    "ORD",
    "ORDINARY",
    "SHARE",
    "SHARES",
    "STOCK",
    "A",
    "B",
    "C",
    "D",
    "E",
    "F",
    "I",
    "II",
    "III",
    "IV",
    "V",
    "MASS",
    "MA",
    "HOLDIN",
    "HOLDI",
    "THERAPEUT",
    "PL",
    "PHARMACEUT",
}
GENERIC_ORG_TOKENS = set(CORPORATE_SUFFIXES) | {
    *LISTING_SUFFIXES,
    *TRAILING_NAME_NOISE_TOKENS,
    "THERAPEUTICS",
    "THERAPEUTIC",
    "PHARMACEUTICALS",
    "PHARMACEUTICAL",
    "BIOPHARMA",
    "BIOPHARMACEUTICALS",
    "BIOSCIENCES",
    "BIOSCIENCE",
    "BIOTECH",
    "BIOTECHNOLOGY",
    "MEDICINES",
    "MEDICINE",
    "LABORATORIES",
    "LABS",
    "ONCOLOGY",
    "HEALTH",
    "RESEARCH",
}

_CACHE_WRITE_LOCK = threading.Lock()
_THREAD_LOCAL = threading.local()
_THREAD_SESSIONS: set[requests.Session] = set()
_THREAD_SESSIONS_LOCK = threading.Lock()
_THREAD_IBS: set[Any] = set()
_THREAD_IBS_LOCK = threading.Lock()
_IB_CLIENT_ID_LOCK = threading.Lock()
_IB_NEXT_CLIENT_OFFSET = 0
_IB_REQUEST_LOCK = threading.Lock()


class HostThrottle:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_allowed: dict[str, float] = {}

    def wait(self, url: str, min_interval_sec: float) -> None:
        if min_interval_sec <= 0:
            return
        host = urlparse(url).netloc.lower()
        if not host:
            return
        while True:
            with self._lock:
                now = time.monotonic()
                allowed_at = self._next_allowed.get(host, 0.0)
                if now >= allowed_at:
                    self._next_allowed[host] = now + min_interval_sec
                    return
                sleep_for = allowed_at - now
            time.sleep(sleep_for)


_REQUEST_THROTTLE = HostThrottle()


@dataclass(frozen=True)
class FilingRef:
    cik10: str
    accession_nodash: str
    filing_date: date
    form: str
    primary_document: str


def load_yaml_config(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        if config_path == DEFAULT_CONFIG_YAML:
            return {}
        raise FileNotFoundError(f"Config YAML not found: {config_path}")
    try:
        import yaml  # type: ignore
    except Exception as exc:
        raise RuntimeError("PyYAML is required to load screener YAML config. Install package 'pyyaml'.") from exc
    with config_path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"Config YAML root must be a mapping: {config_path}")
    return payload


def cfg_get(config: dict[str, Any], dotted_key: str, default: Any = None) -> Any:
    cur: Any = config
    for part in dotted_key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def cfg_bool(config: dict[str, Any], dotted_key: str, default: bool) -> bool:
    raw = cfg_get(config, dotted_key, default)
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "f", "no", "n", "off"}:
        return False
    return bool(raw)


def resolve_config_path(raw: Any, *, base_dir: Path, default: Path) -> Path:
    value = default if raw is None or str(raw).strip() == "" else Path(str(raw)).expanduser()
    return value if value.is_absolute() else (base_dir / value).resolve()


def resolve_optional_config_path(raw: Any, *, base_dir: Path, default: Optional[Path]) -> Optional[Path]:
    if raw is None or str(raw).strip() == "":
        return default
    value = Path(str(raw)).expanduser()
    return value if value.is_absolute() else (base_dir / value).resolve()


def normalize_form_list(raw: Any, default: Iterable[str]) -> list[str]:
    if raw is None:
        return sorted({str(x).strip().upper() for x in default if str(x).strip()})
    if isinstance(raw, str):
        values = re.split(r"[,;\s]+", raw)
    elif isinstance(raw, Iterable):
        values = [str(x) for x in raw]
    else:
        raise ValueError(f"Form list must be a string or sequence, got {type(raw).__name__}")
    return sorted({str(x).strip().upper() for x in values if str(x).strip()})


def normalize_text_list(raw: Any, default: Iterable[str]) -> list[str]:
    if raw is None:
        return sorted({str(x).strip() for x in default if str(x).strip()})
    if isinstance(raw, str):
        values = [part.strip() for part in raw.split(",")]
    elif isinstance(raw, Iterable):
        values = [str(x).strip() for x in raw]
    else:
        raise ValueError(f"Text list must be a string or sequence, got {type(raw).__name__}")
    return sorted({value for value in values if value})


def normalize_ordered_text_list(raw: Any, default: Iterable[str]) -> list[str]:
    if raw is None:
        values = [str(x).strip() for x in default]
    elif isinstance(raw, str):
        values = [part.strip() for part in raw.split(",")]
    elif isinstance(raw, Iterable):
        values = [str(x).strip() for x in raw]
    else:
        raise ValueError(f"Text list must be a string or sequence, got {type(raw).__name__}")
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def parse_args() -> argparse.Namespace:
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_YAML)
    pre_args, remaining = pre_parser.parse_known_args()
    config_path = pre_args.config.expanduser().resolve()
    config = load_yaml_config(config_path)
    config_base_dir = config_path.parent

    mapping_csv_default = resolve_config_path(
        cfg_get(config, "paths.mapping_csv", DEFAULT_MAPPING_CSV),
        base_dir=config_base_dir,
        default=DEFAULT_MAPPING_CSV,
    )
    tickers_csv_default = resolve_optional_config_path(
        cfg_get(config, "paths.tickers_csv", DEFAULT_TICKERS_CSV),
        base_dir=config_base_dir,
        default=DEFAULT_TICKERS_CSV,
    )
    output_dir_default = resolve_config_path(
        cfg_get(config, "paths.output_dir", DEFAULT_OUTPUT_DIR),
        base_dir=config_base_dir,
        default=DEFAULT_OUTPUT_DIR,
    )
    cache_dir_default = resolve_config_path(
        cfg_get(config, "paths.cache_dir", DEFAULT_CACHE_DIR),
        base_dir=config_base_dir,
        default=DEFAULT_CACHE_DIR,
    )
    reverse_split_events_csv_default = resolve_optional_config_path(
        cfg_get(config, "risk.reverse_split_events_csv", None),
        base_dir=config_base_dir,
        default=None,
    )
    google_cache_file_default = resolve_config_path(
        cfg_get(config, "google_confirmation.cache_file", DEFAULT_GOOGLE_CONFIRMATION_CACHE),
        base_dir=config_base_dir,
        default=DEFAULT_GOOGLE_CONFIRMATION_CACHE,
    )
    google_raw_output_file_default = resolve_config_path(
        cfg_get(config, "google_confirmation.raw_output_file", DEFAULT_GOOGLE_CONFIRMATION_RAW),
        base_dir=config_base_dir,
        default=DEFAULT_GOOGLE_CONFIRMATION_RAW,
    )
    output_file_default = Path(str(cfg_get(config, "paths.output_file", DEFAULT_OUTPUT_FILE)))

    parser = argparse.ArgumentParser(
        description=(
            "Screen biotech tickers before SEC fundamentals ingestion using "
            "ClinicalTrials.gov, SEC filings, and optional liquidity checks."
        ),
        parents=[pre_parser],
    )
    parser.add_argument("--mapping-csv", type=Path, default=mapping_csv_default)
    parser.add_argument("--tickers-csv", type=Path, default=tickers_csv_default)
    parser.add_argument("--no-tickers-csv", action="store_true", default=bool(cfg_get(config, "paths.no_tickers_csv", False)))
    parser.add_argument("--output-dir", type=Path, default=output_dir_default)
    parser.add_argument("--output-file", type=Path, default=output_file_default)
    parser.add_argument("--cache-dir", type=Path, default=cache_dir_default)
    parser.add_argument("--user-agent", type=str, default=str(cfg_get(config, "sec.user_agent", DEFAULT_USER_AGENT)))
    parser.add_argument("--ctgov-studies-url", type=str, default=str(cfg_get(config, "clinicaltrials.studies_url", CTG_STUDIES_URL)))
    parser.add_argument("--sec-submissions-url", type=str, default=str(cfg_get(config, "sec.submissions_url", SEC_SUBMISSIONS_URL)))
    parser.add_argument("--sec-companyfacts-url", type=str, default=str(cfg_get(config, "sec.companyfacts_url", SEC_COMPANYFACTS_URL)))
    parser.add_argument("--sec-archives-base", type=str, default=str(cfg_get(config, "sec.archives_base", SEC_ARCHIVES_BASE)))
    parser.add_argument("--lookback-years", type=float, default=float(cfg_get(config, "screen.lookback_years", 2.0)))
    parser.add_argument("--reverse-split-lookback-years", type=float, default=float(cfg_get(config, "screen.reverse_split_lookback_years", 5.0)))
    parser.add_argument("--min-median-addv20", type=float, default=float(cfg_get(config, "screen.min_median_addv20", 1_000_000.0)))
    parser.add_argument("--disable-liquidity", action="store_true", default=bool(cfg_get(config, "screen.disable_liquidity", False)))
    parser.set_defaults(allow_missing_liquidity=bool(cfg_get(config, "screen.allow_missing_liquidity", True)))
    parser.add_argument("--allow-missing-liquidity", dest="allow_missing_liquidity", action="store_true")
    parser.add_argument("--no-allow-missing-liquidity", dest="allow_missing_liquidity", action="store_false")
    parser.add_argument("--liquidity-source", choices=("ib", "none"), default=str(cfg_get(config, "liquidity.source", "ib")).strip().lower())
    parser.add_argument("--ib-host", type=str, default=str(cfg_get(config, "liquidity.ib_host", "127.0.0.1")))
    parser.add_argument("--ib-port", type=int, default=int(cfg_get(config, "liquidity.ib_port", 7497)))
    parser.add_argument("--ib-client-id", type=int, default=int(cfg_get(config, "liquidity.ib_client_id", 91)))
    parser.add_argument("--ib-connect-timeout-sec", type=float, default=float(cfg_get(config, "liquidity.ib_connect_timeout_sec", 8.0)))
    parser.add_argument("--ib-exchange", type=str, default=str(cfg_get(config, "liquidity.ib_exchange", "SMART")))
    parser.add_argument("--ib-currency", type=str, default=str(cfg_get(config, "liquidity.ib_currency", "USD")))
    parser.add_argument("--ib-duration", type=str, default=str(cfg_get(config, "liquidity.ib_duration", "4 M")))
    parser.add_argument("--ib-bar-size", type=str, default=str(cfg_get(config, "liquidity.ib_bar_size", "1 day")))
    parser.add_argument("--ib-what-to-show", type=str, default=str(cfg_get(config, "liquidity.ib_what_to_show", "TRADES")))
    parser.add_argument("--ib-use-rth", action=argparse.BooleanOptionalAction, default=cfg_bool(config, "liquidity.ib_use_rth", True))
    parser.add_argument("--max-text-filings", type=int, default=int(cfg_get(config, "screen.max_text_filings", 8)))
    parser.add_argument("--max-biotech-diagnostic-filings", type=int, default=int(cfg_get(config, "screen.max_biotech_diagnostic_filings", 2)))
    parser.add_argument("--ctgov-max-pages", type=int, default=int(cfg_get(config, "runtime.ctgov_max_pages", 25)))
    parser.add_argument(
        "--review-on-soft-liquidity-warning",
        action=argparse.BooleanOptionalAction,
        default=bool(cfg_get(config, "screen.review_on_soft_liquidity_warning", False)),
        help=(
            "When enabled, soft financing/liquidity language such as accumulated deficit "
            "or need for additional capital forces review. Disabled by default because "
            "that language is common in development-stage biotech filings."
        ),
    )
    parser.add_argument("--disable-identity-gate", action="store_true", default=bool(cfg_get(config, "screen.disable_identity_gate", False)))
    parser.add_argument("--allow-missing-identity-fields", action="store_true", default=bool(cfg_get(config, "screen.allow_missing_identity_fields", False)))
    parser.add_argument("--reverse-split-events-csv", type=Path, default=reverse_split_events_csv_default)
    parser.add_argument(
        "--google-confirmation-enabled",
        action=argparse.BooleanOptionalAction,
        default=cfg_bool(config, "google_confirmation.enabled", False),
    )
    parser.add_argument("--google-api-key-env", type=str, default=str(cfg_get(config, "google_confirmation.api_key_env", "GEMINI_API_KEY")))
    parser.add_argument("--google-model", type=str, default=str(cfg_get(config, "google_confirmation.model", "gemini-2.5-flash-lite")))
    parser.add_argument("--google-fallback-model", type=str, default=str(cfg_get(config, "google_confirmation.fallback_model", "gemini-2.5-flash")))
    parser.add_argument("--google-batch-size", type=int, default=int(cfg_get(config, "google_confirmation.batch_size", 8)))
    parser.add_argument("--google-max-calls-per-run", type=int, default=int(cfg_get(config, "google_confirmation.max_calls_per_run", 30)))
    parser.add_argument("--google-min-seconds-between-calls", type=float, default=float(cfg_get(config, "google_confirmation.min_seconds_between_calls", 5.0)))
    parser.add_argument("--google-cache-file", type=Path, default=google_cache_file_default)
    parser.add_argument("--google-raw-output-file", type=Path, default=google_raw_output_file_default)
    parser.add_argument("--google-cache-ttl-days", type=float, default=float(cfg_get(config, "google_confirmation.cache_ttl_days", 30.0)))
    parser.add_argument("--workers", type=int, default=int(cfg_get(config, "runtime.workers", 4)))
    parser.add_argument("--sleep-sec", type=float, default=float(cfg_get(config, "runtime.sleep_sec", 0.2)))
    parser.add_argument("--json-ttl-hours", type=float, default=float(cfg_get(config, "runtime.json_ttl_hours", 24.0)))
    parser.add_argument("--text-ttl-hours", type=float, default=float(cfg_get(config, "runtime.text_ttl_hours", 168.0)))
    parser.add_argument("--timeout-sec", type=float, default=float(cfg_get(config, "runtime.timeout_sec", 45.0)))
    parser.add_argument("--max-tickers", type=int, default=int(cfg_get(config, "runtime.max_tickers", 0)))
    parser.set_defaults(
        required_sec_forms=normalize_form_list(cfg_get(config, "sec.required_sec_forms", None), REQUIRED_SEC_FORMS),
        current_report_forms=normalize_form_list(cfg_get(config, "sec.current_report_forms", None), CURRENT_REPORT_FORMS),
        periodic_forms=normalize_form_list(cfg_get(config, "sec.periodic_forms", None), PERIODIC_FORMS),
        text_scan_forms=normalize_form_list(cfg_get(config, "sec.text_scan_forms", None), TEXT_SCAN_FORMS),
        soft_risk_text_forms=normalize_form_list(cfg_get(config, "sec.soft_risk_text_forms", None), SOFT_RISK_TEXT_FORMS),
        target_security_types=normalize_text_list(cfg_get(config, "screen.target_security_types", None), DEFAULT_TARGET_SECURITY_TYPES),
        allowed_listing_statuses=normalize_text_list(cfg_get(config, "screen.allowed_listing_statuses", None), DEFAULT_ALLOWED_LISTING_STATUSES),
        require_primary_listing=bool(cfg_get(config, "screen.require_primary_listing", True)),
        manual_include_demotes_remove_to_review=bool(cfg_get(config, "screen.manual_include_demotes_remove_to_review", True)),
        google_confirmation_classifications=normalize_ordered_text_list(
            cfg_get(config, "google_confirmation.classifications", None),
            GOOGLE_CONFIRMATION_CLASSIFICATIONS,
        ),
        google_use_search_grounding=cfg_bool(config, "google_confirmation.use_search_grounding", True),
        google_min_confidence_for_confirmed=str(cfg_get(config, "google_confirmation.min_confidence_for_confirmed", "high")),
        google_require_company_name_match=cfg_bool(config, "google_confirmation.require_company_name_match", True),
        google_require_primary_source=cfg_bool(config, "google_confirmation.require_primary_source", True),
        google_rerun_missing_tickers=cfg_bool(config, "google_confirmation.rerun_missing_tickers", True),
        google_max_missing_rerun_calls=int(cfg_get(config, "google_confirmation.max_missing_rerun_calls", 3)),
        hard_remove_google_confirmed_going_concern=cfg_bool(config, "google_confirmation.hard_remove_confirmed_going_concern", False),
        hard_remove_google_confirmed_reverse_split=cfg_bool(config, "google_confirmation.hard_remove_confirmed_reverse_split", False),
        config_path=config_path,
    )
    args = parser.parse_args(remaining)
    args.config = config_path
    return args


def apply_configured_globals(args: argparse.Namespace) -> None:
    global CTG_STUDIES_URL, SEC_SUBMISSIONS_URL, SEC_COMPANYFACTS_URL, SEC_ARCHIVES_BASE
    global REQUIRED_SEC_FORMS, CURRENT_REPORT_FORMS, PERIODIC_FORMS, TEXT_SCAN_FORMS, SOFT_RISK_TEXT_FORMS

    CTG_STUDIES_URL = str(args.ctgov_studies_url)
    SEC_SUBMISSIONS_URL = str(args.sec_submissions_url)
    SEC_COMPANYFACTS_URL = str(args.sec_companyfacts_url)
    SEC_ARCHIVES_BASE = str(args.sec_archives_base).rstrip("/")
    REQUIRED_SEC_FORMS = set(normalize_form_list(args.required_sec_forms, REQUIRED_SEC_FORMS))
    CURRENT_REPORT_FORMS = set(normalize_form_list(args.current_report_forms, CURRENT_REPORT_FORMS))
    PERIODIC_FORMS = set(normalize_form_list(args.periodic_forms, PERIODIC_FORMS))
    TEXT_SCAN_FORMS = set(normalize_form_list(args.text_scan_forms, TEXT_SCAN_FORMS))
    SOFT_RISK_TEXT_FORMS = set(normalize_form_list(args.soft_risk_text_forms, SOFT_RISK_TEXT_FORMS))


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    logging.getLogger("ib_insync").setLevel(logging.WARNING)
    for handler in logging.getLogger().handlers:
        if handler.formatter is not None:
            handler.formatter.converter = time.gmtime


def normalize_ticker(raw: Any) -> str:
    return str(raw or "").strip().upper().replace(".", "-")


def normalize_cik(raw: Any) -> str:
    digits = re.sub(r"\D", "", str(raw or ""))
    return digits.zfill(10) if digits else ""


def rec_get_any(rec: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = rec.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    lowered = {str(k).lower(): v for k, v in rec.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def optional_identity_fields(rec: dict[str, Any]) -> dict[str, str]:
    return {out_col: rec_get_any(rec, *source_cols) for out_col, source_cols in OPTIONAL_IDENTITY_COLUMNS.items()}


def parse_boolish(raw: Any) -> bool:
    text = str(raw or "").strip().lower()
    return text in {"1", "true", "t", "yes", "y"}


def parse_optional_float(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def normalize_key_text(raw: Any) -> str:
    return re.sub(r"\s+", " ", str(raw or "").strip()).casefold()


def resolve_column(df: pd.DataFrame, candidates: Iterable[str]) -> Optional[str]:
    raw_by_norm = {str(col).casefold(): str(col) for col in df.columns}
    for candidate in candidates:
        found = raw_by_norm.get(str(candidate).casefold())
        if found is not None:
            return found
    return None


def validate_mapping_schema(df: pd.DataFrame, *, allow_missing_identity_fields: bool) -> None:
    missing_columns = [
        canonical
        for canonical, candidates in REQUIRED_IDENTITY_INPUT_COLUMNS.items()
        if resolve_column(df, candidates) is None
    ]
    if missing_columns:
        if allow_missing_identity_fields:
            LOGGER.warning(
                "Mapping CSV is missing identity columns but --allow-missing-identity-fields is enabled: %s",
                sorted(missing_columns),
            )
        else:
            raise ValueError(
                "Mapping CSV must be the enriched universe file; missing identity columns: "
                f"{sorted(missing_columns)}"
            )

    required_nonblank = {
        "Ticker": ("Ticker", "Tickers", "ticker", "tickers", "Symbol", "symbol"),
        "CIK": ("CIK", "cik"),
        "CompanyName": ("CompanyName", "company_name", "Company", "company", "Name", "name"),
    }
    identity_nonblank = {
        "SecurityType": REQUIRED_IDENTITY_INPUT_COLUMNS["SecurityType"],
        "IsPrimaryListing": REQUIRED_IDENTITY_INPUT_COLUMNS["IsPrimaryListing"],
        "ListingStatus": REQUIRED_IDENTITY_INPUT_COLUMNS["ListingStatus"],
        "Country": REQUIRED_IDENTITY_INPUT_COLUMNS["Country"],
        "Currency": REQUIRED_IDENTITY_INPUT_COLUMNS["Currency"],
    }
    if not allow_missing_identity_fields:
        required_nonblank.update(identity_nonblank)
    missing_value_counts: dict[str, int] = {}
    for canonical, candidates in required_nonblank.items():
        col = resolve_column(df, candidates)
        if col is None:
            missing_value_counts[canonical] = len(df)
            continue
        count = int((df[col].fillna("").astype(str).str.strip() == "").sum())
        if count:
            missing_value_counts[canonical] = count
    if missing_value_counts:
        raise ValueError(f"Mapping CSV has missing required identity values: {missing_value_counts}")

    missing_identity_col = resolve_column(df, REQUIRED_IDENTITY_INPUT_COLUMNS["MissingIdentityFields"])
    if missing_identity_col is not None:
        missing_identity_count = int((df[missing_identity_col].fillna("").astype(str).str.strip() != "").sum())
        if missing_identity_count and not allow_missing_identity_fields:
            raise ValueError(
                "Mapping CSV still has rows with MissingIdentityFields populated. "
                f"Rows={missing_identity_count}; rerun enrichment or pass --allow-missing-identity-fields."
            )


def log_mapping_quality(df: pd.DataFrame) -> None:
    for label, candidates in (
        ("SecurityType", REQUIRED_IDENTITY_INPUT_COLUMNS["SecurityType"]),
        ("ListingStatus", REQUIRED_IDENTITY_INPUT_COLUMNS["ListingStatus"]),
        ("Country", REQUIRED_IDENTITY_INPUT_COLUMNS["Country"]),
        ("Currency", REQUIRED_IDENTITY_INPUT_COLUMNS["Currency"]),
    ):
        col = resolve_column(df, candidates)
        if col is None:
            continue
        counts = df[col].fillna("").astype(str).str.strip().replace("", "<blank>").value_counts().to_dict()
        LOGGER.info("%s distribution: %s", label, counts)
    for label, candidates in (
        ("ManualInclude", REQUIRED_IDENTITY_INPUT_COLUMNS["ManualInclude"]),
        ("ManualExclude", REQUIRED_IDENTITY_INPUT_COLUMNS["ManualExclude"]),
        ("ManualReview", REQUIRED_IDENTITY_INPUT_COLUMNS["ManualReview"]),
    ):
        col = resolve_column(df, candidates)
        if col is None:
            continue
        true_count = int(df[col].map(parse_boolish).sum())
        LOGGER.info("%s=true rows: %d", label, true_count)


def parse_iso_date(raw: Any) -> Optional[date]:
    text = str(raw or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y%m%d"):
        try:
            return datetime.strptime(text[:10] if fmt == "%Y-%m-%d" else text, fmt).date()
        except ValueError:
            continue
    return None


def load_reverse_split_events(path: Optional[Path]) -> dict[str, list[date]]:
    if path is None:
        return {}
    path = path.expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"Reverse-split events CSV not found: {path}")
    try:
        df = pd.read_csv(path, dtype=str).fillna("")
    except pd.errors.EmptyDataError:
        LOGGER.warning("Reverse-split events CSV is empty: %s", path)
        return {}
    events: dict[str, list[date]] = {}
    for raw_rec in df.to_dict("records"):
        rec = {str(k): v for k, v in raw_rec.items()}
        ticker = normalize_ticker(
            rec_get_any(
                rec,
                "Ticker",
                "Symbol",
                "Issue Symbol",
                "IssueSymbol",
                "Current Symbol",
                "CurrentSymbol",
            )
        )
        event_date = parse_iso_date(
            rec_get_any(
                rec,
                "Effective Date",
                "EffectiveDate",
                "Ex Date",
                "ExDate",
                "Event Date",
                "EventDate",
                "Date",
            )
        )
        description = " ".join(
            rec_get_any(rec, key)
            for key in (
                "Action",
                "Event",
                "Event Type",
                "EventType",
                "Description",
                "Comments",
                "Notes",
                "Corporate Action",
                "CorporateAction",
            )
        )
        if not ticker or event_date is None:
            continue
        if not re.search(r"\breverse\b", description, re.IGNORECASE) or not re.search(r"\bsplit\b", description, re.IGNORECASE):
            continue
        events.setdefault(ticker, []).append(event_date)
    LOGGER.info("Loaded reverse-split event dates for %d ticker(s) from %s", len(events), path)
    return events


def reverse_split_event_counts(events_by_ticker: dict[str, list[date]], ticker: str, recent_cutoff: date, reverse_cutoff: date) -> tuple[int, int]:
    dates = events_by_ticker.get(normalize_ticker(ticker), [])
    hits_5y = sum(1 for event_date in dates if event_date >= reverse_cutoff)
    hits_2y = sum(1 for event_date in dates if event_date >= recent_cutoff)
    return hits_2y, hits_5y


def clean_name_for_query(raw: str) -> str:
    text = str(raw or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text


def normalize_org_name(raw: str) -> str:
    text = str(raw or "").upper()
    text = text.replace("&", " AND ")
    text = re.sub(r"[^A-Z0-9 ]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def strip_corporate_suffixes(norm_name: str) -> str:
    tokens = [tok for tok in str(norm_name or "").split() if tok]
    while len(tokens) > 1 and (
        tokens[-1] in CORPORATE_SUFFIXES
        or tokens[-1] in LISTING_SUFFIXES
        or tokens[-1] in TRAILING_NAME_NOISE_TOKENS
    ):
        tokens.pop()
    return " ".join(tokens).strip()


def meaningful_org_tokens(norm_name: str) -> list[str]:
    stripped = strip_corporate_suffixes(str(norm_name or "").strip())
    return [
        tok
        for tok in stripped.split()
        if tok and tok not in GENERIC_ORG_TOKENS and len(tok) >= 3
    ]


def alias_spelling_variants(raw_alias: str) -> list[str]:
    norm_alias = normalize_org_name(raw_alias)
    tokens = [tok for tok in norm_alias.split() if tok]
    variants: list[str] = []
    if "PHARMA" in tokens:
        expanded = ["PHARMACEUTICALS" if tok == "PHARMA" else tok for tok in tokens]
        variants.append(" ".join(expanded).title())
    if "PHARMACEUTICALS" in tokens:
        shortened = ["PHARMA" if tok == "PHARMACEUTICALS" else tok for tok in tokens]
        variants.append(" ".join(shortened).title())
    return [variant for variant in variants if variant and normalize_org_name(variant) != norm_alias]


def build_company_aliases(company_name: str) -> tuple[list[str], set[str]]:
    norm_company = normalize_org_name(company_name)
    norm_stripped = strip_corporate_suffixes(norm_company)
    core_tokens = meaningful_org_tokens(norm_stripped)
    core_alias = " ".join(core_tokens).title() if core_tokens and (len(core_tokens) >= 2 or (len(core_tokens) == 1 and len(core_tokens[0]) >= 5)) else ""

    raw_aliases: list[str] = []
    for candidate in (
        clean_name_for_query(company_name),
        clean_name_for_query(norm_stripped.title()),
        clean_name_for_query(core_alias),
    ):
        if candidate and candidate not in raw_aliases:
            raw_aliases.append(candidate)
    for candidate in list(raw_aliases):
        for variant in alias_spelling_variants(candidate):
            variant = clean_name_for_query(variant)
            if variant and variant not in raw_aliases:
                raw_aliases.append(variant)
    raw_aliases = sorted(raw_aliases, key=lambda x: (-len(x), x))
    norm_aliases: set[str] = set()
    for candidate in raw_aliases:
        norm_full = normalize_org_name(candidate)
        norm_stripped_alias = strip_corporate_suffixes(norm_full)
        if norm_full:
            norm_aliases.add(norm_full)
        if norm_stripped_alias:
            norm_aliases.add(norm_stripped_alias)
    return raw_aliases, norm_aliases


def alias_token_sets(norm_aliases: set[str]) -> list[set[str]]:
    seen: set[tuple[str, ...]] = set()
    out: list[set[str]] = []
    for alias in norm_aliases:
        toks = tuple(sorted(set(meaningful_org_tokens(alias))))
        if not toks or toks in seen:
            continue
        seen.add(toks)
        out.append(set(toks))
    out.sort(key=lambda s: (-len(s), sorted(s)))
    return out


def names_match(raw_name: str, norm_aliases: set[str], alias_tokens: Optional[list[set[str]]] = None) -> bool:
    norm_full = normalize_org_name(raw_name)
    norm_stripped = strip_corporate_suffixes(norm_full)
    if norm_full and (norm_full in norm_aliases or norm_stripped in norm_aliases):
        return True

    raw_tokens = set(meaningful_org_tokens(norm_full))
    if not raw_tokens:
        return False

    for toks in alias_tokens if alias_tokens is not None else alias_token_sets(norm_aliases):
        if len(toks) >= 2 and toks.issubset(raw_tokens):
            return True
        if len(toks) == 1:
            tok = next(iter(toks))
            if len(tok) >= 5 and tok in raw_tokens:
                return True
    return False


def _cache_path(cache_dir: Path, namespace: str, url: str, params: Optional[dict[str, Any]]) -> Path:
    key = url
    if params:
        key += "?" + urlencode(sorted((str(k), str(v)) for k, v in params.items()))
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return cache_dir / namespace / digest[:2] / f"{digest}.cache"


def _cache_is_fresh(path: Path, ttl_hours: float) -> bool:
    try:
        if not path.exists():
            return False
        if ttl_hours < 0:
            return True
        age_seconds = time.time() - path.stat().st_mtime
    except OSError:
        return False
    return age_seconds <= ttl_hours * 3600.0


def write_cache_text(path: Path, text: str) -> None:
    tmp_path: Optional[Path] = None
    with _CACHE_WRITE_LOCK:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                delete=False,
                suffix=".tmp",
            ) as tmp:
                tmp.write(text)
                tmp_path = Path(tmp.name)
            tmp_path.replace(path)
        except OSError as exc:
            LOGGER.warning("Cache write failed %s: %s", path, exc)
            if tmp_path is not None:
                try:
                    tmp_path.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    pass


def get_thread_session() -> requests.Session:
    session = getattr(_THREAD_LOCAL, "session", None)
    if not isinstance(session, requests.Session):
        session = requests.Session()
        _THREAD_LOCAL.session = session
        with _THREAD_SESSIONS_LOCK:
            _THREAD_SESSIONS.add(session)
    return session


def ensure_thread_event_loop() -> None:
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


def _next_ib_client_id(base_client_id: int) -> int:
    global _IB_NEXT_CLIENT_OFFSET
    with _IB_CLIENT_ID_LOCK:
        client_id = int(base_client_id) + _IB_NEXT_CLIENT_OFFSET
        _IB_NEXT_CLIENT_OFFSET += 1
    return client_id


def get_thread_ib(args: argparse.Namespace) -> Any:
    ensure_thread_event_loop()
    try:
        from ib_insync import IB  # type: ignore
    except Exception as exc:
        raise RuntimeError("ib_insync is required for IB liquidity checks") from exc

    ib = getattr(_THREAD_LOCAL, "ib", None)
    if ib is not None:
        try:
            if ib.isConnected():
                return ib
        except Exception:
            pass

    ib = IB()
    client_id = _next_ib_client_id(int(args.ib_client_id))
    ib.connect(
        str(args.ib_host),
        int(args.ib_port),
        clientId=client_id,
        timeout=float(args.ib_connect_timeout_sec),
    )
    _THREAD_LOCAL.ib = ib
    with _THREAD_IBS_LOCK:
        _THREAD_IBS.add(ib)
    return ib


def close_thread_sessions() -> None:
    with _THREAD_SESSIONS_LOCK:
        sessions = list(_THREAD_SESSIONS)
        _THREAD_SESSIONS.clear()
    for session in sessions:
        try:
            session.close()
        except Exception as exc:
            LOGGER.debug("Ignoring session close error: %s", exc)
    if isinstance(getattr(_THREAD_LOCAL, "session", None), requests.Session):
        _THREAD_LOCAL.session = None

    with _THREAD_IBS_LOCK:
        ibs = list(_THREAD_IBS)
        _THREAD_IBS.clear()
    for ib in ibs:
        try:
            if ib.isConnected():
                ib.disconnect()
        except Exception as exc:
            LOGGER.debug("Ignoring IB disconnect error: %s", exc)
    _THREAD_LOCAL.ib = None


def fetch_json_cached(
    *,
    session: requests.Session,
    cache_dir: Path,
    namespace: str,
    url: str,
    params: Optional[dict[str, Any]],
    headers: dict[str, str],
    ttl_hours: float,
    timeout_sec: float,
    sleep_sec: float,
) -> Any:
    path = _cache_path(cache_dir, namespace, url, params)
    if _cache_is_fresh(path, ttl_hours):
        try:
            cached_text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            cached_text = None
        except OSError as exc:
            LOGGER.warning("Cache read failed %s: %s", path, exc)
            cached_text = None
        if cached_text is not None:
            try:
                return json.loads(cached_text)
            except json.JSONDecodeError:
                LOGGER.warning("Ignoring invalid JSON cache file: %s", path)
                with _CACHE_WRITE_LOCK:
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
                    except OSError as exc:
                        LOGGER.warning("Invalid JSON cache unlink failed %s: %s", path, exc)
    _REQUEST_THROTTLE.wait(url, sleep_sec)
    resp = session.get(url, params=params, headers=headers, timeout=timeout_sec)
    resp.raise_for_status()
    text = resp.text
    parsed = json.loads(text)
    write_cache_text(path, text)
    return parsed


def fetch_text_cached(
    *,
    session: requests.Session,
    cache_dir: Path,
    namespace: str,
    url: str,
    headers: dict[str, str],
    ttl_hours: float,
    timeout_sec: float,
    sleep_sec: float,
) -> str:
    path = _cache_path(cache_dir, namespace, url, None)
    if _cache_is_fresh(path, ttl_hours):
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            pass
        except OSError as exc:
            LOGGER.warning("Cache read failed %s: %s", path, exc)
    _REQUEST_THROTTLE.wait(url, sleep_sec)
    resp = session.get(url, headers=headers, timeout=timeout_sec)
    resp.raise_for_status()
    text = resp.text
    if not text.strip():
        raise ValueError(f"Empty response body from {url}")
    write_cache_text(path, text)
    return text


def _recent_filings_from_submissions(payload: Any, fallback_cik10: str) -> list[FilingRef]:
    filings = payload.get("filings", {}) if isinstance(payload, dict) else {}
    recent = filings.get("recent", {}) if isinstance(filings, dict) else {}
    forms = recent.get("form", []) or []
    accessions = recent.get("accessionNumber", []) or []
    filing_dates = recent.get("filingDate", []) or []
    primary_docs = recent.get("primaryDocument", []) or []
    cik10 = normalize_cik(payload.get("cik")) or fallback_cik10
    out: list[FilingRef] = []
    n = min(len(forms), len(accessions), len(filing_dates), len(primary_docs))
    for i in range(n):
        filing_dt = parse_iso_date(filing_dates[i])
        accession_nodash = str(accessions[i] or "").replace("-", "").strip()
        form = str(forms[i] or "").strip().upper()
        primary_doc = str(primary_docs[i] or "").strip()
        if not cik10 or not accession_nodash or filing_dt is None or not form:
            continue
        out.append(
            FilingRef(
                cik10=cik10,
                accession_nodash=accession_nodash,
                filing_date=filing_dt,
                form=form,
                primary_document=primary_doc,
            )
        )
    return out


def _submissions_page_urls(payload: Any) -> list[str]:
    filings = payload.get("filings", {}) if isinstance(payload, dict) else {}
    files = filings.get("files", []) if isinstance(filings, dict) else []
    out: list[str] = []
    if not isinstance(files, list):
        return out
    for item in files:
        if not isinstance(item, dict):
            continue
        raw = str(item.get("filingHref") or item.get("name") or "").strip()
        if not raw:
            continue
        if raw.startswith("http://") or raw.startswith("https://"):
            out.append(raw)
        elif raw.startswith("/"):
            out.append(f"https://data.sec.gov{raw}")
        elif raw.lower().startswith("submissions/"):
            out.append(f"https://data.sec.gov/{raw.lower()}")
        else:
            out.append(f"https://data.sec.gov/submissions/{raw}")
    return list(dict.fromkeys(out))


def load_all_submissions(
    *,
    session: requests.Session,
    cik10: str,
    cache_dir: Path,
    user_agent: str,
    timeout_sec: float,
    json_ttl_hours: float,
    sleep_sec: float,
) -> list[FilingRef]:
    headers = {"User-Agent": user_agent, "Accept": "application/json,text/plain,*/*"}
    url = SEC_SUBMISSIONS_URL.format(cik10=cik10)
    root_payload = fetch_json_cached(
        session=session,
        cache_dir=cache_dir,
        namespace="sec_submissions",
        url=url,
        params=None,
        headers=headers,
        ttl_hours=json_ttl_hours,
        timeout_sec=timeout_sec,
        sleep_sec=sleep_sec,
    )
    filings = _recent_filings_from_submissions(root_payload, cik10)
    for page_url in _submissions_page_urls(root_payload):
        try:
            payload = fetch_json_cached(
                session=session,
                cache_dir=cache_dir,
                namespace="sec_submissions_pages",
                url=page_url,
                params=None,
                headers=headers,
                ttl_hours=json_ttl_hours,
                timeout_sec=timeout_sec,
                sleep_sec=sleep_sec,
            )
        except Exception as exc:
            LOGGER.warning("SEC submissions page failed: cik=%s url=%s err=%s", cik10, page_url, exc)
            continue
        filings.extend(_recent_filings_from_submissions(payload, cik10))
    filings = sorted(filings, key=lambda x: (x.filing_date, x.accession_nodash), reverse=True)
    deduped: list[FilingRef] = []
    seen: set[str] = set()
    for filing in filings:
        if filing.accession_nodash in seen:
            continue
        seen.add(filing.accession_nodash)
        deduped.append(filing)
    return deduped


def build_filing_urls(filing: FilingRef) -> list[str]:
    cik_int = str(int(filing.cik10))
    base = f"{SEC_ARCHIVES_BASE}/{cik_int}/{filing.accession_nodash}"
    acc = filing.accession_nodash
    if len(acc) == 18 and acc.isdigit():
        acc_dashed = f"{acc[:10]}-{acc[10:12]}-{acc[12:]}"
    else:
        acc_dashed = acc
    urls = [f"{base}/{acc_dashed}.txt"]
    if filing.primary_document:
        urls.append(f"{base}/{filing.primary_document}")
    return list(dict.fromkeys(urls))


def fetch_first_filing_text(
    *,
    session: requests.Session,
    filing: FilingRef,
    cache_dir: Path,
    user_agent: str,
    timeout_sec: float,
    text_ttl_hours: float,
    sleep_sec: float,
) -> str:
    headers = {"User-Agent": user_agent, "Accept": "text/plain,text/html,*/*"}
    last_error: Optional[Exception] = None
    for url in build_filing_urls(filing):
        try:
            return fetch_text_cached(
                session=session,
                cache_dir=cache_dir,
                namespace="sec_filing_text",
                url=url,
                headers=headers,
                ttl_hours=text_ttl_hours,
                timeout_sec=timeout_sec,
                sleep_sec=sleep_sec,
            )
        except Exception as exc:
            last_error = exc
            continue
    if last_error is not None:
        raise last_error
    raise RuntimeError("No filing URLs available")


def fetch_companyfacts(
    *,
    session: requests.Session,
    cik10: str,
    cache_dir: Path,
    user_agent: str,
    timeout_sec: float,
    json_ttl_hours: float,
    sleep_sec: float,
) -> Any:
    headers = {"User-Agent": user_agent, "Accept": "application/json,text/plain,*/*"}
    url = SEC_COMPANYFACTS_URL.format(cik10=cik10)
    return fetch_json_cached(
        session=session,
        cache_dir=cache_dir,
        namespace="sec_companyfacts",
        url=url,
        params=None,
        headers=headers,
        ttl_hours=json_ttl_hours,
        timeout_sec=timeout_sec,
        sleep_sec=sleep_sec,
    )


def companyfacts_has_recent_rnd(payload: Any, cutoff: date) -> tuple[bool, int]:
    facts = payload.get("facts", {}) if isinstance(payload, dict) else {}
    hit_count = 0
    for _, taxonomy in facts.items():
        if not isinstance(taxonomy, dict):
            continue
        for tag_name, tag_block in taxonomy.items():
            if "researchanddevelopment" not in str(tag_name).lower():
                continue
            units = tag_block.get("units", {}) if isinstance(tag_block, dict) else {}
            if not isinstance(units, dict):
                continue
            for observations in units.values():
                if not isinstance(observations, list):
                    continue
                for obs in observations:
                    if not isinstance(obs, dict):
                        continue
                    filed = parse_iso_date(obs.get("filed"))
                    if filed is None or filed < cutoff:
                        continue
                    form = str(obs.get("form") or "").upper()
                    val = obs.get("val")
                    if not form or form not in PERIODIC_FORMS:
                        continue
                    if val is None:
                        continue
                    try:
                        num = float(cast(Any, val))
                    except Exception:
                        num = math.nan
                    if math.isfinite(num) and abs(num) > 0:
                        hit_count += 1
    return hit_count > 0, hit_count


def _get_nested(obj: Any, path: Iterable[str], default: Any = None) -> Any:
    cur = obj
    for part in path:
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def filing_reverse_split_status(text: str) -> str:
    if not text or not any(p.search(text) for p in REVERSE_SPLIT_PATTERNS):
        return ""
    normalized = re.sub(r"\s+", " ", text)
    for pat in REVERSE_SPLIT_CONFIRMED_PATTERNS:
        for match in pat.finditer(normalized):
            lo = max(0, match.start() - 450)
            hi = min(len(normalized), match.end() + 450)
            window = normalized[lo:hi]
            if any(boilerplate.search(window) for boilerplate in REVERSE_SPLIT_BOILERPLATE_PATTERNS):
                continue
            if any(neg.search(window) for neg in REVERSE_SPLIT_NEGATION_PATTERNS) and not REVERSE_SPLIT_STRONG_CONFIRM_WORDS.search(window):
                continue
            if any(recap.search(window) for recap in REVERSE_SPLIT_RECAPITALIZATION_PATTERNS):
                return "soft"
            return "confirmed"
    return ""


def filing_has_confirmed_reverse_split(text: str) -> bool:
    return filing_reverse_split_status(text) == "confirmed"


def filing_going_concern_status(text: str) -> str:
    """Return hard, resolved, soft, or empty going-concern evidence from filing text."""
    if not text:
        return ""
    normalized = re.sub(r"\s+", " ", text)
    resolved_found = False
    for pat in GOING_CONCERN_PATTERNS:
        for match in pat.finditer(normalized):
            lo = max(0, match.start() - 450)
            hi = min(len(normalized), match.end() + 450)
            window = normalized[lo:hi]
            alleviated = any(p.search(window) for p in GOING_CONCERN_ALLEVIATED_PATTERNS)
            not_alleviated = any(p.search(window) for p in GOING_CONCERN_NOT_ALLEVIATED_PATTERNS)
            if alleviated and not not_alleviated:
                resolved_found = True
                continue
            if any(p.search(window) for p in GOING_CONCERN_CONDITIONAL_PATTERNS):
                return "soft"
            return "hard"
    if resolved_found:
        return "resolved"
    if any(p.search(normalized) for p in GOING_CONCERN_SOFT_PATTERNS):
        return "soft"
    return ""


def filing_has_going_concern_resolution(text: str) -> bool:
    if not text:
        return False
    normalized = re.sub(r"\s+", " ", text)
    return any(p.search(normalized) for p in GOING_CONCERN_RESOLUTION_PATTERNS)


def query_ctgov_matches(
    *,
    session: requests.Session,
    company_name: str,
    cache_dir: Path,
    timeout_sec: float,
    json_ttl_hours: float,
    sleep_sec: float,
    max_pages: int,
) -> dict[str, Any]:
    raw_aliases, norm_aliases = build_company_aliases(company_name)
    norm_alias_tokens = alias_token_sets(norm_aliases)
    matched: dict[str, dict[str, Any]] = {}
    headers = {"Accept": "application/json"}
    search_aliases: list[str] = []
    for alias in raw_aliases:
        alias = alias.strip()
        if len(alias) < 4:
            continue
        search_aliases.append(alias)
        next_page_token: Optional[str] = None
        page_guard = 0
        while True:
            params: dict[str, Any] = {"query.spons": alias, "pageSize": 100}
            if next_page_token:
                params["pageToken"] = next_page_token
            payload = fetch_json_cached(
                session=session,
                cache_dir=cache_dir,
                namespace="ctgov",
                url=CTG_STUDIES_URL,
                params=params,
                headers=headers,
                ttl_hours=json_ttl_hours,
                timeout_sec=timeout_sec,
                sleep_sec=sleep_sec,
            )
            studies = payload.get("studies", []) if isinstance(payload, dict) else []
            if not isinstance(studies, list):
                studies = []
            for study in studies:
                study_type = _get_nested(study, ["protocolSection", "designModule", "studyType"], "")
                if str(study_type).upper() != "INTERVENTIONAL":
                    continue
                nct_id = _get_nested(study, ["protocolSection", "identificationModule", "nctId"], "")
                if not nct_id:
                    continue
                lead_name = _get_nested(
                    study,
                    ["protocolSection", "sponsorCollaboratorsModule", "leadSponsor", "name"],
                    "",
                )
                collaborators = _get_nested(
                    study,
                    ["protocolSection", "sponsorCollaboratorsModule", "collaborators"],
                    [],
                )
                collaborator_names = []
                if isinstance(collaborators, list):
                    for item in collaborators:
                        if isinstance(item, dict) and item.get("name"):
                            collaborator_names.append(str(item["name"]))
                lead_match = names_match(str(lead_name), norm_aliases, norm_alias_tokens)
                collab_match = any(names_match(name, norm_aliases, norm_alias_tokens) for name in collaborator_names)
                if not lead_match and not collab_match:
                    continue
                role = "both" if lead_match and collab_match else "lead" if lead_match else "collaborator"
                matched[nct_id] = {
                    "nct_id": nct_id,
                    "brief_title": _get_nested(study, ["protocolSection", "identificationModule", "briefTitle"], ""),
                    "overall_status": _get_nested(study, ["protocolSection", "statusModule", "overallStatus"], ""),
                    "last_update_post_date": _get_nested(
                        study,
                        ["protocolSection", "statusModule", "lastUpdatePostDateStruct", "date"],
                        "",
                    ),
                    "phases": _get_nested(study, ["protocolSection", "designModule", "phases"], []),
                    "lead_sponsor": lead_name,
                    "collaborators": collaborator_names,
                    "match_role": role,
                }
            next_page_token = payload.get("nextPageToken") if isinstance(payload, dict) else None
            page_guard += 1
            if not next_page_token or (max_pages > 0 and page_guard >= max_pages):
                break
    trials = sorted(matched.values(), key=lambda x: (str(x.get("last_update_post_date") or ""), str(x.get("nct_id") or "")), reverse=True)
    lead_count = sum(1 for row in trials if row["match_role"] in {"lead", "both"})
    collab_count = sum(1 for row in trials if row["match_role"] == "collaborator")
    both_count = sum(1 for row in trials if row["match_role"] == "both")
    active_statuses = {
        "RECRUITING",
        "ENROLLING_BY_INVITATION",
        "ACTIVE_NOT_RECRUITING",
        "NOT_YET_RECRUITING",
    }
    active_count = sum(1 for row in trials if str(row.get("overall_status") or "").upper() in active_statuses)
    return {
        "has_match": bool(trials),
        "interventional_match_count": len(trials),
        "lead_sponsor_match_count": lead_count,
        "collaborator_match_count": collab_count,
        "both_role_match_count": both_count,
        "active_interventional_match_count": active_count,
        "matched_trials": trials,
        "search_aliases": search_aliases,
    }


def scan_filing_texts(
    *,
    session: requests.Session,
    filings: list[FilingRef],
    cache_dir: Path,
    user_agent: str,
    timeout_sec: float,
    text_ttl_hours: float,
    sleep_sec: float,
    recent_cutoff: date,
    reverse_cutoff: date,
    max_text_filings: int,
) -> dict[str, Any]:
    reverse_hits_2y = 0
    reverse_hits_5y = 0
    reverse_soft_hits_2y = 0
    reverse_soft_hits_5y = 0
    going_hits_2y = 0
    going_periodic_hits_2y = 0
    going_soft_hits_2y = 0
    going_resolved_hits_2y = 0
    going_resolution_hits_2y = 0
    pipeline_hits_2y = 0
    rnd_keyword_hits_2y = 0
    latest_periodic_gc = False
    latest_periodic_gc_status = ""
    latest_periodic_seen = False
    latest_periodic_date: Optional[date] = None
    going_resolution_dates: list[date] = []
    scanned_accessions: list[str] = []
    scan_errors: list[str] = []
    scans = 0
    for filing in filings:
        if filing.form not in TEXT_SCAN_FORMS:
            continue
        if filing.filing_date < reverse_cutoff:
            continue
        if scans >= max_text_filings:
            break
        try:
            text = fetch_first_filing_text(
                session=session,
                filing=filing,
                cache_dir=cache_dir,
                user_agent=user_agent,
                timeout_sec=timeout_sec,
                text_ttl_hours=text_ttl_hours,
                sleep_sec=sleep_sec,
            )
        except Exception as exc:
            scan_errors.append(f"{filing.accession_nodash}:{type(exc).__name__}")
            continue
        scans += 1
        scanned_accessions.append(filing.accession_nodash)
        reverse_status = filing_reverse_split_status(text)
        has_reverse = reverse_status == "confirmed"
        has_soft_reverse = reverse_status == "soft"
        going_status = filing_going_concern_status(text)
        has_going = going_status == "hard"
        has_soft_going = going_status == "soft"
        has_resolved_going = going_status == "resolved"
        has_resolution_evidence = filing_has_going_concern_resolution(text) and not has_going
        has_pipeline = any(p.search(text) for p in PIPELINE_PATTERNS)
        has_rnd_keywords = any(p.search(text) for p in RND_KEYWORD_PATTERNS)

        if has_reverse or has_soft_reverse:
            if has_soft_reverse or filing.form in SOFT_RISK_TEXT_FORMS:
                reverse_soft_hits_5y += 1
                if filing.filing_date >= recent_cutoff:
                    reverse_soft_hits_2y += 1
            else:
                reverse_hits_5y += 1
                if filing.filing_date >= recent_cutoff:
                    reverse_hits_2y += 1
        if filing.filing_date >= recent_cutoff:
            if has_going:
                going_hits_2y += 1
            elif has_soft_going:
                going_soft_hits_2y += 1
            elif has_resolved_going:
                going_resolved_hits_2y += 1
            if has_resolution_evidence:
                going_resolution_hits_2y += 1
                going_resolution_dates.append(filing.filing_date)
            if has_pipeline:
                pipeline_hits_2y += 1
            if has_rnd_keywords:
                rnd_keyword_hits_2y += 1

        if filing.form in PERIODIC_FORMS and filing.filing_date >= recent_cutoff:
            if has_going:
                going_periodic_hits_2y += 1
            if latest_periodic_date is None or filing.filing_date > latest_periodic_date:
                latest_periodic_date = filing.filing_date
                latest_periodic_seen = True
                latest_periodic_gc = has_going
                latest_periodic_gc_status = going_status or "none"
    resolution_after_latest_periodic = (
        latest_periodic_date is not None
        and any(resolution_date >= latest_periodic_date for resolution_date in going_resolution_dates)
    )
    if latest_periodic_gc and not resolution_after_latest_periodic:
        going_concern_status = "confirmed"
    elif latest_periodic_gc_status == "resolved" or going_resolved_hits_2y > 0 or resolution_after_latest_periodic:
        going_concern_status = "resolved"
    elif going_hits_2y > 0 or going_periodic_hits_2y > 0 or going_soft_hits_2y > 0:
        going_concern_status = "possible"
    else:
        going_concern_status = "none"
    return {
        "reverse_split_hits_2y": reverse_hits_2y,
        "reverse_split_hits_5y": reverse_hits_5y,
        "reverse_split_soft_hits_2y": reverse_soft_hits_2y,
        "reverse_split_soft_hits_5y": reverse_soft_hits_5y,
        "going_concern_hits_2y": going_hits_2y,
        "going_concern_periodic_hits_2y": going_periodic_hits_2y,
        "going_concern_soft_hits_2y": going_soft_hits_2y,
        "going_concern_resolved_hits_2y": going_resolved_hits_2y,
        "going_concern_resolution_hits_2y": going_resolution_hits_2y,
        "going_concern_resolution_after_latest_periodic": resolution_after_latest_periodic,
        "going_concern_status": going_concern_status,
        "latest_periodic_going_concern": latest_periodic_gc if latest_periodic_seen else False,
        "latest_periodic_going_concern_status": latest_periodic_gc_status if latest_periodic_seen else "",
        "pipeline_hits_2y": pipeline_hits_2y,
        "rnd_keyword_hits_2y": rnd_keyword_hits_2y,
        "text_scan_errors": scan_errors,
        "text_scanned_accessions": scanned_accessions,
    }


def _ib_symbol_candidates(ticker: str) -> list[str]:
    normalized = normalize_ticker(ticker)
    candidates = [normalized]
    if "-" in normalized:
        candidates.append(normalized.replace("-", " "))
        candidates.append(normalized.replace("-", "."))
    return list(dict.fromkeys([x for x in candidates if x]))


def fetch_liquidity_ib(ticker: str, args: argparse.Namespace) -> tuple[Optional[float], str]:
    ensure_thread_event_loop()
    try:
        from ib_insync import Stock  # type: ignore
    except Exception:
        return None, "ib_insync_unavailable"

    try:
        ib = get_thread_ib(args)
    except Exception as exc:
        return None, f"ib_connect_error:{type(exc).__name__}"

    last_error = ""
    for symbol in _ib_symbol_candidates(ticker):
        try:
            contract = Stock(symbol, str(args.ib_exchange), str(args.ib_currency))
            with _IB_REQUEST_LOCK:
                qualified = ib.qualifyContracts(contract)
                if not qualified:
                    last_error = "ib_qualify_empty"
                    continue
                bars = ib.reqHistoricalData(
                    qualified[0],
                    endDateTime="",
                    durationStr=str(args.ib_duration),
                    barSizeSetting=str(args.ib_bar_size),
                    whatToShow=str(args.ib_what_to_show),
                    useRTH=bool(args.ib_use_rth),
                    formatDate=1,
                    keepUpToDate=False,
                )
            if not bars:
                last_error = "ib_no_bars"
                continue
            frame = pd.DataFrame(
                [{"close": getattr(bar, "close", None), "volume": getattr(bar, "volume", None)} for bar in bars]
            )
            if frame.empty or "close" not in frame.columns or "volume" not in frame.columns:
                last_error = "ib_empty"
                continue
            close_values = cast(pd.Series, pd.to_numeric(frame["close"], errors="coerce"))
            volume_values = cast(pd.Series, pd.to_numeric(frame["volume"], errors="coerce"))
            dollar_volume = cast(pd.Series, close_values.mul(volume_values))
            daily_dollar_volume = dollar_volume.dropna()
            if len(daily_dollar_volume) < 20:
                last_error = "ib_insufficient_history"
                continue
            rolling_addv20 = cast(pd.Series, daily_dollar_volume.rolling(20).mean())
            addv20 = rolling_addv20.dropna()
            if addv20.empty:
                last_error = "ib_insufficient_history"
                continue
            return float(addv20.tail(20).median()), "ib"
        except Exception as exc:
            last_error = f"ib_error:{type(exc).__name__}"
            LOGGER.warning("IB liquidity error for %s using symbol %s: %s", ticker, symbol, exc)
    return None, last_error or "ib_unavailable"


def fetch_liquidity(ticker: str, args: argparse.Namespace) -> tuple[Optional[float], str]:
    source = str(getattr(args, "liquidity_source", "ib") or "ib").strip().lower()
    if source == "ib":
        return fetch_liquidity_ib(ticker, args)
    if source == "none":
        return None, "disabled"
    return None, f"unsupported_liquidity_source:{source}"


def load_mapping(
    mapping_csv: Path,
    tickers_csv: Optional[Path],
    *,
    validate_identity: bool,
    allow_missing_identity_fields: bool,
) -> pd.DataFrame:
    if not mapping_csv.exists():
        raise FileNotFoundError(f"Mapping CSV not found: {mapping_csv}")
    df = pd.read_csv(mapping_csv, dtype=str).fillna("")
    if "Ticker" not in df.columns:
        for candidate in ("Tickers", "ticker", "tickers", "Symbol", "symbol"):
            if candidate in df.columns:
                df = df.rename(columns={candidate: "Ticker"})
                break
    required = {"Ticker", "CIK", "CompanyName"}
    missing = required.difference(df.columns)
    if missing:
        raise ValueError(f"Mapping CSV missing required columns: {sorted(missing)}")
    if validate_identity:
        validate_mapping_schema(df, allow_missing_identity_fields=allow_missing_identity_fields)
        log_mapping_quality(df)
    df["Ticker"] = df["Ticker"].map(normalize_ticker)
    df["CIK"] = df["CIK"].map(normalize_cik)
    df = df[(df["Ticker"] != "") & (df["CIK"] != "")].copy()
    if tickers_csv is not None and tickers_csv.exists():
        try:
            tickers_df = pd.read_csv(tickers_csv, dtype=str).fillna("")
        except pd.errors.EmptyDataError:
            LOGGER.warning("Tickers CSV is empty, skipping filter: %s", tickers_csv)
        else:
            if len(tickers_df.columns) == 0:
                LOGGER.warning("Tickers CSV has no columns, skipping filter: %s", tickers_csv)
            else:
                col = tickers_df.columns[0]
                keep = {normalize_ticker(x) for x in tickers_df[col].tolist() if normalize_ticker(x)}
                if keep:
                    ticker_values = cast(pd.Series, df["Ticker"])
                    mask = ticker_values.isin(sorted(keep))
                    df = cast(pd.DataFrame, df.loc[mask]).copy()
    clean_frame = cast(pd.DataFrame, df)
    return clean_frame.drop_duplicates(subset="Ticker", keep="first").reset_index(drop=True)


def empty_ctgov_summary() -> dict[str, Any]:
    return {
        "has_match": False,
        "interventional_match_count": 0,
        "lead_sponsor_match_count": 0,
        "collaborator_match_count": 0,
        "both_role_match_count": 0,
        "active_interventional_match_count": 0,
        "matched_trials": [],
        "search_aliases": [],
    }


def empty_text_scan_summary() -> dict[str, Any]:
    return {
        "reverse_split_hits_2y": 0,
        "reverse_split_hits_5y": 0,
        "reverse_split_soft_hits_2y": 0,
        "reverse_split_soft_hits_5y": 0,
        "going_concern_hits_2y": 0,
        "going_concern_periodic_hits_2y": 0,
        "going_concern_soft_hits_2y": 0,
        "going_concern_resolved_hits_2y": 0,
        "going_concern_resolution_hits_2y": 0,
        "going_concern_resolution_after_latest_periodic": False,
        "going_concern_status": "not_evaluated",
        "latest_periodic_going_concern": False,
        "latest_periodic_going_concern_status": "",
        "pipeline_hits_2y": 0,
        "rnd_keyword_hits_2y": 0,
        "text_scan_errors": [],
        "text_scanned_accessions": [],
    }


def default_google_confirmation_fields() -> dict[str, Any]:
    fields: dict[str, Any] = {
        "google_confirm": "No",
        "google_checked_classifications": "",
        "google_confirmation_error": "",
    }
    for classification in GOOGLE_CONFIRMATION_CLASSIFICATIONS:
        prefix = f"google_{classification}"
        fields.update(
            {
                f"{prefix}_checked": False,
                f"{prefix}_confirmed": False,
                f"{prefix}_status": "",
                f"{prefix}_confidence": "",
                f"{prefix}_company_name_match": False,
                f"{prefix}_event_count": 0,
                f"{prefix}_ratio_date": "",
                f"{prefix}_evidence_url": "",
                f"{prefix}_evidence_title": "",
                f"{prefix}_notes": "",
                f"{prefix}_source": "",
                f"{prefix}_error": "",
            }
        )
    return fields


def target_security_type_allowed(security_type: Any, allowed_security_types: Iterable[str]) -> bool:
    allowed = {normalize_key_text(value) for value in allowed_security_types if str(value).strip()}
    return normalize_key_text(security_type) in allowed


def listing_status_allowed(listing_status: Any, allowed_listing_statuses: Iterable[str]) -> bool:
    allowed = {normalize_key_text(value) for value in allowed_listing_statuses if str(value).strip()}
    return normalize_key_text(listing_status) in allowed


def pre_screen_remove_reasons(rec: dict[str, Any], args: argparse.Namespace) -> list[str]:
    if bool(getattr(args, "disable_identity_gate", False)):
        return []

    reasons: list[str] = []
    if parse_boolish(rec_get_any(rec, "ManualExclude", "manual_exclude")):
        reasons.append("manual_exclude")

    allow_missing_identity = bool(getattr(args, "allow_missing_identity_fields", False))
    security_type = rec_get_any(rec, "SecurityType", "security_type", "quoteType", "QuoteType")
    if security_type:
        if not target_security_type_allowed(security_type, getattr(args, "target_security_types", DEFAULT_TARGET_SECURITY_TYPES)):
            reasons.append("non_target_security_type")
    elif not allow_missing_identity:
        reasons.append("non_target_security_type")

    if bool(getattr(args, "require_primary_listing", True)):
        is_primary = rec_get_any(rec, "IsPrimaryListing", "is_primary_listing", "PrimaryListing", "primary_listing")
        if is_primary:
            if not parse_boolish(is_primary):
                reasons.append("not_primary_listing")
        elif not allow_missing_identity:
            reasons.append("not_primary_listing")

    listing_status = rec_get_any(rec, "ListingStatus", "listing_status", "Status", "status")
    if listing_status and not listing_status_allowed(listing_status, getattr(args, "allowed_listing_statuses", DEFAULT_ALLOWED_LISTING_STATUSES)):
        if normalize_key_text(listing_status).startswith("active_financial_status_"):
            reasons.append("listing_financial_status_not_clean")
        else:
            reasons.append("inactive_or_unknown_listing_status")
    elif not listing_status and not allow_missing_identity:
        reasons.append("inactive_or_unknown_listing_status")

    return sorted(dict.fromkeys(reasons))


def build_default_output_row(
    *,
    rec: dict[str, Any],
    screen_path: str,
    started_at: Optional[float] = None,
) -> dict[str, Any]:
    ticker = normalize_ticker(rec.get("Ticker", ""))
    cik10 = normalize_cik(rec.get("CIK", ""))
    company_name = str(rec.get("CompanyName", "") or "").strip()
    name_contains_biotech = bool(BIOTECH_NAME_PATTERN.search(company_name))
    name_likely_biotech = bool(LIKELY_BIOTECH_NAME_PATTERN.search(company_name))
    elapsed = 0.0 if started_at is None else round(time.perf_counter() - started_at, 3)
    return {
        "ticker": ticker,
        "cik": cik10,
        "company_name": company_name,
        "exchange": str(rec.get("Exchange", "") or ""),
        "sector": str(rec.get("sector", rec.get("Sector", "")) or ""),
        "industry": str(rec.get("industry", rec.get("Industry", "")) or ""),
        "industry_aggregate": str(rec.get("industry_aggregate", rec.get("IndustryAggregate", rec.get("industryAggregate", ""))) or ""),
        **optional_identity_fields(rec),
        "match_type": str(rec.get("MatchType", "") or ""),
        "source": str(rec.get("Source", "") or ""),
        "screen_path": screen_path,
        "name_contains_biotech": name_contains_biotech,
        "name_likely_biotech": name_likely_biotech,
        "ctgov_fetch_error": "",
        "ctgov_evaluated": False,
        "ctgov_evaluation_status": "not_evaluated_pre_screen",
        "has_interventional_trial_match": "",
        "ctgov_interventional_match_count": 0,
        "ctgov_lead_sponsor_match_count": 0,
        "ctgov_collaborator_match_count": 0,
        "ctgov_both_role_match_count": 0,
        "ctgov_active_interventional_match_count": 0,
        "ctgov_matched_ncts": "",
        "ctgov_search_aliases": "",
        "sec_fetch_error": "",
        "companyfacts_fetch_error": "",
        "has_recent_sec_filing_2y": False,
        "recent_sec_filing_count_2y": 0,
        "recent_8k_count_2y": 0,
        "recent_current_report_count_2y": 0,
        "recent_nt_filing_count_2y": 0,
        "reverse_split_hits_2y": 0,
        "reverse_split_hits_5y": 0,
        "reverse_split_external_hits_2y": 0,
        "reverse_split_external_hits_5y": 0,
        "reverse_split_soft_hits_2y": 0,
        "reverse_split_soft_hits_5y": 0,
        "going_concern_hits_2y": 0,
        "going_concern_periodic_hits_2y": 0,
        "going_concern_soft_hits_2y": 0,
        "going_concern_resolved_hits_2y": 0,
        "going_concern_resolution_hits_2y": 0,
        "going_concern_resolution_after_latest_periodic": False,
        "going_concern_status": "not_evaluated",
        "latest_periodic_going_concern": False,
        "latest_periodic_going_concern_status": "",
        "pipeline_hits_2y": 0,
        "rnd_keyword_hits_2y": 0,
        "recent_rnd_fact_hit_count": 0,
        "has_recent_rnd_fact": False,
        "has_recent_rnd_disclosure": False,
        "has_pipeline_disclosure": False,
        "median_addv20": None,
        "liquidity_status": "skipped_pre_screen",
        "text_scan_error_count": 0,
        "text_scan_errors": "",
        **default_google_confirmation_fields(),
        "ctgov_elapsed_sec": 0.0,
        "sec_elapsed_sec": 0.0,
        "companyfacts_elapsed_sec": 0.0,
        "text_scan_elapsed_sec": 0.0,
        "liquidity_elapsed_sec": 0.0,
        "total_elapsed_sec": elapsed,
    }


def build_worker_exception_row(rec: dict[str, Any], exc: BaseException) -> dict[str, Any]:
    ticker = normalize_ticker(rec.get("Ticker", ""))
    company_name = str(rec.get("CompanyName", "") or "").strip()
    row = build_default_output_row(rec=rec, screen_path="worker_exception")
    row.update(
        {
            "ctgov_fetch_error": f"worker_exception:{type(exc).__name__}",
            "ctgov_evaluated": False,
            "ctgov_evaluation_status": "worker_exception",
            "sec_fetch_error": f"worker_exception:{type(exc).__name__}",
            "liquidity_status": "worker_exception",
            "decision": "review",
            "reason_codes": "ctgov_fetch_error;sec_fetch_error",
        }
    )
    LOGGER.exception("Worker failed for %s (%s): %s", ticker, company_name, exc)
    return row


def decide_row(
    *,
    row: dict[str, Any],
    min_median_addv20: float,
    allow_missing_liquidity: bool,
    manual_include_demotes_remove_to_review: bool,
    review_on_soft_liquidity_warning: bool,
    hard_remove_google_confirmed_going_concern: bool = False,
    hard_remove_google_confirmed_reverse_split: bool = False,
) -> tuple[str, list[str]]:
    remove_reasons: list[str] = []
    review_reasons: list[str] = []
    keep_review_reasons: list[str] = []
    has_ctgov = bool(row.get("has_interventional_trial_match"))
    has_recent_sec_activity = bool(row.get("has_recent_sec_filing_2y"))
    has_biotech_disclosure = bool(row.get("has_recent_rnd_disclosure")) or bool(row.get("has_pipeline_disclosure"))
    manual_include = parse_boolish(row.get("manual_include"))
    manual_exclude = parse_boolish(row.get("manual_exclude"))
    manual_review = parse_boolish(row.get("manual_review"))

    listing_status = normalize_key_text(row.get("listing_status"))
    if listing_status.startswith("active_financial_status_"):
        # Nasdaq D/E flags are allocation vetoes, not proof that the security
        # stopped trading. Keep the issuer scoreable for research while the
        # report/portfolio gate blocks deployment until status returns clean.
        keep_review_reasons.append("review:listing_financial_status_not_clean")

    if manual_exclude:
        return "remove", ["manual_exclude"]

    if str(row.get("screen_path") or "") == "early_liquidity_gate":
        if manual_include and manual_include_demotes_remove_to_review:
            return "review", ["manual_include", "manual_include_override:extremely_illiquid"]
        return "remove", ["extremely_illiquid"]

    if row.get("ctgov_fetch_error"):
        review_reasons.append("ctgov_fetch_error")
    elif not has_ctgov:
        if has_recent_sec_activity or has_biotech_disclosure:
            review_reasons.append("ctgov_match_missing_needs_alias_review")
        else:
            remove_reasons.append("no_interventional_ctgov_match")

    if row.get("sec_fetch_error"):
        review_reasons.append("sec_fetch_error")
    elif not has_recent_sec_activity:
        if has_ctgov or has_biotech_disclosure:
            review_reasons.append("no_recent_10k_10q_8k_2y")
        else:
            remove_reasons.append("no_recent_10k_10q_8k_2y")

    going_concern_status = str(row.get("going_concern_status") or "").strip().lower()
    google_going_status = str(row.get("google_going_concern_status") or "").strip().lower()
    google_going_confirmed = parse_boolish(row.get("google_going_concern_confirmed"))
    google_going_not_confirmed = google_going_status in {"not_confirmed", "resolved"}
    if google_going_confirmed or google_going_status == "confirmed":
        if hard_remove_google_confirmed_going_concern:
            remove_reasons.append("confirmed_going_concern")
        else:
            review_reasons.append("google_confirmed_going_concern")
    elif google_going_not_confirmed:
        pass
    elif (
        going_concern_status in {"confirmed", "possible"}
        or int(row.get("going_concern_hits_2y") or 0) > 0
        or bool(row.get("latest_periodic_going_concern"))
    ):
        keep_review_reasons.append("review:possible_going_concern")
    elif review_on_soft_liquidity_warning and int(row.get("going_concern_soft_hits_2y") or 0) > 0:
        review_reasons.append("liquidity_capital_warning")

    if int(row.get("recent_nt_filing_count_2y") or 0) > 0:
        review_reasons.append("late_nt_10k_10q_filing")

    reverse_hits_2y = int(row.get("reverse_split_hits_2y") or 0)
    reverse_hits_5y = int(row.get("reverse_split_hits_5y") or 0)
    reverse_external_hits_2y = int(row.get("reverse_split_external_hits_2y") or 0)
    reverse_external_hits_5y = int(row.get("reverse_split_external_hits_5y") or 0)
    reverse_soft_hits_2y = int(row.get("reverse_split_soft_hits_2y") or 0)
    reverse_soft_hits_5y = int(row.get("reverse_split_soft_hits_5y") or 0)
    google_reverse_status = str(row.get("google_reverse_split_status") or "").strip().lower()
    google_reverse_confirmed = parse_boolish(row.get("google_reverse_split_confirmed"))
    google_reverse_not_confirmed = google_reverse_status == "not_confirmed"
    if google_reverse_confirmed or google_reverse_status == "confirmed":
        if hard_remove_google_confirmed_reverse_split:
            remove_reasons.append("reverse_split")
        else:
            review_reasons.append("google_confirmed_reverse_split")
    elif google_reverse_not_confirmed:
        pass
    elif reverse_external_hits_5y >= 2:
        keep_review_reasons.append("review:possible_reverse_split")
    elif (
        reverse_external_hits_2y >= 1
        or reverse_hits_2y >= 1
        or reverse_hits_5y >= 1
        or reverse_soft_hits_2y >= 1
        or reverse_soft_hits_5y >= 1
    ):
        keep_review_reasons.append("review:possible_reverse_split")

    liquidity_status = str(row.get("liquidity_status") or "")
    median_addv20 = parse_optional_float(row.get("median_addv20"))
    if median_addv20 is None:
        if not allow_missing_liquidity:
            review_reasons.append(f"liquidity_unavailable:{liquidity_status or 'unknown'}")
    elif median_addv20 < float(min_median_addv20):
        remove_reasons.append("extremely_illiquid")

    if (
        bool(row.get("name_contains_biotech"))
        and not has_ctgov
        and not row.get("ctgov_fetch_error")
        and not bool(row.get("has_recent_rnd_disclosure"))
        and not bool(row.get("has_pipeline_disclosure"))
    ):
        remove_reasons.append("biotech_name_no_trials_no_rd_no_pipeline")

    if manual_review:
        review_reasons.append("manual_review")

    if remove_reasons:
        if manual_include and manual_include_demotes_remove_to_review:
            review_reasons.append("manual_include")
            review_reasons.extend(f"manual_include_override:{reason}" for reason in remove_reasons)
            review_reasons.extend(keep_review_reasons)
            return "review", sorted(dict.fromkeys(review_reasons))
        return "remove", sorted(dict.fromkeys(remove_reasons))
    if review_reasons:
        review_reasons.extend(keep_review_reasons)
        return "review", sorted(dict.fromkeys(review_reasons))
    return "keep", sorted(dict.fromkeys(keep_review_reasons))


def screen_one(
    *,
    idx: int,
    total: int,
    rec: dict[str, Any],
    cache_dir: Path,
    recent_cutoff: date,
    reverse_cutoff: date,
    args: argparse.Namespace,
) -> dict[str, Any]:
    ticker = normalize_ticker(rec["Ticker"])
    cik10 = normalize_cik(rec["CIK"])
    company_name = str(rec["CompanyName"] or "").strip()
    name_contains_biotech = bool(BIOTECH_NAME_PATTERN.search(company_name))
    name_likely_biotech = bool(LIKELY_BIOTECH_NAME_PATTERN.search(company_name))
    LOGGER.info("[%d/%d] Screening %s (%s)", idx, total, ticker, company_name)
    started_at = time.perf_counter()

    pre_screen_reasons = pre_screen_remove_reasons(rec, args)
    if pre_screen_reasons:
        row = build_default_output_row(rec=rec, screen_path="pre_screen_identity_gate", started_at=started_at)
        row["decision"] = "remove"
        row["reason_codes"] = ";".join(pre_screen_reasons)
        return row

    ctgov_summary = empty_ctgov_summary()
    text_scan_summary = empty_text_scan_summary()
    ctgov_fetch_error = ""
    sec_fetch_error = ""
    companyfacts_fetch_error = ""
    recent_sec_filing_count = 0
    recent_8k_count = 0
    recent_current_report_count = 0
    recent_nt_filing_count = 0
    has_recent_rnd_fact = False
    rnd_fact_hit_count = 0
    median_addv20: Optional[float] = None
    liquidity_status = "skipped_basic_gate"
    screen_path = "basic_only"
    filings: list[FilingRef] = []

    ctgov_elapsed_sec = 0.0
    sec_elapsed_sec = 0.0
    companyfacts_elapsed_sec = 0.0
    text_scan_elapsed_sec = 0.0
    liquidity_elapsed_sec = 0.0
    reverse_external_hits_2y, reverse_external_hits_5y = reverse_split_event_counts(
        getattr(args, "reverse_split_events_by_ticker", {}),
        ticker,
        recent_cutoff,
        reverse_cutoff,
    )
    if args.disable_liquidity:
        liquidity_status = "disabled"
    else:
        stage_started = time.perf_counter()
        median_addv20, liquidity_status = fetch_liquidity(ticker, args)
        liquidity_elapsed_sec = time.perf_counter() - stage_started
        if median_addv20 is not None and float(median_addv20) < float(args.min_median_addv20):
            row = build_default_output_row(rec=rec, screen_path="early_liquidity_gate", started_at=started_at)
            row.update(
                {
                    "median_addv20": median_addv20,
                    "liquidity_status": liquidity_status,
                    "liquidity_elapsed_sec": round(liquidity_elapsed_sec, 3),
                    "total_elapsed_sec": round(time.perf_counter() - started_at, 3),
                    "decision": "remove",
                    "reason_codes": "extremely_illiquid",
                }
            )
            return row

    with nullcontext(get_thread_session()) as session:
        stage_started = time.perf_counter()
        try:
            ctgov_summary = query_ctgov_matches(
                session=session,
                company_name=company_name,
                cache_dir=cache_dir,
                timeout_sec=float(args.timeout_sec),
                json_ttl_hours=float(args.json_ttl_hours),
                sleep_sec=float(args.sleep_sec),
                max_pages=int(args.ctgov_max_pages),
            )
        except Exception as exc:
            ctgov_fetch_error = f"{type(exc).__name__}: {exc}"
        ctgov_elapsed_sec = time.perf_counter() - stage_started

        stage_started = time.perf_counter()
        try:
            filings = load_all_submissions(
                session=session,
                cik10=cik10,
                cache_dir=cache_dir,
                user_agent=args.user_agent,
                timeout_sec=float(args.timeout_sec),
                json_ttl_hours=float(args.json_ttl_hours),
                sleep_sec=float(args.sleep_sec),
            )
            recent_filings = [f for f in filings if f.filing_date >= recent_cutoff]
            recent_sec_filing_count = len([f for f in recent_filings if f.form in REQUIRED_SEC_FORMS])
            recent_8k_count = len([f for f in recent_filings if f.form in {"8-K", "8-K/A"}])
            recent_current_report_count = len([f for f in recent_filings if f.form in CURRENT_REPORT_FORMS])
            recent_nt_filing_count = len([f for f in recent_filings if f.form in NT_LATE_FILING_FORMS])
        except Exception as exc:
            sec_fetch_error = f"{type(exc).__name__}: {exc}"
        sec_elapsed_sec = time.perf_counter() - stage_started

        keep_candidate = (
            not ctgov_fetch_error
            and not sec_fetch_error
            and bool(ctgov_summary["has_match"])
            and recent_sec_filing_count > 0
        )
        diagnose_biotech_without_trials = (
            not ctgov_fetch_error
            and not sec_fetch_error
            and name_likely_biotech
            and not bool(ctgov_summary["has_match"])
            and recent_sec_filing_count > 0
        )

        if keep_candidate:
            screen_path = "full_keep_candidate"
            stage_started = time.perf_counter()
            try:
                text_scan_summary = scan_filing_texts(
                    session=session,
                    filings=filings,
                    cache_dir=cache_dir,
                    user_agent=args.user_agent,
                    timeout_sec=float(args.timeout_sec),
                    text_ttl_hours=float(args.text_ttl_hours),
                    sleep_sec=float(args.sleep_sec),
                    recent_cutoff=recent_cutoff,
                    reverse_cutoff=reverse_cutoff,
                    max_text_filings=int(args.max_text_filings),
                )
            except Exception as exc:
                sec_fetch_error = sec_fetch_error or f"{type(exc).__name__}: {exc}"
            text_scan_elapsed_sec = time.perf_counter() - stage_started

            if not args.disable_liquidity and not sec_fetch_error:
                if liquidity_status == "skipped_basic_gate":
                    stage_started = time.perf_counter()
                    median_addv20, liquidity_status = fetch_liquidity(ticker, args)
                    liquidity_elapsed_sec = time.perf_counter() - stage_started
            elif args.disable_liquidity:
                liquidity_status = "disabled"
            else:
                liquidity_status = "skipped_sec_error"
        elif diagnose_biotech_without_trials:
            screen_path = "biotech_no_trial_diagnostic"
            if liquidity_status == "skipped_basic_gate":
                liquidity_status = "skipped_no_trials"
            stage_started = time.perf_counter()
            try:
                companyfacts = fetch_companyfacts(
                    session=session,
                    cik10=cik10,
                    cache_dir=cache_dir,
                    user_agent=args.user_agent,
                    timeout_sec=float(args.timeout_sec),
                    json_ttl_hours=float(args.json_ttl_hours),
                    sleep_sec=float(args.sleep_sec),
                )
                has_recent_rnd_fact, rnd_fact_hit_count = companyfacts_has_recent_rnd(companyfacts, recent_cutoff)
            except Exception as exc:
                companyfacts_fetch_error = f"{type(exc).__name__}: {exc}"
            companyfacts_elapsed_sec = time.perf_counter() - stage_started

            if not has_recent_rnd_fact:
                stage_started = time.perf_counter()
                try:
                    text_scan_summary = scan_filing_texts(
                        session=session,
                        filings=filings,
                        cache_dir=cache_dir,
                        user_agent=args.user_agent,
                        timeout_sec=float(args.timeout_sec),
                        text_ttl_hours=float(args.text_ttl_hours),
                        sleep_sec=float(args.sleep_sec),
                        recent_cutoff=recent_cutoff,
                        reverse_cutoff=reverse_cutoff,
                        max_text_filings=max(
                            1,
                            min(
                                int(args.max_text_filings),
                                int(args.max_biotech_diagnostic_filings),
                            ),
                        ),
                    )
                except Exception as exc:
                    sec_fetch_error = sec_fetch_error or f"{type(exc).__name__}: {exc}"
                text_scan_elapsed_sec = time.perf_counter() - stage_started

    row: dict[str, Any] = {
        "ticker": ticker,
        "cik": cik10,
        "company_name": company_name,
        "exchange": str(rec.get("Exchange", "") or ""),
        "sector": str(rec.get("sector", rec.get("Sector", "")) or ""),
        "industry": str(rec.get("industry", rec.get("Industry", "")) or ""),
        "industry_aggregate": str(rec.get("industry_aggregate", rec.get("IndustryAggregate", rec.get("industryAggregate", ""))) or ""),
        **optional_identity_fields(rec),
        "match_type": str(rec.get("MatchType", "") or ""),
        "source": str(rec.get("Source", "") or ""),
        "screen_path": screen_path,
        "name_contains_biotech": name_contains_biotech,
        "name_likely_biotech": name_likely_biotech,
        "ctgov_fetch_error": ctgov_fetch_error,
        "ctgov_evaluated": not bool(ctgov_fetch_error),
        "ctgov_evaluation_status": "error" if ctgov_fetch_error else "queried",
        "has_interventional_trial_match": bool(ctgov_summary["has_match"]),
        "ctgov_interventional_match_count": int(ctgov_summary["interventional_match_count"]),
        "ctgov_lead_sponsor_match_count": int(ctgov_summary["lead_sponsor_match_count"]),
        "ctgov_collaborator_match_count": int(ctgov_summary["collaborator_match_count"]),
        "ctgov_both_role_match_count": int(ctgov_summary["both_role_match_count"]),
        "ctgov_active_interventional_match_count": int(ctgov_summary["active_interventional_match_count"]),
        "ctgov_matched_ncts": ";".join(str(x["nct_id"]) for x in ctgov_summary["matched_trials"]),
        "ctgov_search_aliases": ";".join(str(x) for x in ctgov_summary.get("search_aliases", [])),
        "sec_fetch_error": sec_fetch_error,
        "companyfacts_fetch_error": companyfacts_fetch_error,
        "has_recent_sec_filing_2y": recent_sec_filing_count > 0,
        "recent_sec_filing_count_2y": int(recent_sec_filing_count),
        "recent_8k_count_2y": int(recent_8k_count),
        "recent_current_report_count_2y": int(recent_current_report_count),
        "recent_nt_filing_count_2y": int(recent_nt_filing_count),
        "reverse_split_hits_2y": int(text_scan_summary["reverse_split_hits_2y"]),
        "reverse_split_hits_5y": int(text_scan_summary["reverse_split_hits_5y"]),
        "reverse_split_external_hits_2y": int(reverse_external_hits_2y),
        "reverse_split_external_hits_5y": int(reverse_external_hits_5y),
        "reverse_split_soft_hits_2y": int(text_scan_summary["reverse_split_soft_hits_2y"]),
        "reverse_split_soft_hits_5y": int(text_scan_summary["reverse_split_soft_hits_5y"]),
        "going_concern_hits_2y": int(text_scan_summary["going_concern_hits_2y"]),
        "going_concern_periodic_hits_2y": int(text_scan_summary["going_concern_periodic_hits_2y"]),
        "going_concern_soft_hits_2y": int(text_scan_summary["going_concern_soft_hits_2y"]),
        "going_concern_resolved_hits_2y": int(text_scan_summary["going_concern_resolved_hits_2y"]),
        "going_concern_resolution_hits_2y": int(text_scan_summary["going_concern_resolution_hits_2y"]),
        "going_concern_resolution_after_latest_periodic": bool(text_scan_summary["going_concern_resolution_after_latest_periodic"]),
        "going_concern_status": str(text_scan_summary["going_concern_status"]),
        "latest_periodic_going_concern": bool(text_scan_summary["latest_periodic_going_concern"]),
        "latest_periodic_going_concern_status": str(text_scan_summary["latest_periodic_going_concern_status"]),
        "pipeline_hits_2y": int(text_scan_summary["pipeline_hits_2y"]),
        "rnd_keyword_hits_2y": int(text_scan_summary["rnd_keyword_hits_2y"]),
        "recent_rnd_fact_hit_count": int(rnd_fact_hit_count),
        "has_recent_rnd_fact": bool(has_recent_rnd_fact),
        "has_recent_rnd_disclosure": bool(has_recent_rnd_fact or int(text_scan_summary["rnd_keyword_hits_2y"]) > 0),
        "has_pipeline_disclosure": bool(int(text_scan_summary["pipeline_hits_2y"]) > 0),
        "median_addv20": median_addv20,
        "liquidity_status": liquidity_status,
        "text_scan_error_count": len(text_scan_summary["text_scan_errors"]),
        "text_scan_errors": ";".join(text_scan_summary["text_scan_errors"]),
        **default_google_confirmation_fields(),
        "ctgov_elapsed_sec": round(ctgov_elapsed_sec, 3),
        "sec_elapsed_sec": round(sec_elapsed_sec, 3),
        "companyfacts_elapsed_sec": round(companyfacts_elapsed_sec, 3),
        "text_scan_elapsed_sec": round(text_scan_elapsed_sec, 3),
        "liquidity_elapsed_sec": round(liquidity_elapsed_sec, 3),
        "total_elapsed_sec": round(time.perf_counter() - started_at, 3),
    }
    decision, reason_codes = decide_row(
        row=row,
        min_median_addv20=float(args.min_median_addv20),
        allow_missing_liquidity=bool(args.allow_missing_liquidity),
        manual_include_demotes_remove_to_review=bool(args.manual_include_demotes_remove_to_review),
        review_on_soft_liquidity_warning=bool(args.review_on_soft_liquidity_warning),
        hard_remove_google_confirmed_going_concern=bool(args.hard_remove_google_confirmed_going_concern),
        hard_remove_google_confirmed_reverse_split=bool(args.hard_remove_google_confirmed_reverse_split),
    )
    row["decision"] = decision
    row["reason_codes"] = ";".join(reason_codes)
    return row


def google_classifications_for_row(row: dict[str, Any], enabled_classifications: Iterable[str]) -> list[str]:
    enabled = {str(value).strip().lower() for value in enabled_classifications if str(value).strip()}
    reason_codes = str(row.get("reason_codes") or "")
    classifications: list[str] = []
    if "reverse_split" in enabled:
        reverse_hits = (
            int(row.get("reverse_split_hits_2y") or 0)
            + int(row.get("reverse_split_hits_5y") or 0)
            + int(row.get("reverse_split_soft_hits_2y") or 0)
            + int(row.get("reverse_split_soft_hits_5y") or 0)
            + int(row.get("reverse_split_external_hits_2y") or 0)
            + int(row.get("reverse_split_external_hits_5y") or 0)
        )
        if "possible_reverse_split" in reason_codes or reverse_hits > 0:
            classifications.append("reverse_split")
    if "going_concern" in enabled:
        going_status = str(row.get("going_concern_status") or "").strip().lower()
        going_hits = (
            int(row.get("going_concern_hits_2y") or 0)
            + int(row.get("going_concern_periodic_hits_2y") or 0)
            + int(row.get("going_concern_soft_hits_2y") or 0)
        )
        if going_status in {"confirmed", "possible"} or "possible_going_concern" in reason_codes or going_hits > 0:
            classifications.append("going_concern")
    return classifications


def build_google_confirmation_config(args: argparse.Namespace, as_of: date) -> GoogleScreenerConfig:
    return GoogleScreenerConfig(
        enabled=bool(args.google_confirmation_enabled),
        api_key_env=str(args.google_api_key_env),
        model=str(args.google_model),
        fallback_model=str(args.google_fallback_model),
        use_search_grounding=bool(args.google_use_search_grounding),
        batch_size=max(1, int(args.google_batch_size)),
        max_calls_per_run=max(0, int(args.google_max_calls_per_run)),
        min_seconds_between_calls=max(0.0, float(args.google_min_seconds_between_calls)),
        cache_file=args.google_cache_file.expanduser().resolve(),
        raw_output_file=args.google_raw_output_file.expanduser().resolve(),
        cache_ttl_days=float(args.google_cache_ttl_days),
        min_confidence_for_confirmed=str(args.google_min_confidence_for_confirmed),
        require_company_name_match=bool(args.google_require_company_name_match),
        require_primary_source=bool(args.google_require_primary_source),
        rerun_missing_tickers=bool(args.google_rerun_missing_tickers),
        max_missing_rerun_calls=int(args.google_max_missing_rerun_calls),
        as_of_date=as_of.isoformat(),
        lookback_years=float(args.lookback_years),
    )


def apply_google_confirmations(results_df: pd.DataFrame, args: argparse.Namespace, as_of: date) -> pd.DataFrame:
    for col, default_value in default_google_confirmation_fields().items():
        if col not in results_df.columns:
            results_df[col] = default_value
    if not bool(args.google_confirmation_enabled):
        return results_df

    enabled_classifications = [
        str(value).strip().lower()
        for value in getattr(args, "google_confirmation_classifications", [])
        if str(value).strip().lower() in set(GOOGLE_CONFIRMATION_CLASSIFICATIONS)
    ]
    if not enabled_classifications:
        LOGGER.info("Google confirmation enabled but no supported classifications are configured")
        return results_df

    candidates: list[dict[str, Any]] = []
    for _, row in results_df.iterrows():
        rec = {str(k): v for k, v in row.items()}
        reason_codes = {part.strip() for part in str(rec.get("reason_codes") or "").split(";") if part.strip()}
        if "extremely_illiquid" in reason_codes or str(rec.get("decision") or "") == "remove":
            continue
        classifications = google_classifications_for_row(rec, enabled_classifications)
        if classifications:
            candidates.append(
                {
                    "ticker": rec.get("ticker"),
                    "company_name": rec.get("company_name"),
                    "classifications": classifications,
                }
            )
    if not candidates:
        LOGGER.info("Google confirmation skipped: no candidate rows matched configured classifications")
        return results_df

    config = build_google_confirmation_config(args, as_of)
    LOGGER.info(
        "Google confirmation starting: candidates=%d classifications=%s model=%s max_calls=%d batch_size=%d",
        len(candidates),
        ",".join(enabled_classifications),
        config.model,
        config.max_calls_per_run,
        config.batch_size,
    )
    confirmations = confirm_candidates(candidates, classifications=enabled_classifications, config=config)
    if confirmations.empty:
        LOGGER.info("Google confirmation returned no rows")
        return results_df

    confirmations["ticker_norm"] = confirmations["ticker"].map(normalize_ticker)
    confirmations["classification_norm"] = confirmations["classification"].astype(str).str.strip().str.lower()
    confirmation_map: dict[tuple[str, str], dict[str, Any]] = {}
    for _, conf_row in confirmations.iterrows():
        key = (str(conf_row["ticker_norm"]), str(conf_row["classification_norm"]))
        confirmation_map[key] = conf_row.to_dict()

    updated_rows: list[dict[str, Any]] = []
    for _, row in results_df.iterrows():
        rec = {str(k): v for k, v in row.items()}
        ticker_norm = normalize_ticker(rec.get("ticker"))
        checked: list[str] = [
            part.strip().lower()
            for part in str(rec.get("google_checked_classifications") or "").split(";")
            if part.strip()
        ]
        errors: list[str] = [
            part.strip()
            for part in str(rec.get("google_confirmation_error") or "").split(";")
            if part.strip()
        ]
        for classification in GOOGLE_CONFIRMATION_CLASSIFICATIONS:
            conf = confirmation_map.get((ticker_norm, classification))
            if conf is None:
                continue
            prefix = f"google_{classification}"
            status = str(conf.get("status") or "")
            if classification not in checked:
                checked.append(classification)
            confirmed = parse_boolish(conf.get("confirmed"))
            rec[f"{prefix}_checked"] = True
            rec[f"{prefix}_confirmed"] = confirmed
            rec[f"{prefix}_status"] = status
            rec[f"{prefix}_confidence"] = str(conf.get("confidence") or "")
            rec[f"{prefix}_company_name_match"] = parse_boolish(conf.get("company_name_match"))
            rec[f"{prefix}_event_count"] = int(float(str(conf.get("event_count") or 0)))
            rec[f"{prefix}_ratio_date"] = str(conf.get("ratio_date") or "")
            rec[f"{prefix}_evidence_url"] = str(conf.get("evidence_url") or "")
            rec[f"{prefix}_evidence_title"] = str(conf.get("evidence_title") or "")
            rec[f"{prefix}_notes"] = str(conf.get("notes") or "")
            rec[f"{prefix}_source"] = str(conf.get("source") or "")
            rec[f"{prefix}_error"] = str(conf.get("error") or "")
            if str(conf.get("error") or "").strip():
                error_text = f"{classification}:{conf.get('error')}"
                if error_text not in errors:
                    errors.append(error_text)
        confirmed_any = any(
            parse_boolish(rec.get(f"google_{classification}_confirmed"))
            for classification in GOOGLE_CONFIRMATION_CLASSIFICATIONS
        )
        checked_set = set(checked)
        ordered_checked = [classification for classification in GOOGLE_CONFIRMATION_CLASSIFICATIONS if classification in checked_set]
        ordered_checked.extend(part for part in checked if part not in set(ordered_checked))
        rec["google_confirm"] = "Yes" if confirmed_any else "No"
        rec["google_checked_classifications"] = ";".join(ordered_checked)
        rec["google_confirmation_error"] = ";".join(errors)
        decision, reason_codes = decide_row(
            row=rec,
            min_median_addv20=float(args.min_median_addv20),
            allow_missing_liquidity=bool(args.allow_missing_liquidity),
            manual_include_demotes_remove_to_review=bool(args.manual_include_demotes_remove_to_review),
            review_on_soft_liquidity_warning=bool(args.review_on_soft_liquidity_warning),
            hard_remove_google_confirmed_going_concern=bool(args.hard_remove_google_confirmed_going_concern),
            hard_remove_google_confirmed_reverse_split=bool(args.hard_remove_google_confirmed_reverse_split),
        )
        rec["decision"] = decision
        rec["reason_codes"] = ";".join(reason_codes)
        updated_rows.append(rec)

    out = pd.DataFrame(updated_rows)
    LOGGER.info(
        "Google confirmation finished: rows=%d confirmed=%d cache_file=%s",
        len(confirmations),
        int(confirmations["confirmed"].astype(str).str.lower().eq("true").sum()),
        config.cache_file,
    )
    return out


def main() -> None:
    configure_logging()
    args = parse_args()
    apply_configured_globals(args)

    mapping_csv = args.mapping_csv.expanduser().resolve()
    tickers_csv = None if args.no_tickers_csv else args.tickers_csv.expanduser().resolve() if args.tickers_csv else None
    output_dir = args.output_dir.expanduser().resolve()
    cache_dir = args.cache_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    as_of = datetime.now(timezone.utc).date()
    recent_cutoff = as_of - timedelta(days=int(round(365.25 * float(args.lookback_years))))
    reverse_cutoff = as_of - timedelta(days=int(round(365.25 * float(args.reverse_split_lookback_years))))
    args.reverse_split_events_by_ticker = load_reverse_split_events(args.reverse_split_events_csv)

    universe = load_mapping(
        mapping_csv,
        tickers_csv,
        validate_identity=not bool(args.disable_identity_gate),
        allow_missing_identity_fields=bool(args.allow_missing_identity_fields),
    )
    if args.max_tickers and args.max_tickers > 0:
        universe = universe.head(int(args.max_tickers)).copy()
    LOGGER.info("Loaded %d candidate tickers from %s", len(universe), mapping_csv)
    records = [{str(k): v for k, v in rec.items()} for rec in universe.to_dict("records")]
    total = len(records)
    if total == 0:
        LOGGER.warning("Universe is empty after filtering; no output written.")
        return
    max_workers = max(1, min(int(args.workers), total if total > 0 else 1))
    LOGGER.info("Screening with %d worker(s)", max_workers)

    results: list[dict[str, Any]] = []
    try:
        if max_workers == 1:
            for idx, rec in enumerate(records, start=1):
                try:
                    results.append(
                        screen_one(
                            idx=idx,
                            total=total,
                            rec=rec,
                            cache_dir=cache_dir,
                            recent_cutoff=recent_cutoff,
                            reverse_cutoff=reverse_cutoff,
                            args=args,
                        )
                    )
                except Exception as exc:
                    results.append(build_worker_exception_row(rec, exc))
        else:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_map = {
                    executor.submit(
                        screen_one,
                        idx=idx,
                        total=total,
                        rec=rec,
                        cache_dir=cache_dir,
                        recent_cutoff=recent_cutoff,
                        reverse_cutoff=reverse_cutoff,
                        args=args,
                    ): (idx, rec)
                    for idx, rec in enumerate(records, start=1)
                }
                for future in as_completed(future_map):
                    idx, rec = future_map[future]
                    try:
                        results.append(future.result())
                    except Exception as exc:
                        results.append(build_worker_exception_row(rec, exc))
    finally:
        close_thread_sessions()

    results_df = pd.DataFrame(results)
    results_df = apply_google_confirmations(results_df, args, as_of)
    results_df = results_df.sort_values(["decision", "ticker"]).reset_index(drop=True)
    output_file = args.output_file.expanduser()
    all_path = output_file if output_file.is_absolute() else output_dir / output_file

    all_path.parent.mkdir(parents=True, exist_ok=True)
    results_df.to_csv(all_path, index=False)

    LOGGER.info("Wrote %s", all_path)
    LOGGER.info("Keep=%d Review=%d Remove=%d", int((results_df["decision"] == "keep").sum()), int((results_df["decision"] == "review").sum()), int((results_df["decision"] == "remove").sum()))


if __name__ == "__main__":
    main()
