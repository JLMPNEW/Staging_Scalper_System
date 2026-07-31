from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from typing import Mapping, Sequence

from dedicated_parser.policy import POLICY_FIELDS
from industrials.transportation.parser_coverage import PARSER_DERIVATIONS


MODEL_FAMILY = "transportation"
POLICY_VERSION = "transportation_run58_conservative_adjudication_v1"
REVIEWED_BY = "codex_transportation_conservative_review_v1"

# These mappings are narrower than the retired v2 composites. They are
# permitted only when the new observation is an exact value/source/period/unit
# match to an already accepted legacy observation and the DP0 applicability
# contract has already constrained the issuer to the stated archetype.
REVIEWED_LEGACY_MAPPINGS: dict[str, tuple[str, ...]] = {
    "equipment_utilization": ("asset_utilization",),
    "passenger_load_factor": ("load_factor_or_utilization",),
    "tce_day_rate": ("tce_or_day_rate",),
}

DEFINITE_REJECTION_REASONS = frozenset(
    {
        "growth_ratio_out_of_bounds",
        "missing_period_end",
        "negative_value_prohibited",
        "nonissuer_or_proforma_scope",
        "ratio_value_out_of_bounds",
        "unit_contract_mismatch",
        "years_value_out_of_bounds",
    }
)

ADJUDICATION_FIELDS = (
    "queue_rank",
    "review_priority",
    "run_id",
    "ticker",
    "universe_role",
    "calibration_cohort",
    "primary_archetype",
    "metric_id",
    "metric_pack",
    "source_lane",
    "coverage_status",
    "coverage_target_class",
    "minimum_usable_shortfall",
    "review_decision",
    "decision_reason",
    "confirmation_basis",
    "accepted_confirmed_evidence_count",
    "rejection_lock_evidence_count",
    "deferred_review_evidence_count",
    "selected_evidence_keys",
    "rejection_evidence_keys",
    "source_metric_ids",
    "legacy_metric_ids",
    "reviewed_by",
    "reviewed_at",
)


def _as_float(value: object) -> float | None:
    try:
        output = float(str(value))
    except (TypeError, ValueError):
        return None
    return output if math.isfinite(output) else None


def value_matches(first: object, second: object) -> bool:
    left = _as_float(first)
    right = _as_float(second)
    if left is None or right is None:
        return False
    return abs(left - right) <= max(1e-6, abs(right) * 1e-9)


def legacy_metric_ids(
    *,
    final_metric_id: str,
    evidence_metric_id: str,
) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            (
                evidence_metric_id,
                *REVIEWED_LEGACY_MAPPINGS.get(final_metric_id, ()),
            )
        )
    )


def build_legacy_index(
    rows: Sequence[Mapping[str, object]],
) -> dict[tuple[str, str, str, str, str, str], list[dict[str, object]]]:
    output: dict[
        tuple[str, str, str, str, str, str],
        list[dict[str, object]],
    ] = defaultdict(list)
    for row in rows:
        if str(row.get("candidate_status") or "") != "ACCEPTED":
            continue
        key = (
            str(row.get("ticker") or "").upper(),
            str(row.get("metric_name") or ""),
            str(row.get("accession_number") or ""),
            str(row.get("document_name") or ""),
            str(row.get("period_end") or "")[:10],
            str(row.get("unit") or ""),
        )
        output[key].append(dict(row))
    return dict(output)


def confirmation_basis(
    evidence: Mapping[str, object],
    *,
    final_metric_id: str,
    legacy_index: Mapping[
        tuple[str, str, str, str, str, str],
        Sequence[Mapping[str, object]],
    ],
) -> str:
    if (
        str(evidence.get("candidate_status") or "") != "REVIEW_REQUIRED"
        or str(evidence.get("scope") or "") == "nonissuer"
        or _as_float(evidence.get("candidate_value")) is None
        or not str(evidence.get("period_end") or "")[:10]
        or not str(evidence.get("unit") or "")
    ):
        return ""
    evidence_metric = str(evidence.get("metric_name") or "")
    for legacy_metric in legacy_metric_ids(
        final_metric_id=final_metric_id,
        evidence_metric_id=evidence_metric,
    ):
        key = (
            str(evidence.get("ticker") or "").upper(),
            legacy_metric,
            str(evidence.get("accession_number") or ""),
            str(evidence.get("source_document") or ""),
            str(evidence.get("period_end") or "")[:10],
            str(evidence.get("unit") or ""),
        )
        if any(
            value_matches(
                evidence.get("candidate_value"),
                legacy.get("candidate_value"),
            )
            for legacy in legacy_index.get(key, ())
        ):
            return (
                "EXACT_ACCEPTED_LEGACY_MATCH"
                if legacy_metric == evidence_metric
                else f"EXACT_REVIEWED_MAPPING:{legacy_metric}"
            )
    return ""


def accepted_final_metric(
    *,
    final_metric_id: str,
    source_lane: str,
    confirmed_evidence: Sequence[Mapping[str, object]],
) -> bool:
    if source_lane == "DP":
        return bool(confirmed_evidence)
    if source_lane != "DP-D":
        return False
    rule = PARSER_DERIVATIONS[final_metric_id]
    raw_dependencies = rule["dependencies"]
    if not isinstance(raw_dependencies, (list, tuple)):
        raise TypeError(
            f"{final_metric_id}: dependencies must be a list or tuple"
        )
    dependencies = tuple(str(value) for value in raw_dependencies)
    periods: dict[str, set[str]] = defaultdict(set)
    for row in confirmed_evidence:
        period = str(row.get("period_end") or "")[:10]
        if period:
            periods[str(row.get("metric_name") or "")].add(period)
    mode = str(rule["mode"])
    if mode == "any":
        return any(periods.get(metric_id) for metric_id in dependencies)
    if mode == "all":
        if not all(periods.get(metric_id) for metric_id in dependencies):
            return False
        return bool(
            set.intersection(
                *(set(periods[metric_id]) for metric_id in dependencies)
            )
        )
    minimum_periods = int(str(rule.get("minimum_periods") or 1))
    return len(periods.get(dependencies[0], set())) >= minimum_periods


def policy_match_key(row: Mapping[str, object]) -> tuple[str, ...]:
    return tuple(
        str(row.get(field) or "")
        for field in (
            "model_family",
            "ticker",
            "accession_number",
            "source_document",
            "metric_name",
            "concept_name",
            "candidate_value",
            "unit",
            "period_start",
            "period_end",
        )
    )


def policy_row(
    evidence: Mapping[str, object],
    *,
    decision: str,
    status_reason: str,
    reviewed_at: str,
    run_id: int = 58,
    policy_version: str = POLICY_VERSION,
    reviewed_by: str = REVIEWED_BY,
    value_override: float | None = None,
) -> dict[str, str]:
    evidence_key = str(evidence.get("evidence_key") or "")
    digest = hashlib.sha256(
        (
            f"{MODEL_FAMILY}|{run_id}|{evidence_key}|{decision}|"
            f"{status_reason}|{value_override}"
        ).encode("utf-8")
    ).hexdigest()[:16]
    candidate_value = evidence.get("candidate_value")
    row = {
        "policy_id": f"trprev_r{run_id}_{digest}",
        "policy_version": policy_version,
        "enabled": "1",
        "model_family": MODEL_FAMILY,
        "ticker": str(evidence.get("ticker") or "").upper(),
        "accession_number": str(
            evidence.get("accession_number") or ""
        ),
        "source_document": str(
            evidence.get("source_document") or ""
        ),
        "metric_name": str(evidence.get("metric_name") or ""),
        "concept_name": str(evidence.get("concept_name") or ""),
        "candidate_value": (
            "" if candidate_value is None else str(candidate_value)
        ),
        "value_tolerance": "0.000001",
        "unit": str(evidence.get("unit") or ""),
        "period_start": str(evidence.get("period_start") or ""),
        "period_end": str(evidence.get("period_end") or "")[:10],
        "decision": decision,
        "status_reason": status_reason,
        "scope_override": "",
        "confidence_override": (
            "1.0" if decision == "ACCEPTED" else ""
        ),
        "reviewed_by": reviewed_by,
        "reviewed_at": reviewed_at,
        "period_start_override": "",
        "period_end_override": "",
        "value_override": (
            "" if value_override is None else str(value_override)
        ),
    }
    return {field: row[field] for field in POLICY_FIELDS}


def lockable_rejection(evidence: Mapping[str, object]) -> bool:
    return (
        str(evidence.get("candidate_status") or "")
        == "REJECTED_POLICY"
        and str(evidence.get("status_reason") or "")
        in DEFINITE_REJECTION_REASONS
        and _as_float(evidence.get("candidate_value")) is not None
        and bool(str(evidence.get("unit") or ""))
        and bool(str(evidence.get("period_end") or "")[:10])
    )
