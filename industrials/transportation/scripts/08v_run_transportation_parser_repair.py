#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
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
    cfg_get,
    family_config,
    load_yaml,
    resolve_path,
)
from industrials.core.reports import write_text_atomic  # noqa: E402
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
    resolve_foundation,
)


ADAPTER = (
    "industrials.transportation.dedicated_parser_adapter:"
    "extract_metric_evidence"
)
EXECUTION_VERSION = "transportation_dp6h_pdf_repair_execution_v1"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Plan or execute the 14-document hash-sealed transportation PDF "
            "repair. OCR and every downstream build remain disabled."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--workers", type=int, default=2)
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


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    write_text_atomic(
        path,
        json.dumps(dict(payload), indent=2, sort_keys=True) + "\n",
    )


def _parser_args(
    *,
    db_path: Path,
    cache_dir: Path,
    provider_state_dir: Path,
    asof_date: str,
    source_path: Path,
    output_path: Path,
    cache_gate_path: Path,
    workers: int,
    plan_only: bool,
) -> list[str]:
    result = [
        "--db",
        str(db_path),
        "--cache-dir",
        str(cache_dir),
        "--adapter",
        ADAPTER,
        "--asof",
        asof_date,
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
        "250",
        "--max-pdf-bytes",
        "125000000",
        "--pdf-extraction-timeout-seconds",
        "180",
        "--all-metrics",
        "--require-complete-cache",
        "--skip-adjudication-skeleton",
        "--output-json",
        str(output_path),
        "--cache-gate-output-json",
        str(cache_gate_path),
    ]
    if plan_only:
        result.append("--plan-only")
    return result


def _plan_errors(
    *,
    plan: Mapping[str, Any],
    manifest: Mapping[str, Any],
    source_sha256: str,
) -> list[str]:
    errors: list[str] = []
    summary = plan.get("summary")
    if plan.get("mode") != "plan_only" or not isinstance(
        summary,
        Mapping,
    ):
        return ["repair plan is not a plan-only payload"]
    expected_accessions = int(
        manifest.get("repair_accession_count") or 0
    )
    expected_documents = int(
        manifest.get("repair_document_count") or 0
    )
    if int(summary.get("scheduled_accessions") or 0) != (
        expected_accessions
    ):
        errors.append("repair plan accession count mismatch")
    if int(summary.get("scheduled_documents") or 0) != (
        expected_documents
    ):
        errors.append("repair plan document count mismatch")
    if int(summary.get("missing_cache_accessions") or 0) != 0:
        errors.append("repair plan has missing cache accessions")
    execution = summary.get("execution_scope")
    if not isinstance(execution, Mapping):
        return [*errors, "repair plan has no execution scope"]
    source = execution.get("source_manifest")
    if not isinstance(source, Mapping) or str(
        source.get("sha256") or ""
    ) != source_sha256:
        errors.append("repair plan source hash mismatch")
    if not bool(execution.get("all_metrics")):
        errors.append("repair plan does not enable all metrics")
    if bool(execution.get("enable_pdf_ocr")):
        errors.append("repair plan unexpectedly enables OCR")
    return errors


def _result_errors(
    *,
    result: Mapping[str, Any],
    manifest: Mapping[str, Any],
    source_sha256: str,
    return_code: int,
) -> list[str]:
    errors: list[str] = []
    summary = result.get("summary")
    if result.get("mode") != "shadow" or not isinstance(
        summary,
        Mapping,
    ):
        return ["repair result is not a shadow payload"]
    execution = summary.get("execution_scope")
    if not isinstance(execution, Mapping):
        return ["repair result has no execution scope"]
    source = execution.get("source_manifest")
    expected = int(manifest.get("repair_accession_count") or 0)
    completed = int(result.get("completed_work_count") or 0)
    linked = int(summary.get("linked_completed_work_count") or 0)
    if return_code != 0:
        errors.append(f"shared parser returned code={return_code}")
    if int(result.get("run_id") or 0) <= 0:
        errors.append("repair result has no valid run id")
    if int(result.get("failed_work_count") or 0) != 0:
        errors.append("repair result contains failed work")
    if completed + linked != expected:
        errors.append("repair result does not cover the sealed accessions")
    if int(summary.get("missing_cache_accessions") or 0) != 0:
        errors.append("repair execution found missing cache accessions")
    if not isinstance(source, Mapping) or str(
        source.get("sha256") or ""
    ) != source_sha256:
        errors.append("repair result source hash mismatch")
    if bool(execution.get("enable_pdf_ocr")):
        errors.append("repair execution unexpectedly enabled OCR")
    if bool(result.get("adjudication_skeleton_written")):
        errors.append("repair execution wrote an adjudication skeleton")
    return errors


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.workers < 1 or args.workers > 2:
        raise ValueError("The sealed PDF repair allows one or two workers")
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    parser_cfg = family_config(config, MODEL_FAMILY)[
        "dedicated_parser"
    ]
    if bool(parser_cfg.get("parser_execution_authorized")):
        raise ValueError(
            "The general parser switch must remain false; this wrapper "
            "authorizes only the sealed PDF repair."
        )
    base_dir = config_path.parent
    foundation = resolve_foundation(config_path, args.db)
    output_dir = (
        resolve_path(parser_cfg["output_root"], base_dir=base_dir)
        / str(parser_cfg["source_census_asof_date"])
    )
    run_dir = output_dir / "parser_repair_run"
    run_dir.mkdir(parents=True, exist_ok=True)
    source_path = (
        output_dir
        / "transportation_parser_repair_source_manifest.csv"
    )
    manifest_path = (
        output_dir / "transportation_parser_repair_manifest.json"
    )
    gate_path = (
        output_dir
        / "transportation_parser_repair_execution_gate.json"
    )
    plan_path = (
        run_dir / "transportation_parser_repair_plan.json"
    )
    plan_cache_gate_path = (
        run_dir / "transportation_parser_repair_plan_cache_gate.json"
    )
    result_path = (
        run_dir / "transportation_parser_repair_run.json"
    )
    result_cache_gate_path = (
        run_dir / "transportation_parser_repair_cache_gate.json"
    )
    for path in (source_path, manifest_path):
        if not path.is_file():
            raise FileNotFoundError(path)
    manifest = _read_json(manifest_path)
    source_sha256 = file_sha256(source_path)
    if (
        manifest.get("acceptance") != "PASS"
        or str((manifest.get("artifact") or {}).get("sha256") or "")
        != source_sha256
        or int(manifest.get("repair_document_count") or 0) != 14
    ):
        raise ValueError("PDF repair manifest is not the sealed 14-doc set")
    if args.status:
        payload = (
            {
                **_read_json(gate_path),
                "status_query": "COMPLETED_GATE",
            }
            if gate_path.is_file()
            else {
                "gate": "DP6H_TARGETED_PDF_REPAIR_STATUS",
                "acceptance": "NOT_STARTED",
                "source_manifest_sha256": source_sha256,
            }
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    if gate_path.is_file():
        prior = _read_json(gate_path)
        if (
            prior.get("acceptance") == "PASS"
            and prior.get("source_manifest_sha256")
            == source_sha256
        ):
            print(
                json.dumps(
                    {**prior, "idempotent_reuse": True},
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
    registry = load_registry(ADAPTER)
    cache_dir = resolve_path(
        cfg_get(config, "sec_fundamentals.cache_dir"),
        base_dir=base_dir,
    )
    provider_state_dir = resolve_path(
        parser_cfg["provider_state_dir"],
        base_dir=base_dir,
    )
    plan_stdout = io.StringIO()
    with contextlib.redirect_stdout(plan_stdout):
        plan_code = parser_main(
            _parser_args(
                db_path=foundation.db_path,
                cache_dir=cache_dir,
                provider_state_dir=provider_state_dir,
                asof_date=str(
                    parser_cfg["source_census_asof_date"]
                ),
                source_path=source_path,
                output_path=plan_path,
                cache_gate_path=plan_cache_gate_path,
                workers=args.workers,
                plan_only=True,
            )
        )
    plan = _read_json(plan_path)
    errors = _plan_errors(
        plan=plan,
        manifest=manifest,
        source_sha256=source_sha256,
    )
    if plan_code != 0:
        errors.append(f"repair plan returned code={plan_code}")
    preflight = {
        "acceptance": (
            "PASS_PREFLIGHT" if not errors else "FAIL"
        ),
        "gate": "DP6H_TARGETED_PDF_REPAIR_PREFLIGHT",
        "execution_version": EXECUTION_VERSION,
        "model_family": MODEL_FAMILY,
        "base_delta_run_id": int(
            manifest.get("base_delta_run_id") or 0
        ),
        "source_manifest_path": str(source_path.resolve()),
        "source_manifest_sha256": source_sha256,
        "repair_accession_count": int(
            manifest.get("repair_accession_count") or 0
        ),
        "repair_document_count": int(
            manifest.get("repair_document_count") or 0
        ),
        "adapter_version": registry.adapter_version,
        "parser_metric_count": len(registry.parser_metrics),
        "max_pdf_bytes": 125_000_000,
        "max_pdf_pages": 250,
        "pdf_timeout_seconds": 180.0,
        "pdf_ocr_authorized": False,
        "general_parser_execution_authorized": False,
        "one_shot_execution_authorized": bool(args.execute),
        "parser_invocations": 0,
        "feature_build_invocations": 0,
        "historical_materialization_invocations": 0,
        "calibration_invocations": 0,
        "portfolio_invocations": 0,
        "production_promotion_authorized": False,
        "plan_path": str(plan_path.resolve()),
        "plan_sha256": file_sha256(plan_path),
        "captured_plan_stdout_character_count": len(
            plan_stdout.getvalue()
        ),
        "errors": errors,
    }
    if errors or not args.execute:
        print(json.dumps(preflight, indent=2, sort_keys=True))
        return 2 if errors else 0
    running = {
        **preflight,
        "acceptance": "RUNNING",
        "gate": "DP6H_TARGETED_PDF_REPAIR_EXECUTION",
        "process_id": os.getpid(),
        "workers": args.workers,
        "parser_invocations": 1,
    }
    _write_json(gate_path, running)
    parser_stdout = io.StringIO()
    try:
        with contextlib.redirect_stdout(parser_stdout):
            code = parser_main(
                _parser_args(
                    db_path=foundation.db_path,
                    cache_dir=cache_dir,
                    provider_state_dir=provider_state_dir,
                    asof_date=str(
                        parser_cfg["source_census_asof_date"]
                    ),
                    source_path=source_path,
                    output_path=result_path,
                    cache_gate_path=result_cache_gate_path,
                    workers=args.workers,
                    plan_only=False,
                )
            )
        result = _read_json(result_path)
        result_errors = _result_errors(
            result=result,
            manifest=manifest,
            source_sha256=source_sha256,
            return_code=code,
        )
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
    gate = {
        **running,
        "acceptance": "PASS" if not result_errors else "FAIL",
        "parser_return_code": code,
        "run_id": int(result.get("run_id") or 0),
        "newly_executed_work_count": int(
            result.get("completed_work_count") or 0
        ),
        "resume_linked_work_count": int(
            summary.get("linked_completed_work_count") or 0
        ),
        "failed_work_count": int(
            result.get("failed_work_count") or 0
        ),
        "captured_parser_stdout_character_count": len(
            parser_stdout.getvalue()
        ),
        "result_path": str(result_path.resolve()),
        "result_sha256": file_sha256(result_path),
        "errors": result_errors,
        "next_gate": (
            "BUILD_REPAIRED_SEC_UNION_COVERAGE"
            if not result_errors
            else "RESUME_SEALED_PDF_REPAIR"
        ),
    }
    _write_json(gate_path, gate)
    print(json.dumps(gate, indent=2, sort_keys=True))
    return 0 if gate["acceptance"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
