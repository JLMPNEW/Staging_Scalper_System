from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Any, Mapping, Sequence

from industrials.core.config import load_yaml
from industrials.core.oos_research import finite_float, spearman, weighted_score
from industrials.transportation.financial_contract import MetricDefinition
from industrials.transportation.surface_freight_score_engine import (
    metric_comparison_group as shared_metric_comparison_group,
    score_surface_metric_percentiles,
    surface_freight_score_eligible,
)


METRIC_SCORE_PREFIX = "metric_score__"
POSITIONING_RESEARCH_METRIC_ID = "positioning_composite"


def positioning_research_definition() -> MetricDefinition:
    return MetricDefinition(
        metric_id=POSITIONING_RESEARCH_METRIC_ID,
        component="positioning",
        source="market",
        source_field="positioning_score",
        formula="",
        candidate_metric="",
        direction=1,
        cohorts=("surface_freight_and_logistics",),
        industries=(),
        required_for_rank=False,
        specialized=False,
        unit="percentile_score",
        minimum_history_days=0,
        winsor_lower=0.05,
        winsor_upper=0.95,
        birthdate="",
        production_status="research_only",
    )


def add_positioning_research_scores(
    rows: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    """Expose the already-PIT, direction-adjusted component to train-only research."""
    field = metric_score_field(POSITIONING_RESEARCH_METRIC_ID)
    output: list[dict[str, object]] = []
    for source in rows:
        row = dict(source)
        score = finite_float(row.get("positioning_score"))
        if score is not None:
            row[field] = score
        output.append(row)
    return output


def load_surface_freight_policy(path: Path) -> dict[str, Any]:
    payload = load_yaml(path)
    required = {
        "policy_version",
        "cohort_id",
        "calibration_pool",
        "required_risk_tier",
        "required_portfolio_role",
        "included_economic_peer_groups",
        "minimum_active_cohort_size",
        "metric_research_gates",
        "governance",
    }
    missing = sorted(required - set(payload))
    if missing:
        raise ValueError(f"{path}: missing policy fields={missing}")
    if int(payload["minimum_active_cohort_size"]) < 20:
        raise ValueError("surface-freight research cohort must require at least 20 names")
    governance = payload["governance"]
    if governance.get("membership_selection_uses_outcomes") is not False:
        raise ValueError("cohort membership must be outcome blind")
    if governance.get("promotion_from_revealed_holdout_allowed") is not False:
        raise ValueError("revealed holdout must remain ineligible for promotion")
    return payload


def surface_freight_cohort_eligible(
    row: Mapping[str, object],
    policy: Mapping[str, Any],
) -> bool:
    return surface_freight_score_eligible(row, policy)


def metric_score_field(metric_id: str) -> str:
    return f"{METRIC_SCORE_PREFIX}{metric_id}"


def mean_reversion_score_field(metric_id: str) -> str:
    return f"{METRIC_SCORE_PREFIX}{metric_id}__mean_reversion"


def metric_comparison_group(
    row: Mapping[str, object],
    policy: Mapping[str, Any],
) -> str:
    return shared_metric_comparison_group(row, policy)


def _metric_payload(
    row: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, str]]:
    try:
        values = json.loads(str(row.get("metric_values_json") or "{}"))
        statuses = json.loads(str(row.get("metric_status_json") or "{}"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"invalid PIT metric JSON for {row.get('asof_date')}/{row.get('ticker')}"
        ) from exc
    if not isinstance(values, dict) or not isinstance(statuses, dict):
        raise ValueError("PIT metric payloads must be JSON objects")
    return values, {str(key): str(value) for key, value in statuses.items()}


def build_directional_metric_scores(
    rows: Sequence[Mapping[str, object]],
    *,
    definitions: Sequence[MetricDefinition],
    policy: Mapping[str, Any],
) -> list[dict[str, object]]:
    """Re-rank PIT metrics through the same engine used by serving."""
    output = score_surface_metric_percentiles(
        rows,
        definitions=definitions,
        policy=policy,
    )
    mean_reversion_ids = {
        str(item)
        for item in policy.get("metric_research_gates", {}).get(
            "mean_reversion_metric_ids", []
        )
    }
    for row in output:
        for metric_id in mean_reversion_ids:
            directional_score = finite_float(row.get(metric_score_field(metric_id)))
            if directional_score is not None:
                row[mean_reversion_score_field(metric_id)] = 100.0 - directional_score
    return output


def metric_ic_diagnostics(
    rows: Sequence[Mapping[str, object]],
    *,
    definitions: Sequence[MetricDefinition],
    split: str,
    subperiod_count: int,
    minimum_cross_section: int,
) -> list[dict[str, object]]:
    eligible = [
        row
        for row in rows
        if str(row.get("split") or "") == split
        and str(row.get("calibration_eligible_flag") or "") == "1"
        and str(row.get("outcome_available_flag") or "") == "1"
        and str(row.get("horizon_sessions") or "") == "63"
    ]
    dates = sorted({str(row.get("asof_date") or "") for row in eligible})
    date_bucket = {
        asof: min(
            subperiod_count - 1,
            int(index * subperiod_count / max(1, len(dates))),
        )
        for index, asof in enumerate(dates)
    }
    output: list[dict[str, object]] = []
    for definition in definitions:
        field = metric_score_field(definition.metric_id)
        applicable = [
            row
            for row in eligible
            if definition.applies_to(
                cohort=str(row.get("calibration_cohort") or ""),
                industry=str(row.get("industry") or ""),
            )
            and (not definition.birthdate or str(row.get("asof_date") or "") >= definition.birthdate)
        ]
        observed = [row for row in applicable if finite_float(row.get(field)) is not None]
        by_date: dict[str, list[Mapping[str, object]]] = defaultdict(list)
        for row in observed:
            by_date[str(row.get("asof_date") or "")].append(row)
        period_ics: list[tuple[str, float]] = []
        for asof, members in by_date.items():
            if len(members) < minimum_cross_section:
                continue
            value = spearman(
                [float(row[field]) for row in members],
                [float(row["forward_excess_return"]) for row in members],
            )
            if value is not None:
                period_ics.append((asof, value))
        subperiod_ics: list[float | None] = []
        for bucket in range(subperiod_count):
            values = [
                value
                for asof, value in period_ics
                if date_bucket.get(asof) == bucket
            ]
            subperiod_ics.append(mean(values) if values else None)
        mean_ic = mean(value for _, value in period_ics) if period_ics else None
        output.append(
            {
                "metric_id": definition.metric_id,
                "component": definition.component,
                "specialized": int(definition.specialized),
                "direction": definition.direction,
                "applicable_row_count": len(applicable),
                "observed_row_count": len(observed),
                "observation_coverage": (
                    len(observed) / len(applicable) if applicable else 0.0
                ),
                "ic_snapshot_count": len(period_ics),
                "mean_ic": mean_ic,
                "positive_ic_snapshot_rate": (
                    sum(value > 0 for _, value in period_ics) / len(period_ics)
                    if period_ics
                    else 0.0
                ),
                "subperiod_ics": subperiod_ics,
                "positive_subperiod_count": sum(
                    value is not None and value > 0 for value in subperiod_ics
                ),
            }
        )
    return output


def select_train_metrics(
    diagnostics: Sequence[Mapping[str, object]],
    *,
    policy: Mapping[str, Any],
) -> list[dict[str, object]]:
    gates = policy["metric_research_gates"]
    eligible: list[dict[str, object]] = []
    for source in diagnostics:
        row = dict(source)
        failures: list[str] = []
        mean_ic = finite_float(row.get("mean_ic"))
        if float(row.get("observation_coverage") or 0.0) < float(
            gates["minimum_train_observation_coverage"]
        ):
            failures.append("coverage")
        if int(row.get("ic_snapshot_count") or 0) < int(
            gates["minimum_train_ic_snapshot_count"]
        ):
            failures.append("history")
        if mean_ic is None or mean_ic <= 0:
            failures.append("nonpositive_train_ic")
        if int(row.get("positive_subperiod_count") or 0) < int(
            gates["minimum_positive_train_subperiods"]
        ):
            failures.append("subperiod_instability")
        row["selection_status"] = "PASS" if not failures else "FAIL"
        row["selection_failures"] = ";".join(failures)
        row["selection_strength"] = (
            float(mean_ic)
            * float(row.get("observation_coverage") or 0.0)
            * (1.0 + 0.1 * int(row.get("positive_subperiod_count") or 0))
            if not failures and mean_ic is not None
            else 0.0
        )
        eligible.append(row)
    passing = sorted(
        (row for row in eligible if row["selection_status"] == "PASS"),
        key=lambda row: (-float(row["selection_strength"]), str(row["metric_id"])),
    )
    selected: list[dict[str, object]] = []
    component_counts: defaultdict[str, int] = defaultdict(int)
    for row in passing:
        component = str(row["component"])
        if component_counts[component] >= int(gates["maximum_metrics_per_component"]):
            continue
        selected.append(row)
        component_counts[component] += 1
        if len(selected) >= int(gates["maximum_candidate_metric_count"]):
            break
    return selected


def select_train_mean_reversion_metrics(
    diagnostics: Sequence[Mapping[str, object]],
    *,
    policy: Mapping[str, Any],
) -> list[dict[str, object]]:
    gates = policy["metric_research_gates"]
    allowed = {
        str(item) for item in gates.get("mean_reversion_metric_ids", [])
    }
    output: list[dict[str, object]] = []
    for source in diagnostics:
        if str(source.get("metric_id") or "") not in allowed:
            continue
        mean_ic = finite_float(source.get("mean_ic"))
        subperiods = [
            finite_float(value) for value in source.get("subperiod_ics", [])
        ]
        negative_subperiods = sum(
            value is not None and value < 0 for value in subperiods
        )
        if (
            float(source.get("observation_coverage") or 0.0)
            >= float(gates["minimum_train_observation_coverage"])
            and int(source.get("ic_snapshot_count") or 0)
            >= int(gates["minimum_train_ic_snapshot_count"])
            and mean_ic is not None
            and mean_ic < 0
            and negative_subperiods
            >= int(gates["minimum_negative_train_subperiods"])
        ):
            row = dict(source)
            row["negative_subperiod_count"] = negative_subperiods
            row["selection_strength"] = (
                abs(mean_ic)
                * float(row.get("observation_coverage") or 0.0)
                * (1.0 + 0.1 * negative_subperiods)
            )
            output.append(row)
    return sorted(
        output,
        key=lambda row: (-float(row["selection_strength"]), str(row["metric_id"])),
    )


def _capped_normalize(
    values: Mapping[str, float],
    *,
    cap: float,
) -> dict[str, float]:
    if not values or any(float(value) <= 0 for value in values.values()):
        raise ValueError("candidate strengths must be positive")
    if len(values) * cap < 1.0 - 1e-12:
        raise ValueError("single-metric cap is infeasible")
    remaining = dict(values)
    output: dict[str, float] = {}
    residual = 1.0
    while remaining:
        total = sum(remaining.values())
        provisional = {
            key: residual * value / total for key, value in remaining.items()
        }
        capped = [key for key, value in provisional.items() if value > cap]
        if not capped:
            output.update(provisional)
            break
        for key in capped:
            output[key] = cap
            residual -= cap
            remaining.pop(key)
    return output


def train_derived_candidate_registry(
    selected_metrics: Sequence[Mapping[str, object]],
    *,
    policy: Mapping[str, Any],
    mean_reversion_metrics: Sequence[Mapping[str, object]] = (),
) -> dict[str, dict[str, float]]:
    gates = policy["metric_research_gates"]
    minimum = int(gates["minimum_candidate_metric_count"])
    if len(selected_metrics) < minimum:
        return {}
    fields = {
        metric_score_field(str(row["metric_id"])): row
        for row in selected_metrics
    }
    equal = {field: 1.0 / len(fields) for field in fields}
    proportional = _capped_normalize(
        {
            field: float(row["selection_strength"])
            for field, row in fields.items()
        },
        cap=float(gates["maximum_single_metric_weight"]),
    )
    by_component: dict[str, list[str]] = defaultdict(list)
    for field, row in fields.items():
        by_component[str(row["component"])].append(field)
    component_balanced = {
        field: 1.0 / len(by_component) / len(component_fields)
        for component_fields in by_component.values()
        for field in component_fields
    }
    candidates = {
        "train_ic_equal": equal,
        "train_ic_proportional": proportional,
        "train_ic_component_balanced": component_balanced,
    }
    if mean_reversion_metrics:
        reversion_fields = {
            mean_reversion_score_field(str(row["metric_id"])): row
            for row in mean_reversion_metrics
        }
        combined_count = len(fields) + len(reversion_fields)
        candidates["fundamental_plus_mean_reversion_equal"] = {
            field: 1.0 / combined_count
            for field in [*fields, *reversion_fields]
        }
        reversion_weight = float(gates["mean_reversion_candidate_weight"])
        candidates["fundamental_plus_mean_reversion_bounded"] = {
            **{
                field: (1.0 - reversion_weight) / len(fields)
                for field in fields
            },
            **{
                field: reversion_weight / len(reversion_fields)
                for field in reversion_fields
            },
        }
    return candidates


def top_bottom_diagnostic(
    rows: Sequence[Mapping[str, object]],
    *,
    weights: Mapping[str, float],
    split: str,
    top_fraction: float,
    minimum_cross_section: int,
) -> dict[str, object]:
    by_date: dict[str, list[tuple[Mapping[str, object], float]]] = defaultdict(list)
    for row in rows:
        if (
            str(row.get("split") or "") != split
            or str(row.get("calibration_eligible_flag") or "") != "1"
            or str(row.get("outcome_available_flag") or "") != "1"
            or str(row.get("horizon_sessions") or "") != "63"
        ):
            continue
        score = weighted_score(row, weights, require_complete=True)
        outcome = finite_float(row.get("forward_excess_return"))
        if score is not None and outcome is not None:
            by_date[str(row.get("asof_date") or "")].append((row, score))
    periods: list[dict[str, float]] = []
    for asof in sorted(by_date):
        members = by_date[asof]
        if len(members) < minimum_cross_section:
            continue
        count = min(
            len(members) // 2,
            max(1, int(math.ceil(len(members) * top_fraction))),
        )
        ordered = sorted(
            members,
            key=lambda item: (-item[1], str(item[0].get("ticker") or "")),
        )
        top = ordered[:count]
        bottom = ordered[-count:]
        top_return = mean(float(row["forward_excess_return"]) for row, _ in top)
        bottom_return = mean(float(row["forward_excess_return"]) for row, _ in bottom)
        periods.append(
            {
                "asof_date": asof,
                "top_return": top_return,
                "bottom_return": bottom_return,
                "top_bottom_spread": top_return - bottom_return,
            }
        )
    return {
        "snapshot_count": len(periods),
        "mean_top_return": mean(row["top_return"] for row in periods) if periods else None,
        "mean_bottom_return": mean(row["bottom_return"] for row in periods) if periods else None,
        "mean_top_bottom_spread": mean(row["top_bottom_spread"] for row in periods) if periods else None,
        "positive_spread_rate": (
            sum(row["top_bottom_spread"] > 0 for row in periods) / len(periods)
            if periods
            else None
        ),
    }
