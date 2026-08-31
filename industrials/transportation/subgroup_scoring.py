from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from industrials.core.config import load_yaml
from industrials.transportation.contemporaneous_metric_coverage import (
    availability_date,
    comparison_key,
)
from industrials.transportation.financial_contract import (
    is_rankable_metric_value,
)
from industrials.transportation.surface_freight_score_engine import percentile_scores


POLICY_VERSION = "transportation_subgroup_score_policy_v8"
OBSERVED_STATUSES = frozenset({"REPORTED", "DERIVED", "PROXY"})
COMPONENTS = (
    "market_trend",
    "quality",
    "growth",
    "valuation",
    "operating_efficiency",
    "capital_risk",
    "positioning",
    "specialized",
)
CHANGE_TRANSFORMS = frozenset({"yoy_growth", "yoy_improvement", "yoy_change"})
SUPPORTED_TRANSFORMS = CHANGE_TRANSFORMS | frozenset(
    {"identity", "spread_over_second"}
)


@dataclass(frozen=True)
class AcceptedFact:
    ticker: str
    metric_id: str
    value: float
    unit: str
    period_start: date | None
    period_end: date
    available_on: date
    definition_key: tuple[str, ...]
    source_key: str
    accession_number: str
    source_document: str
    conflict_resolution_status: str


def finite_float(value: object) -> float | None:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def iso_date(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value or "").strip()[:10])
    except ValueError:
        return None


def load_subgroup_score_policy(path: Path) -> dict[str, Any]:
    payload = load_yaml(path)
    if str(payload.get("policy_version")) != POLICY_VERSION:
        raise ValueError(f"{path}: unsupported v8 policy version")
    if payload.get("controls", {}).get("percentile_normalization") != (
        "within_comparison_group"
    ):
        raise ValueError("v8 requires within-comparison-group normalization")
    if payload.get("controls", {}).get("group_weights_use_outcomes") is not False:
        raise ValueError("v8 group weights must be outcome blind")
    if payload.get("controls", {}).get("component_weights_use_outcomes") is not False:
        raise ValueError("v8 component weights must be outcome blind")

    recipes = payload.get("generic_metric_recipes") or {}
    expected_generic = set(COMPONENTS) - {"positioning", "specialized"}
    if set(recipes) != expected_generic:
        raise ValueError("v8 generic recipes do not define the exact generic components")
    for component, raw_recipe in recipes.items():
        weights = [float(item["weight"]) for item in raw_recipe.values()]
        if not weights or any(value < 0 for value in weights):
            raise ValueError(f"{component}: invalid generic metric weights")
        if not math.isclose(sum(weights), 1.0, abs_tol=1e-9):
            raise ValueError(f"{component}: generic metric weights must sum to one")
        for metric_id, item in raw_recipe.items():
            if not metric_id or int(item["direction"]) not in {-1, 1}:
                raise ValueError(f"{component}: invalid generic metric direction")

    current_tickers: set[str] = set()
    valid_locations: set[tuple[str, str]] = set()
    for cohort_id, cohort in (payload.get("cohorts") or {}).items():
        groups = cohort.get("groups") or {}
        group_weights = {
            str(key): float(value)
            for key, value in (cohort.get("aggregate_group_weights") or {}).items()
        }
        if set(groups) != set(group_weights) or not math.isclose(
            sum(group_weights.values()), 1.0, abs_tol=1e-9
        ):
            raise ValueError(f"{cohort_id}: invalid fixed aggregate group weights")
        cohort_tickers: list[str] = []
        for group_id, group in groups.items():
            valid_locations.add((str(cohort_id), str(group_id)))
            tickers = [str(item).upper() for item in group.get("tickers") or []]
            if not tickers or len(tickers) != len(set(tickers)):
                raise ValueError(f"{cohort_id}/{group_id}: invalid ticker membership")
            cohort_tickers.extend(tickers)
            for field in ("component_weights_active", "component_weights_fallback"):
                weights = {str(k): float(v) for k, v in group[field].items()}
                if set(weights) != set(COMPONENTS):
                    raise ValueError(f"{cohort_id}/{group_id}: {field} is incomplete")
                if any(value < 0 for value in weights.values()) or not math.isclose(
                    sum(weights.values()), 1.0, abs_tol=1e-9
                ):
                    raise ValueError(f"{cohort_id}/{group_id}: {field} must sum to one")
            pack = group.get("specialized_pack") or {}
            if pack:
                pack_weights = [float(item["weight"]) for item in pack.values()]
                if not math.isclose(sum(pack_weights), 1.0, abs_tol=1e-9):
                    raise ValueError(f"{cohort_id}/{group_id}: pack weights must sum to one")
                for feature_id, item in pack.items():
                    if str(item.get("transform")) not in SUPPORTED_TRANSFORMS:
                        raise ValueError(f"{feature_id}: unsupported transform")
                    if int(item.get("direction")) not in {-1, 1}:
                        raise ValueError(f"{feature_id}: invalid direction")
                    if str(item.get("source_metric") or "") == "operating_ratio" and (
                        str(item.get("transform")) == "identity"
                    ):
                        raise ValueError("operating-ratio level is prohibited in v8")
            activation = str(group.get("specialized_activation") or "")
            if activation == "required_for_calibration":
                active_weight = float(group["component_weights_active"]["specialized"])
                if active_weight < 0.25:
                    raise ValueError(
                        f"{cohort_id}/{group_id}: required specialized pack is not meaningful"
                    )
        if len(cohort_tickers) != len(set(cohort_tickers)):
            raise ValueError(f"{cohort_id}: comparison groups must partition membership")
        if current_tickers & set(cohort_tickers):
            raise ValueError("v8 cohorts overlap")
        current_tickers.update(cohort_tickers)

    historical = payload.get("historical_calibration_only") or {}
    for ticker, item in historical.items():
        location = (str(item.get("cohort") or ""), str(item.get("group") or ""))
        if location not in valid_locations:
            raise ValueError(f"{ticker}: invalid historical group")
        start = iso_date(item.get("effective_from"))
        end = iso_date(item.get("effective_to"))
        if start is None or end is None or start > end:
            raise ValueError(f"{ticker}: invalid historical membership dates")
        if str(ticker).upper() in current_tickers:
            raise ValueError(f"{ticker}: current ticker cannot be historical-only")

    if len(current_tickers) != 35:
        raise ValueError(f"v8 policy must cover exactly 35 active tickers; got {len(current_tickers)}")
    governance = payload.get("governance") or {}
    if governance.get("membership_selection_uses_outcomes") is not False:
        raise ValueError("v8 membership must remain outcome blind")
    if governance.get("metric_definition_selection_uses_outcomes") is not False:
        raise ValueError("v8 metric definitions must remain outcome blind")
    if governance.get("production_activation_authorized") is not False:
        raise ValueError("v8 research policy cannot authorize production")
    return payload


def ticker_location(
    ticker: str,
    asof: str,
    policy: Mapping[str, Any],
) -> tuple[str, str] | None:
    symbol = str(ticker).upper()
    for cohort_id, cohort in policy["cohorts"].items():
        for group_id, group in cohort["groups"].items():
            if symbol in {str(item).upper() for item in group["tickers"]}:
                return str(cohort_id), str(group_id)
    historical = policy.get("historical_calibration_only") or {}
    item = historical.get(symbol)
    if item and str(item["effective_from"])[:10] <= asof <= str(item["effective_to"])[:10]:
        return str(item["cohort"]), str(item["group"])
    return None


def build_fact_history(
    rows: Iterable[Mapping[str, object]],
) -> dict[tuple[str, str], list[AcceptedFact]]:
    unique: dict[tuple[object, ...], AcceptedFact] = {}
    for row in rows:
        if str(row.get("replay_status") or "ACCEPTED") != "ACCEPTED":
            continue
        ticker = str(row.get("ticker") or "").upper()
        metric_id = str(row.get("metric_id") or "")
        value = finite_float(row.get("value"))
        period_end = iso_date(row.get("period_end"))
        available = availability_date(row)
        if not ticker or not metric_id or value is None or period_end is None or available is None:
            continue
        fact = AcceptedFact(
            ticker=ticker,
            metric_id=metric_id,
            value=value,
            unit=str(row.get("unit") or ""),
            period_start=iso_date(row.get("period_start")),
            period_end=period_end,
            available_on=available,
            definition_key=comparison_key(row),
            source_key=str(row.get("candidate_key") or row.get("evidence_key") or ""),
            accession_number=str(row.get("accession_number") or ""),
            source_document=str(row.get("source_document") or ""),
            conflict_resolution_status=str(
                row.get("conflict_resolution_status") or "NOT_AUDITED"
            ),
        )
        unique[
            (
                fact.ticker,
                fact.metric_id,
                fact.period_start,
                fact.period_end,
                fact.value,
                fact.definition_key,
                fact.source_key,
                fact.conflict_resolution_status,
            )
        ] = fact
    output: dict[tuple[str, str], list[AcceptedFact]] = defaultdict(list)
    for fact in unique.values():
        output[(fact.ticker, fact.metric_id)].append(fact)
    for facts in output.values():
        facts.sort(
            key=lambda item: (
                item.period_end,
                item.available_on,
                item.period_start or date.min,
                item.source_key,
            )
        )
    return dict(output)


def _available_facts(
    history: Mapping[tuple[str, str], Sequence[AcceptedFact]],
    *,
    ticker: str,
    metric_id: str,
    asof: date,
) -> list[AcceptedFact]:
    return [
        fact
        for fact in history.get((ticker, metric_id), ())
        if fact.available_on <= asof and fact.period_end <= asof
    ]


def _current_fact(
    history: Mapping[tuple[str, str], Sequence[AcceptedFact]],
    *,
    ticker: str,
    metric_id: str,
    asof: date,
    max_staleness_days: int,
) -> AcceptedFact | None:
    candidates = [
        fact
        for fact in _available_facts(history, ticker=ticker, metric_id=metric_id, asof=asof)
        if (asof - fact.period_end).days <= max_staleness_days
    ]
    return _latest_unambiguous_fact(candidates)


def _latest_unambiguous_fact(
    candidates: Sequence[AcceptedFact],
) -> AcceptedFact | None:
    """Return the latest economic period only when its value is unambiguous.

    Filing and exhibit parsers can emit multiple table cells for one issuer,
    KPI, period and definition. A candidate-key hash is not a semantic
    tie-breaker: choosing one hash can silently select a segment, adjusted, or
    consolidated value. Prefer the latest economic period, then its latest
    disclosure, and fail closed if that identity still has multiple values.
    """
    if not candidates:
        return None
    latest_period = max(item.period_end for item in candidates)
    period_candidates = [
        item for item in candidates if item.period_end == latest_period
    ]
    latest_available = max(item.available_on for item in period_candidates)
    identity_candidates = [
        item
        for item in period_candidates
        if item.available_on == latest_available
    ]
    identity_candidates = _shortest_compatible_duration(identity_candidates)
    if len({item.value for item in identity_candidates}) != 1:
        return None
    return min(identity_candidates, key=lambda item: item.source_key)


def _shortest_compatible_duration(
    candidates: Sequence[AcceptedFact],
) -> list[AcceptedFact]:
    """Preserve period-start identity; never choose quarter over YTD."""
    selected = list(candidates)
    if (
        not selected
        or any(
            item.conflict_resolution_status == "FAIL_CLOSED_REVIEW_REQUIRED"
            for item in selected
        )
        or any(item.period_start is None for item in selected)
        or len({item.definition_key for item in selected}) != 1
        or len({item.period_start for item in selected}) != 1
    ):
        return selected
    return selected


def _duration_days(fact: AcceptedFact) -> int | None:
    if fact.period_start is None:
        return None
    return (fact.period_end - fact.period_start).days


def _same_disclosure(left: AcceptedFact, right: AcceptedFact) -> bool:
    if left.accession_number and right.accession_number:
        return left.accession_number == right.accession_number
    if left.source_document and right.source_document:
        return left.source_document == right.source_document
    return False


def _coherent_prior_fact(
    candidates: Sequence[AcceptedFact],
    *,
    current: AcceptedFact,
) -> AcceptedFact | None:
    """Resolve an as-reported prior without using a later restatement."""
    same_disclosure = [
        item for item in candidates if _same_disclosure(item, current)
    ]
    eligible = same_disclosure or [
        item for item in candidates if item.available_on <= current.available_on
    ]
    if not eligible:
        return None
    current_duration = _duration_days(current)
    if current_duration is not None:
        duration_matched = [
            item
            for item in eligible
            if (duration := _duration_days(item)) is not None
            and abs(duration - current_duration) <= 15
        ]
        if duration_matched:
            eligible = duration_matched
    best_gap = min(
        abs((current.period_end - item.period_end).days - 365)
        for item in eligible
    )
    nearest = [
        item
        for item in eligible
        if abs((current.period_end - item.period_end).days - 365) == best_gap
    ]
    return _latest_unambiguous_fact(nearest)


def ambiguous_fact_identity_counts(
    history: Mapping[tuple[str, str], Sequence[AcceptedFact]],
    *,
    metric_ids: set[str] | None = None,
) -> dict[str, int]:
    """Count accepted fact identities containing conflicting numeric values."""
    conflicts: Counter[str] = Counter()
    for (_ticker, metric_id), facts in history.items():
        if metric_ids is not None and metric_id not in metric_ids:
            continue
        identities: dict[
            tuple[date | None, date, date, tuple[str, ...]], set[float]
        ] = defaultdict(set)
        for fact in facts:
            identities[
                (
                    fact.period_start,
                    fact.period_end,
                    fact.available_on,
                    fact.definition_key,
                )
            ].add(fact.value)
        count = sum(
            len(values) > 1 for values in identities.values()
        )
        if count:
            conflicts[metric_id] += count
    return dict(sorted(conflicts.items()))


def resolver_selection_conflict_counts(
    history: Mapping[tuple[str, str], Sequence[AcceptedFact]],
    *,
    metric_ids: set[str] | None = None,
) -> dict[str, int]:
    """Count conflicts at the exact identity used by the current-fact resolver.

    Unlike the within-definition diagnostic above, this intentionally ignores
    definition_key. The resolver selects one ticker/metric economic period and
    latest disclosure before it can know which semantic scope is appropriate;
    different values across those scopes therefore fail closed.
    """
    conflicts: Counter[str] = Counter()
    for (_ticker, metric_id), facts in history.items():
        if metric_ids is not None and metric_id not in metric_ids:
            continue
        identities: dict[tuple[date, date], list[AcceptedFact]] = defaultdict(list)
        for fact in facts:
            identities[(fact.period_end, fact.available_on)].append(fact)
        count = sum(
            len(
                {
                    item.value
                    for item in _shortest_compatible_duration(candidates)
                }
            )
            > 1
            for candidates in identities.values()
        )
        if count:
            conflicts[metric_id] += count
    return dict(sorted(conflicts.items()))


def derive_feature(
    *,
    ticker: str,
    asof: date,
    spec: Mapping[str, object],
    history: Mapping[tuple[str, str], Sequence[AcceptedFact]],
    staleness_days: Mapping[str, int],
) -> tuple[float | None, tuple[str, ...]]:
    transform = str(spec["transform"])
    metric_ids = [str(item) for item in spec.get("source_metrics") or []]
    if not metric_ids:
        metric_ids = [str(spec.get("source_metric") or "")]
    if not metric_ids or any(not item for item in metric_ids):
        raise ValueError("specialized feature is missing source metrics")
    current = _current_fact(
        history,
        ticker=ticker,
        metric_id=metric_ids[0],
        asof=asof,
        max_staleness_days=int(staleness_days.get(metric_ids[0], 550)),
    )
    if current is None:
        return None, ()
    if transform == "identity":
        return current.value, (current.source_key,)
    if transform in CHANGE_TRANSFORMS:
        priors = [
            fact
            for fact in _available_facts(
                history, ticker=ticker, metric_id=metric_ids[0], asof=asof
            )
            if fact.definition_key == current.definition_key
            and 300 <= (current.period_end - fact.period_end).days <= 430
        ]
        if not priors:
            return None, ()
        prior = _coherent_prior_fact(priors, current=current)
        if prior is None:
            return None, ()
        if transform == "yoy_growth":
            if prior.value == 0:
                return None, ()
            value = current.value / prior.value - 1.0
        elif transform == "yoy_improvement":
            if prior.value == 0:
                return None, ()
            value = (prior.value - current.value) / abs(prior.value)
        else:
            value = current.value - prior.value
        return value, (current.source_key, prior.source_key)
    if transform == "spread_over_second":
        if len(metric_ids) != 2:
            raise ValueError("spread_over_second requires two source metrics")
        second = _current_fact(
            history,
            ticker=ticker,
            metric_id=metric_ids[1],
            asof=asof,
            max_staleness_days=int(staleness_days.get(metric_ids[1], 550)),
        )
        if second is None or second.value == 0:
            return None, ()
        if abs((current.period_end - second.period_end).days) > 200:
            return None, ()
        allowed_units = {"currency_per_day", "usd_per_day"}
        if current.unit.casefold() not in allowed_units or second.unit.casefold() not in allowed_units:
            return None, ()
        return (
            (current.value - second.value) / abs(second.value),
            (current.source_key, second.source_key),
        )
    raise ValueError(f"unsupported transform={transform}")


def _payload(row: Mapping[str, object]) -> tuple[dict[str, float], dict[str, str]]:
    try:
        raw_values = json.loads(str(row.get("metric_values_json") or "{}"))
        raw_statuses = json.loads(str(row.get("metric_status_json") or "{}"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid metric JSON for {row.get('asof_date')}/{row.get('ticker')}") from exc
    values = {
        str(key): value
        for key, raw in dict(raw_values).items()
        if (value := finite_float(raw)) is not None
    }
    statuses = {str(key): str(value) for key, value in dict(raw_statuses).items()}
    return values, statuses


def _unique_score_rows(
    panel_rows: Iterable[Mapping[str, object]],
    policy: Mapping[str, Any],
) -> list[dict[str, object]]:
    unique: dict[tuple[str, str], dict[str, object]] = {}
    for raw in panel_rows:
        row = dict(raw)
        asof = str(row.get("asof_date") or "")[:10]
        ticker = str(row.get("ticker") or "").upper()
        location = ticker_location(ticker, asof, policy)
        if location is None:
            continue
        key = (asof, ticker)
        prior = unique.get(key)
        if prior is None or str(row.get("horizon_sessions") or "") == "63":
            row["ticker"] = ticker
            row["v8_cohort_id"], row["v8_group_id"] = location
            unique[key] = row
    return [unique[key] for key in sorted(unique)]


def build_v8_score_rows(
    *,
    panel_rows: Iterable[Mapping[str, object]],
    accepted_rows: Iterable[Mapping[str, object]],
    policy: Mapping[str, Any],
    staleness_days: Mapping[str, int],
) -> tuple[list[dict[str, object]], list[dict[str, object]], dict[str, object]]:
    rows = _unique_score_rows(panel_rows, policy)
    history = build_fact_history(accepted_rows)
    controls = policy["controls"]
    neutral = float(controls["neutral_missing_score"])
    winsor_lower = float(controls["winsor_lower"])
    winsor_upper = float(controls["winsor_upper"])

    by_date_group: dict[tuple[str, str, str], list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        by_date_group[
            (str(row["asof_date"]), str(row["v8_cohort_id"]), str(row["v8_group_id"]))
        ].append(row)

    feature_values: dict[tuple[str, str, str], float] = {}
    feature_sources: dict[tuple[str, str, str], tuple[str, ...]] = {}
    for (asof_text, cohort_id, group_id), date_rows in by_date_group.items():
        asof = date.fromisoformat(asof_text)
        group = policy["cohorts"][cohort_id]["groups"][group_id]
        for row in date_rows:
            ticker = str(row["ticker"])
            for feature_id, spec in (group.get("specialized_pack") or {}).items():
                value, source_keys = derive_feature(
                    ticker=ticker,
                    asof=asof,
                    spec=spec,
                    history=history,
                    staleness_days=staleness_days,
                )
                if value is not None:
                    key = (asof_text, ticker, str(feature_id))
                    feature_values[key] = value
                    feature_sources[key] = source_keys

    score_dates = sorted({str(row["asof_date"]) for row in rows})
    if not score_dates:
        raise ValueError("v8 score panel contains no policy-eligible rows")
    coverage_rows: list[dict[str, object]] = []
    date_activation: dict[tuple[str, str, str], bool] = {}
    current_activation: dict[tuple[str, str], bool] = {}
    for cohort_id, cohort in policy["cohorts"].items():
        for group_id, group in cohort["groups"].items():
            pack = group.get("specialized_pack") or {}
            mode = str(group["specialized_activation"])
            date_passes = 0
            latest_pass = False
            group_dates = sorted(
                asof
                for asof, candidate_cohort, candidate_group in by_date_group
                if candidate_cohort == str(cohort_id)
                and candidate_group == str(group_id)
            )
            for asof in group_dates:
                tickers = {
                    str(row["ticker"]).upper()
                    for row in by_date_group[
                        (asof, str(cohort_id), str(group_id))
                    ]
                }
                breadth = sum(
                    any((asof, ticker, str(feature_id)) in feature_values for feature_id in pack)
                    for ticker in tickers
                )
                passes = bool(pack) and breadth >= int(group["minimum_specialized_breadth"])
                date_passes += int(passes)
                latest_pass = passes
                date_activation[(asof, str(cohort_id), str(group_id))] = (
                    mode != "excluded_insufficient_independent_cross_section"
                    and passes
                )
                coverage_rows.append(
                    {
                        "policy_version": POLICY_VERSION,
                        "cohort_id": str(cohort_id),
                        "group_id": str(group_id),
                        "score_date": asof,
                        "applicable_ticker_count": len(tickers),
                        "specialized_observed_breadth": breadth,
                        "minimum_specialized_breadth": int(group["minimum_specialized_breadth"]),
                        "date_gate": "PASS" if passes else "FAIL",
                    }
                )
            pass_fraction = date_passes / len(group_dates) if group_dates else 0.0
            current_activation[(str(cohort_id), str(group_id))] = (
                mode != "excluded_insufficient_independent_cross_section"
                and latest_pass
                and pass_fraction >= float(controls["minimum_specialized_date_fraction"])
            )

    output: list[dict[str, object]] = []
    for (asof, cohort_id, group_id), date_rows in sorted(by_date_group.items()):
        group = policy["cohorts"][cohort_id]["groups"][group_id]
        active = date_activation.get((asof, cohort_id, group_id), False)
        component_weights = group[
            "component_weights_active" if active else "component_weights_fallback"
        ]
        parsed = {str(row["ticker"]): _payload(row) for row in date_rows}
        metric_scores: dict[str, dict[str, float]] = defaultdict(dict)
        for component, recipe in policy["generic_metric_recipes"].items():
            if component in set(policy["cohorts"][cohort_id].get("generic_component_exclusions") or []):
                continue
            for metric_id, metric_spec in recipe.items():
                raw = {
                    ticker: values[metric_id]
                    for ticker, (values, statuses) in parsed.items()
                    if metric_id in values and statuses.get(metric_id) in OBSERVED_STATUSES
                    and is_rankable_metric_value(metric_id, values[metric_id])
                }
                scores = percentile_scores(
                    raw, winsor_lower=winsor_lower, winsor_upper=winsor_upper
                )
                direction = int(metric_spec["direction"])
                for ticker, score in scores.items():
                    metric_scores[ticker][metric_id] = score if direction == 1 else 100.0 - score

        specialized_scores: dict[str, dict[str, float]] = defaultdict(dict)
        for feature_id, feature_spec in (group.get("specialized_pack") or {}).items():
            raw = {
                str(row["ticker"]): feature_values[(asof, str(row["ticker"]), str(feature_id))]
                for row in date_rows
                if (asof, str(row["ticker"]), str(feature_id)) in feature_values
            }
            scores = percentile_scores(
                raw, winsor_lower=winsor_lower, winsor_upper=winsor_upper
            )
            direction = int(feature_spec["direction"])
            for ticker, score in scores.items():
                specialized_scores[ticker][str(feature_id)] = (
                    score if direction == 1 else 100.0 - score
                )

        raw_final: dict[str, float] = {}
        interim: dict[str, dict[str, object]] = {}
        for row in date_rows:
            ticker = str(row["ticker"])
            components: dict[str, float] = {}
            for component, recipe in policy["generic_metric_recipes"].items():
                if component in set(policy["cohorts"][cohort_id].get("generic_component_exclusions") or []):
                    components[component] = neutral
                    continue
                components[component] = sum(
                    float(metric_spec["weight"])
                    * metric_scores[ticker].get(str(metric_id), neutral)
                    for metric_id, metric_spec in recipe.items()
                )
            position = finite_float(row.get("positioning_score"))
            components["positioning"] = neutral if position is None else position
            pack = group.get("specialized_pack") or {}
            components["specialized"] = (
                sum(
                    float(feature_spec["weight"])
                    * specialized_scores[ticker].get(str(feature_id), neutral)
                    for feature_id, feature_spec in pack.items()
                )
                if pack
                else neutral
            )
            final_score = sum(
                components[component] * float(component_weights[component])
                for component in COMPONENTS
            )
            raw_final[ticker] = final_score
            feature_payload = {
                str(feature_id): feature_values.get((asof, ticker, str(feature_id)))
                for feature_id in pack
            }
            source_payload = {
                str(feature_id): list(feature_sources.get((asof, ticker, str(feature_id)), ()))
                for feature_id in pack
            }
            interim[ticker] = {
                "row": row,
                "components": components,
                "features": feature_payload,
                "sources": source_payload,
            }
        group_percentiles = percentile_scores(
            raw_final, winsor_lower=winsor_lower, winsor_upper=winsor_upper
        )
        mode = str(group["specialized_activation"])
        required_pack_ready = mode != "required_for_calibration" or active
        cross_section_ready = len(date_rows) >= int(group["minimum_cross_section"])
        for ticker in sorted(interim):
            item = interim[ticker]
            source_row = item["row"]
            output.append(
                {
                    "asof_date": asof,
                    "ticker": ticker,
                    "calibration_cohort": str(source_row.get("calibration_cohort") or ""),
                    "v8_cohort_id": cohort_id,
                    "v8_group_id": group_id,
                    "ranking_mode": str(group["ranking_mode"]),
                    "specialized_pack_active_flag": int(active),
                    "specialized_activation_policy": mode,
                    "specialized_features_json": json.dumps(item["features"], sort_keys=True, separators=(",", ":")),
                    "specialized_source_keys_json": json.dumps(item["sources"], sort_keys=True, separators=(",", ":")),
                    "component_scores_json": json.dumps(item["components"], sort_keys=True, separators=(",", ":")),
                    "component_weights_json": json.dumps(component_weights, sort_keys=True, separators=(",", ":")),
                    "v8_final_score": raw_final[ticker],
                    "v8_group_percentile_score": group_percentiles.get(ticker, neutral),
                    "source_rank_ready_flag": int(str(source_row.get("rank_ready_flag") or "0") == "1"),
                    "source_calibration_eligible_flag": int(str(source_row.get("calibration_eligible_flag") or "0") == "1"),
                    "group_cross_section_ready_flag": int(cross_section_ready),
                    "group_specialized_ready_flag": int(required_pack_ready),
                    "v8_calibration_eligible_flag": int(
                        str(source_row.get("calibration_eligible_flag") or "0") == "1"
                        and cross_section_ready
                        and required_pack_ready
                    ),
                    "source_score_sha256": str(source_row.get("source_score_sha256") or ""),
                }
            )

    summary_groups: list[dict[str, object]] = []
    for cohort_id, cohort in policy["cohorts"].items():
        for group_id, group in cohort["groups"].items():
            group_coverage = [
                row
                for row in coverage_rows
                if row["cohort_id"] == str(cohort_id) and row["group_id"] == str(group_id)
            ]
            passing = sum(row["date_gate"] == "PASS" for row in group_coverage)
            active = current_activation[(str(cohort_id), str(group_id))]
            required = str(group["specialized_activation"]) == "required_for_calibration"
            summary_groups.append(
                {
                    "cohort_id": str(cohort_id),
                    "group_id": str(group_id),
                    "ranking_mode": str(group["ranking_mode"]),
                    "specialized_activation_policy": str(group["specialized_activation"]),
                    "score_date_count": len(group_coverage),
                    "specialized_passing_date_count": passing,
                    "specialized_passing_date_fraction": (
                        passing / len(group_coverage) if group_coverage else 0.0
                    ),
                    "latest_date_gate": (
                        group_coverage[-1]["date_gate"]
                        if group_coverage
                        else "FAIL"
                    ),
                    "specialized_pack_active_flag": int(active),
                    "group_calibration_ready_flag": int(not required or active),
                }
            )
    source_metric_ids = {
        str(metric_id)
        for cohort in policy["cohorts"].values()
        for group in cohort["groups"].values()
        for feature in (group.get("specialized_pack") or {}).values()
        for metric_id in (
            feature.get("source_metrics")
            or [feature.get("source_metric")]
        )
        if metric_id
    }
    conflict_counts = ambiguous_fact_identity_counts(
        history,
        metric_ids=source_metric_ids,
    )
    selection_conflict_counts = resolver_selection_conflict_counts(
        history,
        metric_ids=source_metric_ids,
    )
    manifest = {
        "policy_version": POLICY_VERSION,
        "score_date_count": len(score_dates),
        "score_date_min": score_dates[0],
        "score_date_max": score_dates[-1],
        "score_row_count": len(output),
        "group_count": len(summary_groups),
        "group_summaries": summary_groups,
        "specialized_score_activation_policy": (
            "point_in_time_group_membership_and_date_gate_no_future_coverage_"
            "leakage"
        ),
        "required_group_failure_count": sum(
            int(row["group_calibration_ready_flag"] == 0) for row in summary_groups
        ),
        "ambiguous_source_fact_identity_count": sum(
            selection_conflict_counts.values()
        ),
        "ambiguous_source_fact_identity_count_by_metric": (
            selection_conflict_counts
        ),
        "within_definition_source_fact_conflict_count": sum(
            conflict_counts.values()
        ),
        "within_definition_source_fact_conflict_count_by_metric": (
            conflict_counts
        ),
        "ambiguous_source_fact_policy": (
            "latest_period_latest_disclosure_conflicting_values_across_"
            "definition_scopes_fail_closed_no_feature_value"
        ),
        "network_requests": 0,
        "parser_invocations": 0,
        "production_activation_authorized": False,
    }
    return output, coverage_rows, manifest
