#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.contracts import file_sha256  # noqa: E402
from dedicated_parser.golden import validate_corpus  # noqa: E402
from industrials.core.config import (  # noqa: E402
    family_config,
    load_yaml,
    resolve_path,
)
from industrials.core.reports import (  # noqa: E402
    write_csv_atomic,
    write_text_atomic,
)
from industrials.transportation.dedicated_parser_adapter import (  # noqa: E402
    get_registry,
)
from industrials.transportation.final_metric_freeze import (  # noqa: E402
    FINAL_METRIC_DISPOSITION_FIELDS,
    FINAL_METRIC_FREEZE_VERSION,
    build_final_metric_dispositions,
)
from industrials.transportation.parser_coverage import (  # noqa: E402
    accepted_periods_for_final_metric,
    load_review_evidence_stats,
    read_csv,
    read_only_connection,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
    resolve_foundation,
)
from industrials.transportation.sec_union_coverage import (  # noqa: E402
    merge_evidence_stats,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Freeze final transportation metric dispositions from the sealed "
            "all-source coverage, policy replay, semantic fixtures, and "
            "financial repair contracts. This command is parse-free."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument(
        "--coverage-prefix",
        default="transportation_all_source_union",
    )
    parser.add_argument(
        "--financial-execution-manifest",
        type=Path,
        default=None,
        help=(
            "Optional bounded financial-repair execution manifest. When "
            "provided, it supersedes the old requirement that the original "
            "repair-contract freeze hash the newly regenerated semantic "
            "fixture manifest."
        ),
    )
    parser.add_argument(
        "--policy-replay-manifest",
        type=Path,
        action="append",
        default=None,
        help=(
            "Repeatable policy-only replay manifest. Relative paths resolve "
            "inside the configured dedicated-parser output directory. "
            "Reviewed multi-run coverage requires one manifest per source "
            "run."
        ),
    )
    parser.add_argument(
        "--policy-replay-views-manifest",
        type=Path,
        default=Path(
            "transportation_fixture_replay_policy_views_manifest.json"
        ),
        help=(
            "Run-scoped policy/golden view manifest used to verify each "
            "offline replay without comparing a scoped hash to the full "
            "active registry."
        ),
    )
    return parser.parse_args(argv)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected JSON object")
    return payload


def _sealed_artifact(
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
            "Final metric freeze requires parser execution disabled"
        )
    base_dir = config_path.parent
    foundation = resolve_foundation(config_path, args.db)
    asof_date = str(parser_cfg["source_census_asof_date"])
    output_dir = (
        resolve_path(parser_cfg["output_root"], base_dir=base_dir)
        / asof_date
    )

    coverage_path = (
        output_dir / f"{coverage_prefix}_ticker_metric_coverage.csv"
    )
    coverage_manifest_path = (
        output_dir / f"{coverage_prefix}_coverage_manifest.json"
    )
    adjudication_path = (
        output_dir / f"{coverage_prefix}_evidence_adjudication.csv"
    )
    adjudication_manifest_path = (
        output_dir
        / f"{coverage_prefix}_evidence_adjudication_manifest.json"
    )
    semantic_manifest_path = (
        output_dir
        / "transportation_semantic_fixture_freeze_manifest.json"
    )
    financial_manifest_path = (
        output_dir
        / "transportation_financial_repair_freeze_manifest.json"
    )
    financial_execution_manifest_path = (
        (
            args.financial_execution_manifest
            if args.financial_execution_manifest.is_absolute()
            else output_dir / args.financial_execution_manifest
        )
        .expanduser()
        .resolve()
        if args.financial_execution_manifest is not None
        else None
    )
    replay_manifest_args = args.policy_replay_manifest or [
        Path(f"transportation_fixture_policy_replay_run{run_id}.json")
        for run_id in (58, 59, 60, 65)
    ]
    replay_manifest_paths = [
        (
            value if value.is_absolute() else output_dir / value
        ).expanduser().resolve()
        for value in replay_manifest_args
    ]
    replay_views_manifest_path = (
        (
            args.policy_replay_views_manifest
            if args.policy_replay_views_manifest.is_absolute()
            else output_dir / args.policy_replay_views_manifest
        )
        .expanduser()
        .resolve()
    )
    execution_gate_path = (
        output_dir
        / "transportation_non_sec_direct_delta_execution_gate.json"
    )
    ocr_execution_gate_path = (
        output_dir
        / "transportation_ocr_delta_parser_execution_gate.json"
    )
    ocr_recovery_manifest_path = (
        output_dir / "transportation_ocr_delta_manifest.json"
    )
    required_paths = [
        coverage_path,
        coverage_manifest_path,
        adjudication_path,
        adjudication_manifest_path,
        semantic_manifest_path,
        financial_manifest_path,
        replay_views_manifest_path,
        execution_gate_path,
    ]
    required_paths.extend(replay_manifest_paths)
    if financial_execution_manifest_path is not None:
        required_paths.append(financial_execution_manifest_path)
    missing = [str(path) for path in required_paths if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Missing final-freeze inputs: {missing}")

    coverage_manifest = _read_json(coverage_manifest_path)
    adjudication_manifest = _read_json(adjudication_manifest_path)
    semantic_manifest = _read_json(semantic_manifest_path)
    financial_manifest = _read_json(financial_manifest_path)
    financial_execution_manifest = (
        _read_json(financial_execution_manifest_path)
        if financial_execution_manifest_path is not None
        else None
    )
    replay_manifests = [
        _read_json(path) for path in replay_manifest_paths
    ]
    replay_views_manifest = _read_json(replay_views_manifest_path)
    execution_gate = _read_json(execution_gate_path)
    additional_run_ids = [
        int(value)
        for value in coverage_manifest.get("additional_run_ids") or ()
        if int(value) > 0
    ]
    supplemental_run_ids = [
        run_id
        for run_id in additional_run_ids
        if run_id
        != int(coverage_manifest.get("direct_document_run_id") or 0)
    ]
    ocr_execution_gate = (
        _read_json(ocr_execution_gate_path)
        if supplemental_run_ids
        else None
    )
    ocr_recovery_manifest = (
        _read_json(ocr_recovery_manifest_path)
        if supplemental_run_ids
        else None
    )
    errors: list[str] = []
    raw_review_evaluation_ids = (
        coverage_manifest.get("review_evaluation_ids") or {}
    )
    review_evaluation_ids = {
        str(label): int(value)
        for label, value in raw_review_evaluation_ids.items()
        if int(value or 0) > 0
    }
    if not review_evaluation_ids:
        base_evaluation_id = int(
            coverage_manifest.get("base_review_evaluation_id") or 0
        )
        if base_evaluation_id > 0:
            review_evaluation_ids["base"] = base_evaluation_id
    run_ids_by_label = {
        "base": int(coverage_manifest.get("base_run_id") or 0),
        "delta": int(coverage_manifest.get("delta_run_id") or 0),
        "repair": int(coverage_manifest.get("repair_run_id") or 0),
        "direct": int(
            coverage_manifest.get("direct_document_run_id") or 0
        ),
    }
    run_evaluation_ids = {
        run_id: review_evaluation_ids[label]
        for label, run_id in run_ids_by_label.items()
        if run_id > 0 and review_evaluation_ids.get(label, 0) > 0
    }
    expected_source_run_ids = {
        run_id
        for run_id in run_ids_by_label.values()
        if run_id > 0
    }

    if (
        coverage_manifest.get("acceptance") != "PASS"
        or not _sealed_artifact(
            coverage_manifest,
            "ticker_metric_coverage",
            coverage_path,
        )
    ):
        errors.append("all-source coverage is not hash-sealed and passing")
    if (
        int(coverage_manifest.get("base_review_evaluation_id") or 0)
        != review_evaluation_ids.get("base", 0)
        or set(run_evaluation_ids) != expected_source_run_ids
    ):
        errors.append(
            "all-source coverage does not map every source run to an "
            "exact review evaluation"
        )
    if int(coverage_manifest.get("parser_invocations", -1)) != 0:
        errors.append("all-source coverage unexpectedly invoked the parser")
    if (
        adjudication_manifest.get("acceptance") != "PASS"
        or not _sealed_artifact(
            adjudication_manifest,
            "pair_adjudication",
            adjudication_path,
        )
        or int(
            adjudication_manifest.get("exact_confirmation_pair_count")
            or 0
        )
        != 0
        or bool(adjudication_manifest.get("policy_candidate_required"))
    ):
        errors.append(
            "all-source adjudication is not final and policy-idempotent"
        )
    semantic_input = (
        (semantic_manifest.get("inputs") or {}).get("adjudication") or {}
    )
    if (
        semantic_manifest.get("acceptance") != "PASS"
        or str(semantic_manifest.get("adjudication_prefix") or "")
        != coverage_prefix
        or str(semantic_input.get("sha256") or "")
        != file_sha256(adjudication_path)
    ):
        errors.append("semantic fixtures do not seal final adjudication")
    financial_semantic = (
        (financial_manifest.get("inputs") or {}).get(
            "semantic_freeze_manifest"
        )
        or {}
    )
    if financial_execution_manifest is None:
        if (
            financial_manifest.get("acceptance") != "PASS"
            or str(financial_semantic.get("sha256") or "")
            != file_sha256(semantic_manifest_path)
        ):
            errors.append("financial repairs do not seal final fixtures")
    else:
        financial_result_artifact = (
            financial_execution_manifest.get("artifacts") or {}
        ).get("financial_repairs") or {}
        financial_result_path = Path(
            str(financial_result_artifact.get("path") or "")
        )
        if (
            financial_manifest.get("acceptance") != "PASS"
            or financial_execution_manifest.get("acceptance")
            not in {"PASS", "PASS_WITH_EXPLICIT_LIMITATIONS"}
            or int(
                financial_execution_manifest.get("parser_invocations")
                or 0
            )
            != 0
            or int(
                financial_execution_manifest.get("network_requests")
                or 0
            )
            != 0
            or not financial_result_path.is_file()
            or str(financial_result_artifact.get("sha256") or "")
            != file_sha256(financial_result_path)
        ):
            errors.append(
                "bounded financial repair execution is not sealed and "
                "parse-free"
            )
    replay_by_run: dict[int, dict[str, Any]] = {}
    for replay_manifest in replay_manifests:
        replay_run_id = int(replay_manifest.get("base_run_id") or 0)
        if replay_run_id in replay_by_run:
            errors.append(
                f"duplicate policy replay manifest for run {replay_run_id}"
            )
        replay_by_run[replay_run_id] = replay_manifest
    replay_artifacts = replay_views_manifest.get("artifacts") or {}
    active_policy_input = (
        (replay_views_manifest.get("inputs") or {}).get(
            "active_policy"
        )
        or replay_views_manifest.get("input")
        or {}
    )
    if (
        replay_views_manifest.get("acceptance") != "PASS"
        or str(active_policy_input.get("sha256") or "")
        != file_sha256(
            Path(get_registry().review_policy_path)
            .expanduser()
            .resolve()
        )
    ):
        errors.append("run-scoped replay policy views are not sealed")
    for run_id, evaluation_id in sorted(run_evaluation_ids.items()):
        replay_manifest = replay_by_run.get(run_id) or {}
        policy_artifact = (
            replay_artifacts.get(f"run_{run_id}_policy") or {}
        )
        policy_path = Path(str(policy_artifact.get("path") or ""))
        if (
            replay_manifest.get("status") != "COMPLETED"
            or int(replay_manifest.get("evaluation_id") or 0)
            != evaluation_id
            or int(replay_manifest.get("base_run_id") or 0) != run_id
            or int(
                replay_manifest.get("source_document_open_count")
                or 0
            )
            != 0
            or int(
                replay_manifest.get("arelle_invocation_count") or 0
            )
            != 0
            or int(
                replay_manifest.get("edgartools_invocation_count")
                or 0
            )
            != 0
            or int(replay_manifest.get("ocr_invocation_count") or 0)
            != 0
            or int(
                replay_manifest.get("materialized_evidence_count")
                or 0
            )
            != 0
            or str(
                replay_manifest.get("base_scope_hash_before") or ""
            )
            != str(replay_manifest.get("base_scope_hash_after") or "")
            or not policy_path.is_file()
            or str(policy_artifact.get("sha256") or "")
            != file_sha256(policy_path)
            or str(replay_manifest.get("policy_sha256") or "")
            != str(policy_artifact.get("sha256") or "")
        ):
            errors.append(
                f"policy-only replay contract is not passing for "
                f"run {run_id} evaluation {evaluation_id}"
            )
    if set(replay_by_run) != set(run_evaluation_ids):
        errors.append(
            "policy replay manifests do not exactly match reviewed "
            "source runs"
        )
    if (
        execution_gate.get("acceptance") != "PASS"
        or int(execution_gate.get("run_id") or 0)
        != int(coverage_manifest.get("direct_document_run_id") or 0)
        or int(execution_gate.get("parser_invocations") or 0) != 1
        or int(
            execution_gate.get(
                "physical_document_reextraction_count"
            )
            or 0
        )
        != 0
    ):
        errors.append("one-shot semantic execution gate is not passing")
    if supplemental_run_ids and (
        ocr_execution_gate is None
        or ocr_recovery_manifest is None
        or ocr_execution_gate.get("acceptance") != "PASS"
        or int(ocr_execution_gate.get("run_id") or 0)
        != supplemental_run_ids[-1]
        or int(ocr_execution_gate.get("parser_invocations") or 0) != 1
        or int(
            ocr_execution_gate.get(
                "physical_document_reextraction_count"
            )
            or 0
        )
        != 0
        or ocr_recovery_manifest.get("acceptance")
        not in {"PASS", "PASS_WITH_EXPLICIT_LIMITATIONS"}
        or not bool(
            ocr_recovery_manifest.get("original_cache_unchanged")
        )
    ):
        errors.append("bounded OCR execution gate is not passing")

    with read_only_connection(
        foundation.db_path,
        timeout_sec=foundation.timeout_sec,
    ) as connection:
        evidence = merge_evidence_stats(
            *[
                load_review_evidence_stats(
                    connection,
                    evaluation_id=evaluation_id,
                )
                for _, evaluation_id in sorted(
                    run_evaluation_ids.items()
                )
            ],
        )
        golden_errors: list[str] = []
        for run_id, evaluation_id in sorted(
            run_evaluation_ids.items()
        ):
            golden_artifact = (
                replay_artifacts.get(f"run_{run_id}_golden") or {}
            )
            golden_view_path = Path(
                str(golden_artifact.get("path") or "")
            )
            golden_count = int(golden_artifact.get("row_count") or 0)
            if golden_count == 0:
                continue
            if (
                not golden_view_path.is_file()
                or str(golden_artifact.get("sha256") or "")
                != file_sha256(golden_view_path)
            ):
                golden_errors.append(
                    f"run_{run_id}: scoped golden artifact is not sealed"
                )
                continue
            golden_errors.extend(
                f"run_{run_id}:{error}"
                for error in validate_corpus(
                    connection,
                    corpus_path=golden_view_path,
                    table="sec_parser_review_evidence",
                    evaluation_id=evaluation_id,
                )
            )
    errors.extend(
        f"golden:{error}" for error in golden_errors[:20]
    )

    coverage_rows = read_csv(coverage_path)
    accepted_periods = {
        (str(row["ticker"]), str(row["metric_id"])): (
            accepted_periods_for_final_metric(
                ticker=str(row["ticker"]),
                metric_id=str(row["metric_id"]),
                source_lane=str(row["source_lane"]),
                evidence=evidence,
            )
        )
        for row in coverage_rows
        if str(row["applicability_status"]) == "APPLICABLE"
    }
    disposition_rows = build_final_metric_dispositions(
        coverage_rows=coverage_rows,
        policy_golden_validated=not golden_errors,
        accepted_periods=accepted_periods,
    )
    if len(coverage_rows) != 14_400:
        errors.append(
            f"coverage rows={len(coverage_rows)} expected=14400"
        )
    if len(disposition_rows) != 90:
        errors.append(
            f"metric dispositions={len(disposition_rows)} expected=90"
        )
    if sum(row["source_lane"] == "DP-D" for row in disposition_rows) != 7:
        errors.append("formula-derived metric count is not seven")
    if sum(row["source_lane"] == "FIN-D" for row in disposition_rows) != 6:
        errors.append("financial-derived metric count is not six")

    disposition_path = (
        output_dir / "transportation_final_metric_dispositions.csv"
    )
    manifest_path = (
        output_dir / "transportation_final_metric_freeze_manifest.json"
    )
    write_csv_atomic(
        disposition_path,
        FINAL_METRIC_DISPOSITION_FIELDS,
        disposition_rows,
    )
    disposition_counts = Counter(
        str(row["metric_disposition"]) for row in disposition_rows
    )
    candidates = [
        str(row["metric_id"])
        for row in disposition_rows
        if int(str(row["calibration_candidate"]))
    ]
    payload = {
        "acceptance": (
            "PASS" if disposition_rows and not errors else "FAIL"
        ),
        "gate": "DP6X_FINAL_ALL_SOURCE_METRIC_FREEZE",
        "freeze_version": FINAL_METRIC_FREEZE_VERSION,
        "model_family": MODEL_FAMILY,
        "asof_date": asof_date,
        "coverage_prefix": coverage_prefix,
        "base_review_evaluation_id": review_evaluation_ids.get(
            "base", 0
        ),
        "review_evaluation_ids": review_evaluation_ids,
        "reviewed_source_run_ids": sorted(run_evaluation_ids),
        "direct_document_run_id": int(
            coverage_manifest.get("direct_document_run_id") or 0
        ),
        "additional_run_ids": additional_run_ids,
        "supplemental_parser_run_ids": supplemental_run_ids,
        "final_scope_row_count": len(coverage_rows),
        "final_metric_count": len(disposition_rows),
        "formula_derived_metric_count": sum(
            row["source_lane"] == "DP-D" for row in disposition_rows
        ),
        "financial_derived_metric_count": sum(
            row["source_lane"] == "FIN-D" for row in disposition_rows
        ),
        "metric_disposition_counts": dict(
            sorted(disposition_counts.items())
        ),
        "calibration_candidate_count": len(candidates),
        "calibration_candidate_metric_ids": candidates,
        "financial_repair_classification_counts": (
            financial_manifest.get("repair_classification_counts") or {}
        ),
        "financial_repair_execution_acceptance": (
            financial_execution_manifest.get("acceptance")
            if financial_execution_manifest is not None
            else "NOT_PROVIDED"
        ),
        "financial_repair_coverage_override_counts": (
            financial_execution_manifest.get(
                "financial_coverage_override_counts"
            )
            if financial_execution_manifest is not None
            else {}
        ),
        "bounded_repair_limitations": (
            financial_execution_manifest.get("limitations") or []
            if financial_execution_manifest is not None
            else []
        ),
        "remaining_deferred_pair_count": int(
            adjudication_manifest.get("review_pair_count") or 0
        ),
        "golden_validation_error_count": len(golden_errors),
        "one_pass_semantic_parser_invocations": 1,
        "bounded_ocr_parser_invocations": len(
            supplemental_run_ids
        ),
        "post_batch_parser_invocations": len(
            supplemental_run_ids
        ),
        "additional_parser_batches_required": 0,
        "source_document_open_count": 0,
        "network_requests": 0,
        "retrieval_invocations": 0,
        "feature_build_invocations": 0,
        "historical_materialization_invocations": 0,
        "calibration_invocations": 0,
        "portfolio_invocations": 0,
        "production_promotion_authorized": False,
        "errors": errors,
        "inputs": {
            "all_source_coverage": {
                "path": str(coverage_path.resolve()),
                "sha256": file_sha256(coverage_path),
            },
            "all_source_coverage_manifest": {
                "path": str(coverage_manifest_path.resolve()),
                "sha256": file_sha256(coverage_manifest_path),
            },
            "all_source_adjudication": {
                "path": str(adjudication_path.resolve()),
                "sha256": file_sha256(adjudication_path),
            },
            "semantic_fixture_manifest": {
                "path": str(semantic_manifest_path.resolve()),
                "sha256": file_sha256(semantic_manifest_path),
            },
            "financial_repair_manifest": {
                "path": str(financial_manifest_path.resolve()),
                "sha256": file_sha256(financial_manifest_path),
            },
            **(
                {
                    "bounded_financial_repair_execution_manifest": {
                        "path": str(
                            financial_execution_manifest_path.resolve()
                        ),
                        "sha256": file_sha256(
                            financial_execution_manifest_path
                        ),
                    }
                }
                if financial_execution_manifest_path is not None
                else {}
            ),
            "policy_replay_views_manifest": {
                "path": str(replay_views_manifest_path.resolve()),
                "sha256": file_sha256(replay_views_manifest_path),
            },
            "policy_replay_manifests": [
                {
                    "path": str(path.resolve()),
                    "sha256": file_sha256(path),
                }
                for path in replay_manifest_paths
            ],
            **(
                {
                    "bounded_ocr_recovery_manifest": {
                        "path": str(
                            ocr_recovery_manifest_path.resolve()
                        ),
                        "sha256": file_sha256(
                            ocr_recovery_manifest_path
                        ),
                    },
                    "bounded_ocr_execution_gate": {
                        "path": str(
                            ocr_execution_gate_path.resolve()
                        ),
                        "sha256": file_sha256(
                            ocr_execution_gate_path
                        ),
                    },
                }
                if supplemental_run_ids
                else {}
            ),
        },
        "artifacts": {
            "final_metric_dispositions": {
                "path": str(disposition_path.resolve()),
                "row_count": len(disposition_rows),
                "sha256": file_sha256(disposition_path),
            }
        },
        "next_gate": (
            "BUILD_SELECTED_FEATURE_TABLES_AND_PIT_HISTORY_ONCE"
            if not errors
            else "REVIEW_FINAL_METRIC_FREEZE_ERRORS"
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
