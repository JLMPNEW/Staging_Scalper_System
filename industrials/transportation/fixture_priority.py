from __future__ import annotations

from collections import Counter
from typing import Mapping, Sequence


FIXTURE_PRIORITY_VERSION = "transportation_dp6y_fixture_priority_v1"
TOP_REVIEW_METRICS = frozenset(
    {
        "revenue_days",
        "average_length_of_haul",
        "vessel_count",
        "fleet_capacity",
        "tce_day_rate",
        "fuel_surcharge_revenue_ratio",
    }
)

PAIR_PRIORITY_FIELDS = (
    "priority_version",
    "review_order",
    "phase_rank",
    "review_phase",
    "pair_key",
    "fixture_id",
    "fixture_priority",
    "ticker",
    "universe_role",
    "calibration_cohort",
    "primary_archetype",
    "metric_id",
    "metric_pack",
    "source_lane",
    "source_metric_ids",
    "review_numeric_count",
    "review_no_value_count",
    "representative_evidence_count",
    "representative_evidence_keys",
    "semantic_contract_sha256",
    "evidence_bundle_sha256",
    "review_route",
    "priority_reason",
)

EVIDENCE_PRIORITY_FIELDS = (
    "priority_version",
    "review_order",
    "phase_rank",
    "review_phase",
    "pair_key",
    "fixture_id",
    "ticker",
    "metric_id",
    "source_lane",
    "source_metric_id",
    "evidence_key",
    "candidate_status",
    "candidate_value",
    "unit",
    "period_end",
    "scope",
    "confidence",
    "source_stage",
    "accession_number",
    "form_type",
    "filing_date",
    "source_document",
    "extraction_method",
    "status_reason",
    "evidence_row_sha256",
    "evidence_text",
)


def review_phase(row: Mapping[str, str]) -> tuple[int, str, str]:
    numeric_count = int(str(row.get("review_numeric_count") or "0"))
    no_value_count = int(
        str(row.get("review_no_value_count") or "0")
    )
    metric_id = str(row.get("metric_id") or "")
    if numeric_count == 1 and no_value_count == 0:
        return (
            1,
            "A_STRICT_SINGLE_NUMERIC",
            "one_numeric_candidate_no_text_only_review_rows",
        )
    if numeric_count == 1:
        return (
            2,
            "B_SINGLE_NUMERIC_WITH_TEXT_NOISE",
            "one_numeric_candidate_with_text_only_review_rows",
        )
    if metric_id in TOP_REVIEW_METRICS:
        return (
            3,
            "C_TOP_SIX_REMAINING",
            "high_volume_metric_after_single_numeric_deduplication",
        )
    return (
        4,
        "D_REMAINING_FROZEN_QUEUE",
        "outside_current_fixture_review_batch",
    )


def build_fixture_priority_batches(
    *,
    adjudication_rows: Sequence[Mapping[str, str]],
    pair_contract_rows: Sequence[Mapping[str, str]],
    evidence_rows: Sequence[Mapping[str, str]],
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    dict[str, object],
    list[str],
]:
    errors: list[str] = []
    adjudication = {
        str(row["pair_key"]): row for row in adjudication_rows
    }
    contracts = {
        str(row["pair_key"]): row for row in pair_contract_rows
    }
    if len(adjudication) != len(adjudication_rows):
        errors.append("adjudication pair keys are duplicated")
    if len(contracts) != len(pair_contract_rows):
        errors.append("fixture pair keys are duplicated")
    if set(adjudication) != set(contracts):
        errors.append("adjudication and fixture pair universes differ")

    pair_rows: list[dict[str, object]] = []
    for pair_key in sorted(adjudication):
        row = adjudication[pair_key]
        contract = contracts[pair_key]
        phase_rank, phase, reason = review_phase(row)
        source_lane = str(row["source_lane"])
        pair_rows.append(
            {
                "priority_version": FIXTURE_PRIORITY_VERSION,
                "review_order": 0,
                "phase_rank": phase_rank,
                "review_phase": phase,
                "pair_key": pair_key,
                "fixture_id": contract["fixture_id"],
                "fixture_priority": row["fixture_priority"],
                "ticker": row["ticker"],
                "universe_role": row["universe_role"],
                "calibration_cohort": row["calibration_cohort"],
                "primary_archetype": row["primary_archetype"],
                "metric_id": row["metric_id"],
                "metric_pack": row["metric_pack"],
                "source_lane": source_lane,
                "source_metric_ids": row["source_metric_ids"],
                "review_numeric_count": row["review_numeric_count"],
                "review_no_value_count": row[
                    "review_no_value_count"
                ],
                "representative_evidence_count": contract[
                    "representative_evidence_count"
                ],
                "representative_evidence_keys": contract[
                    "representative_evidence_keys"
                ],
                "semantic_contract_sha256": contract[
                    "semantic_contract_sha256"
                ],
                "evidence_bundle_sha256": contract[
                    "evidence_bundle_sha256"
                ],
                "review_route": (
                    "VALIDATE_DERIVATION_DEPENDENCIES"
                    if source_lane == "DP-D"
                    else "REVIEW_DIRECT_SEMANTIC_FIXTURE"
                ),
                "priority_reason": reason,
            }
        )
    pair_rows.sort(
        key=lambda row: (
            int(str(row["phase_rank"])),
            int(str(row["fixture_priority"])),
            str(row["metric_id"]),
            str(row["ticker"]),
        )
    )
    for review_order, row in enumerate(pair_rows, start=1):
        row["review_order"] = review_order

    pair_index = {
        str(row["pair_key"]): row for row in pair_rows
    }
    evidence_output: list[dict[str, object]] = []
    seen_evidence_keys: set[tuple[str, str]] = set()
    for evidence in evidence_rows:
        pair_key = str(evidence["pair_key"])
        pair = pair_index.get(pair_key)
        if pair is None:
            errors.append(f"{pair_key}: orphan fixture evidence")
            continue
        evidence_key = str(evidence["evidence_key"])
        pair_evidence_key = (pair_key, evidence_key)
        if pair_evidence_key in seen_evidence_keys:
            errors.append(
                "duplicate fixture pair/evidence key="
                f"{pair_key}/{evidence_key}"
            )
        seen_evidence_keys.add(pair_evidence_key)
        evidence_output.append(
            {
                "priority_version": FIXTURE_PRIORITY_VERSION,
                "review_order": pair["review_order"],
                "phase_rank": pair["phase_rank"],
                "review_phase": pair["review_phase"],
                "pair_key": pair_key,
                "fixture_id": pair["fixture_id"],
                "ticker": evidence["ticker"],
                "metric_id": evidence["metric_id"],
                "source_lane": pair["source_lane"],
                "source_metric_id": evidence["source_metric_id"],
                "evidence_key": evidence_key,
                "candidate_status": evidence["candidate_status"],
                "candidate_value": evidence["candidate_value"],
                "unit": evidence["unit"],
                "period_end": evidence["period_end"],
                "scope": evidence["scope"],
                "confidence": evidence["confidence"],
                "source_stage": evidence["source_stage"],
                "accession_number": evidence["accession_number"],
                "form_type": evidence["form_type"],
                "filing_date": evidence["filing_date"],
                "source_document": evidence["source_document"],
                "extraction_method": evidence["extraction_method"],
                "status_reason": evidence["status_reason"],
                "evidence_row_sha256": evidence[
                    "evidence_row_sha256"
                ],
                "evidence_text": evidence["evidence_text"],
            }
        )
    evidence_output.sort(
        key=lambda row: (
            int(str(row["review_order"])),
            not bool(str(row["candidate_value"])),
            str(row["evidence_key"]),
        )
    )

    phase_counts = Counter(
        str(row["review_phase"]) for row in pair_rows
    )
    route_counts = Counter(
        str(row["review_route"]) for row in pair_rows
    )
    selected = [
        row for row in pair_rows if int(str(row["phase_rank"])) <= 3
    ]
    top_six_count = sum(
        str(row["metric_id"]) in TOP_REVIEW_METRICS
        for row in pair_rows
    )
    top_six_single_overlap = sum(
        str(row["metric_id"]) in TOP_REVIEW_METRICS
        and int(str(row["review_numeric_count"])) == 1
        for row in pair_rows
    )
    expected = {
        "A_STRICT_SINGLE_NUMERIC": 57,
        "B_SINGLE_NUMERIC_WITH_TEXT_NOISE": 36,
        "C_TOP_SIX_REMAINING": 141,
        "D_REMAINING_FROZEN_QUEUE": 485,
    }
    if dict(phase_counts) != expected:
        errors.append(
            f"fixture phase counts={dict(phase_counts)} expected={expected}"
        )
    summary: dict[str, object] = {
        "fixture_pair_count": len(pair_rows),
        "fixture_evidence_row_count": len(evidence_output),
        "phase_counts": dict(sorted(phase_counts.items())),
        "review_route_counts": dict(sorted(route_counts.items())),
        "selected_pair_count": len(selected),
        "single_numeric_pair_count": (
            phase_counts["A_STRICT_SINGLE_NUMERIC"]
            + phase_counts["B_SINGLE_NUMERIC_WITH_TEXT_NOISE"]
        ),
        "top_six_pair_count": top_six_count,
        "top_six_single_overlap_count": top_six_single_overlap,
        "selected_unique_queue_share": (
            len(selected) / len(pair_rows) if pair_rows else 0.0
        ),
    }
    return pair_rows, evidence_output, summary, errors
