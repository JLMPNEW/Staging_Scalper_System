#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.contracts import file_sha256  # noqa: E402
from dedicated_parser.policy import (  # noqa: E402
    POLICY_FIELDS,
    export_policy_golden_corpus,
    load_review_policies,
)
from industrials.core.config import (  # noqa: E402
    family_config,
    load_yaml,
    resolve_path,
)
from industrials.core.reports import (  # noqa: E402
    write_csv_atomic,
    write_text_atomic,
)
from industrials.transportation.adjudication import (  # noqa: E402
    policy_match_key,
    policy_row,
)
from industrials.transportation.dedicated_parser_adapter import (  # noqa: E402
    get_registry,
)
from industrials.transportation.parser_coverage import (  # noqa: E402
    read_csv,
    read_only_connection,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
    resolve_foundation,
)


POLICY_VERSION = "transportation_priority_semantic_fixture_v1"
REVIEWED_BY = "codex_transportation_semantic_fixture_review_v2"
STAGE_TO_MANIFEST_KEY = {
    "BASE_REVIEW_EVALUATION": "base_run_id",
    "SEC_DELTA_RUN": "delta_run_id",
    "PARSER_REPAIR_RUN": "repair_run_id",
    "ADDITIONAL_EVIDENCE_RUN_65": "direct_document_run_id",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build or apply exact evidence-key policies from the sealed "
            "transportation A/B/C semantic fixture review. Parse-free."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument(
        "--reviewed-at",
        default="2026-07-29T00:00:00-05:00",
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--decision-file",
        type=Path,
        default=Path(
            "transportation_fixture_review_evidence_decisions.csv"
        ),
    )
    parser.add_argument(
        "--pair-file",
        type=Path,
        default=Path(
            "transportation_fixture_review_pair_decisions.csv"
        ),
    )
    parser.add_argument(
        "--review-manifest",
        type=Path,
        default=Path("transportation_fixture_review_manifest.json"),
    )
    parser.add_argument(
        "--coverage-manifest",
        type=Path,
        default=Path(
            "transportation_all_source_union_coverage_manifest.json"
        ),
    )
    parser.add_argument("--policy-version", default=POLICY_VERSION)
    parser.add_argument("--reviewed-by", default=REVIEWED_BY)
    parser.add_argument(
        "--artifact-stem",
        default="transportation_fixture_review",
    )
    return parser.parse_args(argv)


def _resolve_in_output(path: Path, *, output_dir: Path) -> Path:
    return (
        path if path.is_absolute() else output_dir / path
    ).expanduser().resolve()


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected JSON object")
    return payload


def _numeric_equal(left: object, right: object) -> bool:
    try:
        left_number = float(str(left))
        right_number = float(str(right))
    except (TypeError, ValueError):
        return False
    if not math.isfinite(left_number) or not math.isfinite(right_number):
        return False
    return abs(left_number - right_number) <= max(
        1e-6,
        abs(right_number) * 1e-9,
    )


def _validate_source_row(
    decision: Mapping[str, str],
    evidence: Mapping[str, object],
) -> list[str]:
    errors: list[str] = []
    comparisons = {
        "ticker": str(evidence.get("ticker") or "").upper(),
        "source_metric_id": str(evidence.get("metric_name") or ""),
        "unit": str(evidence.get("unit") or ""),
        "period_end": str(evidence.get("period_end") or "")[:10],
        "accession_number": str(evidence.get("accession_number") or ""),
        "source_document": str(evidence.get("source_document") or ""),
    }
    for decision_field, source_value in comparisons.items():
        decision_value = str(decision.get(decision_field) or "")
        if decision_field == "ticker":
            decision_value = decision_value.upper()
        if decision_value != source_value:
            errors.append(
                f"{decision['source_stage']}:{decision['evidence_key']}: "
                f"{decision_field} changed "
                f"{decision_value!r}!={source_value!r}"
            )
    if not _numeric_equal(
        decision.get("candidate_value"),
        evidence.get("candidate_value"),
    ):
        errors.append(
            f"{decision['source_stage']}:{decision['evidence_key']}: "
            "candidate_value changed"
        )
    return errors


def _dedupe_decisions(
    rows: Sequence[Mapping[str, str]],
) -> tuple[list[dict[str, str]], list[str]]:
    output: dict[tuple[str, str], dict[str, str]] = {}
    errors: list[str] = []
    for raw in rows:
        row = dict(raw)
        key = (row["source_stage"], row["evidence_key"])
        prior = output.get(key)
        if prior is None:
            output[key] = row
            continue
        for field in (
            "semantic_decision",
            "candidate_value",
            "unit",
            "period_end",
            "source_metric_id",
            "value_override",
            "rule_id",
        ):
            # .get keeps pre-override-schema decision CSVs comparable.
            if prior.get(field, "") != row.get(field, ""):
                errors.append(
                    f"{key}: duplicate fixture decision conflicts on {field}"
                )
    return [
        output[key] for key in sorted(output)
    ], errors


def _load_source_evidence(
    *,
    connection: Any,
    decisions: Sequence[Mapping[str, str]],
    base_evaluation_id: int,
    stage_run_ids: Mapping[str, int],
) -> tuple[dict[tuple[str, str], dict[str, object]], list[str]]:
    output: dict[tuple[str, str], dict[str, object]] = {}
    errors: list[str] = []
    for stage in sorted({row["source_stage"] for row in decisions}):
        stage_rows = [
            row for row in decisions if row["source_stage"] == stage
        ]
        keys = sorted({row["evidence_key"] for row in stage_rows})
        if stage == "BASE_REVIEW_EVALUATION":
            query = (
                "SELECT * FROM sec_parser_review_evidence "
                "WHERE evaluation_id=? "
                "AND evaluated_evidence_key IN ("
                + ",".join("?" for _ in keys)
                + ")"
            )
            params: tuple[object, ...] = (
                base_evaluation_id,
                *keys,
            )
            for source in connection.execute(query, params):
                normalized = dict(source)
                key = str(normalized["evaluated_evidence_key"])
                normalized["evidence_key"] = key
                output[(stage, key)] = normalized
        else:
            run_id = stage_run_ids[stage]
            query = (
                "SELECT * FROM sec_parser_metric_evidence_shadow "
                "WHERE run_id=? AND evidence_key IN ("
                + ",".join("?" for _ in keys)
                + ")"
            )
            params = (run_id, *keys)
            for source in connection.execute(query, params):
                normalized = dict(source)
                key = str(normalized["evidence_key"])
                output[(stage, key)] = normalized
        missing = sorted(
            set(keys)
            - {
                evidence_key
                for source_stage, evidence_key in output
                if source_stage == stage
            }
        )
        if missing:
            errors.append(
                f"{stage}: missing source evidence keys={missing[:10]}"
            )
    return output, errors


def _dedupe_policy_rows(
    rows: Sequence[Mapping[str, str]],
) -> tuple[list[dict[str, str]], list[str]]:
    output: dict[tuple[str, ...], dict[str, str]] = {}
    errors: list[str] = []
    for raw in rows:
        row = dict(raw)
        key = policy_match_key(row)
        prior = output.get(key)
        if prior is not None and prior["decision"] != row["decision"]:
            errors.append(
                "exact policy match has conflicting decisions: "
                f"{row['ticker']} {row['metric_name']} "
                f"{row['period_end']}"
            )
            continue
        output.setdefault(key, row)
    return sorted(
        output.values(),
        key=lambda row: (
            row["ticker"],
            row["metric_name"],
            row["period_end"],
            row["policy_id"],
        ),
    ), errors


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    parser_cfg = family_config(config, MODEL_FAMILY)[
        "dedicated_parser"
    ]
    if bool(parser_cfg.get("parser_execution_authorized")):
        raise ValueError(
            "Fixture policy generation requires parser execution disabled"
        )
    foundation = resolve_foundation(config_path, args.db)
    output_dir = (
        resolve_path(
            parser_cfg["output_root"],
            base_dir=config_path.parent,
        )
        / str(parser_cfg["source_census_asof_date"])
    )
    artifact_stem = str(args.artifact_stem).strip()
    if (
        not artifact_stem
        or artifact_stem in {".", ".."}
        or "/" in artifact_stem
        or "\\" in artifact_stem
    ):
        raise ValueError("--artifact-stem must be a filename stem")
    decision_path = _resolve_in_output(
        args.decision_file,
        output_dir=output_dir,
    )
    pair_path = _resolve_in_output(
        args.pair_file,
        output_dir=output_dir,
    )
    review_manifest_path = _resolve_in_output(
        args.review_manifest,
        output_dir=output_dir,
    )
    coverage_manifest_path = _resolve_in_output(
        args.coverage_manifest,
        output_dir=output_dir,
    )
    for path in (
        decision_path,
        pair_path,
        review_manifest_path,
        coverage_manifest_path,
    ):
        if not path.is_file():
            raise FileNotFoundError(path)

    review_manifest = _read_json(review_manifest_path)
    coverage_manifest = _read_json(coverage_manifest_path)
    review_artifacts = review_manifest.get("artifacts") or {}
    if (
        review_manifest.get("acceptance") != "PASS"
        or str(
            (
                review_artifacts.get("evidence_decisions") or {}
            ).get("sha256")
            or ""
        )
        != file_sha256(decision_path)
        or str(
            (
                review_artifacts.get("pair_decisions") or {}
            ).get("sha256")
            or ""
        )
        != file_sha256(pair_path)
    ):
        raise ValueError("Fixture review is not sealed and passing")
    if coverage_manifest.get("acceptance") != "PASS":
        raise ValueError("All-source coverage manifest is not passing")

    eligible_rows = [
        row
        for row in read_csv(decision_path)
        if row["policy_eligible"] == "1"
        and row["semantic_decision"] in {"ACCEPT", "REJECT"}
    ]
    decisions, errors = _dedupe_decisions(eligible_rows)
    base_evaluation_id = int(
        coverage_manifest["base_review_evaluation_id"]
    )
    stage_run_ids = {
        stage: int(coverage_manifest[manifest_key])
        for stage, manifest_key in STAGE_TO_MANIFEST_KEY.items()
    }
    with read_only_connection(
        foundation.db_path,
        timeout_sec=foundation.timeout_sec,
    ) as connection:
        evaluation = connection.execute(
            """
            SELECT *
            FROM sec_parser_review_evaluation
            WHERE evaluation_id=?
            """,
            (base_evaluation_id,),
        ).fetchone()
        if (
            evaluation is None
            or str(evaluation["status"]) != "COMPLETED"
            or int(evaluation["base_run_id"])
            != stage_run_ids["BASE_REVIEW_EVALUATION"]
        ):
            raise ValueError(
                "Base review evaluation does not match sealed coverage"
            )
        source_evidence, source_errors = _load_source_evidence(
            connection=connection,
            decisions=decisions,
            base_evaluation_id=base_evaluation_id,
            stage_run_ids=stage_run_ids,
        )
    errors.extend(source_errors)

    generated_rows: list[dict[str, str]] = []
    for decision in decisions:
        key = (decision["source_stage"], decision["evidence_key"])
        evidence = source_evidence.get(key)
        if evidence is None:
            continue
        errors.extend(_validate_source_row(decision, evidence))
        policy_decision = (
            "ACCEPTED"
            if decision["semantic_decision"] == "ACCEPT"
            else "REJECTED_POLICY"
        )
        generated_rows.append(
            policy_row(
                evidence,
                decision=policy_decision,
                status_reason=(
                    "reviewed_positive_semantic_fixture:"
                    if policy_decision == "ACCEPTED"
                    else "reviewed_prohibited_semantic_fixture:"
                )
                + decision["rule_id"],
                reviewed_at=str(args.reviewed_at),
                run_id=stage_run_ids[decision["source_stage"]],
                policy_version=str(args.policy_version),
                reviewed_by=str(args.reviewed_by),
                value_override=(
                    float(decision["value_override"])
                    if str(decision.get("value_override") or "")
                    else None
                ),
            )
        )
    generated, policy_errors = _dedupe_policy_rows(generated_rows)
    errors.extend(policy_errors)

    adapter_registry = get_registry()
    registry_path = Path(
        adapter_registry.review_policy_path
    ).expanduser().resolve()
    golden_path = Path(
        adapter_registry.review_policy_golden_path
    ).expanduser().resolve()
    existing = [
        row
        for row in read_csv(registry_path)
        if row["policy_version"] != str(args.policy_version)
    ]
    existing_keys = {policy_match_key(row): row for row in existing}
    overlaps = [
        row
        for row in generated
        if policy_match_key(row) in existing_keys
    ]
    if overlaps:
        errors.append(
            "generated policies overlap existing exact policies: "
            + ", ".join(
                f"{row['ticker']}|{row['metric_name']}|{row['period_end']}"
                for row in overlaps[:10]
            )
        )
    merged = [*existing, *generated]
    candidate_registry_path = (
        output_dir / f"{artifact_stem}_policy_candidate.csv"
    )
    candidate_golden_path = (
        output_dir / f"{artifact_stem}_policy_golden_candidate.json"
    )
    manifest_path = (
        output_dir / f"{artifact_stem}_policy_manifest.json"
    )
    write_csv_atomic(
        candidate_registry_path,
        POLICY_FIELDS,
        merged,
    )
    candidate_policies = load_review_policies(candidate_registry_path)
    export_policy_golden_corpus(
        candidate_policies,
        output_path=candidate_golden_path,
        corpus_id="transportation_review_policy_generated",
    )

    expected_unique = len(
        {
            (row["source_stage"], row["evidence_key"])
            for row in eligible_rows
        }
    )
    if len(decisions) != expected_unique:
        errors.append(
            f"deduped decisions={len(decisions)} "
            f"expected_unique={expected_unique}"
        )
    if len(generated) != len(decisions):
        errors.append(
            f"generated policies={len(generated)} "
            f"decisions={len(decisions)}"
        )
    if not generated:
        errors.append("no exact fixture policies were generated")
    acceptance = "PASS" if not errors else "FAIL"
    if args.apply and acceptance != "PASS":
        raise ValueError(
            "Refusing to apply a failing fixture policy candidate: "
            + "; ".join(errors)
        )
    if args.apply:
        write_csv_atomic(registry_path, POLICY_FIELDS, merged)
        load_review_policies(registry_path)
        export_policy_golden_corpus(
            load_review_policies(registry_path),
            output_path=golden_path,
            corpus_id="transportation_review_policy_generated",
        )

    decision_counts = Counter(
        row["decision"] for row in generated
    )
    stage_counts = Counter(
        row["source_stage"] for row in decisions
    )
    accepted_pair_count = sum(
        row["pair_decision"] == "ACCEPT"
        for row in read_csv(pair_path)
    )
    payload = {
        "acceptance": acceptance,
        "gate": "DP6ZA_EXACT_PRIORITY_FIXTURE_POLICY",
        "model_family": MODEL_FAMILY,
        "policy_version": str(args.policy_version),
        "reviewed_by": str(args.reviewed_by),
        "reviewed_at": args.reviewed_at,
        "base_review_evaluation_id": base_evaluation_id,
        "stage_run_ids": stage_run_ids,
        "eligible_decision_row_count": len(eligible_rows),
        "unique_eligible_evidence_count": len(decisions),
        "generated_policy_count": len(generated),
        "generated_policy_decision_counts": dict(
            sorted(decision_counts.items())
        ),
        "generated_policy_source_stage_counts": dict(
            sorted(stage_counts.items())
        ),
        "accepted_pair_count": accepted_pair_count,
        "applied": bool(args.apply),
        "policy_registry_mutated": bool(args.apply),
        "source_document_open_count": 0,
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
            "evidence_decisions": {
                "path": str(decision_path.resolve()),
                "sha256": file_sha256(decision_path),
            },
            "pair_decisions": {
                "path": str(pair_path.resolve()),
                "sha256": file_sha256(pair_path),
            },
            "coverage_manifest": {
                "path": str(coverage_manifest_path.resolve()),
                "sha256": file_sha256(coverage_manifest_path),
            },
        },
        "artifacts": {
            "candidate_registry": {
                "path": str(candidate_registry_path.resolve()),
                "row_count": len(merged),
                "sha256": file_sha256(candidate_registry_path),
            },
            "candidate_golden": {
                "path": str(candidate_golden_path.resolve()),
                "sha256": file_sha256(candidate_golden_path),
            },
            "applied_registry": {
                "path": str(registry_path),
                "sha256": (
                    file_sha256(registry_path) if args.apply else ""
                ),
            },
            "applied_golden": {
                "path": str(golden_path),
                "sha256": (
                    file_sha256(golden_path) if args.apply else ""
                ),
            },
        },
        "next_gate": (
            "POLICY_ONLY_REPLAY_RUNS_58_59_65"
            if args.apply
            else "REVIEW_CANDIDATE_THEN_APPLY"
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
