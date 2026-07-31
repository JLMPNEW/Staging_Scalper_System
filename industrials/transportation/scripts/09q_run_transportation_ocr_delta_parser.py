#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.adapters import load_registry  # noqa: E402
from dedicated_parser.cli import main as parser_main  # noqa: E402
from dedicated_parser.contracts import file_sha256  # noqa: E402
from industrials.core.config import (  # noqa: E402
    family_config,
    load_yaml,
    resolve_path,
)
from industrials.core.reports import write_text_atomic  # noqa: E402
from industrials.transportation.efficient_parser_batch import (  # noqa: E402
    read_csv,
)
from industrials.transportation.content_text_cache import (  # noqa: E402
    cache_path,
)
from industrials.transportation.ocr_recovery import (  # noqa: E402
    OCR_TIMEOUT_SECONDS,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
    resolve_foundation,
)


ADAPTER = (
    "industrials.transportation.dedicated_parser_adapter:"
    "extract_metric_evidence"
)
PASS_OCR = {"PASS", "PASS_WITH_EXPLICIT_LIMITATIONS"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run one all-metric semantic parse over only the PDF contexts "
            "whose text was recovered by the sealed bounded OCR pass."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--workers", type=int, default=0)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true")
    mode.add_argument("--status", action="store_true")
    return parser.parse_args(argv)


def _json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected JSON object")
    return payload


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    write_text_atomic(
        path,
        json.dumps(dict(payload), indent=2, sort_keys=True) + "\n",
    )


def _cache_inventory(root: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    for path in sorted(root.rglob("*.json.gz")):
        stat = path.stat()
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(str(stat.st_size).encode("ascii"))
        digest.update(str(stat.st_mtime_ns).encode("ascii"))
        count += 1
    return count, digest.hexdigest()


def _matching_run(
    db_path: Path,
    *,
    source_hash: str,
) -> dict[str, Any] | None:
    from dedicated_parser.storage import connect_database

    with contextlib.closing(
        connect_database(db_path, readonly=True)
    ) as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM sec_parser_run
            WHERE model_family=?
            ORDER BY run_id DESC
            LIMIT 25
            """,
            (MODEL_FAMILY,),
        ).fetchall()
        for row in rows:
            candidate = dict(row)
            try:
                metadata = json.loads(
                    str(candidate.get("metadata_json") or "{}")
                )
            except json.JSONDecodeError:
                continue
            source = (
                ((metadata.get("plan") or {}).get("execution_scope") or {})
                .get("source_manifest")
                or {}
            )
            if str(source.get("sha256") or "") == source_hash:
                return candidate
    return None


def _parser_args(
    *,
    db_path: Path,
    cache_root: Path,
    provider_state_dir: Path,
    parser_cfg: Mapping[str, Any],
    source_path: Path,
    output_dir: Path,
    workers: int,
) -> list[str]:
    run_dir = output_dir / "ocr_delta_run"
    run_dir.mkdir(parents=True, exist_ok=True)
    return [
        "--db",
        str(db_path),
        "--cache-dir",
        str(cache_root),
        "--adapter",
        ADAPTER,
        "--asof",
        str(parser_cfg["source_census_asof_date"]),
        "--source-manifest",
        str(source_path),
        "--workers",
        str(workers),
        "--max-filings-per-ticker",
        "0",
        "--max-documents-per-filing",
        "0",
        "--provider-state-dir",
        str(provider_state_dir),
        "--max-pdf-pages",
        str(int(parser_cfg.get("max_pdf_pages", 250))),
        "--max-pdf-bytes",
        str(int(parser_cfg.get("max_pdf_bytes", 25_000_000))),
        "--pdf-extraction-timeout-seconds",
        str(OCR_TIMEOUT_SECONDS),
        "--all-metrics",
        "--require-complete-cache",
        "--disable-arelle",
        "--disable-edgartools",
        "--enable-pdf-ocr",
        "--skip-adjudication-skeleton",
        "--output-json",
        str(
            run_dir
            / "transportation_ocr_delta_parser_run.json"
        ),
        "--cache-gate-output-json",
        str(
            run_dir
            / "transportation_ocr_delta_parser_cache_gate.json"
        ),
    ]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    parser_cfg = family_config(config, MODEL_FAMILY)[
        "dedicated_parser"
    ]
    if bool(parser_cfg.get("parser_execution_authorized")):
        raise ValueError(
            "OCR delta execution requires the general parser switch off"
        )
    base_dir = config_path.parent
    foundation = resolve_foundation(config_path, args.db)
    asof_date = str(parser_cfg["source_census_asof_date"])
    output_dir = (
        resolve_path(parser_cfg["output_root"], base_dir=base_dir)
        / asof_date
    )
    recovery_manifest_path = (
        output_dir
        / "transportation_ocr_recovery_union_manifest.json"
    )
    source_path = (
        output_dir
        / "transportation_ocr_recovery_union_source_manifest.csv"
    )
    gate_path = (
        output_dir
        / "transportation_ocr_delta_parser_execution_gate.json"
    )
    result_path = (
        output_dir
        / "ocr_delta_run"
        / "transportation_ocr_delta_parser_run.json"
    )
    recovery_manifest = _json(recovery_manifest_path)
    source_hash = file_sha256(source_path)
    source_rows = read_csv(source_path)
    expected_contexts = int(
        recovery_manifest.get("union_context_count") or 0
    )
    expected_hashes = int(
        recovery_manifest.get("union_unique_content_hash_count") or 0
    )
    registry = load_registry(ADAPTER)
    errors: list[str] = []
    artifact = (
        (recovery_manifest.get("artifacts") or {}).get(
            "union_source_manifest"
        )
        or {}
    )
    if (
        recovery_manifest.get("acceptance") not in PASS_OCR
        or not bool(
            recovery_manifest.get("original_cache_unchanged")
        )
        or int(recovery_manifest.get("parser_invocations") or 0) != 0
    ):
        errors.append("bounded OCR recovery union is not passing")
    if (
        str(artifact.get("path") or "") != str(source_path.resolve())
        or str(artifact.get("sha256") or "") != source_hash
        or int(artifact.get("row_count") or 0) != len(source_rows)
    ):
        errors.append("OCR recovered-source manifest is not hash-sealed")
    unique_hashes = {
        str(row.get("content_sha256") or "").lower()
        for row in source_rows
    }
    if (
        not source_rows
        or len(source_rows) != expected_contexts
        or len(unique_hashes) != expected_hashes
    ):
        errors.append("OCR source cardinality does not match recovery gate")
    cache_root = (
        PROJECT_ROOT
        / "output"
        / "industrials_cache"
        / "transportation"
        / "ocr_delta"
        / "non_sec_primary_documents"
    ).resolve()
    cache_count, cache_sha = _cache_inventory(
        cache_root / "extracted_text_sha256"
    )
    missing_cache_hashes = sorted(
        content_hash
        for content_hash in unique_hashes
        if not cache_path(cache_root, content_hash).is_file()
    )
    if cache_count < expected_hashes or missing_cache_hashes:
        errors.append(
            "OCR recovery union does not have a complete text cache"
        )
    preflight = {
        "acceptance": (
            "PASS_PREFLIGHT" if not errors else "FAIL_PREFLIGHT"
        ),
        "gate": "DP7C_OCR_DELTA_SEMANTIC_PREFLIGHT",
        "model_family": MODEL_FAMILY,
        "asof_date": asof_date,
        "source_manifest_path": str(source_path.resolve()),
        "source_manifest_sha256": source_hash,
        "adapter_version": registry.adapter_version,
        "parser_metric_count": len(registry.parser_metrics),
        "logical_context_count": expected_contexts,
        "unique_content_hash_count": expected_hashes,
        "cache_file_count": cache_count,
        "cache_inventory_sha256": cache_sha,
        "missing_cache_hash_count": len(missing_cache_hashes),
        "pdf_extraction_timeout_seconds": OCR_TIMEOUT_SECONDS,
        "general_parser_execution_authorized": False,
        "ocr_delta_execution_authorized": not errors,
        "network_requests": 0,
        "parser_invocations": 0,
        "feature_build_invocations": 0,
        "historical_materialization_invocations": 0,
        "calibration_invocations": 0,
        "portfolio_invocations": 0,
        "production_promotion_authorized": False,
        "errors": errors,
    }
    if args.status:
        prior = _json(gate_path) if gate_path.is_file() else {}
        matched = _matching_run(
            foundation.db_path,
            source_hash=source_hash,
        )
        print(
            json.dumps(
                {
                    **preflight,
                    "acceptance": (
                        prior.get("acceptance")
                        or preflight["acceptance"]
                    ),
                    "run_id": (
                        prior.get("run_id")
                        or (matched or {}).get("run_id")
                    ),
                    "run_status": (
                        (matched or {}).get("status")
                        or "NOT_STARTED"
                    ),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if errors:
        print(json.dumps(preflight, indent=2, sort_keys=True))
        return 2
    if gate_path.is_file():
        prior = _json(gate_path)
        if (
            prior.get("acceptance") == "PASS"
            and prior.get("source_manifest_sha256") == source_hash
            and prior.get("adapter_version")
            == registry.adapter_version
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
    workers = args.workers or int(parser_cfg.get("workers") or 4)
    if workers < 1:
        raise ValueError("workers must be at least 1")
    running = {
        **preflight,
        "acceptance": "RUNNING",
        "gate": "DP7C_OCR_DELTA_SEMANTIC_EXECUTION",
        "process_id": os.getpid(),
        "workers": workers,
        "parser_invocations": 1,
        "cache_file_count_before": cache_count,
        "cache_inventory_sha256_before": cache_sha,
    }
    _write_json(gate_path, running)
    parser_stdout = io.StringIO()
    try:
        with contextlib.redirect_stdout(parser_stdout):
            code = parser_main(
                _parser_args(
                    db_path=foundation.db_path,
                    cache_root=cache_root,
                    provider_state_dir=resolve_path(
                        parser_cfg["provider_state_dir"],
                        base_dir=base_dir,
                    ),
                    parser_cfg=parser_cfg,
                    source_path=source_path,
                    output_dir=output_dir,
                    workers=workers,
                )
            )
        result = _json(result_path)
    except BaseException as exc:
        _write_json(
            gate_path,
            {
                **running,
                "acceptance": "FAIL",
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
        raise
    summary = result.get("summary") or {}
    execution = summary.get("execution_scope") or {}
    planned_source = execution.get("source_manifest") or {}
    scheduled = int(summary.get("scheduled_accessions") or 0)
    linked = int(summary.get("linked_completed_work_count") or 0)
    completed = int(result.get("completed_work_count") or 0)
    failed = int(result.get("failed_work_count") or 0)
    cache_after_count, cache_after_sha = _cache_inventory(
        cache_root / "extracted_text_sha256"
    )
    execution_errors: list[str] = []
    if code != 0 or result.get("mode") != "shadow":
        execution_errors.append(
            f"shared parser failed or left shadow mode code={code}"
        )
    if failed != 0 or completed != scheduled:
        execution_errors.append(
            "scheduled OCR semantic work did not complete cleanly"
        )
    if completed + linked != expected_contexts:
        execution_errors.append(
            "executed plus resume-linked OCR contexts do not reconcile"
        )
    if int(summary.get("missing_cache_accessions") or 0) != 0:
        execution_errors.append(
            "OCR execution reported missing cached documents"
        )
    if str(planned_source.get("sha256") or "") != source_hash:
        execution_errors.append("OCR execution source hash mismatch")
    if not bool(planned_source.get("direct_document_mode")):
        execution_errors.append(
            "OCR execution did not use direct-document mode"
        )
    if int(
        planned_source.get("metric_scoped_filing_count") or 0
    ) != expected_contexts:
        execution_errors.append("OCR execution scope count mismatch")
    if (
        not bool(execution.get("all_metrics"))
        or bool(execution.get("force"))
        or not bool(execution.get("resume"))
        or bool(execution.get("enable_arelle"))
        or bool(execution.get("enable_edgartools"))
    ):
        execution_errors.append(
            "OCR execution flags violate the bounded contract"
        )
    if bool(result.get("adjudication_skeleton_written")):
        execution_errors.append(
            "OCR execution unexpectedly built adjudication output"
        )
    if (
        cache_after_count != cache_count
        or cache_after_sha != cache_sha
    ):
        execution_errors.append(
            "semantic execution changed the isolated OCR text cache"
        )
    gate = {
        **running,
        "acceptance": (
            "PASS" if not execution_errors else "FAIL"
        ),
        "parser_return_code": code,
        "run_id": int(result.get("run_id") or 0),
        "newly_executed_work_count": completed,
        "resume_linked_work_count": linked,
        "effective_completed_work_count": completed + linked,
        "failed_work_count": failed,
        "cache_file_count_after": cache_after_count,
        "cache_inventory_sha256_after": cache_after_sha,
        "physical_document_reextraction_count": 0,
        "captured_parser_stdout_character_count": len(
            parser_stdout.getvalue()
        ),
        "result_path": str(result_path.resolve()),
        "result_sha256": file_sha256(result_path),
        "errors": execution_errors,
        "next_gate": (
            "BUILD_PARSE_FREE_OCR_UNION_COVERAGE"
            if not execution_errors
            else "RESUME_SAME_SEALED_OCR_DELTA"
        ),
    }
    _write_json(gate_path, gate)
    print(json.dumps(gate, indent=2, sort_keys=True))
    return 0 if gate["acceptance"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
