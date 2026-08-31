"""Canonical governing-v7 Transportation prospective evaluation."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from future_only_evidence.authority_config import (
    DEFAULT_AUTHORITY_REGISTRY,
    load_pinned_authority,
)
from future_only_evidence.outcome_integrity import OUTCOME_SOURCE_ROLES
from future_only_evidence.protocol import canonical_sha256, file_sha256
from future_only_evidence.prospective_contracts import normalize_signal_rows
from future_only_evidence.prospective_evaluator import (
    apply_costs,
    build_ranked_periods,
    deterministic_nonoverlap,
    equal_weight_monitor_periods,
    load_verified_evidence,
    scope_verdict,
    weighted_verdict_periods,
)

from .future_oos_capture_v2 import (
    validate_governing_contracts,
    validate_membership_snapshot,
)
from .future_oos_capture_v5 import (
    TRANSPORT_CONTRACT,
    _policy_weights,
    derive_transport_signals,
)
from .future_oos_protocol_v1 import TRANSPORT_POLICY, validate_fresh_sources


EXPECTED_SLEEVES = {
    "north_american_surface_freight_and_logistics_v5": {
        "rail_networks",
        "ltl_carriers",
        "truckload_intermodal",
        "asset_light_logistics",
        "integrated_parcel",
    },
    "oil_tanker_operators_v5": {"oil_tankers"},
}
PARCEL_GROUP = "integrated_parcel"


def _revalidate_domain_capture(capture: Mapping[str, Any]) -> tuple[dict[str, dict[str, float]], dict[str, str]]:
    if capture.get("domain_schema_version") != "transportation_future_only_signal_capture_v5":
        raise ValueError("legacy Transportation capture cannot satisfy canonical future evidence")
    source_paths = {
        role: Path(identity["path"])
        for role, identity in capture["source_identities"].items()
    }
    governance = validate_governing_contracts(
        v8_policy_path=source_paths["v8_policy"],
        v7_research_decision_path=source_paths["v7_research_decision"],
    )
    if governance != capture.get("governing_contract_audit"):
        raise ValueError("Transportation governing contract audit is not reproducible")
    membership = validate_membership_snapshot(
        asof_date=str(capture["asof_date"]),
        membership_path=source_paths["membership_snapshot"],
        score_path=source_paths["canonical_v8_score"],
        rank_path=source_paths["canonical_v8_rank"],
        source_manifest_path=source_paths["source_manifest"],
    )
    if membership != capture.get("membership_audit"):
        raise ValueError("Transportation membership audit is not reproducible")
    score_rows, rank_rows, manifest = validate_fresh_sources(
        asof_date=str(capture["asof_date"]),
        capture_date=str(capture["asof_date"]),
        score_path=source_paths["canonical_v8_score"],
        rank_path=source_paths["canonical_v8_rank"],
        source_manifest_path=source_paths["source_manifest"],
    )
    if (
        manifest.get("evidence_role") != "prospective_future_only_capture"
        or manifest.get("source_generation_state")
        != "canonical_v8_outcome_blind_frozen_before_entry"
    ):
        raise ValueError("Transportation capture manifest is not exact prospective state")
    weights, modes = _policy_weights(source_paths["v8_policy"])
    derived, coverage = derive_transport_signals(
        score_rows=score_rows,
        rank_rows=rank_rows,
        group_weights=weights,
        group_modes=modes,
        asof_date=str(capture["asof_date"]),
    )
    if normalize_signal_rows(derived, asof_date=str(capture["asof_date"])) != capture["signal_rows"]:
        raise ValueError("Transportation capture differs from exact score/rank bytes")
    if coverage != capture.get("sleeve_coverage_gates"):
        raise ValueError("Transportation capture coverage gate is not reproducible")
    if weights != capture.get("frozen_group_weights") or modes != capture.get("group_ranking_modes"):
        raise ValueError("Transportation capture group construction changed")
    return weights, modes


def evaluate(
    *,
    capture_paths: Sequence[Path],
    capture_registry_path: Path,
    capture_registry_receipt_path: Path,
    expected_capture_registry_receipt_sha256: str,
    outcome_path: Path,
    outcome_source_paths: Mapping[str, Path],
    outcome_receipt_path: Path,
    expected_outcome_receipt_sha256: str,
    trading_calendar_path: Path,
    trusted_public_key_path: Path,
    authority_registry_path: Path = DEFAULT_AUTHORITY_REGISTRY,
    evaluated_at_utc: str,
) -> dict[str, Any]:
    if set(outcome_source_paths) != OUTCOME_SOURCE_ROLES:
        raise ValueError("Transportation outcome source roles changed")
    authority, authority_audit = load_pinned_authority(
        "transportation",
        public_key_path=trusted_public_key_path,
        registry_path=authority_registry_path,
    )
    evidence = load_verified_evidence(
        contract=TRANSPORT_CONTRACT,
        authority=authority,
        capture_paths=capture_paths,
        capture_registry_path=capture_registry_path,
        capture_registry_receipt_path=capture_registry_receipt_path,
        expected_capture_registry_receipt_sha256=expected_capture_registry_receipt_sha256,
        outcome_path=outcome_path,
        outcome_source_paths=outcome_source_paths,
        outcome_receipt_path=outcome_receipt_path,
        expected_outcome_receipt_sha256=expected_outcome_receipt_sha256,
        trading_calendar_path=trading_calendar_path,
        evaluated_at_utc=evaluated_at_utc,
    )
    weights_by_sleeve: dict[str, dict[str, float]] | None = None
    modes: dict[str, str] | None = None
    for capture in evidence["captures"]:
        weights, capture_modes = _revalidate_domain_capture(capture)
        if weights_by_sleeve is None:
            weights_by_sleeve, modes = weights, capture_modes
        elif weights != weights_by_sleeve or capture_modes != modes:
            raise ValueError("Transportation frozen group construction changed across captures")
    if weights_by_sleeve is None or modes is None:
        raise ValueError("Transportation has no canonical captures")
    if set(weights_by_sleeve) != set(EXPECTED_SLEEVES):
        raise ValueError("Transportation policy sleeve census changed")
    observed_groups = {
        sleeve: {
            row["group_id"]
            for capture in evidence["captures"]
            for row in capture["signal_rows"]
            if row["sleeve_id"] == sleeve
        }
        for sleeve in EXPECTED_SLEEVES
    }
    group_verdicts: list[dict[str, Any]] = []
    aggregate_verdicts: list[dict[str, Any]] = []
    parcel_audit: list[dict[str, Any]] = []
    deployed_surface_monitor: list[dict[str, Any]] = []
    sleeve_verdicts: list[dict[str, Any]] = []
    costed_by_group_horizon: dict[tuple[str, str, int], list[dict[str, Any]]] = {}
    parcel_by_horizon: dict[int, list[dict[str, Any]]] = {}
    for sleeve, expected_groups in EXPECTED_SLEEVES.items():
        if observed_groups[sleeve] != expected_groups:
            missing = sorted(expected_groups - observed_groups[sleeve])
            extra = sorted(observed_groups[sleeve] - expected_groups)
            raise ValueError(f"{sleeve}: capture group census mismatch missing={missing} extra={extra}")
        for group in sorted(expected_groups):
            for horizon in TRANSPORT_CONTRACT.horizons:
                if modes[group] == "eligibility_equal_weight":
                    periods = equal_weight_monitor_periods(
                        evidence,
                        sleeve_id=sleeve,
                        group_id=group,
                        horizon=horizon,
                        transaction_cost_bps=TRANSPORT_CONTRACT.transaction_cost_bps,
                    )
                    parcel_by_horizon[horizon] = periods
                    parcel_audit.append(
                        {
                            "scope_kind": "group",
                            "scope_id": group,
                            "sleeve_id": sleeve,
                            "horizon_sessions": horizon,
                            "applicability": "not_applicable",
                            "pass": None,
                            "action": "monitor_eligibility_return_and_cost_not_predictive_gate",
                            "reason": "eligibility_equal_weight_has_no_rank_spread",
                            "periods": periods,
                            "initial_cost_charged_pass": bool(periods)
                            and periods[0]["entry_turnover"] == 1.0,
                            "final_cost_charged_pass": bool(periods)
                            and periods[-1]["exit_turnover"] == 1.0,
                        }
                    )
                    continue
                periods = build_ranked_periods(
                    evidence,
                    sleeve_id=sleeve,
                    group_id=group,
                    horizon=horizon,
                )
                costed = apply_costs(
                    deterministic_nonoverlap(periods),
                    transaction_cost_bps=TRANSPORT_CONTRACT.transaction_cost_bps,
                )
                costed_by_group_horizon[(sleeve, group, horizon)] = costed
                verdict = scope_verdict(
                    costed,
                    contract=TRANSPORT_CONTRACT,
                    horizon=horizon,
                    minimum_cross_section=int(TRANSPORT_POLICY.minimum_cross_sections[group]),
                    efficacy_field="top_minus_cohort_net",
                    hit_field="top_minus_cohort_net",
                )
                group_verdicts.append(
                    {
                        "scope_kind": "ranked_group",
                        "scope_id": group,
                        "sleeve_id": sleeve,
                        "applicability": "predictive_gate",
                        **verdict,
                    }
                )
    for sleeve, expected_groups in EXPECTED_SLEEVES.items():
        ranked_groups = sorted(group for group in expected_groups if modes[group] == "ranked")
        sleeve_aggregates: list[dict[str, Any]] = []
        for horizon in TRANSPORT_CONTRACT.horizons:
            group_periods = {
                group: costed_by_group_horizon[(sleeve, group, horizon)]
                for group in ranked_groups
            }
            ranked_weights = {group: weights_by_sleeve[sleeve][group] for group in ranked_groups}
            weighted = weighted_verdict_periods(group_periods, group_weights=ranked_weights)
            verdict = scope_verdict(
                weighted,
                contract=TRANSPORT_CONTRACT,
                horizon=horizon,
                minimum_cross_section=int(TRANSPORT_POLICY.minimum_cross_sections[sleeve]),
                efficacy_field="top_minus_cohort_net",
                hit_field="top_minus_cohort_net",
            )
            aggregate = {
                "scope_kind": "fixed_weight_predictive_sleeve",
                "scope_id": sleeve,
                "sleeve_id": sleeve,
                "predictive_group_weights": ranked_weights,
                "predictive_group_weight_total": sum(ranked_weights.values()),
                **verdict,
            }
            aggregate_verdicts.append(aggregate)
            sleeve_aggregates.append(aggregate)
            if sleeve == "north_american_surface_freight_and_logistics_v5":
                parcel_index = {
                    row["capture_id"]: row for row in parcel_by_horizon.get(horizon, [])
                }
                for row in weighted:
                    parcel = parcel_index.get(row["capture_id"])
                    if parcel is None:
                        raise ValueError("surface deployed aggregate is missing parcel monitoring return")
                    deployed_surface_monitor.append(
                        {
                            "capture_id": row["capture_id"],
                            "horizon_sessions": horizon,
                            "ranked_predictive_weight": 0.90,
                            "parcel_operational_weight": 0.10,
                            "deployed_gross_return": 0.90 * row["top_gross"]
                            + 0.10 * parcel["gross_return"],
                            "deployed_net_return": 0.90 * row["top_net"]
                            + 0.10 * parcel["net_return"],
                            "predictive_gate_uses_parcel": False,
                            "deployed_weight_tie_out_pass": True,
                        }
                    )
        group_rows = [row for row in group_verdicts if row["sleeve_id"] == sleeve]
        group_census_complete = {
            (row["scope_id"], row["horizon_sessions"]) for row in group_rows
        } == {
            (group, horizon)
            for group in ranked_groups
            for horizon in TRANSPORT_CONTRACT.horizons
        }
        group_pass = group_census_complete and all(row["pass"] for row in group_rows)
        aggregate_pass = (
            {row["horizon_sessions"] for row in sleeve_aggregates}
            == set(TRANSPORT_CONTRACT.horizons)
            and all(row["pass"] for row in sleeve_aggregates)
        )
        coverage_pass = all(
            bool(capture.get("sleeve_coverage_gates", {}).get(sleeve, False))
            for capture in evidence["captures"]
        )
        pass_flag = group_pass and aggregate_pass and coverage_pass
        sleeve_verdicts.append(
            {
                "sleeve_id": sleeve,
                "ranked_group_census_complete": group_census_complete,
                "all_ranked_groups_pass": group_pass,
                "fixed_weight_predictive_aggregate_pass": aggregate_pass,
                "coverage_gate_pass": coverage_pass,
                "parcel_predictive_status": (
                    "not_applicable_monitor_only"
                    if sleeve == "north_american_surface_freight_and_logistics_v5"
                    else "not_present"
                ),
                "pass": pass_flag,
                "action": (
                    "submit_this_sleeve_for_independent_promotion_review"
                    if pass_flag
                    else "remain_shadow_fail_closed"
                ),
                "production_activation_authorized": False,
            }
        )
    passing = [row["sleeve_id"] for row in sleeve_verdicts if row["pass"]]
    body: dict[str, Any] = {
        "schema_version": "transportation_future_only_evaluation_v5",
        "state": "evaluated_future_only",
        "evidence_class": "prospective_future_only",
        "family": "transportation",
        "policy_id": TRANSPORT_CONTRACT.policy_id,
        "target_contract": {
            "target_field": "forward_iyt_excess_return",
            "benchmark_ticker": "IYT",
            "horizons": list(TRANSPORT_CONTRACT.horizons),
            "return_convention": TRANSPORT_CONTRACT.identity()["return_convention"],
        },
        "evaluated_at_utc": datetime.fromisoformat(
            str(evaluated_at_utc).replace("Z", "+00:00")
        ).astimezone(timezone.utc).isoformat(),
        "trusted_authority_audit": {
            **authority_audit,
            "authority_registry_sha256": file_sha256(authority_registry_path),
        },
        "capture_registry_audit": evidence["capture_registry_audit"],
        "due_capture_census_audit": evidence["due_capture_census_audit"],
        "outcome_integrity_audit": evidence["outcome_integrity_audit"],
        "exact_all_matured_outcome_census_pass": True,
        "group_scope_verdicts": group_verdicts,
        "fixed_weight_aggregate_verdicts": aggregate_verdicts,
        "not_applicable_group_audit": parcel_audit,
        "deployed_surface_weight_monitor": deployed_surface_monitor,
        "sleeve_verdicts": sleeve_verdicts,
        "passing_sleeves": passing,
        "blocked_sleeves": [row["sleeve_id"] for row in sleeve_verdicts if not row["pass"]],
        "any_sleeve_pass": bool(passing),
        "sector_wide_all_sleeves_pass": len(passing) == len(EXPECTED_SLEEVES),
        "action": (
            "submit_passing_sleeves_for_independent_review"
            if passing
            else "remain_shadow_fail_closed"
        ),
        "independent_promotion_review_required": True,
        "production_activation_authorized": False,
        "portfolio_write_enabled": False,
        "optimizer_cap": 0.0,
    }
    body["payload_sha256"] = canonical_sha256(body)
    return body


__all__ = ["EXPECTED_SLEEVES", "PARCEL_GROUP", "evaluate"]
