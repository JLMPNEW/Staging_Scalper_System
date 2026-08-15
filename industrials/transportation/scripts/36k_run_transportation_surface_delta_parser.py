#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.adapters import load_registry  # noqa: E402
from dedicated_parser.cli import main as parser_main  # noqa: E402
from industrials.core.config import cfg_get, family_config, load_yaml, resolve_path  # noqa: E402
from industrials.core.reports import write_text_atomic  # noqa: E402
from industrials.transportation.contracts import file_sha256  # noqa: E402
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
    resolve_foundation,
)


ADAPTER = "industrials.transportation.dedicated_parser_adapter:extract_metric_evidence"
DATA_ROOT = PROJECT_ROOT / "industrials" / "transportation" / "data"
SOURCE_MAP = DATA_ROOT / "transportation_surface_metric_source_map_v1.csv"
DEFAULT_OUTPUT_ROOT = (
    PROJECT_ROOT
    / "output"
    / "industrials"
    / "transportation"
    / "investable_v3"
    / "surface_delta"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Plan or execute the exact 19-name, metric-specific surface-freight "
            "parser batch against a complete-cache surface census."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--workers", type=int, default=0)
    parser.add_argument("--output-dir", type=Path, default=None)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--plan-only", action="store_true")
    modes.add_argument("--execute", action="store_true")
    return parser.parse_args()


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected JSON object")
    return payload


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    family = family_config(config, MODEL_FAMILY)
    parser_cfg = family["dedicated_parser"]
    foundation = resolve_foundation(config_path, args.db)
    base_dir = config_path.parent
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else DEFAULT_OUTPUT_ROOT / args.asof
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    census_module = importlib.import_module(
        "industrials.transportation.scripts.36j_build_transportation_surface_delta_census"
    )
    runner_helpers = importlib.import_module(
        "industrials.transportation.scripts.36d_run_transportation_tanker_delta_parser"
    )
    tickers, direct_metrics, _ = census_module._surface_contract(
        resolve_path(parser_cfg["discovery_registry_csv"], base_dir=base_dir)
    )

    census_path = output_dir / "transportation_surface_delta_source_census.csv"
    census_manifest_path = output_dir / "transportation_surface_delta_census_manifest.json"
    source_manifest = output_dir / "transportation_surface_delta_parser_source_manifest.csv"
    plan_path = output_dir / "transportation_surface_delta_parser_plan.json"
    plan_gate_path = output_dir / "transportation_surface_delta_parser_plan_gate.json"
    run_path = output_dir / "transportation_surface_delta_parser_run.json"
    cache_gate_path = output_dir / "transportation_surface_delta_parser_cache_gate.json"

    census = _read_json(census_manifest_path)
    if census.get("acceptance") != "PASS" or int(census.get("unresolved_gap_count") or 0):
        raise ValueError("surface parser requires a PASS census with zero cache gaps")
    scope = census.get("execution_scope") or {}
    if set(scope.get("tickers", ())) != set(tickers):
        raise ValueError("census ticker scope does not match the 19-name surface contract")
    if set(scope.get("direct_metric_ids", ())) != set(direct_metrics):
        raise ValueError("census metric scope does not match the direct surface contract")
    if scope.get("source_map_sha256") != file_sha256(SOURCE_MAP):
        raise ValueError("surface source map changed after the census was sealed")

    registry = load_registry(ADAPTER)
    registry_names = {request.metric_name for request in registry.parser_metrics}
    if not set(direct_metrics) <= registry_names:
        raise ValueError(
            f"adapter registry is missing direct surface metrics={sorted(set(direct_metrics) - registry_names)}"
        )
    parser_source_rows = runner_helpers._build_parser_source_manifest(
        census_path=census_path,
        output_path=source_manifest,
        db_path=foundation.db_path,
        direct_metric_ids=direct_metrics,
    )
    if parser_source_rows != int(census.get("selected_document_row_count") or -1):
        raise ValueError("surface parser manifest row count does not match its census")

    workers = args.workers or int(parser_cfg.get("workers") or 4)
    common = runner_helpers._parser_args(
        db_path=foundation.db_path,
        cache_dir=resolve_path(
            cfg_get(config, "sec_fundamentals.cache_dir"), base_dir=base_dir
        ),
        source_manifest=source_manifest,
        output_json=plan_path if args.plan_only else run_path,
        cache_gate_json=cache_gate_path,
        provider_state_dir=(
            PROJECT_ROOT / "tmp" / "edgartools" / "transportation_surface_v3"
        ),
        asof=args.asof,
        workers=workers,
    )

    if args.plan_only:
        code = parser_main([*common, "--plan-only"])
        plan = _read_json(plan_path)
        summary = plan.get("summary") or {}
        execution = summary.get("execution_scope") or {}
        errors: list[str] = []
        if code != 0:
            errors.append(f"parser plan return code={code}")
        if plan.get("mode") != "plan_only":
            errors.append("parser did not produce plan_only mode")
        if int(summary.get("requested_tickers") or 0) != len(tickers):
            errors.append("plan requested ticker count is not 19")
        if int(summary.get("missing_cache_accessions") or 0) != 0:
            errors.append("plan contains missing cache accessions")
        if not execution.get("all_metrics"):
            errors.append("plan did not request all manifest-scoped metrics")
        source_scope = execution.get("source_manifest") or {}
        if source_scope.get("sha256") != file_sha256(source_manifest):
            errors.append("plan source-manifest hash mismatch")
        gate = {
            "acceptance": "PASS" if not errors else "FAIL",
            "gate": "TRANSPORTATION_SURFACE_V3_COMPLETE_CACHE_PLAN",
            "asof_date": args.asof,
            "source_manifest_sha256": file_sha256(source_manifest),
            "source_map_sha256": file_sha256(SOURCE_MAP),
            "adapter_version": registry.adapter_version,
            "ticker_count": len(tickers),
            "direct_metric_count": len(direct_metrics),
            "scheduled_accessions": int(summary.get("scheduled_accessions") or 0),
            "scheduled_documents": int(summary.get("scheduled_documents") or 0),
            "errors": errors,
            "historical_reconstruction_authorized": False,
            "calibration_authorized": False,
            "production_promotion_authorized": False,
        }
        write_text_atomic(plan_gate_path, json.dumps(gate, indent=2, sort_keys=True) + "\n")
        print(json.dumps(gate, indent=2, sort_keys=True))
        return 0 if not errors else 2

    gate = _read_json(plan_gate_path)
    if (
        gate.get("acceptance") != "PASS"
        or gate.get("source_manifest_sha256") != file_sha256(source_manifest)
        or gate.get("source_map_sha256") != file_sha256(SOURCE_MAP)
        or gate.get("adapter_version") != registry.adapter_version
    ):
        raise ValueError("execute requires a current PASS plan for the same sealed inputs")

    placeholders = ",".join("?" for _ in tickers)
    with sqlite3.connect(foundation.db_path) as connection:
        before = connection.execute(
            "SELECT COUNT(*) FROM fact_sec_metric_disclosure_candidate "
            f"WHERE model_family='transportation' AND ticker IN ({placeholders})",
            tickers,
        ).fetchone()[0]
    code = parser_main(common)
    with sqlite3.connect(foundation.db_path) as connection:
        after = connection.execute(
            "SELECT COUNT(*) FROM fact_sec_metric_disclosure_candidate "
            f"WHERE model_family='transportation' AND ticker IN ({placeholders})",
            tickers,
        ).fetchone()[0]
    run = _read_json(run_path)
    run["bounded_batch_contract"] = {
        "contract_version": "transportation_surface_delta_parser_v1",
        "source_map_sha256": file_sha256(SOURCE_MAP),
        "ticker_count": len(tickers),
        "direct_metric_count": len(direct_metrics),
        "candidate_rows_before": int(before),
        "candidate_rows_after": int(after),
        "candidate_row_delta": int(after - before),
        "parser_return_code": code,
        "historical_reconstruction_authorized": False,
        "calibration_authorized": False,
        "production_promotion_authorized": False,
    }
    write_text_atomic(run_path, json.dumps(run, indent=2, sort_keys=True) + "\n")
    print(json.dumps(run["bounded_batch_contract"], indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
