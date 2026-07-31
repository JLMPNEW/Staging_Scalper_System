#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


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
    ADJUDICATION_FIELDS,
    POLICY_VERSION,
    REVIEWED_BY,
    accepted_final_metric,
    build_legacy_index,
    confirmation_basis,
    legacy_metric_ids,
    lockable_rejection,
    policy_match_key,
    policy_row,
)
from industrials.transportation.dedicated_parser_adapter import (  # noqa: E402
    get_registry,
)
from industrials.transportation.parser_coverage import (  # noqa: E402
    PARSER_DERIVATIONS,
    read_only_connection,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
    resolve_foundation,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Conservatively adjudicate transportation DP6 priority-1/2 "
            "evidence using exact prior accepted-disclosure confirmation."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--run-id", type=int, default=58)
    parser.add_argument("--max-review-priority", type=int, default=2)
    parser.add_argument("--reviewed-at", required=True)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args(argv)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [
            {
                str(key): str(value or "").strip()
                for key, value in row.items()
            }
            for row in csv.DictReader(handle)
        ]


def _source_metrics(
    *,
    metric_id: str,
    source_lane: str,
) -> tuple[str, ...]:
    if source_lane == "DP":
        return (metric_id,)
    if source_lane == "DP-D":
        return tuple(
            str(value)
            for value in PARSER_DERIVATIONS[metric_id]["dependencies"]
        )
    return ()


def _load_base_evidence(
    connection: Any,
    *,
    run_id: int,
    source_pairs: set[tuple[str, str]],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    output: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(
        list
    )
    for row in connection.execute(
        """
        SELECT evidence.*
        FROM sec_parser_run_metric_evidence AS relation
        JOIN sec_parser_metric_evidence_shadow AS evidence
          ON evidence.evidence_key=relation.evidence_key
        WHERE relation.run_id=?
          AND evidence.model_family=?
        ORDER BY evidence.ticker, evidence.metric_name,
                 evidence.period_end, evidence.evidence_key
        """,
        (run_id, MODEL_FAMILY),
    ):
        key = (str(row["ticker"]), str(row["metric_name"]))
        if key in source_pairs:
            output[key].append(dict(row))
    return dict(output)


def _load_legacy(connection: Any) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            """
            SELECT *
            FROM fact_sec_metric_disclosure_candidate
            WHERE model_family=?
              AND candidate_status='ACCEPTED'
            ORDER BY ticker, metric_name, period_end, candidate_key
            """,
            (MODEL_FAMILY,),
        )
    ]


def _dedupe_policies(
    rows: list[dict[str, str]],
) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    seen: set[tuple[str, ...]] = set()
    for row in sorted(
        rows,
        key=lambda item: (
            item["decision"] != "ACCEPTED",
            item["ticker"],
            item["metric_name"],
            item["period_end"],
            item["policy_id"],
        ),
    ):
        key = policy_match_key(row)
        if key in seen:
            continue
        seen.add(key)
        output.append(row)
    return output


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.max_review_priority != 2:
        raise ValueError(
            "This sealed batch requires --max-review-priority=2"
        )
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    family = family_config(config, MODEL_FAMILY)
    parser_cfg = family["dedicated_parser"]
    if bool(parser_cfg.get("parser_execution_authorized")):
        raise ValueError(
            "Adjudication requires parser_execution_authorized=false"
        )
    foundation = resolve_foundation(config_path, args.db)
    base_dir = config_path.parent
    asof_date = str(parser_cfg["source_census_asof_date"])
    output_dir = (
        resolve_path(parser_cfg["output_root"], base_dir=base_dir)
        / asof_date
    )
    queue_path = (
        output_dir / "transportation_coverage_lift_review_queue.csv"
    )
    lift_manifest_path = (
        output_dir / "transportation_coverage_lift_manifest.json"
    )
    adapter_registry = get_registry()
    registry_path = Path(
        adapter_registry.review_policy_path
    ).expanduser().resolve()
    golden_path = Path(
        adapter_registry.review_policy_golden_path
    ).expanduser().resolve()
    for path in (queue_path, lift_manifest_path, registry_path, golden_path):
        if not path.is_file():
            raise FileNotFoundError(path)
    lift_manifest = json.loads(
        lift_manifest_path.read_text(encoding="utf-8")
    )
    expected_queue_hash = str(
        lift_manifest["artifacts"]["review_queue"]["sha256"]
    )
    if (
        lift_manifest.get("acceptance") != "PASS"
        or int(lift_manifest.get("run_id") or 0) != args.run_id
        or file_sha256(queue_path) != expected_queue_hash
    ):
        raise ValueError("DP6A queue is not the sealed run-58 artifact")
    queue = [
        row
        for row in _read_csv(queue_path)
        if int(row["review_priority"]) <= args.max_review_priority
    ]
    priority_counts = Counter(row["review_priority"] for row in queue)
    if priority_counts != Counter({"1": 551, "2": 59}):
        raise ValueError(
            f"Unexpected priority-1/2 population: {priority_counts}"
        )
    source_pairs = {
        (row["ticker"], source_metric)
        for row in queue
        for source_metric in _source_metrics(
            metric_id=row["metric_id"],
            source_lane=row["source_lane"],
        )
    }
    with read_only_connection(
        foundation.db_path,
        timeout_sec=foundation.timeout_sec,
    ) as connection:
        run = connection.execute(
            """
            SELECT *
            FROM sec_parser_run
            WHERE run_id=?
            """,
            (args.run_id,),
        ).fetchone()
        if (
            run is None
            or str(run["model_family"]) != MODEL_FAMILY
            or str(run["status"]) != "COMPLETED"
            or int(run["failed_work_count"] or 0) != 0
        ):
            raise ValueError("Canonical parser run is not complete")
        evidence = _load_base_evidence(
            connection,
            run_id=args.run_id,
            source_pairs=source_pairs,
        )
        legacy_index = build_legacy_index(_load_legacy(connection))

    policies: list[dict[str, str]] = []
    adjudication: list[dict[str, object]] = []
    accepted_evidence_keys: set[str] = set()
    rejection_evidence_keys: set[str] = set()
    rejection_contracts: set[tuple[str, str]] = set()
    for pair in queue:
        source_metrics = _source_metrics(
            metric_id=pair["metric_id"],
            source_lane=pair["source_lane"],
        )
        pair_evidence = [
            item
            for source_metric in source_metrics
            for item in evidence.get(
                (pair["ticker"], source_metric),
                (),
            )
        ]
        confirmed: list[dict[str, Any]] = []
        bases: set[str] = set()
        for item in pair_evidence:
            basis = confirmation_basis(
                item,
                final_metric_id=pair["metric_id"],
                legacy_index=legacy_index,
            )
            if not basis:
                continue
            confirmed.append(item)
            bases.add(basis)
            evidence_key = str(item["evidence_key"])
            if evidence_key not in accepted_evidence_keys:
                accepted_evidence_keys.add(evidence_key)
                policies.append(
                    policy_row(
                        item,
                        decision="ACCEPTED",
                        status_reason=(
                            "exact_prior_accepted_disclosure_confirmation"
                        ),
                        reviewed_at=args.reviewed_at,
                    )
                )
        rejections = [
            item for item in pair_evidence if lockable_rejection(item)
        ]
        pair_locked_rejections: list[dict[str, Any]] = []
        for item in rejections:
            rejection_contract = (
                str(item["metric_name"]),
                str(item["status_reason"]),
            )
            if rejection_contract in rejection_contracts:
                continue
            evidence_key = str(item["evidence_key"])
            if evidence_key in rejection_evidence_keys:
                continue
            rejection_contracts.add(rejection_contract)
            rejection_evidence_keys.add(evidence_key)
            pair_locked_rejections.append(item)
            policies.append(
                policy_row(
                    item,
                    decision="REJECTED_POLICY",
                    status_reason=(
                        "confirmed_frozen_contract_rejection:"
                        + str(item["status_reason"])
                    ),
                    reviewed_at=args.reviewed_at,
                )
            )
        accepted = accepted_final_metric(
            final_metric_id=pair["metric_id"],
            source_lane=pair["source_lane"],
            confirmed_evidence=confirmed,
        )
        ambiguous_count = sum(
            str(item.get("candidate_status") or "")
            == "REVIEW_REQUIRED"
            and str(item.get("evidence_key") or "")
            not in {
                str(row.get("evidence_key") or "")
                for row in confirmed
            }
            for item in pair_evidence
        )
        if accepted:
            decision = "ACCEPT"
            reason = (
                "exact_prior_accepted_source_confirmation_satisfies_"
                "final_metric_contract"
            )
        elif pair["review_priority"] == "2" and ambiguous_count == 0:
            decision = "REJECT"
            reason = (
                "all_discovered_values_fail_frozen_contract_or_have_"
                "no_usable_value"
            )
        else:
            decision = "DEFER"
            reason = (
                "no_exact_prior_accepted_confirmation;manual_source_"
                "review_required"
            )
        adjudication.append(
            {
                "queue_rank": pair["queue_rank"],
                "review_priority": pair["review_priority"],
                "run_id": args.run_id,
                "ticker": pair["ticker"],
                "universe_role": pair["universe_role"],
                "calibration_cohort": pair["calibration_cohort"],
                "primary_archetype": pair["primary_archetype"],
                "metric_id": pair["metric_id"],
                "metric_pack": pair["metric_pack"],
                "source_lane": pair["source_lane"],
                "coverage_status": pair["coverage_status"],
                "coverage_target_class": pair[
                    "coverage_target_class"
                ],
                "minimum_usable_shortfall": pair[
                    "minimum_usable_shortfall"
                ],
                "review_decision": decision,
                "decision_reason": reason,
                "confirmation_basis": "|".join(sorted(bases)),
                "accepted_confirmed_evidence_count": len(confirmed),
                "rejection_lock_evidence_count": len(
                    pair_locked_rejections
                ),
                "deferred_review_evidence_count": ambiguous_count,
                "selected_evidence_keys": "|".join(
                    sorted(
                        str(item["evidence_key"]) for item in confirmed
                    )
                ),
                "rejection_evidence_keys": "|".join(
                    sorted(
                        str(item["evidence_key"])
                        for item in pair_locked_rejections
                    )
                ),
                "source_metric_ids": "|".join(source_metrics),
                "legacy_metric_ids": "|".join(
                    sorted(
                        {
                            legacy_metric
                            for source_metric in source_metrics
                            for legacy_metric in legacy_metric_ids(
                                final_metric_id=pair["metric_id"],
                                evidence_metric_id=source_metric,
                            )
                        }
                    )
                ),
                "reviewed_by": REVIEWED_BY,
                "reviewed_at": args.reviewed_at,
            }
        )

    generated = _dedupe_policies(policies)
    existing = [
        row
        for row in _read_csv(registry_path)
        if row["policy_version"] != POLICY_VERSION
    ]
    merged = existing + generated
    candidate_registry = (
        output_dir
        / "transportation_dedicated_parser_review_policy_candidate.csv"
    )
    adjudication_path = (
        output_dir / "transportation_evidence_adjudication.csv"
    )
    manifest_path = (
        output_dir / "transportation_evidence_adjudication_manifest.json"
    )
    candidate_golden = (
        output_dir
        / "transportation_dedicated_parser_review_policy_golden_candidate.json"
    )
    write_csv_atomic(
        adjudication_path,
        ADJUDICATION_FIELDS,
        adjudication,
    )
    write_csv_atomic(candidate_registry, POLICY_FIELDS, merged)
    validated = load_review_policies(candidate_registry)
    export_policy_golden_corpus(
        validated,
        output_path=candidate_golden,
        corpus_id="transportation_review_policy_generated",
    )
    if args.apply:
        write_csv_atomic(registry_path, POLICY_FIELDS, merged)
        export_policy_golden_corpus(
            load_review_policies(registry_path),
            output_path=golden_path,
            corpus_id="transportation_review_policy_generated",
        )
    decision_counts = Counter(
        str(row["review_decision"]) for row in adjudication
    )
    policy_counts = Counter(row["decision"] for row in generated)
    payload = {
        "acceptance": "PASS",
        "gate": "DP6C_CONSERVATIVE_EVIDENCE_ADJUDICATION",
        "model_family": MODEL_FAMILY,
        "asof_date": asof_date,
        "run_id": args.run_id,
        "reviewed_by": REVIEWED_BY,
        "reviewed_at": args.reviewed_at,
        "review_method": (
            "EXACT_PRIOR_ACCEPTED_DISCLOSURE_CONFIRMATION_ONLY"
        ),
        "review_pair_count": len(adjudication),
        "priority_counts": dict(sorted(priority_counts.items())),
        "decision_counts": dict(sorted(decision_counts.items())),
        "generated_policy_count": len(generated),
        "generated_policy_decision_counts": dict(
            sorted(policy_counts.items())
        ),
        "accepted_evidence_key_count": len(accepted_evidence_keys),
        "rejection_evidence_key_count": len(rejection_evidence_keys),
        "applied": bool(args.apply),
        "parser_authorized": False,
        "network_invocations": 0,
        "provider_invocations": 0,
        "parser_invocations": 0,
        "feature_build_invocations": 0,
        "calibration_invocations": 0,
        "inputs": {
            "coverage_lift_manifest": {
                "path": str(lift_manifest_path),
                "sha256": file_sha256(lift_manifest_path),
            },
            "review_queue": {
                "path": str(queue_path),
                "sha256": file_sha256(queue_path),
            },
            "registry_before_or_after": {
                "path": str(registry_path),
                "sha256": file_sha256(registry_path),
            },
        },
        "artifacts": {
            "adjudication": {
                "path": str(adjudication_path),
                "row_count": len(adjudication),
                "sha256": file_sha256(adjudication_path),
            },
            "candidate_registry": {
                "path": str(candidate_registry),
                "row_count": len(merged),
                "sha256": file_sha256(candidate_registry),
            },
            "candidate_golden": {
                "path": str(candidate_golden),
                "sha256": file_sha256(candidate_golden),
            },
        },
        "next_gate": (
            "POLICY_ONLY_REPLAY_RUN_58"
            if args.apply
            else "REVIEW_CANDIDATE_AND_RERUN_WITH_APPLY"
        ),
    }
    write_text_atomic(
        manifest_path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
