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
from dedicated_parser.storage import connect_database  # noqa: E402
from industrials.core.config import (  # noqa: E402
    family_config,
    load_yaml,
    resolve_path,
)
from industrials.core.reports import write_text_atomic  # noqa: E402
from industrials.transportation.content_text_cache import (  # noqa: E402
    ExtractionOptions,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
    resolve_foundation,
)


ADAPTER = "industrials.transportation.dedicated_parser_adapter:extract_metric_evidence"
PASS_CACHE = {"PASS", "PASS_WITH_EXPLICIT_EXTRACTION_LIMITATIONS"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Execute exactly one resumable all-metric semantic parse over "
            "the sealed transportation direct-document delta."
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
    run_dir = output_dir / "non_sec_direct_delta_run"
    run_dir.mkdir(parents=True, exist_ok=True)
    args = [
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
        str(
            float(
                parser_cfg.get(
                    "pdf_extraction_timeout_seconds",
                    30.0,
                )
            )
        ),
        "--all-metrics",
        "--require-complete-cache",
        "--disable-arelle",
        "--disable-edgartools",
        "--skip-adjudication-skeleton",
        "--output-json",
        str(run_dir / "transportation_non_sec_direct_delta_run.json"),
        "--cache-gate-output-json",
        str(run_dir / "transportation_non_sec_direct_delta_cache_gate.json"),
    ]
    if bool(parser_cfg.get("pdf_ocr_enabled")):
        args.append("--enable-pdf-ocr")
    return args


def _matching_run(db_path: Path, source_hash: str) -> dict[str, Any] | None:
    with contextlib.closing(connect_database(db_path, readonly=True)) as connection:
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
                metadata = json.loads(str(candidate.get("metadata_json") or "{}"))
            except json.JSONDecodeError:
                continue
            source = ((metadata.get("plan") or {}).get("execution_scope") or {}).get("source_manifest") or {}
            if str(source.get("sha256") or "") == source_hash:
                counts = connection.execute(
                    """
                    SELECT ledger.status, COUNT(*) AS row_count
                    FROM sec_parser_run_work AS relation
                    JOIN sec_parser_work_ledger AS ledger
                      ON ledger.work_key=relation.work_key
                    WHERE relation.run_id=?
                    GROUP BY ledger.status
                    """,
                    (int(candidate["run_id"]),),
                ).fetchall()
                candidate["ledger_status_counts"] = {str(item["status"]): int(item["row_count"]) for item in counts}
                return candidate
    return None


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


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    parser_cfg = family_config(config, MODEL_FAMILY)["dedicated_parser"]
    if bool(parser_cfg.get("parser_execution_authorized")):
        raise ValueError(
            "The general parser switch must remain false; this runner authorizes only the sealed one-shot direct delta."
        )
    base_dir = config_path.parent
    foundation = resolve_foundation(config_path, args.db)
    asof_date = str(parser_cfg["source_census_asof_date"])
    output_dir = resolve_path(parser_cfg["output_root"], base_dir=base_dir) / asof_date
    source_path = output_dir / "transportation_non_sec_direct_delta_source_manifest.csv"
    seal_path = output_dir / "transportation_efficient_parser_batch_manifest.json"
    plan_gate_path = output_dir / "transportation_non_sec_direct_delta_plan_gate.json"
    cache_manifest_path = output_dir / "transportation_content_text_cache_manifest.json"
    gate_path = output_dir / "transportation_non_sec_direct_delta_execution_gate.json"
    result_path = output_dir / "non_sec_direct_delta_run" / "transportation_non_sec_direct_delta_run.json"
    seal = _json(seal_path)
    plan_gate = _json(plan_gate_path)
    cache_manifest = _json(cache_manifest_path)
    source_hash = file_sha256(source_path)
    registry = load_registry(ADAPTER)
    delta = seal.get("direct_delta_summary") or {}
    expected_contexts = int(delta.get("logical_ticker_content_context_count") or 0)
    expected_hashes = int(delta.get("new_unique_content_hash_count") or 0)
    options = ExtractionOptions(
        enable_pdf_ocr=bool(parser_cfg.get("pdf_ocr_enabled")),
        max_pdf_pages=int(parser_cfg.get("max_pdf_pages", 250)),
        max_pdf_bytes=int(parser_cfg.get("max_pdf_bytes", 25_000_000)),
        pdf_extraction_timeout_seconds=float(parser_cfg.get("pdf_extraction_timeout_seconds", 30.0)),
    )
    errors: list[str] = []
    source_contract = seal.get("source_manifest") or {}
    if seal.get("acceptance") != "PASS":
        errors.append("DP6S source seal is not PASS")
    if str(source_contract.get("sha256") or "") != source_hash:
        errors.append("DP6S source hash mismatch")
    if plan_gate.get("acceptance") != "PASS":
        errors.append("DP6T plan gate is not PASS")
    if str(plan_gate.get("source_manifest_sha256") or "") != source_hash:
        errors.append("DP6T source hash mismatch")
    if str(plan_gate.get("adapter_version") or "") != registry.adapter_version:
        errors.append("DP6T adapter version is stale")
    if int(plan_gate.get("planned_context_count") or 0) != expected_contexts:
        errors.append("DP6T planned context count mismatch")
    if cache_manifest.get("acceptance") not in PASS_CACHE:
        errors.append("DP6U content cache is not complete")
    if str(cache_manifest.get("source_manifest_sha256") or "") != source_hash:
        errors.append("DP6U source hash mismatch")
    if str(cache_manifest.get("extraction_options_sha256") or "") != options.content_sha256:
        errors.append("DP6U extraction options are stale")
    if int(cache_manifest.get("completed_unique_hash_count") or 0) != expected_hashes:
        errors.append("DP6U unique hash count mismatch")
    if int(cache_manifest.get("failure_count") or 0) != 0:
        errors.append("DP6U contains failed extractions")
    if int(cache_manifest.get("legacy_word_conversion_ready_count") or 0) != int(
        cache_manifest.get("legacy_word_conversion_count") or 0
    ):
        errors.append("DP6U legacy Word conversion is incomplete")
    results_contract = cache_manifest.get("results") or {}
    results_path = Path(str(results_contract.get("path") or ""))
    if not results_path.is_file() or file_sha256(results_path) != str(results_contract.get("sha256") or ""):
        errors.append("DP6U result manifest is missing or changed")
    preflight = {
        "acceptance": "PASS_PREFLIGHT" if not errors else "FAIL_PREFLIGHT",
        "gate": "DP6V_ONE_SHOT_DIRECT_DELTA_PREFLIGHT",
        "model_family": MODEL_FAMILY,
        "asof_date": asof_date,
        "source_manifest_path": str(source_path.resolve()),
        "source_manifest_sha256": source_hash,
        "adapter_version": registry.adapter_version,
        "parser_metric_count": len(registry.parser_metrics),
        "logical_context_count": expected_contexts,
        "unique_content_hash_count": expected_hashes,
        "content_cache_acceptance": cache_manifest.get("acceptance"),
        "general_parser_execution_authorized": False,
        "one_shot_direct_delta_execution_authorized": not errors,
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
        prior_gate = _json(gate_path) if gate_path.is_file() else {}
        matched = _matching_run(foundation.db_path, source_hash)
        status = {
            **preflight,
            "acceptance": prior_gate.get("acceptance") or preflight["acceptance"],
            "execution_gate_status": prior_gate.get("acceptance") or "NOT_STARTED",
            "process_id": prior_gate.get("process_id"),
            "run_id": prior_gate.get("run_id") or (matched or {}).get("run_id"),
            "run_status": (matched or {}).get("status") or "NOT_STARTED",
            "planned_work_count": (matched or {}).get("planned_work_count"),
            "completed_work_count": (matched or {}).get("completed_work_count"),
            "failed_work_count": (matched or {}).get("failed_work_count"),
            "ledger_status_counts": (matched or {}).get("ledger_status_counts") or {},
        }
        print(json.dumps(status, indent=2, sort_keys=True))
        return 0
    if errors:
        print(json.dumps(preflight, indent=2, sort_keys=True))
        return 2
    if gate_path.is_file():
        prior_gate = _json(gate_path)
        if (
            prior_gate.get("acceptance") == "PASS"
            and prior_gate.get("source_manifest_sha256") == source_hash
            and prior_gate.get("adapter_version") == registry.adapter_version
        ):
            print(
                json.dumps(
                    {**prior_gate, "idempotent_reuse": True},
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
    cache_root = (
        PROJECT_ROOT / "output" / "industrials_cache" / "transportation" / "non_sec_primary_documents"
    ).resolve()
    cache_file_count_before, cache_inventory_sha256_before = _cache_inventory(cache_root / "extracted_text_sha256")
    if cache_file_count_before != expected_hashes:
        raise ValueError(
            "Physical text cache count differs from the DP6U seal: "
            f"expected={expected_hashes} actual={cache_file_count_before}"
        )
    running = {
        **preflight,
        "acceptance": "RUNNING",
        "gate": "DP6V_ONE_SHOT_DIRECT_DELTA_EXECUTION",
        "process_id": os.getpid(),
        "workers": workers,
        "parser_invocations": 1,
        "cache_file_count_before": cache_file_count_before,
        "cache_inventory_sha256_before": cache_inventory_sha256_before,
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
    cache_file_count_after, cache_inventory_sha256_after = _cache_inventory(cache_root / "extracted_text_sha256")
    execution_errors: list[str] = []
    if code != 0 or result.get("mode") != "shadow":
        execution_errors.append(f"shared parser failed code={code}")
    if failed != 0 or completed != scheduled:
        execution_errors.append("scheduled work did not complete cleanly")
    if completed + linked != expected_contexts:
        execution_errors.append("executed plus resume-linked contexts do not reconcile")
    if int(summary.get("missing_cache_accessions") or 0) != 0:
        execution_errors.append("execution reported missing source documents")
    if str(planned_source.get("sha256") or "") != source_hash:
        execution_errors.append("execution source hash mismatch")
    if not bool(planned_source.get("direct_document_mode")):
        execution_errors.append("execution did not use direct-document mode")
    if int(planned_source.get("metric_scoped_filing_count") or 0) != expected_contexts:
        execution_errors.append("execution metric scope count mismatch")
    if (
        not bool(execution.get("all_metrics"))
        or bool(execution.get("force"))
        or not bool(execution.get("resume"))
        or bool(execution.get("enable_arelle"))
        or bool(execution.get("enable_edgartools"))
    ):
        execution_errors.append("execution flags violate the one-shot contract")
    if bool(result.get("adjudication_skeleton_written")):
        execution_errors.append("execution unexpectedly built adjudication output")
    if (
        cache_file_count_after != cache_file_count_before
        or cache_inventory_sha256_after != cache_inventory_sha256_before
    ):
        execution_errors.append("semantic execution changed the physical text cache")
    gate = {
        **running,
        "acceptance": "PASS" if not execution_errors else "FAIL",
        "parser_return_code": code,
        "run_id": int(result.get("run_id") or 0),
        "newly_executed_work_count": completed,
        "resume_linked_work_count": linked,
        "effective_completed_work_count": completed + linked,
        "failed_work_count": failed,
        "cache_file_count_after": cache_file_count_after,
        "cache_inventory_sha256_after": cache_inventory_sha256_after,
        "physical_document_reextraction_count": 0,
        "captured_parser_stdout_character_count": len(parser_stdout.getvalue()),
        "result_path": str(result_path.resolve()),
        "result_sha256": file_sha256(result_path),
        "errors": execution_errors,
        "next_gate": (
            "BUILD_PARSE_FREE_UNION_COVERAGE_AND_ADJUDICATION"
            if not execution_errors
            else "RESUME_SAME_SEALED_DIRECT_DELTA"
        ),
    }
    _write_json(gate_path, gate)
    print(json.dumps(gate, indent=2, sort_keys=True))
    return 0 if gate["acceptance"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
