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
from industrials.core.reports import write_text_atomic  # noqa: E402
from industrials.transportation.bounded_repair import (  # noqa: E402
    BOUNDED_REPAIR_EXECUTION_VERSION,
    apply_financial_overrides,
)
from industrials.transportation.parser_coverage import (  # noqa: E402
    build_cohort_summary,
    build_metric_summary,
    read_csv,
    write_coverage_artifacts,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
)
from industrials.transportation.sec_union_coverage import (  # noqa: E402
    coverage_counts,
    coverage_rates_from_counts,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Apply only sealed bounded financial overrides to the final "
            "transportation coverage and rebuild summaries once. No parser, "
            "source retrieval, feature build, or historical build is run."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--base-coverage-prefix",
        default="transportation_all_source_union",
    )
    parser.add_argument(
        "--artifact-prefix",
        default="transportation_bounded_repair_union",
    )
    return parser.parse_args(argv)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected JSON object")
    return payload


def _valid_prefix(value: str) -> str:
    result = value.strip()
    if not result or result in {".", ".."} or "/" in result or "\\" in result:
        raise ValueError("artifact prefix must be a filename prefix")
    return result


def _sealed(
    manifest: dict[str, Any],
    name: str,
    path: Path,
) -> bool:
    artifact = (manifest.get("artifacts") or {}).get(name) or {}
    return (
        str(artifact.get("path") or "") == str(path.resolve())
        and str(artifact.get("sha256") or "") == file_sha256(path)
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    base_prefix = _valid_prefix(str(args.base_coverage_prefix))
    artifact_prefix = _valid_prefix(str(args.artifact_prefix))
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    parser_cfg = family_config(config, MODEL_FAMILY)[
        "dedicated_parser"
    ]
    if bool(parser_cfg.get("parser_execution_authorized")):
        raise ValueError(
            "Bounded coverage requires general parser execution disabled"
        )
    asof_date = str(parser_cfg["source_census_asof_date"])
    output_dir = (
        resolve_path(parser_cfg["output_root"], base_dir=config_path.parent)
        / asof_date
    )
    base_coverage_path = (
        output_dir / f"{base_prefix}_ticker_metric_coverage.csv"
    )
    base_metric_path = output_dir / f"{base_prefix}_metric_summary.csv"
    base_cohort_path = (
        output_dir / f"{base_prefix}_cohort_metric_coverage.csv"
    )
    base_support_path = (
        output_dir / f"{base_prefix}_support_coverage.csv"
    )
    base_manifest_path = (
        output_dir / f"{base_prefix}_coverage_manifest.json"
    )
    execution_path = (
        output_dir / "transportation_bounded_financial_repairs.csv"
    )
    execution_manifest_path = (
        output_dir
        / "transportation_bounded_repair_execution_manifest.json"
    )
    scope_manifest_path = (
        output_dir / "transportation_bounded_repair_scope_manifest.json"
    )
    source_path = (
        output_dir
        / "transportation_non_sec_direct_delta_source_manifest.csv"
    )
    scope_path = resolve_path(
        parser_cfg["scope_manifest_csv"],
        base_dir=config_path.parent,
    )
    support_scope_path = resolve_path(
        parser_cfg["supporting_scope_manifest_csv"],
        base_dir=config_path.parent,
    )
    required = (
        base_coverage_path,
        base_metric_path,
        base_cohort_path,
        base_support_path,
        base_manifest_path,
        execution_path,
        execution_manifest_path,
        scope_manifest_path,
        source_path,
        scope_path,
        support_scope_path,
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            f"Missing bounded-coverage inputs: {missing}"
        )
    base_manifest = _read_json(base_manifest_path)
    execution_manifest = _read_json(execution_manifest_path)
    scope_manifest = _read_json(scope_manifest_path)
    errors: list[str] = []
    if (
        base_manifest.get("acceptance") != "PASS"
        or not _sealed(
            base_manifest,
            "ticker_metric_coverage",
            base_coverage_path,
        )
        or not _sealed(
            base_manifest,
            "support_metric_coverage",
            base_support_path,
        )
        or int(base_manifest.get("parser_invocations") or 0) != 0
    ):
        errors.append("base coverage is not sealed and parse-free")
    if (
        execution_manifest.get("acceptance")
        not in {"PASS", "PASS_WITH_EXPLICIT_LIMITATIONS"}
        or execution_manifest.get("execution_version")
        != BOUNDED_REPAIR_EXECUTION_VERSION
        or not _sealed(
            execution_manifest,
            "financial_repairs",
            execution_path,
        )
        or int(execution_manifest.get("parser_invocations") or 0) != 0
        or int(execution_manifest.get("network_requests") or 0) != 0
    ):
        errors.append("bounded repair execution is not sealed and parse-free")
    if scope_manifest.get("acceptance") != "PASS":
        errors.append("bounded repair scope is not passing")

    base_rows = read_csv(base_coverage_path)
    support_rows = read_csv(base_support_path)
    financial_rows = read_csv(execution_path)
    final_rows, override_counts = apply_financial_overrides(
        coverage_rows=base_rows,
        financial_rows=financial_rows,
    )
    metric_rows = build_metric_summary(final_rows)
    cohort_rows = build_cohort_summary(final_rows)
    before_counts = coverage_counts(base_rows)
    after_counts = coverage_counts(final_rows)
    before_rates = coverage_rates_from_counts(before_counts)
    after_rates = coverage_rates_from_counts(after_counts)
    if override_counts != {
        "COVERED_FINANCIAL_DERIVED": 6,
        "NOT_APPLICABLE": 9,
    }:
        errors.append(
            f"bounded coverage overrides changed: {override_counts}"
        )
    if len(final_rows) != len(base_rows):
        errors.append("bounded overlay changed final scope row count")
    if sum(after_counts.values()) != sum(before_counts.values()) - 9:
        errors.append("not-applicable denominator adjustment is not exact")
    before_accepted = int(before_counts.get("COVERED_ACCEPTED") or 0) + int(
        before_counts.get("COVERED_FINANCIAL_DERIVED") or 0
    )
    after_accepted = int(after_counts.get("COVERED_ACCEPTED") or 0) + int(
        after_counts.get("COVERED_FINANCIAL_DERIVED") or 0
    )
    if after_accepted != before_accepted + 6:
        errors.append(
            "bounded aligned and exact-source financial repairs did not "
            "add six pairs"
        )

    final_path = (
        output_dir / f"{artifact_prefix}_ticker_metric_coverage.csv"
    )
    metric_path = output_dir / f"{artifact_prefix}_metric_summary.csv"
    cohort_path = (
        output_dir / f"{artifact_prefix}_cohort_metric_coverage.csv"
    )
    support_path = (
        output_dir / f"{artifact_prefix}_support_coverage.csv"
    )
    manifest_path = (
        output_dir / f"{artifact_prefix}_coverage_manifest.json"
    )
    run = {
        "run_id": int(base_manifest.get("run_id") or 0),
        "status": str(base_manifest.get("run_status") or ""),
        "planned_work_count": int(
            base_manifest.get("planned_work_count") or 0
        ),
        "completed_work_count": int(
            base_manifest.get("newly_executed_work_count") or 0
        ),
        "linked_completed_work_count": int(
            base_manifest.get("linked_completed_work_count") or 0
        ),
        "failed_work_count": int(
            base_manifest.get("failed_work_count") or 0
        ),
    }
    payload = write_coverage_artifacts(
        final_rows=final_rows,
        metric_rows=metric_rows,
        cohort_rows=cohort_rows,
        support_rows=support_rows,
        final_path=final_path,
        metric_path=metric_path,
        cohort_path=cohort_path,
        support_path=support_path,
        manifest_path=manifest_path,
        run=run,
        scope_path=scope_path,
        support_scope_path=support_scope_path,
    )
    if payload["acceptance"] != "PASS":
        errors.append("coverage artifact cardinality gate failed")
    payload.update(
        {
            "acceptance": "PASS" if not errors else "FAIL",
            "gate": "DP7A_BOUNDED_REPAIR_UNION_COVERAGE",
            "artifact_prefix": artifact_prefix,
            "base_coverage_prefix": base_prefix,
            "base_run_id": int(base_manifest.get("base_run_id") or 0),
            "base_review_evaluation_id": int(
                base_manifest.get("base_review_evaluation_id") or 0
            ),
            "review_evaluation_ids": dict(
                base_manifest.get("review_evaluation_ids") or {}
            ),
            "delta_run_id": int(
                base_manifest.get("delta_run_id") or 0
            ),
            "repair_run_id": int(
                base_manifest.get("repair_run_id") or 0
            ),
            "direct_document_run_id": int(
                base_manifest.get("direct_document_run_id") or 0
            ),
            "additional_run_ids": list(
                base_manifest.get("additional_run_ids") or ()
            ),
            "supplemental_run_ids": list(
                base_manifest.get("supplemental_run_ids") or ()
            ),
            "source_manifest_path": str(source_path.resolve()),
            "source_manifest_sha256": file_sha256(source_path),
            "before_coverage_status_counts": before_counts,
            "coverage_status_counts": after_counts,
            "financial_override_counts": override_counts,
            "before_applicable_final_scope_row_count": sum(
                before_counts.values()
            ),
            "applicable_final_scope_row_count": sum(after_counts.values()),
            "before_accepted_coverage_rate": before_rates["accepted"],
            "before_usable_coverage_rate": before_rates["usable"],
            "before_discovery_coverage_rate": before_rates["discovery"],
            "accepted_coverage_rate": after_rates["accepted"],
            "usable_coverage_rate": after_rates["usable"],
            "discovery_coverage_rate": after_rates["discovery"],
            "accepted_coverage_rate_change": (
                after_rates["accepted"] - before_rates["accepted"]
            ),
            "usable_coverage_rate_change": (
                after_rates["usable"] - before_rates["usable"]
            ),
            "discovery_coverage_rate_change": (
                after_rates["discovery"] - before_rates["discovery"]
            ),
            "bounded_repair_execution_acceptance": (
                execution_manifest.get("acceptance")
            ),
            "limitations": list(
                execution_manifest.get("limitations") or ()
            ),
            "parser_invocations": 0,
            "network_requests": 0,
            "retrieval_invocations": 0,
            "feature_build_invocations": 0,
            "historical_materialization_invocations": 0,
            "calibration_invocations": 0,
            "portfolio_invocations": 0,
            "production_promotion_authorized": False,
            "errors": errors,
            "next_gate": (
                "FINAL_STORED_EVIDENCE_ADJUDICATION"
                if not errors
                else "REVIEW_BOUNDED_COVERAGE_ERRORS"
            ),
        }
    )
    payload_inputs = payload.get("inputs")
    if not isinstance(payload_inputs, dict):
        raise ValueError("coverage payload is missing its inputs mapping")
    payload_inputs.update(
        {
            "base_coverage": {
                "path": str(base_coverage_path.resolve()),
                "sha256": file_sha256(base_coverage_path),
            },
            "bounded_repair_execution_manifest": {
                "path": str(execution_manifest_path.resolve()),
                "sha256": file_sha256(execution_manifest_path),
            },
            "bounded_repair_scope_manifest": {
                "path": str(scope_manifest_path.resolve()),
                "sha256": file_sha256(scope_manifest_path),
            },
        }
    )
    write_text_atomic(
        manifest_path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["acceptance"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
