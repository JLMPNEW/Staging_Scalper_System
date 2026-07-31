#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
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
from industrials.transportation.accepted_conflicts import (  # noqa: E402
    ACCEPTED_CONFLICT_RESOLUTIONS,
    CONFLICT_RESOLUTION_VERSION,
    numeric_equal,
    replace_exact_policy,
)
from industrials.transportation.adjudication import policy_row  # noqa: E402
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


REVIEWED_BY = "codex_transportation_accepted_conflict_review_v1"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Resolve the sealed same-ticker/metric/period accepted-value "
            "conflicts using exact cached-filing scope decisions. Parse-free."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument(
        "--evaluation-ids",
        default="5,7,8,9",
        help="Comma-delimited completed review evaluation IDs to audit.",
    )
    parser.add_argument(
        "--reviewed-at",
        default="2026-07-29T18:00:00-05:00",
    )
    parser.add_argument(
        "--artifact-stem",
        default="transportation_accepted_conflict_resolution",
    )
    parser.add_argument(
        "--base-registry",
        type=Path,
        default=Path(
            "transportation_candidate_unlock_policy_candidate.csv"
        ),
        help=(
            "Sealed pre-correction registry whose row order must be "
            "preserved. Relative paths resolve in the parser output "
            "directory."
        ),
    )
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args(argv)


def _json_object(value: object, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(str(value or "{}"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label}: invalid JSON object") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label}: expected JSON object")
    return payload


def _accepted_rows(
    connection: Any,
    *,
    evaluation_ids: list[int],
) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in evaluation_ids)
    return [
        dict(row)
        for row in connection.execute(
            f"""
            SELECT *
            FROM sec_parser_review_evidence
            WHERE evaluation_id IN ({placeholders})
              AND candidate_status='ACCEPTED'
            ORDER BY ticker, metric_name, period_end,
                     candidate_value, evaluated_evidence_key
            """,
            evaluation_ids,
        )
    ]


def _conflict_groups(
    rows: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        period_end = str(row.get("period_end") or "")[:10]
        if not period_end:
            continue
        key = (
            str(row.get("ticker") or "").upper(),
            str(row.get("metric_name") or ""),
            period_end,
        )
        groups.setdefault(key, []).append(dict(row))
    return {
        key: values
        for key, values in groups.items()
        if len(
            {
                round(float(row["candidate_value"]), 10)
                for row in values
                if row.get("candidate_value") is not None
            }
        )
        > 1
    }


def _base_rows(
    connection: Any,
    *,
    evidence_keys: list[str],
) -> dict[str, dict[str, Any]]:
    placeholders = ",".join("?" for _ in evidence_keys)
    return {
        str(row["evidence_key"]): dict(row)
        for row in connection.execute(
            f"""
            SELECT *
            FROM sec_parser_metric_evidence_shadow
            WHERE evidence_key IN ({placeholders})
            """,
            evidence_keys,
        )
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    parser_cfg = family_config(config, MODEL_FAMILY)[
        "dedicated_parser"
    ]
    if bool(parser_cfg.get("parser_execution_authorized")):
        raise ValueError(
            "Accepted-conflict resolution requires parser execution disabled"
        )
    evaluation_ids = sorted(
        {
            int(value.strip())
            for value in str(args.evaluation_ids).split(",")
            if value.strip()
        }
    )
    if not evaluation_ids:
        raise ValueError("--evaluation-ids cannot be empty")
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

    expected_keys = {
        (
            item.ticker,
            item.metric_name,
            item.period_end,
        )
        for item in ACCEPTED_CONFLICT_RESOLUTIONS
    }
    evidence_keys = sorted(
        {
            key
            for item in ACCEPTED_CONFLICT_RESOLUTIONS
            for key in (
                item.winner_base_evidence_key,
                item.loser_base_evidence_key,
            )
        }
    )
    errors: list[str] = []
    with read_only_connection(
        foundation.db_path,
        timeout_sec=foundation.timeout_sec,
    ) as connection:
        evaluations = {
            int(row["evaluation_id"]): dict(row)
            for row in connection.execute(
                (
                    "SELECT * FROM sec_parser_review_evaluation "
                    "WHERE evaluation_id IN ("
                    + ",".join("?" for _ in evaluation_ids)
                    + ")"
                ),
                evaluation_ids,
            )
        }
        incomplete = [
            evaluation_id
            for evaluation_id in evaluation_ids
            if evaluation_id not in evaluations
            or str(evaluations[evaluation_id]["status"]) != "COMPLETED"
        ]
        if incomplete:
            errors.append(
                f"review evaluations are missing or incomplete: {incomplete}"
            )
        accepted = _accepted_rows(
            connection,
            evaluation_ids=evaluation_ids,
        )
        conflicts = _conflict_groups(accepted)
        base_rows = _base_rows(
            connection,
            evidence_keys=evidence_keys,
        )

    actual_keys = set(conflicts)
    if actual_keys != expected_keys:
        errors.append(
            "accepted conflict set changed: "
            f"actual={sorted(actual_keys)} expected={sorted(expected_keys)}"
        )

    registry = get_registry()
    registry_path = Path(
        registry.review_policy_path
    ).expanduser().resolve()
    golden_path = Path(
        registry.review_policy_golden_path
    ).expanduser().resolve()
    current_active_rows = read_csv(registry_path)
    base_registry_path = (
        (
            args.base_registry
            if args.base_registry.is_absolute()
            else output_dir / args.base_registry
        ).expanduser().resolve()
        if args.base_registry is not None
        else registry_path
    )
    if not base_registry_path.is_file():
        raise FileNotFoundError(base_registry_path)
    active_rows = read_csv(base_registry_path)
    candidate_rows = list(active_rows)
    resolution_rows: list[dict[str, Any]] = []
    replaced_policy_ids: list[str] = []
    replacement_policy_ids: list[str] = []

    for item in ACCEPTED_CONFLICT_RESOLUTIONS:
        key = (item.ticker, item.metric_name, item.period_end)
        group = conflicts.get(key, [])
        by_base_key = {
            str(row.get("base_evidence_key") or ""): row
            for row in group
        }
        winner = by_base_key.get(item.winner_base_evidence_key)
        loser = by_base_key.get(item.loser_base_evidence_key)
        base_loser = base_rows.get(item.loser_base_evidence_key)
        if winner is None or loser is None or base_loser is None:
            errors.append(f"{key}: expected winner/loser evidence is missing")
            continue
        provenance = _json_object(
            base_loser.get("provenance_json"),
            label=f"{key}:loser provenance",
        )
        validations = (
            (
                numeric_equal(
                    winner.get("candidate_value"),
                    item.winner_value,
                ),
                "winner value changed",
            ),
            (
                numeric_equal(
                    loser.get("candidate_value"),
                    item.loser_value,
                ),
                "loser value changed",
            ),
            (
                str(winner.get("scope") or "") == item.winner_scope,
                "winner scope changed",
            ),
            (
                str(loser.get("source_document") or "")
                == item.source_document,
                "source document changed",
            ),
            (
                str(provenance.get("document_sha256") or "")
                == item.document_sha256,
                "source document hash changed",
            ),
        )
        failed = [message for passed, message in validations if not passed]
        if failed:
            errors.extend(f"{key}: {message}" for message in failed)
            continue
        losing_policy_id = str(loser.get("policy_id") or "")
        replacement = policy_row(
            base_loser,
            decision="SUPPRESSED_SEMANTIC_DUPLICATE",
            status_reason=(
                "accepted_conflict_resolution:" + item.loser_reason
            ),
            reviewed_at=str(args.reviewed_at),
            run_id=int(base_loser["run_id"]) + 1,
            policy_version=CONFLICT_RESOLUTION_VERSION,
            reviewed_by=REVIEWED_BY,
        )
        try:
            candidate_rows = replace_exact_policy(
                candidate_rows,
                policy_id=losing_policy_id,
                replacement=replacement,
            )
        except ValueError as exc:
            errors.append(str(exc))
            continue
        replaced_policy_ids.append(losing_policy_id)
        replacement_policy_ids.append(replacement["policy_id"])
        resolution_rows.append(
            {
                "ticker": item.ticker,
                "metric_name": item.metric_name,
                "period_end": item.period_end,
                "winner_base_evidence_key": item.winner_base_evidence_key,
                "winner_value": item.winner_value,
                "winner_scope": item.winner_scope,
                "loser_base_evidence_key": item.loser_base_evidence_key,
                "loser_value": item.loser_value,
                "loser_prior_policy_id": losing_policy_id,
                "replacement_policy_id": replacement["policy_id"],
                "replacement_decision": replacement["decision"],
                "resolution_reason": item.loser_reason,
                "source_document": item.source_document,
                "document_sha256": item.document_sha256,
            }
        )

    if len(candidate_rows) != len(active_rows):
        errors.append(
            f"registry row count changed {len(active_rows)}"
            f"->{len(candidate_rows)}"
        )
    if len(resolution_rows) != len(ACCEPTED_CONFLICT_RESOLUTIONS):
        errors.append(
            f"resolved conflicts={len(resolution_rows)} "
            f"expected={len(ACCEPTED_CONFLICT_RESOLUTIONS)}"
        )
    if len(set(replaced_policy_ids)) != len(replaced_policy_ids):
        errors.append("a prior policy was replaced more than once")

    candidate_registry_path = (
        output_dir / f"{artifact_stem}_policy_candidate.csv"
    )
    candidate_golden_path = (
        output_dir / f"{artifact_stem}_policy_golden_candidate.json"
    )
    resolution_path = (
        output_dir / f"{artifact_stem}_decisions.json"
    )
    manifest_path = output_dir / f"{artifact_stem}_manifest.json"
    write_csv_atomic(
        candidate_registry_path,
        POLICY_FIELDS,
        candidate_rows,
    )
    candidate_policies = load_review_policies(candidate_registry_path)
    export_policy_golden_corpus(
        candidate_policies,
        output_path=candidate_golden_path,
        corpus_id="transportation_review_policy_generated",
    )
    write_text_atomic(
        resolution_path,
        json.dumps(
            {
                "resolution_version": CONFLICT_RESOLUTION_VERSION,
                "reviewed_by": REVIEWED_BY,
                "reviewed_at": args.reviewed_at,
                "evaluation_ids": evaluation_ids,
                "accepted_conflict_count": len(conflicts),
                "resolutions": resolution_rows,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )

    acceptance = "PASS" if not errors else "FAIL"
    if args.apply and errors:
        raise ValueError(
            "Refusing to apply failing accepted-conflict resolution: "
            + "; ".join(errors)
        )
    if args.apply:
        write_csv_atomic(
            registry_path,
            POLICY_FIELDS,
            candidate_rows,
        )
        applied_policies = load_review_policies(registry_path)
        export_policy_golden_corpus(
            applied_policies,
            output_path=golden_path,
            corpus_id="transportation_review_policy_generated",
        )

    decision_counts = Counter(
        row["replacement_decision"] for row in resolution_rows
    )
    payload = {
        "acceptance": acceptance,
        "gate": "DP6ZB_ACCEPTED_VALUE_CONFLICT_RESOLUTION",
        "model_family": MODEL_FAMILY,
        "resolution_version": CONFLICT_RESOLUTION_VERSION,
        "reviewed_by": REVIEWED_BY,
        "reviewed_at": args.reviewed_at,
        "evaluation_ids": evaluation_ids,
        "accepted_conflict_count": len(conflicts),
        "resolved_conflict_count": len(resolution_rows),
        "replacement_decision_counts": dict(sorted(decision_counts.items())),
        "active_registry_row_count_before": len(active_rows),
        "current_active_registry_row_count": len(current_active_rows),
        "candidate_registry_row_count": len(candidate_rows),
        "replaced_policy_ids": sorted(replaced_policy_ids),
        "replacement_policy_ids": sorted(replacement_policy_ids),
        "applied": bool(args.apply),
        "policy_registry_mutated": bool(args.apply),
        "cached_source_documents_inspected": 2,
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
            "base_registry": {
                "path": str(base_registry_path),
                "sha256": file_sha256(base_registry_path),
            },
        },
        "artifacts": {
            "resolution_decisions": {
                "path": str(resolution_path.resolve()),
                "row_count": len(resolution_rows),
                "sha256": file_sha256(resolution_path),
            },
            "candidate_registry": {
                "path": str(candidate_registry_path.resolve()),
                "row_count": len(candidate_rows),
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
            "REBUILD_RUN_SCOPED_POLICY_VIEWS"
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
