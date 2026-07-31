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
from industrials.transportation.bounded_repair import (  # noqa: E402
    BOUNDED_REPAIR_SCOPE_FIELDS,
    BOUNDED_REPAIR_SCOPE_VERSION,
    build_bounded_repair_scope,
    summarize_scope,
)
from industrials.transportation.parser_coverage import (  # noqa: E402
    read_csv,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
)


EXPECTED_LANE_COUNTS = {
    "EMPTY_PDF_OCR": 34,
    "FINANCIAL_DETERMINISTIC_ALIGNMENT": 23,
    "FINANCIAL_NOT_APPLICABLE": 9,
    "FINANCIAL_SOURCE_GAP": 13,
    "STORED_EVIDENCE_REVIEW": 719,
    "TEXT_HIT_NO_VALUE": 100,
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Hash-seal the exact post-one-pass transportation repair scope. "
            "This command is read-only with respect to source data and never "
            "invokes retrieval, parsing, features, calibration, or portfolio."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--coverage-prefix",
        default="transportation_all_source_union",
    )
    return parser.parse_args(argv)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected JSON object")
    return payload


def _sealed(
    manifest: dict[str, Any],
    *,
    artifact_name: str,
    path: Path,
) -> bool:
    artifact = (manifest.get("artifacts") or {}).get(artifact_name) or {}
    return (
        str(artifact.get("path") or "") == str(path.resolve())
        and str(artifact.get("sha256") or "") == file_sha256(path)
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    coverage_prefix = str(args.coverage_prefix).strip()
    if (
        not coverage_prefix
        or "/" in coverage_prefix
        or "\\" in coverage_prefix
    ):
        raise ValueError("--coverage-prefix must be a filename prefix")

    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    parser_cfg = family_config(config, MODEL_FAMILY)[
        "dedicated_parser"
    ]
    if bool(parser_cfg.get("parser_execution_authorized")):
        raise ValueError(
            "Bounded repair scope requires general parser execution disabled"
        )
    asof_date = str(parser_cfg["source_census_asof_date"])
    output_dir = (
        resolve_path(parser_cfg["output_root"], base_dir=config_path.parent)
        / asof_date
    )

    coverage_path = (
        output_dir / f"{coverage_prefix}_ticker_metric_coverage.csv"
    )
    coverage_manifest_path = (
        output_dir / f"{coverage_prefix}_coverage_manifest.json"
    )
    financial_path = (
        output_dir
        / "transportation_financial_repair_pair_contract.csv"
    )
    financial_manifest_path = (
        output_dir
        / "transportation_financial_repair_freeze_manifest.json"
    )
    cache_path = (
        output_dir / "transportation_content_text_cache_results.csv"
    )
    cache_manifest_path = (
        output_dir / "transportation_content_text_cache_manifest.json"
    )
    source_path = (
        output_dir
        / "transportation_non_sec_direct_delta_source_manifest.csv"
    )
    direct_gate_path = (
        output_dir
        / "transportation_non_sec_direct_delta_execution_gate.json"
    )
    adjudication_path = (
        output_dir
        / f"{coverage_prefix}_evidence_adjudication.csv"
    )
    adjudication_manifest_path = (
        output_dir
        / f"{coverage_prefix}_evidence_adjudication_manifest.json"
    )
    required = (
        coverage_path,
        coverage_manifest_path,
        financial_path,
        financial_manifest_path,
        cache_path,
        cache_manifest_path,
        source_path,
        direct_gate_path,
        adjudication_path,
        adjudication_manifest_path,
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Missing bounded-scope inputs: {missing}")

    coverage_manifest = _read_json(coverage_manifest_path)
    financial_manifest = _read_json(financial_manifest_path)
    cache_manifest = _read_json(cache_manifest_path)
    direct_gate = _read_json(direct_gate_path)
    adjudication_manifest = _read_json(adjudication_manifest_path)
    errors: list[str] = []
    if (
        coverage_manifest.get("acceptance") != "PASS"
        or not _sealed(
            coverage_manifest,
            artifact_name="ticker_metric_coverage",
            path=coverage_path,
        )
        or int(coverage_manifest.get("parser_invocations") or 0) != 0
    ):
        errors.append("base all-source coverage is not sealed and parse-free")
    if (
        financial_manifest.get("acceptance") != "PASS"
        or not _sealed(
            financial_manifest,
            artifact_name="financial_repair_pair_contract",
            path=financial_path,
        )
    ):
        errors.append("financial repair contract is not hash-sealed")
    cache_result = cache_manifest.get("results") or {}
    if (
        cache_manifest.get("acceptance")
        != "PASS_WITH_EXPLICIT_EXTRACTION_LIMITATIONS"
        or str(cache_result.get("path") or "") != str(cache_path.resolve())
        or str(cache_result.get("sha256") or "") != file_sha256(cache_path)
        or int(cache_manifest.get("network_requests") or 0) != 0
        or int(cache_manifest.get("parser_invocations") or 0) != 0
    ):
        errors.append("content-text cache is not sealed and parse-free")
    if (
        direct_gate.get("acceptance") != "PASS"
        or str(direct_gate.get("source_manifest_sha256") or "")
        != file_sha256(source_path)
    ):
        errors.append("direct source manifest is not hash-sealed")
    if (
        adjudication_manifest.get("acceptance") != "PASS"
        or not _sealed(
            adjudication_manifest,
            artifact_name="pair_adjudication",
            path=adjudication_path,
        )
    ):
        errors.append("all-source adjudication is not hash-sealed")

    coverage_rows = read_csv(coverage_path)
    financial_rows = read_csv(financial_path)
    cache_rows = read_csv(cache_path)
    source_rows = read_csv(source_path)
    adjudication_rows = read_csv(adjudication_path)
    empty_hashes = {
        str(row["content_sha256"]).lower()
        for row in cache_rows
        if row["cache_status"] == "CACHE_VALIDATED_EMPTY_PYMUPDF"
    }
    empty_context_rows = [
        row
        for row in source_rows
        if str(row["content_sha256"]).lower() in empty_hashes
    ]
    scope_rows = build_bounded_repair_scope(
        financial_rows=financial_rows,
        coverage_rows=coverage_rows,
        empty_context_rows=empty_context_rows,
        adjudication_rows=adjudication_rows,
    )
    summary = summarize_scope(scope_rows)
    observed_lanes = summary["repair_lane_counts"]
    if observed_lanes != EXPECTED_LANE_COUNTS:
        errors.append(
            "bounded repair lane counts changed: "
            f"observed={observed_lanes} expected={EXPECTED_LANE_COUNTS}"
        )
    expected_total = sum(EXPECTED_LANE_COUNTS.values())
    if int(str(summary["repair_item_count"])) != expected_total:
        errors.append(
            f"repair items={summary['repair_item_count']} "
            f"expected={expected_total}"
        )
    if len(empty_hashes) != EXPECTED_LANE_COUNTS["EMPTY_PDF_OCR"]:
        errors.append("validated-empty content hash count changed")
    if len({row["content_sha256"] for row in empty_context_rows}) != len(
        empty_hashes
    ):
        errors.append("not every empty content hash maps to source context")

    scope_path = (
        output_dir / "transportation_bounded_repair_scope.csv"
    )
    manifest_path = (
        output_dir / "transportation_bounded_repair_scope_manifest.json"
    )
    write_csv_atomic(
        scope_path,
        BOUNDED_REPAIR_SCOPE_FIELDS,
        scope_rows,
    )
    payload = {
        "acceptance": "PASS" if scope_rows and not errors else "FAIL",
        "gate": "DP6Y_BOUNDED_REPAIR_SCOPE_FREEZE",
        "scope_version": BOUNDED_REPAIR_SCOPE_VERSION,
        "model_family": MODEL_FAMILY,
        "asof_date": asof_date,
        "coverage_prefix": coverage_prefix,
        **summary,
        "empty_pdf_context_count": len(empty_context_rows),
        "full_parser_batch_authorized": False,
        "scope_expansion_authorized": False,
        "network_requests": 0,
        "retrieval_invocations": 0,
        "parser_invocations": 0,
        "feature_build_invocations": 0,
        "historical_materialization_invocations": 0,
        "calibration_invocations": 0,
        "portfolio_invocations": 0,
        "production_promotion_authorized": False,
        "errors": errors,
        "inputs": {
            "coverage": {
                "path": str(coverage_path.resolve()),
                "sha256": file_sha256(coverage_path),
            },
            "coverage_manifest": {
                "path": str(coverage_manifest_path.resolve()),
                "sha256": file_sha256(coverage_manifest_path),
            },
            "financial_repair_contract": {
                "path": str(financial_path.resolve()),
                "sha256": file_sha256(financial_path),
            },
            "content_text_cache": {
                "path": str(cache_path.resolve()),
                "sha256": file_sha256(cache_path),
            },
            "direct_source_manifest": {
                "path": str(source_path.resolve()),
                "sha256": file_sha256(source_path),
            },
            "adjudication": {
                "path": str(adjudication_path.resolve()),
                "sha256": file_sha256(adjudication_path),
            },
        },
        "artifacts": {
            "bounded_repair_scope": {
                "path": str(scope_path.resolve()),
                "row_count": len(scope_rows),
                "sha256": file_sha256(scope_path),
            }
        },
        "next_gate": (
            "EXECUTE_BOUNDED_CACHE_ONLY_REPAIRS"
            if not errors
            else "REVIEW_BOUNDED_REPAIR_SCOPE_ERRORS"
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
