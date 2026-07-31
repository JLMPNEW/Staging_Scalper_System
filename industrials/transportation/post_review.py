from __future__ import annotations

from collections import defaultdict
from datetime import date
from statistics import median
from typing import Mapping, Sequence


POST_REVIEW_METRIC_FIELDS = (
    "run_id",
    "evaluation_id",
    "metric_id",
    "metric_pack",
    "source_lane",
    "pre_active_accepted_count",
    "pre_active_usable_count",
    "post_active_accepted_count",
    "post_active_usable_count",
    "post_inactive_accepted_count",
    "broad_required_count",
    "broad_accepted_shortfall",
    "best_accepted_niche_archetype",
    "best_accepted_niche_required_count",
    "best_accepted_niche_count",
    "best_accepted_niche_shortfall",
    "accepted_breadth_gate_pass",
    "accepted_review_pair_count",
    "rejected_review_pair_count",
    "deferred_review_pair_count",
    "accepted_validation_rate",
    "evidence_precision_gate_status",
    "covered_issuer_median_accepted_period_count",
    "covered_issuer_median_history_span_years",
    "historical_depth_gate_pass",
    "survivor_bias_status",
    "formal_calibration_gate_pass",
    "metric_disposition",
    "disposition_reason",
)


def _date_ordinal(value: str) -> int:
    try:
        return date.fromisoformat(value[:10]).toordinal()
    except ValueError:
        return 0


def _history_summary(
    period_sets: Sequence[set[str]],
) -> tuple[float, float]:
    if not period_sets:
        return 0.0, 0.0
    counts = [len(periods) for periods in period_sets]
    spans = []
    for periods in period_sets:
        ordinals = sorted(
            value
            for value in (_date_ordinal(period) for period in periods)
            if value
        )
        spans.append(
            (
                (ordinals[-1] - ordinals[0]) / 365.25
                if len(ordinals) >= 2
                else 0.0
            )
        )
    return float(median(counts)), float(median(spans))


def build_post_review_metric_rows(
    *,
    run_id: int,
    evaluation_id: int,
    pre_gate_rows: Sequence[Mapping[str, object]],
    post_gate_rows: Sequence[Mapping[str, object]],
    post_coverage_rows: Sequence[Mapping[str, object]],
    adjudication_rows: Sequence[Mapping[str, object]],
    accepted_periods: Mapping[tuple[str, str], set[str]],
) -> list[dict[str, object]]:
    pre = {str(row["metric_id"]): row for row in pre_gate_rows}
    post = {str(row["metric_id"]): row for row in post_gate_rows}
    coverage: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for row in post_coverage_rows:
        if str(row.get("applicability_status") or "") == "APPLICABLE":
            coverage[str(row["metric_id"])].append(row)
    decisions: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for row in adjudication_rows:
        decisions[str(row["metric_id"])].append(row)

    output: list[dict[str, object]] = []
    for metric_id, gate in sorted(post.items()):
        scoped = coverage[metric_id]
        active = [
            row
            for row in scoped
            if str(row["universe_role"]) == "active"
        ]
        inactive = [
            row
            for row in scoped
            if str(row["universe_role"]) != "active"
        ]
        accepted_statuses = {
            "COVERED_ACCEPTED",
            "COVERED_FINANCIAL_DERIVED",
        }
        usable_statuses = accepted_statuses | {
            "COVERED_REVIEW_REQUIRED"
        }
        active_accepted = [
            row
            for row in active
            if str(row["coverage_status"]) in accepted_statuses
        ]
        active_usable = [
            row
            for row in active
            if str(row["coverage_status"]) in usable_statuses
        ]
        inactive_accepted = [
            row
            for row in inactive
            if str(row["coverage_status"]) in accepted_statuses
        ]
        metric_decisions = decisions.get(metric_id, [])
        accepted_decisions = [
            row
            for row in metric_decisions
            if str(row.get("review_decision") or "") == "ACCEPT"
        ]
        rejected_decisions = [
            row
            for row in metric_decisions
            if str(row.get("review_decision") or "") == "REJECT"
        ]
        deferred_decisions = [
            row
            for row in metric_decisions
            if str(row.get("review_decision") or "") == "DEFER"
        ]
        source_lane = str(gate["source_lane"])
        exact_accepts = sum(
            bool(str(row.get("confirmation_basis") or "").strip())
            for row in accepted_decisions
        )
        if source_lane == "FIN-D":
            precision_rate = 1.0
            precision_status = "PASS_FINANCIAL_DERIVATION_CONTRACT"
            precision_pass = True
        elif accepted_decisions and exact_accepts == len(
            accepted_decisions
        ):
            precision_rate = 1.0
            precision_status = (
                "PASS_EXACT_PRIOR_ACCEPTED_DISCLOSURE_CONFIRMATION"
            )
            precision_pass = True
        elif active_accepted:
            precision_rate = 0.0
            precision_status = "FAIL_ACCEPTED_EVIDENCE_NOT_REVIEW_CONFIRMED"
            precision_pass = False
        else:
            precision_rate = 0.0
            precision_status = "NOT_ESTIMABLE_NO_ACCEPTED_REVIEW"
            precision_pass = False

        period_sets = [
            accepted_periods.get(
                (str(row["ticker"]), metric_id),
                set(),
            )
            for row in [*active_accepted, *inactive_accepted]
            if source_lane != "FIN-D"
        ]
        period_sets = [periods for periods in period_sets if periods]
        median_periods, median_span = _history_summary(period_sets)
        if source_lane == "FIN-D":
            history_pass = False
            history_reason = (
                "financial_derived_history_requires_frozen_pit_panel_check"
            )
        else:
            history_pass = median_periods >= 4 and median_span >= 3.0
            history_reason = (
                "historical_depth_pass"
                if history_pass
                else "median_history_below_four_periods_or_three_years"
            )
        breadth_pass = bool(int(str(gate["accepted_gate_pass"])))
        formal_pass = breadth_pass and precision_pass and history_pass
        if formal_pass:
            disposition = "CALIBRATION_CANDIDATE"
            disposition_reason = "all_predeclared_acceptance_gates_pass"
        elif active_accepted:
            disposition = "DIAGNOSTIC_ONLY"
            disposition_reason = ";".join(
                reason
                for passed, reason in (
                    (
                        breadth_pass,
                        "accepted_issuer_breadth_below_gate",
                    ),
                    (
                        precision_pass,
                        "evidence_precision_below_gate",
                    ),
                    (history_pass, history_reason),
                )
                if not passed
            )
        elif active_usable:
            disposition = "DEFERRED_REVIEW"
            disposition_reason = (
                "usable_evidence_remains_unaccepted_after_conservative_review"
            )
        else:
            disposition = "EXCLUDED_INSUFFICIENT_EVIDENCE"
            disposition_reason = "no_accepted_or_usable_active_evidence"
        survivor_status = (
            "PASS_INACTIVE_EVIDENCE_PRESENT"
            if inactive_accepted
            else "LIMITATION_NO_ACCEPTED_INACTIVE_EVIDENCE"
        )
        pre_gate = pre.get(metric_id, {})
        output.append(
            {
                "run_id": run_id,
                "evaluation_id": evaluation_id,
                "metric_id": metric_id,
                "metric_pack": gate["metric_pack"],
                "source_lane": source_lane,
                "pre_active_accepted_count": pre_gate.get(
                    "active_accepted_count",
                    0,
                ),
                "pre_active_usable_count": pre_gate.get(
                    "active_usable_count",
                    0,
                ),
                "post_active_accepted_count": len(active_accepted),
                "post_active_usable_count": len(active_usable),
                "post_inactive_accepted_count": len(inactive_accepted),
                "broad_required_count": gate["broad_required_count"],
                "broad_accepted_shortfall": gate[
                    "broad_accepted_shortfall"
                ],
                "best_accepted_niche_archetype": gate[
                    "best_accepted_niche_archetype"
                ],
                "best_accepted_niche_required_count": gate[
                    "best_accepted_niche_required_count"
                ],
                "best_accepted_niche_count": gate[
                    "best_accepted_niche_count"
                ],
                "best_accepted_niche_shortfall": gate[
                    "best_accepted_niche_shortfall"
                ],
                "accepted_breadth_gate_pass": int(breadth_pass),
                "accepted_review_pair_count": len(accepted_decisions),
                "rejected_review_pair_count": len(rejected_decisions),
                "deferred_review_pair_count": len(deferred_decisions),
                "accepted_validation_rate": precision_rate,
                "evidence_precision_gate_status": precision_status,
                "covered_issuer_median_accepted_period_count": round(
                    median_periods,
                    4,
                ),
                "covered_issuer_median_history_span_years": round(
                    median_span,
                    4,
                ),
                "historical_depth_gate_pass": int(history_pass),
                "survivor_bias_status": survivor_status,
                "formal_calibration_gate_pass": int(formal_pass),
                "metric_disposition": disposition,
                "disposition_reason": disposition_reason,
            }
        )
    return output
