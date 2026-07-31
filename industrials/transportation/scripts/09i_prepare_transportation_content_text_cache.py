#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.contracts import DocumentRef, file_sha256  # noqa: E402
from dedicated_parser.source_manifest import (  # noqa: E402
    load_source_manifest,
)
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
    cache_root_from_document,
    extract_document_once,
    legacy_word_docx_path,
    load_cached_text,
    repair_pdf_cache_if_better,
)
from industrials.transportation.efficient_parser_batch import (  # noqa: E402
    read_csv,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
)


RESULT_FIELDS = (
    "content_sha256",
    "document_name",
    "content_type",
    "content_bytes",
    "cache_status",
    "cache_path",
    "extraction_method",
    "text_character_count",
    "page_count",
    "ocr_used",
    "warning",
    "elapsed_seconds",
    "error",
)

WORD_REQUEST_FIELDS = (
    "content_sha256",
    "local_path",
    "converted_path",
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Extract every unique direct-document hash once into a "
            "resumable text cache. Identical ticker contexts reuse it."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--workers", type=int, default=0)
    parser.add_argument("--progress-every", type=int, default=25)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true")
    mode.add_argument("--status", action="store_true")
    mode.add_argument(
        "--repair-limitations",
        action="store_true",
        help=(
            "Revisit only cached warning/empty PDF results with the bounded "
            "local PyMuPDF fallback; no source retrieval or parser call."
        ),
    )
    return parser.parse_args(argv)


def _json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected JSON object")
    return payload


def _document_index(
    *,
    source_rows: list[dict[str, str]],
    source_manifest: Any,
) -> dict[str, tuple[DocumentRef, str]]:
    output: dict[str, tuple[DocumentRef, str]] = {}
    for row in source_rows:
        key = (
            str(row["ticker"]).upper(),
            str(row["accession_number"]),
        )
        documents = source_manifest.direct_documents.get(key, ())
        if len(documents) != 1:
            raise ValueError(f"Expected one direct document for {key}, got {len(documents)}")
        document = documents[0]
        content_hash = document.content_sha256
        prior = output.get(content_hash)
        candidate = (document, str(row.get("content_type") or ""))
        if prior is not None and prior[0].name != document.name:
            raise ValueError(f"Content hash has conflicting document names: {content_hash}")
        output[content_hash] = candidate
    return output


def _word_conversion(
    *,
    documents: dict[str, tuple[DocumentRef, str]],
    output_dir: Path,
    execute: bool,
) -> tuple[int, int, Path, Path]:
    requests: list[dict[str, str]] = []
    for content_hash, (document, _) in sorted(documents.items()):
        if not document.name.lower().endswith(".doc"):
            continue
        cache_root = cache_root_from_document(Path(document.path))
        requests.append(
            {
                "content_sha256": content_hash,
                "local_path": document.path,
                "converted_path": str(legacy_word_docx_path(cache_root, content_hash)),
            }
        )
    request_path = output_dir / "transportation_legacy_word_conversion_requests.csv"
    result_path = output_dir / "transportation_legacy_word_conversion_results.csv"
    write_csv_atomic(request_path, WORD_REQUEST_FIELDS, requests)
    if not execute:
        ready = sum(
            Path(row["converted_path"]).is_file() and Path(row["converted_path"]).stat().st_size > 0 for row in requests
        )
        return len(requests), ready, request_path, result_path
    converter = PROJECT_ROOT / "industrials" / "transportation" / "scripts" / "convert_transportation_legacy_word.ps1"
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(converter),
            "-InputCsv",
            str(request_path),
            "-OutputCsv",
            str(result_path),
        ],
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"Legacy Word conversion failed code={completed.returncode}")
    results = read_csv(result_path)
    failed = [row for row in results if str(row.get("status") or "").startswith("FAILED_")]
    if len(results) != len(requests) or failed:
        raise RuntimeError(
            "Legacy Word conversion results do not reconcile: "
            f"requests={len(requests)} results={len(results)} "
            f"failed={len(failed)}"
        )
    return len(requests), len(results), request_path, result_path


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.workers < 0 or args.progress_every <= 0:
        raise ValueError("workers must be non-negative and progress positive")
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    parser_cfg = family_config(config, MODEL_FAMILY)["dedicated_parser"]
    if bool(parser_cfg.get("parser_execution_authorized")):
        raise ValueError("General parser authorization must remain disabled")
    base_dir = config_path.parent
    asof_date = str(parser_cfg["source_census_asof_date"])
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else resolve_path(parser_cfg["output_root"], base_dir=base_dir) / asof_date
    )
    source_path = output_dir / "transportation_non_sec_direct_delta_source_manifest.csv"
    seal_path = output_dir / "transportation_efficient_parser_batch_manifest.json"
    plan_gate_path = output_dir / "transportation_non_sec_direct_delta_plan_gate.json"
    for path in (source_path, seal_path, plan_gate_path):
        if not path.is_file():
            raise FileNotFoundError(path)
    seal = _json(seal_path)
    plan_gate = _json(plan_gate_path)
    source_hash = file_sha256(source_path)
    if (
        seal.get("acceptance") != "PASS"
        or plan_gate.get("acceptance") != "PASS"
        or plan_gate.get("source_manifest_sha256") != source_hash
    ):
        raise ValueError("Direct delta seal or plan gate is not PASS")
    source_rows = read_csv(source_path)
    source_manifest = load_source_manifest(source_path)
    documents = _document_index(
        source_rows=source_rows,
        source_manifest=source_manifest,
    )
    expected_hashes = int((seal.get("direct_delta_summary") or {}).get("new_unique_content_hash_count", 0))
    if len(documents) != expected_hashes:
        raise ValueError(f"Unique hash count mismatch expected={expected_hashes} actual={len(documents)}")
    options = ExtractionOptions(
        enable_pdf_ocr=bool(parser_cfg.get("pdf_ocr_enabled")),
        max_pdf_pages=int(parser_cfg.get("max_pdf_pages", 250)),
        max_pdf_bytes=int(parser_cfg.get("max_pdf_bytes", 25_000_000)),
        pdf_extraction_timeout_seconds=float(parser_cfg.get("pdf_extraction_timeout_seconds", 30.0)),
    )
    conversion_count, conversion_ready, conversion_request_path, conversion_result_path = _word_conversion(
        documents=documents,
        output_dir=output_dir,
        execute=bool(args.execute),
    )
    result_path = output_dir / "transportation_content_text_cache_results.csv"
    progress_path = output_dir / "transportation_content_text_cache_progress.json"
    manifest_path = output_dir / "transportation_content_text_cache_manifest.json"
    workers = args.workers or int(parser_cfg.get("workers") or 4)
    started = time.time()
    lock = threading.Lock()
    results: list[dict[str, object]] = []

    def process(
        content_hash: str,
        document: DocumentRef,
        content_type: str,
    ) -> dict[str, object]:
        item_started = time.time()
        try:
            cache_root = cache_root_from_document(Path(document.path))
            if args.repair_limitations:
                existing = load_cached_text(
                    cache_root=cache_root,
                    content_sha256=content_hash,
                    options=options,
                )
                if existing is None:
                    return {
                        "content_sha256": content_hash,
                        "document_name": document.name,
                        "content_type": content_type,
                        "content_bytes": document.file_size,
                        "cache_status": "MISSING",
                        "cache_path": str(cache_path(cache_root, content_hash)),
                        "extraction_method": "",
                        "text_character_count": 0,
                        "page_count": 0,
                        "ocr_used": 0,
                        "warning": "",
                        "elapsed_seconds": round(time.time() - item_started, 3),
                        "error": "",
                    }
                if existing.warning or not existing.text.strip():
                    extracted, status = repair_pdf_cache_if_better(
                        document,
                        options=options,
                    )
                else:
                    extracted = existing
                    status = "CACHE_NOT_LIMITED"
            elif args.execute:
                extracted, status = extract_document_once(
                    document,
                    content_type=content_type,
                    options=options,
                )
            else:
                extracted = load_cached_text(
                    cache_root=cache_root,
                    content_sha256=content_hash,
                    options=options,
                )
                status = "CACHE_HIT" if extracted is not None else "MISSING"
                if extracted is None:
                    return {
                        "content_sha256": content_hash,
                        "document_name": document.name,
                        "content_type": content_type,
                        "content_bytes": document.file_size,
                        "cache_status": status,
                        "cache_path": str(cache_path(cache_root, content_hash)),
                        "extraction_method": "",
                        "text_character_count": 0,
                        "page_count": 0,
                        "ocr_used": 0,
                        "warning": "",
                        "elapsed_seconds": round(time.time() - item_started, 3),
                        "error": "",
                    }
            return {
                "content_sha256": content_hash,
                "document_name": document.name,
                "content_type": content_type,
                "content_bytes": document.file_size,
                "cache_status": status,
                "cache_path": str(cache_path(cache_root, content_hash)),
                "extraction_method": extracted.extraction_method,
                "text_character_count": len(extracted.text),
                "page_count": extracted.page_count,
                "ocr_used": int(extracted.ocr_used),
                "warning": extracted.warning,
                "elapsed_seconds": round(time.time() - item_started, 3),
                "error": "",
            }
        except Exception as exc:
            return {
                "content_sha256": content_hash,
                "document_name": document.name,
                "content_type": content_type,
                "content_bytes": document.file_size,
                "cache_status": "FAILED",
                "cache_path": "",
                "extraction_method": "",
                "text_character_count": 0,
                "page_count": 0,
                "ocr_used": 0,
                "warning": "",
                "elapsed_seconds": round(time.time() - item_started, 3),
                "error": f"{type(exc).__name__}: {exc}",
            }

    def write_progress() -> None:
        ordered = sorted(results, key=lambda row: str(row["content_sha256"]))
        write_csv_atomic(result_path, RESULT_FIELDS, ordered)
        status_counts = Counter(str(row["cache_status"]) for row in ordered)
        write_text_atomic(
            progress_path,
            json.dumps(
                {
                    "execute": bool(args.execute),
                    "repair_limitations": bool(args.repair_limitations),
                    "source_manifest_sha256": source_hash,
                    "extraction_options": {
                        "enable_pdf_ocr": options.enable_pdf_ocr,
                        "max_pdf_pages": options.max_pdf_pages,
                        "max_pdf_bytes": options.max_pdf_bytes,
                        "pdf_extraction_timeout_seconds": (options.pdf_extraction_timeout_seconds),
                    },
                    "extraction_options_sha256": options.content_sha256,
                    "planned_unique_hash_count": len(documents),
                    "completed_unique_hash_count": len(ordered),
                    "remaining_unique_hash_count": len(documents) - len(ordered),
                    "status_counts": dict(sorted(status_counts.items())),
                    "elapsed_seconds": round(time.time() - started, 3),
                    "parser_invocations": 0,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(process, content_hash, document, content_type): content_hash
            for content_hash, (document, content_type) in documents.items()
        }
        for future in as_completed(futures):
            row = future.result()
            with lock:
                results.append(row)
                completed = len(results)
                if completed % args.progress_every == 0 or completed == len(documents):
                    write_progress()
                    print(
                        "[content-cache] "
                        f"{completed}/{len(documents)} "
                        f"failed={sum(r['cache_status'] == 'FAILED' for r in results)}",
                        flush=True,
                    )
    ordered = sorted(results, key=lambda row: str(row["content_sha256"]))
    status_counts = Counter(str(row["cache_status"]) for row in ordered)
    failures = [row for row in ordered if row["cache_status"] == "FAILED"]
    missing = [row for row in ordered if row["cache_status"] == "MISSING"]
    warnings = [row for row in ordered if str(row["warning"])]
    empty_text = [
        row
        for row in ordered
        if int(str(row["text_character_count"])) == 0
        and row["cache_status"] not in {"FAILED", "MISSING"}
    ]
    pymupdf_rows = [row for row in ordered if row["extraction_method"] == "pdf_pymupdf_targeted_recovery"]
    pymupdf_nonempty = [
        row for row in pymupdf_rows if int(str(row["text_character_count"])) > 0
    ]
    pymupdf_empty = [
        row for row in pymupdf_rows if int(str(row["text_character_count"])) == 0
    ]
    if failures:
        acceptance = "FAIL"
    elif not (args.execute or args.repair_limitations):
        acceptance = "STATUS_COMPLETE"
    elif missing:
        acceptance = "FAIL"
    elif warnings or empty_text:
        acceptance = "PASS_WITH_EXPLICIT_EXTRACTION_LIMITATIONS"
    else:
        acceptance = "PASS"
    payload = {
        "acceptance": acceptance,
        "gate": "DP6U_UNIQUE_CONTENT_TEXT_CACHE",
        "repair_limitations": bool(args.repair_limitations),
        "model_family": MODEL_FAMILY,
        "asof_date": asof_date,
        "source_manifest_path": str(source_path.resolve()),
        "source_manifest_sha256": source_hash,
        "extraction_options_sha256": options.content_sha256,
        "planned_unique_hash_count": len(documents),
        "completed_unique_hash_count": len(ordered),
        "status_counts": dict(sorted(status_counts.items())),
        "failure_count": len(failures),
        "warning_count": len(warnings),
        "empty_text_count": len(empty_text),
        "legacy_word_conversion_count": conversion_count,
        "legacy_word_conversion_ready_count": conversion_ready,
        "workers": workers,
        "elapsed_seconds": round(time.time() - started, 3),
        "results": {
            "path": str(result_path.resolve()),
            "row_count": len(ordered),
            "sha256": file_sha256(result_path),
        },
        "legacy_word_conversion_requests": {
            "path": str(conversion_request_path.resolve()),
            "sha256": file_sha256(conversion_request_path),
        },
        "legacy_word_conversion_results": (
            {
                "path": str(conversion_result_path.resolve()),
                "sha256": file_sha256(conversion_result_path),
            }
            if conversion_result_path.is_file()
            else None
        ),
        "network_requests": 0,
        "physical_document_extraction_count": int(status_counts.get("EXTRACTED_AND_CACHED", 0)),
        "targeted_pymupdf_cache_count": len(pymupdf_rows),
        "targeted_pymupdf_text_recovery_count": len(pymupdf_nonempty),
        "targeted_pymupdf_validated_empty_count": len(pymupdf_empty),
        "parser_invocations": 0,
        "feature_build_invocations": 0,
        "historical_materialization_invocations": 0,
        "calibration_invocations": 0,
        "portfolio_invocations": 0,
        "general_parser_execution_authorized": False,
        "one_shot_direct_delta_execution_authorized": (
            acceptance in {"PASS", "PASS_WITH_EXPLICIT_EXTRACTION_LIMITATIONS"}
        ),
        "production_promotion_authorized": False,
        "next_gate": (
            "EXECUTE_ONE_RESUMABLE_DIRECT_DELTA_BATCH"
            if acceptance in {"PASS", "PASS_WITH_EXPLICIT_EXTRACTION_LIMITATIONS"}
            else "REPAIR_OR_RESUME_UNIQUE_CONTENT_TEXT_CACHE"
        ),
    }
    write_text_atomic(
        manifest_path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 2 if acceptance == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
