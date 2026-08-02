from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

from industrials.core.config import load_yaml
from industrials.core.oos_research import finite_float
from industrials.transportation.financial_contract import MetricDefinition


SCORE_ENGINE_VERSION = "transportation_surface_freight_fixed_denominator_v2"
METRIC_SCORE_PREFIX = "metric_score__"
OBSERVED_STATUSES = frozenset({"REPORTED", "DERIVED", "PROXY"})
COMPONENT_FIELD = {
    "market_trend": "market_trend_score",
    "quality": "quality_score",
    "growth": "growth_score",
    "valuation": "valuation_score",
    "operating_efficiency": "operating_efficiency_score",
    "capital_risk": "capital_risk_score",
    "development_stage_risk": "development_stage_risk_score",
    "positioning": "positioning_score",
}


def _quantile(sorted_values: Sequence[float], fraction: float) -> float:
    if not sorted_values:
        raise ValueError("quantile requires at least one value")
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    position = (len(sorted_values) - 1) * fraction
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return float(sorted_values[lower])
    weight = position - lower
    return (
        float(sorted_values[lower]) * (1.0 - weight)
        + float(sorted_values[upper]) * weight
    )


def percentile_scores(
    values: Mapping[str, float],
    *,
    winsor_lower: float = 0.05,
    winsor_upper: float = 0.95,
) -> dict[str, float]:
    """Exact copy of the legacy percentile math, kept dependency-free."""
    finite = {
        str(key): float(value)
        for key, value in values.items()
        if math.isfinite(float(value))
    }
    if not finite:
        return {}
    ordered = sorted(finite.values())
    low = _quantile(ordered, winsor_lower)
    high = _quantile(ordered, winsor_upper)
    clipped = {
        key: min(high, max(low, value))
        for key, value in finite.items()
    }
    unique = sorted(set(clipped.values()))
    if len(unique) == 1:
        return {key: 50.0 for key in clipped}
    positions = {value: index for index, value in enumerate(unique)}
    scale = 100.0 / (len(unique) - 1)
    return {key: positions[value] * scale for key, value in clipped.items()}


def metric_score_field(metric_id: str) -> str:
    return f"{METRIC_SCORE_PREFIX}{metric_id}"


def load_surface_freight_score_policy(path: Path) -> dict[str, Any]:
    payload = load_yaml(path)
    required = {
        "policy_version",
        "score_engine_version",
        "cohort_id",
        "calibration_pool",
        "required_risk_tier",
        "required_portfolio_role",
        "included_economic_peer_groups",
        "metric_comparison_groups",
        "minimum_active_cohort_size",
        "eligible_tickers",
        "score_construction",
        "candidate_component_weights",
        "specialized_metric_dispositions",
        "governance",
    }
    missing = sorted(required - set(payload))
    if missing:
        raise ValueError(f"{path}: missing score-policy fields={missing}")
    if str(payload["score_engine_version"]) != SCORE_ENGINE_VERSION:
        raise ValueError(
            f"{path}: unsupported score_engine_version="
            f"{payload['score_engine_version']!r}"
        )
    eligible = [str(item).upper() for item in payload["eligible_tickers"]]
    if len(eligible) != len(set(eligible)):
        raise ValueError(f"{path}: duplicate eligible tickers")
    if len(eligible) < int(payload["minimum_active_cohort_size"]):
        raise ValueError(f"{path}: eligible cohort is below its minimum size")
    governance = payload["governance"]
    if governance.get("membership_selection_uses_outcomes") is not False:
        raise ValueError("surface-freight membership must be outcome blind")
    if governance.get("promotion_from_revealed_holdout_allowed") is not False:
        raise ValueError("revealed holdout must remain promotion-ineligible")

    construction = payload["score_construction"]
    neutral = float(construction.get("neutral_missing_score"))
    if not math.isfinite(neutral) or not 0.0 <= neutral <= 100.0:
        raise ValueError("neutral_missing_score must be within 0..100")
    recipes = construction.get("component_metric_weights") or {}
    default = recipes.get("default") or {}
    expected = {
        "market_trend",
        "quality",
        "growth",
        "valuation",
        "operating_efficiency",
        "capital_risk",
    }
    if set(default) != expected:
        raise ValueError(
            "default component recipe must define exactly "
            f"{sorted(expected)}"
        )
    for recipe_id, recipe in recipes.items():
        for component, weights in recipe.items():
            values = {str(key): float(value) for key, value in weights.items()}
            if not values or any(value < 0.0 for value in values.values()):
                raise ValueError(f"{recipe_id}/{component}: invalid metric weights")
            if not math.isclose(sum(values.values()), 1.0, abs_tol=1e-9):
                raise ValueError(f"{recipe_id}/{component}: weights must sum to 1")

    disposition = {
        str(metric_id): str(status)
        for metric_id, status in payload["specialized_metric_dispositions"].items()
    }
    retained = {
        str(item)
        for item in construction.get("retained_specialized_metrics", [])
    }
    if retained != {"operating_ratio"}:
        raise ValueError("operating_ratio must be the sole retained specialized metric")
    if disposition.get("operating_ratio") != "CALIBRATION_CANDIDATE":
        raise ValueError("operating_ratio disposition must be CALIBRATION_CANDIDATE")
    if any(
        status == "CALIBRATION_CANDIDATE" and metric_id not in retained
        for metric_id, status in disposition.items()
    ):
        raise ValueError("unretained specialized metric marked calibration candidate")

    candidates = candidate_registry_from_policy(payload, positioning_enabled=True)
    if not 2 <= len(candidates) <= 3:
        raise ValueError("score policy must pre-register two or three candidates")
    return payload


def surface_freight_score_eligible(
    row: Mapping[str, object],
    policy: Mapping[str, Any],
) -> bool:
    ticker = str(row.get("ticker") or "").upper()
    eligible_tickers = {
        str(item).upper() for item in policy.get("eligible_tickers", [])
    }
    excluded = {
        str(item).upper() for item in (policy.get("excluded_tickers") or {})
    }
    return (
        (not eligible_tickers or ticker in eligible_tickers)
        and ticker not in excluded
        and str(row.get("calibration_pool") or "")
        == str(policy["calibration_pool"])
        and str(row.get("risk_tier") or "")
        == str(policy["required_risk_tier"])
        and str(row.get("portfolio_role") or "")
        == str(policy["required_portfolio_role"])
        and str(row.get("economic_peer_group") or "")
        in {str(item) for item in policy["included_economic_peer_groups"]}
    )


def metric_comparison_group(
    row: Mapping[str, object],
    policy: Mapping[str, Any],
) -> str:
    peer = str(row.get("economic_peer_group") or "")
    matches = [
        str(group)
        for group, peers in policy["metric_comparison_groups"].items()
        if peer in {str(item) for item in peers}
    ]
    if len(matches) != 1:
        raise ValueError(
            f"{row.get('ticker')}: expected one metric comparison group for {peer!r}"
        )
    return matches[0]


def _metric_payload(
    row: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, str]]:
    values_raw = row.get("metric_values_json")
    statuses_raw = row.get("metric_status_json")
    try:
        values = (
            dict(values_raw)
            if isinstance(values_raw, Mapping)
            else json.loads(str(values_raw or "{}"))
        )
        statuses = (
            dict(statuses_raw)
            if isinstance(statuses_raw, Mapping)
            else json.loads(str(statuses_raw or "{}"))
        )
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"invalid PIT metric JSON for {row.get('asof_date')}/{row.get('ticker')}"
        ) from exc
    if not isinstance(values, dict) or not isinstance(statuses, dict):
        raise ValueError("PIT metric payloads must be JSON objects")
    return values, {str(key): str(value) for key, value in statuses.items()}


def score_surface_metric_percentiles(
    rows: Sequence[Mapping[str, object]],
    *,
    definitions: Sequence[MetricDefinition],
    policy: Mapping[str, Any],
) -> list[dict[str, object]]:
    """One percentile implementation shared by production and research."""
    output = [
        dict(row) for row in rows if surface_freight_score_eligible(row, policy)
    ]
    by_date: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in output:
        by_date[str(row.get("asof_date") or "")].append(row)
    for asof, date_rows in by_date.items():
        if len({str(row.get("ticker") or "") for row in date_rows}) != len(date_rows):
            raise ValueError(f"duplicate surface-freight ticker rows at {asof}")
        payloads = {
            str(row.get("ticker") or ""): _metric_payload(row)
            for row in date_rows
        }
        rows_by_ticker = {
            str(row.get("ticker") or ""): row for row in date_rows
        }
        for definition in definitions:
            values_by_group: dict[str, dict[str, float]] = defaultdict(dict)
            for ticker, row in rows_by_ticker.items():
                if definition.birthdate and asof < definition.birthdate:
                    continue
                if not definition.applies_to(
                    cohort=str(row.get("calibration_cohort") or ""),
                    industry=str(row.get("industry") or ""),
                ):
                    continue
                values, statuses = payloads[ticker]
                if statuses.get(definition.metric_id) not in OBSERVED_STATUSES:
                    continue
                value = finite_float(values.get(definition.metric_id))
                if value is not None:
                    group = metric_comparison_group(row, policy)
                    values_by_group[group][ticker] = value
            for values in values_by_group.values():
                scores = percentile_scores(
                    values,
                    winsor_lower=definition.winsor_lower,
                    winsor_upper=definition.winsor_upper,
                )
                for ticker, score in scores.items():
                    rows_by_ticker[ticker][metric_score_field(definition.metric_id)] = (
                        score if definition.direction == 1 else 100.0 - score
                    )
    return output


def component_metric_recipe(
    comparison_group: str,
    policy: Mapping[str, Any],
) -> dict[str, dict[str, float]]:
    recipes = policy["score_construction"]["component_metric_weights"]
    output = {
        str(component): {
            str(metric_id): float(weight)
            for metric_id, weight in weights.items()
        }
        for component, weights in recipes["default"].items()
    }
    for component, weights in (recipes.get(comparison_group) or {}).items():
        output[str(component)] = {
            str(metric_id): float(weight)
            for metric_id, weight in weights.items()
        }
    return output


def build_surface_component_scores(
    row: Mapping[str, object],
    *,
    policy: Mapping[str, Any],
) -> tuple[dict[str, float], dict[str, dict[str, int]]]:
    """Build fixed-denominator components; unavailable optional slots are neutral."""
    group = metric_comparison_group(row, policy)
    recipe = component_metric_recipe(group, policy)
    neutral = float(policy["score_construction"]["neutral_missing_score"])
    values: dict[str, float] = {}
    coverage: dict[str, dict[str, int]] = {}
    for component, weights in recipe.items():
        observed = 0
        score = 0.0
        for metric_id, weight in weights.items():
            value = finite_float(row.get(metric_score_field(metric_id)))
            if value is None:
                value = neutral
            else:
                observed += 1
            score += value * weight
        values[COMPONENT_FIELD[component]] = score
        coverage[component] = {
            "observed": observed,
            "applicable": len(weights),
        }
    return values, coverage


def candidate_registry_from_policy(
    policy: Mapping[str, Any],
    *,
    positioning_enabled: bool,
) -> dict[str, dict[str, float]]:
    requirements = {
        str(key): {str(item) for item in value}
        for key, value in (policy.get("candidate_requirements") or {}).items()
    }
    output: dict[str, dict[str, float]] = {}
    for candidate_id, component_weights in policy["candidate_component_weights"].items():
        required = requirements.get(str(candidate_id), set())
        if "positioning_history" in required and not positioning_enabled:
            continue
        weights = {field: 0.0 for field in COMPONENT_FIELD.values()}
        for component, weight in component_weights.items():
            component = str(component)
            if component not in COMPONENT_FIELD:
                raise ValueError(f"{candidate_id}: unknown component={component}")
            weights[COMPONENT_FIELD[component]] = float(weight)
        if any(value < 0.0 for value in weights.values()) or not math.isclose(
            sum(weights.values()), 1.0, abs_tol=1e-9
        ):
            raise ValueError(f"{candidate_id}: component weights must sum to 1")
        if weights["development_stage_risk_score"] != 0.0:
            raise ValueError(f"{candidate_id}: development-stage risk must be zero")
        output[str(candidate_id)] = weights
    return output
