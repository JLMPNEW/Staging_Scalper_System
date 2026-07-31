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
from industrials.transportation.one_pass_preflight import (  # noqa: E402
    ONE_PASS_PREFLIGHT_VERSION,
    ONE_PASS_REQUIREMENT_FIELDS,
    ONE_PASS_TICKER_SCOPE_FIELDS,
    build_one_pass_preflight,
    summarize_one_pass_preflight,
)
from industrials.transportation.parser_coverage import (  # noqa: E402
    read_csv,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Reconcile every unresolved transportation pair to its sealed "
            "endpoint, semantic fixture, or financial repair contract and "
            "freeze the all-applicable-metrics one-pass parser scope. This "
            "command performs no network retrieval."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    return parser.parse_args(argv)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected JSON object")
    return payload


def _sealed_artifact(
    *,
    manifest: dict[str, Any],
    artifact_name: str,
    path: Path,
) -> bool:
    return (
        manifest.get("acceptance") == "PASS"
        and str(
            (
                manifest.get("artifacts") or {}
            ).get(artifact_name, {}).get("sha256")
            or ""
        )
        == file_sha256(path)
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    parser_cfg = family_config(config, MODEL_FAMILY)[
        "dedicated_parser"
    ]
    if bool(parser_cfg.get("parser_execution_authorized")):
        raise ValueError(
            "One-pass preflight requires parser execution disabled"
        )
    base_dir = config_path.parent
    asof_date = str(parser_cfg["source_census_asof_date"])
    output_dir = (
        resolve_path(parser_cfg["output_root"], base_dir=base_dir)
        / asof_date
    )
    residual_path = (
        output_dir
        / "transportation_non_sec_residual_source_audit.csv"
    )
    residual_manifest_path = (
        output_dir
        / "transportation_non_sec_residual_source_manifest.json"
    )
    endpoint_path = (
        output_dir / "transportation_non_sec_endpoint_roots.csv"
    )
    base_pair_path = (
        output_dir
        / "transportation_non_sec_pair_endpoint_map.csv"
    )
    endpoint_manifest_path = (
        output_dir / "transportation_non_sec_endpoint_manifest.json"
    )
    semantic_pair_path = (
        output_dir
        / "transportation_semantic_fixture_pair_contract.csv"
    )
    semantic_manifest_path = (
        output_dir
        / "transportation_semantic_fixture_freeze_manifest.json"
    )
    financial_pair_path = (
        output_dir
        / "transportation_financial_repair_pair_contract.csv"
    )
    financial_manifest_path = (
        output_dir
        / "transportation_financial_repair_freeze_manifest.json"
    )
    full_scope_path = resolve_path(
        parser_cfg["scope_manifest_csv"],
        base_dir=base_dir,
    )
    supporting_scope_path = resolve_path(
        parser_cfg["supporting_scope_manifest_csv"],
        base_dir=base_dir,
    )
    required = (
        residual_path,
        residual_manifest_path,
        endpoint_path,
        base_pair_path,
        endpoint_manifest_path,
        semantic_pair_path,
        semantic_manifest_path,
        financial_pair_path,
        financial_manifest_path,
        full_scope_path,
        supporting_scope_path,
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            f"Missing one-pass preflight inputs: {missing}"
        )
    residual_manifest = _read_json(residual_manifest_path)
    endpoint_manifest = _read_json(endpoint_manifest_path)
    semantic_manifest = _read_json(semantic_manifest_path)
    financial_manifest = _read_json(financial_manifest_path)
    if (
        residual_manifest.get("acceptance") != "PASS"
        or str(
            (residual_manifest.get("artifact") or {}).get("sha256")
            or ""
        )
        != file_sha256(residual_path)
    ):
        raise ValueError("Residual audit is not hash-sealed")
    if not _sealed_artifact(
        manifest=endpoint_manifest,
        artifact_name="endpoint_roots",
        path=endpoint_path,
    ) or not _sealed_artifact(
        manifest=endpoint_manifest,
        artifact_name="pair_endpoint_map",
        path=base_pair_path,
    ):
        raise ValueError("Endpoint artifacts are not hash-sealed")
    if not _sealed_artifact(
        manifest=semantic_manifest,
        artifact_name="semantic_fixture_pair_contract",
        path=semantic_pair_path,
    ):
        raise ValueError("Semantic fixture contract is not hash-sealed")
    if not _sealed_artifact(
        manifest=financial_manifest,
        artifact_name="financial_repair_pair_contract",
        path=financial_pair_path,
    ):
        raise ValueError("Financial repair contract is not hash-sealed")
    residual_rows = read_csv(residual_path)
    endpoint_rows = read_csv(endpoint_path)
    base_pair_rows = read_csv(base_pair_path)
    semantic_pair_rows = read_csv(semantic_pair_path)
    financial_pair_rows = read_csv(financial_pair_path)
    requirement_rows, ticker_scope_rows, errors = (
        build_one_pass_preflight(
            residual_rows=residual_rows,
            endpoint_rows=endpoint_rows,
            base_pair_endpoint_rows=base_pair_rows,
            semantic_pair_rows=semantic_pair_rows,
            financial_pair_rows=financial_pair_rows,
            full_scope_rows=read_csv(full_scope_path),
            supporting_scope_rows=read_csv(
                supporting_scope_path
            ),
        )
    )
    expected_pairs = int(
        residual_manifest.get("residual_pair_count") or 0
    )
    expected_tickers = int(
        endpoint_manifest.get("endpoint_root_count") or 0
    )
    if len(requirement_rows) != expected_pairs:
        errors.append(
            "requirement rows="
            f"{len(requirement_rows)} expected={expected_pairs}"
        )
    if len(ticker_scope_rows) != expected_tickers:
        errors.append(
            f"ticker scopes={len(ticker_scope_rows)} "
            f"expected={expected_tickers}"
        )
    if any(
        int(str(row["parse_all_applicable_metrics"])) != 1
        for row in requirement_rows
    ):
        errors.append(
            "a discovery requirement does not use the all-metric parser scope"
        )
    requirement_path = (
        output_dir
        / "transportation_one_pass_source_requirement_map.csv"
    )
    ticker_scope_path = (
        output_dir
        / "transportation_one_pass_ticker_parser_scope.csv"
    )
    manifest_path = (
        output_dir
        / "transportation_one_pass_preflight_manifest.json"
    )
    write_csv_atomic(
        requirement_path,
        ONE_PASS_REQUIREMENT_FIELDS,
        requirement_rows,
    )
    write_csv_atomic(
        ticker_scope_path,
        ONE_PASS_TICKER_SCOPE_FIELDS,
        ticker_scope_rows,
    )
    payload = {
        "acceptance": (
            "PASS"
            if requirement_rows and ticker_scope_rows and not errors
            else "FAIL"
        ),
        "gate": "DP6N_ALL_INCLUSIVE_ONE_PASS_SOURCE_PREFLIGHT",
        "preflight_version": ONE_PASS_PREFLIGHT_VERSION,
        "model_family": MODEL_FAMILY,
        "asof_date": asof_date,
        **summarize_one_pass_preflight(requirement_rows),
        "ticker_parser_scope_count": len(ticker_scope_rows),
        "all_residual_pairs_reconciled": not errors,
        "all_inclusive_metric_search_scope_frozen": not errors,
        "parse_all_applicable_metrics_per_document": not errors,
        "endpoint_roots_hash_sealed": True,
        "semantic_fixtures_hash_sealed": True,
        "financial_repairs_hash_sealed": True,
        "document_urls_enumerated": False,
        "retrieval_authorized": False,
        "parser_execution_authorized": False,
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
            "residual_audit": {
                "path": str(residual_path.resolve()),
                "sha256": file_sha256(residual_path),
            },
            "endpoint_roots": {
                "path": str(endpoint_path.resolve()),
                "sha256": file_sha256(endpoint_path),
            },
            "base_pair_endpoint_map": {
                "path": str(base_pair_path.resolve()),
                "sha256": file_sha256(base_pair_path),
            },
            "semantic_pair_contract": {
                "path": str(semantic_pair_path.resolve()),
                "sha256": file_sha256(semantic_pair_path),
            },
            "financial_pair_contract": {
                "path": str(financial_pair_path.resolve()),
                "sha256": file_sha256(financial_pair_path),
            },
            "full_metric_scope": {
                "path": str(full_scope_path.resolve()),
                "sha256": file_sha256(full_scope_path),
            },
            "supporting_metric_scope": {
                "path": str(supporting_scope_path.resolve()),
                "sha256": file_sha256(supporting_scope_path),
            },
        },
        "artifacts": {
            "one_pass_source_requirement_map": {
                "path": str(requirement_path.resolve()),
                "row_count": len(requirement_rows),
                "sha256": file_sha256(requirement_path),
            },
            "one_pass_ticker_parser_scope": {
                "path": str(ticker_scope_path.resolve()),
                "row_count": len(ticker_scope_rows),
                "sha256": file_sha256(ticker_scope_path),
            },
        },
        "next_gate": (
            "ENUMERATE_DEDUPLICATE_AND_HASH_PRIMARY_DOCUMENTS_ONCE"
            if not errors
            else "REVIEW_ONE_PASS_PREFLIGHT_ERRORS"
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
