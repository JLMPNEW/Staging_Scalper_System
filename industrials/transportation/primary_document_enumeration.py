from __future__ import annotations

import hashlib
import gzip
import ipaddress
import json
import re
from collections import Counter, defaultdict
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, Mapping, Sequence
from urllib.parse import (
    parse_qsl,
    quote,
    urlencode,
    urljoin,
    urlparse,
    urlunparse,
)
from xml.etree import ElementTree

from industrials.transportation.non_sec_endpoints import (
    is_excluded_domain,
    normalized_domain,
)


PRIMARY_DOCUMENT_ENUMERATION_VERSION = (
    "transportation_dp6o_primary_document_enumeration_v1"
)

DISCOVERY_PAGE_FIELDS = (
    "enumeration_version",
    "endpoint_id",
    "ticker",
    "page_role",
    "depth",
    "request_url",
    "canonical_url",
    "approved_domain",
    "target_domain",
    "page_status",
    "http_status",
    "final_url",
    "final_domain",
    "content_type",
    "page_title",
    "published_date_hint",
    "content_bytes",
    "content_sha256",
    "cache_path",
    "candidate_link_count",
    "navigation_link_count",
    "error",
)

PRIMARY_DOCUMENT_FIELDS = (
    "enumeration_version",
    "document_id",
    "ticker",
    "endpoint_id",
    "endpoint_type",
    "universe_role",
    "source_id",
    "source_type",
    "source_rank",
    "freshness_status",
    "evidence_label",
    "document_type",
    "title",
    "published_date_hint",
    "source_url",
    "canonical_url",
    "retrieval_url",
    "source_domain",
    "domain_status",
    "discovered_from_url",
    "discovery_page_role",
    "archive_timestamp",
    "archive_digest",
    "earliest_archive_timestamp",
    "latest_archive_timestamp",
    "archive_capture_count",
    "metric_search_match_count",
    "metric_search_matches",
    "candidate_source_lane_ids",
    "applicable_parser_metric_count",
    "applicable_parser_metric_ids",
    "applicable_supporting_metric_count",
    "applicable_supporting_metric_ids",
    "parse_all_applicable_metrics",
    "url_identity_sha256",
    "source_content_digest",
    "source_content_digest_algorithm",
    "content_type",
    "content_bytes",
    "content_cache_path",
    "content_sha256",
    "content_hash_status",
    "document_status",
    "retrieval_authorized",
    "parser_execution_authorized",
)

ENDPOINT_ENUMERATION_FIELDS = (
    "enumeration_version",
    "endpoint_id",
    "ticker",
    "endpoint_type",
    "universe_role",
    "original_discovery_url",
    "discovery_url",
    "approved_domain",
    "target_domain",
    "root_repair_action",
    "root_repair_review_status",
    "root_repair_unresolved_disposition",
    "root_repair_fallback_source_lane",
    "required_pair_count",
    "discovery_page_count",
    "ready_discovery_page_count",
    "failed_discovery_page_count",
    "candidate_document_count",
    "external_asset_document_count",
    "archive_digest_document_count",
    "endpoint_status",
    "retrieval_authorized",
    "parser_execution_authorized",
)

EXTERNAL_DOMAIN_FIELDS = (
    "enumeration_version",
    "ticker",
    "endpoint_id",
    "source_domain",
    "source_url",
    "discovered_from_url",
    "title",
    "document_type",
    "domain_status",
    "review_required",
    "retrieval_authorized",
)

FUTURE_EXCLUSION_FIELDS = (
    "enumeration_version",
    "document_id",
    "ticker",
    "source_url",
    "published_date_hint",
    "exclusion_reason",
    "retrieval_authorized",
    "parser_execution_authorized",
)

DOCUMENT_SUFFIXES = frozenset(
    {
        ".csv",
        ".doc",
        ".docx",
        ".htm",
        ".html",
        ".pdf",
        ".txt",
        ".xls",
        ".xlsx",
        ".xhtml",
    }
)
FILE_DOCUMENT_SUFFIXES = frozenset(
    {
        ".csv",
        ".doc",
        ".docx",
        ".pdf",
        ".txt",
        ".xls",
        ".xlsx",
    }
)
NON_DOCUMENT_ASSET_SUFFIXES = frozenset(
    {
        ".avi",
        ".bmp",
        ".css",
        ".eot",
        ".gif",
        ".ico",
        ".jpeg",
        ".jpg",
        ".js",
        ".m4a",
        ".mov",
        ".mp3",
        ".mp4",
        ".mpeg",
        ".mpg",
        ".ogg",
        ".png",
        ".svg",
        ".tif",
        ".tiff",
        ".ttf",
        ".wav",
        ".webm",
        ".webp",
        ".woff",
        ".woff2",
    }
)
TRACKING_QUERY_KEYS = frozenset(
    {
        "fbclid",
        "gclid",
        "mc_cid",
        "mc_eid",
        "ref",
        "source",
        "utm_campaign",
        "utm_content",
        "utm_medium",
        "utm_source",
        "utm_term",
    }
)
SOURCE_DOCUMENT_PHRASES = (
    "annual report",
    "annual reports",
    "earnings release",
    "earnings releases",
    "financial results",
    "quarterly results",
    "results presentation",
    "investor presentation",
    "investor presentations",
    "operating statistics",
    "operating data",
    "traffic report",
    "traffic results",
    "monthly traffic",
    "fleet report",
    "fleet plan",
    "supplemental data",
    "financial supplement",
    "statistical supplement",
    "fact sheet",
    "factsheet",
    "form 20 f",
    "form 6 k",
)
SOURCE_DOCUMENT_TOKENS = frozenset(
    {
        "apresentacoes",
        "annual",
        "earnings",
        "ergebnisse",
        "financial",
        "financiero",
        "financieros",
        "financeiro",
        "financeiros",
        "fleet",
        "informes",
        "informacoes",
        "investor",
        "investidores",
        "inversionistas",
        "operating",
        "operacionais",
        "operativos",
        "presentation",
        "presentaciones",
        "quarter",
        "quartalsberichte",
        "relatorios",
        "report",
        "results",
        "resultados",
        "statistics",
        "supplement",
        "traffic",
        "trafego",
        "trafico",
        "trimestrais",
        "trimestrales",
    }
)
BLOCKED_PATH_TOKENS = frozenset(
    {
        "alerts",
        "board",
        "careers",
        "contacts",
        "cookie",
        "education",
        "email",
        "elections",
        "facebook",
        "faq",
        "faqs",
        "governance",
        "instagram",
        "linkedin",
        "privacy",
        "perguntas",
        "proxy",
        "questions",
        "quote",
        "ratings",
        "respostas",
        "search",
        "services",
        "servicos",
        "soporte",
        "soutien",
        "stock",
        "support",
        "twitter",
        "youtube",
    }
)
ARCHIVE_HTML_COMPONENT_BLOCKLIST = frozenset(
    {
        "disclaimer",
        "flash",
        "footer",
        "header",
        "intro",
        "partners",
        "sniffer",
    }
)
ASSET_HOST_SUFFIXES = (
    "amazonaws.com",
    "cloudfront.net",
    "q4cdn.com",
    "q4inc.com",
)


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href = ""
        self._title = ""
        self._text: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = {
            str(key).lower(): str(value or "")
            for key, value in attrs
        }
        if tag.lower() == "a" and attributes.get("href"):
            self._href = attributes["href"]
            self._title = attributes.get("title", "")
            self._text = []
        elif (
            tag.lower() == "link"
            and attributes.get("href")
            and (
                "sitemap" in attributes.get("rel", "").lower()
                or attributes.get("type", "").lower()
                in {"application/rss+xml", "application/atom+xml"}
            )
        ):
            self.links.append(
                (
                    attributes["href"],
                    attributes.get("title", "linked feed"),
                )
            )

    def handle_data(self, data: str) -> None:
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href:
            text = " ".join(
                part.strip() for part in self._text if part.strip()
            )
            self.links.append(
                (self._href, self._title or text)
            )
            self._href = ""
            self._title = ""
            self._text = []


class _MetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._in_title = False
        self._title_parts: list[str] = []
        self.date_candidates: list[str] = []

    @property
    def title(self) -> str:
        return " ".join(
            part.strip()
            for part in self._title_parts
            if part.strip()
        )

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = {
            str(key).lower(): str(value or "").strip()
            for key, value in attrs
        }
        normalized_tag = tag.lower()
        if normalized_tag == "title":
            self._in_title = True
        if normalized_tag == "meta":
            marker = " ".join(
                (
                    attributes.get("name", ""),
                    attributes.get("property", ""),
                    attributes.get("itemprop", ""),
                )
            ).lower()
            if any(
                token in marker
                for token in (
                    "article:published",
                    "datepublished",
                    "date.published",
                    "publishdate",
                    "pubdate",
                )
            ):
                value = attributes.get("content", "")
                if value:
                    self.date_candidates.append(value)
        elif normalized_tag == "time":
            value = attributes.get("datetime", "")
            if value:
                self.date_candidates.append(value)

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False


def normalized_text(value: str) -> str:
    text = re.sub(
        r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])",
        " ",
        str(value or "").lower(),
    )
    return " ".join(
        re.findall(r"[a-z0-9]+", text)
    )


def _base_domain(domain: str) -> str:
    labels = [
        label
        for label in str(domain or "").lower().rstrip(".").split(".")
        if label
    ]
    if len(labels) <= 2:
        return ".".join(labels)
    country_second_levels = {
        "ac",
        "co",
        "com",
        "gov",
        "net",
        "org",
    }
    if len(labels[-1]) == 2 and labels[-2] in country_second_levels:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _is_ip_or_local_host(host: str) -> bool:
    normalized = host.lower().rstrip(".")
    if normalized in {"localhost", "localhost.localdomain"}:
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return not address.is_global


def canonicalize_url(raw_url: str, *, base_url: str = "") -> str:
    text = str(raw_url or "").strip()
    if not text or text.lower().startswith(
        ("data:", "javascript:", "mailto:", "tel:")
    ):
        return ""
    candidate = urljoin(base_url, text) if base_url else text
    parsed = urlparse(candidate)
    if parsed.scheme.lower() not in {"http", "https"}:
        return ""
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host or _is_ip_or_local_host(host):
        return ""
    port = parsed.port
    netloc = host
    if port and not (
        (parsed.scheme.lower() == "http" and port == 80)
        or (parsed.scheme.lower() == "https" and port == 443)
    ):
        netloc = f"{host}:{port}"
    path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = urlencode(
        sorted(
            (key, value)
            for key, value in parse_qsl(
                parsed.query,
                keep_blank_values=True,
            )
            if key.lower() not in TRACKING_QUERY_KEYS
        ),
        doseq=True,
    )
    return urlunparse(
        (
            parsed.scheme.lower(),
            netloc,
            path,
            "",
            query,
            "",
        )
    )


def is_issuer_domain(
    domain: str,
    *,
    approved_domain: str,
    target_domain: str,
) -> bool:
    normalized = normalized_domain(domain)
    if not normalized:
        return False
    governing = {
        normalized_domain(approved_domain),
        normalized_domain(target_domain),
    }
    governing.discard("")
    if normalized in governing:
        return True
    return any(
        _base_domain(normalized) == _base_domain(item)
        for item in governing
    )


def classify_domain(
    url: str,
    *,
    approved_domain: str,
    target_domain: str,
    linked_from_issuer_page: bool,
) -> str:
    domain = normalized_domain(url)
    if not domain or is_excluded_domain(domain):
        return "EXCLUDED_OR_INVALID_DOMAIN"
    if is_issuer_domain(
        domain,
        approved_domain=approved_domain,
        target_domain=target_domain,
    ):
        return "ISSUER_CONTROLLED_DOMAIN"
    if linked_from_issuer_page and any(
        domain == suffix or domain.endswith("." + suffix)
        for suffix in ASSET_HOST_SUFFIXES
    ):
        return "ISSUER_LINKED_KNOWN_ASSET_DOMAIN"
    if linked_from_issuer_page:
        return "ISSUER_LINKED_EXTERNAL_ASSET_REVIEW_REQUIRED"
    return "OUTSIDE_SEALED_SOURCE_CHAIN"


def parse_html_links(
    payload: bytes,
    *,
    base_url: str,
) -> list[tuple[str, str]]:
    parser = _LinkParser()
    parser.feed(payload.decode("utf-8", errors="ignore"))
    output: dict[str, str] = {}
    for raw_url, title in parser.links:
        url = canonicalize_url(raw_url, base_url=base_url)
        if url:
            output.setdefault(url, normalized_text(title))
    return sorted(output.items())


def parse_robots_sitemaps(
    payload: bytes,
    *,
    base_url: str,
) -> list[str]:
    output: set[str] = set()
    text = payload.decode("utf-8", errors="ignore")
    for line in text.splitlines():
        key, _, value = line.partition(":")
        if key.strip().lower() != "sitemap":
            continue
        url = canonicalize_url(value.strip(), base_url=base_url)
        if url:
            output.add(url)
    return sorted(output)


def parse_sitemap_urls(payload: bytes) -> list[str]:
    if payload.startswith(b"\x1f\x8b"):
        try:
            payload = gzip.decompress(payload)
        except (OSError, EOFError):
            return []
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError:
        return []
    output: set[str] = set()
    for element in root.iter():
        if element.tag.lower().endswith("loc") and element.text:
            url = canonicalize_url(element.text.strip())
            if url:
                output.add(url)
    return sorted(output)


def is_navigation_url(url: str, title: str = "") -> bool:
    suffix = document_suffix(url)
    if suffix in FILE_DOCUMENT_SUFFIXES | NON_DOCUMENT_ASSET_SUFFIXES:
        return False
    text = normalized_text(f"{url} {title}")
    tokens = set(text.split())
    if tokens & BLOCKED_PATH_TOKENS:
        return False
    if {"sec", "filings"}.issubset(tokens):
        return False
    if "annual" in tokens and "meeting" in tokens:
        return False
    if tokens & {
        "apresentacoes",
        "earnings",
        "ergebnisse",
        "investor",
        "investidores",
        "inversionistas",
        "presentation",
        "presentaciones",
        "quarter",
        "quartalsberichte",
        "relatorios",
        "results",
        "resultados",
        "statistics",
        "traffic",
        "trafego",
        "trafico",
        "trimestrais",
        "trimestrales",
    }:
        return True
    if "annual" in tokens and bool(
        tokens & {"financial", "report", "reports", "results"}
    ):
        return True
    if "financial" in tokens and tokens & {
        "information",
        "investor",
        "report",
        "reports",
        "results",
    }:
        return True
    return "operating" in tokens and bool(
        tokens & {"data", "results", "statistics"}
    )


def document_suffix(url: str) -> str:
    return Path(urlparse(url).path).suffix.lower()


def _published_date_hint(url: str, title: str) -> str:
    text = f"{url} {title}"
    match = re.search(
        r"(?<!\d)(20\d{2})[-_/](0[1-9]|1[0-2])[-_/]"
        r"(0[1-9]|[12]\d|3[01])(?!\d)",
        text,
    )
    if match:
        return "-".join(match.groups())
    compact_match = re.search(
        r"(?<!\d)(20\d{2})(0[1-9]|1[0-2])"
        r"(0[1-9]|[12]\d|3[01])(?!\d)",
        text,
    )
    if compact_match:
        return "-".join(compact_match.groups())
    normalized = normalized_text(text)
    month_numbers = {
        "january": 1,
        "february": 2,
        "march": 3,
        "april": 4,
        "may": 5,
        "june": 6,
        "july": 7,
        "august": 8,
        "september": 9,
        "october": 10,
        "november": 11,
        "december": 12,
    }
    month_pattern = "|".join(month_numbers)
    month_first = re.search(
        rf"\b({month_pattern})\s+([0-3]?\d)\s+(20\d{{2}})\b",
        normalized,
    )
    if month_first:
        month_name, day, year = month_first.groups()
        return (
            f"{int(year):04d}-{month_numbers[month_name]:02d}-"
            f"{int(day):02d}"
        )
    day_first = re.search(
        rf"\b([0-3]?\d)\s+({month_pattern})\s+(20\d{{2}})\b",
        normalized,
    )
    if day_first:
        day, month_name, year = day_first.groups()
        return (
            f"{int(year):04d}-{month_numbers[month_name]:02d}-"
            f"{int(day):02d}"
        )
    return ""


def parse_html_metadata(payload: bytes) -> tuple[str, str]:
    parser = _MetadataParser()
    parser.feed(
        payload[:4_000_000].decode("utf-8", errors="ignore")
    )
    title = " ".join(parser.title.split())
    for candidate in parser.date_candidates:
        date_hint = _published_date_hint(candidate, "")
        if date_hint:
            return title, date_hint
    return title, _published_date_hint("", title)


def classify_document(
    *,
    url: str,
    title: str,
    metric_search_terms: Sequence[str],
    linked_from_relevant_page: bool = False,
) -> tuple[bool, str, tuple[str, ...]]:
    text = normalized_text(f"{url} {title}")
    tokens = set(text.split())
    if tokens & BLOCKED_PATH_TOKENS:
        return False, "", ()
    if {"sec", "filings"}.issubset(tokens):
        return False, "", ()
    if "annual" in tokens and "meeting" in tokens:
        return False, "", ()
    suffix = document_suffix(url)
    if suffix in NON_DOCUMENT_ASSET_SUFFIXES:
        return False, "", ()
    normalized_terms = tuple(
        dict.fromkeys(
            normalized_text(term)
            for term in metric_search_terms
            if normalized_text(term)
        )
    )
    text_tokens = set(text.split())
    matches = tuple(
        term
        for term in normalized_terms
        if (
            term in text_tokens
            if " " not in term and len(term) <= 4
            else term in text
        )
    )
    phrase_match = any(
        phrase in text for phrase in SOURCE_DOCUMENT_PHRASES
    )
    token_match = len(tokens & SOURCE_DOCUMENT_TOKENS) >= 2
    is_file = suffix in FILE_DOCUMENT_SUFFIXES
    is_page = suffix in {"", ".htm", ".html", ".xhtml"}
    if is_page and (
        tokens & ARCHIVE_HTML_COMPONENT_BLOCKLIST
        or re.search(r"/mov/date\d", url.lower())
    ):
        return False, "", matches
    relevant = bool(
        (is_file and (phrase_match or token_match or matches))
        or (
            is_file
            and linked_from_relevant_page
            and suffix in {".pdf", ".xls", ".xlsx", ".csv"}
        )
        or (
            is_page
            and (
                phrase_match
                or "annual" in tokens
                or len(tokens & SOURCE_DOCUMENT_TOKENS) >= 3
                or len(matches) >= 2
            )
        )
    )
    if not relevant:
        return False, "", matches
    if "annual" in tokens:
        document_type = "ANNUAL_REPORT"
    elif tokens & {
        "earnings",
        "ergebnisse",
        "quartalsberichte",
        "results",
        "resultados",
        "trimestrais",
        "trimestrales",
    }:
        document_type = "EARNINGS_OR_FINANCIAL_RESULTS"
    elif tokens & {
        "apresentacoes",
        "presentation",
        "presentaciones",
    }:
        document_type = "INVESTOR_PRESENTATION"
    elif tokens & {"traffic", "trafego", "trafico"}:
        document_type = "OPERATING_STATISTICS"
    elif tokens & {"statistics", "supplement"}:
        document_type = "OPERATING_OR_FINANCIAL_SUPPLEMENT"
    elif suffix in {".xls", ".xlsx", ".csv"}:
        document_type = "DATA_SUPPLEMENT"
    elif suffix == ".pdf":
        document_type = "PRIMARY_DISCLOSURE_PDF"
    else:
        document_type = "PRIMARY_DISCLOSURE_HTML"
    return True, document_type, matches


def build_archive_queries(
    discovery_url: str,
) -> tuple[str, str, str]:
    parsed = urlparse(discovery_url)
    parameters = parse_qsl(parsed.query, keep_blank_values=True)
    common: list[tuple[str, str]] = []
    for key, value in parameters:
        if key == "fl":
            continue
        if key == "filter" and value.startswith("mimetype:"):
            continue
        common.append((key, value))
    fields = (
        "timestamp,original,digest,statuscode,mimetype"
    )
    html = [
        *common,
        ("filter", "mimetype:text/html"),
        ("fl", fields),
    ]
    pdf_documents = [
        *common,
        ("filter", "mimetype:application/pdf"),
        ("fl", fields),
    ]
    data_documents = [
        *common,
        (
            "filter",
            "mimetype:(application/(vnd.*|ms-excel)|text/csv)",
        ),
        ("fl", fields),
    ]
    base = urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path, "", "", "")
    )
    return (
        f"{base}?{urlencode(html)}",
        f"{base}?{urlencode(pdf_documents)}",
        f"{base}?{urlencode(data_documents)}",
    )


def build_archive_year_queries(
    discovery_url: str,
    *,
    max_years: int = 50,
) -> list[tuple[str, int, str]]:
    parsed = urlparse(discovery_url)
    parameters = parse_qsl(parsed.query, keep_blank_values=True)
    by_key = {key: value for key, value in parameters}
    try:
        start_year = int(by_key["from"])
        end_year = int(by_key["to"])
    except (KeyError, TypeError, ValueError):
        return []
    if (
        end_year < start_year
        or end_year - start_year + 1 > max_years
    ):
        return []
    output: list[tuple[str, int, str]] = []
    for year in range(start_year, end_year + 1):
        yearly_parameters = [
            (
                key,
                str(year) if key in {"from", "to"} else value,
            )
            for key, value in parameters
        ]
        yearly_url = urlunparse(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                "",
                urlencode(yearly_parameters),
                "",
            )
        )
        html_url, pdf_url, data_url = build_archive_queries(yearly_url)
        output.extend(
            (
                ("ARCHIVE_CDX_HTML", year, html_url),
                ("ARCHIVE_CDX_PDF", year, pdf_url),
                ("ARCHIVE_CDX_DATA", year, data_url),
            )
        )
    return output


def parse_cdx_rows(payload: bytes) -> list[dict[str, str]]:
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list) or not parsed:
        return []
    header = parsed[0]
    if not isinstance(header, list):
        return []
    fields = [str(item) for item in header]
    required = {
        "timestamp",
        "original",
        "digest",
        "statuscode",
        "mimetype",
    }
    if not required.issubset(fields):
        return []
    output: list[dict[str, str]] = []
    for item in parsed[1:]:
        if not isinstance(item, list) or len(item) != len(fields):
            continue
        row = {
            field: str(value or "").strip()
            for field, value in zip(fields, item)
        }
        if row["statuscode"] == "200" and row["original"]:
            output.append(row)
    return output


def _archive_retrieval_url(timestamp: str, original: str) -> str:
    return (
        "https://web.archive.org/web/"
        f"{timestamp}id_/{original}"
    )


def _document_id(
    *,
    ticker: str,
    canonical_url: str,
    archive_digest: str,
) -> str:
    identity = archive_digest or canonical_url
    digest = hashlib.sha256(
        (
            f"{PRIMARY_DOCUMENT_ENUMERATION_VERSION}|"
            f"{ticker}|{identity}"
        ).encode("utf-8")
    ).hexdigest()[:24]
    return f"trndoc_{ticker.lower()}_{digest}"


def _base_document_row(
    *,
    endpoint: Mapping[str, str],
    scope: Mapping[str, str],
    title: str,
    document_type: str,
    source_url: str,
    canonical_url: str,
    retrieval_url: str,
    source_domain: str,
    domain_status: str,
    discovered_from_url: str,
    discovery_page_role: str,
    metric_matches: Sequence[str],
    archive_timestamp: str = "",
    archive_digest: str = "",
    earliest_archive_timestamp: str = "",
    latest_archive_timestamp: str = "",
    archive_capture_count: int = 0,
    published_date_hint: str = "",
) -> dict[str, object]:
    ticker = str(endpoint["ticker"]).upper()
    url_hash = hashlib.sha256(
        canonical_url.encode("utf-8")
    ).hexdigest()
    return {
        "enumeration_version": PRIMARY_DOCUMENT_ENUMERATION_VERSION,
        "document_id": _document_id(
            ticker=ticker,
            canonical_url=canonical_url,
            archive_digest=archive_digest,
        ),
        "ticker": ticker,
        "endpoint_id": endpoint["endpoint_id"],
        "endpoint_type": endpoint["endpoint_type"],
        "universe_role": endpoint["universe_role"],
        "source_id": (
            "issuer_archive_cdx"
            if archive_digest
            else "issuer_primary_site"
        ),
        "source_type": (
            "archived_primary_issuer_document"
            if archive_digest
            else "primary_issuer_document"
        ),
        "source_rank": 3,
        "freshness_status": (
            "acceptable_for_period"
            if archive_digest
            else "unknown"
        ),
        "evidence_label": "fact_source_reported",
        "document_type": document_type,
        "title": title,
        "published_date_hint": (
            published_date_hint
            or _published_date_hint(source_url, title)
        ),
        "source_url": source_url,
        "canonical_url": canonical_url,
        "retrieval_url": retrieval_url,
        "source_domain": source_domain,
        "domain_status": domain_status,
        "discovered_from_url": discovered_from_url,
        "discovery_page_role": discovery_page_role,
        "archive_timestamp": archive_timestamp,
        "archive_digest": archive_digest,
        "earliest_archive_timestamp": earliest_archive_timestamp,
        "latest_archive_timestamp": latest_archive_timestamp,
        "archive_capture_count": archive_capture_count,
        "metric_search_match_count": len(metric_matches),
        "metric_search_matches": "|".join(sorted(set(metric_matches))),
        "candidate_source_lane_ids": scope[
            "candidate_source_lane_ids"
        ],
        "applicable_parser_metric_count": scope[
            "applicable_parser_metric_count"
        ],
        "applicable_parser_metric_ids": scope[
            "applicable_parser_metric_ids"
        ],
        "applicable_supporting_metric_count": scope[
            "applicable_supporting_metric_count"
        ],
        "applicable_supporting_metric_ids": scope[
            "applicable_supporting_metric_ids"
        ],
        "parse_all_applicable_metrics": 1,
        "url_identity_sha256": url_hash,
        "source_content_digest": archive_digest,
        "source_content_digest_algorithm": (
            "wayback_cdx_digest" if archive_digest else ""
        ),
        "content_type": "",
        "content_bytes": 0,
        "content_cache_path": "",
        "content_sha256": "",
        "content_hash_status": (
            "SOURCE_ARCHIVE_DIGEST_AVAILABLE"
            if archive_digest
            else "PENDING_ONE_TIME_DOCUMENT_HYDRATION"
        ),
        "document_status": (
            "ENUMERATED_PRIMARY_DOCUMENT"
            if not domain_status.endswith("REVIEW_REQUIRED")
            else "ENUMERATED_DOMAIN_REVIEW_REQUIRED"
        ),
        "retrieval_authorized": 0,
        "parser_execution_authorized": 0,
    }


def build_live_document_candidates(
    *,
    endpoint: Mapping[str, str],
    scope: Mapping[str, str],
    pages: Sequence[Mapping[str, object]],
    metric_search_terms: Sequence[str],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    documents: list[dict[str, object]] = []
    external: list[dict[str, object]] = []
    for page in pages:
        payload = page.get("payload")
        if not isinstance(payload, bytes):
            continue
        page_url = str(page.get("final_url") or page["request_url"])
        page_role = str(page["page_role"])
        page_title = str(page.get("page_title") or "")
        page_published_date = str(
            page.get("published_date_hint") or ""
        )
        content_type = str(page.get("content_type") or "").lower()
        if "html" not in content_type and not page_url.lower().endswith(
            (".htm", ".html", "/")
        ):
            continue
        page_relevant = (
            page_role == "NAVIGATION"
            and is_navigation_url(page_url)
        )
        if page_role == "NAVIGATION":
            relevant, document_type, matches = classify_document(
                url=page_url,
                title=page_title,
                metric_search_terms=metric_search_terms,
            )
            if relevant:
                domain_status = classify_domain(
                    page_url,
                    approved_domain=endpoint["approved_domain"],
                    target_domain=endpoint["target_domain"],
                    linked_from_issuer_page=True,
                )
                if domain_status == "ISSUER_CONTROLLED_DOMAIN":
                    documents.append(
                        _base_document_row(
                            endpoint=endpoint,
                            scope=scope,
                            title=page_title,
                            document_type=document_type,
                            source_url=page_url,
                            canonical_url=page_url,
                            retrieval_url=page_url,
                            source_domain=normalized_domain(page_url),
                            domain_status=domain_status,
                            discovered_from_url=page_url,
                            discovery_page_role=page_role,
                            metric_matches=matches,
                            published_date_hint=page_published_date,
                        )
                    )
        for url, title in parse_html_links(
            payload,
            base_url=page_url,
        ):
            relevant, document_type, matches = classify_document(
                url=url,
                title=title,
                metric_search_terms=metric_search_terms,
                linked_from_relevant_page=page_relevant,
            )
            if not relevant:
                continue
            domain_status = classify_domain(
                url,
                approved_domain=endpoint["approved_domain"],
                target_domain=endpoint["target_domain"],
                linked_from_issuer_page=True,
            )
            if domain_status in {
                "EXCLUDED_OR_INVALID_DOMAIN",
                "OUTSIDE_SEALED_SOURCE_CHAIN",
            }:
                continue
            row = _base_document_row(
                endpoint=endpoint,
                scope=scope,
                title=title,
                document_type=document_type,
                source_url=url,
                canonical_url=url,
                retrieval_url=url,
                source_domain=normalized_domain(url),
                domain_status=domain_status,
                discovered_from_url=page_url,
                discovery_page_role=page_role,
                metric_matches=matches,
            )
            documents.append(row)
            if domain_status.endswith("REVIEW_REQUIRED"):
                external.append(
                    {
                        "enumeration_version": (
                            PRIMARY_DOCUMENT_ENUMERATION_VERSION
                        ),
                        "ticker": endpoint["ticker"],
                        "endpoint_id": endpoint["endpoint_id"],
                        "source_domain": normalized_domain(url),
                        "source_url": url,
                        "discovered_from_url": page_url,
                        "title": title,
                        "document_type": document_type,
                        "domain_status": domain_status,
                        "review_required": 1,
                        "retrieval_authorized": 0,
                    }
                )
    return documents, external


def build_sitemap_document_candidates(
    *,
    endpoint: Mapping[str, str],
    scope: Mapping[str, str],
    pages: Sequence[Mapping[str, object]],
    metric_search_terms: Sequence[str],
) -> list[dict[str, object]]:
    documents: list[dict[str, object]] = []
    for page in pages:
        if str(page["page_role"]) != "SITEMAP":
            continue
        payload = page.get("payload")
        if not isinstance(payload, bytes):
            continue
        for url in parse_sitemap_urls(payload):
            relevant, document_type, matches = classify_document(
                url=url,
                title="",
                metric_search_terms=metric_search_terms,
            )
            if not relevant:
                continue
            domain_status = classify_domain(
                url,
                approved_domain=endpoint["approved_domain"],
                target_domain=endpoint["target_domain"],
                linked_from_issuer_page=True,
            )
            if domain_status != "ISSUER_CONTROLLED_DOMAIN":
                continue
            documents.append(
                _base_document_row(
                    endpoint=endpoint,
                    scope=scope,
                    title="",
                    document_type=document_type,
                    source_url=url,
                    canonical_url=url,
                    retrieval_url=url,
                    source_domain=normalized_domain(url),
                    domain_status=domain_status,
                    discovered_from_url=str(page["request_url"]),
                    discovery_page_role="SITEMAP",
                    metric_matches=matches,
                )
            )
    return documents


def build_archive_document_candidates(
    *,
    endpoint: Mapping[str, str],
    scope: Mapping[str, str],
    cdx_pages: Sequence[Mapping[str, object]],
    metric_search_terms: Sequence[str],
) -> list[dict[str, object]]:
    captures: dict[
        tuple[str, str],
        list[tuple[dict[str, str], str]],
    ] = defaultdict(list)
    for page in cdx_pages:
        payload = page.get("payload")
        if not isinstance(payload, bytes):
            continue
        for row in parse_cdx_rows(payload):
            original = canonicalize_url(row["original"])
            if not original:
                continue
            relevant, document_type, matches = classify_document(
                url=original,
                title="",
                metric_search_terms=metric_search_terms,
            )
            if not relevant:
                continue
            domain_status = classify_domain(
                original,
                approved_domain=endpoint["target_domain"],
                target_domain=endpoint["target_domain"],
                linked_from_issuer_page=True,
            )
            if domain_status != "ISSUER_CONTROLLED_DOMAIN":
                continue
            digest = row["digest"]
            identity = digest or original
            captures[(identity, document_type)].append((row, "|".join(matches)))
    output: list[dict[str, object]] = []
    for (_, document_type), items in sorted(captures.items()):
        ordered = sorted(
            items,
            key=lambda item: (
                item[0]["timestamp"],
                item[0]["original"],
            ),
        )
        first = ordered[0][0]
        last = ordered[-1][0]
        selected = first
        canonical = canonicalize_url(selected["original"])
        all_matches = sorted(
            {
                match
                for _, raw_matches in ordered
                for match in raw_matches.split("|")
                if match
            }
        )
        output.append(
            _base_document_row(
                endpoint=endpoint,
                scope=scope,
                title="",
                document_type=document_type,
                source_url=canonical,
                canonical_url=canonical,
                retrieval_url=_archive_retrieval_url(
                    selected["timestamp"],
                    selected["original"],
                ),
                source_domain=normalized_domain(canonical),
                domain_status="ISSUER_CONTROLLED_DOMAIN",
                discovered_from_url=str(
                    items[0][0].get("original") or ""
                ),
                discovery_page_role="ARCHIVE_CDX",
                metric_matches=all_matches,
                archive_timestamp=selected["timestamp"],
                archive_digest=selected["digest"],
                earliest_archive_timestamp=first["timestamp"],
                latest_archive_timestamp=last["timestamp"],
                archive_capture_count=len(ordered),
            )
        )
    return output


def deduplicate_document_rows(
    rows: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    by_identity: dict[tuple[str, str], dict[str, object]] = {}
    for raw in sorted(
        rows,
        key=lambda row: (
            str(row["ticker"]),
            str(row["canonical_url"]),
            str(row["document_id"]),
        ),
    ):
        row = dict(raw)
        identity = str(
            row.get("archive_digest")
            or row.get("canonical_url")
            or ""
        )
        key = (str(row["ticker"]), identity)
        existing = by_identity.get(key)
        if existing is None:
            by_identity[key] = row
            continue
        existing_matches = {
            item
            for item in str(
                existing.get("metric_search_matches") or ""
            ).split("|")
            if item
        }
        existing_matches.update(
            item
            for item in str(
                row.get("metric_search_matches") or ""
            ).split("|")
            if item
        )
        existing["metric_search_matches"] = "|".join(
            sorted(existing_matches)
        )
        existing["metric_search_match_count"] = len(existing_matches)
        date_hints = sorted(
            {
                str(existing.get("published_date_hint") or ""),
                str(row.get("published_date_hint") or ""),
            }
            - {""}
        )
        existing["published_date_hint"] = (
            date_hints[0] if date_hints else ""
        )
        if not str(existing.get("title") or ""):
            existing["title"] = str(row.get("title") or "")
        sources = sorted(
            {
                str(existing.get("discovered_from_url") or ""),
                str(row.get("discovered_from_url") or ""),
            }
            - {""}
        )
        existing["discovered_from_url"] = "|".join(sources)
    return sorted(
        by_identity.values(),
        key=lambda row: (
            str(row["ticker"]),
            str(row["document_type"]),
            str(row["canonical_url"]),
        ),
    )


def build_endpoint_enumeration_rows(
    *,
    endpoint_rows: Sequence[Mapping[str, str]],
    discovery_rows: Sequence[Mapping[str, object]],
    document_rows: Sequence[Mapping[str, object]],
    required_pair_counts: Mapping[str, int],
) -> tuple[list[dict[str, object]], list[str]]:
    pages_by_endpoint: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    docs_by_endpoint: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for row in discovery_rows:
        pages_by_endpoint[str(row["endpoint_id"])].append(row)
    for row in document_rows:
        docs_by_endpoint[str(row["endpoint_id"])].append(row)
    output: list[dict[str, object]] = []
    errors: list[str] = []
    for endpoint in sorted(
        endpoint_rows,
        key=lambda row: (row["ticker"], row["endpoint_id"]),
    ):
        endpoint_id = endpoint["endpoint_id"]
        pages = pages_by_endpoint.get(endpoint_id, [])
        documents = docs_by_endpoint.get(endpoint_id, [])
        ready = sum(
            str(row.get("page_status") or "") == "READY"
            for row in pages
        )
        failed = sum(
            str(row.get("page_status") or "") == "FAILED"
            for row in pages
        )
        root_ready = any(
            (
                str(row.get("page_role") or "") == "ROOT"
                or str(row.get("page_role") or "").startswith(
                    "ARCHIVE_CDX_HTML"
                )
            )
            and str(row.get("page_status") or "") == "READY"
            for row in pages
        )
        if not root_ready:
            unresolved_disposition = str(
                endpoint.get("root_repair_unresolved_disposition")
                or ""
            )
            if unresolved_disposition.startswith("REVIEWED_"):
                status = unresolved_disposition
            else:
                status = "ROOT_DISCOVERY_FAILED"
                errors.append(
                    f"{endpoint['ticker']}: sealed root was not enumerated"
                )
        elif not documents:
            status = "NO_PRIMARY_DOCUMENT_CANDIDATES_REVIEW_REQUIRED"
        elif failed:
            status = "ENUMERATED_WITH_PARTIAL_DISCOVERY_FAILURES"
        else:
            status = "ENUMERATED_AND_URL_DEDUPLICATED"
        output.append(
            {
                "enumeration_version": (
                    PRIMARY_DOCUMENT_ENUMERATION_VERSION
                ),
                "endpoint_id": endpoint_id,
                "ticker": endpoint["ticker"],
                "endpoint_type": endpoint["endpoint_type"],
                "universe_role": endpoint["universe_role"],
                "original_discovery_url": endpoint.get(
                    "original_discovery_url",
                    endpoint["discovery_url"],
                ),
                "discovery_url": endpoint["discovery_url"],
                "approved_domain": endpoint["approved_domain"],
                "target_domain": endpoint["target_domain"],
                "root_repair_action": endpoint.get(
                    "root_repair_action",
                    "",
                ),
                "root_repair_review_status": endpoint.get(
                    "root_repair_review_status",
                    "",
                ),
                "root_repair_unresolved_disposition": endpoint.get(
                    "root_repair_unresolved_disposition",
                    "",
                ),
                "root_repair_fallback_source_lane": endpoint.get(
                    "root_repair_fallback_source_lane",
                    "",
                ),
                "required_pair_count": required_pair_counts.get(
                    endpoint["ticker"],
                    0,
                ),
                "discovery_page_count": len(pages),
                "ready_discovery_page_count": ready,
                "failed_discovery_page_count": failed,
                "candidate_document_count": len(documents),
                "external_asset_document_count": sum(
                    str(row.get("domain_status") or "")
                    != "ISSUER_CONTROLLED_DOMAIN"
                    for row in documents
                ),
                "archive_digest_document_count": sum(
                    bool(str(row.get("archive_digest") or ""))
                    for row in documents
                ),
                "endpoint_status": status,
                "retrieval_authorized": 0,
                "parser_execution_authorized": 0,
            }
        )
    return output, errors


def summarize_primary_document_enumeration(
    *,
    endpoint_rows: Sequence[Mapping[str, object]],
    discovery_rows: Sequence[Mapping[str, object]],
    document_rows: Sequence[Mapping[str, object]],
    external_rows: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    statuses = Counter(
        str(row["endpoint_status"]) for row in endpoint_rows
    )
    types = Counter(
        str(row["document_type"]) for row in document_rows
    )
    return {
        "endpoint_count": len(endpoint_rows),
        "endpoint_status_counts": dict(sorted(statuses.items())),
        "discovery_page_count": len(discovery_rows),
        "ready_discovery_page_count": sum(
            str(row.get("page_status") or "") == "READY"
            for row in discovery_rows
        ),
        "failed_discovery_page_count": sum(
            str(row.get("page_status") or "") == "FAILED"
            for row in discovery_rows
        ),
        "primary_document_count": len(document_rows),
        "primary_document_ticker_count": len(
            {str(row["ticker"]) for row in document_rows}
        ),
        "document_type_counts": dict(sorted(types.items())),
        "archive_source_digest_count": sum(
            bool(str(row.get("archive_digest") or ""))
            for row in document_rows
        ),
        "document_content_sha256_count": sum(
            bool(str(row.get("content_sha256") or ""))
            for row in document_rows
        ),
        "live_document_pending_content_hash_count": sum(
            not bool(str(row.get("archive_digest") or ""))
            and not bool(str(row.get("content_sha256") or ""))
            for row in document_rows
        ),
        "external_domain_review_count": len(external_rows),
        "url_identity_unique_count": len(
            {
                (
                    str(row["ticker"]),
                    str(row["url_identity_sha256"]),
                )
                for row in document_rows
            }
        ),
    }


def unique_navigation_links(
    *,
    pages: Iterable[Mapping[str, object]],
    endpoint: Mapping[str, str],
) -> list[str]:
    output: set[str] = set()
    for page in pages:
        payload = page.get("payload")
        if not isinstance(payload, bytes):
            continue
        page_url = str(page.get("final_url") or page["request_url"])
        for url, title in parse_html_links(
            payload,
            base_url=page_url,
        ):
            if (
                is_navigation_url(url, title)
                and is_issuer_domain(
                    normalized_domain(url),
                    approved_domain=endpoint["approved_domain"],
                    target_domain=endpoint["target_domain"],
                )
            ):
                output.add(url)
    return sorted(output)
