"""Executable, fail-closed Transportation subgroup rank-table contract.

This module bridges the v8 subgroup score history to the ordinary
Transportation final-rank schema.  It deliberately supports shadow publication
only: group recipes and point-in-time membership are fully validated and
stamped, while portfolio and OOS gates stay zero until separately governed
future-only evidence authorizes activation.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import date
from typing import Mapping, Sequence

from industrials.transportation.contracts import (
    FINAL_RANK_FIELDS,
    SCORING_FEATURE_FIELDS,
    validate_rank_rows,
)
from industrials.transportation.subgroup_production_lock import (
    TransportationGroupMembership,
    TransportationSubgroupLockSpec,
    canonical_sha256,
    validate_subgroup_lock_payload,
)


SHADOW_SCORE_MODEL_VERSION = "transportation_hierarchical_subgroup_v8_shadow"
SHADOW_MODEL_VERSION = "transportation_subgroup_model_v8_shadow"
SHADOW_CONTRACT_VERSION = "transportation_final_rank_table_v2_subgroup_shadow"


def _finite(value: object, *, label: str) -> float:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} is not numeric") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{label} is not finite")
    return parsed


def _flag(value: object, *, label: str) -> bool:
    text = str(value or "").strip()
    if text not in {"0", "1"}:
        raise ValueError(f"{label} must be 0 or 1")
    return text == "1"


def _iso(value: object, *, label: str, required: bool = True) -> date | None:
    text = str(value or "").strip()[:10]
    if not text:
        if required:
            raise ValueError(f"{label} is required")
        return None
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{label} is not an ISO date") from exc


def _unique_rows(
    rows: Sequence[Mapping[str, object]],
    *,
    label: str,
) -> tuple[str, dict[str, dict[str, object]]]:
    if not rows:
        raise ValueError(f"{label} is empty")
    dates = {str(row.get("asof_date") or "")[:10] for row in rows}
    if "" in dates or len(dates) != 1:
        raise ValueError(f"{label} must contain exactly one asof_date")
    by_ticker: dict[str, dict[str, object]] = {}
    for raw in rows:
        row = dict(raw)
        ticker = str(row.get("ticker") or "").strip().upper()
        if not ticker:
            raise ValueError(f"{label} has a blank ticker")
        if ticker in by_ticker:
            raise ValueError(f"{label} has duplicate ticker={ticker}")
        row["ticker"] = ticker
        by_ticker[ticker] = row
    return next(iter(dates)), by_ticker


def _weights_from_score_row(
    row: Mapping[str, object],
    *,
    ticker: str,
) -> dict[str, float]:
    try:
        raw = json.loads(str(row.get("component_weights_json") or ""))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{ticker}: component_weights_json is invalid") from exc
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"{ticker}: component_weights_json must be an object")
    return {
        str(key): _finite(value, label=f"{ticker}.{key}.weight")
        for key, value in raw.items()
    }


def _weights_tie(
    actual: Mapping[str, float],
    expected: Mapping[str, float],
) -> bool:
    return set(actual) == set(expected) and all(
        math.isclose(actual[key], expected[key], abs_tol=1e-9)
        for key in expected
    )


def _validate_source_membership(
    row: Mapping[str, object],
    *,
    ticker: str,
    asof: date,
) -> tuple[str, str]:
    start = _iso(
        row.get("membership_start_date"),
        label=f"{ticker}.membership_start_date",
        required=False,
    )
    end = _iso(
        row.get("membership_end_date"),
        label=f"{ticker}.membership_end_date",
        required=False,
    )
    if start is not None and start > asof:
        raise ValueError(f"{ticker}: source membership starts after score date")
    if end is not None and end < asof:
        raise ValueError(f"{ticker}: source membership ended before score date")
    if start is not None and end is not None and start > end:
        raise ValueError(f"{ticker}: source membership interval is reversed")
    return (
        start.isoformat() if start is not None else "",
        end.isoformat() if end is not None else "",
    )


def _group_ranks(
    rows: Sequence[dict[str, object]],
) -> dict[str, int]:
    by_group: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        by_group[str(row["transportation_group_recipe_key"])].append(row)
    output: dict[str, int] = {}
    for group_rows in by_group.values():
        ordered = sorted(
            group_rows,
            key=lambda row: (
                -int(str(row.get("rank_ready_flag") or "0")),
                -float(str(row["final_score"])),
                str(row["ticker"]),
            ),
        )
        for rank, row in enumerate(ordered, start=1):
            output[str(row["ticker"])] = rank
    return output


def build_shadow_subgroup_rank_rows(
    *,
    source_rows: Sequence[Mapping[str, object]],
    subgroup_score_rows: Sequence[Mapping[str, object]],
    lock_payload: Mapping[str, object],
    activation_enabled: bool = False,
    allow_pre_effective_diagnostic_replay: bool = False,
) -> list[dict[str, str]]:
    """Apply sealed v8 group recipes while keeping every investable gate off."""
    if activation_enabled:
        raise ValueError(
            "Transportation subgroup activation is fail-closed until "
            "independent future-only evidence passes and is separately activated"
        )
    spec: TransportationSubgroupLockSpec = validate_subgroup_lock_payload(
        lock_payload
    )
    asof_text, sources = _unique_rows(source_rows, label="source score rows")
    subgroup_asof, subgroup = _unique_rows(
        subgroup_score_rows,
        label="subgroup score rows",
    )
    if subgroup_asof != asof_text:
        raise ValueError("source and subgroup score dates do not match")
    if set(sources) != set(subgroup):
        missing = sorted(set(sources) - set(subgroup))
        extra = sorted(set(subgroup) - set(sources))
        raise ValueError(
            f"subgroup score ticker census mismatch missing={missing} extra={extra}"
        )
    asof = _iso(asof_text, label="asof_date")
    assert asof is not None
    if asof >= spec.policy_effective_from:
        expected_current = {
            ticker
            for ticker, memberships in spec.memberships.items()
            if any(
                item.membership_scope == "current_recipe"
                for item in memberships
            )
        }
        if set(sources) != expected_current:
            missing = sorted(expected_current - set(sources))
            extra = sorted(set(sources) - expected_current)
            raise ValueError(
                "current subgroup ticker census does not tie to the locked "
                f"policy missing={missing} extra={extra}"
            )

    prepared: list[dict[str, object]] = []
    group_counts: dict[str, int] = defaultdict(int)
    for ticker, source in sorted(sources.items()):
        score_row = subgroup[ticker]
        membership = spec.membership_for(ticker, asof)
        if (
            membership is None
            and allow_pre_effective_diagnostic_replay
            and asof < spec.policy_effective_from
        ):
            future_current = [
                item
                for item in spec.memberships.get(ticker, ())
                if item.membership_scope == "current_recipe"
                and item.effective_from == spec.policy_effective_from
            ]
            if len(future_current) > 1:
                raise ValueError(
                    f"{ticker}: ambiguous pre-effective diagnostic recipe"
                )
            if future_current:
                locked = future_current[0]
                membership = TransportationGroupMembership(
                    ticker=ticker,
                    cohort_id=locked.cohort_id,
                    group_id=locked.group_id,
                    membership_scope=(
                        "pre_effective_policy_diagnostic_replay"
                    ),
                    effective_from=asof,
                    effective_to=asof,
                )
        if membership is None:
            raise ValueError(
                f"{ticker}: no point-in-time/current subgroup membership"
            )
        recipe_key = f"{membership.cohort_id}::{membership.group_id}"
        recipe = spec.groups.get(recipe_key)
        if recipe is None:
            raise ValueError(f"{ticker}: missing group recipe={recipe_key}")
        actual_key = (
            f"{score_row.get('v8_cohort_id')}::{score_row.get('v8_group_id')}"
        )
        if actual_key != recipe_key:
            raise ValueError(
                f"{ticker}: subgroup score maps to {actual_key}, expected {recipe_key}"
            )
        if str(score_row.get("ranking_mode") or "") != recipe.ranking_mode:
            raise ValueError(f"{ticker}: ranking mode does not tie to recipe")
        active = _flag(
            score_row.get("specialized_pack_active_flag"),
            label=f"{ticker}.specialized_pack_active_flag",
        )
        component_state = "active" if active else "fallback"
        expected_weights = (
            recipe.component_weights_active
            if active
            else recipe.component_weights_fallback
        )
        applied_weights = _weights_from_score_row(score_row, ticker=ticker)
        if not _weights_tie(applied_weights, expected_weights):
            raise ValueError(
                f"{ticker}: applied component weights do not tie to {component_state} recipe"
            )
        group_cross_section_ready = _flag(
            score_row.get("group_cross_section_ready_flag"),
            label=f"{ticker}.group_cross_section_ready_flag",
        )
        group_specialized_ready = _flag(
            score_row.get("group_specialized_ready_flag"),
            label=f"{ticker}.group_specialized_ready_flag",
        )
        source_ready = _flag(
            source.get("rank_ready_flag"),
            label=f"{ticker}.rank_ready_flag",
        )
        rank_ready = (
            source_ready
            and group_cross_section_ready
            and group_specialized_ready
        )
        score = _finite(
            score_row.get("v8_group_percentile_score"),
            label=f"{ticker}.v8_group_percentile_score",
        )
        if not 0.0 <= score <= 100.0:
            raise ValueError(f"{ticker}: subgroup percentile is outside 0..100")
        source_start, source_end = _validate_source_membership(
            source,
            ticker=ticker,
            asof=asof,
        )
        membership_start = (
            membership.effective_from.isoformat()
            if membership.effective_from is not None
            else source_start
        )
        membership_end = (
            membership.effective_to.isoformat()
            if membership.effective_to is not None
            else source_end
        )
        weight_sha256 = canonical_sha256(applied_weights)
        score_sha256 = canonical_sha256(
            {
                "asof_date": asof_text,
                "ticker": ticker,
                "policy_sha256": spec.policy_sha256,
                "recipe_sha256": recipe.recipe_sha256,
                "component_weights_sha256": weight_sha256,
                "component_recipe_state": component_state,
                "score": score,
                "source_score_sha256": str(
                    score_row.get("source_score_sha256") or ""
                ),
            }
        )
        row: dict[str, object] = {
            field: str(source.get(field) or "")
            for field in SCORING_FEATURE_FIELDS
        }
        row.update(
            {
                "final_score": f"{score:.10f}",
                "rank_ready_flag": "1" if rank_ready else "0",
                "rank_ready_reason": (
                    "ok"
                    if rank_ready
                    else "subgroup_cross_section_not_ready"
                    if not group_cross_section_ready
                    else "subgroup_specialized_pack_not_ready"
                    if not group_specialized_ready
                    else str(source.get("rank_ready_reason") or "source_not_rank_ready")
                ),
                "model_status": "complete" if rank_ready else "incomplete",
                "score_model_version": SHADOW_SCORE_MODEL_VERSION,
                "model_version": SHADOW_MODEL_VERSION,
                "scoring_contract_version": SHADOW_CONTRACT_VERSION,
                "portfolio_candidate_gate": "0",
                "portfolio_candidate_score": f"{score:.10f}",
                "portfolio_candidate_status": "shadow_only",
                "portfolio_candidate_reason": "shadow_subgroup_evidence_not_authorized",
                "calibration_eligible_flag": "1" if rank_ready else "0",
                "research_calibration_input_eligible_flag": "0",
                "research_calibration_reason": "current_snapshot_not_survivorship_corrected",
                "calibration_sample_role": "excluded",
                "stage11_calibration_panel_source": "current_transportation_subgroup_snapshot",
                "stage11_calibration_input_eligible_flag": "0",
                "stage11_calibration_input_reason": "current_snapshot_not_survivorship_corrected",
                "survivorship_corrected_panel_flag": "0",
                "oos_score_valid_flag": "0",
                "oos_score_asof_date": "",
                "oos_invalid_reason": "shadow_subgroup_future_evidence_not_available",
                "calibration_lock_date": "",
                "transportation_scoring_mode": "subgroup_v8",
                "transportation_production_state": "shadow",
                "transportation_group_recipe_version": spec.recipe_version,
                "transportation_subgroup_policy_sha256": spec.policy_sha256,
                "transportation_cohort_id": membership.cohort_id,
                "transportation_group_id": membership.group_id,
                "transportation_group_recipe_key": recipe_key,
                "transportation_group_recipe_sha256": recipe.recipe_sha256,
                "transportation_group_ranking_mode": recipe.ranking_mode,
                "transportation_group_aggregate_weight": f"{recipe.aggregate_group_weight:.10f}",
                "transportation_membership_scope": membership.membership_scope,
                "transportation_membership_effective_from": membership_start,
                "transportation_membership_effective_to": membership_end,
                "transportation_component_recipe_state": component_state,
                "transportation_applied_component_weights_sha256": weight_sha256,
                "transportation_subgroup_score_sha256": score_sha256,
                "transportation_expected_group_count": str(len(spec.groups)),
                "transportation_expected_ticker_count": str(len(sources)),
                "transportation_production_lock_id": "",
                "transportation_decision_manifest_sha256": "",
            }
        )
        group_counts[recipe_key] += 1
        prepared.append(row)

    observed_groups = {
        str(row["transportation_group_recipe_key"]) for row in prepared
    }
    if observed_groups != set(spec.groups):
        raise ValueError(
            "subgroup score snapshot is missing one or more locked group recipes: "
            f"expected={sorted(spec.groups)} observed={sorted(observed_groups)}"
        )
    group_rank = _group_ranks(prepared)
    for row in prepared:
        ticker = str(row["ticker"])
        key = str(row["transportation_group_recipe_key"])
        row["transportation_group_rank"] = str(group_rank[ticker])
        row["transportation_group_expected_ticker_count"] = str(
            group_counts[key]
        )

    ordered = sorted(
        prepared,
        key=lambda row: (
            -int(str(row.get("rank_ready_flag") or "0")),
            -float(str(row["final_score"])),
            str(row["ticker"]),
        ),
    )
    cohort_rows: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in ordered:
        cohort_rows[str(row["calibration_pool"])].append(row)
    cohort_rank: dict[str, int] = {}
    for rows in cohort_rows.values():
        for rank, row in enumerate(rows, start=1):
            cohort_rank[str(row["ticker"])] = rank
    output: list[dict[str, str]] = []
    for final_rank, row in enumerate(ordered, start=1):
        row["final_rank"] = str(final_rank)
        row["cohort_rank"] = str(cohort_rank[str(row["ticker"])])
        output.append(
            {field: str(row.get(field) or "") for field in FINAL_RANK_FIELDS}
        )
    errors = validate_rank_rows(output, asof=asof_text)
    if errors:
        raise ValueError("; ".join(errors[:20]))
    return output
