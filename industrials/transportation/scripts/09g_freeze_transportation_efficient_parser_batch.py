#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.adapters import load_registry  # noqa: E402
from dedicated_parser.contracts import file_sha256  # noqa: E402
from dedicated_parser.source_manifest import (  # noqa: E402
    load_source_manifest,
)
from dedicated_parser.storage import connect_database  # noqa: E402
from industrials.core.config import (  # noqa: E402
    family_config,
    load_yaml,
    resolve_path,
)
from industrials.core.reports import (  # noqa: E402
    write_csv_atomic,
    write_text_atomic,
)
from industrials.transportation.efficient_parser_batch import (  # noqa: E402
    DEFAULT_COMPLETED_RUN_IDS,
    DELTA_SOURCE_FIELDS,
    EFFICIENT_BATCH_VERSION,
    RESIDUAL_DOCUMENT_FIELDS,
    RESIDUAL_PAIR_FIELDS,
    build_direct_delta_manifest,
    build_residual_dispositions,
    completed_content_hashes,
    pipe_values,
    read_csv,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
    resolve_foundation,
)


ADAPTER = "industrials.transportation.dedicated_parser_adapter:extract_metric_evidence"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Freeze every residual primary-document source gap and build "
            "one exact hash-deduplicated non-SEC parser delta. This gate "
            "does not retrieve, parse, build features, or calibrate."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
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
        raise ValueError("The general parser authorization must remain disabled")
    base_dir = config_path.parent
    asof_date = str(parser_cfg["source_census_asof_date"])
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else resolve_path(parser_cfg["output_root"], base_dir=base_dir) / asof_date
    )
    hydration_path = output_dir / "transportation_primary_document_hydration_manifest.json"
    document_path = output_dir / "transportation_primary_document_hydrated_manifest.csv"
    content_path = output_dir / "transportation_primary_document_content_catalog.csv"
    sec_coverage_path = output_dir / "transportation_repaired_sec_union_ticker_metric_coverage.csv"
    for path in (
        hydration_path,
        document_path,
        content_path,
        sec_coverage_path,
    ):
        if not path.is_file():
            raise FileNotFoundError(path)
    hydration = _json(hydration_path)
    errors: list[str] = []
    if hydration.get("acceptance") not in {
        "PASS",
        "PASS_WITH_REQUIRED_RECOVERY",
    }:
        errors.append("DP6R hydration acceptance is not parse-eligible")
    if int(str(hydration.get("completed_request_count") or 0)) != int(
        str(hydration.get("available_request_count") or -1)
    ):
        errors.append("DP6R request selection is incomplete")
    if int(str(hydration.get("parser_invocations") or 0)) != 0:
        errors.append("DP6R unexpectedly invoked the parser")
    artifacts = hydration.get("artifacts")
    if not isinstance(artifacts, dict):
        errors.append("DP6R artifact contract is missing")
    else:
        for key, path in (
            ("hydrated_document_manifest", document_path),
            ("content_catalog", content_path),
        ):
            artifact = artifacts.get(key)
            if not isinstance(artifact, dict) or str(artifact.get("sha256") or "") != file_sha256(path):
                errors.append(f"DP6R artifact hash mismatch: {key}")
    if errors:
        raise ValueError("Efficient batch preflight failed: " + "; ".join(errors))

    document_rows = read_csv(document_path)
    content_rows = read_csv(content_path)
    sec_coverage_rows = read_csv(sec_coverage_path)
    residual_documents, residual_pairs, residual_summary = build_residual_dispositions(
        document_rows=document_rows,
        sec_coverage_rows=sec_coverage_rows,
    )
    foundation = resolve_foundation(config_path, args.db)
    with connect_database(foundation.db_path, readonly=True) as conn:
        prior_hashes, prior_run_hash_counts = completed_content_hashes(
            conn,
            run_ids=DEFAULT_COMPLETED_RUN_IDS,
        )
        company_currencies = {
            str(row["ticker"]).upper(): str(row["currency"] or "USD")
            for row in conn.execute("SELECT ticker, currency FROM dim_company")
        }
    registry = load_registry(ADAPTER)
    registry_metrics = {request.metric_name for request in registry.parser_metrics}
    delta_rows, delta_summary = build_direct_delta_manifest(
        document_rows=document_rows,
        content_rows=content_rows,
        prior_hashes=prior_hashes,
        company_currencies=company_currencies,
        asof_date=asof_date,
        allowed_metric_ids=registry_metrics,
    )

    residual_document_path = output_dir / "transportation_residual_source_document_dispositions.csv"
    residual_pair_path = output_dir / "transportation_residual_source_pair_dispositions.csv"
    delta_path = output_dir / "transportation_non_sec_direct_delta_source_manifest.csv"
    manifest_path = output_dir / "transportation_efficient_parser_batch_manifest.json"
    write_csv_atomic(
        residual_document_path,
        RESIDUAL_DOCUMENT_FIELDS,
        residual_documents,
    )
    write_csv_atomic(
        residual_pair_path,
        RESIDUAL_PAIR_FIELDS,
        residual_pairs,
    )
    write_csv_atomic(delta_path, DELTA_SOURCE_FIELDS, delta_rows)

    source_manifest = load_source_manifest(delta_path)
    requested_metrics = {metric for row in delta_rows for metric in pipe_values(row["requested_metric_ids"])}
    errors = []
    if source_manifest.row_count != len(delta_rows):
        errors.append("direct source manifest row count mismatch")
    if not source_manifest.direct_document_mode:
        errors.append("direct source manifest mode is not active")
    if requested_metrics - registry_metrics:
        errors.append("direct source manifest contains unknown metrics")
    if int(str(residual_summary["unresolved_pair_count"])) != 0:
        errors.append("residual source dispositions are unresolved")
    if int(str(residual_summary["targeted_recovery_authorized_count"])) != 0:
        errors.append("additional retrieval remains authorized")
    if not delta_rows:
        errors.append("direct parser delta is empty")

    acceptance = "PASS" if not errors else "FAIL"
    payload = {
        "acceptance": acceptance,
        "gate": "DP6S_RESIDUAL_FREEZE_AND_DIRECT_DELTA_SEAL",
        "batch_version": EFFICIENT_BATCH_VERSION,
        "model_family": MODEL_FAMILY,
        "asof_date": asof_date,
        "completed_parser_run_ids_excluded": list(DEFAULT_COMPLETED_RUN_IDS),
        "completed_run_unique_hash_counts": prior_run_hash_counts,
        "completed_run_union_hash_count": len(prior_hashes),
        "residual_source_summary": residual_summary,
        "direct_delta_summary": delta_summary,
        "adapter_version": registry.adapter_version,
        "adapter_parser_metric_count": len(registry_metrics),
        "requested_metric_count": len(requested_metrics),
        "requested_metrics": sorted(requested_metrics),
        "source_manifest": {
            "path": str(delta_path.resolve()),
            "sha256": file_sha256(delta_path),
            "row_count": len(delta_rows),
            "direct_document_mode": True,
            "content_hash_validation": "PASS",
        },
        "artifacts": {
            "residual_document_dispositions": {
                "path": str(residual_document_path.resolve()),
                "row_count": len(residual_documents),
                "sha256": file_sha256(residual_document_path),
            },
            "residual_pair_dispositions": {
                "path": str(residual_pair_path.resolve()),
                "row_count": len(residual_pairs),
                "sha256": file_sha256(residual_pair_path),
            },
        },
        "network_requests": 0,
        "parser_invocations": 0,
        "feature_build_invocations": 0,
        "historical_materialization_invocations": 0,
        "calibration_invocations": 0,
        "portfolio_invocations": 0,
        "further_retrieval_authorized": False,
        "general_parser_execution_authorized": False,
        "one_shot_direct_delta_plan_authorized": acceptance == "PASS",
        "production_promotion_authorized": False,
        "errors": errors,
        "next_gate": (
            "BUILD_AND_VALIDATE_OFFLINE_DIRECT_DELTA_PLAN" if acceptance == "PASS" else "REPAIR_EFFICIENT_BATCH_SEAL"
        ),
    }
    write_text_atomic(
        manifest_path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if acceptance == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
