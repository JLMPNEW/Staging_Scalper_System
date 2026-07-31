#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
from pathlib import Path


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
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
    resolve_foundation,
)


ADAPTER = "industrials.transportation.dedicated_parser_adapter:extract_metric_evidence"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build the offline exact plan for the sealed transportation "
            "non-SEC direct-document delta. No parsing is authorized."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--workers", type=int, default=0)
    return parser.parse_args(argv)


def _json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected JSON object")
    return payload


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    parser_cfg = family_config(config, MODEL_FAMILY)["dedicated_parser"]
    if bool(parser_cfg.get("parser_execution_authorized")):
        raise ValueError("Offline planning requires the general parser switch disabled")
    base_dir = config_path.parent
    asof_date = str(parser_cfg["source_census_asof_date"])
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else resolve_path(parser_cfg["output_root"], base_dir=base_dir) / asof_date
    )
    source_path = output_dir / "transportation_non_sec_direct_delta_source_manifest.csv"
    seal_path = output_dir / "transportation_efficient_parser_batch_manifest.json"
    plan_path = output_dir / "transportation_non_sec_direct_delta_plan.json"
    gate_path = output_dir / "transportation_non_sec_direct_delta_plan_gate.json"
    for path in (source_path, seal_path):
        if not path.is_file():
            raise FileNotFoundError(path)
    seal = _json(seal_path)
    source_hash = file_sha256(source_path)
    source_contract = seal.get("source_manifest")
    if (
        seal.get("acceptance") != "PASS"
        or not seal.get("one_shot_direct_delta_plan_authorized")
        or not isinstance(source_contract, dict)
        or str(source_contract.get("sha256") or "") != source_hash
    ):
        raise ValueError("DP6S direct delta seal is not valid")
    foundation = resolve_foundation(config_path, args.db)
    cache_root = (
        PROJECT_ROOT / "output" / "industrials_cache" / "transportation" / "non_sec_primary_documents"
    ).resolve()
    workers = args.workers or int(parser_cfg.get("workers") or 4)
    registry = load_registry(ADAPTER)
    parser_args = [
        "--db",
        str(foundation.db_path),
        "--cache-dir",
        str(cache_root),
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
        str(resolve_path(parser_cfg["provider_state_dir"], base_dir=base_dir)),
        "--max-pdf-pages",
        str(int(parser_cfg.get("max_pdf_pages", 250))),
        "--max-pdf-bytes",
        str(int(parser_cfg.get("max_pdf_bytes", 25_000_000))),
        "--pdf-extraction-timeout-seconds",
        str(float(parser_cfg.get("pdf_extraction_timeout_seconds") or 30.0)),
        "--all-metrics",
        "--require-complete-cache",
        "--disable-arelle",
        "--disable-edgartools",
        "--skip-adjudication-skeleton",
        "--plan-only",
        "--output-json",
        str(plan_path),
        "--cache-gate-output-json",
        str(output_dir / "transportation_non_sec_direct_delta_cache_gate.json"),
    ]
    if bool(parser_cfg.get("pdf_ocr_enabled")):
        parser_args.append("--enable-pdf-ocr")
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        code = parser_main(parser_args)
    plan = _json(plan_path)
    summary = plan.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    execution = summary.get("execution_scope")
    execution = execution if isinstance(execution, dict) else {}
    planned_source = execution.get("source_manifest")
    planned_source = planned_source if isinstance(planned_source, dict) else {}
    direct_delta_summary = seal.get("direct_delta_summary")
    direct_delta_summary = (
        direct_delta_summary
        if isinstance(direct_delta_summary, dict)
        else {}
    )
    expected_contexts = int(
        str(
            direct_delta_summary.get(
                "logical_ticker_content_context_count",
                0,
            )
        )
    )
    errors: list[str] = []
    if code != 0 or plan.get("mode") != "plan_only":
        errors.append(f"shared parser plan failed code={code}")
    if int(summary.get("scheduled_accessions") or 0) != expected_contexts:
        errors.append("planned context count does not match the seal")
    if int(summary.get("scheduled_documents") or 0) != expected_contexts:
        errors.append("planned document count does not match the seal")
    if int(summary.get("missing_cache_accessions") or 0) != 0:
        errors.append("direct plan has missing local documents")
    if str(planned_source.get("sha256") or "") != source_hash:
        errors.append("planned source-manifest hash mismatch")
    if not bool(planned_source.get("direct_document_mode")):
        errors.append("planned source manifest is not direct-document mode")
    if int(planned_source.get("metric_scoped_filing_count") or 0) != (expected_contexts):
        errors.append("planned metric-scoped filing count mismatch")
    if not bool(execution.get("all_metrics")):
        errors.append("plan does not evaluate all manifest-scoped metrics")
    if bool(execution.get("enable_arelle")) or bool(execution.get("enable_edgartools")):
        errors.append("non-SEC plan enabled an SEC-only provider")
    if int(execution.get("max_pdf_pages", -1)) != int(parser_cfg.get("max_pdf_pages", 250)):
        errors.append("planned PDF page scope differs from config")
    if int(execution.get("max_pdf_bytes", -1)) != int(parser_cfg.get("max_pdf_bytes", 25_000_000)):
        errors.append("planned PDF byte scope differs from config")
    if bool(execution.get("enable_pdf_ocr")) != bool(parser_cfg.get("pdf_ocr_enabled")):
        errors.append("planned PDF OCR scope differs from config")

    acceptance = "PASS" if not errors else "FAIL"
    gate = {
        "acceptance": acceptance,
        "gate": "DP6T_OFFLINE_DIRECT_DELTA_PLAN",
        "model_family": MODEL_FAMILY,
        "asof_date": asof_date,
        "adapter_version": registry.adapter_version,
        "adapter_parser_metric_count": len(registry.parser_metrics),
        "source_manifest_path": str(source_path.resolve()),
        "source_manifest_sha256": source_hash,
        "plan_path": str(plan_path.resolve()),
        "plan_sha256": file_sha256(plan_path),
        "planned_context_count": int(summary.get("scheduled_accessions") or 0),
        "planned_document_count": int(summary.get("scheduled_documents") or 0),
        "missing_cache_count": int(summary.get("missing_cache_accessions") or 0),
        "captured_parser_stdout_character_count": len(stdout.getvalue()),
        "database_mode": "plan_only",
        "network_requests": 0,
        "parser_invocations": 0,
        "feature_build_invocations": 0,
        "historical_materialization_invocations": 0,
        "calibration_invocations": 0,
        "portfolio_invocations": 0,
        "general_parser_execution_authorized": False,
        "one_shot_direct_delta_execution_authorized": (acceptance == "PASS"),
        "production_promotion_authorized": False,
        "errors": errors,
        "next_gate": (
            "EXECUTE_ONE_RESUMABLE_DIRECT_DELTA_BATCH" if acceptance == "PASS" else "REPAIR_OFFLINE_DIRECT_DELTA_PLAN"
        ),
    }
    write_text_atomic(
        gate_path,
        json.dumps(gate, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(gate, indent=2, sort_keys=True))
    return 0 if acceptance == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
