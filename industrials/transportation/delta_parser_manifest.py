from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Mapping, Sequence

from dedicated_parser.contracts import file_sha256, stable_hash
from industrials.core.reports import write_csv_atomic, write_text_atomic


DELTA_PARSER_MANIFEST_VERSION = "transportation_dp6e_delta_parser_v1"
DELTA_PARSER_FIELDS = (
    "manifest_version",
    "row_key",
    "ticker",
    "cik",
    "accession_number",
    "form_type",
    "filing_date",
    "accepted_at",
    "report_date",
    "source_id",
    "document_name",
    "document_kind",
    "local_path",
    "file_size",
    "content_sha256",
    "cache_status",
    "is_primary",
    "is_full_submission",
    "is_exhibit",
    "selection_rule",
    "applicable_metric_ids",
    "applicable_metric_count",
)


def build_delta_parser_rows(
    *,
    delta_rows: Sequence[Mapping[str, str]],
    archive_cache_dir: Path,
    source_id: str,
) -> tuple[list[dict[str, object]], list[str]]:
    output: list[dict[str, object]] = []
    errors: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    for filing in delta_rows:
        ticker = str(filing.get("ticker") or "").strip().upper()
        accession = str(
            filing.get("accession_number") or ""
        ).strip()
        action = str(filing.get("delta_action") or "").strip()
        if action != "PARSE_NEW_CACHED_DOCUMENT_HASHES":
            errors.append(
                f"{ticker}|{accession}: unresolved delta_action={action}"
            )
            continue
        cik = str(filing.get("cik") or "").strip().zfill(10)
        primary_document = str(
            filing.get("primary_document") or ""
        ).strip()
        document_names = [
            name.strip()
            for name in str(
                filing.get("selected_document_names") or ""
            ).split("|")
            if name.strip()
        ]
        if not document_names:
            errors.append(
                f"{ticker}|{accession}: no selected documents"
            )
            continue
        metric_ids = tuple(
            metric_id
            for metric_id in str(
                filing.get("target_metric_ids") or ""
            ).split("|")
            if metric_id
        )
        accession_dir = (
            archive_cache_dir
            / f"CIK{cik}"
            / accession.replace("-", "")
        )
        for document_name in document_names:
            key = (ticker, accession, document_name)
            if key in seen:
                errors.append(
                    "duplicate parser document key: "
                    f"{ticker}|{accession}|{document_name}"
                )
                continue
            seen.add(key)
            if Path(document_name).name != document_name:
                errors.append(
                    f"{ticker}|{accession}: unsafe document name "
                    f"{document_name!r}"
                )
                continue
            path = accession_dir / document_name
            if not path.is_file() or path.stat().st_size <= 0:
                errors.append(
                    f"{ticker}|{accession}: missing document {document_name}"
                )
                continue
            if path.suffix.lower() == ".pdf":
                with path.open("rb") as handle:
                    if handle.read(5) != b"%PDF-":
                        errors.append(
                            f"{ticker}|{accession}: invalid PDF "
                            f"{document_name}"
                        )
                        continue
            content_hash = file_sha256(path)
            is_full_submission = int(
                document_name == f"{accession}.txt"
            )
            row = {
                "manifest_version": DELTA_PARSER_MANIFEST_VERSION,
                "row_key": stable_hash(
                    {
                        "ticker": ticker,
                        "accession_number": accession,
                        "document_name": document_name,
                        "content_sha256": content_hash,
                    }
                ),
                "ticker": ticker,
                "cik": cik,
                "accession_number": accession,
                "form_type": str(
                    filing.get("form_type") or ""
                ).strip(),
                "filing_date": str(
                    filing.get("filing_date") or ""
                ).strip(),
                "accepted_at": str(
                    filing.get("accepted_at") or ""
                ).strip(),
                "report_date": str(
                    filing.get("report_date") or ""
                ).strip(),
                "source_id": source_id,
                "document_name": document_name,
                "document_kind": (
                    "sec_full_submission_sgml"
                    if is_full_submission
                    else (
                        "sec_archive_pdf"
                        if path.suffix.lower() == ".pdf"
                        else "sec_archive_document"
                    )
                ),
                "local_path": str(path.resolve()),
                "file_size": path.stat().st_size,
                "content_sha256": content_hash,
                "cache_status": "CACHED_HASHED",
                "is_primary": int(document_name == primary_document),
                "is_full_submission": is_full_submission,
                "is_exhibit": int(
                    document_name != primary_document
                    and not is_full_submission
                ),
                "selection_rule": "dp6e_source_exhaustion_delta",
                "applicable_metric_ids": "|".join(metric_ids),
                "applicable_metric_count": len(metric_ids),
            }
            output.append(row)
    output.sort(
        key=lambda row: (
            str(row["ticker"]),
            str(row["filing_date"]),
            str(row["accession_number"]),
            str(row["document_name"]).lower(),
        )
    )
    return output, errors


def write_delta_parser_manifest(
    *,
    rows: Sequence[Mapping[str, object]],
    errors: Sequence[str],
    source_manifest_path: Path,
    output_dir: Path,
    expected_metric_count: int,
    parser_metric_count: int,
) -> dict[str, object]:
    csv_path = (
        output_dir / "transportation_delta_parser_source_manifest.csv"
    )
    json_path = (
        output_dir / "transportation_delta_parser_source_manifest.json"
    )
    write_csv_atomic(csv_path, DELTA_PARSER_FIELDS, rows)
    accession_keys = {
        (str(row["ticker"]), str(row["accession_number"]))
        for row in rows
    }
    duplicate_row_keys = [
        key
        for key, count in Counter(
            str(row["row_key"]) for row in rows
        ).items()
        if count > 1
    ]
    final_errors = list(errors)
    if duplicate_row_keys:
        final_errors.append(
            f"duplicate row keys={duplicate_row_keys[:10]}"
        )
    payload = {
        "acceptance": "PASS" if not final_errors and rows else "FAIL",
        "gate": "DP6E_DELTA_PARSER_SOURCE_MANIFEST",
        "manifest_version": DELTA_PARSER_MANIFEST_VERSION,
        "model_family": "transportation",
        "source_manifest_path": str(source_manifest_path.resolve()),
        "source_manifest_sha256": file_sha256(source_manifest_path),
        "expected_specialized_metric_count": expected_metric_count,
        "parser_metric_count": parser_metric_count,
        "selected_ticker_count": len(
            {str(row["ticker"]) for row in rows}
        ),
        "selected_identity_count": len(
            {str(row["ticker"]) for row in rows}
        ),
        "selected_accession_count": len(accession_keys),
        "selected_document_count": len(rows),
        "selected_document_row_count": len(rows),
        "unique_content_hash_count": len(
            {str(row["content_sha256"]) for row in rows}
        ),
        "all_parser_metrics": True,
        "database_mode": "not_opened",
        "network_requests": 0,
        "parser_invocations": 0,
        "feature_build_invocations": 0,
        "historical_materialization_invocations": 0,
        "calibration_invocations": 0,
        "portfolio_invocations": 0,
        "parser_execution_authorized": False,
        "errors": final_errors,
        "artifact": {
            "path": str(csv_path.resolve()),
            "row_count": len(rows),
            "sha256": file_sha256(csv_path),
        },
        "next_gate": (
            "DELTA_PARSER_PLAN_ONLY"
            if not final_errors and rows
            else "REPAIR_DELTA_PARSER_MANIFEST"
        ),
    }
    write_text_atomic(
        json_path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    return payload
