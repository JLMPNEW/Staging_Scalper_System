#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import logging
import re
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, resolve_path
from biotech_index.core.db import connect, finish_run, init_db, refresh_sec_latest_documents, start_run, utc_now
from biotech_index.core.logging_utils import configure_utc_logging
from biotech_index.core.pipeline_guards import normalize_ticker, read_final_scoring_tickers


LOGGER = logging.getLogger("parse_sec_biotech_events")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
PARSER_LOGIC_VERSION = "2026-07-03-sec-event-parser-v5"
SQLITE_PARAM_CHUNK_SIZE = 800


def chunked(values: list[Any] | tuple[Any, ...], size: int = SQLITE_PARAM_CHUNK_SIZE) -> list[list[Any]]:
    step = max(1, int(size))
    return [list(values[start : start + step]) for start in range(0, len(values), step)]


OUTPUT_FIELDS = [
    "ticker",
    "company_name",
    "accession_nodash",
    "filing_date",
    "form",
    "event_type",
    "event_date",
    "event_value",
    "polarity",
    "confidence",
    "extracted_text",
    "document_url",
]


@dataclass(frozen=True)
class FilingText:
    company_id: int
    ticker: str
    company_name: str
    accession_nodash: str
    filing_date: str
    form: str
    document_url: str
    text_hash: str
    text_content: str


@dataclass(frozen=True)
class EventRule:
    event_type: str
    polarity: str
    confidence: float
    patterns: tuple[re.Pattern[str], ...]
    value_pattern: re.Pattern[str] | None = None


@dataclass(frozen=True)
class SecEvent:
    company_id: int
    ticker: str
    company_name: str
    accession_nodash: str
    filing_date: str
    form: str
    event_type: str
    event_date: str
    event_value: str
    polarity: str
    confidence: float
    extracted_text: str
    document_url: str
    source_payload: str


def rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE | re.DOTALL)


TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")
PDUFA_VALUE = rx(r"\b(?:PDUFA|Prescription Drug User Fee Act)(?: date| goal date| action date| target action date)?\b.{0,160}")
RULES: tuple[EventRule, ...] = (
    EventRule(
        "going_concern_confirmed",
        "negative",
        0.95,
        (
            rx(r"\bsubstantial doubt\b.{0,260}\bcontinue as a going concern\b"),
            rx(r"\bgoing concern\b.{0,180}\bsubstantial doubt\b"),
        ),
    ),
    EventRule(
        "atm_program",
        "negative",
        0.85,
        (
            rx(r"\bat[- ]the[- ]market\b.{0,120}\b(offering|program|facility|sales agreement)\b"),
            rx(r"\bATM\b.{0,80}\b(offering|program|facility|sales agreement)\b"),
            rx(r"\bequity distribution agreement\b"),
            rx(r"\bopen market sale agreement\b"),
            rx(r"\bsales agreement\b.{0,160}\b(common stock|ordinary shares|ADSs|American Depositary Shares)\b"),
        ),
    ),
    EventRule(
        "shelf_registration",
        "negative",
        0.78,
        (
            rx(r"\bshelf registration statement\b"),
            rx(r"\buniversal shelf\b"),
            rx(r"\bForm S-3\b.{0,160}\bregistration statement\b"),
            rx(r"\bautomatic shelf registration statement\b"),
        ),
    ),
    EventRule(
        "public_offering",
        "negative",
        0.82,
        (
            rx(r"\bunderwritten public offering\b"),
            rx(r"\bregistered direct offering\b"),
            rx(r"\bpriced\b.{0,120}\bpublic offering\b"),
            rx(r"\bpublic offering\b.{0,160}\b(common stock|ordinary shares|ADSs|American Depositary Shares)\b"),
        ),
    ),
    EventRule(
        "pipe_financing",
        "negative",
        0.78,
        (
            rx(r"\bPIPE\b.{0,80}\b(financing|private placement|transaction)\b"),
            rx(r"\bprivate placement\b.{0,160}\b(common stock|ordinary shares|warrants|pre-funded warrants)\b"),
            rx(r"\bsecurities purchase agreement\b.{0,160}\b(private placement|institutional investor|accredited investor)\b"),
        ),
    ),
    EventRule(
        "nda_bla_accepted",
        "positive",
        0.92,
        (
            rx(r"\bFDA\b.{0,160}\baccepted\b.{0,80}\b(NDA|BLA|new drug application|biologics license application)\b"),
            rx(r"\b(NDA|BLA|new drug application|biologics license application)\b.{0,160}\baccepted\b.{0,80}\b(FDA|for review)\b"),
            rx(r"\baccepted for (filing|review)\b.{0,120}\b(NDA|BLA|new drug application|biologics license application)\b"),
        ),
    ),
    EventRule(
        "pdufa_date",
        "positive",
        0.90,
        (
            rx(r"\bPDUFA\b.{0,160}\b(date|goal date|action date|target action date)\b"),
            rx(r"\bPrescription Drug User Fee Act\b.{0,160}\b(date|goal date|action date|target action date)\b"),
        ),
        value_pattern=PDUFA_VALUE,
    ),
    EventRule(
        "regulatory_submission",
        "positive",
        0.76,
        (
            rx(r"\b(submitted|filed|completed submission of)\b.{0,120}\b(NDA|BLA|MAA|new drug application|biologics license application)\b"),
            rx(r"\b(NDA|BLA|MAA|new drug application|biologics license application)\b.{0,120}\b(submitted|filed)\b"),
        ),
    ),
    EventRule(
        "partial_clinical_hold",
        "negative",
        0.92,
        (
            rx(r"\b(FDA|regulator|regulatory authority|agency)\b.{0,140}\b(imposed|placed|issued|maintained|continued)\b.{0,80}\bpartial clinical hold\b"),
            rx(r"\b(received|announced|was placed on|has been placed on)\b.{0,120}\bpartial clinical hold\b"),
        ),
    ),
    EventRule(
        "clinical_hold",
        "negative",
        0.92,
        (
            rx(r"\b(FDA|regulator|regulatory authority|agency)\b.{0,140}\b(imposed|placed|issued|maintained|continued)\b.{0,80}\bclinical hold\b"),
            rx(r"\b(received|announced|was placed on|has been placed on)\b.{0,120}\bclinical hold\b"),
            rx(r"\bclinical hold\b.{0,120}\b(our|the)\b.{0,80}\b(IND|trial|study|program)\b"),
        ),
    ),
    EventRule(
        "endpoint_missed",
        "negative",
        0.90,
        (
            rx(r"\b(did not|failed to|fails to)\b.{0,80}\b(primary|secondary)?\s*endpoint\b"),
            rx(r"\bprimary endpoint\b.{0,80}\b(was not met|not met|was not achieved)\b"),
            rx(r"\bnot statistically significant\b.{0,100}\b(primary|endpoint|study|trial)\b"),
        ),
    ),
    EventRule(
        "endpoint_met",
        "positive",
        0.90,
        (
            rx(r"\b(met|achieved)\b.{0,80}\b(primary|secondary)?\s*endpoint\b"),
            rx(r"\bprimary endpoint\b.{0,80}\b(was met|achieved|statistically significant)\b"),
            rx(r"\bstatistically significant\b.{0,120}\b(primary endpoint|improvement|benefit)\b"),
        ),
    ),
    EventRule(
        "clinical_update_negative",
        "negative",
        0.74,
        (
            rx(r"\b(topline|clinical|phase [123])\b.{0,120}\b(negative|disappointing|unfavorable)\b.{0,80}\b(data|results|outcome)\b"),
            rx(r"\b(discontinue|discontinued|terminate|terminated|pause|paused|halt|halted)\b.{0,160}\b(trial|study|program|development)\b"),
            rx(r"\bsafety signal\b"),
        ),
    ),
    EventRule(
        "clinical_update_positive",
        "positive",
        0.74,
        (
            rx(r"\bpositive\b.{0,80}\b(topline|clinical|phase [123])\b.{0,80}\b(data|results)\b"),
            rx(r"\b(topline|clinical|phase [123])\b.{0,120}\bpositive\b.{0,80}\b(data|results|outcome)\b"),
            rx(r"\bpromising\b.{0,120}\b(phase [123]|clinical|data|results)\b"),
        ),
    ),
    EventRule(
        "partnership_license",
        "positive",
        0.70,
        (
            rx(r"\b(entered into|announced|signed|executed|granted|obtained|expanded|amended)\b.{0,160}\b(license agreement|collaboration agreement|strategic collaboration|strategic partnership|exclusive license)\b"),
            rx(r"\b(license agreement|collaboration agreement|strategic collaboration|strategic partnership|exclusive license)\b.{0,160}\b(entered into|announced|signed|executed|granted|obtained|expanded|amended)\b"),
        ),
    ),
    EventRule(
        "breakthrough_therapy_granted",
        "positive",
        0.92,
        (
            rx(r"\bFDA\b.{0,160}\bgranted\b.{0,80}\bBreakthrough Therapy(?: designation)?\b"),
            rx(r"\bBreakthrough Therapy(?: designation)?\b.{0,160}\b(granted|received|awarded|obtained)\b"),
            rx(r"\b(received|been granted|obtained)\b.{0,80}\bBreakthrough Therapy(?: designation)?\b"),
        ),
    ),
    EventRule(
        "rmat_granted",
        "positive",
        0.92,
        (
            rx(r"\bFDA\b.{0,160}\bgranted\b.{0,80}\bRMAT(?: designation)?\b"),
            rx(r"\bRMAT(?: designation)?\b.{0,160}\b(granted|received|awarded|obtained)\b"),
            rx(r"\b(received|been granted|obtained)\b.{0,80}\bRegenerative Medicine Advanced Therapy(?: designation)?\b"),
        ),
    ),
    EventRule(
        "fast_track_granted",
        "positive",
        0.85,
        (
            rx(r"\bFDA\b.{0,160}\bgranted\b.{0,80}\bFast Track(?: designation)?\b"),
            rx(r"\bFast Track(?: designation)?\b.{0,160}\b(granted|received|awarded|obtained)\b"),
            rx(r"\b(received|been granted|obtained)\b.{0,80}\bFast Track(?: designation)?\b"),
        ),
    ),
    EventRule(
        "orphan_drug_granted",
        "positive",
        0.88,
        (
            rx(r"\bFDA\b.{0,160}\bgranted\b.{0,80}\b(Orphan Drug|orphan drug)(?: designation)?\b"),
            rx(r"\b(Orphan Drug|orphan drug)(?: designation)?\b.{0,160}\b(granted|received|awarded|obtained)\b"),
            rx(r"\b(received|been granted|obtained)\b.{0,80}\b(Orphan Drug|orphan drug)(?: designation)?\b"),
        ),
    ),
    EventRule(
        "priority_review_granted",
        "positive",
        0.92,
        (
            rx(r"\bFDA\b.{0,160}\bgranted\b.{0,80}\bPriority Review(?: designation)?\b"),
            rx(r"\bPriority Review\b.{0,160}\b(granted|received|awarded|accepted)\b"),
            rx(r"\b(received|been granted|accepted)\b.{0,80}\bPriority Review\b"),
        ),
    ),
)

RULE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "going_concern_confirmed": ("substantial doubt", "going concern"),
    "atm_program": ("at-the-market", "at the market", "atm", "equity distribution agreement", "sales agreement"),
    "shelf_registration": ("shelf", "form s-3", "registration statement"),
    "public_offering": ("public offering", "registered direct", "underwritten"),
    "pipe_financing": ("pipe", "private placement", "securities purchase agreement"),
    "nda_bla_accepted": ("accepted", "nda", "bla", "new drug application", "biologics license application"),
    "pdufa_date": ("pdufa", "prescription drug user fee act"),
    "regulatory_submission": ("submitted", "filed", "nda", "bla", "maa", "new drug application", "biologics license application"),
    "partial_clinical_hold": ("partial clinical hold",),
    "clinical_hold": ("clinical hold",),
    "endpoint_missed": ("endpoint", "not statistically significant", "failed to", "did not"),
    "endpoint_met": ("endpoint", "statistically significant", "achieved", "met"),
    "clinical_update_negative": ("negative", "disappointing", "unfavorable", "discontinue", "discontinued", "terminate", "terminated", "pause", "paused", "halt", "halted", "safety signal"),
    "clinical_update_positive": ("positive", "promising", "topline", "clinical"),
    "partnership_license": ("license agreement", "collaboration agreement", "strategic collaboration", "strategic partnership", "exclusive license"),
    "breakthrough_therapy_granted": ("breakthrough therapy",),
    "rmat_granted": ("rmat", "regenerative medicine advanced therapy"),
    "fast_track_granted": ("fast track",),
    "orphan_drug_granted": ("orphan drug", "orphan designation"),
    "priority_review_granted": ("priority review",),
}

NEGATED_HOLD = rx(r"\b(no|not|without|never)\b.{0,80}\bclinical hold\b")
NEGATED_GOING_CONCERN = rx(r"\b(no|not)\b.{0,80}\bsubstantial doubt\b.{0,160}\bgoing concern\b")
ALLEV_GOING_CONCERN = rx(r"\b(alleviate|alleviates|alleviated|alleviating)\b.{0,80}\bsubstantial doubt\b|\bsubstantial doubt\b.{0,120}\b(alleviate|alleviates|alleviated|alleviating)\b")
GENERIC_RISK_FACTOR = rx(
    r"\b(risk factors|may subject|could subject|may have to|may decide|clinical failure can occur|negative or inconclusive results|administrative or judicial sanctions|there can be no assurance|we may|we could|if we)\b"
)
NEGATED_SAFETY_SIGNAL = rx(r"\b(no|not|without)\b.{0,80}\b(?:new )?safety signal\b|\b(?:new )?safety signal\b.{0,80}\b(not observed|not identified|not detected|was not observed)")
GENERIC_NDA_BLA = rx(
    r"\b(reviews all submitted|must make a decision|may refuse to file|refusal to file|"
    r"once (?:the )?(?:submission|NDA|BLA|application) (?:is|has been) accepted|"
    r"(?:an|a) (?:NDA|BLA) must contain|as part of (?:an|a) (?:NDA|BLA))\b"
)
GENERIC_PDUFA = rx(
    r"\b(review goal|goal dates?|within (?:six|ten|6|10)\s+months|does not always meet|may be extended|or at all)\b"
)
HYPOTHETICAL_SUBMISSION = rx(r"\b(can submit|may submit|would submit|is required to submit|could submit)\b")
HYPOTHETICAL_DESIGNATION = rx(
    r"\b(may seek|plan to seek|seeking|intend to seek|could seek|might seek|"
    r"applied for|plan to apply|may apply|plan to request|may request|considering|"
    r"if (?:we receive|granted|we are granted|it is granted))\b"
)
GENERIC_DESIGNATION_RISK = rx(
    r"\b(not guarantee|no assurance|may not|cannot guarantee|"
    r"there can be no assurance|if we are unable to|if we fail to)\b"
)
PARTNERSHIP_NEGATIVE_OR_GENERIC = rx(
    r"\b(termination of|terminated the|may enter|could enter|would enter|if we enter|"
    r"license termination rights|breach our license agreement)\b"
)
DATE_HINT = rx(
    r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|"
    r"Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s+\d{1,2},\s+\d{4}\b"
    r"|\b\d{1,2}/\d{1,2}/\d{2,4}\b"
    r"|\b\d{4}-\d{2}-\d{2}\b"
)
def latest_docs_cte(target_filings_sql: str) -> str:
    return f"""
WITH target_filings AS (
{target_filings_sql}
),
latest_docs AS (
    SELECT accession_nodash, document_id, document_url, text_hash, text_length
    FROM sec_filing_latest_document
    WHERE COALESCE(text_hash, '') <> ''
)
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Parse SEC filing text into structured biotech events.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--tickers", type=str, default="", help="Optional comma-separated ticker subset.")
    parser.add_argument("--max-filings", type=int, default=0, help="Limit filing texts for smoke tests. 0 means all.")
    parser.add_argument("--offset", type=int, default=0, help="Offset into the eligible filing list for chunked parsing.")
    parser.add_argument("--asof", type=str, default="", help="Parser as-of date in YYYY-MM-DD. Defaults to UTC today.")
    parser.add_argument("--export-only", action="store_true", help="Only export current sec_events table to CSV.")
    parser.add_argument("--full-rescan", action="store_true", help="Parse all eligible filing texts, not just new/changed documents.")
    parser.add_argument("--all-db-companies", action="store_true", help="Parse all SEC filings in the DB window instead of the final scoring universe.")
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


def build_parser_signature(*, min_confidence: float, max_per_type: int) -> str:
    payload = {
        "logic_version": PARSER_LOGIC_VERSION,
        "min_confidence": round(float(min_confidence), 6),
        "max_per_type": int(max_per_type),
        "rules": [
            {
                "event_type": rule.event_type,
                "polarity": rule.polarity,
                "confidence": rule.confidence,
                "patterns": [pattern.pattern for pattern in rule.patterns],
                "value_pattern": rule.value_pattern.pattern if rule.value_pattern else "",
            }
            for rule in RULES
        ],
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def clean_text(text: str) -> str:
    normalized = html.unescape(str(text or ""))
    normalized = TAG_RE.sub(" ", normalized)
    normalized = normalized.replace("\xa0", " ")
    return WHITESPACE_RE.sub(" ", normalized).strip()


def extract_window(text: str, start: int, end: int, radius: int = 320) -> str:
    return clean_text(text[max(0, start - radius) : min(len(text), end + radius)])[:1400]


def extract_value(rule: EventRule, window: str) -> str:
    if rule.value_pattern:
        match = rule.value_pattern.search(window)
        if match:
            date_match = DATE_HINT.search(match.group(0))
            return clean_text(date_match.group(0) if date_match else "")
    date_match = DATE_HINT.search(window)
    if rule.event_type == "pdufa_date" and date_match:
        return clean_text(date_match.group(0))
    return ""


def stale_historical_window(window: str, *, filing_date: str, max_age_years: int = 1) -> bool:
    filing_dt = parse_date(filing_date)
    if filing_dt is None:
        return False
    years = [int(match) for match in re.findall(r"\b(20\d{2})\b", window)]
    return bool(years and max(years) < filing_dt.year - max_age_years)


def stale_event_value_date(event_value: str, *, filing_date: str, max_age_years: int = 1) -> bool:
    filing_dt = parse_date(filing_date)
    if filing_dt is None:
        return False
    years = [int(match) for match in re.findall(r"\b(20\d{2})\b", str(event_value or ""))]
    return bool(years and max(years) < filing_dt.year - max_age_years)


def partial_hold_suppression(match_context: str) -> bool:
    # Suppress only when "partial" directly precedes the matched "clinical hold"
    # (allowing a couple of intervening words); a partial hold mentioned elsewhere
    # in the surrounding window must not mask a genuine full clinical hold event.
    context_lower = match_context.lower()
    hold_idx = context_lower.rfind("clinical hold")
    if hold_idx < 0:
        return False
    preceding = context_lower[max(0, hold_idx - 40) : hold_idx]
    return bool(re.search(r"\bpartial\b(?:\s+\w+){0,2}\s*$", preceding))


def should_skip(rule: EventRule, window: str, *, filing_date: str, match_context: str = "") -> bool:
    if rule.event_type in {"clinical_hold", "partial_clinical_hold"} and NEGATED_HOLD.search(window):
        return True
    if rule.event_type in {"clinical_hold", "partial_clinical_hold"} and GENERIC_RISK_FACTOR.search(window):
        return True
    if rule.event_type in {"clinical_hold", "partial_clinical_hold"} and stale_historical_window(window, filing_date=filing_date):
        return True
    if rule.event_type == "clinical_update_negative" and GENERIC_RISK_FACTOR.search(window):
        return True
    if rule.event_type == "clinical_update_negative" and NEGATED_SAFETY_SIGNAL.search(window):
        return True
    if rule.event_type == "going_concern_confirmed" and NEGATED_GOING_CONCERN.search(window):
        return True
    if rule.event_type == "going_concern_confirmed" and ALLEV_GOING_CONCERN.search(window):
        return True
    if rule.event_type == "clinical_hold" and partial_hold_suppression(match_context):
        return True
    if rule.event_type in {"nda_bla_accepted", "regulatory_submission"} and GENERIC_NDA_BLA.search(window):
        return True
    if rule.event_type == "regulatory_submission" and HYPOTHETICAL_SUBMISSION.search(window):
        return True
    if rule.event_type == "pdufa_date" and GENERIC_PDUFA.search(window) and not DATE_HINT.search(window):
        return True
    if rule.event_type == "partnership_license":
        if PARTNERSHIP_NEGATIVE_OR_GENERIC.search(window):
            return True
        if stale_historical_window(window, filing_date=filing_date):
            return True
    if rule.event_type in {
        "breakthrough_therapy_granted",
        "rmat_granted",
        "fast_track_granted",
        "orphan_drug_granted",
        "priority_review_granted",
    }:
        if HYPOTHETICAL_DESIGNATION.search(window):
            return True
        if GENERIC_DESIGNATION_RISK.search(window):
            return True
        if GENERIC_RISK_FACTOR.search(window):
            return True
    return False


def detect_events(row: FilingText, *, min_confidence: float, max_per_type: int) -> list[SecEvent]:
    scan_text = clean_text(row.text_content)
    if not scan_text.strip():
        return []
    lower_text = scan_text.lower()
    events: list[SecEvent] = []
    seen_windows: set[tuple[str, str]] = set()
    for rule in RULES:
        if rule.confidence < min_confidence:
            continue
        keywords = RULE_KEYWORDS.get(rule.event_type, ())
        if keywords and not any(keyword in lower_text for keyword in keywords):
            continue
        per_type = 0
        for pattern in rule.patterns:
            for match in pattern.finditer(scan_text):
                window = extract_window(scan_text, match.start(), match.end())
                match_context = scan_text[max(0, match.start() - 40) : match.end()]
                if should_skip(rule, window, filing_date=row.filing_date, match_context=match_context):
                    continue
                dedupe_key = (rule.event_type, hashlib.sha256(window.lower().encode("utf-8", errors="ignore")).hexdigest())
                if dedupe_key in seen_windows:
                    continue
                seen_windows.add(dedupe_key)
                event_value = extract_value(rule, window)
                if rule.event_type == "pdufa_date" and not event_value:
                    continue
                if rule.event_type == "pdufa_date" and stale_event_value_date(event_value, filing_date=row.filing_date):
                    continue
                source_payload = json.dumps(
                    {
                        "parser_logic_version": PARSER_LOGIC_VERSION,
                        "pattern": pattern.pattern,
                        "document_url": row.document_url,
                        "match_start": match.start(),
                        "match_end": match.end(),
                    },
                    ensure_ascii=True,
                    sort_keys=True,
                )
                events.append(
                    SecEvent(
                        company_id=row.company_id,
                        ticker=row.ticker,
                        company_name=row.company_name,
                        accession_nodash=row.accession_nodash,
                        filing_date=row.filing_date,
                        form=row.form,
                        event_type=rule.event_type,
                        event_date=event_value if rule.event_type == "pdufa_date" else "",
                        event_value=event_value,
                        polarity=rule.polarity,
                        confidence=rule.confidence,
                        extracted_text=window,
                        document_url=row.document_url,
                        source_payload=source_payload,
                    )
                )
                per_type += 1
                if max_per_type > 0 and per_type >= max_per_type:
                    break
            if max_per_type > 0 and per_type >= max_per_type:
                break
    return events


def filing_text_where(
    *,
    cutoff: str,
    asof: str,
    ticker_filter: set[str],
) -> tuple[list[str], list[Any]]:
    params: list[Any] = [cutoff, asof]
    where = ["f.filing_date >= ?", "f.filing_date <= ?"]
    if ticker_filter:
        placeholders = ",".join("?" for _ in ticker_filter)
        where.append(f"upper(c.ticker) IN ({placeholders})")
        params.extend(sorted(ticker_filter))
    return where, params


def target_filings_sql(where_sql: str) -> str:
    return f"""
    SELECT
        f.company_id,
        c.ticker,
        c.company_name,
        f.accession_nodash,
        f.filing_date,
        f.form,
        COALESCE(f.text_hash, '') AS filing_text_hash
    FROM sec_filings f
    JOIN companies c ON c.company_id = f.company_id
    WHERE {where_sql}
""".strip()


def read_scoring_tickers(path: Path) -> set[str]:
    return read_final_scoring_tickers(path)


def load_target_accessions(
    conn: sqlite3.Connection,
    *,
    cutoff: str,
    asof: str,
    ticker_filter: set[str],
) -> list[str]:
    where, params = filing_text_where(cutoff=cutoff, asof=asof, ticker_filter=ticker_filter)
    where_sql = " AND ".join(where)
    rows = conn.execute(
        f"""
        SELECT f.accession_nodash
        FROM sec_filings f
        JOIN companies c ON c.company_id = f.company_id
        WHERE {where_sql}
        ORDER BY f.filing_date DESC, f.accession_nodash DESC
        """,
        params,
    ).fetchall()
    return [str(row["accession_nodash"] or "") for row in rows if str(row["accession_nodash"] or "")]


def ensure_latest_documents_for_accessions(conn: sqlite3.Connection, accessions: list[str]) -> int:
    if not accessions:
        return 0
    existing: set[str] = set()
    for start in range(0, len(accessions), 800):
        chunk = accessions[start : start + 800]
        placeholders = ",".join("?" for _ in chunk)
        existing.update(
            str(row["accession_nodash"])
            for row in conn.execute(
                f"SELECT accession_nodash FROM sec_filing_latest_document WHERE accession_nodash IN ({placeholders})",
                chunk,
            )
        )
    missing = [accession for accession in accessions if accession not in existing]
    return refresh_sec_latest_documents(conn, missing) if missing else 0


def count_filing_texts(
    conn: sqlite3.Connection,
    *,
    cutoff: str,
    asof: str,
    ticker_filter: set[str],
    incremental_only: bool,
    parser_signature: str,
) -> int:
    where, params = filing_text_where(cutoff=cutoff, asof=asof, ticker_filter=ticker_filter)
    where_sql = " AND ".join(where)
    target_sql = target_filings_sql(where_sql)
    join_clause = (
        "LEFT JOIN sec_event_parse_state s ON s.accession_nodash = f.accession_nodash"
        if incremental_only
        else ""
    )
    incremental_clause = (
        " AND (s.accession_nodash IS NULL OR COALESCE(s.text_hash, '') <> COALESCE(d.text_hash, '') OR COALESCE(s.parser_signature, '') <> ?)"
        if incremental_only
        else ""
    )
    if incremental_only:
        params.append(parser_signature)
    return int(
        conn.execute(
            f"""{latest_docs_cte(target_sql)}
            SELECT COUNT(*)
            FROM target_filings f
            JOIN latest_docs d ON d.accession_nodash = f.accession_nodash
            JOIN sec_filing_documents doc ON doc.document_id = d.document_id
            {join_clause}
            WHERE 1 = 1
              AND doc.text_content IS NOT NULL
              AND doc.text_content <> ''
              {incremental_clause}
            """,
            params,
        ).fetchone()[0]
    )


def load_filing_texts_to_parse(
    conn: sqlite3.Connection,
    *,
    cutoff: str,
    asof: str,
    ticker_filter: set[str],
    max_filings: int,
    offset: int,
    parser_signature: str,
) -> list[FilingText]:
    where, params = filing_text_where(cutoff=cutoff, asof=asof, ticker_filter=ticker_filter)
    where_sql = " AND ".join(where)
    target_sql = target_filings_sql(where_sql)
    sql = f"""{latest_docs_cte(target_sql)},
        eligible_filings AS (
            SELECT
                f.company_id,
                f.ticker,
                f.company_name,
                f.accession_nodash,
                f.filing_date,
                f.form,
                d.document_id,
                d.document_url,
                COALESCE(d.text_hash, '') AS text_hash,
                doc.text_content
            FROM target_filings f
            JOIN latest_docs d ON d.accession_nodash = f.accession_nodash
            JOIN sec_filing_documents doc ON doc.document_id = d.document_id
            LEFT JOIN sec_event_parse_state s ON s.accession_nodash = f.accession_nodash
            WHERE (
                   s.accession_nodash IS NULL
                OR COALESCE(s.text_hash, '') <> COALESCE(d.text_hash, '')
                OR COALESCE(s.parser_signature, '') <> ?
            )
              AND doc.text_content IS NOT NULL
              AND doc.text_content <> ''
            ORDER BY f.filing_date DESC, f.accession_nodash DESC
    """
    params.append(parser_signature)
    if max_filings > 0:
        sql += " LIMIT ? OFFSET ?"
        params.extend([max_filings, max(0, offset)])
    elif offset > 0:
        sql += " LIMIT -1 OFFSET ?"
        params.append(max(0, offset))
    sql += """
        )
        SELECT
            f.company_id,
            f.ticker,
            f.company_name,
            f.accession_nodash,
            f.filing_date,
            f.form,
            f.document_url,
            f.text_hash,
            f.text_content
        FROM eligible_filings f
        ORDER BY f.filing_date DESC, f.accession_nodash DESC
    """
    rows = conn.execute(sql, params).fetchall()
    return [
        FilingText(
            company_id=int(row["company_id"]),
            ticker=str(row["ticker"]).upper(),
            company_name=str(row["company_name"] or ""),
            accession_nodash=str(row["accession_nodash"]),
            filing_date=str(row["filing_date"]),
            form=str(row["form"]),
            document_url=str(row["document_url"] or ""),
            text_hash=str(row["text_hash"] or ""),
            text_content=str(row["text_content"] or ""),
        )
        for row in rows
    ]


def load_filing_texts_full(
    conn: sqlite3.Connection,
    *,
    cutoff: str,
    asof: str,
    ticker_filter: set[str],
    max_filings: int,
    offset: int,
) -> list[FilingText]:
    where, params = filing_text_where(cutoff=cutoff, asof=asof, ticker_filter=ticker_filter)
    where_sql = " AND ".join(where)
    target_sql = target_filings_sql(where_sql)
    sql = f"""{latest_docs_cte(target_sql)},
        eligible_filings AS (
            SELECT
                f.company_id,
                f.ticker,
                f.company_name,
                f.accession_nodash,
                f.filing_date,
                f.form,
                d.document_id,
                d.document_url,
                COALESCE(d.text_hash, '') AS text_hash,
                doc.text_content
            FROM target_filings f
            JOIN latest_docs d ON d.accession_nodash = f.accession_nodash
            JOIN sec_filing_documents doc ON doc.document_id = d.document_id
            WHERE doc.text_content IS NOT NULL
              AND doc.text_content <> ''
            ORDER BY f.filing_date DESC, f.accession_nodash DESC
    """
    if max_filings > 0:
        sql += " LIMIT ? OFFSET ?"
        params.extend([max_filings, max(0, offset)])
    elif offset > 0:
        sql += " LIMIT -1 OFFSET ?"
        params.append(max(0, offset))
    sql += """
        )
        SELECT
            f.company_id,
            f.ticker,
            f.company_name,
            f.accession_nodash,
            f.filing_date,
            f.form,
            f.document_url,
            f.text_hash,
            f.text_content
        FROM eligible_filings f
        ORDER BY f.filing_date DESC, f.accession_nodash DESC
    """
    rows = conn.execute(sql, params).fetchall()
    return [
        FilingText(
            company_id=int(row["company_id"]),
            ticker=str(row["ticker"]).upper(),
            company_name=str(row["company_name"] or ""),
            accession_nodash=str(row["accession_nodash"]),
            filing_date=str(row["filing_date"]),
            form=str(row["form"]),
            document_url=str(row["document_url"] or ""),
            text_hash=str(row["text_hash"] or ""),
            text_content=str(row["text_content"] or ""),
        )
        for row in rows
    ]


def replace_events(conn: sqlite3.Connection, events: list[SecEvent], *, filings: Iterable[FilingText], parser_signature: str) -> None:
    now = utc_now()
    filing_list = [filing for filing in filings if filing.accession_nodash]
    accession_list = sorted({filing.accession_nodash for filing in filing_list})
    event_counts: dict[str, int] = {filing.accession_nodash: 0 for filing in filing_list}
    for event in events:
        event_counts[event.accession_nodash] = event_counts.get(event.accession_nodash, 0) + 1
    with conn:
        if accession_list:
            for accession_chunk in chunked(accession_list):
                placeholders = ",".join("?" for _ in accession_chunk)
                conn.execute(f"DELETE FROM sec_events WHERE accession_nodash IN ({placeholders})", accession_chunk)
        event_params = [
            (
                event.company_id,
                event.accession_nodash,
                event.filing_date,
                event.form,
                event.event_type,
                event.event_date,
                event.event_value,
                event.polarity,
                event.confidence,
                event.extracted_text,
                event.source_payload,
                now,
                now,
            )
            for event in events
        ]
        if event_params:
            conn.executemany(
                """
                INSERT INTO sec_events(
                    company_id, accession_nodash, filing_date, form, event_type, event_date, event_value,
                    polarity, confidence, extracted_text, source_payload, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                event_params,
            )
        state_params = [
            (
                filing.accession_nodash,
                filing.text_hash,
                parser_signature,
                now,
                event_counts.get(filing.accession_nodash, 0),
                now,
                now,
            )
            for filing in filing_list
        ]
        if state_params:
            conn.executemany(
                """
                INSERT INTO sec_event_parse_state(accession_nodash, text_hash, parser_signature, parsed_at, event_count, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(accession_nodash) DO UPDATE SET
                    text_hash = excluded.text_hash,
                    parser_signature = excluded.parser_signature,
                    parsed_at = excluded.parsed_at,
                    event_count = excluded.event_count,
                    updated_at = excluded.updated_at
                """,
                state_params,
            )


def parse_filing_batch(
    conn: sqlite3.Connection,
    filings: list[FilingText],
    *,
    min_confidence: float,
    max_per_type: int,
    max_workers: int,
    parser_signature: str,
) -> int:
    if not filings:
        return 0
    events_out: list[SecEvent] = []
    if max_workers <= 1:
        for filing in filings:
            events_out.extend(detect_events(filing, min_confidence=min_confidence, max_per_type=max_per_type))
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(detect_events, filing, min_confidence=min_confidence, max_per_type=max_per_type): filing
                for filing in filings
            }
            pending_raise: BaseException | None = None
            for future in as_completed(futures):
                filing = futures[future]
                try:
                    events_out.extend(future.result())
                except BaseException as exc:
                    pending_raise = exc
                    if isinstance(exc, (SystemExit, KeyboardInterrupt, GeneratorExit)):
                        LOGGER.warning(
                            "SEC event parser worker interrupted for accession=%s ticker=%s",
                            filing.accession_nodash,
                            filing.ticker,
                        )
                    else:
                        LOGGER.exception(
                            "SEC event parser worker failed for accession=%s ticker=%s",
                            filing.accession_nodash,
                            filing.ticker,
                        )
                    for other in futures:
                        if other is not future:
                            other.cancel()
                    break
            if pending_raise is not None:
                raise pending_raise
    replace_events(conn, events_out, filings=filings, parser_signature=parser_signature)
    return len(events_out)


def write_csv(path: Path, events: list[SecEvent]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        for event in events:
            writer.writerow(
                {
                    "ticker": event.ticker,
                    "company_name": event.company_name,
                    "accession_nodash": event.accession_nodash,
                    "filing_date": event.filing_date,
                    "form": event.form,
                    "event_type": event.event_type,
                    "event_date": event.event_date,
                    "event_value": event.event_value,
                    "polarity": event.polarity,
                    "confidence": event.confidence,
                    "extracted_text": event.extracted_text,
                    "document_url": event.document_url,
                }
            )


def write_csv_header(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS, lineterminator="\n", extrasaction="ignore").writeheader()


def append_csv(path: Path, events: list[SecEvent]) -> None:
    if not events:
        return
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS, lineterminator="\n", extrasaction="ignore")
        for event in events:
            writer.writerow(
                {
                    "ticker": event.ticker,
                    "company_name": event.company_name,
                    "accession_nodash": event.accession_nodash,
                    "filing_date": event.filing_date,
                    "form": event.form,
                    "event_type": event.event_type,
                    "event_date": event.event_date,
                    "event_value": event.event_value,
                    "polarity": event.polarity,
                    "confidence": event.confidence,
                    "extracted_text": event.extracted_text,
                    "document_url": event.document_url,
                }
            )


def export_events_from_db(
    conn: sqlite3.Connection,
    output_csv: Path,
    *,
    cutoff: str = "",
    asof: str = "",
    ticker_filter: set[str] | None = None,
) -> int:
    row_filters: list[str] = []
    row_params: list[Any] = []
    if cutoff:
        row_filters.append("e.filing_date >= ?")
        row_params.append(cutoff)
    if asof:
        row_filters.append("e.filing_date <= ?")
        row_params.append(asof)
    if ticker_filter:
        placeholders = ",".join("?" for _ in ticker_filter)
        row_filters.append(f"e.company_id IN (SELECT company_id FROM companies WHERE upper(ticker) IN ({placeholders}))")
        row_params.extend(sorted(ticker_filter))
    row_where = f" WHERE {' AND '.join(row_filters)}" if row_filters else ""
    cursor = conn.execute(
        f"""
        WITH event_rows AS (
            SELECT
                c.ticker,
                c.company_name,
                e.accession_nodash,
                e.filing_date,
                e.form,
                e.event_type,
                e.event_date,
                e.event_value,
                e.polarity,
                e.confidence,
                e.extracted_text
            FROM sec_events e
            JOIN companies c ON c.company_id = e.company_id
            {row_where}
        ),
        doc_urls AS (
            SELECT accession_nodash, document_url
            FROM (
                SELECT
                    d.accession_nodash,
                    d.document_url,
                    ROW_NUMBER() OVER (
                        PARTITION BY d.accession_nodash
                        ORDER BY
                            CASE WHEN d.document_type = 'complete_submission_text' THEN 0 ELSE 1 END,
                            COALESCE(d.fetched_at, d.updated_at, d.created_at) DESC,
                            d.document_id DESC
                    ) AS rn
                FROM sec_filing_documents d
                JOIN (SELECT DISTINCT accession_nodash FROM event_rows) a
                  ON a.accession_nodash = d.accession_nodash
            )
            WHERE rn = 1
        )
        SELECT
            e.ticker,
            e.company_name,
            e.accession_nodash,
            e.filing_date,
            e.form,
            e.event_type,
            e.event_date,
            e.event_value,
            e.polarity,
            e.confidence,
            e.extracted_text,
            COALESCE(d.document_url, f.archive_url, '') AS document_url
        FROM event_rows e
        LEFT JOIN doc_urls d ON d.accession_nodash = e.accession_nodash
        LEFT JOIN sec_filings f ON f.accession_nodash = e.accession_nodash
        ORDER BY e.ticker, e.filing_date, e.event_type, e.accession_nodash
        """,
        row_params,
    )
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    row_count = 0
    with output_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        for row in cursor:
            writer.writerow(
                {
                    "ticker": str(row["ticker"] or ""),
                    "company_name": str(row["company_name"] or ""),
                    "accession_nodash": str(row["accession_nodash"] or ""),
                    "filing_date": str(row["filing_date"] or ""),
                    "form": str(row["form"] or ""),
                    "event_type": str(row["event_type"] or ""),
                    "event_date": str(row["event_date"] or ""),
                    "event_value": str(row["event_value"] or ""),
                    "polarity": str(row["polarity"] or ""),
                    "confidence": float(row["confidence"] or 0.0),
                    "extracted_text": str(row["extracted_text"] or ""),
                    "document_url": str(row["document_url"] or ""),
                }
            )
            row_count += 1
    return row_count


def main() -> None:
    configure_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    asof = parse_date(args.asof) if args.asof else datetime.now(timezone.utc).date()
    if asof is None:
        raise ValueError(f"Invalid --asof date: {args.asof}")
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_csv = resolve_path(cfg_get(config, "sec_event_parser.output_csv"), base_dir=base_dir)
    final_universe_value = cfg_get(
        config,
        "sec_event_parser.final_scoring_universe_csv",
        cfg_get(config, "sec_filings.final_scoring_universe_csv"),
    )
    final_universe_csv = resolve_path(final_universe_value, base_dir=base_dir) if final_universe_value else None
    lookback_days = int(cfg_get(config, "sec_event_parser.lookback_days", 730))
    asof_str = asof.isoformat()
    cutoff = (asof - timedelta(days=max(1, lookback_days))).isoformat()
    min_confidence = float(cfg_get(config, "sec_event_parser.min_confidence", 0.65))
    max_per_type = int(cfg_get(config, "sec_event_parser.max_events_per_filing_type", 1))
    parser_signature = build_parser_signature(min_confidence=min_confidence, max_per_type=max_per_type)
    ticker_filter = {normalize_ticker(x) for x in args.tickers.split(",") if normalize_ticker(x)}
    scope_label = "tickers_arg" if ticker_filter else "all_db_companies"
    if not ticker_filter and not args.all_db_companies:
        if final_universe_csv is None:
            raise ValueError("sec_event_parser.final_scoring_universe_csv or sec_filings.final_scoring_universe_csv is required.")
        ticker_filter = read_scoring_tickers(final_universe_csv)
        scope_label = "final_scoring_universe"

    run_id: int | None = None
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        run_type = "parse_sec_biotech_events_export" if args.export_only else "parse_sec_biotech_events"
        try:
            run_id = start_run(conn, run_type=run_type, input_path=db_path)
            target_accessions = load_target_accessions(conn, cutoff=cutoff, asof=asof_str, ticker_filter=ticker_filter)
            refreshed_manifest = ensure_latest_documents_for_accessions(conn, target_accessions)
            LOGGER.info(
                "SEC event parser scope=%s target_tickers=%d target_filings=%d latest_manifest_refreshed=%d",
                scope_label,
                len(ticker_filter),
                len(target_accessions),
                refreshed_manifest,
            )
            if args.export_only:
                row_count = export_events_from_db(conn, output_csv, cutoff=cutoff, asof=asof_str, ticker_filter=ticker_filter)
                finish_run(conn, run_id=run_id, status="success", row_count=row_count, message=f"output={output_csv}")
                LOGGER.info("Exported SEC biotech events: rows=%d output=%s", row_count, output_csv)
                return
            if not target_accessions:
                raise RuntimeError(
                    f"SEC event parser selected zero target filings for scope={scope_label} "
                    f"asof={asof_str}; refusing to continue"
                )
            incremental_only = not args.full_rescan
            total_available = count_filing_texts(
                conn,
                cutoff=cutoff,
                asof=asof_str,
                ticker_filter=ticker_filter,
                incremental_only=incremental_only,
                parser_signature=parser_signature,
            )
            batch_size = max(1, int(cfg_get(config, "sec_event_parser.batch_size", 250)))
            max_workers = max(1, int(cfg_get(config, "sec_event_parser.max_workers", 1)))
            total_filings = 0
            event_count = 0
            explicit_chunk = int(args.max_filings) > 0 or int(args.offset) > 0
            if not explicit_chunk:
                LOGGER.info(
                    "SEC event parser eligible filings=%d mode=%s",
                    total_available,
                    "incremental" if incremental_only else "full_rescan",
                )
            else:
                filings = (
                    load_filing_texts_to_parse(
                        conn,
                        cutoff=cutoff,
                        asof=asof_str,
                        ticker_filter=ticker_filter,
                        max_filings=int(args.max_filings),
                        offset=int(args.offset),
                        parser_signature=parser_signature,
                    )
                    if incremental_only
                    else load_filing_texts_full(
                        conn,
                        cutoff=cutoff,
                        asof=asof_str,
                        ticker_filter=ticker_filter,
                        max_filings=int(args.max_filings),
                        offset=int(args.offset),
                    )
                )
                total_filings = len(filings)
                LOGGER.info(
                    "SEC event parser chunk offset=%d limit=%d filings=%d eligible_total=%d mode=%s",
                    int(args.offset),
                    int(args.max_filings),
                    total_filings,
                    total_available,
                    "incremental" if incremental_only else "full_rescan",
                )
                event_count += parse_filing_batch(
                    conn,
                    filings,
                    min_confidence=min_confidence,
                    max_per_type=max_per_type,
                    max_workers=max_workers,
                    parser_signature=parser_signature,
                )
            if not explicit_chunk and total_available > 0:
                page_offset = 0
                processed_incremental_accessions: set[str] = set()
                while True:
                    filings = (
                        load_filing_texts_to_parse(
                            conn,
                            cutoff=cutoff,
                            asof=asof_str,
                            ticker_filter=ticker_filter,
                            max_filings=batch_size,
                            offset=0,
                            parser_signature=parser_signature,
                        )
                        if incremental_only
                        else load_filing_texts_full(
                            conn,
                            cutoff=cutoff,
                            asof=asof_str,
                            ticker_filter=ticker_filter,
                            max_filings=batch_size,
                            offset=page_offset,
                        )
                    )
                    if not filings:
                        break
                    batch_accessions = {filing.accession_nodash for filing in filings}
                    if incremental_only and batch_accessions and batch_accessions.issubset(processed_incremental_accessions):
                        raise RuntimeError(
                            "SEC event parser made no incremental progress; refusing to reprocess the same filing batch"
                        )
                    parsed_events = parse_filing_batch(
                        conn,
                        filings,
                        min_confidence=min_confidence,
                        max_per_type=max_per_type,
                        max_workers=max_workers,
                        parser_signature=parser_signature,
                    )
                    total_filings += len(filings)
                    event_count += parsed_events
                    if incremental_only:
                        processed_incremental_accessions.update(batch_accessions)
                    if total_filings % max(500, batch_size) == 0 or total_filings >= total_available:
                        LOGGER.info(
                            "Parsed %d/%d SEC filing texts; events=%d max_workers=%d",
                            total_filings,
                            total_available,
                            event_count,
                            max_workers,
                        )
                    if not incremental_only:
                        page_offset += batch_size
            output_rows = export_events_from_db(conn, output_csv, cutoff=cutoff, asof=asof_str, ticker_filter=ticker_filter)
            finish_run(
                conn,
                run_id=run_id,
                status="success",
                row_count=output_rows,
                message=f"filings={total_filings} parsed_events={event_count} output={output_csv}",
            )
            LOGGER.info("Parsed SEC biotech events: filings=%d events=%d output=%s", total_filings, event_count, output_csv)
        except BaseException as exc:
            if run_id is not None and not (isinstance(exc, SystemExit) and exc.code in (0, None)):
                finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()
