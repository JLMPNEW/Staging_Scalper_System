from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from industrials.core.oos_research import normalized_weights
from industrials.transportation.contracts import COMPONENT_FIELDS


STRUCTURALLY_EXCLUDED_BY_POLICY = {
    "operating_core_only": frozenset({"development_stage_risk_score"}),
}


def candidate_registry(
    baseline: Mapping[str, float],
) -> dict[str, dict[str, float]]:
    raw: dict[str, Mapping[str, float]] = {
        "baseline_frozen": baseline,
        "equal_nonzero": {
            field: 1.0
            for field in COMPONENT_FIELDS
            if field != "positioning_score"
        },
        "market_quality": {
            "market_trend_score": 0.35,
            "quality_score": 0.25,
            "growth_score": 0.10,
            "valuation_score": 0.05,
            "operating_efficiency_score": 0.10,
            "capital_risk_score": 0.10,
            "development_stage_risk_score": 0.05,
        },
        "quality_efficiency": {
            "market_trend_score": 0.20,
            "quality_score": 0.25,
            "growth_score": 0.10,
            "valuation_score": 0.10,
            "operating_efficiency_score": 0.20,
            "capital_risk_score": 0.10,
            "development_stage_risk_score": 0.05,
        },
        "risk_control": {
            "market_trend_score": 0.20,
            "quality_score": 0.20,
            "growth_score": 0.05,
            "valuation_score": 0.10,
            "operating_efficiency_score": 0.15,
            "capital_risk_score": 0.25,
            "development_stage_risk_score": 0.05,
        },
        "growth_quality": {
            "market_trend_score": 0.25,
            "quality_score": 0.20,
            "growth_score": 0.20,
            "valuation_score": 0.05,
            "operating_efficiency_score": 0.15,
            "capital_risk_score": 0.10,
            "development_stage_risk_score": 0.05,
        },
        "balanced_value": {
            "market_trend_score": 0.20,
            "quality_score": 0.15,
            "growth_score": 0.10,
            "valuation_score": 0.20,
            "operating_efficiency_score": 0.15,
            "capital_risk_score": 0.15,
            "development_stage_risk_score": 0.05,
        },
    }
    excluded = STRUCTURALLY_EXCLUDED_BY_POLICY["operating_core_only"]
    operating_core = {
        name: {field: weight for field, weight in weights.items() if field not in excluded}
        for name, weights in raw.items()
    }
    return {
        name: normalized_weights(COMPONENT_FIELDS, weights)
        for name, weights in operating_core.items()
    }


def _finite(value: object) -> bool:
    try:
        return math.isfinite(float(str(value).strip()))
    except (TypeError, ValueError):
        return False


def audit_candidate_component_coverage(
    rows: Sequence[Mapping[str, object]],
    *,
    candidates: Mapping[str, Mapping[str, float]],
    horizon_sessions: int,
    production_universe_policy: str,
    minimum_complete_row_coverage: float,
    splits: frozenset[str] = frozenset({"train", "validation"}),
) -> dict[str, Any]:
    if not 0.0 <= minimum_complete_row_coverage <= 1.0:
        raise ValueError("minimum_complete_row_coverage must be within 0..1")
    eligible = [
        row
        for row in rows
        if str(row.get("split") or "") in splits
        and str(row.get("calibration_eligible_flag") or "") == "1"
        and str(row.get("outcome_available_flag") or "") == "1"
        and int(float(str(row.get("horizon_sessions") or 0)))
        == horizon_sessions
    ]
    excluded = STRUCTURALLY_EXCLUDED_BY_POLICY.get(
        production_universe_policy,
        frozenset(),
    )
    report_rows: list[dict[str, object]] = []
    candidate_results: dict[str, dict[str, object]] = {}
    issues: list[str] = []
    for candidate_id, weights in candidates.items():
        positive = {
            str(field): float(weight)
            for field, weight in weights.items()
            if float(weight) > 0.0
        }
        structural = sorted(set(positive) & set(excluded))
        complete = sum(
            all(_finite(row.get(field)) for field in positive)
            for row in eligible
        )
        complete_coverage = complete / len(eligible) if eligible else 0.0
        candidate_issues: list[str] = []
        if structural:
            candidate_issues.append(
                "positive_weight_structurally_excluded_components="
                + ",".join(structural)
            )
        if complete_coverage < minimum_complete_row_coverage:
            candidate_issues.append(
                "complete_row_coverage_below_minimum="
                f"{complete_coverage:.6f}<{minimum_complete_row_coverage:.6f}"
            )
        for field, weight in sorted(positive.items()):
            available = sum(_finite(row.get(field)) for row in eligible)
            coverage = available / len(eligible) if eligible else 0.0
            reason = (
                "structurally_excluded_but_positive_weight"
                if field in excluded
                else "coverage_below_minimum"
                if coverage < minimum_complete_row_coverage
                else "ok"
            )
            status = "FAIL" if reason != "ok" else "PASS"
            if status == "FAIL" and field not in structural:
                candidate_issues.append(f"{field}_coverage={coverage:.6f}")
            report_rows.append(
                {
                    "candidate_id": candidate_id,
                    "component_field": field,
                    "configured_weight": weight,
                    "eligible_row_count": len(eligible),
                    "available_row_count": available,
                    "coverage": coverage,
                    "status": status,
                    "reason": reason,
                }
            )
        candidate_results[candidate_id] = {
            "status": "PASS" if not candidate_issues else "FAIL",
            "eligible_row_count": len(eligible),
            "complete_row_count": complete,
            "complete_row_coverage": complete_coverage,
            "issues": candidate_issues,
        }
        issues.extend(f"{candidate_id}:{issue}" for issue in candidate_issues)
    return {
        "acceptance": "PASS" if not issues else "FAIL",
        "horizon_sessions": horizon_sessions,
        "production_universe_policy": production_universe_policy,
        "structurally_excluded_components": sorted(excluded),
        "minimum_complete_row_coverage": minimum_complete_row_coverage,
        "eligible_row_count": len(eligible),
        "candidate_results": candidate_results,
        "issues": issues,
        "report_rows": report_rows,
    }
