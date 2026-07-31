from __future__ import annotations

from collections import Counter
from typing import Mapping, Sequence

from dedicated_parser.contracts import stable_hash
from industrials.transportation.delta_parser_manifest import (
    DELTA_PARSER_FIELDS,
)


PARSER_REPAIR_VERSION = "transportation_dp6h_pdf_repair_v1"
PARSER_REPAIR_FIELDS = DELTA_PARSER_FIELDS
REPAIRABLE_METHODS = frozenset(
    {
        "dedicated_parser:pdf_size_limit",
        "dedicated_parser:pdf_pypdf_timeout",
    }
)


def failure_document_keys(
    rows: Sequence[Mapping[str, object]],
) -> set[tuple[str, str, str]]:
    keys: set[tuple[str, str, str]] = set()
    for row in rows:
        if str(row.get("candidate_status") or "") != "PARSER_FAILURE":
            continue
        method = str(row.get("extraction_method") or "")
        if method not in REPAIRABLE_METHODS:
            continue
        ticker = str(row.get("ticker") or "").upper()
        accession = str(row.get("accession_number") or "")
        document = str(row.get("source_document") or "")
        if ticker and accession and document:
            keys.add((ticker, accession, document))
    return keys


def build_parser_repair_rows(
    *,
    source_rows: Sequence[Mapping[str, str]],
    failure_rows: Sequence[Mapping[str, object]],
) -> tuple[list[dict[str, object]], list[str]]:
    required = failure_document_keys(failure_rows)
    errors: list[str] = []
    selected: list[dict[str, object]] = []
    seen: set[tuple[str, str, str]] = set()
    for source in source_rows:
        key = (
            str(source.get("ticker") or "").upper(),
            str(source.get("accession_number") or ""),
            str(source.get("document_name") or ""),
        )
        if key not in required:
            continue
        if key in seen:
            errors.append(
                "duplicate repair source document="
                + "|".join(key)
            )
            continue
        seen.add(key)
        if not key[2].lower().endswith(".pdf"):
            errors.append(
                "repair source is not PDF=" + "|".join(key)
            )
            continue
        if str(source.get("cache_status") or "") != "CACHED_HASHED":
            errors.append(
                "repair source is not cached and hashed="
                + "|".join(key)
            )
            continue
        row: dict[str, object] = {
            field: source.get(field, "")
            for field in PARSER_REPAIR_FIELDS
        }
        row.update(
            {
                "manifest_version": PARSER_REPAIR_VERSION,
                "row_key": stable_hash(
                    {
                        "repair_version": PARSER_REPAIR_VERSION,
                        "ticker": key[0],
                        "accession_number": key[1],
                        "document_name": key[2],
                        "content_sha256": str(
                            source.get("content_sha256") or ""
                        ),
                    }
                ),
                "selection_rule": (
                    "dp6h_targeted_existing_pdf_failure_repair"
                ),
            }
        )
        selected.append(row)
    missing = sorted(required - seen)
    errors.extend(
        "missing repair source document=" + "|".join(key)
        for key in missing
    )
    selected.sort(
        key=lambda row: (
            str(row["ticker"]),
            str(row["accession_number"]),
            str(row["document_name"]).lower(),
        )
    )
    return selected, errors


def summarize_parser_repair(
    *,
    repair_rows: Sequence[Mapping[str, object]],
    failure_rows: Sequence[Mapping[str, object]],
    residual_failure_pairs: set[tuple[str, str]],
) -> dict[str, object]:
    repair_keys = {
        (
            str(row["ticker"]),
            str(row["accession_number"]),
            str(row["document_name"]),
        )
        for row in repair_rows
    }
    failure_pair_keys = {
        (
            str(row.get("ticker") or "").upper(),
            str(row.get("metric_name") or ""),
        )
        for row in failure_rows
        if str(row.get("candidate_status") or "")
        == "PARSER_FAILURE"
    }
    methods = Counter(
        str(row.get("extraction_method") or "")
        for row in failure_rows
        if str(row.get("candidate_status") or "")
        == "PARSER_FAILURE"
    )
    return {
        "failure_evidence_count": sum(methods.values()),
        "failure_evidence_method_counts": dict(sorted(methods.items())),
        "failure_evidence_pair_count": len(failure_pair_keys),
        "residual_parser_failure_pair_count": len(
            residual_failure_pairs
        ),
        "residual_pairs_covered_by_failure_evidence_count": len(
            residual_failure_pairs & failure_pair_keys
        ),
        "repair_ticker_count": len(
            {str(row["ticker"]) for row in repair_rows}
        ),
        "repair_accession_count": len(
            {
                (str(row["ticker"]), str(row["accession_number"]))
                for row in repair_rows
            }
        ),
        "repair_document_count": len(repair_keys),
        "repair_max_document_bytes": max(
            (
                int(str(row.get("file_size") or 0))
                for row in repair_rows
            ),
            default=0,
        ),
    }
