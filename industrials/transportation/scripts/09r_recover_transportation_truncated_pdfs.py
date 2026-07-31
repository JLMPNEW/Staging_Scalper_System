#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.contracts import DocumentRef, file_sha256  # noqa: E402
from industrials.core.config import (  # noqa: E402
    family_config,
    load_yaml,
    resolve_path,
)
from industrials.core.reports import (  # noqa: E402
    write_csv_atomic,
    write_text_atomic,
)
from industrials.transportation.content_text_cache import (  # noqa: E402
    ExtractionOptions,
    cache_path,
    extract_document_once,
)
from industrials.transportation.efficient_parser_batch import (  # noqa: E402
    DELTA_SOURCE_FIELDS,
    read_csv,
)
from industrials.transportation.ocr_recovery import (  # noqa: E402
    OCR_TIMEOUT_SECONDS,
    configure_tesseract_environment,
    inventory_sha256,
)
from industrials.transportation.primary_document_hydration import (  # noqa: E402
    DomainThrottle,
    _default_fetch,
    store_content,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
)


TRUNCATED_REPAIR_VERSION = (
    "transportation_dp7c_truncated_pdf_repair_v1"
)
EXPECTED_TRUNCATED_HASH_COUNT = 7
EXPECTED_TRUNCATED_CONTEXT_COUNT = 10
TRUNCATED_BYTES = 1_048_576
MAX_DOCUMENT_BYTES = 75_000_000
HYDRATION_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36 "
    "TransportationPrimaryDocumentHydrator/1.0"
)
OFFICIAL_URL_OVERRIDES = {
    # The ATSG server is case-sensitive in these legacy media segments.
    "ce3a36097c5bab800ea06ea19dca4001c6458029596ba41e868f6d441cbc3609": (
        "https://www.atsginc.com/~/media/Files/A/ATSG/ATSGINC/"
        "docs/investor/news-and-events/events-and-presentation/"
        "atsg-investor-presentation-2019-11.pdf"
    ),
}
RESULT_FIELDS = (
    "repair_version",
    "original_content_sha256",
    "ticker_contexts",
    "canonical_url",
    "http_status",
    "network_request_count",
    "original_content_bytes",
    "recovered_content_bytes",
    "recovered_content_sha256",
    "recovered_local_path",
    "cache_path",
    "repair_status",
    "extraction_method",
    "ocr_used",
    "page_count",
    "text_character_count",
    "warning",
    "error",
    "elapsed_seconds",
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Re-download only the seven structurally truncated one-MiB PDFs, "
            "extract their text in the isolated OCR namespace, and combine "
            "them with the prior OCR successes for one semantic batch."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--timeout-sec", type=float, default=90.0)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument(
        "--retry-limitations",
        action="store_true",
        help=(
            "Reuse sealed successful repairs and retry only previously "
            "unrecovered hashes, including approved official URL fixes."
        ),
    )
    return parser.parse_args(argv)


def _json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected JSON object")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    write_text_atomic(
        path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )


def _url(row: dict[str, str]) -> str:
    return str(row.get("canonical_urls") or "").split("|", 1)[0].strip()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.timeout_sec <= 0 or args.max_retries <= 0:
        raise ValueError("timeout-sec and max-retries must be positive")
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    parser_cfg = family_config(config, MODEL_FAMILY)[
        "dedicated_parser"
    ]
    if bool(parser_cfg.get("parser_execution_authorized")):
        raise ValueError(
            "Truncated-PDF repair requires general parser execution disabled"
        )
    asof_date = str(parser_cfg["source_census_asof_date"])
    output_dir = (
        resolve_path(parser_cfg["output_root"], base_dir=config_path.parent)
        / asof_date
    )
    ocr_manifest_path = (
        output_dir / "transportation_ocr_delta_manifest.json"
    )
    ocr_results_path = (
        output_dir / "transportation_ocr_delta_cache_results.csv"
    )
    base_source_path = (
        output_dir
        / "transportation_non_sec_direct_delta_source_manifest.csv"
    )
    ocr_source_path = (
        output_dir / "transportation_ocr_delta_source_manifest.csv"
    )
    result_path = (
        output_dir
        / "transportation_truncated_pdf_repair_results.csv"
    )
    union_source_path = (
        output_dir
        / "transportation_ocr_recovery_union_source_manifest.csv"
    )
    manifest_path = (
        output_dir
        / "transportation_ocr_recovery_union_manifest.json"
    )
    required = (
        ocr_manifest_path,
        ocr_results_path,
        base_source_path,
        ocr_source_path,
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Missing truncated-repair inputs: {missing}")
    ocr_manifest = _json(ocr_manifest_path)
    ocr_rows = read_csv(ocr_results_path)
    base_source_rows = read_csv(base_source_path)
    recovered_ocr_rows = read_csv(ocr_source_path)
    failed_rows = [
        row for row in ocr_rows if row["cache_status"] == "OCR_FAILED"
    ]
    failed_hashes = {
        str(row["content_sha256"]).lower() for row in failed_rows
    }
    failed_context_rows = [
        row
        for row in base_source_rows
        if str(row["content_sha256"]).lower() in failed_hashes
    ]
    errors: list[str] = []
    if (
        ocr_manifest.get("acceptance")
        not in {"PASS", "PASS_WITH_EXPLICIT_LIMITATIONS"}
        or str(
            (
                (ocr_manifest.get("artifacts") or {}).get(
                    "ocr_cache_results"
                )
                or {}
            ).get("sha256")
            or ""
        )
        != file_sha256(ocr_results_path)
        or not bool(ocr_manifest.get("original_cache_unchanged"))
    ):
        errors.append("bounded OCR result is not sealed and passing")
    if (
        len(failed_hashes) != EXPECTED_TRUNCATED_HASH_COUNT
        or len(failed_context_rows) != EXPECTED_TRUNCATED_CONTEXT_COUNT
    ):
        errors.append("truncated-PDF scope cardinality changed")
    failed_by_hash = {
        str(row["content_sha256"]).lower(): row
        for row in failed_rows
    }
    source_by_hash: dict[str, dict[str, str]] = {}
    tickers_by_hash: dict[str, set[str]] = {}
    for row in failed_context_rows:
        content_hash = str(row["content_sha256"]).lower()
        source_by_hash.setdefault(content_hash, row)
        tickers_by_hash.setdefault(content_hash, set()).add(row["ticker"])
        if (
            int(row.get("content_bytes") or 0) != TRUNCATED_BYTES
            or int(
                failed_by_hash[content_hash].get("content_bytes") or 0
            )
            != TRUNCATED_BYTES
            or not _url(row)
        ):
            errors.append(
                f"{content_hash}: not an exact one-MiB URL-backed failure"
            )
    preflight = {
        "acceptance": (
            "PASS_PREFLIGHT" if not errors else "FAIL_PREFLIGHT"
        ),
        "gate": "DP7C_TRUNCATED_PDF_REPAIR_PREFLIGHT",
        "repair_version": TRUNCATED_REPAIR_VERSION,
        "model_family": MODEL_FAMILY,
        "asof_date": asof_date,
        "truncated_hash_count": len(failed_hashes),
        "truncated_context_count": len(failed_context_rows),
        "original_cache_mutation_authorized": False,
        "parser_invocations": 0,
        "network_requests": 0,
        "errors": errors,
    }
    if args.status:
        prior = _json(manifest_path) if manifest_path.is_file() else {}
        print(
            json.dumps(
                prior if prior else preflight,
                indent=2,
                sort_keys=True,
            )
        )
        return 0 if not errors else 2
    if errors:
        print(json.dumps(preflight, indent=2, sort_keys=True))
        return 2
    if manifest_path.is_file():
        prior = _json(manifest_path)
        artifacts = prior.get("artifacts") or {}
        prior_union = artifacts.get("union_source_manifest") or {}
        if (
            not args.retry_limitations
            and
            prior.get("acceptance")
            in {"PASS", "PASS_WITH_EXPLICIT_LIMITATIONS"}
            and str(
                (
                    (prior.get("inputs") or {}).get("ocr_results")
                    or {}
                ).get("sha256")
                or ""
            )
            == file_sha256(ocr_results_path)
            and union_source_path.is_file()
            and str(prior_union.get("sha256") or "")
            == file_sha256(union_source_path)
        ):
            print(
                json.dumps(
                    {**prior, "idempotent_reuse": True},
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
    if not args.execute:
        print(json.dumps(preflight, indent=2, sort_keys=True))
        return 0

    isolated_root = (
        PROJECT_ROOT
        / "output"
        / "industrials_cache"
        / "transportation"
        / "ocr_delta"
        / "non_sec_primary_documents"
    ).resolve()
    original_root = (
        PROJECT_ROOT
        / "output"
        / "industrials_cache"
        / "transportation"
        / "non_sec_primary_documents"
        / "extracted_text_sha256"
    ).resolve()
    original_before = inventory_sha256(original_root)
    tesseract_exe = Path(
        str(ocr_manifest.get("tesseract_executable") or "")
    )
    configure_tesseract_environment(tesseract_exe)
    options = ExtractionOptions(
        enable_pdf_ocr=True,
        max_pdf_pages=int(parser_cfg.get("max_pdf_pages", 0)),
        max_pdf_bytes=int(
            parser_cfg.get("max_pdf_bytes", MAX_DOCUMENT_BYTES)
        ),
        pdf_extraction_timeout_seconds=OCR_TIMEOUT_SECONDS,
    )
    throttle = DomainThrottle(0.35)
    results: list[dict[str, object]] = []
    recovered_sources: list[dict[str, str]] = []
    prior_results = {
        str(row["original_content_sha256"]): row
        for row in (
            read_csv(result_path)
            if args.retry_limitations and result_path.is_file()
            else []
        )
    }
    for original_hash, source in sorted(source_by_hash.items()):
        prior_result = prior_results.get(original_hash)
        if (
            prior_result is not None
            and prior_result.get("repair_status")
            == "RECOVERED_FULL_DOCUMENT"
        ):
            recovered_path = Path(
                str(prior_result["recovered_local_path"])
            )
            recovered_hash = str(
                prior_result["recovered_content_sha256"]
            )
            if (
                recovered_path.is_file()
                and file_sha256(recovered_path) == recovered_hash
                and cache_path(
                    isolated_root,
                    recovered_hash,
                ).is_file()
            ):
                results.append(dict(prior_result))
                for context in failed_context_rows:
                    if (
                        str(context["content_sha256"]).lower()
                        != original_hash
                    ):
                        continue
                    updated = dict(context)
                    document_name = f"{recovered_hash}.pdf"
                    updated.update(
                        {
                            "document_name": document_name,
                            "content_sha256": recovered_hash,
                            "cache_status": "CACHED_HASHED",
                            "local_path": str(recovered_path),
                            "primary_document": document_name,
                            "source_id": (
                                "dedicated_parser_transportation_"
                                "truncated_pdf_repair"
                            ),
                            "content_type": "application/pdf",
                            "content_bytes": str(
                                prior_result[
                                    "recovered_content_bytes"
                                ]
                            ),
                            "canonical_urls": str(
                                prior_result["canonical_url"]
                            ),
                        }
                    )
                    recovered_sources.append(updated)
                continue
        started = time.time()
        url = OFFICIAL_URL_OVERRIDES.get(
            original_hash,
            _url(source),
        )
        item_network_requests = 0
        fetched = _default_fetch(
            url,
            user_agent=HYDRATION_USER_AGENT,
            timeout_sec=float(args.timeout_sec),
            max_retries=int(args.max_retries),
            max_bytes=MAX_DOCUMENT_BYTES,
            throttle=throttle,
        )
        item_network_requests += fetched.network_request_count
        if (
            fetched.http_status != 200
            or len(fetched.payload) <= TRUNCATED_BYTES
        ):
            fallback = _default_fetch(
                url,
                user_agent=HYDRATION_USER_AGENT,
                timeout_sec=float(args.timeout_sec),
                max_retries=1,
                max_bytes=MAX_DOCUMENT_BYTES,
                throttle=throttle,
                preflight_head_for_cookie=True,
            )
            item_network_requests += fallback.network_request_count
            if (
                fallback.http_status == 200
                and len(fallback.payload) > len(fetched.payload)
            ):
                fetched = fallback
        recovered_hash = ""
        recovered_path: Path | None = None
        extracted = None
        repair_status = "FETCH_FAILED"
        error = fetched.error
        if (
            fetched.http_status == 200
            and len(fetched.payload) > TRUNCATED_BYTES
            and fetched.payload.startswith(b"%PDF")
        ):
            try:
                recovered_hash, recovered_path = store_content(
                    cache_root=isolated_root,
                    payload=fetched.payload,
                )
                document_name = f"{recovered_hash}.pdf"
                stat = recovered_path.stat()
                document = DocumentRef(
                    name=document_name,
                    path=str(recovered_path),
                    content_sha256=recovered_hash,
                    file_size=int(stat.st_size),
                    modified_ns=int(stat.st_mtime_ns),
                    is_primary=True,
                    is_full_submission=False,
                    source_kind=(
                        "transportation_non_sec_primary_document"
                    ),
                )
                extracted, _ = extract_document_once(
                    document,
                    content_type=(
                        fetched.content_type or "application/pdf"
                    ),
                    options=options,
                )
                if extracted.text.strip():
                    repair_status = "RECOVERED_FULL_DOCUMENT"
                    error = ""
                    for context in failed_context_rows:
                        if (
                            str(context["content_sha256"]).lower()
                            != original_hash
                        ):
                            continue
                        updated = dict(context)
                        updated.update(
                            {
                                "document_name": document_name,
                                "content_sha256": recovered_hash,
                                "cache_status": "CACHED_HASHED",
                                "local_path": str(recovered_path),
                                "primary_document": document_name,
                                "source_id": (
                                    "dedicated_parser_transportation_"
                                    "truncated_pdf_repair"
                                ),
                                "content_type": (
                                    fetched.content_type
                                    or "application/pdf"
                                ),
                                "content_bytes": str(
                                    len(fetched.payload)
                                ),
                                "canonical_urls": (
                                    fetched.final_url or url
                                ),
                            }
                        )
                        recovered_sources.append(updated)
                else:
                    repair_status = "EXTRACTION_EMPTY"
                    error = extracted.warning
            except Exception as exc:
                repair_status = "EXTRACTION_FAILED"
                error = f"{type(exc).__name__}: {exc}"
        elif not error:
            error = (
                "response is not a complete PDF larger than the "
                "sealed one-MiB truncation"
            )
        results.append(
            {
                "repair_version": TRUNCATED_REPAIR_VERSION,
                "original_content_sha256": original_hash,
                "ticker_contexts": "|".join(
                    sorted(tickers_by_hash[original_hash])
                ),
                "canonical_url": url,
                "http_status": fetched.http_status,
                "network_request_count": (
                    item_network_requests
                ),
                "original_content_bytes": TRUNCATED_BYTES,
                "recovered_content_bytes": len(fetched.payload),
                "recovered_content_sha256": recovered_hash,
                "recovered_local_path": (
                    str(recovered_path) if recovered_path else ""
                ),
                "cache_path": (
                    str(cache_path(isolated_root, recovered_hash))
                    if recovered_hash
                    else ""
                ),
                "repair_status": repair_status,
                "extraction_method": (
                    extracted.extraction_method if extracted else ""
                ),
                "ocr_used": int(
                    bool(extracted is not None and extracted.ocr_used)
                ),
                "page_count": (
                    extracted.page_count if extracted else 0
                ),
                "text_character_count": (
                    len(extracted.text) if extracted else 0
                ),
                "warning": (
                    extracted.warning if extracted else ""
                ),
                "error": error,
                "elapsed_seconds": round(
                    time.time() - started,
                    3,
                ),
            }
        )
        write_csv_atomic(result_path, RESULT_FIELDS, results)

    # The --retry-limitations reuse path appends prior rows and continues
    # without hitting the per-iteration flush above; one final write keeps
    # the sealed results file complete regardless of row ordering.
    write_csv_atomic(result_path, RESULT_FIELDS, results)

    combined_by_accession: dict[str, dict[str, str]] = {
        str(row["accession_number"]): row
        for row in recovered_ocr_rows
    }
    for row in recovered_sources:
        combined_by_accession[str(row["accession_number"])] = row
    union_rows = sorted(
        combined_by_accession.values(),
        key=lambda row: (
            row["ticker"],
            row["accession_number"],
            row["content_sha256"],
        ),
    )
    write_csv_atomic(
        union_source_path,
        DELTA_SOURCE_FIELDS,
        union_rows,
    )
    original_after = inventory_sha256(original_root)
    recovered_hash_count = sum(
        row["repair_status"] == "RECOVERED_FULL_DOCUMENT"
        for row in results
    )
    recovery_errors: list[str] = []
    if original_after != original_before:
        recovery_errors.append("original one-pass cache changed")
    if len(results) != EXPECTED_TRUNCATED_HASH_COUNT:
        recovery_errors.append("truncated repair result count changed")
    if not recovered_hash_count:
        recovery_errors.append("no truncated PDF was recovered")
    union_hash_count = len(
        {row["content_sha256"] for row in union_rows}
    )
    acceptance = (
        "FAIL"
        if recovery_errors
        else (
            "PASS"
            if recovered_hash_count == EXPECTED_TRUNCATED_HASH_COUNT
            else "PASS_WITH_EXPLICIT_LIMITATIONS"
        )
    )
    total_network_requests = sum(
        int(str(row.get("network_request_count") or 0))
        for row in results
    )
    payload = {
        **preflight,
        "acceptance": acceptance,
        "gate": "DP7C_TRUNCATED_PDF_REPAIR_AND_OCR_UNION",
        "recovered_truncated_hash_count": recovered_hash_count,
        "unrecovered_truncated_hash_count": (
            EXPECTED_TRUNCATED_HASH_COUNT - recovered_hash_count
        ),
        "recovered_truncated_context_count": len(recovered_sources),
        "union_context_count": len(union_rows),
        "union_unique_content_hash_count": union_hash_count,
        "union_ticker_count": len(
            {row["ticker"] for row in union_rows}
        ),
        "repair_status_counts": {
            status: sum(
                row["repair_status"] == status for row in results
            )
            for status in sorted(
                {str(row["repair_status"]) for row in results}
            )
        },
        "network_requests": total_network_requests,
        "parser_invocations": 0,
        "original_cache_inventory_before": {
            "file_count": original_before[0],
            "sha256": original_before[1],
        },
        "original_cache_inventory_after": {
            "file_count": original_after[0],
            "sha256": original_after[1],
        },
        "original_cache_unchanged": (
            original_before == original_after
        ),
        "errors": recovery_errors,
        "inputs": {
            "ocr_manifest": {
                "path": str(ocr_manifest_path.resolve()),
                "sha256": file_sha256(ocr_manifest_path),
            },
            "ocr_results": {
                "path": str(ocr_results_path.resolve()),
                "sha256": file_sha256(ocr_results_path),
            },
            "ocr_source_manifest": {
                "path": str(ocr_source_path.resolve()),
                "sha256": file_sha256(ocr_source_path),
            },
        },
        "artifacts": {
            "truncated_pdf_repair_results": {
                "path": str(result_path.resolve()),
                "row_count": len(results),
                "sha256": file_sha256(result_path),
            },
            "union_source_manifest": {
                "path": str(union_source_path.resolve()),
                "row_count": len(union_rows),
                "sha256": file_sha256(union_source_path),
            },
        },
        "next_gate": (
            "EXECUTE_ONE_SEMANTIC_BATCH_OVER_RECOVERY_UNION"
            if acceptance != "FAIL"
            else "STOP_TRUNCATED_PDF_REPAIR_FAILED"
        ),
    }
    _write_json(manifest_path, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if acceptance != "FAIL" else 2


if __name__ == "__main__":
    raise SystemExit(main())
