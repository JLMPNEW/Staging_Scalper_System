from __future__ import annotations

import csv
import hashlib
import ipaddress
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from industrials.core.db import utc_now
from industrials.machinery.disclosure_candidates import DisclosureCandidate


ISSUER_IR_SOURCE_ID = "machinery_issuer_ir"
ISSUER_IR_SOURCE_DETAIL = "issuer_ir_prose_metric"
DOCUMENT_TYPES = frozenset(
    {
        "EARNINGS_RELEASE",
        "INVESTOR_PRESENTATION",
        "EARNINGS_TRANSCRIPT",
    }
)
PROMOTABLE_DOCUMENT_TYPES = frozenset({"EARNINGS_RELEASE", "INVESTOR_PRESENTATION"})
MANIFEST_FIELDS = (
    "ticker",
    "document_type",
    "published_at",
    "period_end",
    "title",
    "url",
    "approved_domain",
    "scope_override",
    "reviewed_by",
    "reviewed_at",
    "expected_sha256",
    "enabled",
    "notes",
)
DOCUMENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS fact_machinery_issuer_ir_document (
    document_key TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    source_id TEXT NOT NULL,
    document_type TEXT NOT NULL,
    published_at TEXT NOT NULL,
    period_end TEXT,
    title TEXT,
    source_url TEXT NOT NULL,
    final_url TEXT,
    approved_domain TEXT NOT NULL,
    content_type TEXT,
    content_sha256 TEXT,
    cache_path TEXT,
    extraction_method TEXT,
    page_count INTEGER NOT NULL DEFAULT 0,
    ocr_used INTEGER NOT NULL DEFAULT 0,
    candidate_count INTEGER NOT NULL DEFAULT 0,
    promoted_count INTEGER NOT NULL DEFAULT 0,
    retrieval_status TEXT NOT NULL,
    status_reason TEXT,
    retrieved_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(ticker, source_url, published_at),
    FOREIGN KEY (source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_machinery_issuer_ir_document_pit
ON fact_machinery_issuer_ir_document(ticker, published_at, document_type);
"""


@dataclass(frozen=True)
class IssuerIRDocument:
    ticker: str
    document_type: str
    published_at: str
    period_end: str
    title: str
    url: str
    approved_domain: str
    scope_override: str
    reviewed_by: str
    reviewed_at: str
    expected_sha256: str
    notes: str

    @property
    def document_key(self) -> str:
        payload = "|".join((self.ticker, self.url, self.published_at))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @property
    def accession_number(self) -> str:
        return f"IR-{self.document_key[:24].upper()}"

    @property
    def document_name(self) -> str:
        path_name = Path(urlparse(self.url).path).name
        return path_name or f"{self.accession_number}.html"


def ensure_issuer_ir_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(DOCUMENT_SCHEMA)


def parse_published_at(value: str) -> datetime:
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("published_at must include an explicit UTC offset")
    return parsed.astimezone(timezone.utc)


def _parse_date(value: str, *, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise ValueError(f"Invalid {field}={value!r}; expected YYYY-MM-DD") from exc


def _is_enabled(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _validated_domain(url: str, approved_domain: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ValueError(f"Issuer IR URL must use HTTPS: {url!r}")
    hostname = parsed.hostname.lower().rstrip(".")
    domain = approved_domain.lower().strip().rstrip(".")
    if not domain:
        raise ValueError(f"approved_domain is required for {url!r}")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise ValueError(f"IP-address issuer IR URLs are not allowed: {url!r}")
    if hostname != domain and not hostname.endswith(f".{domain}"):
        raise ValueError(
            f"Issuer IR URL host {hostname!r} is outside approved_domain={domain!r}"
        )
    return domain


def load_issuer_ir_manifest(path: Path) -> list[IssuerIRDocument]:
    if not path.exists():
        raise FileNotFoundError(f"Missing issuer IR manifest: {path}")
    documents: list[IssuerIRDocument] = []
    seen: set[tuple[str, str, str]] = set()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing_fields = [field for field in MANIFEST_FIELDS if field not in (reader.fieldnames or [])]
        if missing_fields:
            raise ValueError(f"Issuer IR manifest missing fields={missing_fields}: {path}")
        for line_number, row in enumerate(reader, start=2):
            if not _is_enabled(str(row.get("enabled") or "")):
                continue
            ticker = str(row.get("ticker") or "").strip().upper()
            document_type = str(row.get("document_type") or "").strip().upper()
            published = parse_published_at(str(row.get("published_at") or ""))
            published_at = published.isoformat().replace("+00:00", "Z")
            url = str(row.get("url") or "").strip()
            approved_domain = _validated_domain(
                url,
                str(row.get("approved_domain") or ""),
            )
            if not ticker:
                raise ValueError(f"Issuer IR manifest line {line_number} has no ticker")
            if document_type not in DOCUMENT_TYPES:
                raise ValueError(
                    f"Issuer IR manifest line {line_number} has unsupported "
                    f"document_type={document_type!r}"
                )
            scope_override = str(row.get("scope_override") or "").strip().lower()
            reviewed_by = str(row.get("reviewed_by") or "").strip()
            reviewed_at = _parse_date(str(row.get("reviewed_at") or ""), field="reviewed_at")
            if scope_override and scope_override != "consolidated":
                raise ValueError(
                    f"Issuer IR manifest line {line_number} has invalid "
                    f"scope_override={scope_override!r}"
                )
            if scope_override and (not reviewed_by or not reviewed_at):
                raise ValueError(
                    f"Issuer IR manifest line {line_number} scope override requires "
                    "reviewed_by and reviewed_at"
                )
            expected_sha256 = str(row.get("expected_sha256") or "").strip().lower()
            if expected_sha256 and (
                len(expected_sha256) != 64
                or any(character not in "0123456789abcdef" for character in expected_sha256)
            ):
                raise ValueError(
                    f"Issuer IR manifest line {line_number} has invalid expected_sha256"
                )
            key = (ticker, url, published_at)
            if key in seen:
                raise ValueError(f"Duplicate issuer IR manifest row={key}")
            seen.add(key)
            documents.append(
                IssuerIRDocument(
                    ticker=ticker,
                    document_type=document_type,
                    published_at=published_at,
                    period_end=_parse_date(
                        str(row.get("period_end") or ""),
                        field="period_end",
                    ),
                    title=str(row.get("title") or "").strip(),
                    url=url,
                    approved_domain=approved_domain,
                    scope_override=scope_override,
                    reviewed_by=reviewed_by,
                    reviewed_at=reviewed_at,
                    expected_sha256=expected_sha256,
                    notes=str(row.get("notes") or "").strip(),
                )
            )
    return sorted(documents, key=lambda item: (item.published_at, item.ticker, item.url))


def document_known_by_asof(document: IssuerIRDocument, *, asof: str) -> bool:
    return parse_published_at(document.published_at).date().isoformat() <= asof


def validate_final_url(document: IssuerIRDocument, final_url: str) -> None:
    _validated_domain(final_url, document.approved_domain)


def issuer_ir_filing(document: IssuerIRDocument) -> dict[str, Any]:
    form_type = {
        "EARNINGS_RELEASE": "IR-RELEASE",
        "INVESTOR_PRESENTATION": "IR-PRESENTATION",
        "EARNINGS_TRANSCRIPT": "IR-TRANSCRIPT",
    }[document.document_type]
    return {
        "accession_number": document.accession_number,
        "form_type": form_type,
        "filing_date": document.published_at[:10],
        "accepted_at": document.published_at,
        "report_date": document.period_end or document.published_at[:10],
        "fiscal_year": int((document.period_end or document.published_at[:10])[:4]),
        "fiscal_period": "",
        "primary_document": document.document_name,
    }


def apply_issuer_ir_policy(
    candidates: Iterable[DisclosureCandidate],
    *,
    document: IssuerIRDocument,
) -> list[DisclosureCandidate]:
    output: list[DisclosureCandidate] = []
    extraction_method = f"issuer_ir_{document.document_type.lower()}"
    for candidate in candidates:
        item = replace(candidate, extraction_method=extraction_method)
        if document.document_type not in PROMOTABLE_DOCUMENT_TYPES:
            if item.candidate_status == "ACCEPTED":
                item = replace(
                    item,
                    candidate_status="REVIEW_REQUIRED",
                    status_reason="issuer_transcript_requires_primary_document_corroboration",
                    confidence=min(item.confidence, 0.55),
                )
            output.append(item)
            continue
        if document.scope_override == "consolidated" and item.scope == "unknown":
            item = replace(
                item,
                scope="consolidated",
                status_reason="reviewed_manifest_consolidated_scope",
            )
        if item.candidate_status == "ACCEPTED" and item.scope != "consolidated":
            item = replace(
                item,
                candidate_status="REVIEW_REQUIRED",
                status_reason="issuer_ir_consolidated_scope_not_established",
                confidence=min(item.confidence, 0.65),
            )
        elif item.candidate_status == "ACCEPTED":
            item = replace(item, confidence=min(item.confidence, 0.80))
        output.append(item)
    return output


def upsert_issuer_ir_document(
    conn: sqlite3.Connection,
    *,
    document: IssuerIRDocument,
    final_url: str,
    content_type: str,
    content_sha256: str,
    cache_path: Path,
    extraction_method: str,
    page_count: int,
    ocr_used: bool,
    candidate_count: int,
    promoted_count: int,
    retrieval_status: str,
    status_reason: str,
) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO fact_machinery_issuer_ir_document(
            document_key, ticker, source_id, document_type, published_at,
            period_end, title, source_url, final_url, approved_domain,
            content_type, content_sha256, cache_path, extraction_method,
            page_count, ocr_used, candidate_count, promoted_count,
            retrieval_status, status_reason, retrieved_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(document_key) DO UPDATE SET
            final_url = excluded.final_url,
            content_type = excluded.content_type,
            content_sha256 = excluded.content_sha256,
            cache_path = excluded.cache_path,
            extraction_method = excluded.extraction_method,
            page_count = excluded.page_count,
            ocr_used = excluded.ocr_used,
            candidate_count = excluded.candidate_count,
            promoted_count = excluded.promoted_count,
            retrieval_status = excluded.retrieval_status,
            status_reason = excluded.status_reason,
            retrieved_at = excluded.retrieved_at,
            updated_at = excluded.updated_at
        """,
        (
            document.document_key,
            document.ticker,
            ISSUER_IR_SOURCE_ID,
            document.document_type,
            document.published_at,
            document.period_end,
            document.title,
            document.url,
            final_url,
            document.approved_domain,
            content_type,
            content_sha256,
            str(cache_path),
            extraction_method,
            page_count,
            int(ocr_used),
            candidate_count,
            promoted_count,
            retrieval_status,
            status_reason,
            now,
            now,
            now,
        ),
    )
