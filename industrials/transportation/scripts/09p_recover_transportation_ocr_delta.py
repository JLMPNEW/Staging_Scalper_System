#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.contracts import (  # noqa: E402
    DocumentRef,
    file_sha256,
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
)
from industrials.transportation.efficient_parser_batch import (  # noqa: E402
    DELTA_SOURCE_FIELDS,
    read_csv,
)
from industrials.transportation.ocr_recovery import (  # noqa: E402
    OCR_RECOVERY_VERSION,
    OCR_RESULT_FIELDS,
    OCR_TIMEOUT_SECONDS,
    build_recovered_source_rows,
    configure_tesseract_environment,
    inventory_sha256,
    isolate_document,
    summarize_ocr_results,
    tesseract_candidates,
    verify_tesseract,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
)


EXPECTED_OCR_HASH_COUNT = 34
EXPECTED_OCR_CONTEXT_COUNT = 38


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "OCR only the 34 hash-sealed native-text-empty transportation "
            "PDFs in an isolated cache namespace. The original one-pass "
            "content cache is never modified."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--tesseract-exe", type=Path, default=None)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--progress-every", type=int, default=1)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true")
    mode.add_argument("--status", action="store_true")
    return parser.parse_args(argv)


def _read_json(path: Path) -> dict[str, Any]:
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


def _resolve_tesseract(
    explicit: Path | None,
) -> tuple[Path, str, str, list[str]]:
    errors: list[str] = []
    candidates = (
        (explicit,)
        if explicit is not None
        else tesseract_candidates(
            python_executable=Path(sys.executable)
        )
    )
    for candidate in candidates:
        if candidate is None or not candidate.expanduser().is_file():
            continue
        try:
            version, content_hash = verify_tesseract(candidate)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            errors.append(
                f"{candidate}:{type(exc).__name__}:{exc}"
            )
            continue
        return (
            candidate.expanduser().resolve(),
            version,
            content_hash,
            errors,
        )
    raise RuntimeError(
        "No working Tesseract executable was found; "
        + "; ".join(errors)
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.workers < 1 or args.progress_every < 1:
        raise ValueError("workers and progress-every must be positive")
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    parser_cfg = family_config(config, MODEL_FAMILY)[
        "dedicated_parser"
    ]
    if bool(parser_cfg.get("parser_execution_authorized")):
        raise ValueError(
            "OCR recovery requires general parser execution disabled"
        )
    asof_date = str(parser_cfg["source_census_asof_date"])
    output_dir = (
        resolve_path(parser_cfg["output_root"], base_dir=config_path.parent)
        / asof_date
    )
    scope_path = (
        output_dir / "transportation_bounded_repair_scope.csv"
    )
    scope_manifest_path = (
        output_dir / "transportation_bounded_repair_scope_manifest.json"
    )
    base_source_path = (
        output_dir
        / "transportation_non_sec_direct_delta_source_manifest.csv"
    )
    base_cache_path = (
        output_dir / "transportation_content_text_cache_results.csv"
    )
    base_cache_manifest_path = (
        output_dir / "transportation_content_text_cache_manifest.json"
    )
    result_path = (
        output_dir / "transportation_ocr_delta_cache_results.csv"
    )
    recovered_source_path = (
        output_dir / "transportation_ocr_delta_source_manifest.csv"
    )
    progress_path = (
        output_dir / "transportation_ocr_delta_progress.json"
    )
    manifest_path = (
        output_dir / "transportation_ocr_delta_manifest.json"
    )
    required = (
        scope_path,
        scope_manifest_path,
        base_source_path,
        base_cache_path,
        base_cache_manifest_path,
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Missing OCR recovery inputs: {missing}")

    scope_manifest = _read_json(scope_manifest_path)
    base_cache_manifest = _read_json(base_cache_manifest_path)
    scope_rows = read_csv(scope_path)
    base_source_rows = read_csv(base_source_path)
    base_cache_rows = read_csv(base_cache_path)
    empty_hashes = {
        str(row["content_sha256"]).lower()
        for row in scope_rows
        if row["repair_lane"] == "EMPTY_PDF_OCR"
    }
    empty_cache_rows = {
        str(row["content_sha256"]).lower(): row
        for row in base_cache_rows
        if str(row["content_sha256"]).lower() in empty_hashes
        and row["cache_status"] == "CACHE_VALIDATED_EMPTY_PYMUPDF"
    }
    context_rows = [
        row
        for row in base_source_rows
        if str(row["content_sha256"]).lower() in empty_hashes
    ]
    errors: list[str] = []
    scope_artifact = (
        (scope_manifest.get("artifacts") or {}).get(
            "bounded_repair_scope"
        )
        or {}
    )
    cache_artifact = base_cache_manifest.get("results") or {}
    if (
        scope_manifest.get("acceptance") != "PASS"
        or str(scope_artifact.get("sha256") or "")
        != file_sha256(scope_path)
    ):
        errors.append("bounded OCR scope is not hash-sealed")
    if (
        base_cache_manifest.get("acceptance")
        != "PASS_WITH_EXPLICIT_EXTRACTION_LIMITATIONS"
        or str(cache_artifact.get("sha256") or "")
        != file_sha256(base_cache_path)
    ):
        errors.append("base content cache is not hash-sealed")
    if len(empty_hashes) != EXPECTED_OCR_HASH_COUNT:
        errors.append(
            f"empty hashes={len(empty_hashes)} "
            f"expected={EXPECTED_OCR_HASH_COUNT}"
        )
    if len(empty_cache_rows) != len(empty_hashes):
        errors.append("not every OCR hash is a validated-empty cache row")
    if len(context_rows) != EXPECTED_OCR_CONTEXT_COUNT:
        errors.append(
            f"OCR contexts={len(context_rows)} "
            f"expected={EXPECTED_OCR_CONTEXT_COUNT}"
        )

    try:
        (
            tesseract_exe,
            tesseract_version,
            tesseract_sha256,
            rejected_engines,
        ) = _resolve_tesseract(args.tesseract_exe)
    except RuntimeError as exc:
        tesseract_exe = Path()
        tesseract_version = ""
        tesseract_sha256 = ""
        rejected_engines = [str(exc)]
        errors.append(str(exc))
    preflight = {
        "acceptance": "PASS_PREFLIGHT" if not errors else "FAIL",
        "gate": "DP7B_BOUNDED_OCR_RECOVERY_PREFLIGHT",
        "recovery_version": OCR_RECOVERY_VERSION,
        "model_family": MODEL_FAMILY,
        "asof_date": asof_date,
        "ocr_hash_count": len(empty_hashes),
        "ocr_context_count": len(context_rows),
        "ocr_page_count": sum(
            int(row.get("page_count") or 0)
            for row in empty_cache_rows.values()
        ),
        "tesseract_executable": (
            str(tesseract_exe) if str(tesseract_exe) else ""
        ),
        "tesseract_version": tesseract_version,
        "tesseract_sha256": tesseract_sha256,
        "rejected_tesseract_candidates": rejected_engines,
        "ocr_timeout_seconds": OCR_TIMEOUT_SECONDS,
        "workers": args.workers,
        "original_cache_mutation_authorized": False,
        "parser_invocations": 0,
        "network_requests": 0,
        "errors": errors,
    }
    if args.status or not args.execute:
        prior = _read_json(manifest_path) if manifest_path.is_file() else {}
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
        prior = _read_json(manifest_path)
        if (
            prior.get("acceptance")
            in {"PASS", "PASS_WITH_EXPLICIT_LIMITATIONS"}
            and str(prior.get("tesseract_sha256") or "")
            == tesseract_sha256
            and str(
                (
                    (prior.get("inputs") or {}).get("bounded_scope")
                    or {}
                ).get("sha256")
                or ""
            )
            == file_sha256(scope_path)
        ):
            print(
                json.dumps(
                    {**prior, "idempotent_reuse": True},
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0

    configure_tesseract_environment(tesseract_exe)
    options = ExtractionOptions(
        enable_pdf_ocr=True,
        max_pdf_pages=int(parser_cfg.get("max_pdf_pages", 250)),
        max_pdf_bytes=int(
            parser_cfg.get("max_pdf_bytes", 25_000_000)
        ),
        pdf_extraction_timeout_seconds=OCR_TIMEOUT_SECONDS,
    )
    isolated_root = (
        PROJECT_ROOT
        / "output"
        / "industrials_cache"
        / "transportation"
        / "ocr_delta"
        / "non_sec_primary_documents"
    ).resolve()
    original_cache_root = cache_root_from_document(
        Path(context_rows[0]["local_path"])
    )
    original_inventory_before = inventory_sha256(
        original_cache_root / "extracted_text_sha256"
    )

    source_by_hash: dict[str, dict[str, str]] = {}
    tickers_by_hash: dict[str, set[str]] = {}
    for row in context_rows:
        content_hash = str(row["content_sha256"]).lower()
        source_by_hash.setdefault(content_hash, row)
        tickers_by_hash.setdefault(content_hash, set()).add(row["ticker"])
    isolation_methods: dict[str, str] = {}
    isolated_paths: dict[str, Path] = {}
    for content_hash, row in sorted(source_by_hash.items()):
        suffix = Path(row["local_path"]).suffix.lower() or ".pdf"
        isolated = (
            isolated_root
            / "sha256"
            / content_hash[:2]
            / f"{content_hash}{suffix}"
        )
        isolation_methods[content_hash] = isolate_document(
            source_path=Path(row["local_path"]),
            target_path=isolated,
            expected_sha256=content_hash,
        )
        isolated_paths[content_hash] = isolated

    started = time.time()
    lock = threading.Lock()
    results: list[dict[str, object]] = []

    def process(content_hash: str) -> dict[str, object]:
        item_started = time.time()
        source = source_by_hash[content_hash]
        isolated = isolated_paths[content_hash]
        stat = isolated.stat()
        document = DocumentRef(
            name=source["document_name"],
            path=str(isolated),
            content_sha256=content_hash,
            file_size=int(stat.st_size),
            modified_ns=int(stat.st_mtime_ns),
            is_primary=True,
            is_full_submission=False,
            source_kind=(
                "transportation_non_sec_primary_document"
            ),
        )
        try:
            extracted, operation = extract_document_once(
                document,
                content_type=source.get("content_type", ""),
                options=options,
            )
            recovered = bool(
                extracted.ocr_used and extracted.text.strip()
            )
            status = (
                "RECOVERED_OCR"
                if recovered
                else (
                    "OCR_FAILED"
                    if extracted.warning
                    else "OCR_EMPTY"
                )
            )
            error = ""
        except Exception as exc:
            extracted = None
            operation = "FAILED"
            status = "OCR_FAILED"
            error = f"{type(exc).__name__}:{exc}"
        return {
            "recovery_version": OCR_RECOVERY_VERSION,
            "content_sha256": content_hash,
            "document_name": source["document_name"],
            "ticker_contexts": "|".join(
                sorted(tickers_by_hash[content_hash])
            ),
            "page_count": (
                extracted.page_count
                if extracted is not None
                else empty_cache_rows[content_hash]["page_count"]
            ),
            "content_bytes": stat.st_size,
            "isolated_local_path": str(isolated),
            "isolation_method": isolation_methods[content_hash],
            "cache_path": str(
                cache_path(isolated_root, content_hash)
            ),
            "cache_status": status,
            "extraction_method": (
                extracted.extraction_method
                if extracted is not None
                else ""
            ),
            "ocr_used": int(
                bool(extracted is not None and extracted.ocr_used)
            ),
            "text_character_count": (
                len(extracted.text) if extracted is not None else 0
            ),
            "warning": (
                extracted.warning if extracted is not None else ""
            ),
            "elapsed_seconds": round(
                time.time() - item_started, 3
            ),
            "error": (
                error
                if error
                else (
                    ""
                    if operation
                    in {
                        "EXTRACTED_AND_CACHED",
                        "CACHE_HIT",
                        "CACHE_POST_LOCK_HIT",
                        "CACHE_WAIT_HIT",
                    }
                    else str(operation)
                )
            ),
        }

    def write_progress() -> None:
        ordered = sorted(
            results, key=lambda row: str(row["content_sha256"])
        )
        write_csv_atomic(result_path, OCR_RESULT_FIELDS, ordered)
        _write_json(
            progress_path,
            {
                **preflight,
                "acceptance": "RUNNING",
                "completed_hash_count": len(ordered),
                "remaining_hash_count": (
                    len(source_by_hash) - len(ordered)
                ),
                **summarize_ocr_results(ordered),
                "elapsed_seconds": round(time.time() - started, 3),
            },
        )

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(process, content_hash): content_hash
            for content_hash in sorted(source_by_hash)
        }
        for future in as_completed(futures):
            row = future.result()
            with lock:
                results.append(row)
                completed = len(results)
                if (
                    completed % args.progress_every == 0
                    or completed == len(futures)
                ):
                    write_progress()
                    print(
                        "[ocr-delta] "
                        f"{completed}/{len(futures)} "
                        f"recovered={sum(r['cache_status'] == 'RECOVERED_OCR' for r in results)} "
                        f"failed={sum(r['cache_status'] == 'OCR_FAILED' for r in results)}",
                        flush=True,
                    )

    ordered = sorted(
        results, key=lambda row: str(row["content_sha256"])
    )
    summary = summarize_ocr_results(ordered)
    recovered_hashes = {
        str(row["content_sha256"]): isolated_paths[
            str(row["content_sha256"])
        ]
        for row in ordered
        if row["cache_status"] == "RECOVERED_OCR"
    }
    recovered_source_rows = build_recovered_source_rows(
        base_rows=context_rows,
        recovered_paths=recovered_hashes,
    )
    write_csv_atomic(
        recovered_source_path,
        DELTA_SOURCE_FIELDS,
        recovered_source_rows,
    )
    original_inventory_after = inventory_sha256(
        original_cache_root / "extracted_text_sha256"
    )
    execution_errors: list[str] = []
    if original_inventory_after != original_inventory_before:
        execution_errors.append("original one-pass cache changed")
    if len(ordered) != EXPECTED_OCR_HASH_COUNT:
        execution_errors.append("OCR result cardinality changed")
    if not recovered_hashes:
        execution_errors.append("OCR recovered no usable text")
    recovered_context_count = len(recovered_source_rows)
    recovered_ticker_count = len(
        {row["ticker"] for row in recovered_source_rows}
    )
    acceptance = (
        "FAIL"
        if execution_errors
        else (
            "PASS"
            if len(recovered_hashes) == EXPECTED_OCR_HASH_COUNT
            else "PASS_WITH_EXPLICIT_LIMITATIONS"
        )
    )
    payload = {
        **preflight,
        "acceptance": acceptance,
        "gate": "DP7B_BOUNDED_OCR_RECOVERY",
        **summary,
        "ocr_recovered_context_count": recovered_context_count,
        "ocr_recovered_ticker_count": recovered_ticker_count,
        "ocr_extraction_options_sha256": options.content_sha256,
        "original_cache_inventory_before": {
            "file_count": original_inventory_before[0],
            "sha256": original_inventory_before[1],
        },
        "original_cache_inventory_after": {
            "file_count": original_inventory_after[0],
            "sha256": original_inventory_after[1],
        },
        "original_cache_unchanged": (
            original_inventory_after == original_inventory_before
        ),
        "ocr_invocations": len(ordered),
        "parser_invocations": 0,
        "network_requests": 0,
        "feature_build_invocations": 0,
        "historical_materialization_invocations": 0,
        "calibration_invocations": 0,
        "portfolio_invocations": 0,
        "production_promotion_authorized": False,
        "errors": execution_errors,
        "inputs": {
            "bounded_scope": {
                "path": str(scope_path.resolve()),
                "sha256": file_sha256(scope_path),
            },
            "base_source_manifest": {
                "path": str(base_source_path.resolve()),
                "sha256": file_sha256(base_source_path),
            },
            "base_cache_results": {
                "path": str(base_cache_path.resolve()),
                "sha256": file_sha256(base_cache_path),
            },
        },
        "artifacts": {
            "ocr_cache_results": {
                "path": str(result_path.resolve()),
                "row_count": len(ordered),
                "sha256": file_sha256(result_path),
            },
            "ocr_delta_source_manifest": {
                "path": str(recovered_source_path.resolve()),
                "row_count": len(recovered_source_rows),
                "sha256": file_sha256(recovered_source_path),
            },
        },
        "next_gate": (
            "EXECUTE_RECOVERED_OCR_CONTEXTS_ONLY"
            if acceptance != "FAIL"
            else "STOP_NO_USABLE_OCR_RECOVERY"
        ),
    }
    _write_json(manifest_path, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if acceptance != "FAIL" else 2


if __name__ == "__main__":
    raise SystemExit(main())
