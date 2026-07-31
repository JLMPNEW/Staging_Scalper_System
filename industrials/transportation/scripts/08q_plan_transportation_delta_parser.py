#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import importlib
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
validate_plan_payload = importlib.import_module(
    "industrials.transportation.scripts."
    "08f_run_transportation_dedicated_parser_shadow"
).validate_plan_payload


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build and validate the offline shared-parser plan for the sealed "
            "transportation DP6E delta. No parser execution is authorized."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--workers", type=int, default=0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    parser_cfg = family_config(config, MODEL_FAMILY)["dedicated_parser"]
    if bool(parser_cfg.get("parser_execution_authorized")):
        raise ValueError(
            "Delta plan-only gate requires "
            "parser_execution_authorized=false"
        )
    base_dir = config_path.parent
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else (
            resolve_path(parser_cfg["output_root"], base_dir=base_dir)
            / str(parser_cfg["source_census_asof_date"])
        )
    )
    source_csv = (
        output_dir / "transportation_delta_parser_source_manifest.csv"
    )
    source_json = (
        output_dir / "transportation_delta_parser_source_manifest.json"
    )
    plan_path = output_dir / "transportation_delta_parser_plan.json"
    gate_path = (
        output_dir / "transportation_delta_parser_plan_gate.json"
    )
    source = json.loads(source_json.read_text(encoding="utf-8"))
    if (
        source.get("acceptance") != "PASS"
        or file_sha256(source_csv)
        != str((source.get("artifact") or {}).get("sha256") or "")
    ):
        raise ValueError("Delta parser source manifest is not sealed")
    foundation = resolve_foundation(config_path, args.db)
    cache_dir = resolve_path(
        cfg_get(config, "sec_fundamentals.cache_dir"),
        base_dir=base_dir,
    )
    workers = args.workers or int(parser_cfg.get("workers") or 4)
    registry = load_registry(ADAPTER)
    parser_args = [
        "--db",
        str(foundation.db_path),
        "--cache-dir",
        str(cache_dir),
        "--adapter",
        ADAPTER,
        "--asof",
        str(parser_cfg["source_census_asof_date"]),
        "--source-manifest",
        str(source_csv),
        "--workers",
        str(workers),
        "--max-filings-per-ticker",
        "0",
        "--max-documents-per-filing",
        "0",
        "--provider-state-dir",
        str(resolve_path(parser_cfg["provider_state_dir"], base_dir=base_dir)),
        "--max-pdf-pages",
        # 0 means unlimited; only a missing key may fall back.
        str(
            int(parser_cfg["max_pdf_pages"])
            if parser_cfg.get("max_pdf_pages") is not None
            else 250
        ),
        "--max-pdf-bytes",
        str(
            int(parser_cfg["max_pdf_bytes"])
            if parser_cfg.get("max_pdf_bytes") is not None
            else 25_000_000
        ),
        "--pdf-extraction-timeout-seconds",
        str(
            float(
                parser_cfg["pdf_extraction_timeout_seconds"]
                if parser_cfg.get("pdf_extraction_timeout_seconds")
                is not None
                else 30.0
            )
        ),
        "--all-metrics",
        "--require-complete-cache",
        "--skip-adjudication-skeleton",
        "--plan-only",
        "--output-json",
        str(plan_path),
        "--cache-gate-output-json",
        str(output_dir / "transportation_delta_parser_cache_gate.json"),
    ]
    parser_stdout = io.StringIO()
    with contextlib.redirect_stdout(parser_stdout):
        code = parser_main(parser_args)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan_summary = plan.get("summary") or {}
    errors = validate_plan_payload(
        payload=plan,
        source_manifest=source,
        source_manifest_sha256=file_sha256(source_csv),
        parser_metric_count=len(registry.parser_metrics),
    )
    gate = {
        "acceptance": "PASS" if code == 0 and not errors else "FAIL",
        "gate": "DP6E_DELTA_PARSER_OFFLINE_PLAN",
        "model_family": MODEL_FAMILY,
        "parser_return_code": code,
        "mode": plan.get("mode"),
        "source_manifest_path": str(source_csv.resolve()),
        "source_manifest_sha256": file_sha256(source_csv),
        "adapter_version": registry.adapter_version,
        "parser_metric_count": len(registry.parser_metrics),
        "requested_tickers": int(
            plan_summary.get("requested_tickers") or 0
        ),
        "scheduled_accessions": int(
            plan_summary.get("scheduled_accessions") or 0
        ),
        "scheduled_documents": int(
            plan_summary.get("scheduled_documents") or 0
        ),
        "missing_cache_accessions": int(
            plan_summary.get("missing_cache_accessions") or 0
        ),
        "unresolved_metric_pairs": int(
            plan_summary.get("unresolved_metric_pairs") or 0
        ),
        "all_parser_metrics": bool(
            (
                plan_summary.get("execution_scope") or {}
            ).get("all_metrics")
        ),
        "captured_parser_stdout_character_count": len(
            parser_stdout.getvalue()
        ),
        "database_mode": "read_only",
        "network_requests": 0,
        "parser_invocations": 0,
        "feature_build_invocations": 0,
        "historical_materialization_invocations": 0,
        "calibration_invocations": 0,
        "portfolio_invocations": 0,
        "parser_execution_authorized": False,
        "production_promotion_authorized": False,
        "errors": errors,
    }
    write_text_atomic(
        gate_path,
        json.dumps(gate, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(gate, indent=2, sort_keys=True))
    return 0 if gate["acceptance"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
