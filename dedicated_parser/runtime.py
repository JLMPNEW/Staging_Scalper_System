from __future__ import annotations

import os
import sqlite3
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from importlib import import_module
from pathlib import Path
from typing import Iterable

from dedicated_parser.adapters import load_extractor, load_fact_mapper
from dedicated_parser.contracts import WorkItem, WorkResult, file_sha256
from dedicated_parser.providers.arelle_provider import extract_facts
from dedicated_parser.providers.edgartools_provider import inspect_full_submission
from dedicated_parser.policy import apply_review_policies
from dedicated_parser.storage import (
    mark_work_started,
    persist_result,
    register_work,
)


def default_worker_count() -> int:
    return max(1, min(6, (os.cpu_count() or 2) - 1))


def validate_provider_dependencies(
    *,
    enable_arelle: bool,
    enable_edgartools: bool,
) -> None:
    required_modules = {
        "Arelle": ("arelle.Cntlr", enable_arelle, "--disable-arelle"),
        "EdgarTools": (
            "edgar.sgml",
            enable_edgartools,
            "--disable-edgartools",
        ),
    }
    missing: list[str] = []
    for provider, (module_name, enabled, disable_flag) in required_modules.items():
        if not enabled:
            continue
        try:
            import_module(module_name)
        except ImportError:
            missing.append(
                f"{provider} ({module_name}; explicitly disable with "
                f"{disable_flag})"
            )
    if missing:
        raise RuntimeError(
            "Enabled parser dependencies are missing: "
            + ", ".join(missing)
            + ". Install dedicated_parser/requirements.txt in the active "
            "Python environment or explicitly disable the unavailable provider."
        )


def _validate_document(document: object) -> None:
    path = Path(str(getattr(document, "path")))
    stat = path.stat()
    if (
        int(stat.st_size) != int(getattr(document, "file_size"))
        or int(stat.st_mtime_ns) != int(getattr(document, "modified_ns"))
    ):
        actual_hash = file_sha256(path)
        if actual_hash != str(getattr(document, "content_sha256")):
            raise RuntimeError(f"Cached document changed after planning: {path}")


def parse_work_item(
    item: WorkItem,
    *,
    provider_state_dir: str,
) -> WorkResult:
    started = time.perf_counter()
    metadata: dict[str, object] = {}
    try:
        for document in item.documents:
            _validate_document(document)
        if item.enable_edgartools:
            full_submission = next(
                (
                    document
                    for document in item.documents
                    if document.is_full_submission
                ),
                None,
            )
            if full_submission is not None:
                metadata["edgartools"] = inspect_full_submission(
                    Path(full_submission.path),
                    state_dir=Path(provider_state_dir),
                )

        facts = []
        if item.enable_arelle:
            entrypoint = next(
                (
                    document
                    for document in item.documents
                    if document.is_primary
                    and Path(document.name).suffix.lower()
                    in {".htm", ".html", ".xhtml", ".xml"}
                ),
                None,
            )
            patterns = tuple(
                pattern
                for request in item.requested_metrics
                for pattern in request.concept_patterns
            )
            if entrypoint is not None and patterns:
                facts, arelle_metadata = extract_facts(
                    Path(entrypoint.path),
                    concept_patterns=patterns,
                )
                metadata["arelle"] = arelle_metadata

        extractor = load_extractor(item.adapter_path)
        evidence = list(extractor(item))
        fact_mapper = load_fact_mapper(item.adapter_path)
        if fact_mapper is not None:
            evidence.extend(fact_mapper(item, tuple(facts)))
        reviewed_evidence = apply_review_policies(item, evidence)
        return WorkResult(
            work_key=item.work_key,
            model_family=item.model_family,
            adapter_version=item.adapter_version,
            filing=item.filing,
            parser_release=item.parser_release,
            status="COMPLETED",
            normalized_facts=tuple(facts),
            metric_evidence=reviewed_evidence,
            provider_metadata=metadata,
            elapsed_seconds=time.perf_counter() - started,
        )
    except Exception as exc:
        return WorkResult(
            work_key=item.work_key,
            model_family=item.model_family,
            adapter_version=item.adapter_version,
            filing=item.filing,
            parser_release=item.parser_release,
            status="FAILED",
            provider_metadata=metadata,
            elapsed_seconds=time.perf_counter() - started,
            error=f"{type(exc).__name__}: {exc}",
        )


def _persist_batch(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    results: list[WorkResult],
) -> None:
    for result in results:
        persist_result(conn, run_id=run_id, result=result)
    conn.commit()


def execute_plan(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    work_items: Iterable[WorkItem],
    worker_count: int,
    provider_state_dir: Path,
    write_batch_size: int = 8,
) -> tuple[int, int]:
    items = list(work_items)
    with conn:
        for item in items:
            register_work(conn, run_id=run_id, item=item)
            mark_work_started(conn, item=item)
    completed = 0
    failed = 0
    buffer: list[WorkResult] = []
    if worker_count == 1:
        result_stream = (
            parse_work_item(
                item,
                provider_state_dir=str(provider_state_dir),
            )
            for item in items
        )
        for result in result_stream:
            buffer.append(result)
            completed += result.status == "COMPLETED"
            failed += result.status != "COMPLETED"
            if len(buffer) >= write_batch_size:
                _persist_batch(conn, run_id=run_id, results=buffer)
                buffer.clear()
    else:
        with ProcessPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(
                    parse_work_item,
                    item,
                    provider_state_dir=str(provider_state_dir),
                ): item
                for item in items
            }
            for future in as_completed(futures):
                item = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = WorkResult(
                        work_key=item.work_key,
                        model_family=item.model_family,
                        adapter_version=item.adapter_version,
                        filing=item.filing,
                        parser_release=item.parser_release,
                        status="FAILED",
                        error=f"WorkerFailure: {type(exc).__name__}: {exc}",
                    )
                buffer.append(result)
                completed += result.status == "COMPLETED"
                failed += result.status != "COMPLETED"
                if len(buffer) >= write_batch_size:
                    _persist_batch(conn, run_id=run_id, results=buffer)
                    buffer.clear()
    if buffer:
        _persist_batch(conn, run_id=run_id, results=buffer)
    return completed, failed
