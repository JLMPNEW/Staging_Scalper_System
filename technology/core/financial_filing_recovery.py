from __future__ import annotations

import hashlib
import html
import json
import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from dedicated_parser.catalog import accession_directory, build_document_refs
from dedicated_parser.contracts import FilingRef
from dedicated_parser.semantic import SemanticBlock, parse_semantic_document
from dedicated_parser.storage import catalog_documents


RECOVERY_VERSION = "technology_financial_filing_recovery_v1"
CORE_METRICS = frozenset(
    {
        "assets",
        "cash_and_equivalents",
        "equity",
        "gross_profit",
        "net_income",
        "operating_cash_flow",
        "operating_income",
        "revenue",
    }
)
SUPPORTED_DOCUMENT_SUFFIXES = frozenset({".htm", ".html", ".xhtml", ".xml"})
MAX_DOCUMENT_BYTES = 12 * 1024 * 1024

ATTR_RE = re.compile(
    r"([A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)"
)
CONTEXT_RE = re.compile(
    r"<(?:[A-Za-z0-9_-]+:)?context\b(?P<attrs>[^>]*)>"
    r"(?P<body>.*?)</(?:[A-Za-z0-9_-]+:)?context>",
    re.IGNORECASE | re.DOTALL,
)
UNIT_RE = re.compile(
    r"<(?:[A-Za-z0-9_-]+:)?unit\b(?P<attrs>[^>]*)>"
    r"(?P<body>.*?)</(?:[A-Za-z0-9_-]+:)?unit>",
    re.IGNORECASE | re.DOTALL,
)
NON_FRACTION_RE = re.compile(
    r"<ix:nonfraction\b(?P<attrs>[^>]*)>"
    r"(?P<body>.*?)</ix:nonfraction>",
    re.IGNORECASE | re.DOTALL,
)
TAG_RE = re.compile(r"<[^>]+>")
NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")
RESULTS_PATTERN = re.compile(
    r"\b(?:financial results|results of operations|unaudited results|"
    r"consolidated statements?|consolidated results|presentation of operations|"
    r"three months ended|six months ended|nine months ended|"
    r"quarter ended|year ended|[1-4]Q\d{2,4})\b",
    re.IGNORECASE,
)
EXPLICIT_OCF_RE = re.compile(
    r"(?P<label>cash flow from operating activities|"
    r"net cash (?:provided by|used in) operating activities)"
    r"(?P<period>[^.]{0,180}?)\b(?:was|were)\s+"
    r"(?P<open>\()?\s*(?P<currency>US\$|USD|NT\$|EUR|\$)?\s*"
    r"(?P<amount>\d[\d,]*(?:\.\d+)?)\s*(?P<close>\))?\s*"
    r"(?P<scale>thousand|million|billion)?",
    re.IGNORECASE,
)
TABLE_LABELS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^(?:total )?(?:net )?revenues?$", re.IGNORECASE), "revenue"),
    (re.compile(r"^(?:total )?net sales$", re.IGNORECASE), "revenue"),
    (re.compile(r"^gross profit$", re.IGNORECASE), "gross_profit"),
    (
        re.compile(
            r"^(?:operating (?:income|profit|loss)(?: \(loss\))?|"
            r"income \(loss\) from operations|income from operations)$",
            re.IGNORECASE,
        ),
        "operating_income",
    ),
    (
        re.compile(
            r"^(?:net (?:income|profit|loss)(?: \(loss\))?|profit \(loss\))$",
            re.IGNORECASE,
        ),
        "net_income",
    ),
    (re.compile(r"^total assets$", re.IGNORECASE), "assets"),
    (re.compile(r"^cash and cash equivalents$", re.IGNORECASE), "cash_and_equivalents"),
    (
        re.compile(
            r"^total (?:shareholders'?|stockholders'?|owners'?) equity$|^total equity$",
            re.IGNORECASE,
        ),
        "equity",
    ),
    (
        re.compile(
            r"^(?:net )?cash flow (?:from|used in) operating activities$|"
            r"^net cash (?:provided by|used in|provided by \(used in\)) "
            r"operating activities$",
            re.IGNORECASE,
        ),
        "operating_cash_flow",
    ),
)
STANDARD_CONCEPTS: Mapping[str, Mapping[str, str]] = {
    "us-gaap": {
        "revenue": "Revenues",
        "gross_profit": "GrossProfit",
        "operating_income": "OperatingIncomeLoss",
        "net_income": "NetIncomeLoss",
        "assets": "Assets",
        "cash_and_equivalents": "CashAndCashEquivalentsAtCarryingValue",
        "equity": "StockholdersEquity",
        "operating_cash_flow": "NetCashProvidedByUsedInOperatingActivities",
    },
    "ifrs-full": {
        "revenue": "Revenue",
        "gross_profit": "GrossProfit",
        "operating_income": "ProfitLossFromOperatingActivities",
        "net_income": "ProfitLoss",
        "assets": "Assets",
        "cash_and_equivalents": "CashAndCashEquivalents",
        "equity": "Equity",
        "operating_cash_flow": "CashFlowsFromUsedInOperatingActivities",
    },
}
INSTANT_METRICS = frozenset({"assets", "cash_and_equivalents", "equity"})


@dataclass(frozen=True)
class RecoveredFact:
    taxonomy: str
    concept: str
    unit: str
    value: float
    start_date: str
    end_date: str
    period_type: str
    frame: str
    source_document: str
    source_detail: str
    content_sha256: str
    precision_scale: float = 1.0


def _attributes(raw: str) -> dict[str, str]:
    output: dict[str, str] = {}
    for match in ATTR_RE.finditer(raw or ""):
        value = match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        output[match.group(1).lower()] = html.unescape(value)
    return output


def _clean_text(raw: str) -> str:
    without_exclusions = re.sub(
        r"<ix:exclude\b.*?</ix:exclude>",
        "",
        raw or "",
        flags=re.IGNORECASE | re.DOTALL,
    )
    return " ".join(html.unescape(TAG_RE.sub("", without_exclusions)).replace("\xa0", " ").split())


def _date_text(raw: str) -> str:
    value = _clean_text(raw)[:10]
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return ""


def _contexts(document_text: str) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for match in CONTEXT_RE.finditer(document_text):
        attributes = _attributes(match.group("attrs"))
        context_id = attributes.get("id", "").strip()
        if not context_id:
            continue
        body = match.group("body")
        start_match = re.search(
            r"<(?:[A-Za-z0-9_-]+:)?startdate[^>]*>(.*?)"
            r"</(?:[A-Za-z0-9_-]+:)?startdate>",
            body,
            re.IGNORECASE | re.DOTALL,
        )
        end_match = re.search(
            r"<(?:[A-Za-z0-9_-]+:)?enddate[^>]*>(.*?)"
            r"</(?:[A-Za-z0-9_-]+:)?enddate>",
            body,
            re.IGNORECASE | re.DOTALL,
        )
        instant_match = re.search(
            r"<(?:[A-Za-z0-9_-]+:)?instant[^>]*>(.*?)"
            r"</(?:[A-Za-z0-9_-]+:)?instant>",
            body,
            re.IGNORECASE | re.DOTALL,
        )
        start = _date_text(start_match.group(1)) if start_match else ""
        end = _date_text(end_match.group(1)) if end_match else ""
        instant = _date_text(instant_match.group(1)) if instant_match else ""
        output[context_id] = {
            "start_date": start,
            "end_date": end or instant,
            "period_type": "duration" if start and end else "instant",
            "has_dimensions": bool(
                re.search(
                    r"<(?:[A-Za-z0-9_-]+:)?(?:explicitmember|typedmember)\b",
                    body,
                    re.IGNORECASE,
                )
            ),
        }
    return output


def _units(document_text: str) -> dict[str, str]:
    output: dict[str, str] = {}
    for match in UNIT_RE.finditer(document_text):
        attributes = _attributes(match.group("attrs"))
        unit_id = attributes.get("id", "").strip()
        if not unit_id:
            continue
        measures = re.findall(
            r"<(?:[A-Za-z0-9_-]+:)?measure[^>]*>(.*?)"
            r"</(?:[A-Za-z0-9_-]+:)?measure>",
            match.group("body"),
            re.IGNORECASE | re.DOTALL,
        )
        measure = _clean_text(measures[0]) if measures else unit_id
        upper = measure.upper()
        if upper.startswith("ISO4217:"):
            output[unit_id] = upper.split(":", 1)[1]
        elif upper.endswith(":SHARES") or upper == "SHARES":
            output[unit_id] = "shares"
        elif upper.endswith(":PURE") or upper == "PURE":
            output[unit_id] = "pure"
        else:
            output[unit_id] = measure
    return output


def _inline_number(raw_html: str, attributes: Mapping[str, str]) -> float | None:
    if str(attributes.get("xsi:nil") or "").lower() in {"1", "true"}:
        return None
    text = _clean_text(raw_html)
    if not text or text.lower() in {"-", "--", "n/a", "na"}:
        return None
    negative = "(" in text and ")" in text
    normalized = re.sub(r"[$,%()\s]", "", text)
    match = NUMBER_RE.search(normalized)
    if match is None:
        return None
    try:
        value = float(match.group(0))
        scale = int(str(attributes.get("scale") or "0"))
    except ValueError:
        return None
    if negative or str(attributes.get("sign") or "").strip().startswith("-"):
        value = -abs(value)
    value *= 10**scale
    return value if math.isfinite(value) else None


def parse_inline_document_set(
    documents: Iterable[Path],
) -> list[RecoveredFact]:
    texts: dict[Path, str] = {}
    contexts: dict[str, dict[str, Any]] = {}
    units: dict[str, str] = {}
    for path in documents:
        if path.suffix.lower() not in SUPPORTED_DOCUMENT_SUFFIXES:
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if not raw or len(raw) > MAX_DOCUMENT_BYTES:
            continue
        text = raw.decode("utf-8", errors="replace")
        texts[path] = text
        contexts.update(_contexts(text))
        units.update(_units(text))

    output: list[RecoveredFact] = []
    seen: set[tuple[Any, ...]] = set()
    for path, text in texts.items():
        payload_hash = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
        for match in NON_FRACTION_RE.finditer(text):
            attributes = _attributes(match.group("attrs"))
            name = str(attributes.get("name") or "").strip()
            context_ref = str(attributes.get("contextref") or "").strip()
            context = contexts.get(context_ref)
            if ":" not in name or context is None or context["has_dimensions"]:
                continue
            end_date = str(context.get("end_date") or "")
            value = _inline_number(match.group("body"), attributes)
            if not end_date or value is None:
                continue
            taxonomy, concept = name.split(":", 1)
            unit_ref = str(attributes.get("unitref") or "").strip()
            key = (
                taxonomy,
                concept,
                units.get(unit_ref, unit_ref),
                str(context.get("start_date") or ""),
                end_date,
                context_ref,
                value,
            )
            if key in seen:
                continue
            seen.add(key)
            output.append(
                RecoveredFact(
                    taxonomy=taxonomy,
                    concept=concept,
                    unit=units.get(unit_ref, unit_ref),
                    value=value,
                    start_date=str(context.get("start_date") or ""),
                    end_date=end_date,
                    period_type=str(context.get("period_type") or ""),
                    frame=f"inline_context:{context_ref}:{path.name}",
                    source_document=path.name,
                    source_detail="filing_document_inline_xbrl",
                    content_sha256=payload_hash,
                )
            )
    return output


def _metric_for_label(raw: str) -> str:
    label = " ".join(raw.replace("\xa0", " ").split()).strip(" :")
    label = re.sub(r"\s*\(\d+\)\s*$", "", label)
    if re.search(r"\b(?:non-gaap|non-ifrs|adjusted)\b", label, re.IGNORECASE):
        return ""
    for pattern, metric in TABLE_LABELS:
        if pattern.fullmatch(label):
            return metric
    return ""


def _scale(context: str) -> float:
    match = re.search(
        r"\b(?:in|figures? (?:are|in)|unit\s*[:\-]?)\s+"
        r"(?:(?:U[.]?S[.]?\s+)?dollars?|US\$|NT\$|USD|EUR)?\s*"
        r"(thousands?|millions?|billions?)\b",
        context,
        re.IGNORECASE,
    )
    if match is None:
        return 1.0
    token = match.group(1).lower()
    if token.startswith("thousand"):
        return 1_000.0
    if token.startswith("million"):
        return 1_000_000.0
    return 1_000_000_000.0


def _currency(context: str, fallback: str) -> str:
    if re.search(r"\b(?:NT\$|NTD|new Taiwan dollars?)\b", context, re.IGNORECASE):
        return "TWD"
    if re.search(r"\b(?:US\$|USD|U[.]?S[.]? dollars?)\b", context, re.IGNORECASE):
        return "USD"
    if re.search(r"(?:€|\bEUR\b|euros?)", context, re.IGNORECASE):
        return "EUR"
    return fallback if re.fullmatch(r"[A-Z]{3}", fallback) else ""


def _duration_months(context: str) -> int:
    if re.search(
        r"\b(?:three months|quarter) ended\b|\bQ[1-4]\s+20\d{2}\b|"
        r"\b[1-4]Q\d{2,4}\b",
        context,
        re.IGNORECASE,
    ):
        return 3
    if re.search(r"\bsix months ended\b", context, re.IGNORECASE):
        return 6
    if re.search(r"\bnine months ended\b", context, re.IGNORECASE):
        return 9
    if re.search(r"\b(?:twelve months|year) ended\b", context, re.IGNORECASE):
        return 12
    return 0


def _period_start(end_date: str, months: int) -> str:
    end = date.fromisoformat(end_date)
    zero_based = end.month - months
    start_year = end.year + zero_based // 12
    start_month = zero_based % 12 + 1
    return date(start_year, start_month, 1).isoformat()


MONTH_TOKEN = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
    r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?)"
)
PERIOD_DATE_RE = re.compile(
    rf"\b(?:ended|ending|as\s+of)\s*[:|,-]*\s*"
    rf"(?P<date>(?:{MONTH_TOKEN}\s+\d{{1,2}},?\s+20\d{{2}}|"
    rf"\d{{1,2}}[-/ ]{MONTH_TOKEN}[-/ ]\d{{2,4}}|"
    rf"20\d{{2}}[-/]\d{{1,2}}[-/]\d{{1,2}}))\b",
    re.IGNORECASE,
)
QUARTER_TOKEN_RE = re.compile(
    r"\b(?:Q(?P<q1>[1-4])|(?P<q2>[1-4])Q)\s*'?(?P<year>\d{2,4})\b",
    re.IGNORECASE,
)


def _parse_disclosure_date(raw: str) -> date | None:
    value = " ".join(str(raw or "").replace("/", "-").split()).strip(" ,")
    for pattern in ("%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y"):
        try:
            return datetime.strptime(value, pattern).date()
        except ValueError:
            pass
    match = re.fullmatch(
        rf"(?P<day>\d{{1,2}})-(?P<month>{MONTH_TOKEN})-(?P<year>\d{{2,4}})",
        value,
        re.IGNORECASE,
    )
    if match is not None:
        year_text = match.group("year")
        year = int(year_text) + (2000 if len(year_text) == 2 else 0)
        try:
            month = datetime.strptime(match.group("month")[:3], "%b").month
            return date(year, month, int(match.group("day")))
        except ValueError:
            return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _financial_period_end(context: str, fallback_report_date: str) -> str:
    """Resolve issuer-stated period end without treating the 6-K event date as data."""
    try:
        fallback = date.fromisoformat(fallback_report_date)
    except ValueError:
        return ""
    explicit_dates = [
        parsed
        for match in PERIOD_DATE_RE.finditer(context)
        if (parsed := _parse_disclosure_date(match.group("date"))) is not None
        and parsed <= fallback
    ]
    if explicit_dates:
        return max(explicit_dates).isoformat()
    quarter_dates: list[date] = []
    for match in QUARTER_TOKEN_RE.finditer(context):
        quarter = int(match.group("q1") or match.group("q2"))
        year_text = match.group("year")
        year = int(year_text) + (2000 if len(year_text) == 2 else 0)
        month = quarter * 3
        candidate = date(year, month, {3: 31, 6: 30, 9: 30, 12: 31}[month])
        age_days = (fallback - candidate).days
        if 0 <= age_days <= 120:
            quarter_dates.append(candidate)
    return max(quarter_dates).isoformat() if quarter_dates else fallback.isoformat()


def _first_numeric(cells: Iterable[str]) -> tuple[float, int] | None:
    text = " ".join(str(cell or "") for cell in cells)
    pattern = re.compile(r"(?P<open>\()?\s*(?:[$€£]|NT\$|US\$)?\s*(?P<number>\d[\d,]*(?:\.\d+)?)\s*(?P<close>\))?")
    match = pattern.search(text)
    if match is None:
        return None
    value = float(match.group("number").replace(",", ""))
    if match.group("open") or match.group("close"):
        value = -abs(value)
    return value, match.start()


def _table_groups(blocks: Iterable[SemanticBlock]) -> dict[int, list[SemanticBlock]]:
    output: dict[int, list[SemanticBlock]] = {}
    for block in blocks:
        if block.kind == "table_row" and block.table_id is not None:
            output.setdefault(block.table_id, []).append(block)
    return output


def parse_explicit_statement_tables(
    documents: Iterable[Path],
    *,
    report_date: str,
    taxonomy: str,
    fallback_currency: str,
) -> list[RecoveredFact]:
    if taxonomy not in STANDARD_CONCEPTS:
        return []
    try:
        date.fromisoformat(report_date)
    except ValueError:
        return []
    candidates: dict[tuple[str, str, str], RecoveredFact] = {}
    for path in documents:
        if path.suffix.lower() not in {".htm", ".html", ".xhtml"}:
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if not raw or len(raw) > MAX_DOCUMENT_BYTES:
            continue
        text = raw.decode("utf-8", errors="replace")
        document = parse_semantic_document(text, source_document=path.name)
        document_text = " ".join(block.text for block in document.blocks)
        if RESULTS_PATTERN.search(document_text) is None:
            continue
        content_hash = hashlib.sha256(raw).hexdigest()
        for table_id, rows in _table_groups(document.blocks).items():
            table_context = " | ".join(
                [
                    rows[0].preamble_text if rows else "",
                    *(" | ".join(row.header_cells) for row in rows[:8]),
                    *(row.text for row in rows[:8]),
                ]
            )
            if RESULTS_PATTERN.search(table_context) is None:
                continue
            financial_period_end = _financial_period_end(table_context, report_date)
            if not financial_period_end:
                continue
            scale = _scale(table_context + " " + document_text[:3000])
            currency = _currency(table_context, fallback_currency)
            if not currency:
                continue
            duration_months = _duration_months(table_context)
            for row in rows:
                nonempty = [cell.strip() for cell in row.cells if cell.strip()]
                if len(nonempty) < 2:
                    continue
                metric = _metric_for_label(nonempty[0])
                if not metric:
                    continue
                parsed = _first_numeric(nonempty[1:])
                if parsed is None:
                    continue
                raw_value, _ = parsed
                if metric in INSTANT_METRICS:
                    start_date = ""
                    period_type = "instant"
                else:
                    if duration_months == 0:
                        continue
                    start_date = _period_start(financial_period_end, duration_months)
                    period_type = "duration"
                fact = RecoveredFact(
                    taxonomy=taxonomy,
                    concept=STANDARD_CONCEPTS[taxonomy][metric],
                    unit=currency,
                    value=raw_value * scale,
                    start_date=start_date,
                    end_date=financial_period_end,
                    period_type=period_type,
                    frame=f"explicit_table:{path.name}:{table_id}:{row.row_index}",
                    source_document=path.name,
                    source_detail="filing_document_explicit_statement_table",
                    content_sha256=content_hash,
                    precision_scale=scale,
                )
                key = (metric, start_date, financial_period_end)
                prior = candidates.get(key)
                if prior is None or fact.precision_scale < prior.precision_scale:
                    candidates[key] = fact
    return list(candidates.values())


def parse_explicit_financial_prose(
    documents: Iterable[Path],
    *,
    report_date: str,
    taxonomy: str,
    fallback_currency: str,
) -> list[RecoveredFact]:
    if taxonomy not in STANDARD_CONCEPTS:
        return []
    try:
        date.fromisoformat(report_date)
    except ValueError:
        return []
    quarter_tokens = {
        1: r"(?:first quarter|Q1|1Q)",
        2: r"(?:second quarter|Q2|2Q)",
        3: r"(?:third quarter|Q3|3Q)",
        4: r"(?:fourth quarter|Q4|4Q)",
    }
    scale_values = {
        "thousand": 1_000.0,
        "million": 1_000_000.0,
        "billion": 1_000_000_000.0,
    }
    candidates: dict[tuple[str, str], RecoveredFact] = {}
    for path in documents:
        if path.suffix.lower() not in {".htm", ".html", ".xhtml"}:
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if not raw or len(raw) > MAX_DOCUMENT_BYTES:
            continue
        document = parse_semantic_document(
            raw.decode("utf-8", errors="replace"),
            source_document=path.name,
        )
        document_text = " ".join(block.text for block in document.blocks)
        financial_period_end = _financial_period_end(document_text, report_date)
        if not financial_period_end:
            continue
        period_end = date.fromisoformat(financial_period_end)
        quarter = (period_end.month - 1) // 3 + 1
        period_pattern = re.compile(
            rf"\b{quarter_tokens[quarter]}(?:\s+of)?\s+{period_end.year}\b",
            re.IGNORECASE,
        )
        content_hash = hashlib.sha256(raw).hexdigest()
        for match in EXPLICIT_OCF_RE.finditer(document_text):
            period_text = str(match.group("period") or "")
            if period_pattern.search(period_text) is None:
                continue
            amount = float(str(match.group("amount") or "0").replace(",", ""))
            scale = scale_values.get(str(match.group("scale") or "").lower(), 1.0)
            label = str(match.group("label") or "").lower()
            if match.group("open") or match.group("close") or "used in" in label:
                amount = -abs(amount)
            unit_token = str(match.group("currency") or "").upper()
            if unit_token in {"US$", "USD", "$"}:
                unit = "USD" if unit_token != "$" else _currency(document_text[:3000], fallback_currency)
            elif unit_token == "NT$":
                unit = "TWD"
            elif unit_token == "EUR":
                unit = "EUR"
            else:
                unit = _currency(document_text[:3000], fallback_currency)
            if not unit:
                continue
            fact = RecoveredFact(
                taxonomy=taxonomy,
                concept=STANDARD_CONCEPTS[taxonomy]["operating_cash_flow"],
                unit=unit,
                value=amount * scale,
                start_date=_period_start(financial_period_end, 3),
                end_date=financial_period_end,
                period_type="duration",
                frame=f"explicit_prose:{path.name}:operating_cash_flow",
                source_document=path.name,
                source_detail="filing_document_explicit_financial_prose",
                content_sha256=content_hash,
                precision_scale=scale,
            )
            key = (fact.concept, fact.end_date)
            prior = candidates.get(key)
            if prior is None or fact.precision_scale < prior.precision_scale:
                candidates[key] = fact
    return list(candidates.values())


def _fact_key(
    ticker: str,
    accession: str,
    fact: RecoveredFact,
) -> str:
    payload = {
        "version": RECOVERY_VERSION,
        "ticker": ticker,
        "accession": accession,
        "taxonomy": fact.taxonomy,
        "concept": fact.concept,
        "unit": fact.unit,
        "start": fact.start_date,
        "end": fact.end_date,
        "frame": fact.frame,
        "value": fact.value,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _mapped_metrics(
    conn: sqlite3.Connection,
    facts: Iterable[RecoveredFact],
) -> set[str]:
    pairs = {(fact.taxonomy, fact.concept) for fact in facts}
    if not pairs:
        return set()
    output: set[str] = set()
    for taxonomy, concept in pairs:
        rows = conn.execute(
            """
            SELECT canonical_metric FROM dim_xbrl_concept_map
            WHERE taxonomy = ? AND concept = ?
            """,
            (taxonomy, concept),
        ).fetchall()
        output.update(str(row[0]) for row in rows)
    return output


def _upsert_raw_facts(
    conn: sqlite3.Connection,
    *,
    filing: FilingRef,
    source_id: str,
    facts: Iterable[RecoveredFact],
) -> int:
    inserted = 0
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        """
        DELETE FROM fact_sec_xbrl_fact_raw
        WHERE ticker = ? AND accession_number = ?
          AND source_detail IN (
              'filing_document_inline_xbrl',
              'filing_document_explicit_statement_table',
              'filing_document_explicit_financial_prose'
          )
        """,
        (filing.ticker, filing.accession_number),
    )
    accession_no_dash = filing.accession_number.replace("-", "")
    archive_cik = str(filing.archive_cik or filing.cik).lstrip("0") or "0"
    for fact in facts:
        source_url = (
            "https://www.sec.gov/Archives/edgar/data/"
            f"{archive_cik}/{accession_no_dash}/{fact.source_document}"
        )
        conn.execute(
            """
            INSERT INTO fact_sec_xbrl_fact_raw(
                fact_key, ticker, cik, source_id, taxonomy, concept, unit, value,
                start_date, end_date, fiscal_year, fiscal_period, form_type,
                filing_date, accession_number, frame, period_type, source_detail,
                source_accession_url, source_payload_hash, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(fact_key) DO UPDATE SET
                value = excluded.value,
                source_detail = excluded.source_detail,
                source_accession_url = excluded.source_accession_url,
                source_payload_hash = excluded.source_payload_hash,
                updated_at = excluded.updated_at
            """,
            (
                _fact_key(filing.ticker, filing.accession_number, fact),
                filing.ticker,
                filing.cik,
                source_id,
                fact.taxonomy,
                fact.concept,
                fact.unit,
                fact.value,
                fact.start_date,
                fact.end_date,
                int(fact.end_date[:4]),
                "",
                filing.form_type,
                filing.filing_date,
                filing.accession_number,
                fact.frame,
                fact.period_type,
                fact.source_detail,
                source_url,
                fact.content_sha256,
                now,
                now,
            ),
        )
        inserted += 1
    return inserted


def recover_cached_filing(
    conn: sqlite3.Connection,
    *,
    cache_dir: Path,
    filing: FilingRef,
    facts_source_id: str,
    primary_taxonomy: str,
    fallback_currency: str,
) -> dict[str, Any]:
    directory = accession_directory(cache_dir, filing)
    if not directory.is_dir():
        return {
            "ticker": filing.ticker,
            "accession_number": filing.accession_number,
            "status": "CACHE_MISSING",
            "document_count": 0,
            "raw_fact_count": 0,
            "core_metrics": "",
        }
    required_names = tuple(
        sorted(
            path.name
            for path in directory.iterdir()
            if path.is_file() and path.name != "index.json"
        )
    )
    documents = build_document_refs(
        conn,
        cache_dir=cache_dir,
        filing=filing,
        keywords=(),
        max_documents=0,
        required_documents=required_names,
    )
    if not documents:
        return {
            "ticker": filing.ticker,
            "accession_number": filing.accession_number,
            "status": "CACHE_EMPTY",
            "document_count": 0,
            "raw_fact_count": 0,
            "core_metrics": "",
        }
    catalog_documents(conn, filing=filing, documents=documents)
    paths = [Path(document.path) for document in documents]
    structured = parse_inline_document_set(paths)
    structured_metrics = _mapped_metrics(conn, structured)
    explicit = parse_explicit_statement_tables(
        paths,
        report_date=filing.report_date,
        taxonomy=primary_taxonomy,
        fallback_currency=fallback_currency,
    )
    explicit = [
        fact
        for fact in explicit
        if next(
            (
                metric
                for metric, concept in STANDARD_CONCEPTS[primary_taxonomy].items()
                if concept == fact.concept
            ),
            "",
        )
        not in structured_metrics
    ]
    table_metrics = structured_metrics | _mapped_metrics(conn, explicit)
    explicit_prose = parse_explicit_financial_prose(
        paths,
        report_date=filing.report_date,
        taxonomy=primary_taxonomy,
        fallback_currency=fallback_currency,
    )
    explicit_prose = [
        fact
        for fact in explicit_prose
        if next(
            (
                metric
                for metric, concept in STANDARD_CONCEPTS[primary_taxonomy].items()
                if concept == fact.concept
            ),
            "",
        )
        not in table_metrics
    ]
    facts = [*structured, *explicit, *explicit_prose]
    inserted = _upsert_raw_facts(
        conn,
        filing=filing,
        source_id=facts_source_id,
        facts=facts,
    )
    metrics = _mapped_metrics(conn, facts) & CORE_METRICS
    return {
        "ticker": filing.ticker,
        "accession_number": filing.accession_number,
        "status": "RECOVERED" if len(metrics) >= 2 else "INSUFFICIENT_CORE_FACTS",
        "document_count": len(documents),
        "raw_fact_count": inserted,
        "structured_fact_count": len(structured),
        "explicit_table_fact_count": len(explicit),
        "explicit_prose_fact_count": len(explicit_prose),
        "core_metrics": ";".join(sorted(metrics)),
    }
