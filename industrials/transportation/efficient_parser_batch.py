from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable, Mapping, Sequence


EFFICIENT_BATCH_VERSION = "transportation_dp6s_efficient_batch_v1"
DEFAULT_COMPLETED_RUN_IDS = (58, 59, 60)
NON_READY_DOCUMENT_STATUSES = frozenset(
    {
        "PRIMARY_DOCUMENT_SOURCE_GAP",
        "EXCLUDED_AFTER_DP6Q_REVIEW",
        "EXCLUDED_AFTER_DP6R_REDIRECT_REVIEW",
    }
)

RESIDUAL_PAIR_FIELDS = (
    "batch_version",
    "ticker",
    "metric_id",
    "non_ready_document_count",
    "non_ready_document_ids",
    "non_ready_statuses",
    "ready_alternate_document_count",
    "ready_alternate_document_ids",
    "completed_sec_filing_count",
    "sec_coverage_status",
    "disposition",
    "retrieval_retry_authorized",
    "parser_execution_blocking",
)

RESIDUAL_DOCUMENT_FIELDS = (
    "batch_version",
    "document_id",
    "ticker",
    "document_hydration_status",
    "document_type",
    "published_date_hint",
    "source_domain",
    "canonical_url",
    "retrieval_url",
    "request_status",
    "retryable",
    "error_class",
    "error",
    "applicable_metric_count",
    "applicable_metric_ids",
    "covered_metric_count",
    "terminal_metric_count",
    "terminal_metric_ids",
    "disposition",
    "retrieval_retry_authorized",
    "parser_execution_blocking",
)

DELTA_SOURCE_FIELDS = (
    "ticker",
    "accession_number",
    "document_name",
    "content_sha256",
    "cache_status",
    "local_path",
    "cik",
    "form_type",
    "filing_date",
    "accepted_at",
    "report_date",
    "primary_document",
    "source_id",
    "company_currency",
    "source_kind",
    "is_primary",
    "is_full_submission",
    "requested_metric_ids",
    "date_basis",
    "content_type",
    "content_bytes",
    "document_ids",
    "canonical_urls",
    "source_domains",
    "document_types",
)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [{str(key): str(value or "").strip() for key, value in row.items()} for row in csv.DictReader(handle)]


def pipe_values(*values: object) -> tuple[str, ...]:
    return tuple(sorted({item.strip() for value in values for item in str(value or "").split("|") if item.strip()}))


def row_metric_ids(row: Mapping[str, str]) -> tuple[str, ...]:
    return pipe_values(
        row.get("applicable_parser_metric_ids"),
        row.get("applicable_supporting_metric_ids"),
    )


def completed_content_hashes(
    conn: sqlite3.Connection,
    *,
    run_ids: Sequence[int] = DEFAULT_COMPLETED_RUN_IDS,
) -> tuple[set[str], dict[int, int]]:
    hashes: set[str] = set()
    counts: dict[int, int] = {}
    for run_id in run_ids:
        run_hashes: set[str] = set()
        rows = conn.execute(
            """
            SELECT ledger.input_hashes_json
            FROM sec_parser_run_work AS relation
            JOIN sec_parser_work_ledger AS ledger
              ON ledger.work_key = relation.work_key
            WHERE relation.run_id = ? AND ledger.status = 'COMPLETED'
            """,
            (int(run_id),),
        ).fetchall()
        for row in rows:
            payload = json.loads(str(row[0] or "{}"))
            if not isinstance(payload, dict):
                raise ValueError(f"run_id={run_id} has invalid input_hashes_json")
            run_hashes.update(str(value) for value in payload.values())
        counts[int(run_id)] = len(run_hashes)
        hashes.update(run_hashes)
    return hashes, counts


def build_residual_dispositions(
    *,
    document_rows: Sequence[Mapping[str, str]],
    sec_coverage_rows: Sequence[Mapping[str, str]],
) -> tuple[list[dict[str, object]], list[dict[str, object]], dict[str, object]]:
    ready_documents: dict[tuple[str, str], set[str]] = defaultdict(set)
    non_ready_documents: dict[tuple[str, str], set[str]] = defaultdict(set)
    non_ready_statuses: dict[tuple[str, str], set[str]] = defaultdict(set)
    rows_by_document: dict[str, Mapping[str, str]] = {}
    for row in document_rows:
        document_id = str(row.get("document_id") or "")
        if not document_id or document_id in rows_by_document:
            raise ValueError("Hydrated document ids are blank or duplicated")
        rows_by_document[document_id] = row
        ticker = str(row.get("ticker") or "").upper()
        for metric_id in row_metric_ids(row):
            key = (ticker, metric_id)
            if str(row.get("content_ready") or "0") == "1":
                ready_documents[key].add(document_id)
            elif str(row.get("document_hydration_status") or "") in (NON_READY_DOCUMENT_STATUSES):
                non_ready_documents[key].add(document_id)
                non_ready_statuses[key].add(str(row.get("document_hydration_status") or ""))

    sec_coverage = {
        (
            str(row.get("ticker") or "").upper(),
            str(row.get("metric_id") or ""),
        ): row
        for row in sec_coverage_rows
        if str(row.get("applicability_status") or "") == "APPLICABLE"
    }
    pair_rows: list[dict[str, object]] = []
    pair_dispositions: dict[tuple[str, str], str] = {}
    for key in sorted(non_ready_documents):
        ticker, metric_id = key
        alternate_ids = sorted(ready_documents.get(key, set()))
        coverage = sec_coverage.get(key, {})
        completed_sec = int(str(coverage.get("completed_filing_count") or "0"))
        if alternate_ids:
            disposition = "COVERED_BY_ALTERNATE_READY_SOURCE"
        elif completed_sec:
            disposition = "TERMINAL_AFTER_COMPLETED_SEC_SEARCH"
        else:
            disposition = "TERMINAL_SOURCE_UNAVAILABLE"
        pair_dispositions[key] = disposition
        pair_rows.append(
            {
                "batch_version": EFFICIENT_BATCH_VERSION,
                "ticker": ticker,
                "metric_id": metric_id,
                "non_ready_document_count": len(non_ready_documents[key]),
                "non_ready_document_ids": "|".join(sorted(non_ready_documents[key])),
                "non_ready_statuses": "|".join(sorted(non_ready_statuses[key])),
                "ready_alternate_document_count": len(alternate_ids),
                "ready_alternate_document_ids": "|".join(alternate_ids),
                "completed_sec_filing_count": completed_sec,
                "sec_coverage_status": str(coverage.get("coverage_status") or ""),
                "disposition": disposition,
                "retrieval_retry_authorized": 0,
                "parser_execution_blocking": 0,
            }
        )

    document_output: list[dict[str, object]] = []
    for document_id, row in sorted(rows_by_document.items()):
        status = str(row.get("document_hydration_status") or "")
        if status not in NON_READY_DOCUMENT_STATUSES:
            continue
        ticker = str(row.get("ticker") or "").upper()
        metrics = row_metric_ids(row)
        terminal = [
            metric_id
            for metric_id in metrics
            if pair_dispositions[(ticker, metric_id)] != "COVERED_BY_ALTERNATE_READY_SOURCE"
        ]
        document_output.append(
            {
                "batch_version": EFFICIENT_BATCH_VERSION,
                "document_id": document_id,
                "ticker": ticker,
                "document_hydration_status": status,
                "document_type": row.get("document_type", ""),
                "published_date_hint": row.get("published_date_hint", ""),
                "source_domain": row.get("source_domain", ""),
                "canonical_url": row.get("canonical_url", ""),
                "retrieval_url": row.get("retrieval_url", ""),
                "request_status": row.get("request_status", ""),
                "retryable": row.get("retryable", ""),
                "error_class": row.get("error_class", ""),
                "error": row.get("error", ""),
                "applicable_metric_count": len(metrics),
                "applicable_metric_ids": "|".join(metrics),
                "covered_metric_count": len(metrics) - len(terminal),
                "terminal_metric_count": len(terminal),
                "terminal_metric_ids": "|".join(terminal),
                "disposition": ("COVERED_BY_ALTERNATE_READY_SOURCE" if not terminal else "TERMINAL_DISPOSITION_FROZEN"),
                "retrieval_retry_authorized": 0,
                "parser_execution_blocking": 0,
            }
        )
    summary = {
        "non_ready_document_count": len(document_output),
        "non_ready_pair_count": len(pair_rows),
        "pair_disposition_counts": dict(sorted(Counter(pair_dispositions.values()).items())),
        "terminal_pair_count": sum(
            value != "COVERED_BY_ALTERNATE_READY_SOURCE" for value in pair_dispositions.values()
        ),
        "unresolved_pair_count": 0,
        "targeted_recovery_authorized_count": 0,
    }
    return document_output, pair_rows, summary


def _document_suffix(content_type: str, *, path: Path) -> str:
    normalized = content_type.lower()
    if "pdf" in normalized:
        return ".pdf"
    if "spreadsheetml" in normalized:
        return ".xlsx"
    if "msword" in normalized:
        with path.open("rb") as handle:
            return ".docx" if handle.read(2) == b"PK" else ".doc"
    if "ms-excel" in normalized:
        return ".xls"
    if "xml" in normalized or "rss" in normalized:
        return ".xml"
    if "calendar" in normalized:
        return ".ics"
    return ".html"


def _valid_date(value: object, *, asof_date: str) -> str:
    candidate = str(value or "")[:10]
    if len(candidate) == 10 and candidate <= asof_date:
        try:
            from datetime import date

            date.fromisoformat(candidate)
            return candidate
        except ValueError:
            return ""
    return ""


def _catalog_by_hash(
    content_rows: Sequence[Mapping[str, str]],
) -> dict[str, Mapping[str, str]]:
    output: dict[str, Mapping[str, str]] = {}
    for row in content_rows:
        content_hash = str(row.get("content_sha256") or "").lower()
        if len(content_hash) != 64 or content_hash in output:
            raise ValueError("Content catalog hashes are blank, invalid, or duplicated")
        output[content_hash] = row
    return output


def build_direct_delta_manifest(
    *,
    document_rows: Sequence[Mapping[str, str]],
    content_rows: Sequence[Mapping[str, str]],
    prior_hashes: set[str],
    company_currencies: Mapping[str, str],
    asof_date: str,
    allowed_metric_ids: set[str] | None = None,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    catalog = _catalog_by_hash(content_rows)
    ready_rows = [row for row in document_rows if str(row.get("content_ready") or "0") == "1"]
    mappings: dict[tuple[str, str], list[Mapping[str, str]]] = defaultdict(list)
    for row in ready_rows:
        ticker = str(row.get("ticker") or "").upper()
        content_hash = str(row.get("content_sha256") or "").lower()
        if content_hash not in catalog:
            raise ValueError(f"Ready document hash absent from content catalog: {content_hash}")
        if content_hash not in prior_hashes:
            mappings[(ticker, content_hash)].append(row)

    delta_rows: list[dict[str, object]] = []
    excluded_metric_ids: set[str] = set()
    for (ticker, content_hash), rows in sorted(mappings.items()):
        content = catalog[content_hash]
        local_path = Path(str(content.get("content_cache_path") or "")).expanduser().resolve()
        if not local_path.is_file():
            raise FileNotFoundError(local_path)
        expected_bytes = int(str(content.get("content_bytes") or "0"))
        if local_path.stat().st_size != expected_bytes:
            raise ValueError(
                f"Content size mismatch for {content_hash}: "
                f"expected={expected_bytes} "
                f"actual={local_path.stat().st_size}"
            )
        known_dates = sorted(
            {
                value
                for row in rows
                if (
                    value := _valid_date(
                        row.get("published_date_hint"),
                        asof_date=asof_date,
                    )
                )
            }
        )
        availability_date = known_dates[0] if known_dates else asof_date
        date_basis = (
            "earliest_identical_content_published_date_hint" if known_dates else "source_census_cutoff_conservative"
        )
        content_type = str(content.get("content_types") or "")
        suffix = _document_suffix(content_type, path=local_path)
        document_name = f"{content_hash}{suffix}"
        discovered_metrics = pipe_values(
            *(row.get("applicable_parser_metric_ids") for row in rows),
            *(row.get("applicable_supporting_metric_ids") for row in rows),
        )
        if allowed_metric_ids is None:
            metrics = discovered_metrics
        else:
            metrics = tuple(metric_id for metric_id in discovered_metrics if metric_id in allowed_metric_ids)
            excluded_metric_ids.update(set(discovered_metrics) - allowed_metric_ids)
        if not metrics:
            raise ValueError(f"No applicable metrics for {ticker}/{content_hash}")
        delta_rows.append(
            {
                "ticker": ticker,
                "accession_number": (f"NONSEC-{ticker}-{content_hash[:24]}"),
                "document_name": document_name,
                "content_sha256": content_hash,
                "cache_status": "CACHED_HASHED",
                "local_path": str(local_path),
                "cik": f"NONSEC-{ticker}",
                "form_type": "NON-SEC",
                "filing_date": availability_date,
                "accepted_at": f"{availability_date}T23:59:59Z",
                "report_date": availability_date,
                "primary_document": document_name,
                "source_id": ("dedicated_parser_transportation_non_sec_candidate"),
                "company_currency": str(company_currencies.get(ticker) or "USD").upper(),
                "source_kind": ("transportation_non_sec_primary_document"),
                "is_primary": 1,
                "is_full_submission": 0,
                "requested_metric_ids": "|".join(metrics),
                "date_basis": date_basis,
                "content_type": content_type,
                "content_bytes": expected_bytes,
                "document_ids": "|".join(sorted({str(row.get("document_id") or "") for row in rows})),
                "canonical_urls": "|".join(sorted({str(row.get("canonical_url") or "") for row in rows})),
                "source_domains": "|".join(sorted({str(row.get("source_domain") or "") for row in rows})),
                "document_types": "|".join(sorted({str(row.get("document_type") or "") for row in rows})),
            }
        )
    new_hashes = sorted(set(catalog) - set(prior_hashes))
    represented_hashes = sorted({str(row["content_sha256"]) for row in delta_rows})
    if represented_hashes != new_hashes:
        missing = sorted(set(new_hashes) - set(represented_hashes))
        raise ValueError(f"New content hashes are not fully represented by ticker contexts: missing={missing[:10]}")
    summary = {
        "content_catalog_hash_count": len(catalog),
        "previously_completed_hash_count": len(set(catalog) & prior_hashes),
        "new_unique_content_hash_count": len(new_hashes),
        "logical_ticker_content_context_count": len(delta_rows),
        "cross_ticker_context_overhead_count": (len(delta_rows) - len(new_hashes)),
        "selected_ticker_count": len({str(row["ticker"]) for row in delta_rows}),
        "requested_ticker_metric_context_count": sum(
            len(pipe_values(row["requested_metric_ids"])) for row in delta_rows
        ),
        "non_parser_metric_count": len(excluded_metric_ids),
        "non_parser_metric_ids": sorted(excluded_metric_ids),
        "date_basis_counts": dict(sorted(Counter(row["date_basis"] for row in delta_rows).items())),
        "content_type_counts": dict(sorted(Counter(row["content_type"] for row in delta_rows).items())),
    }
    return delta_rows, summary


def artifact_sha256(paths: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(path.resolve() for path in paths):
        digest.update(str(path).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()
