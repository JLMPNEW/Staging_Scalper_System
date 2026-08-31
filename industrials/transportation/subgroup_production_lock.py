"""Fail-closed production-lock contract for Transportation subgroup recipes.

The shared Industrials lock supports one flat component-weight vector.  The
Transportation v8 design instead requires independently calibrated recipes by
cohort and economic subgroup.  This module validates that richer decision
payload without authorizing activation; callers must still require passing,
future-only evidence and an explicitly compatible scoring/portfolio adapter.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping


COMPONENTS = {
    "market_trend",
    "quality",
    "growth",
    "valuation",
    "operating_efficiency",
    "capital_risk",
    "positioning",
    "specialized",
}
RANKING_MODES = {"ranked", "eligibility_equal_weight"}
SPECIALIZED_ACTIVATION_MODES = {
    "coverage_gated_optional",
    "required_for_calibration",
    "excluded_insufficient_independent_cross_section",
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class TransportationGroupRecipe:
    cohort_id: str
    group_id: str
    ranking_mode: str
    tickers: tuple[str, ...]
    aggregate_group_weight: float
    component_weights_active: dict[str, float]
    component_weights_fallback: dict[str, float]
    specialized_pack: dict[str, dict[str, Any]]
    specialized_activation: str
    minimum_cross_section: int
    recipe_sha256: str


@dataclass(frozen=True)
class TransportationGroupMembership:
    ticker: str
    cohort_id: str
    group_id: str
    membership_scope: str
    effective_from: date | None = None
    effective_to: date | None = None

    def applies_on(self, asof: date) -> bool:
        return (
            (self.effective_from is None or self.effective_from <= asof)
            and (self.effective_to is None or asof <= self.effective_to)
        )


@dataclass(frozen=True)
class TransportationSubgroupLockSpec:
    recipe_version: str
    policy_sha256: str
    policy_effective_from: date
    expected_group_keys_sha256: str
    group_recipe_set_sha256: str
    groups: dict[str, TransportationGroupRecipe]
    memberships: dict[str, tuple[TransportationGroupMembership, ...]]

    def membership_for(
        self,
        ticker: str,
        asof: date,
    ) -> TransportationGroupMembership | None:
        matches = [
            item
            for item in self.memberships.get(str(ticker).strip().upper(), ())
            if item.applies_on(asof)
        ]
        if len(matches) > 1:
            raise ValueError(
                f"{ticker}: ambiguous subgroup membership on {asof.isoformat()}"
            )
        return matches[0] if matches else None


def canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _date_or_none(value: object, *, label: str) -> date | None:
    text = str(value or "").strip()[:10]
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{label} is not an ISO date") from exc


def _weights(
    value: object,
    *,
    label: str,
    expected_keys: set[str] | None = None,
) -> dict[str, float]:
    if not isinstance(value, Mapping) or not value:
        raise ValueError(f"{label} must be a non-empty mapping")
    parsed = {str(key): float(weight) for key, weight in value.items()}
    if expected_keys is not None and set(parsed) != expected_keys:
        raise ValueError(
            f"{label} keys mismatch expected={sorted(expected_keys)} "
            f"actual={sorted(parsed)}"
        )
    if any(not math.isfinite(weight) or weight < 0.0 for weight in parsed.values()):
        raise ValueError(f"{label} contains a negative or non-finite weight")
    if not math.isclose(sum(parsed.values()), 1.0, abs_tol=1e-9):
        raise ValueError(f"{label} must sum to one")
    return parsed


def validate_subgroup_lock_payload(
    payload: Mapping[str, object],
) -> TransportationSubgroupLockSpec:
    """Validate a complete, reproducible v8 group-recipe decision payload."""
    if str(payload.get("scoring_mode") or "") != "subgroup_v8":
        raise ValueError("subgroup production payload must use scoring_mode=subgroup_v8")
    if payload.get("production_activation_authorized") is not False:
        raise ValueError(
            "subgroup recipe contract must remain non-authorizing"
        )
    if payload.get("future_only_evidence_passed") is not False:
        raise ValueError(
            "subgroup recipe contract cannot claim future-only evidence"
        )
    recipe_version = str(payload.get("group_recipe_version") or "").strip()
    policy_sha256 = str(payload.get("subgroup_policy_sha256") or "").strip().lower()
    policy_effective_from = _date_or_none(
        payload.get("policy_effective_from"),
        label="policy_effective_from",
    )
    raw_groups = payload.get("group_recipes")
    if not recipe_version:
        raise ValueError("subgroup production payload has no group_recipe_version")
    if not SHA256_RE.fullmatch(policy_sha256):
        raise ValueError("subgroup production payload has no valid policy SHA-256")
    if policy_effective_from is None:
        raise ValueError("subgroup production payload has no policy_effective_from")
    if not isinstance(raw_groups, Mapping) or not raw_groups:
        raise ValueError("subgroup production payload has no group recipes")
    expected_group_count = int(payload.get("expected_group_count") or 0)
    expected_ticker_count = int(
        payload.get("expected_current_ticker_count") or 0
    )
    if expected_group_count != len(raw_groups):
        raise ValueError(
            "subgroup production payload is missing or has extra group recipes"
        )
    expected_keys_sha256 = str(
        payload.get("expected_group_keys_sha256") or ""
    ).strip().lower()
    if expected_keys_sha256 != canonical_sha256(sorted(str(key) for key in raw_groups)):
        raise ValueError("subgroup group-key seal does not tie")
    recipe_set_sha256 = str(
        payload.get("group_recipe_set_sha256") or ""
    ).strip().lower()

    recipes: dict[str, TransportationGroupRecipe] = {}
    normalized_recipe_payloads: dict[str, dict[str, object]] = {}
    ticker_owner: dict[str, str] = {}
    cohort_group_weights: dict[str, list[float]] = {}
    for recipe_key, raw_recipe in sorted(raw_groups.items()):
        key = str(recipe_key)
        if not isinstance(raw_recipe, Mapping):
            raise ValueError(f"{key}: group recipe must be a mapping")
        cohort_id = str(raw_recipe.get("cohort_id") or "").strip()
        group_id = str(raw_recipe.get("group_id") or "").strip()
        if key != f"{cohort_id}::{group_id}" or not cohort_id or not group_id:
            raise ValueError(f"{key}: recipe identity does not match cohort/group")
        ranking_mode = str(raw_recipe.get("ranking_mode") or "").strip()
        if ranking_mode not in RANKING_MODES:
            raise ValueError(f"{key}: unsupported ranking_mode={ranking_mode!r}")
        raw_tickers = raw_recipe.get("tickers")
        if not isinstance(raw_tickers, list) or not raw_tickers:
            raise ValueError(f"{key}: tickers must be a non-empty list")
        tickers = tuple(str(item).strip().upper() for item in raw_tickers)
        if any(not ticker for ticker in tickers) or len(set(tickers)) != len(tickers):
            raise ValueError(f"{key}: blank or duplicate ticker")
        for ticker in tickers:
            previous = ticker_owner.get(ticker)
            if previous is not None:
                raise ValueError(
                    f"{ticker}: assigned to multiple production groups "
                    f"({previous}, {key})"
                )
            ticker_owner[ticker] = key

        aggregate_weight = float(raw_recipe.get("aggregate_group_weight") or 0.0)
        if not math.isfinite(aggregate_weight) or aggregate_weight <= 0.0:
            raise ValueError(f"{key}: aggregate_group_weight must be positive")
        cohort_group_weights.setdefault(cohort_id, []).append(aggregate_weight)
        active = _weights(
            raw_recipe.get("component_weights_active"),
            label=f"{key}.component_weights_active",
            expected_keys=COMPONENTS,
        )
        fallback = _weights(
            raw_recipe.get("component_weights_fallback"),
            label=f"{key}.component_weights_fallback",
            expected_keys=COMPONENTS,
        )
        if fallback["specialized"] != 0.0:
            raise ValueError(
                f"{key}: fallback specialized weight must be zero"
            )
        raw_pack = raw_recipe.get("specialized_pack") or {}
        if not isinstance(raw_pack, Mapping):
            raise ValueError(f"{key}.specialized_pack must be a mapping")
        specialized_pack = {
            str(metric_id): dict(definition)
            for metric_id, definition in raw_pack.items()
            if isinstance(definition, Mapping)
        }
        if len(specialized_pack) != len(raw_pack):
            raise ValueError(f"{key}.specialized_pack contains an invalid definition")
        specialized_activation = str(
            raw_recipe.get("specialized_activation") or ""
        )
        if specialized_activation not in SPECIALIZED_ACTIVATION_MODES:
            raise ValueError(
                f"{key}: invalid specialized_activation={specialized_activation!r}"
            )
        if bool(specialized_pack) != (active["specialized"] > 0.0):
            raise ValueError(
                f"{key}: specialized pack and active weight are inconsistent"
            )
        if active["specialized"] > 0.0:
            pack_weights = _weights(
                {
                    metric_id: definition.get("weight")
                    for metric_id, definition in specialized_pack.items()
                },
                label=f"{key}.specialized_pack.weights",
            )
            for metric_id, definition in specialized_pack.items():
                if not (
                    definition.get("source_metric")
                    or definition.get("source_metrics")
                ):
                    raise ValueError(
                        f"{key}.{metric_id}: missing specialized source metric"
                    )
                if int(definition.get("direction") or 0) not in {-1, 1}:
                    raise ValueError(
                        f"{key}.{metric_id}: direction must be -1 or 1"
                    )
                if not str(definition.get("transform") or "").strip():
                    raise ValueError(f"{key}.{metric_id}: transform is required")
            if set(pack_weights) != set(specialized_pack):
                raise AssertionError("specialized weight validation lost a metric")
        elif specialized_pack and ranking_mode == "eligibility_equal_weight":
            raise ValueError(
                f"{key}: non-ranked zero-specialized recipe must not carry a pack"
            )
        normalized_recipe = {
            "cohort_id": cohort_id,
            "group_id": group_id,
            "ranking_mode": ranking_mode,
            "tickers": list(tickers),
            "aggregate_group_weight": aggregate_weight,
            "component_weights_active": active,
            "component_weights_fallback": fallback,
            "specialized_pack": specialized_pack,
            "specialized_activation": str(
                specialized_activation
            ),
            "minimum_cross_section": int(
                raw_recipe.get("minimum_cross_section") or 1
            ),
        }
        if normalized_recipe["minimum_cross_section"] <= 0:
            raise ValueError(f"{key}: minimum_cross_section must be positive")
        recipe_sha256 = canonical_sha256(normalized_recipe)
        sealed_recipe_sha256 = str(
            raw_recipe.get("group_recipe_sha256") or ""
        ).strip().lower()
        if sealed_recipe_sha256 and sealed_recipe_sha256 != recipe_sha256:
            raise ValueError(f"{key}: group recipe SHA-256 does not tie")
        recipes[key] = TransportationGroupRecipe(
            cohort_id=cohort_id,
            group_id=group_id,
            ranking_mode=ranking_mode,
            tickers=tickers,
            aggregate_group_weight=aggregate_weight,
            component_weights_active=active,
            component_weights_fallback=fallback,
            specialized_pack=specialized_pack,
            specialized_activation=str(
                normalized_recipe["specialized_activation"]
            ),
            minimum_cross_section=int(
                normalized_recipe["minimum_cross_section"]
            ),
            recipe_sha256=recipe_sha256,
        )
        normalized_recipe_payloads[key] = normalized_recipe

    for cohort_id, weights in cohort_group_weights.items():
        if not math.isclose(sum(weights), 1.0, abs_tol=1e-9):
            raise ValueError(
                f"{cohort_id}: aggregate group weights must sum to one"
            )
    if expected_ticker_count != len(ticker_owner):
        raise ValueError(
            "subgroup production payload current ticker census does not tie"
        )
    if recipe_set_sha256 != canonical_sha256(normalized_recipe_payloads):
        raise ValueError("subgroup group-recipe set seal does not tie")

    memberships: dict[str, list[TransportationGroupMembership]] = {}
    for ticker, recipe_key in ticker_owner.items():
        recipe = recipes[recipe_key]
        memberships.setdefault(ticker, []).append(
            TransportationGroupMembership(
                ticker=ticker,
                cohort_id=recipe.cohort_id,
                group_id=recipe.group_id,
                membership_scope="current_recipe",
                effective_from=policy_effective_from,
            )
        )

    raw_historical = payload.get("historical_calibration_memberships") or {}
    if not isinstance(raw_historical, Mapping):
        raise ValueError("historical_calibration_memberships must be a mapping")
    for raw_ticker, raw_intervals in raw_historical.items():
        ticker = str(raw_ticker).strip().upper()
        if not ticker:
            raise ValueError("historical subgroup membership has a blank ticker")
        if ticker in ticker_owner:
            raise ValueError(
                f"{ticker}: cannot be both current and historical-only"
            )
        intervals = (
            list(raw_intervals)
            if isinstance(raw_intervals, list)
            else [raw_intervals]
        )
        if not intervals or any(not isinstance(item, Mapping) for item in intervals):
            raise ValueError(f"{ticker}: invalid historical membership intervals")
        for index, item in enumerate(intervals):
            cohort_id = str(
                item.get("cohort_id") or item.get("cohort") or ""
            ).strip()
            group_id = str(
                item.get("group_id") or item.get("group") or ""
            ).strip()
            recipe_key = f"{cohort_id}::{group_id}"
            if recipe_key not in recipes:
                raise ValueError(
                    f"{ticker}: historical membership has no group recipe {recipe_key}"
                )
            start = _date_or_none(
                item.get("effective_from"),
                label=f"{ticker}[{index}].effective_from",
            )
            end = _date_or_none(
                item.get("effective_to"),
                label=f"{ticker}[{index}].effective_to",
            )
            if start is None or end is None or start > end:
                raise ValueError(
                    f"{ticker}[{index}]: historical membership range is invalid"
                )
            memberships.setdefault(ticker, []).append(
                TransportationGroupMembership(
                    ticker=ticker,
                    cohort_id=cohort_id,
                    group_id=group_id,
                    membership_scope="historical_calibration_only",
                    effective_from=start,
                    effective_to=end,
                )
            )

    sealed_memberships: dict[
        str, tuple[TransportationGroupMembership, ...]
    ] = {}
    for ticker, items in memberships.items():
        ordered = sorted(
            items,
            key=lambda item: (
                item.effective_from or date.min,
                item.effective_to or date.max,
                item.cohort_id,
                item.group_id,
            ),
        )
        for previous, current in zip(ordered, ordered[1:]):
            previous_end = previous.effective_to or date.max
            current_start = current.effective_from or date.min
            if previous_end >= current_start:
                raise ValueError(
                    f"{ticker}: overlapping subgroup membership intervals"
                )
        sealed_memberships[ticker] = tuple(ordered)
    return TransportationSubgroupLockSpec(
        recipe_version=recipe_version,
        policy_sha256=policy_sha256,
        policy_effective_from=policy_effective_from,
        expected_group_keys_sha256=expected_keys_sha256,
        group_recipe_set_sha256=recipe_set_sha256,
        groups=recipes,
        memberships=sealed_memberships,
    )


def build_subgroup_lock_payload(
    policy: Mapping[str, object],
    *,
    policy_sha256: str,
    recipe_version: str = "transportation_subgroup_v8_lock_v1",
) -> dict[str, object]:
    """Materialize the self-contained, outcome-blind v8 recipe contract.

    The returned payload is suitable for shadow scoring. Production activation
    remains a separate evidence decision and is deliberately not asserted here.
    """
    if not SHA256_RE.fullmatch(str(policy_sha256).strip().lower()):
        raise ValueError("policy_sha256 must be a lowercase SHA-256")
    raw_cohorts = policy.get("cohorts")
    if not isinstance(raw_cohorts, Mapping) or not raw_cohorts:
        raise ValueError("subgroup policy has no cohorts")
    group_recipes: dict[str, dict[str, object]] = {}
    for raw_cohort_id, raw_cohort in raw_cohorts.items():
        cohort_id = str(raw_cohort_id)
        if not isinstance(raw_cohort, Mapping):
            raise ValueError(f"{cohort_id}: cohort must be a mapping")
        groups = raw_cohort.get("groups")
        aggregate = raw_cohort.get("aggregate_group_weights")
        if not isinstance(groups, Mapping) or not isinstance(aggregate, Mapping):
            raise ValueError(f"{cohort_id}: missing groups or aggregate weights")
        for raw_group_id, raw_group in groups.items():
            group_id = str(raw_group_id)
            if not isinstance(raw_group, Mapping):
                raise ValueError(
                    f"{cohort_id}/{group_id}: group must be a mapping"
                )
            key = f"{cohort_id}::{group_id}"
            group_recipes[key] = {
                "cohort_id": cohort_id,
                "group_id": group_id,
                "ranking_mode": raw_group.get("ranking_mode"),
                "tickers": [
                    str(ticker).strip().upper()
                    for ticker in list(raw_group.get("tickers") or [])
                ],
                "aggregate_group_weight": float(
                    aggregate.get(raw_group_id) or 0.0
                ),
                "component_weights_active": {
                    str(component): float(weight)
                    for component, weight in dict(
                        raw_group.get("component_weights_active") or {}
                    ).items()
                },
                "component_weights_fallback": {
                    str(component): float(weight)
                    for component, weight in dict(
                        raw_group.get("component_weights_fallback") or {}
                    ).items()
                },
                "specialized_pack": dict(
                    raw_group.get("specialized_pack") or {}
                ),
                "specialized_activation": str(
                    raw_group.get("specialized_activation") or ""
                ),
                "minimum_cross_section": int(
                    raw_group.get("minimum_cross_section") or 1
                ),
            }
    historical = policy.get("historical_calibration_only") or {}
    if not isinstance(historical, Mapping):
        raise ValueError("historical_calibration_only must be a mapping")
    historical_memberships = {
        str(ticker).strip().upper(): {
            "cohort_id": str(item.get("cohort") or ""),
            "group_id": str(item.get("group") or ""),
            "effective_from": str(item.get("effective_from") or ""),
            "effective_to": str(item.get("effective_to") or ""),
        }
        for ticker, item in historical.items()
        if isinstance(item, Mapping)
    }
    payload: dict[str, object] = {
        "scoring_mode": "subgroup_v8",
        "group_recipe_version": recipe_version,
        "subgroup_policy_sha256": str(policy_sha256).strip().lower(),
        "policy_effective_from": str(policy.get("effective_from") or ""),
        "group_recipes": group_recipes,
        "historical_calibration_memberships": historical_memberships,
        "aggregation": dict(policy.get("aggregation") or {}),
        "production_activation_authorized": False,
        "future_only_evidence_passed": False,
    }
    payload["expected_group_count"] = len(group_recipes)
    payload["expected_current_ticker_count"] = sum(
        len(list(recipe.get("tickers") or []))
        for recipe in group_recipes.values()
    )
    payload["expected_group_keys_sha256"] = canonical_sha256(
        sorted(group_recipes)
    )
    payload["group_recipe_set_sha256"] = canonical_sha256(group_recipes)
    spec = validate_subgroup_lock_payload(payload)
    for key, recipe in spec.groups.items():
        group_recipes[key]["group_recipe_sha256"] = recipe.recipe_sha256
    validate_subgroup_lock_payload(payload)
    return payload
