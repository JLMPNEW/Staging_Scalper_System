#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping


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
from industrials.transportation.dedicated_parser_adapter import (  # noqa: E402
    metric_search_aliases,
)
from industrials.transportation.fixture_review import (  # noqa: E402
    EVIDENCE_DECISION_FIELDS,
    PAIR_DECISION_FIELDS,
    build_fixture_review_decisions,
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


REVIEW_VERSION = "transportation_dp6zb_candidate_unlock_review_v1"
REVIEWED_BY = "codex_transportation_candidate_unlock_review_v1"
TARGET_METRICS = (
    "capacity_growth",
    "equipment_utilization",
    "fleet_utilization",
    "fuel_surcharge_revenue_ratio",
    "passenger_load_factor",
)
EVALUATION_STAGE = {
    "base": "BASE_REVIEW_EVALUATION",
    "delta": "SEC_DELTA_RUN",
    "repair": "PARSER_REPAIR_RUN",
    "direct": "ADDITIONAL_EVIDENCE_RUN_65",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build the bounded candidate-unlock semantic review from stored "
            "transportation review evidence. No source document is opened "
            "and no parser, retrieval, feature, or calibration job is run."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument(
        "--reviewed-at",
        default="2026-07-29T00:00:00-05:00",
    )
    return parser.parse_args(argv)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected JSON object")
    return payload


def _stable_hash(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        dict(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _number(value: object) -> str:
    try:
        number = float(str(value))
    except (TypeError, ValueError):
        return ""
    return f"{number:.12g}" if math.isfinite(number) else ""


def _resolve_acceptance_conflicts(
    evidence_rows: list[dict[str, object]],
    pair_rows: list[dict[str, object]],
) -> tuple[int, int]:
    accepted: dict[
        tuple[str, str, str], list[dict[str, object]]
    ] = defaultdict(list)
    for row in evidence_rows:
        if row["semantic_decision"] == "ACCEPT":
            accepted[
                (
                    str(row["ticker"]),
                    str(row["metric_id"]),
                    str(row["period_end"]),
                )
            ].append(row)
    conflict_keys = {
        key
        for key, rows in accepted.items()
        if len(
            {
                _number(
                    row.get("value_override")
                    or row["candidate_value"]
                )
                for row in rows
            }
        )
        > 1
    }
    for key in conflict_keys:
        for row in accepted[key]:
            row["semantic_decision"] = "DEFER"
            row["fixture_polarity"] = "UNRESOLVED"
            row["rule_id"] = "conflicting_same_period_values"
            row["decision_reason"] = (
                "multiple_distinct_values_share_the_same_ticker_metric_period"
            )
            row["policy_eligible"] = 0

    by_pair: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in evidence_rows:
        by_pair[str(row["pair_key"])].append(row)
    accepted_pairs = 0
    for pair in pair_rows:
        rows = [
            row
            for row in by_pair[str(pair["pair_key"])]
            if bool(str(row.get("candidate_value") or ""))
        ]
        accepted_rows = [
            row for row in rows if row["semantic_decision"] == "ACCEPT"
        ]
        rejected_rows = [
            row for row in rows if row["semantic_decision"] == "REJECT"
        ]
        deferred_rows = [
            row for row in rows if row["semantic_decision"] == "DEFER"
        ]
        if accepted_rows:
            pair["pair_decision"] = "ACCEPT"
            pair["decision_reason"] = (
                "at_least_one_positive_exact_semantic_fixture"
            )
            accepted_pairs += 1
        elif deferred_rows:
            pair["pair_decision"] = "DEFER"
            pair["decision_reason"] = (
                "semantic_fixture_requires_manual_resolution"
            )
        elif rejected_rows:
            pair["pair_decision"] = "REJECT"
            pair["decision_reason"] = (
                "all_numeric_fixture_candidates_prohibited"
            )
        else:
            pair["pair_decision"] = "DEFER"
            pair["decision_reason"] = "no_numeric_fixture"
        pair["accepted_evidence_count"] = len(accepted_rows)
        pair["rejected_evidence_count"] = len(rejected_rows)
        pair["deferred_evidence_count"] = len(deferred_rows)
        pair["accepted_evidence_keys"] = "|".join(
            str(row["evidence_key"]) for row in accepted_rows
        )
        pair["rejected_evidence_keys"] = "|".join(
            str(row["evidence_key"]) for row in rejected_rows
        )
        pair["deferred_evidence_keys"] = "|".join(
            str(row["evidence_key"]) for row in deferred_rows
        )
        pair["policy_eligible_evidence_count"] = sum(
            int(str(row["policy_eligible"])) for row in rows
        )
    return len(conflict_keys), accepted_pairs


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    parser_cfg = family_config(config, MODEL_FAMILY)["dedicated_parser"]
    if bool(parser_cfg.get("parser_execution_authorized")):
        raise ValueError(
            "Candidate-unlock review requires parser execution disabled"
        )
    foundation = resolve_foundation(config_path, args.db)
    output_dir = (
        resolve_path(parser_cfg["output_root"], base_dir=config_path.parent)
        / str(parser_cfg["source_census_asof_date"])
    )
    freeze_path = (
        output_dir / "transportation_final_metric_freeze_manifest.json"
    )
    freeze = _read_json(freeze_path)
    if (
        freeze.get("acceptance") != "PASS"
        or freeze.get("coverage_prefix")
        != "transportation_fixture_bounded_union"
    ):
        raise ValueError("Current final metric freeze is not the fixture union")
    coverage_reference = freeze["inputs"]["all_source_coverage"]
    coverage_path = Path(str(coverage_reference["path"])).resolve()
    coverage_manifest_reference = freeze["inputs"][
        "all_source_coverage_manifest"
    ]
    coverage_manifest_path = Path(
        str(coverage_manifest_reference["path"])
    ).resolve()
    if (
        file_sha256(coverage_path)
        != str(coverage_reference["sha256"])
        or file_sha256(coverage_manifest_path)
        != str(coverage_manifest_reference["sha256"])
    ):
        raise ValueError("Frozen candidate-unlock coverage input changed")
    coverage_rows = read_csv(coverage_path)
    depth_only_metrics = {
        "capacity_growth",
        "passenger_load_factor",
    }
    selected_pairs = {
        (row["ticker"], row["metric_id"]): row
        for row in coverage_rows
        if row["metric_id"] in TARGET_METRICS
        and row["universe_role"] == "active"
        and row["applicability_status"] == "APPLICABLE"
        and row["coverage_status"]
        in {"COVERED_ACCEPTED", "COVERED_REVIEW_REQUIRED"}
        and (
            row["metric_id"] not in depth_only_metrics
            or row["coverage_status"] == "COVERED_ACCEPTED"
        )
    }
    evaluation_ids = {
        label: int(value)
        for label, value in freeze["review_evaluation_ids"].items()
    }
    evaluation_to_stage = {
        evaluation_id: EVALUATION_STAGE[label]
        for label, evaluation_id in evaluation_ids.items()
    }
    placeholders = ",".join("?" for _ in evaluation_to_stage)
    metric_placeholders = ",".join("?" for _ in TARGET_METRICS)
    errors: list[str] = []
    source_rows: list[dict[str, object]] = []
    with read_only_connection(
        foundation.db_path,
        timeout_sec=foundation.timeout_sec,
    ) as connection:
        evaluations = {
            int(row["evaluation_id"]): dict(row)
            for row in connection.execute(
                f"""
                SELECT evaluation_id, base_run_id, status, model_family
                FROM sec_parser_review_evaluation
                WHERE evaluation_id IN ({placeholders})
                """,
                sorted(evaluation_to_stage),
            )
        }
        if set(evaluations) != set(evaluation_to_stage):
            errors.append("not all frozen review evaluations exist")
        invalid = [
            evaluation_id
            for evaluation_id, row in evaluations.items()
            if row["status"] != "COMPLETED"
            or row["model_family"] != MODEL_FAMILY
        ]
        if invalid:
            errors.append(f"invalid review evaluations={sorted(invalid)}")
        query_rows = connection.execute(
            f"""
            SELECT evaluation_id, evaluated_evidence_key,
                   base_evidence_key, ticker, metric_name,
                   candidate_status, candidate_value, unit, period_end,
                   scope, confidence, accession_number, form_type,
                   filing_date, source_document, extraction_method,
                   status_reason, evidence_text
            FROM sec_parser_review_evidence
            WHERE evaluation_id IN ({placeholders})
              AND metric_name IN ({metric_placeholders})
              AND candidate_status='REVIEW_REQUIRED'
              AND candidate_value IS NOT NULL
              AND COALESCE(period_end, '') != ''
            ORDER BY ticker, metric_name, period_end,
                     evaluated_evidence_key
            """,
            (*sorted(evaluation_to_stage), *TARGET_METRICS),
        )
        for source in query_rows:
            row = dict(source)
            pair_key = (
                str(row["ticker"]).upper(),
                str(row["metric_name"]),
            )
            if pair_key not in selected_pairs:
                continue
            evaluation_id = int(row["evaluation_id"])
            stage = evaluation_to_stage[evaluation_id]
            evidence_key = (
                str(row["evaluated_evidence_key"])
                if stage == "BASE_REVIEW_EVALUATION"
                else str(row["base_evidence_key"] or "")
            )
            if not evidence_key:
                errors.append(
                    f"{evaluation_id}:{pair_key}: blank base evidence key"
                )
                continue
            normalized = {
                "priority_version": REVIEW_VERSION,
                "review_order": 0,
                "phase_rank": 1,
                "review_phase": "BOUNDED_CANDIDATE_UNLOCK",
                "pair_key": "|".join(pair_key),
                "fixture_id": "unlock_"
                + hashlib.sha256(
                    "|".join(pair_key).encode("utf-8")
                ).hexdigest()[:20],
                "ticker": pair_key[0],
                "metric_id": pair_key[1],
                "source_lane": selected_pairs[pair_key]["source_lane"],
                "source_metric_id": pair_key[1],
                "evidence_key": evidence_key,
                "candidate_status": row["candidate_status"],
                # Stringify so a legitimate 0.0 stays truthy for the
                # downstream `bool(str(value))` numericity test.
                "candidate_value": (
                    ""
                    if row["candidate_value"] is None
                    else str(row["candidate_value"])
                ),
                "unit": row["unit"],
                "period_end": str(row["period_end"])[:10],
                "scope": row["scope"],
                "confidence": row["confidence"],
                "source_stage": stage,
                "accession_number": row["accession_number"],
                "form_type": row["form_type"],
                "filing_date": row["filing_date"],
                "source_document": row["source_document"],
                "extraction_method": row["extraction_method"],
                "status_reason": row["status_reason"],
                "evidence_text": row["evidence_text"],
            }
            normalized["evidence_row_sha256"] = _stable_hash(normalized)
            source_rows.append(normalized)
    pair_source_rows: list[dict[str, object]] = []
    evidence_by_pair: Counter[str] = Counter(
        str(row["pair_key"]) for row in source_rows
    )
    for review_order, (key, coverage) in enumerate(
        sorted(selected_pairs.items()),
        start=1,
    ):
        pair_key = "|".join(key)
        if not evidence_by_pair[pair_key]:
            continue
        pair_source_rows.append(
            {
                "review_order": review_order,
                "phase_rank": 1,
                "review_phase": "BOUNDED_CANDIDATE_UNLOCK",
                "pair_key": pair_key,
                "fixture_id": "unlock_"
                + hashlib.sha256(pair_key.encode("utf-8")).hexdigest()[:20],
                "ticker": key[0],
                "metric_id": key[1],
                "source_lane": coverage["source_lane"],
                "review_route": "REVIEW_DIRECT_SEMANTIC_FIXTURE",
            }
        )
    review_order = {
        str(row["pair_key"]): int(str(row["review_order"]))
        for row in pair_source_rows
    }
    for row in source_rows:
        row["review_order"] = review_order[str(row["pair_key"])]

    pair_rows, evidence_rows, _, review_errors = (
        build_fixture_review_decisions(
            pair_rows=pair_source_rows,
            evidence_rows=source_rows,
            aliases=metric_search_aliases(),
            reviewed_at=str(args.reviewed_at),
            review_version=REVIEW_VERSION,
            reviewed_by=REVIEWED_BY,
        )
    )
    errors.extend(review_errors)
    conflict_count, accepted_pair_count = _resolve_acceptance_conflicts(
        evidence_rows,
        pair_rows,
    )
    policy_rows = [
        row
        for row in evidence_rows
        if int(str(row["policy_eligible"])) == 1
        and row["semantic_decision"] in {"ACCEPT", "REJECT"}
    ]
    accepted_rows = [
        row
        for row in policy_rows
        if row["semantic_decision"] == "ACCEPT"
    ]
    if not pair_rows:
        errors.append("candidate-unlock pair scope is empty")
    if not accepted_rows:
        errors.append("candidate-unlock review produced no exact acceptances")
    duplicate_source_keys = len(source_rows) - len(
        {
            (str(row["source_stage"]), str(row["evidence_key"]))
            for row in source_rows
        }
    )
    if duplicate_source_keys:
        errors.append(
            f"duplicate source-stage evidence keys={duplicate_source_keys}"
        )

    pair_output_path = (
        output_dir / "transportation_candidate_unlock_pair_decisions.csv"
    )
    evidence_output_path = (
        output_dir
        / "transportation_candidate_unlock_evidence_decisions.csv"
    )
    manifest_path = (
        output_dir / "transportation_candidate_unlock_review_manifest.json"
    )
    write_csv_atomic(pair_output_path, PAIR_DECISION_FIELDS, pair_rows)
    write_csv_atomic(
        evidence_output_path,
        EVIDENCE_DECISION_FIELDS,
        evidence_rows,
    )
    metric_accept_counts = Counter(
        str(row["metric_id"]) for row in accepted_rows
    )
    metric_pair_counts = Counter(
        str(row["metric_id"])
        for row in pair_rows
        if row["pair_decision"] == "ACCEPT"
    )
    payload = {
        "acceptance": "PASS" if not errors else "FAIL",
        "gate": "DP6ZB_BOUNDED_CANDIDATE_UNLOCK_REVIEW",
        "review_version": REVIEW_VERSION,
        "model_family": MODEL_FAMILY,
        "reviewed_by": REVIEWED_BY,
        "reviewed_at": args.reviewed_at,
        "target_metric_ids": list(TARGET_METRICS),
        "target_pair_count": len(pair_rows),
        "reviewed_evidence_row_count": len(evidence_rows),
        "same_period_conflict_count": conflict_count,
        "accepted_pair_count": accepted_pair_count,
        "policy_eligible_evidence_count": len(policy_rows),
        "accepted_evidence_count": len(accepted_rows),
        "accepted_evidence_count_by_metric": dict(
            sorted(metric_accept_counts.items())
        ),
        "accepted_pair_count_by_metric": dict(
            sorted(metric_pair_counts.items())
        ),
        "pair_decision_counts": dict(
            sorted(
                Counter(
                    str(row["pair_decision"]) for row in pair_rows
                ).items()
            )
        ),
        "evidence_decision_counts": dict(
            sorted(
                Counter(
                    str(row["semantic_decision"])
                    for row in evidence_rows
                ).items()
            )
        ),
        "review_evaluation_ids": evaluation_ids,
        "source_document_open_count": 0,
        "network_requests": 0,
        "retrieval_invocations": 0,
        "parser_invocations": 0,
        "feature_build_invocations": 0,
        "historical_materialization_invocations": 0,
        "calibration_invocations": 0,
        "portfolio_invocations": 0,
        "policy_registry_mutated": False,
        "production_promotion_authorized": False,
        "errors": errors,
        "inputs": {
            "final_freeze": {
                "path": str(freeze_path.resolve()),
                "sha256": file_sha256(freeze_path),
            },
            "coverage": {
                "path": str(coverage_path),
                "sha256": file_sha256(coverage_path),
            },
            "coverage_manifest": {
                "path": str(coverage_manifest_path),
                "sha256": file_sha256(coverage_manifest_path),
            },
        },
        "artifacts": {
            "pair_decisions": {
                "path": str(pair_output_path.resolve()),
                "row_count": len(pair_rows),
                "sha256": file_sha256(pair_output_path),
            },
            "evidence_decisions": {
                "path": str(evidence_output_path.resolve()),
                "row_count": len(evidence_rows),
                "sha256": file_sha256(evidence_output_path),
            },
        },
        "next_gate": (
            "BUILD_AND_APPLY_EXACT_UNLOCK_POLICIES"
            if not errors
            else "STOP_REVIEW_UNLOCK_ERRORS"
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
