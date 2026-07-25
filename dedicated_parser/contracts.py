from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


DOCUMENT_PARSER_RELEASE = "0.3.0"
PARSER_SCHEMA_VERSION = 2


def stable_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def file_sha256(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class MetricRequest:
    metric_name: str
    concept_patterns: tuple[str, ...] = ()


@dataclass(frozen=True)
class AdapterRegistry:
    model_family: str
    adapter_version: str
    supported_forms: tuple[str, ...]
    source_metrics: tuple[MetricRequest, ...]
    metric_dependencies: dict[str, str]
    document_keywords: tuple[str, ...]
    review_policy_path: str = ""
    review_policy_golden_path: str = ""

    def source_metric(self, metric_name: str) -> str:
        return self.metric_dependencies.get(metric_name, metric_name)

    def request(self, metric_name: str) -> MetricRequest | None:
        source_name = self.source_metric(metric_name)
        return next(
            (
                request
                for request in self.source_metrics
                if request.metric_name == source_name
            ),
            None,
        )


@dataclass(frozen=True)
class FilingRef:
    ticker: str
    cik: str
    accession_number: str
    form_type: str
    filing_date: str
    accepted_at: str
    report_date: str
    primary_document: str
    source_id: str
    company_currency: str = "USD"


@dataclass(frozen=True)
class DocumentRef:
    name: str
    path: str
    content_sha256: str
    file_size: int
    modified_ns: int
    is_primary: bool = False
    is_full_submission: bool = False
    source_kind: str = "archive_file"


@dataclass(frozen=True)
class WorkItem:
    model_family: str
    adapter_path: str
    adapter_version: str
    filing: FilingRef
    documents: tuple[DocumentRef, ...]
    requested_metrics: tuple[MetricRequest, ...]
    review_policy_path: str = ""
    review_policy_sha256: str = ""
    parser_release: str = DOCUMENT_PARSER_RELEASE
    enable_arelle: bool = True
    enable_edgartools: bool = True
    enable_pdf_ocr: bool = False
    max_pdf_pages: int = 250
    max_pdf_bytes: int = 25_000_000
    pdf_extraction_timeout_seconds: float = 30.0

    @property
    def work_key(self) -> str:
        return stable_hash(
            {
                "model_family": self.model_family,
                "adapter_version": self.adapter_version,
                "filing": asdict(self.filing),
                "documents": [
                    {
                        "name": item.name,
                        "sha256": item.content_sha256,
                        "source_kind": item.source_kind,
                    }
                    for item in self.documents
                ],
                "requested_metrics": [asdict(item) for item in self.requested_metrics],
                "review_policy_sha256": self.review_policy_sha256,
                "parser_release": self.parser_release,
                "schema_version": PARSER_SCHEMA_VERSION,
                "options": {
                    "enable_arelle": self.enable_arelle,
                    "enable_edgartools": self.enable_edgartools,
                    "enable_pdf_ocr": self.enable_pdf_ocr,
                    "max_pdf_pages": self.max_pdf_pages,
                    "max_pdf_bytes": self.max_pdf_bytes,
                    "pdf_extraction_timeout_seconds": (
                        self.pdf_extraction_timeout_seconds
                    ),
                },
            }
        )


@dataclass(frozen=True)
class NormalizedFact:
    taxonomy: str
    concept_name: str
    value_text: str
    numeric_value: float | None
    unit: str
    period_start: str
    period_end: str
    context_id: str
    dimensions_json: str
    scope: str
    source_document: str
    provider: str
    decimals: str = ""
    concept_metadata_json: str = "{}"

    def fingerprint(self, *, filing: FilingRef) -> str:
        return stable_hash(
            {
                "cik": filing.cik,
                "accession_number": filing.accession_number,
                **asdict(self),
            }
        )


@dataclass(frozen=True)
class MetricEvidence:
    metric_name: str
    concept_name: str
    value: float | None
    unit: str
    period_start: str
    period_end: str
    scope: str
    confidence: float
    status: str
    reason: str
    evidence_text: str
    source_document: str
    extraction_method: str
    provenance: dict[str, Any] = field(default_factory=dict)

    def evidence_key(self, *, model_family: str, filing: FilingRef) -> str:
        return stable_hash(
            {
                "model_family": model_family,
                "ticker": filing.ticker,
                "accession_number": filing.accession_number,
                **asdict(self),
            }
        )


@dataclass(frozen=True)
class WorkResult:
    work_key: str
    model_family: str
    adapter_version: str
    filing: FilingRef
    parser_release: str
    status: str
    normalized_facts: tuple[NormalizedFact, ...] = ()
    metric_evidence: tuple[MetricEvidence, ...] = ()
    provider_metadata: dict[str, Any] = field(default_factory=dict)
    elapsed_seconds: float = 0.0
    error: str = ""


@dataclass(frozen=True)
class PlanSummary:
    asof_date: str
    model_family: str
    requested_tickers: int
    unresolved_metric_pairs: int
    database_satisfied_pairs: int
    scheduled_accessions: int
    scheduled_documents: int
    skipped_completed_accessions: int
    missing_cache_accessions: int
    missing_cache_details: tuple[dict[str, str], ...] = ()
