"""Canonical Consumer prospective evaluation with independent cohort verdicts."""

from __future__ import annotations

import hashlib
import math
import statistics
from pathlib import Path
from typing import Any, Mapping, Sequence

from future_only_evidence.canonical_domain import (
    require_receipt_contract_binding,
    revalidate_capture_timestamp,
    validate_canonical_outcome_attestations,
    validate_capture_timestamp_chain,
    validate_market_source_provenance,
)
from future_only_evidence.canonical_values import exact_utc
from future_only_evidence.lifecycle_snapshot import (
    validate_lifecycle_capture_chronology,
)
from future_only_evidence.protocol import canonical_sha256
from future_only_evidence.prospective_contracts import normalize_signal_rows
from future_only_evidence.score_input_availability import (
    validate_score_input_availability_capture_chronology,
)
from future_only_evidence.prospective_evaluator import (
    apply_costs,
    build_ranked_periods,
    deterministic_nonoverlap,
    load_verified_evidence,
    scope_verdict,
)

from .future_oos_capture_v4 import derive_rank_signals
from .future_oos_capture_v5 import (
    REQUIRED_CAPTURE_ROLES_V5,
    reconcile_exact_registered_census,
    validate_capture_sources_v5,
)
from .future_oos_plan_v5 import (
    ACCEPTANCE_THRESHOLDS,
    COHORT_MINIMUMS,
    POLICY_ID,
    TARGET_CONTRACT,
    validate_registered_plan_v5,
)
from .future_oos_score_lineage_v2 import validate_and_replay_consumer_scores


EVALUATION_SCHEMA = "consumer_defensive_future_oos_evaluation_v5"


def _mean(values: Sequence[float]) -> float | None:
    return statistics.fmean(values) if values else None


def evaluate_v5(
    *,
    plan_path: Path,
    plan_source_paths: Mapping[str, Path],
    registration_receipt_path: Path,
    expected_registration_receipt_sha256: str,
    registration_timestamp_receipt_path: Path,
    expected_registration_timestamp_receipt_sha256: str,
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
    plan, contract, bundle, plan_audit = validate_registered_plan_v5(
        plan_path,
        source_paths=plan_source_paths,
        registration_receipt_path=registration_receipt_path,
        expected_registration_receipt_sha256=expected_registration_receipt_sha256,
        registration_timestamp_receipt_path=registration_timestamp_receipt_path,
        expected_registration_timestamp_receipt_sha256=(
            expected_registration_timestamp_receipt_sha256
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
        raise ValueError("Consumer evaluator calendar differs from registered calendar")
    source_snapshots_by_id = evidence["_capture_source_snapshot_bytes_by_id"]
    receipt_snapshots_by_id = evidence["_capture_receipt_snapshot_bytes_by_id"]
    timestamp_audits: list[dict[str, Any]] = []
    for capture in evidence["captures"]:
        capture_id = str(capture["capture_id"])
        source_snapshots = source_snapshots_by_id[capture_id]
        capture_receipt_bytes = receipt_snapshots_by_id[capture_id]
        if (
            capture.get("domain_schema_version")
            != "consumer_defensive_future_only_signal_capture_v5"
            or capture.get("domain_contract_sha256")
            != plan_audit["domain_contract_sha256"]
            or capture.get("registered_plan_sha256")
            != plan_audit["registered_plan_sha256"]
        ):
            raise ValueError("Consumer capture is outside the canonical v5 domain contract")
        source_identities = capture.get("source_identities")
        archive_audit = capture.get("content_addressed_archive_audit")
        if (
            not isinstance(source_identities, dict)
            or set(source_identities) != REQUIRED_CAPTURE_ROLES_V5
            or set(source_snapshots) != REQUIRED_CAPTURE_ROLES_V5
            or not isinstance(archive_audit, dict)
            or set(archive_audit) != REQUIRED_CAPTURE_ROLES_V5
        ):
            raise ValueError("Consumer capture lacks the exact archived source-role census")
        source_paths = {
            role: Path(str(identity["path"]))
            for role, identity in source_identities.items()
        }
        for role, identity in source_identities.items():
            archived = archive_audit[role]
            if (
                archived.get("archive_path") != identity.get("path")
                or archived.get("sha256") != identity.get("sha256")
                or hashlib.sha256(source_snapshots[role]).hexdigest()
                != identity.get("sha256")
            ):
                raise ValueError(f"Consumer archived source audit changed: {role}")
        for role, expected_hash in plan_audit["domain_contract"][
            "registered_source_sha256"
        ].items():
            if (
                role not in source_identities
                or source_identities[role].get("sha256") != expected_hash
            ):
                raise ValueError(f"Consumer capture changed registered plan source: {role}")
        require_receipt_contract_binding(
            Path(str(capture["trusted_receipt"]["path"])),
            expected_domain_contract_sha256=plan_audit["domain_contract_sha256"],
            receipt_snapshot_bytes=capture_receipt_bytes,
        )
        derived = derive_rank_signals(
            source_paths["rank_snapshot"],
            asof_date=str(capture["asof_date"]),
            rank_snapshot_bytes=source_snapshots["rank_snapshot"],
        )
        score_replay = validate_and_replay_consumer_scores(
            asof_date=str(capture["asof_date"]),
            signal_cutoff_at_utc=str(
                capture["trusted_capture_timing"][
                    "signal_information_cutoff_at_utc"
                ]
            ),
            rank_snapshot_path=source_paths["rank_snapshot"],
            feature_snapshot_path=source_paths["atomic_feature_snapshot"],
            frozen_baseline_spec_path=source_paths["frozen_baseline_spec"],
            score_input_availability_snapshot_path=source_paths[
                "score_input_availability_snapshot"
            ],
            score_input_availability_attestation_path=source_paths[
                "score_input_availability_attestation"
            ],
            expected_score_input_availability_attestation_sha256=str(
                source_identities["score_input_availability_attestation"][
                    "sha256"
                ]
            ),
            canonical_trust_bundle=bundle,
            policy_id=POLICY_ID,
            expected_cohorts=sorted(COHORT_MINIMUMS),
            rank_snapshot_bytes=source_snapshots["rank_snapshot"],
            feature_snapshot_bytes=source_snapshots["atomic_feature_snapshot"],
            frozen_baseline_spec_bytes=source_snapshots["frozen_baseline_spec"],
            score_input_availability_snapshot_bytes=source_snapshots[
                "score_input_availability_snapshot"
            ],
            score_input_availability_attestation_bytes=source_snapshots[
                "score_input_availability_attestation"
            ],
        )
        frozen_contract = plan_audit["domain_contract"][
            "frozen_score_replay_contract"
        ]
        if (
            score_replay.get("frozen_model_identity_sha256")
            != frozen_contract.get("model_identity_sha256")
            or score_replay.get("no_reestimation_from_outcomes_pass") is not True
            or score_replay != capture.get("frozen_score_replay_audit")
        ):
            raise ValueError("Consumer frozen score replay audit is not reproducible")
        source_semantics = validate_capture_sources_v5(
            asof_date=str(capture["asof_date"]),
            source_paths=source_paths,
            source_snapshot_bytes=source_snapshots,
        )
        if source_semantics != capture.get("source_semantics_audit"):
            raise ValueError("Consumer source freshness/manifest audit is not reproducible")
        resolved, census_audit = reconcile_exact_registered_census(
            asof_date=str(capture["asof_date"]),
            signals=derived,
            membership_path=source_paths["membership_snapshot"],
            plan_audit=plan_audit,
            score_replay_audit=score_replay,
            lifecycle_snapshot_path=source_paths["lifecycle_event_snapshot"],
            lifecycle_attestation_path=source_paths["lifecycle_source_attestation"],
            expected_lifecycle_attestation_sha256=str(
                source_identities["lifecycle_source_attestation"]["sha256"]
            ),
            trust_bundle=bundle,
            signal_cutoff_at_utc=str(
                capture["trusted_capture_timing"][
                    "signal_information_cutoff_at_utc"
                ]
            ),
            membership_snapshot_bytes=source_snapshots["membership_snapshot"],
            lifecycle_snapshot_bytes=source_snapshots["lifecycle_event_snapshot"],
            lifecycle_attestation_bytes=source_snapshots[
                "lifecycle_source_attestation"
            ],
        )
        validate_lifecycle_capture_chronology(
            census_audit["independent_lifecycle_source_audit"],
            trusted_capture_timing=capture["trusted_capture_timing"],
            captured_at_utc=capture["captured_at_utc"],
            label="Consumer evaluator",
        )
        validate_score_input_availability_capture_chronology(
            score_replay["score_input_availability_audit"],
            trusted_capture_timing=capture["trusted_capture_timing"],
            captured_at_utc=capture["captured_at_utc"],
            label="Consumer evaluator",
        )
        if (
            normalize_signal_rows(
                resolved, asof_date=str(capture["asof_date"])
            )
            != capture["signal_rows"]
        ):
            raise ValueError(
                "Consumer capture differs from deterministic rank/lifecycle replay"
            )
        timestamp_audits.append(
            revalidate_capture_timestamp(
                capture,
                bundle=bundle,
                capture_receipt_snapshot_bytes=capture_receipt_bytes,
            )
        )
        if census_audit != capture.get("registered_census_audit"):
            raise ValueError("Consumer registered census audit is not reproducible")
        if any(
            hashlib.sha256(source_snapshots[role]).hexdigest()
            != identity["sha256"]
            for role, identity in source_identities.items()
        ):
            raise ValueError("Consumer archived source changed during semantic validation")
    chain = validate_capture_timestamp_chain(
        evidence["captures"],
        initial_anchor_audit=plan_audit["registration_external_timestamp"],
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
        expected_benchmark_ticker="XLP",
        outcome_source_snapshot_bytes=evidence["_outcome_source_snapshot_bytes"],
    )
    cohort_results: dict[str, Any] = {}
    for cohort, minimum_cross_section in COHORT_MINIMUMS.items():
        horizons: dict[str, Any] = {}
        for horizon in contract.horizons:
            all_periods = build_ranked_periods(
                evidence,
                sleeve_id=cohort,
                group_id=cohort,
                horizon=horizon,
            )
            nonoverlap = deterministic_nonoverlap(all_periods)
            decision_count = contract.minimum_counts[horizon]
            decision_periods = nonoverlap[:decision_count]
            costed = apply_costs(
                decision_periods,
                transaction_cost_bps=contract.transaction_cost_bps,
            )
            verdict = scope_verdict(
                costed,
                contract=contract,
                horizon=horizon,
                minimum_cross_section=minimum_cross_section,
                efficacy_field="top_net",
                hit_field="top_net",
            )
            mean_top_minus_cohort = verdict["mean_top_minus_cohort_net"]
            verdict["gates"]["top_minus_cohort_net_pass"] = (
                mean_top_minus_cohort is not None
                and mean_top_minus_cohort
                > ACCEPTANCE_THRESHOLDS["minimum_top_minus_cohort_net"]
            )
            verdict["pass"] = all(verdict["gates"].values())
            verdict["decision_window_policy"] = TARGET_CONTRACT[
                "decision_window_policy"
            ]
            verdict["decision_period_count"] = len(decision_periods)
            verdict["post_checkpoint_monitor_period_count"] = max(
                0, len(nonoverlap) - decision_count
            )
            horizons[str(horizon)] = verdict
        weighted_ic = sum(
            TARGET_CONTRACT["horizon_weights"][str(horizon)]
            * float(horizons[str(horizon)]["mean_ic"])
            for horizon in contract.horizons
            if horizons[str(horizon)]["mean_ic"] is not None
        )
        all_ic_present = all(
            horizons[str(horizon)]["mean_ic"] is not None
            for horizon in contract.horizons
        )
        weighted_gate = (
            all_ic_present
            and math.isfinite(weighted_ic)
            and weighted_ic
            > ACCEPTANCE_THRESHOLDS["minimum_weighted_mean_rank_ic"]
        )
        cohort_pass = weighted_gate and all(
            horizons[str(horizon)]["pass"] for horizon in contract.horizons
        )
        cohort_results[cohort] = {
            "status": "pass" if cohort_pass else "insufficient_or_failed_evidence",
            "action": (
                "eligible_for_independent_review"
                if cohort_pass
                else "remain_shadow_zero_cap"
            ),
            "weighted_mean_rank_ic": weighted_ic if all_ic_present else None,
            "weighted_mean_rank_ic_pass": weighted_gate,
            "horizon_weights": TARGET_CONTRACT["horizon_weights"],
            "horizons": horizons,
            "pass": cohort_pass,
            "production_activation_authorized": False,
            "portfolio_write_enabled": False,
            "optimizer_cap": 0.0,
        }
    expected_cohorts_present = set(cohort_results) == set(COHORT_MINIMUMS)
    all_cohorts_pass = expected_cohorts_present and all(
        item["pass"] for item in cohort_results.values()
    )
    body: dict[str, Any] = {
        "schema_version": EVALUATION_SCHEMA,
        "family": "consumer_defensive",
        "policy_id": contract.policy_id,
        "evaluated_at_utc": exact_utc(
            evaluated_at_utc,
            label="Consumer evaluated_at_utc",
        ).isoformat(),
        "target_contract": TARGET_CONTRACT,
        "domain_contract_sha256": plan_audit["domain_contract_sha256"],
        "canonical_trust_audit": bundle.audit(),
        "cohort_independent_verdicts": cohort_results,
        "expected_cohort_census_pass": expected_cohorts_present,
        "sector_wide_all_cohorts_pass": all_cohorts_pass,
        "sector_wide_action": (
            "eligible_for_separate_independent_review"
            if all_cohorts_pass
            else "remain_shadow_zero_cap"
        ),
        "capture_timestamp_audits": timestamp_audits,
        "capture_timestamp_chain_audit": chain,
        "capture_registry_audit": evidence["capture_registry_audit"],
        "due_capture_census_audit": evidence["due_capture_census_audit"],
        "outcome_integrity_audit": evidence["outcome_integrity_audit"],
        "canonical_attestations": attestations,
        "market_source_provenance": market_provenance,
        "historical_results_can_authorize_production": False,
        "production_activation_authorized": False,
        "portfolio_write_enabled": False,
        "optimizer_cap": 0.0,
        "next_required_action": "separate_independent_review_receipt_per_passing_cohort",
    }
    body["payload_sha256"] = canonical_sha256(body)
    return body


__all__ = ["EVALUATION_SCHEMA", "evaluate_v5"]
