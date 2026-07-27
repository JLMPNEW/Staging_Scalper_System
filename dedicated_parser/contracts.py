from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


DOCUMENT_PARSER_RELEASE = "0.4.6"
# Version 7 scopes persisted evidence keys to the immutable work identity so
# a later adapter or review-policy evaluation cannot mutate an earlier run.
PARSER_SCHEMA_VERSION = 7


def _hash_default(value: Any) -> str:
    # Fail closed: str() of sets or arbitrary objects is process-dependent
    # (hash randomization, memory addresses) and would silently break the
    # one-vs-many-worker determinism guarantee. Only explicitly stable types
    # may fall back to string form.
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Unhashable payload type for stable_hash: {type(value).__name__}")


def stable_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=_hash_default,
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
class MetricRequirement:
    """Database state that satisfies one downstream metric dependency."""

    satisfaction_field: str
    mode: str = "point"
    series_metric: str = ""
    minimum_discrete_periods: int = 0
    lookback_days: int = 0


@dataclass(frozen=True)
class ProductionMetricMapping:
    canonical_metric: str
    financial_statement: str
    period_type: str
    source_priority: int
    sign_policy: str = "positive_abs"


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
    supporting_metrics: tuple[MetricRequest, ...] = ()
    metric_requirements: dict[str, MetricRequirement] = field(default_factory=dict)
    production_mappings: dict[str, ProductionMetricMapping] = field(default_factory=dict)
    metric_freshness_days: dict[str, int] = field(default_factory=dict)

    def source_metric(self, metric_name: str) -> str:
        return self.metric_dependencies.get(metric_name, metric_name)

    @property
    def parser_metrics(self) -> tuple[MetricRequest, ...]:
        seen: set[str] = set()
        output: list[MetricRequest] = []
        for request in (*self.source_metrics, *self.supporting_metrics):
            if request.metric_name in seen:
                continue
            seen.add(request.metric_name)
            output.append(request)
        return tuple(output)

    def request(self, metric_name: str) -> MetricRequest | None:
        source_name = self.source_metric(metric_name)
        return next(
            (request for request in self.parser_metrics if request.metric_name == source_name),
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
        filing_identity = {
            "ticker": self.filing.ticker,
            "cik": self.filing.cik,
            "accession_number": self.filing.accession_number,
            "form_type": self.filing.form_type,
            "primary_document": self.filing.primary_document,
        }
        # Reporting currency changes the meaning of bare "$" prose values.
        # Keep the common USD identity backward-compatible while ensuring
        # corrected non-USD metadata invalidates only affected filing work.
        company_currency = str(self.filing.company_currency or "USD").upper()
        if company_currency != "USD":
            filing_identity["company_currency"] = company_currency
        return stable_hash(
            {
                "model_family": self.model_family,
                "adapter_version": self.adapter_version,
                # Content-bearing filing identity only: volatile planner-derived
                # metadata such as later-populated accepted_at must not force a
                # mass reparse of unchanged filings.
                "filing": filing_identity,
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
                    "pdf_extraction_timeout_seconds": (self.pdf_extraction_timeout_seconds),
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
        # concept_metadata_json is excluded so the persistence upsert's
        # DO UPDATE branch for it is reachable: metadata refreshes update the
        # existing row instead of inserting a sibling under a new fingerprint.
        payload = asdict(self)
        payload.pop("concept_metadata_json", None)
        return stable_hash(
            {
                "cik": filing.cik,
                "accession_number": filing.accession_number,
                **payload,
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

    def evidence_key(
        self,
        *,
        model_family: str,
        filing: FilingRef,
        work_key: str = "",
    ) -> str:
        # Review-mutable fields (confidence/status/reason/provenance) are
        # excluded from the observation identity. Persistence also supplies
        # work_key so each parser/adapter/policy evaluation remains immutable;
        # adapters omit it when deduplicating observations inside one work item.
        payload = asdict(self)
        for mutable_field in ("confidence", "status", "reason", "provenance"):
            payload.pop(mutable_field, None)
        return stable_hash(
            {
                "model_family": model_family,
                "ticker": filing.ticker,
                "accession_number": filing.accession_number,
                "work_key": work_key,
                **payload,
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
    series_gap_details: tuple[dict[str, Any], ...] = ()
    # Resume-skipped completed work, so the run can still link the prior
    # evidence into its own scope; without this, a re-run classifies those
    # pairs from zero evidence and regresses them to UNCONFIRMED/MISSING.
    skipped_completed_work: tuple[dict[str, str], ...] = ()
    # Keep the exhaustive requested scope. Deriving it from scheduled work
    # drops issuers with no supported filing metadata or no cached documents.
    selected_tickers: tuple[str, ...] = ()
