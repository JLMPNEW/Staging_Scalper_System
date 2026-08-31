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
COHORT_SCORE_ENGINE_VERSION = "transportation_cohort_fixed_denominator_v3"
SUPPORTED_SCORE_ENGINE_VERSIONS = frozenset(
    {SCORE_ENGINE_VERSION, COHORT_SCORE_ENGINE_VERSION}
)
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
    """Winsorized average-rank percentiles with deterministic ties."""
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
    if len(clipped) == 1:
        return {key: 50.0 for key in clipped}
    ordered_items = sorted(clipped.items(), key=lambda item: (item[1], item[0]))
    ranks: dict[str, float] = {}
    cursor = 0
    while cursor < len(ordered_items):
        end = cursor + 1
        while (
            end < len(ordered_items)
            and ordered_items[end][1] == ordered_items[cursor][1]
        ):
            end += 1
        average_rank = (cursor + end - 1) / 2.0
        for key, _value in ordered_items[cursor:end]:
            ranks[key] = average_rank
        cursor = end
    scale = 100.0 / (len(ordered_items) - 1)
    return {key: ranks[key] * scale for key in clipped}


def metric_score_field(metric_id: str) -> str:
    return f"{METRIC_SCORE_PREFIX}{metric_id}"


def load_cohort_score_policy(path: Path) -> dict[str, Any]:
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
    if str(payload["score_engine_version"]) not in SUPPORTED_SCORE_ENGINE_VERSIONS:
        raise ValueError(
            f"{path}: unsupported score_engine_version="
            f"{payload['score_engine_version']!r}"
        )
    eligible = [str(item).upper() for item in payload["eligible_tickers"]]
    if len(eligible) != len(set(eligible)):
        raise ValueError(f"{path}: duplicate eligible tickers")
    if len(eligible) < int(payload["minimum_active_cohort_size"]):
        raise ValueError(f"{path}: eligible cohort is below its minimum size")
    comparison_group_tickers = payload.get("comparison_group_tickers") or {}
    if comparison_group_tickers:
        assigned: list[str] = []
        for group, raw_tickers in comparison_group_tickers.items():
            tickers = [str(item).upper() for item in raw_tickers]
            if not str(group) or not tickers or len(tickers) != len(set(tickers)):
                raise ValueError(f"{path}: invalid comparison_group_tickers={group!r}")
            assigned.extend(tickers)
        if len(assigned) != len(set(assigned)) or set(assigned) != set(eligible):
            raise ValueError(
                f"{path}: comparison_group_tickers must partition eligible_tickers"
            )
    historical_only = payload.get("historical_calibration_only") or {}
    if not isinstance(historical_only, Mapping):
        raise ValueError(f"{path}: historical_calibration_only must be a mapping")
    historical_tickers = {str(ticker).upper() for ticker in historical_only}
    if historical_tickers & set(eligible):
        raise ValueError(
            f"{path}: historical-only tickers overlap current eligible tickers"
        )
    valid_groups = {str(group) for group in comparison_group_tickers}
    for raw_ticker, raw_entry in historical_only.items():
        ticker = str(raw_ticker).upper()
        if not ticker or not isinstance(raw_entry, Mapping):
            raise ValueError(f"{path}: invalid historical-only entry={raw_ticker!r}")
        if str(raw_entry.get("comparison_group") or "") not in valid_groups:
            raise ValueError(
                f"{path}: {ticker} has invalid historical comparison group"
            )
        effective_from = str(raw_entry.get("effective_from") or "")[:10]
        effective_to = str(raw_entry.get("effective_to") or "")[:10]
        if not effective_from or not effective_to or effective_from > effective_to:
            raise ValueError(f"{path}: {ticker} has invalid historical date bounds")
    metric_domains = payload.get("metric_comparison_domains") or {}
    for metric_id, raw_domains in metric_domains.items():
        assigned: list[str] = []
        if not str(metric_id) or not isinstance(raw_domains, Mapping):
            raise ValueError(f"{path}: invalid metric comparison domains")
        for domain_id, raw_tickers in raw_domains.items():
            tickers = [str(item).upper() for item in raw_tickers]
            if not str(domain_id) or not tickers or len(tickers) != len(set(tickers)):
                raise ValueError(
                    f"{path}: invalid metric domain={metric_id}/{domain_id}"
                )
            if not set(tickers) <= set(eligible):
                raise ValueError(
                    f"{path}: metric domain ticker outside eligible set={metric_id}/{domain_id}"
                )
            assigned.extend(tickers)
        if len(assigned) != len(set(assigned)):
            raise ValueError(f"{path}: overlapping metric domains for {metric_id}")
    governance = payload["governance"]
    if governance.get("membership_selection_uses_outcomes") is not False:
        raise ValueError("surface-freight membership must be outcome blind")
    if governance.get("promotion_from_revealed_holdout_allowed") is not False:
        raise ValueError("revealed holdout must remain promotion-ineligible")
    if historical_only and governance.get("historical_calibration_role") != (
        "historical_calibration_only_no_portfolio_eligibility"
    ):
        raise ValueError(
            "historical-only membership requires a no-portfolio-eligibility role"
        )

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
    if not retained:
        raise ValueError("at least one specialized metric must be retained")
    if any(
        disposition.get(metric_id) != "CALIBRATION_CANDIDATE"
        for metric_id in retained
    ):
        raise ValueError("every retained specialized metric must be a calibration candidate")
    if any(
        status == "CALIBRATION_CANDIDATE" and metric_id not in retained
        for metric_id, status in disposition.items()
    ):
        raise ValueError("unretained specialized metric marked calibration candidate")

    candidates = candidate_registry_from_policy(payload, positioning_enabled=True)
    if not 2 <= len(candidates) <= 3:
        raise ValueError("score policy must pre-register two or three candidates")
    return payload


def load_surface_freight_score_policy(path: Path) -> dict[str, Any]:
    """Backward-compatible alias for the shared cohort policy loader."""
    return load_cohort_score_policy(path)


def cohort_score_eligible(
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
    historical_entry = (policy.get("historical_calibration_only") or {}).get(ticker)
    if historical_entry is not None:
        asof = str(row.get("asof_date") or "")[:10]
        return (
            str(row.get("_score_membership_mode") or "current") == "pit"
            and ticker not in excluded
            and str(row.get("calibration_use") or "") == "historical_research"
            and str(row.get("calibration_cohort") or "")
            == str(policy["calibration_pool"])
            and str(historical_entry["effective_from"])[:10] <= asof
            <= str(historical_entry["effective_to"])[:10]
        )
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


def surface_freight_score_eligible(
    row: Mapping[str, object],
    policy: Mapping[str, Any],
) -> bool:
    """Backward-compatible alias for the shared cohort eligibility gate."""
    return cohort_score_eligible(row, policy)


def metric_comparison_group(
    row: Mapping[str, object],
    policy: Mapping[str, Any],
) -> str:
    ticker = str(row.get("ticker") or "").upper()
    historical_entry = (policy.get("historical_calibration_only") or {}).get(ticker)
    if historical_entry is not None:
        if str(row.get("_score_membership_mode") or "current") != "pit":
            raise ValueError(f"{ticker}: historical-only group requested outside PIT mode")
        return str(historical_entry["comparison_group"])
    ticker_groups = policy.get("comparison_group_tickers") or {}
    if ticker_groups:
        matches = [
            str(group)
            for group, tickers in ticker_groups.items()
            if ticker in {str(item).upper() for item in tickers}
        ]
        if len(matches) != 1:
            raise ValueError(
                f"{row.get('ticker')}: expected one ticker comparison group"
            )
        return matches[0]
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


def metric_comparison_group_for_metric(
    row: Mapping[str, object],
    policy: Mapping[str, Any],
    metric_id: str,
) -> str | None:
    domains = (policy.get("metric_comparison_domains") or {}).get(metric_id)
    if not domains:
        return metric_comparison_group(row, policy)
    ticker = str(row.get("ticker") or "").upper()
    matches = [
        str(domain)
        for domain, tickers in domains.items()
        if ticker in {str(item).upper() for item in tickers}
    ]
    if len(matches) > 1:
        raise ValueError(
            f"{ticker}: multiple metric comparison domains for {metric_id}"
        )
    return matches[0] if matches else None


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


def score_cohort_metric_percentiles(
    rows: Sequence[Mapping[str, object]],
    *,
    definitions: Sequence[MetricDefinition],
    policy: Mapping[str, Any],
) -> list[dict[str, object]]:
    """One percentile implementation shared by production and research."""
    output = [
        dict(row) for row in rows if cohort_score_eligible(row, policy)
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
                    group = metric_comparison_group_for_metric(
                        row, policy, definition.metric_id
                    )
                    if group is None:
                        continue
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


def score_surface_metric_percentiles(
    rows: Sequence[Mapping[str, object]],
    *,
    definitions: Sequence[MetricDefinition],
    policy: Mapping[str, Any],
) -> list[dict[str, object]]:
    """Backward-compatible alias for shared cohort percentile scoring."""
    return score_cohort_metric_percentiles(
        rows, definitions=definitions, policy=policy
    )


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


def build_cohort_component_scores(
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


def build_surface_component_scores(
    row: Mapping[str, object],
    *,
    policy: Mapping[str, Any],
) -> tuple[dict[str, float], dict[str, dict[str, int]]]:
    """Backward-compatible alias for shared cohort component construction."""
    return build_cohort_component_scores(row, policy=policy)


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
