from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

from dedicated_parser.contracts import (
    DocumentRef,
    FilingRef,
    file_sha256,
)
from dedicated_parser.path_io import (
    is_file_path,
    open_path,
    resolve_path as resolve_filesystem_path,
    runtime_path,
    stat_path,
)
from dedicated_parser.sec_paths import (
    SEC_DOCUMENT_SUFFIXES,
    validate_sec_relative_document_path,
)


REQUIRED_FIELDS = frozenset(
    {
        "ticker",
        "accession_number",
        "document_name",
        "content_sha256",
        "cache_status",
    }
)


@dataclass(frozen=True)
class SourceManifest:
    path: Path
    content_sha256: str
    row_count: int
    tickers: tuple[str, ...]
    accessions: tuple[str, ...]
    documents: dict[tuple[str, str], dict[str, str]]
    direct_filings: dict[tuple[str, str], FilingRef]
    direct_documents: dict[tuple[str, str], tuple[DocumentRef, ...]]
    metric_scope: dict[tuple[str, str], frozenset[str]]

    @property
    def direct_document_mode(self) -> bool:
        return bool(self.direct_filings)


def _optional_path(
    raw: object,
    *,
    manifest_path: Path,
) -> Path | None:
    value = str(raw or "").strip()
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = manifest_path.parent / path
    return resolve_filesystem_path(path)


def _flag(raw: object, *, default: bool = False) -> bool:
    value = str(raw or "").strip().lower()
    if not value:
        return default
    if value in {"1", "true", "yes", "y"}:
        return True
    if value in {"0", "false", "no", "n"}:
        return False
    raise ValueError(f"invalid boolean value={raw!r}")


def load_source_manifest(path: Path) -> SourceManifest:
    resolved = resolve_filesystem_path(path.expanduser())
    if not is_file_path(resolved):
        raise FileNotFoundError(f"Source manifest does not exist: {resolved}")
    documents: dict[tuple[str, str], dict[str, str]] = {}
    direct_filings: dict[tuple[str, str], FilingRef] = {}
    direct_documents_mutable: dict[tuple[str, str], list[DocumentRef]] = {}
    metric_scope_mutable: dict[tuple[str, str], set[str]] = {}
    tickers: set[str] = set()
    accessions: set[str] = set()
    row_count = 0
    with open_path(resolved, "r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = set(reader.fieldnames or ())
        missing_fields = sorted(REQUIRED_FIELDS - fields)
        if missing_fields:
            raise ValueError(f"{resolved}: missing required fields={missing_fields}")
        for line_number, row in enumerate(reader, start=2):
            ticker = str(row.get("ticker") or "").strip().upper()
            accession = str(row.get("accession_number") or "").strip()
            document_raw = row.get("document_name")
            document = str(document_raw or "")
            content_hash = str(row.get("content_sha256") or "").strip().lower()
            cache_status = str(row.get("cache_status") or "").strip().upper()
            if not ticker or not accession or not document:
                raise ValueError(f"{resolved}:{line_number}: ticker, accession_number, and document_name are required")
            document = validate_sec_relative_document_path(
                document_raw, allowed_suffixes=SEC_DOCUMENT_SUFFIXES,
                context=f"{resolved}:{line_number}: source-manifest document_name",
            )
            if cache_status != "CACHED_HASHED":
                raise ValueError(
                    f"{resolved}:{line_number}: source document is not cached and sealed: cache_status={cache_status!r}"
                )
            if len(content_hash) != 64 or any(char not in "0123456789abcdef" for char in content_hash):
                raise ValueError(f"{resolved}:{line_number}: invalid content_sha256")
            key = (ticker, accession)
            scoped = documents.setdefault(key, {})
            if document.casefold() in {name.casefold() for name in scoped}:
                raise ValueError(
                    f"{resolved}:{line_number}: duplicate case-insensitive source-manifest key {ticker}/{accession}/{document}"
                )
            scoped[document] = content_hash
            requested_metrics = {
                item.strip() for item in str(row.get("requested_metric_ids") or "").split("|") if item.strip()
            }
            metric_scope_mutable.setdefault(key, set()).update(requested_metrics)
            local_path = _optional_path(
                row.get("local_path"),
                manifest_path=resolved,
            )
            if local_path is not None:
                if not is_file_path(local_path):
                    raise FileNotFoundError(f"{resolved}:{line_number}: local_path does not exist: {local_path}")
                actual_hash = file_sha256(local_path)
                if actual_hash != content_hash:
                    raise ValueError(
                        f"{resolved}:{line_number}: local_path hash "
                        f"mismatch expected={content_hash} "
                        f"actual={actual_hash}"
                    )
                primary_document = validate_sec_relative_document_path(
                    row.get("primary_document") or document,
                    allowed_suffixes=SEC_DOCUMENT_SUFFIXES,
                    context=f"{resolved}:{line_number}: source-manifest primary_document",
                )
                company_currency = str(
                    row.get('company_currency') or 'USD'
                ).strip().upper()
                if not re.fullmatch(r'[A-Z]{3}', company_currency):
                    raise ValueError(
                        f'{resolved}:{line_number}: invalid company_currency'
                    )
                filing = FilingRef(
                    ticker=ticker,
                    cik=str(row.get("cik") or "NONSEC").strip(),
                    accession_number=accession,
                    form_type=str(row.get("form_type") or "NON-SEC").strip(),
                    filing_date=str(row.get("filing_date") or "").strip(),
                    accepted_at=str(row.get("accepted_at") or row.get("filing_date") or "").strip(),
                    report_date=str(row.get("report_date") or row.get("filing_date") or "").strip(),
                    primary_document=primary_document,
                    source_id=str(row.get("source_id") or "local_source_manifest").strip(),
                    company_currency=company_currency,
                )
                prior_filing = direct_filings.get(key)
                if prior_filing is not None and prior_filing != filing:
                    raise ValueError(
                        f"{resolved}:{line_number}: inconsistent direct filing metadata for {ticker}/{accession}"
                    )
                direct_filings[key] = filing
                stat = stat_path(local_path)
                direct_documents_mutable.setdefault(key, []).append(
                    DocumentRef(
                        name=document,
                        path=str(runtime_path(local_path)),
                        content_sha256=content_hash,
                        file_size=int(stat.st_size),
                        modified_ns=int(stat.st_mtime_ns),
                        is_primary=_flag(
                            row.get("is_primary"),
                            default=(document == filing.primary_document),
                        ),
                        is_full_submission=_flag(
                            row.get("is_full_submission"),
                        ),
                        source_kind=str(row.get("source_kind") or "local_source_manifest").strip(),
                    )
                )
            tickers.add(ticker)
            accessions.add(accession)
            row_count += 1
    if not row_count:
        raise ValueError(f"{resolved}: source manifest has no document rows")
    if direct_filings and len(direct_filings) != len(documents):
        missing = sorted(set(documents) - set(direct_filings))
        raise ValueError(
            f"{resolved}: local_path must be populated for every filing in direct-document mode; missing={missing[:10]}"
        )
    return SourceManifest(
        path=resolved,
        content_sha256=file_sha256(resolved),
        row_count=row_count,
        tickers=tuple(sorted(tickers)),
        accessions=tuple(sorted(accessions)),
        documents=documents,
        direct_filings=direct_filings,
        direct_documents={key: tuple(value) for key, value in direct_documents_mutable.items()},
        metric_scope={key: frozenset(value) for key, value in metric_scope_mutable.items() if value},
    )
