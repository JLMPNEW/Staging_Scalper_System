"""Canonical fixed-horizon XLP-residual Consumer prospective evaluation."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from future_only_evidence.authority_config import DEFAULT_AUTHORITY_REGISTRY
from future_only_evidence.outcome_integrity import OUTCOME_SOURCE_ROLES
from future_only_evidence.protocol import canonical_sha256, file_sha256
from future_only_evidence.prospective_contracts import normalize_signal_rows
from future_only_evidence.prospective_evaluator import (
    apply_costs,
    build_ranked_periods,
    deterministic_nonoverlap,
    load_verified_evidence,
    scope_verdict,
)

from .future_oos_capture_v4 import derive_rank_signals, validate_capture_sources_v4
from .future_oos_plan_v4 import validate_registered_plan_v4


def evaluate(
    *,
    plan_path: Path,
    plan_source_paths: Mapping[str, Path],
    registration_receipt_path: Path,
    expected_registration_receipt_sha256: str,
    trusted_public_key_path: Path,
    authority_registry_path: Path = DEFAULT_AUTHORITY_REGISTRY,
    capture_paths: Sequence[Path],
    capture_registry_path: Path,
    capture_registry_receipt_path: Path,
    expected_capture_registry_receipt_sha256: str,
    outcome_path: Path,
    outcome_source_paths: Mapping[str, Path],
    outcome_receipt_path: Path,
    expected_outcome_receipt_sha256: str,
    trading_calendar_path: Path,
    evaluated_at_utc: str,
) -> dict[str, Any]:
    plan, contract, authority, plan_audit = validate_registered_plan_v4(
        plan_path,
        source_paths=plan_source_paths,
        registration_receipt_path=registration_receipt_path,
        expected_registration_receipt_sha256=expected_registration_receipt_sha256,
        trusted_public_key_path=trusted_public_key_path,
        authority_registry_path=authority_registry_path,
    )
    if set(outcome_source_paths) != OUTCOME_SOURCE_ROLES:
        raise ValueError("Consumer outcome source roles changed")
    if file_sha256(trading_calendar_path) != file_sha256(plan_source_paths["trading_calendar"]):
        raise ValueError("Consumer evaluation calendar differs from registered plan")
    evidence = load_verified_evidence(
        contract=contract,
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
    for capture in evidence["captures"]:
        if capture.get("domain_schema_version") != "consumer_defensive_future_only_signal_capture_v4":
            raise ValueError("noncanonical Consumer capture cannot satisfy future evidence")
        if capture.get("registered_plan_sha256") != file_sha256(plan_path):
            raise ValueError("Consumer capture references a different registered plan")
        source_paths = {
            role: Path(identity["path"])
            for role, identity in capture["source_identities"].items()
        }
        derived = normalize_signal_rows(
            derive_rank_signals(
                source_paths["rank_snapshot"],
                asof_date=str(capture["asof_date"]),
            ),
            asof_date=str(capture["asof_date"]),
        )
        if derived != capture["signal_rows"]:
            raise ValueError("Consumer capture signal rows differ from bound rank bytes")
        audit = validate_capture_sources_v4(
            asof_date=str(capture["asof_date"]),
            signals=derived,
            source_paths=source_paths,
        )
        if audit != capture.get("source_semantics_audit"):
            raise ValueError("Consumer capture source-semantics audit is not reproducible")
    minimum_cross = {
        str(key): int(value) for key, value in dict(plan["minimum_cross_sections"]).items()
    }
    expected_sleeves = sorted(minimum_cross)
    observed_sleeves = {
        row["sleeve_id"]
        for capture in evidence["captures"]
        for row in capture["signal_rows"]
    }
    scope_verdicts: list[dict[str, Any]] = []
    sleeve_verdicts: list[dict[str, Any]] = []
    for sleeve in expected_sleeves:
        horizon_rows: list[dict[str, Any]] = []
        if sleeve in observed_sleeves:
            for horizon in contract.horizons:
                periods = build_ranked_periods(
                    evidence,
                    sleeve_id=sleeve,
                    group_id=None,
                    horizon=horizon,
                )
                costed = apply_costs(
                    deterministic_nonoverlap(periods),
                    transaction_cost_bps=contract.transaction_cost_bps,
                )
                verdict = scope_verdict(
                    costed,
                    contract=contract,
                    horizon=horizon,
                    minimum_cross_section=minimum_cross[sleeve],
                    efficacy_field="top_net",
                    hit_field="top_net",
                )
                row = {
                    "scope_kind": "consumer_cohort",
                    "scope_id": sleeve,
                    "sleeve_id": sleeve,
                    "benchmark_gate_semantics": "mean_top_xlp_residual_net",
                    **verdict,
                }
                scope_verdicts.append(row)
                horizon_rows.append(row)
        horizons_complete = {row["horizon_sessions"] for row in horizon_rows} == set(
            contract.horizons
        )
        by_horizon = {int(row["horizon_sessions"]): row for row in horizon_rows}
        primary_objective = None
        if horizons_complete and all(
            by_horizon[horizon].get("mean_ic") is not None
            for horizon in contract.horizons
        ):
            primary_objective = (
                0.20 * float(by_horizon[21]["mean_ic"])
                + 0.50 * float(by_horizon[63]["mean_ic"])
                + 0.30 * float(by_horizon[126]["mean_ic"])
            )
        primary_objective_pass = primary_objective is not None and primary_objective > 0.0
        pass_flag = (
            horizons_complete
            and all(row["pass"] for row in horizon_rows)
            and primary_objective_pass
        )
        sleeve_verdicts.append(
            {
                "sleeve_id": sleeve,
                "observed_in_capture_registry": sleeve in observed_sleeves,
                "horizons_complete": horizons_complete,
                "primary_objective": "0.20*mean_ic_21+0.50*mean_ic_63+0.30*mean_ic_126",
                "primary_objective_value": primary_objective,
                "primary_objective_pass": primary_objective_pass,
                "cohort_pass": pass_flag,
                "pass": pass_flag,
                "action": (
                    "submit_this_cohort_for_independent_promotion_review"
                    if pass_flag
                    else "remain_shadow_fail_closed"
                ),
                "production_activation_authorized": False,
            }
        )
    extra_sleeves = sorted(observed_sleeves - set(expected_sleeves))
    if extra_sleeves:
        raise ValueError(f"Consumer capture contains unregistered cohorts={extra_sleeves}")
    passing = [row["sleeve_id"] for row in sleeve_verdicts if row["pass"]]
    body: dict[str, Any] = {
        "schema_version": "consumer_defensive_future_only_evaluation_v4",
        "state": "evaluated_future_only",
        "evidence_class": "prospective_future_only",
        "family": contract.family,
        "policy_id": contract.policy_id,
        "target_contract": {
            "target_field": "forward_xlp_residual_return",
            "benchmark_ticker": "XLP",
            "horizons": list(contract.horizons),
            "return_convention": contract.identity()["return_convention"],
        },
        "evaluated_at_utc": datetime.fromisoformat(
            str(evaluated_at_utc).replace("Z", "+00:00")
        ).astimezone(timezone.utc).isoformat(),
        "plan_integrity_audit": plan_audit,
        "capture_registry_audit": evidence["capture_registry_audit"],
        "due_capture_census_audit": evidence["due_capture_census_audit"],
        "outcome_integrity_audit": evidence["outcome_integrity_audit"],
        "exact_all_matured_outcome_census_pass": True,
        "scope_verdicts": scope_verdicts,
        "sleeve_verdicts": sleeve_verdicts,
        "passing_sleeves": passing,
        "blocked_sleeves": [row["sleeve_id"] for row in sleeve_verdicts if not row["pass"]],
        "any_sleeve_pass": bool(passing),
        "sector_wide_all_sleeves_pass": bool(sleeve_verdicts)
        and len(passing) == len(sleeve_verdicts),
        "action": (
            "submit_passing_cohorts_for_independent_review"
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


__all__ = ["evaluate"]
