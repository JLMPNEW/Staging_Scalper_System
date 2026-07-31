#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.adapters import load_registry  # noqa: E402
from dedicated_parser.cli import main as parser_main  # noqa: E402
from dedicated_parser.contracts import file_sha256  # noqa: E402
from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.reports import write_text_atomic  # noqa: E402
from industrials.transportation.scripts._shared import DEFAULT_CONFIG  # noqa: E402


ADAPTER = (
    "industrials.transportation.required_metric_parser_adapter:"
    "extract_metric_evidence"
)
DEFAULT_OUTPUT_ROOT = (
    PROJECT_ROOT
    / "output"
    / "industrials"
    / "transportation"
    / "required_metric_repair"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Plan or execute exactly one shadow Arelle pass over the sealed "
            "transportation required-metric residual manifest."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--workers", type=int, default=2)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan-only", action="store_true")
    mode.add_argument("--execute", action="store_true")
    return parser.parse_args()


def _json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected JSON object")
    return payload


def main() -> int:
    args = parse_args()
    asof_date = str(args.asof)[:10]
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    db_path = (
        args.db.expanduser().resolve()
        if args.db is not None
        else resolve_path(
            cfg_get(config, "paths.database_path"),
            base_dir=config_path.parent,
        )
    )
    cache_root = resolve_path(
        cfg_get(config, "sec_fundamentals.cache_dir"),
        base_dir=config_path.parent,
    )
    provider_state_dir = (
        PROJECT_ROOT
        / "tmp"
        / "edgartools"
        / "transportation_required_metric_repair"
    ).resolve()
    output_dir = args.output_root.expanduser().resolve() / asof_date
    plan_path = (
        output_dir / "transportation_required_metric_parser_plan.json"
    )
    source_path = (
        output_dir
        / "transportation_required_metric_parser_source_manifest.csv"
    )
    if not plan_path.is_file() or not source_path.is_file():
        raise FileNotFoundError("Run 09y parser planner first")
    plan = _json(plan_path)
    registry = load_registry(ADAPTER)
    errors: list[str] = []
    if plan.get("acceptance") != "PASS":
        errors.append("09y residual parser plan is not PASS")
    if plan.get("adapter_version") != registry.adapter_version:
        errors.append("09y adapter version is stale")
    source_contract = plan.get("source_manifest") or {}
    if source_contract.get("sha256") != file_sha256(source_path):
        errors.append("09y source-manifest hash mismatch")
    for contract in (plan.get("sealed_inputs") or {}).values():
        path = Path(str(contract.get("path") or ""))
        if (
            not path.is_file()
            or file_sha256(path) != str(contract.get("sha256") or "")
        ):
            errors.append(f"sealed input missing or changed={path}")
    run_dir = output_dir / "required_metric_parser_run"
    run_dir.mkdir(parents=True, exist_ok=True)
    result_path = (
        run_dir / "transportation_required_metric_parser_shadow_run.json"
    )
    cache_gate_path = (
        run_dir / "transportation_required_metric_parser_cache_gate.json"
    )
    gate_path = (
        output_dir / "transportation_required_metric_parser_execution.json"
    )
    parser_args = [
        "--db",
        str(db_path),
        "--cache-dir",
        str(cache_root),
        "--adapter",
        ADAPTER,
        "--asof",
        asof_date,
        "--source-manifest",
        str(source_path),
        "--workers",
        str(max(1, args.workers)),
        "--max-filings-per-ticker",
        "0",
        "--max-documents-per-filing",
        "0",
        "--provider-state-dir",
        str(provider_state_dir),
        "--all-metrics",
        "--require-complete-cache",
        "--disable-edgartools",
        "--skip-adjudication-skeleton",
        "--output-json",
        str(result_path),
        "--cache-gate-output-json",
        str(cache_gate_path),
    ]
    if args.plan_only:
        parser_args.append("--plan-only")
    parser_stdout = io.StringIO()
    parser_code = 2
    if not errors:
        with contextlib.redirect_stdout(parser_stdout):
            parser_code = parser_main(parser_args)
    result = _json(result_path) if result_path.is_file() else {}
    summary = result.get("summary") or {}
    planned_accessions = int(summary.get("scheduled_accessions") or 0)
    planned_documents = int(summary.get("scheduled_documents") or 0)
    missing_cache = int(summary.get("missing_cache_accessions") or 0)
    if parser_code != 0:
        errors.append(f"dedicated parser returned code={parser_code}")
    if planned_accessions != 178 or planned_documents != 178:
        errors.append(
            "parser scope mismatch="
            f"{planned_accessions} accessions/{planned_documents} documents"
        )
    if missing_cache:
        errors.append(f"parser reports missing cache accessions={missing_cache}")
    if args.execute:
        failed_work_count = int(result.get("failed_work_count") or 0)
        completed_work_count = int(result.get("completed_work_count") or 0)
        if failed_work_count:
            errors.append(f"parser failed work count={failed_work_count}")
        if completed_work_count != 178:
            errors.append(
                f"parser completed work count={completed_work_count} expected=178"
            )
    acceptance = "PASS" if not errors else "FAIL"
    payload = {
        "acceptance": acceptance,
        "gate": (
            "TRANSPORTATION_REQUIRED_METRIC_PARSER_PREFLIGHT"
            if args.plan_only
            else "TRANSPORTATION_REQUIRED_METRIC_PARSER_EXECUTION"
        ),
        "mode": "plan_only" if args.plan_only else "shadow_execute",
        "asof_date": asof_date,
        "adapter": ADAPTER,
        "adapter_version": registry.adapter_version,
        "source_manifest_path": str(source_path.resolve()),
        "source_manifest_sha256": file_sha256(source_path),
        "planned_accession_count": planned_accessions,
        "planned_document_count": planned_documents,
        "missing_cache_accession_count": missing_cache,
        "completed_work_count": int(result.get("completed_work_count") or 0),
        "failed_work_count": int(result.get("failed_work_count") or 0),
        "run_id": result.get("run_id"),
        "recovery_assessment": result.get("recovery_assessment") or {},
        "parser_stdout_character_count": len(parser_stdout.getvalue()),
        "network_requests": 0,
        "parser_invocations": int(args.execute and not errors),
        "feature_build_invocations": 0,
        "calibration_invocations": 0,
        "portfolio_layer_invocations": 0,
        "automatic_extension_promotion_authorized": False,
        "production_promotion_authorized": False,
        "result": {
            "path": str(result_path.resolve()),
            "sha256": (
                file_sha256(result_path) if result_path.is_file() else ""
            ),
        },
        "cache_gate": {
            "path": str(cache_gate_path.resolve()),
            "sha256": (
                file_sha256(cache_gate_path)
                if cache_gate_path.is_file()
                else ""
            ),
        },
        "errors": errors,
        "next_gate": (
            (
                "EXECUTE_ONE_RESIDUAL_ARELLE_SHADOW_PARSE"
                if args.plan_only
                else "AUDIT_REQUIRED_METRIC_PARSER_EVIDENCE"
            )
            if not errors
            else "REPAIR_REQUIRED_METRIC_PARSER_EXECUTION"
        ),
    }
    write_text_atomic(
        gate_path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if acceptance == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
