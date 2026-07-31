from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
from statistics import median
from typing import Mapping, Sequence

from industrials.transportation.coverage_lift import (
    build_metric_gate_rows,
)


FINAL_METRIC_FREEZE_VERSION = "transportation_dp6x_final_metric_freeze_v1"

FINAL_METRIC_DISPOSITION_FIELDS = (
    "freeze_version",
    "metric_id",
    "metric_pack",
    "source_lane",
    "applicable_ticker_count",
    "active_applicable_count",
    "inactive_applicable_count",
    "active_accepted_count",
    "active_usable_count",
    "inactive_accepted_count",
    "review_required_count",
    "rejected_count",
    "searched_not_found_count",
    "text_hit_no_value_count",
    "financial_inputs_missing_count",
    "accepted_breadth_gate_pass",
    "accepted_breadth_gate_basis",
    "evidence_precision_gate_pass",
    "evidence_precision_gate_status",
    "covered_issuer_median_accepted_period_count",
    "covered_issuer_median_history_span_years",
    "historical_depth_gate_pass",
    "survivor_bias_status",
    "calibration_candidate",
    "metric_disposition",
    "disposition_reason",
)

ACCEPTED_STATUSES = frozenset(
    {"COVERED_ACCEPTED", "COVERED_FINANCIAL_DERIVED"}
)
USABLE_STATUSES = ACCEPTED_STATUSES | frozenset(
    {"COVERED_REVIEW_REQUIRED"}
)


def _integer(value: object) -> int:
    try:
        return int(str(value or "0"))
    except ValueError:
        return 0


def _date_ordinal(value: object) -> int:
    try:
        return date.fromisoformat(str(value or "")[:10]).toordinal()
    except ValueError:
        return 0


def _history_summary(
    period_sets: Sequence[set[str]],
) -> tuple[float, float]:
    if not period_sets:
        return 0.0, 0.0
    period_counts = [len(periods) for periods in period_sets]
    spans: list[float] = []
    for periods in period_sets:
        ordinals = sorted(
            value for value in map(_date_ordinal, periods) if value
        )
        spans.append(
            (ordinals[-1] - ordinals[0]) / 365.25
            if len(ordinals) >= 2
            else 0.0
        )
    return float(median(period_counts)), float(median(spans))


def build_final_metric_dispositions(
    *,
    coverage_rows: Sequence[Mapping[str, object]],
    policy_golden_validated: bool,
    accepted_periods: Mapping[tuple[str, str], set[str]],
) -> list[dict[str, object]]:
    by_metric: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for row in coverage_rows:
        if str(row.get("applicability_status")) == "APPLICABLE":
            by_metric[str(row["metric_id"])].append(row)
    breadth_gates = {
        str(row["metric_id"]): row
        for row in build_metric_gate_rows(coverage_rows)
    }

    output: list[dict[str, object]] = []
    for metric_id, rows in sorted(by_metric.items()):
        source_lane = str(rows[0].get("source_lane") or "")
        metric_pack = str(rows[0].get("metric_pack") or "")
        active = [
            row
            for row in rows
            if str(row.get("universe_role")) == "active"
        ]
        inactive = [
            row
            for row in rows
            if str(row.get("universe_role")) != "active"
        ]
        active_accepted = [
            row
            for row in active
            if str(row.get("coverage_status")) in ACCEPTED_STATUSES
        ]
        active_usable = [
            row
            for row in active
            if str(row.get("coverage_status")) in USABLE_STATUSES
        ]
        inactive_accepted = [
            row
            for row in inactive
            if str(row.get("coverage_status")) in ACCEPTED_STATUSES
        ]
        status_counts = Counter(
            str(row.get("coverage_status") or "") for row in rows
        )
        breadth = breadth_gates.get(metric_id, {})
        breadth_pass = bool(_integer(breadth.get("accepted_gate_pass")))
        if _integer(breadth.get("broad_accepted_shortfall")) == 0:
            breadth_basis = (
                "broad_active_accepts="
                f"{breadth.get('active_accepted_count', 0)}/"
                f"{breadth.get('broad_required_count', 0)}"
            )
        elif (
            _integer(breadth.get("best_accepted_niche_shortfall"))
            == 0
        ):
            breadth_basis = (
                "niche_active_accepts="
                f"{breadth.get('best_accepted_niche_archetype', '')}:"
                f"{breadth.get('best_accepted_niche_count', 0)}/"
                f"{breadth.get('best_accepted_niche_required_count', 0)}"
            )
        else:
            breadth_basis = (
                "broad_active_accepts="
                f"{breadth.get('active_accepted_count', 0)}/"
                f"{breadth.get('broad_required_count', 0)};"
                "best_niche_shortfall="
                f"{breadth.get('best_accepted_niche_shortfall', 0)}"
            )
        if source_lane == "FIN-D":
            precision_pass = True
            precision_status = "PASS_FINANCIAL_DERIVATION_CONTRACT"
        elif active_accepted and policy_golden_validated:
            precision_pass = True
            precision_status = "PASS_POLICY_REPLAY_GOLDEN_VALIDATED"
        elif active_accepted:
            precision_pass = False
            precision_status = "FAIL_POLICY_REPLAY_GOLDEN_NOT_VALIDATED"
        else:
            precision_pass = False
            precision_status = "NOT_ESTIMABLE_NO_ACCEPTED_EVIDENCE"

        accepted_period_sets = [
            accepted_periods.get(
                (str(row.get("ticker") or ""), metric_id),
                set(),
            )
            for row in [*active_accepted, *inactive_accepted]
        ]
        accepted_period_sets = [
            periods for periods in accepted_period_sets if periods
        ]
        median_periods, median_span = _history_summary(
            accepted_period_sets
        )
        if source_lane == "FIN-D":
            history_pass = False
            history_reason = (
                "financial_derived_history_requires_frozen_pit_panel"
            )
        else:
            history_pass = median_periods >= 4.0 and median_span >= 3.0
            history_reason = (
                "historical_depth_pass"
                if history_pass
                else "median_history_below_four_periods_or_three_years"
            )
        candidate = breadth_pass and precision_pass and history_pass
        if candidate:
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
                        "evidence_precision_gate_failed",
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

        output.append(
            {
                "freeze_version": FINAL_METRIC_FREEZE_VERSION,
                "metric_id": metric_id,
                "metric_pack": metric_pack,
                "source_lane": source_lane,
                "applicable_ticker_count": len(rows),
                "active_applicable_count": len(active),
                "inactive_applicable_count": len(inactive),
                "active_accepted_count": len(active_accepted),
                "active_usable_count": len(active_usable),
                "inactive_accepted_count": len(inactive_accepted),
                "review_required_count": status_counts[
                    "COVERED_REVIEW_REQUIRED"
                ],
                "rejected_count": status_counts["DISCOVERED_REJECTED"],
                "searched_not_found_count": status_counts[
                    "SEARCHED_NOT_FOUND"
                ],
                "text_hit_no_value_count": status_counts[
                    "TEXT_HIT_NO_VALUE"
                ],
                "financial_inputs_missing_count": status_counts[
                    "FINANCIAL_INPUTS_MISSING"
                ],
                "accepted_breadth_gate_pass": int(breadth_pass),
                "accepted_breadth_gate_basis": breadth_basis,
                "evidence_precision_gate_pass": int(precision_pass),
                "evidence_precision_gate_status": precision_status,
                "covered_issuer_median_accepted_period_count": round(
                    median_periods, 4
                ),
                "covered_issuer_median_history_span_years": round(
                    median_span, 4
                ),
                "historical_depth_gate_pass": int(history_pass),
                "survivor_bias_status": (
                    "PASS_INACTIVE_EVIDENCE_PRESENT"
                    if inactive_accepted
                    else "LIMITATION_NO_ACCEPTED_INACTIVE_EVIDENCE"
                ),
                "calibration_candidate": int(candidate),
                "metric_disposition": disposition,
                "disposition_reason": disposition_reason,
            }
        )
    return output
