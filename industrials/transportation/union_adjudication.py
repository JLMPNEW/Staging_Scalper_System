from __future__ import annotations

import math
from collections import Counter
from typing import Mapping, Sequence

from industrials.transportation.adjudication import (
    accepted_final_metric,
    confirmation_basis,
)
from industrials.transportation.parser_coverage import (
    PARSER_DERIVATIONS,
)


UNION_ADJUDICATION_VERSION = (
    "transportation_dp6i_union_adjudication_v1"
)
UNION_ADJUDICATION_FIELDS = (
    "adjudication_version",
    "pair_key",
    "queue_rank",
    "fixture_priority",
    "ticker",
    "universe_role",
    "calibration_cohort",
    "primary_archetype",
    "metric_id",
    "metric_pack",
    "source_lane",
    "source_metric_ids",
    "coverage_status",
    "source_evidence_count",
    "review_evidence_count",
    "review_numeric_count",
    "review_no_value_count",
    "review_consolidated_numeric_count",
    "review_unknown_scope_numeric_count",
    "accepted_dependency_evidence_count",
    "rejected_evidence_count",
    "parser_failure_evidence_count",
    "distinct_accession_count",
    "distinct_document_count",
    "distinct_period_count",
    "unit_variants",
    "exact_confirmation_count",
    "confirmation_basis",
    "confirmed_evidence_keys",
    "review_decision",
    "decision_reason",
    "required_next_action",
    "qa_flags",
    "representative_evidence_keys",
    "reviewed_by",
    "reviewed_at",
)
FIXTURE_EVIDENCE_FIELDS = (
    "pair_key",
    "fixture_priority",
    "ticker",
    "metric_id",
    "source_metric_id",
    "source_stage",
    "evidence_key",
    "candidate_status",
    "candidate_value",
    "unit",
    "period_end",
    "scope",
    "confidence",
    "accession_number",
    "form_type",
    "filing_date",
    "source_document",
    "extraction_method",
    "status_reason",
    "evidence_text",
)


def source_metrics_for_pair(
    *,
    metric_id: str,
    source_lane: str,
) -> tuple[str, ...]:
    if source_lane == "DP":
        return (metric_id,)
    if source_lane == "DP-D":
        dependencies = PARSER_DERIVATIONS[metric_id]["dependencies"]
        if not isinstance(dependencies, (list, tuple)):
            raise TypeError(
                f"{metric_id}: dependencies must be a list or tuple"
            )
        return tuple(
            str(value)
            for value in dependencies
        )
    return ()


def _has_value(row: Mapping[str, object]) -> bool:
    value = row.get("candidate_value")
    if value is not None and str(value) != "":
        try:
            return math.isfinite(float(str(value)))
        except ValueError:
            return False
    return False


def _evidence_rank(
    row: Mapping[str, object],
) -> tuple[object, ...]:
    return (
        str(row.get("candidate_status") or "")
        != "REVIEW_REQUIRED",
        not _has_value(row),
        str(row.get("scope") or "") != "consolidated",
        -float(str(row.get("confidence") or 0.0)),
        not bool(str(row.get("period_end") or "")[:10]),
        str(row.get("filing_date") or ""),
        str(row.get("evidence_key") or ""),
    )


def _qa_flags(
    *,
    review_rows: Sequence[Mapping[str, object]],
    exact_count: int,
) -> tuple[str, ...]:
    flags: list[str] = []
    numeric = [row for row in review_rows if _has_value(row)]
    if any(not _has_value(row) for row in review_rows):
        flags.append("NON_NUMERIC_DISCOVERY_PRESENT")
    if any(
        str(row.get("scope") or "") != "consolidated"
        for row in numeric
    ):
        flags.append("UNKNOWN_OR_NONCONSOLIDATED_SCOPE_PRESENT")
    if len(numeric) > 1:
        flags.append("MULTIPLE_CANDIDATES_REQUIRE_DISAMBIGUATION")
    units = {
        str(row.get("unit") or "")
        for row in numeric
        if str(row.get("unit") or "")
    }
    if len(units) > 1:
        flags.append("UNIT_VARIANTS_PRESENT")
    if exact_count == 0:
        flags.append("NO_EXACT_ACCEPTED_FIXTURE")
    return tuple(flags)


def build_union_adjudication(
    *,
    coverage_rows: Sequence[Mapping[str, str]],
    evidence_rows: Sequence[Mapping[str, object]],
    legacy_index: Mapping[
        tuple[str, str, str, str, str, str],
        Sequence[Mapping[str, object]],
    ],
    reviewed_at: str,
    reviewed_by: str,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    indexed: dict[tuple[str, str], list[Mapping[str, object]]] = {}
    for row in evidence_rows:
        key = (
            str(row.get("ticker") or "").upper(),
            str(row.get("metric_name") or ""),
        )
        indexed.setdefault(key, []).append(row)
    queue = [
        row
        for row in coverage_rows
        if row.get("applicability_status") == "APPLICABLE"
        and row.get("coverage_status") == "COVERED_REVIEW_REQUIRED"
        and row.get("source_lane") in {"DP", "DP-D"}
    ]
    queue.sort(
        key=lambda row: (
            row.get("universe_role") != "active",
            row.get("metric_id") or "",
            row.get("ticker") or "",
        )
    )
    decisions: list[dict[str, object]] = []
    fixture_rows: list[dict[str, object]] = []
    for rank, pair in enumerate(queue, start=1):
        ticker = str(pair["ticker"]).upper()
        metric_id = str(pair["metric_id"])
        source_lane = str(pair["source_lane"])
        source_metrics = source_metrics_for_pair(
            metric_id=metric_id,
            source_lane=source_lane,
        )
        scoped = [
            item
            for source_metric in source_metrics
            for item in indexed.get((ticker, source_metric), ())
        ]
        review = [
            item
            for item in scoped
            if str(item.get("candidate_status") or "")
            == "REVIEW_REQUIRED"
        ]
        confirmed: list[Mapping[str, object]] = []
        bases: set[str] = set()
        for item in review:
            basis = confirmation_basis(
                item,
                final_metric_id=metric_id,
                legacy_index=legacy_index,
            )
            if basis:
                confirmed.append(item)
                bases.add(basis)
        accepted = accepted_final_metric(
            final_metric_id=metric_id,
            source_lane=source_lane,
            confirmed_evidence=confirmed,
        )
        if accepted:
            decision = "ACCEPT"
            reason = (
                "exact_prior_accepted_source_confirmation_satisfies_"
                "the_final_metric_contract"
            )
            next_action = "BUILD_HASH_EXACT_REVIEW_POLICY_CANDIDATE"
        else:
            decision = "DEFER"
            reason = (
                "broad_discovery_evidence_has_no_exact_accepted_fixture;"
                "automatic_promotion_is_not_safe"
            )
            next_action = (
                "VALIDATE_DERIVATION_DEPENDENCY_FIXTURES"
                if source_lane == "DP-D"
                else "BUILD_METRIC_SPECIFIC_SEMANTIC_FIXTURE"
            )
        numeric = [item for item in review if _has_value(item)]
        fixture_priority = (
            1 if str(pair.get("universe_role") or "") == "active" else 2
        )
        representatives = sorted(review, key=_evidence_rank)[:3]
        pair_key = f"{ticker}|{metric_id}"
        representative_keys = [
            str(item.get("evidence_key") or "")
            for item in representatives
            if str(item.get("evidence_key") or "")
        ]
        decisions.append(
            {
                "adjudication_version": UNION_ADJUDICATION_VERSION,
                "pair_key": pair_key,
                "queue_rank": rank,
                "fixture_priority": fixture_priority,
                "ticker": ticker,
                "universe_role": pair.get("universe_role") or "",
                "calibration_cohort": (
                    pair.get("calibration_cohort") or ""
                ),
                "primary_archetype": (
                    pair.get("primary_archetype") or ""
                ),
                "metric_id": metric_id,
                "metric_pack": pair.get("metric_pack") or "",
                "source_lane": source_lane,
                "source_metric_ids": "|".join(source_metrics),
                "coverage_status": pair.get("coverage_status") or "",
                "source_evidence_count": len(scoped),
                "review_evidence_count": len(review),
                "review_numeric_count": len(numeric),
                "review_no_value_count": len(review) - len(numeric),
                "review_consolidated_numeric_count": sum(
                    str(item.get("scope") or "") == "consolidated"
                    for item in numeric
                ),
                "review_unknown_scope_numeric_count": sum(
                    str(item.get("scope") or "") != "consolidated"
                    for item in numeric
                ),
                "accepted_dependency_evidence_count": sum(
                    str(item.get("candidate_status") or "") == "ACCEPTED"
                    for item in scoped
                ),
                "rejected_evidence_count": sum(
                    str(item.get("candidate_status") or "")
                    == "REJECTED_POLICY"
                    for item in scoped
                ),
                "parser_failure_evidence_count": sum(
                    str(item.get("candidate_status") or "")
                    == "PARSER_FAILURE"
                    for item in scoped
                ),
                "distinct_accession_count": len(
                    {
                        str(item.get("accession_number") or "")
                        for item in scoped
                        if str(item.get("accession_number") or "")
                    }
                ),
                "distinct_document_count": len(
                    {
                        (
                            str(item.get("accession_number") or ""),
                            str(item.get("source_document") or ""),
                        )
                        for item in scoped
                        if str(item.get("source_document") or "")
                    }
                ),
                "distinct_period_count": len(
                    {
                        str(item.get("period_end") or "")[:10]
                        for item in review
                        if str(item.get("period_end") or "")[:10]
                    }
                ),
                "unit_variants": "|".join(
                    sorted(
                        {
                            str(item.get("unit") or "")
                            for item in numeric
                            if str(item.get("unit") or "")
                        }
                    )
                ),
                "exact_confirmation_count": len(confirmed),
                "confirmation_basis": "|".join(sorted(bases)),
                "confirmed_evidence_keys": "|".join(
                    sorted(
                        str(item.get("evidence_key") or "")
                        for item in confirmed
                        if str(item.get("evidence_key") or "")
                    )
                ),
                "review_decision": decision,
                "decision_reason": reason,
                "required_next_action": next_action,
                "qa_flags": "|".join(
                    _qa_flags(
                        review_rows=review,
                        exact_count=len(confirmed),
                    )
                ),
                "representative_evidence_keys": "|".join(
                    representative_keys
                ),
                "reviewed_by": reviewed_by,
                "reviewed_at": reviewed_at,
            }
        )
        for item in representatives:
            fixture_rows.append(
                {
                    "pair_key": pair_key,
                    "fixture_priority": fixture_priority,
                    "ticker": ticker,
                    "metric_id": metric_id,
                    "source_metric_id": (
                        item.get("metric_name") or ""
                    ),
                    "source_stage": item.get("source_stage") or "",
                    "evidence_key": item.get("evidence_key") or "",
                    "candidate_status": (
                        item.get("candidate_status") or ""
                    ),
                    "candidate_value": (
                        ""
                        if item.get("candidate_value") is None
                        else item.get("candidate_value")
                    ),
                    "unit": item.get("unit") or "",
                    "period_end": item.get("period_end") or "",
                    "scope": item.get("scope") or "",
                    "confidence": item.get("confidence") or "",
                    "accession_number": (
                        item.get("accession_number") or ""
                    ),
                    "form_type": item.get("form_type") or "",
                    "filing_date": item.get("filing_date") or "",
                    "source_document": (
                        item.get("source_document") or ""
                    ),
                    "extraction_method": (
                        item.get("extraction_method") or ""
                    ),
                    "status_reason": (
                        item.get("status_reason") or ""
                    ),
                    "evidence_text": item.get("evidence_text") or "",
                }
            )
    return decisions, fixture_rows


def summarize_union_adjudication(
    rows: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    return {
        "review_pair_count": len(rows),
        "review_ticker_count": len(
            {str(row["ticker"]) for row in rows}
        ),
        "review_metric_count": len(
            {str(row["metric_id"]) for row in rows}
        ),
        "decision_counts": dict(
            sorted(
                Counter(
                    str(row["review_decision"]) for row in rows
                ).items()
            )
        ),
        "fixture_priority_counts": dict(
            sorted(
                Counter(
                    str(row["fixture_priority"]) for row in rows
                ).items()
            )
        ),
        "exact_confirmation_pair_count": sum(
            int(str(row["exact_confirmation_count"]) or "0") > 0
            for row in rows
        ),
        "metric_fixture_pair_count": sum(
            str(row["required_next_action"])
            == "BUILD_METRIC_SPECIFIC_SEMANTIC_FIXTURE"
            for row in rows
        ),
        "derivation_fixture_pair_count": sum(
            str(row["required_next_action"])
            == "VALIDATE_DERIVATION_DEPENDENCY_FIXTURES"
            for row in rows
        ),
    }
