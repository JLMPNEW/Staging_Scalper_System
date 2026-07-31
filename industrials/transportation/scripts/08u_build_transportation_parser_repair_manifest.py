#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.contracts import file_sha256  # noqa: E402
from industrials.core.config import (  # noqa: E402
    family_config,
    load_yaml,
    resolve_path,
)
from industrials.core.reports import (  # noqa: E402
    write_csv_atomic,
    write_text_atomic,
)
from industrials.transportation.parser_coverage import (  # noqa: E402
    read_csv,
    read_only_connection,
)
from industrials.transportation.parser_repair import (  # noqa: E402
    PARSER_REPAIR_FIELDS,
    PARSER_REPAIR_VERSION,
    build_parser_repair_rows,
    summarize_parser_repair,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
    resolve_foundation,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build the hash-sealed transportation PDF repair manifest from "
            "run-59 parser-failure evidence. No source is opened or parsed."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    return parser.parse_args(argv)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected JSON object")
    return payload


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    parser_cfg = family_config(config, MODEL_FAMILY)[
        "dedicated_parser"
    ]
    if bool(parser_cfg.get("parser_execution_authorized")):
        raise ValueError(
            "Parser repair manifest requires the general parser switch off"
        )
    base_dir = config_path.parent
    foundation = resolve_foundation(config_path, args.db)
    output_dir = (
        resolve_path(parser_cfg["output_root"], base_dir=base_dir)
        / str(parser_cfg["source_census_asof_date"])
    )
    execution_gate_path = (
        output_dir / "transportation_sec_delta_execution_gate.json"
    )
    # 08u consumes the DP6G (pre-repair) residual audit, which now carries a
    # variant-scoped name so the DP6J rerun can no longer overwrite it.
    residual_manifest_path = (
        output_dir
        / "transportation_pre_repair_non_sec_residual_source_manifest.json"
    )
    residual_path = (
        output_dir
        / "transportation_pre_repair_non_sec_residual_source_audit.csv"
    )
    source_path = (
        output_dir
        / "transportation_delta_parser_source_manifest.csv"
    )
    for path in (
        execution_gate_path,
        residual_manifest_path,
        residual_path,
        source_path,
    ):
        if not path.is_file():
            raise FileNotFoundError(path)
    execution_gate = _read_json(execution_gate_path)
    residual_manifest = _read_json(residual_manifest_path)
    run_id = int(execution_gate.get("run_id") or 0)
    if (
        execution_gate.get("acceptance") != "PASS"
        or residual_manifest.get("acceptance") != "PASS"
        or run_id <= 0
        or str(execution_gate.get("source_manifest_sha256") or "")
        != file_sha256(source_path)
        or str(
            (residual_manifest.get("artifact") or {}).get("sha256")
            or ""
        )
        != file_sha256(residual_path)
    ):
        raise ValueError("Run-59 and residual inputs are not sealed")
    residual_failure_pairs = {
        (row["ticker"], row["metric_id"])
        for row in read_csv(residual_path)
        if row["coverage_status"] == "PARSER_FAILURE_ONLY"
    }
    with read_only_connection(
        foundation.db_path,
        timeout_sec=foundation.timeout_sec,
    ) as connection:
        failure_rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT evidence.ticker, evidence.accession_number,
                       evidence.metric_name, evidence.source_document,
                       evidence.candidate_status, evidence.status_reason,
                       evidence.extraction_method
                FROM sec_parser_run_metric_evidence AS relation
                JOIN sec_parser_metric_evidence_shadow AS evidence
                  ON evidence.evidence_key=relation.evidence_key
                WHERE relation.run_id=?
                  AND evidence.model_family=?
                  AND evidence.candidate_status='PARSER_FAILURE'
                ORDER BY evidence.ticker, evidence.accession_number,
                         evidence.source_document, evidence.metric_name
                """,
                (run_id, MODEL_FAMILY),
            )
        ]
    repair_rows, errors = build_parser_repair_rows(
        source_rows=read_csv(source_path),
        failure_rows=failure_rows,
    )
    summary = summarize_parser_repair(
        repair_rows=repair_rows,
        failure_rows=failure_rows,
        residual_failure_pairs=residual_failure_pairs,
    )
    if (
        int(
            str(
                summary[
                    "residual_pairs_covered_by_failure_evidence_count"
                ]
            )
        )
        != len(residual_failure_pairs)
    ):
        errors.append(
            "not every residual parser-failure pair maps to run evidence"
        )
    if int(str(summary["repair_max_document_bytes"])) > 125_000_000:
        errors.append("repair PDF exceeds the sealed 125 MB ceiling")
    csv_path = (
        output_dir
        / "transportation_parser_repair_source_manifest.csv"
    )
    manifest_path = (
        output_dir / "transportation_parser_repair_manifest.json"
    )
    write_csv_atomic(csv_path, PARSER_REPAIR_FIELDS, repair_rows)
    payload = {
        "acceptance": (
            "PASS" if repair_rows and not errors else "FAIL"
        ),
        "gate": "DP6H_TARGETED_PDF_REPAIR_MANIFEST",
        "repair_version": PARSER_REPAIR_VERSION,
        "model_family": MODEL_FAMILY,
        "base_delta_run_id": run_id,
        **summary,
        "sealed_max_pdf_bytes": 125_000_000,
        "sealed_pdf_timeout_seconds": 180.0,
        "sealed_max_pdf_pages": 250,
        "pdf_ocr_authorized": False,
        "network_requests": 0,
        "parser_invocations": 0,
        "feature_build_invocations": 0,
        "historical_materialization_invocations": 0,
        "calibration_invocations": 0,
        "portfolio_invocations": 0,
        "production_promotion_authorized": False,
        "errors": errors,
        "inputs": {
            "sec_delta_execution_gate": {
                "path": str(execution_gate_path.resolve()),
                "sha256": file_sha256(execution_gate_path),
            },
            "residual_source_audit": {
                "path": str(residual_path.resolve()),
                "sha256": file_sha256(residual_path),
            },
            "delta_source_manifest": {
                "path": str(source_path.resolve()),
                "sha256": file_sha256(source_path),
            },
        },
        "artifact": {
            "path": str(csv_path.resolve()),
            "row_count": len(repair_rows),
            "sha256": file_sha256(csv_path),
        },
        "next_gate": (
            "PLAN_AND_EXECUTE_SEALED_PDF_REPAIR"
            if repair_rows and not errors
            else "REPAIR_PDF_MANIFEST"
        ),
    }
    write_text_atomic(
        manifest_path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["acceptance"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
