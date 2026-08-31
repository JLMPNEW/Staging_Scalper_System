"""Canonical Transportation v6 future evaluation with independent sleeve verdicts."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping, Sequence

from future_only_evidence.canonical_domain import (
    require_receipt_contract_binding,
    revalidate_capture_timestamp,
    validate_canonical_outcome_attestations,
    validate_capture_timestamp_chain,
    validate_market_source_provenance,
)
from future_only_evidence.canonical_trust import CanonicalTrustBundle
from future_only_evidence.canonical_values import exact_utc
from future_only_evidence.lifecycle_snapshot import (
    validate_lifecycle_capture_chronology,
)
from future_only_evidence.protocol import canonical_sha256
from future_only_evidence.prospective_contracts import (
    ProspectiveContract,
    normalize_signal_rows,
    read_calendar_bytes,
    scheduled_asofs,
)
from future_only_evidence.prospective_evaluator import (
    apply_costs,
    build_ranked_periods,
    deterministic_nonoverlap,
    equal_weight_monitor_periods,
    load_verified_evidence,
    scope_verdict,
    weighted_verdict_periods,
)
from future_only_evidence.transport_score_input_availability import (
    validate_transport_score_input_availability_capture_chronology,
)

from .future_oos_activation_v6 import (
    GROUP_MODES,
    GROUP_MINIMUM_CROSS_SECTIONS,
    GROUP_TICKERS,
    GROUP_WEIGHTS,
    POLICY_ID,
    SCORE_REPLAY_CONTRACT,
    TARGET_CONTRACT,
    validate_activation_plan_v6,
)
from .future_oos_capture_v5 import SOURCE_GENERATION_STATE, derive_transport_signals
from .future_oos_capture_v6 import (
    CAPTURE_DOMAIN_SCHEMA,
    REQUIRED_CAPTURE_ROLES_V6,
    validate_exact_transport_census,
)
from .future_oos_protocol_v1 import validate_fresh_sources
from .future_oos_score_lineage_v1 import (
    SOURCE_ROLES,
    validate_and_replay_transport_scores,
)


EVALUATION_SCHEMA = "transportation_future_only_evaluation_v6"
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


def _revalidate_domain_capture(
    capture: Mapping[str, Any],
    *,
    registered_plan_sha256: str,
    domain_contract_sha256: str,
    registered_source_sha256: Mapping[str, str],
    source_snapshot_bytes: Mapping[str, bytes],
    capture_receipt_snapshot_bytes: bytes,
    bundle: CanonicalTrustBundle,
    contract: ProspectiveContract,
    calendar_rows: Sequence[Mapping[str, Any]],
    predecessor_replay_audit: Mapping[str, Any] | None,
    registered_baseline_availability_audit: Mapping[str, Any],
) -> dict[str, Any]:
    if (
        capture.get("domain_schema_version") != CAPTURE_DOMAIN_SCHEMA
        or capture.get("domain_contract_sha256") != domain_contract_sha256
        or capture.get("activation_plan_sha256") != registered_plan_sha256
    ):
        raise ValueError("Transportation capture is outside the canonical v6 domain")
    source_identities = capture.get("source_identities")
    archive_audit = capture.get("content_addressed_archive_audit")
    if (
        not isinstance(source_identities, dict)
        or set(source_identities) != REQUIRED_CAPTURE_ROLES_V6
        or set(source_snapshot_bytes) != REQUIRED_CAPTURE_ROLES_V6
        or not isinstance(archive_audit, dict)
        or set(archive_audit) != REQUIRED_CAPTURE_ROLES_V6
    ):
        raise ValueError("Transportation capture lacks the exact archived source census")
    source_paths = {
        role: Path(str(identity["path"]))
        for role, identity in source_identities.items()
    }
    for role, identity in source_identities.items():
        archived = archive_audit[role]
        if (
            archived.get("archive_path") != identity.get("path")
            or archived.get("sha256") != identity.get("sha256")
            or hashlib.sha256(source_snapshot_bytes[role]).hexdigest()
            != identity.get("sha256")
        ):
            raise ValueError(f"Transportation archived source audit changed: {role}")
    for role, expected_hash in registered_source_sha256.items():
        if role not in source_identities or source_identities[role].get("sha256") != expected_hash:
            raise ValueError(f"Transportation capture changed activation source: {role}")
    receipt_path = Path(str(capture["trusted_receipt"]["path"]))
    require_receipt_contract_binding(
        receipt_path,
        expected_domain_contract_sha256=domain_contract_sha256,
        receipt_snapshot_bytes=capture_receipt_snapshot_bytes,
    )
    score_rows, rank_rows, manifest = validate_fresh_sources(
        asof_date=str(capture["asof_date"]),
        capture_date=str(capture["asof_date"]),
        score_path=source_paths["canonical_v8_score"],
        rank_path=source_paths["canonical_v8_rank"],
        source_manifest_path=source_paths["source_manifest"],
        source_snapshot_bytes={
            role: source_snapshot_bytes[role]
            for role in (
                "canonical_v8_score",
                "canonical_v8_rank",
                "source_manifest",
            )
        },
    )
    if (
        manifest.get("evidence_role") != "prospective_future_only_capture"
        or manifest.get("source_generation_state") != SOURCE_GENERATION_STATE
        or manifest.get("return_target")
        != {
            "benchmark_ticker": "IYT",
            "return_convention": "next_session_open_execution_total_return_v1",
            "target_field": "forward_iyt_excess_return_at_fixed_session_horizon",
        }
    ):
        raise ValueError("Transportation archived source manifest changed")
    derived, coverage = derive_transport_signals(
        score_rows=score_rows,
        rank_rows=rank_rows,
        group_weights=GROUP_WEIGHTS,
        group_modes=GROUP_MODES,
        asof_date=str(capture["asof_date"]),
    )
    replay_roles = set(SOURCE_ROLES)
    capture_asof = str(capture["asof_date"])
    scheduled = scheduled_asofs(
        contract,
        calendar_rows=calendar_rows,
        complete_through_asof=capture_asof,
    )
    if not scheduled or scheduled[-1] != capture_asof:
        raise ValueError("Transportation evaluated capture is outside the schedule")
    predecessor_availability_audit = (
        registered_baseline_availability_audit
        if predecessor_replay_audit is None
        else predecessor_replay_audit.get("score_input_availability_audit")
    )
    if not isinstance(predecessor_availability_audit, Mapping):
        raise ValueError(
            "Transportation predecessor score-input availability audit is absent"
        )
    score_replay = validate_and_replay_transport_scores(
        asof_date=capture_asof,
        signal_cutoff_at_utc=str(
            capture["trusted_capture_timing"]["signal_information_cutoff_at_utc"]
        ),
        scheduled_append_asof_dates=scheduled,
        score_path=source_paths["canonical_v8_score"],
        scoring_panel_path=source_paths["scoring_panel"],
        accepted_facts_path=source_paths["accepted_facts"],
        score_replay_baseline_path=source_paths["score_replay_baseline"],
        score_input_availability_baseline_snapshot_path=source_paths[
            "score_input_availability_baseline_snapshot"
        ],
        score_input_availability_baseline_attestation_path=source_paths[
            "score_input_availability_baseline_attestation"
        ],
        score_input_availability_snapshot_path=source_paths[
            "score_input_availability_snapshot"
        ],
        score_input_availability_attestation_path=source_paths[
            "score_input_availability_attestation"
        ],
        v8_policy_path=source_paths["v8_policy"],
        policy_id=POLICY_ID,
        canonical_trust_bundle=bundle,
        expected_sha256={
            role: str(source_identities[role]["sha256"])
            for role in replay_roles
        },
        predecessor_replay_audit=predecessor_replay_audit,
        predecessor_score_input_availability_audit=(
            predecessor_availability_audit
        ),
        source_snapshot_bytes={
            role: source_snapshot_bytes[role] for role in replay_roles
        },
    )
    if (
        {field: score_replay.get(field) for field in SCORE_REPLAY_CONTRACT}
        != SCORE_REPLAY_CONTRACT
        or score_replay != capture.get("frozen_score_replay_audit")
    ):
        raise ValueError("Transportation frozen score replay audit is not reproducible")
    resolved, census = validate_exact_transport_census(
        signals=derived,
        membership_path=source_paths["membership_snapshot"],
        asof_date=str(capture["asof_date"]),
        score_replay_audit=score_replay,
        lifecycle_snapshot_path=source_paths["lifecycle_event_snapshot"],
        lifecycle_attestation_path=source_paths["lifecycle_source_attestation"],
        expected_lifecycle_attestation_sha256=str(
            source_identities["lifecycle_source_attestation"]["sha256"]
        ),
        trust_bundle=bundle,
        signal_cutoff_at_utc=str(
            capture["trusted_capture_timing"]["signal_information_cutoff_at_utc"]
        ),
        membership_snapshot_bytes=source_snapshot_bytes["membership_snapshot"],
        lifecycle_snapshot_bytes=source_snapshot_bytes["lifecycle_event_snapshot"],
        lifecycle_attestation_bytes=source_snapshot_bytes[
            "lifecycle_source_attestation"
        ],
    )
    validate_lifecycle_capture_chronology(
        census["independent_lifecycle_source_audit"],
        trusted_capture_timing=capture["trusted_capture_timing"],
        captured_at_utc=capture["captured_at_utc"],
        label="Transportation evaluator",
    )
    validate_transport_score_input_availability_capture_chronology(
        score_replay["score_input_availability_audit"],
        trusted_capture_timing=capture["trusted_capture_timing"],
        captured_at_utc=capture["captured_at_utc"],
        label="Transportation evaluator",
    )
    if normalize_signal_rows(
        resolved, asof_date=str(capture["asof_date"])
    ) != capture["signal_rows"]:
        raise ValueError(
            "Transportation capture differs from deterministic score/lifecycle replay"
        )
    if (
        coverage != capture.get("sleeve_coverage_gates")
        or coverage.get("oil_tanker_operators_v5") is not True
        or capture.get("frozen_group_weights") != GROUP_WEIGHTS
        or capture.get("group_ranking_modes") != GROUP_MODES
        or capture.get("group_ticker_census") != GROUP_TICKERS
        or capture.get("parcel_predictive_applicability")
        != "not_applicable_monitor_only"
    ):
        raise ValueError("Transportation capture changed group/coverage construction")
    if census != capture.get("registered_census_audit"):
        raise ValueError("Transportation registered census audit is not reproducible")
    if any(
        hashlib.sha256(source_snapshot_bytes[role]).hexdigest()
        != identity["sha256"]
        for role, identity in source_identities.items()
    ):
        raise ValueError("Transportation archived source changed during semantic validation")
    return {
        "registered_census_audit": census,
        "frozen_score_replay_audit": score_replay,
    }


def _decision_parcel_periods(
    periods: Sequence[Mapping[str, Any]],
    *,
    count: int,
    transaction_cost_bps: float,
) -> list[dict[str, Any]]:
    rows = [dict(row) for row in periods[:count]]
    if not rows:
        return rows
    # Preserve any internal liquidation caused by a cash gap. Only force the
    # decision window's terminal liquidation on its last selected period.
    rows[-1]["exit_turnover"] = 1.0
    rate = transaction_cost_bps / 10_000.0
    for row in rows:
        row["net_return"] = float(row["gross_return"]) - rate * (
            float(row["entry_turnover"]) + float(row["exit_turnover"])
        )
    return rows


def evaluate_v6(
    *,
    activation_plan_path: Path,
    plan_source_paths: Mapping[str, Path],
    activation_receipt_path: Path,
    expected_activation_receipt_sha256: str,
    activation_timestamp_receipt_path: Path,
    expected_activation_timestamp_receipt_sha256: str,
    evidence_public_key_path: Path,
    timestamp_public_key_path: Path,
    market_data_public_key_path: Path,
    capture_paths: Sequence[Path],
    capture_registry_path: Path,
    capture_registry_receipt_path: Path,
    expected_capture_registry_receipt_sha256: str,
    capture_registry_timestamp_receipt_path: Path,
    expected_capture_registry_timestamp_receipt_sha256: str,
    outcome_path: Path,
    outcome_source_paths: Mapping[str, Path],
    outcome_receipt_path: Path,
    expected_outcome_receipt_sha256: str,
    outcome_timestamp_receipt_path: Path,
    expected_outcome_timestamp_receipt_sha256: str,
    market_export_receipt_path: Path,
    expected_market_export_receipt_sha256: str,
    trading_calendar_path: Path,
    evaluated_at_utc: str,
) -> dict[str, Any]:
    _, contract, bundle, plan_audit = validate_activation_plan_v6(
        activation_plan_path,
        source_paths=plan_source_paths,
        activation_receipt_path=activation_receipt_path,
        expected_activation_receipt_sha256=expected_activation_receipt_sha256,
        activation_timestamp_receipt_path=activation_timestamp_receipt_path,
        expected_activation_timestamp_receipt_sha256=(
            expected_activation_timestamp_receipt_sha256
        ),
        evidence_public_key_path=evidence_public_key_path,
        timestamp_public_key_path=timestamp_public_key_path,
        market_data_public_key_path=market_data_public_key_path,
    )
    evidence = load_verified_evidence(
        contract=contract,
        authority=bundle.evidence_seal,
        capture_paths=capture_paths,
        capture_registry_path=capture_registry_path,
        capture_registry_receipt_path=capture_registry_receipt_path,
        expected_capture_registry_receipt_sha256=(
            expected_capture_registry_receipt_sha256
        ),
        outcome_path=outcome_path,
        outcome_source_paths=outcome_source_paths,
        outcome_receipt_path=outcome_receipt_path,
        expected_outcome_receipt_sha256=expected_outcome_receipt_sha256,
        trading_calendar_path=trading_calendar_path,
        evaluated_at_utc=evaluated_at_utc,
    )
    calendar_digest = hashlib.sha256(
        evidence["_trading_calendar_snapshot_bytes"]
    ).hexdigest()
    if calendar_digest != plan_audit["domain_contract"][
        "registered_source_sha256"
    ]["trading_calendar"]:
        raise ValueError("Transportation evaluator calendar differs from activation")
    source_snapshots_by_id = evidence["_capture_source_snapshot_bytes_by_id"]
    receipt_snapshots_by_id = evidence["_capture_receipt_snapshot_bytes_by_id"]
    calendar_rows, _ = read_calendar_bytes(
        evidence["_trading_calendar_snapshot_bytes"]
    )
    timestamp_audits: list[dict[str, Any]] = []
    predecessor_replay_audit: Mapping[str, Any] | None = None
    for capture in evidence["captures"]:
        capture_id = str(capture["capture_id"])
        replay_validation = _revalidate_domain_capture(
            capture,
            registered_plan_sha256=plan_audit["registered_plan_sha256"],
            domain_contract_sha256=plan_audit["domain_contract_sha256"],
            registered_source_sha256=plan_audit["domain_contract"][
                "registered_source_sha256"
            ],
            source_snapshot_bytes=source_snapshots_by_id[capture_id],
            capture_receipt_snapshot_bytes=receipt_snapshots_by_id[capture_id],
            bundle=bundle,
            contract=contract,
            calendar_rows=calendar_rows,
            predecessor_replay_audit=predecessor_replay_audit,
            registered_baseline_availability_audit=plan_audit[
                "domain_contract"
            ]["score_input_availability_baseline_audit"],
        )
        predecessor_replay_audit = replay_validation[
            "frozen_score_replay_audit"
        ]
        timestamp_audits.append(
            revalidate_capture_timestamp(
                capture,
                bundle=bundle,
                capture_receipt_snapshot_bytes=receipt_snapshots_by_id[capture_id],
            )
        )
    chain = validate_capture_timestamp_chain(
        evidence["captures"],
        initial_anchor_audit=plan_audit["activation_external_timestamp"],
    )
    latest_exit = max(
        str(row["exit_execution_at_utc"]) for row in evidence["outcomes"]
    )
    source_hashes = evidence["outcome_integrity_audit"]["outcome_source_sha256"]
    attestations = validate_canonical_outcome_attestations(
        outcome_receipt_path=outcome_receipt_path,
        outcome_timestamp_receipt_path=outcome_timestamp_receipt_path,
        expected_outcome_timestamp_receipt_sha256=(
            expected_outcome_timestamp_receipt_sha256
        ),
        market_export_receipt_path=market_export_receipt_path,
        expected_market_export_receipt_sha256=expected_market_export_receipt_sha256,
        expected_outcome_receipt_sha256=expected_outcome_receipt_sha256,
        source_sha256=source_hashes,
        capture_registry_path=capture_registry_path,
        expected_capture_registry_sha256=evidence["capture_registry_audit"][
            "capture_registry_sha256"
        ],
        capture_registry_receipt_path=capture_registry_receipt_path,
        expected_capture_registry_receipt_sha256=(
            expected_capture_registry_receipt_sha256
        ),
        capture_registry_timestamp_receipt_path=(
            capture_registry_timestamp_receipt_path
        ),
        expected_capture_registry_timestamp_receipt_sha256=(
            expected_capture_registry_timestamp_receipt_sha256
        ),
        expected_domain_contract_sha256=plan_audit["domain_contract_sha256"],
        expected_latest_capture_log_head_sha256=chain["latest_log_head_sha256"],
        expected_latest_capture_log_sequence=chain["latest_log_sequence"],
        bundle=bundle,
        evaluated_at_utc=evaluated_at_utc,
        latest_exit_execution_at_utc=latest_exit,
        capture_registry_snapshot_bytes=evidence[
            "_capture_registry_snapshot_bytes"
        ],
        capture_registry_receipt_snapshot_bytes=evidence[
            "_capture_registry_receipt_snapshot_bytes"
        ],
        outcome_receipt_snapshot_bytes=evidence[
            "_outcome_receipt_snapshot_bytes"
        ],
    )
    market_provenance = validate_market_source_provenance(
        outcome_source_paths=outcome_source_paths,
        market_export_attestation=attestations["market_data_export_attestation"],
        expected_source_sha256=source_hashes,
        bundle=bundle,
        expected_benchmark_ticker="IYT",
        outcome_source_snapshot_bytes=evidence["_outcome_source_snapshot_bytes"],
    )
    group_verdicts: list[dict[str, Any]] = []
    aggregate_verdicts: list[dict[str, Any]] = []
    parcel_audit: list[dict[str, Any]] = []
    deployed_surface_monitor: list[dict[str, Any]] = []
    costed_by_group_horizon: dict[tuple[str, str, int], list[dict[str, Any]]] = {}
    parcel_by_horizon: dict[int, list[dict[str, Any]]] = {}
    for sleeve, groups in EXPECTED_SLEEVES.items():
        for group in sorted(groups):
            for horizon in contract.horizons:
                required_count = contract.minimum_counts[horizon]
                if GROUP_MODES[group] == "eligibility_equal_weight":
                    all_periods = equal_weight_monitor_periods(
                        evidence,
                        sleeve_id=sleeve,
                        group_id=group,
                        horizon=horizon,
                        transaction_cost_bps=contract.transaction_cost_bps,
                    )
                    periods = _decision_parcel_periods(
                        all_periods,
                        count=required_count,
                        transaction_cost_bps=contract.transaction_cost_bps,
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
                            "action": "monitor_only_excluded_from_predictive_denominator",
                            "reason": "eligibility_equal_weight_has_no_rank_spread",
                            "decision_period_count": len(periods),
                            "remaining_count": max(0, required_count - len(periods)),
                            "periods": periods,
                        }
                    )
                    continue
                all_periods = deterministic_nonoverlap(
                    build_ranked_periods(
                        evidence,
                        sleeve_id=sleeve,
                        group_id=group,
                        horizon=horizon,
                    )
                )
                decision = all_periods[:required_count]
                costed = apply_costs(
                    decision,
                    transaction_cost_bps=contract.transaction_cost_bps,
                )
                costed_by_group_horizon[(sleeve, group, horizon)] = costed
                verdict = scope_verdict(
                    costed,
                    contract=contract,
                    horizon=horizon,
                    minimum_cross_section=GROUP_MINIMUM_CROSS_SECTIONS[group],
                    efficacy_field="top_minus_cohort_net",
                    hit_field="top_minus_cohort_net",
                )
                group_verdicts.append(
                    {
                        "scope_kind": "ranked_group",
                        "scope_id": group,
                        "sleeve_id": sleeve,
                        "applicability": "predictive_gate",
                        "decision_window_policy": TARGET_CONTRACT[
                            "decision_window_policy"
                        ],
                        "post_checkpoint_monitor_period_count": max(
                            0, len(all_periods) - required_count
                        ),
                        **verdict,
                    }
                )
    sleeve_verdicts: list[dict[str, Any]] = []
    for sleeve, groups in EXPECTED_SLEEVES.items():
        ranked_groups = sorted(group for group in groups if GROUP_MODES[group] == "ranked")
        sleeve_aggregates: list[dict[str, Any]] = []
        for horizon in contract.horizons:
            ranked_weights = {group: GROUP_WEIGHTS[sleeve][group] for group in ranked_groups}
            weighted = weighted_verdict_periods(
                {
                    group: costed_by_group_horizon[(sleeve, group, horizon)]
                    for group in ranked_groups
                },
                group_weights=ranked_weights,
            )
            verdict = scope_verdict(
                weighted,
                contract=contract,
                horizon=horizon,
                minimum_cross_section=sum(
                    GROUP_MINIMUM_CROSS_SECTIONS[group]
                    for group in ranked_groups
                ),
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
                    row["capture_id"]: row for row in parcel_by_horizon[horizon]
                }
                for row in weighted:
                    parcel = parcel_index.get(row["capture_id"])
                    if parcel is None:
                        raise ValueError("deployed surface monitor lacks parcel return")
                    deployed_surface_monitor.append(
                        {
                            "capture_id": row["capture_id"],
                            "horizon_sessions": horizon,
                            "ranked_predictive_weight": 0.90,
                            "parcel_operational_weight": 0.10,
                            "total_weight": 1.0,
                            "deployed_gross_return": 0.90 * row["top_gross"]
                            + 0.10 * parcel["gross_return"],
                            "deployed_net_return": 0.90 * row["top_net"]
                            + 0.10 * parcel["net_return"],
                            "predictive_gate_uses_parcel": False,
                            "deployed_weight_tie_out_pass": True,
                        }
                    )
        group_rows = [row for row in group_verdicts if row["sleeve_id"] == sleeve]
        expected_group_horizons = {
            (group, horizon)
            for group in ranked_groups
            for horizon in contract.horizons
        }
        observed_group_horizons = {
            (row["scope_id"], row["horizon_sessions"]) for row in group_rows
        }
        group_pass = observed_group_horizons == expected_group_horizons and all(
            row["pass"] for row in group_rows
        )
        aggregate_pass = len(sleeve_aggregates) == len(contract.horizons) and all(
            row["pass"] for row in sleeve_aggregates
        )
        coverage_pass = all(
            bool(capture.get("sleeve_coverage_gates", {}).get(sleeve, False))
            for capture in evidence["captures"]
        )
        pass_flag = group_pass and aggregate_pass and coverage_pass
        sleeve_verdicts.append(
            {
                "sleeve_id": sleeve,
                "ranked_group_census_complete": (
                    observed_group_horizons == expected_group_horizons
                ),
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
                    "eligible_for_independent_review"
                    if pass_flag
                    else "remain_shadow_zero_cap"
                ),
                "production_activation_authorized": False,
                "portfolio_write_enabled": False,
                "optimizer_cap": 0.0,
            }
        )
    passing = [row["sleeve_id"] for row in sleeve_verdicts if row["pass"]]
    body: dict[str, Any] = {
        "schema_version": EVALUATION_SCHEMA,
        "state": "evaluated_future_only",
        "evidence_class": "prospective_future_only",
        "family": "transportation",
        "policy_id": contract.policy_id,
        "evaluated_at_utc": exact_utc(
            evaluated_at_utc,
            label="Transportation evaluated_at_utc",
        ).isoformat(),
        "target_contract": TARGET_CONTRACT,
        "domain_contract_sha256": plan_audit["domain_contract_sha256"],
        "canonical_trust_audit": bundle.audit(),
        "capture_timestamp_audits": timestamp_audits,
        "capture_timestamp_chain_audit": chain,
        "capture_registry_audit": evidence["capture_registry_audit"],
        "due_capture_census_audit": evidence["due_capture_census_audit"],
        "outcome_integrity_audit": evidence["outcome_integrity_audit"],
        "canonical_attestations": attestations,
        "market_source_provenance": market_provenance,
        "group_scope_verdicts": group_verdicts,
        "fixed_weight_aggregate_verdicts": aggregate_verdicts,
        "not_applicable_group_audit": parcel_audit,
        "deployed_surface_weight_monitor": deployed_surface_monitor,
        "sleeve_independent_verdicts": sleeve_verdicts,
        "passing_sleeves": passing,
        "blocked_sleeves": [
            row["sleeve_id"] for row in sleeve_verdicts if not row["pass"]
        ],
        "any_sleeve_pass": bool(passing),
        "sector_wide_all_sleeves_pass": len(passing) == len(EXPECTED_SLEEVES),
        "sector_wide_action": (
            "eligible_for_separate_independent_review"
            if len(passing) == len(EXPECTED_SLEEVES)
            else "remain_shadow_zero_cap"
        ),
        "historical_results_can_authorize_production": False,
        "production_activation_authorized": False,
        "portfolio_write_enabled": False,
        "optimizer_cap": 0.0,
        "next_required_action": "separate_independent_review_receipt_per_passing_sleeve",
    }
    body["payload_sha256"] = canonical_sha256(body)
    return body


__all__ = ["EVALUATION_SCHEMA", "EXPECTED_SLEEVES", "PARCEL_GROUP", "evaluate_v6"]
