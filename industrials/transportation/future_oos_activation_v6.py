"""Prospective Transportation activation plan after the missed 2026-08-24 slot."""

from __future__ import annotations

import json
import hashlib
import math
from datetime import date
from pathlib import Path
from typing import Any, Mapping

import yaml

from future_only_evidence.canonical_archive import require_content_addressed_archive
from future_only_evidence.canonical_domain import (
    domain_contract_sha256,
    first_proven_month_end_after,
    require_receipt_contract_binding,
)
from future_only_evidence.canonical_trust import (
    CanonicalTrustBundle,
    load_canonical_trust_bundle,
    validate_external_timestamp,
)
from future_only_evidence.canonical_values import exact_utc
from future_only_evidence.official_calendar import validate_official_xnys_calendar_bytes
from future_only_evidence.lifecycle_snapshot import lifecycle_event_contract
from future_only_evidence.protocol import canonical_sha256, exact_sha256
from future_only_evidence.prospective_contracts import ProspectiveContract, read_json_snapshot
from future_only_evidence.transport_score_input_availability import (
    transport_score_input_availability_contract,
)


ACTIVATION_PLAN_SCHEMA = "transportation_future_oos_activation_plan_v6"
ACTIVATION_RECEIPT_SCHEMA = "transportation_future_activation_receipt_v1"
POLICY_ID = "transportation_v7_future_gate_prospective_restart_v1"
LIFECYCLE_EVENT_SCHEMA_V6 = "transportation_future_lifecycle_event_snapshot_v1"
MISSED_ORIGINAL_FIRST_SIGNAL = "2026-08-24"
REQUIRED_PLAN_ROLES = frozenset(
    {
        "v8_policy",
        "v7_research_decision",
        "universe_contract",
        "terminal_event_policy",
        "trading_calendar",
        "score_replay_baseline",
        "score_input_availability_baseline_snapshot",
        "score_input_availability_baseline_attestation",
    }
)
GROUP_WEIGHTS = {
    "north_american_surface_freight_and_logistics_v5": {
        "rail_networks": 0.25,
        "ltl_carriers": 0.25,
        "truckload_intermodal": 0.25,
        "asset_light_logistics": 0.15,
        "integrated_parcel": 0.10,
    },
    "oil_tanker_operators_v5": {"oil_tankers": 1.0},
}
GROUP_MODES = {
    "rail_networks": "ranked",
    "ltl_carriers": "ranked",
    "truckload_intermodal": "ranked",
    "asset_light_logistics": "ranked",
    "integrated_parcel": "eligibility_equal_weight",
    "oil_tankers": "ranked",
}
GROUP_TICKERS = {
    "rail_networks": ["CNI", "CP", "CSX", "NSC", "UNP"],
    "ltl_carriers": ["ARCB", "ODFL", "SAIA", "TFII", "XPO"],
    "truckload_intermodal": [
        "HUBG",
        "JBHT",
        "KNX",
        "SNDR",
        "CVLG",
        "HTLD",
        "MRTN",
        "WERN",
    ],
    "asset_light_logistics": ["CHRW", "EXPD", "LSTR", "FWRD"],
    "integrated_parcel": ["FDX", "UPS"],
    "oil_tankers": [
        "DHT",
        "ECO",
        "FRO",
        "NAT",
        "ASC",
        "HAFN",
        "STNG",
        "TRMD",
        "INSW",
        "TNK",
        "TEN",
    ],
}
GROUP_MINIMUM_CROSS_SECTIONS = {
    "rail_networks": 4,
    "ltl_carriers": 4,
    "truckload_intermodal": 6,
    "asset_light_logistics": 4,
    "integrated_parcel": 2,
    "oil_tankers": 8,
}
SCORE_REPLAY_CONTRACT = {
    "schema_version": "transportation_future_v8_score_replay_audit_v1",
    "score_formula_id": "transportation_v8_pit_subgroup_score_replay_v1",
    "scoring_panel_schema": "transportation_future_v8_scoring_panel_v1",
    "accepted_facts_schema": "transportation_future_v8_accepted_facts_v1",
    "score_replay_baseline_schema": (
        "transportation_future_v8_score_replay_baseline_v1"
    ),
    "governed_source_horizon_sessions": 63,
    "source_roles": [
        "accepted_facts",
        "canonical_v8_score",
        "score_input_availability_attestation",
        "score_input_availability_baseline_attestation",
        "score_input_availability_baseline_snapshot",
        "score_input_availability_snapshot",
        "score_replay_baseline",
        "scoring_panel",
        "v8_policy",
    ],
    "no_reestimation_policy": "frozen_v8_no_outcome_reestimation_v1",
}
TARGET_CONTRACT = {
    "benchmark_ticker": "IYT",
    "target_field": "forward_iyt_excess_return_at_fixed_session_horizon",
    "residual_formula": "arithmetic_stock_total_return_minus_benchmark_total_return_v1",
    "horizons_sessions": [21, 63],
    "entry_policy": "next_official_xnys_session_open",
    "exit_policy": "official_xnys_open_after_exact_21_63_sessions",
    "cadence": "monthly_true_month_end_after_reviewed_restart",
    "decision_window_policy": "first_n_nonoverlapping_once_v1",
    "surface_aggregate_recipe": "fixed_v8_weights_90pct_ranked_plus_10pct_parcel_monitor",
    "cost_convention": "20bps_per_one_way_turnover_initial_entry_rebalance_terminal_liquidation_v1",
    "pre_entry_nonexecution_policy_id": "governed_pre_entry_nonexecution_cash_carry_with_intended_turnover_cost_v1",
    "pre_entry_nonexecution_return_policy": "captured_name_retained_stock_return_zero_residual_minus_benchmark_no_reselection_v1",
    "pre_entry_nonexecution_cost_policy": "normal_intended_one_way_turnover_cost_charged_despite_nonexecution_v1",
    "score_replay_contract": SCORE_REPLAY_CONTRACT,
}
ACCEPTANCE_THRESHOLDS = {
    "minimum_nonoverlapping_outcomes": {"21": 12, "63": 4},
    "minimum_mean_rank_ic": 0.0,
    "minimum_top_minus_cohort_net": 0.0,
    "minimum_top_minus_bottom_gross": 0.0,
    "minimum_sign_hit_rate": 0.55,
    "maximum_ic_sign_pvalue": 0.10,
    "transaction_cost_bps": 20.0,
    "require_every_ranked_group_pass": True,
    "tanker_specialized_coverage_required": True,
}


def _json(path: Path, *, label: str) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    return payload


def _exact_date(value: Any, *, label: str) -> date:
    if type(value) is not str:
        raise ValueError(f"{label} must be an exact YYYY-MM-DD string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be exact YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{label} must be exact YYYY-MM-DD")
    return parsed


def validate_frozen_v8_policy(
    path: Path,
    *,
    policy_snapshot_bytes: bytes | None = None,
) -> dict[str, Any]:
    policy_bytes = (
        bytes(policy_snapshot_bytes)
        if policy_snapshot_bytes is not None
        else Path(path).expanduser().resolve().read_bytes()
    )
    try:
        policy = yaml.safe_load(policy_bytes.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ValueError("Transportation v8 policy must be valid UTF-8 YAML") from exc
    if not isinstance(policy, dict):
        raise ValueError("Transportation v8 policy must be a mapping")
    observed_weights: dict[str, dict[str, float]] = {}
    observed_modes: dict[str, str] = {}
    observed_tickers: dict[str, list[str]] = {}
    for cohort in dict(policy.get("cohorts") or {}).values():
        sleeve = str(cohort.get("calibration_cohort") or "")
        raw_weights = dict(cohort.get("aggregate_group_weights") or {})
        if any(
            type(weight) not in {int, float} or not math.isfinite(float(weight))
            for weight in raw_weights.values()
        ):
            raise ValueError("Transportation v8 group weights must be finite numeric values")
        observed_weights[sleeve] = {
            str(group): float(weight) for group, weight in raw_weights.items()
        }
        for group, definition in dict(cohort.get("groups") or {}).items():
            group_id = str(group)
            observed_modes[group_id] = str(definition.get("ranking_mode") or "")
            raw_tickers = definition.get("tickers")
            if (
                not isinstance(raw_tickers, list)
                or any(
                    type(ticker) is not str
                    or not ticker
                    or ticker.strip() != ticker
                    or ticker.upper() != ticker
                    for ticker in raw_tickers
                )
            ):
                raise ValueError(
                    "Transportation v8 tickers must be canonical uppercase strings"
                )
            observed_tickers[group_id] = list(raw_tickers)
    if (
        observed_weights != GROUP_WEIGHTS
        or observed_modes != GROUP_MODES
        or observed_tickers != GROUP_TICKERS
    ):
        raise ValueError("Transportation v8 policy differs from code-frozen groups/weights/tickers")
    all_tickers = [ticker for values in GROUP_TICKERS.values() for ticker in values]
    if len(all_tickers) != len(set(all_tickers)):
        raise ValueError("Transportation frozen ticker appears in more than one group")
    return {
        "group_weights": GROUP_WEIGHTS,
        "group_modes": GROUP_MODES,
        "group_tickers": GROUP_TICKERS,
        "group_minimum_cross_sections": GROUP_MINIMUM_CROSS_SECTIONS,
        "ticker_census_sha256": canonical_sha256(sorted(all_tickers)),
        "source_snapshot": {
            "path": str(Path(path).expanduser().resolve()),
            "sha256": hashlib.sha256(policy_bytes).hexdigest(),
            "bytes": len(policy_bytes),
        },
        "exact_v8_policy_pass": True,
    }


def _contract(plan: Mapping[str, Any]) -> ProspectiveContract:
    return ProspectiveContract(
        family="transportation",
        policy_id=POLICY_ID,
        effective_from=_exact_date(plan["effective_from"], label="effective_from"),
        first_signal_date=_exact_date(
            plan["first_signal_date"], label="first_signal_date"
        ),
        horizons=(21, 63),
        minimum_counts={21: 12, 63: 4},
        benchmark_ticker="IYT",
        cadence_id="monthly_true_month_end_v1",
        minimum_ic=0.0,
        minimum_efficacy=0.0,
        minimum_top_minus_bottom=0.0,
        minimum_hit_rate=0.55,
        transaction_cost_bps=20.0,
        top_minus_bottom_basis="gross",
        maximum_ic_sign_pvalue=0.10,
    )


def validate_activation_plan_v6(
    activation_plan_path: Path,
    *,
    source_paths: Mapping[str, Path],
    activation_receipt_path: Path,
    expected_activation_receipt_sha256: str,
    activation_timestamp_receipt_path: Path,
    expected_activation_timestamp_receipt_sha256: str,
    evidence_public_key_path: Path,
    timestamp_public_key_path: Path,
    market_data_public_key_path: Path,
) -> tuple[
    dict[str, Any],
    ProspectiveContract,
    CanonicalTrustBundle,
    dict[str, Any],
]:
    if set(source_paths) != REQUIRED_PLAN_ROLES:
        raise ValueError("Transportation activation source role census changed")
    bundle = load_canonical_trust_bundle(
        "transportation",
        evidence_public_key_path=evidence_public_key_path,
        timestamp_public_key_path=timestamp_public_key_path,
        market_data_public_key_path=market_data_public_key_path,
    )
    plan, plan_sha, _, _ = read_json_snapshot(
        activation_plan_path,
        label="Transportation activation plan",
    )
    exact_claims = {
        "schema_version": ACTIVATION_PLAN_SCHEMA,
        "status": "reviewed_prospective_restart_pending_first_signal",
        "evidence_class": "prospective_future_only",
        "policy_id": POLICY_ID,
        "missed_original_first_signal": MISSED_ORIGINAL_FIRST_SIGNAL,
        "missed_signal_disposition": "ineligible_no_backfill_superseded_by_restart",
        "target_contract": TARGET_CONTRACT,
        "acceptance_thresholds": ACCEPTANCE_THRESHOLDS,
        "group_weights": GROUP_WEIGHTS,
        "group_modes": GROUP_MODES,
        "group_tickers": GROUP_TICKERS,
        "group_minimum_cross_sections": GROUP_MINIMUM_CROSS_SECTIONS,
        "historical_results_can_authorize_production": False,
        "production_activation_authorized": False,
        "portfolio_write_enabled": False,
        "optimizer_cap": 0.0,
    }
    for field, expected in exact_claims.items():
        if plan.get(field) != expected:
            raise ValueError(f"Transportation activation changed field: {field}")
    optimizer_cap = plan.get("optimizer_cap")
    if (
        type(optimizer_cap) not in {int, float}
        or not math.isfinite(float(optimizer_cap))
        or float(optimizer_cap) != 0.0
    ):
        raise ValueError("Transportation activation optimizer cap must be explicit numeric zero")
    source_snapshots = {
        role: Path(path).expanduser().resolve().read_bytes()
        for role, path in sorted(source_paths.items())
    }
    source_hashes = {
        role: hashlib.sha256(payload).hexdigest()
        for role, payload in source_snapshots.items()
    }
    source_archive_audit = {
        role: require_content_addressed_archive(path, expected_sha256=source_hashes[role])
        for role, path in sorted(source_paths.items())
    }
    if plan.get("registered_source_sha256") != source_hashes:
        raise ValueError("Transportation activation does not bind exact source bytes")
    calendar_rows, calendar_audit = validate_official_xnys_calendar_bytes(
        source_snapshots["trading_calendar"]
    )
    policy_audit = validate_frozen_v8_policy(
        source_paths["v8_policy"],
        policy_snapshot_bytes=source_snapshots["v8_policy"],
    )
    # Local import avoids the score-lineage module's intentional dependency on
    # the code-frozen group constants above during module initialization.
    from .future_oos_score_lineage_v1 import (
        validate_transport_score_replay_baseline,
    )

    contract = _contract(plan)
    receipt_path = Path(activation_receipt_path).expanduser().resolve()
    receipt_bytes = receipt_path.read_bytes()
    receipt_hash = hashlib.sha256(receipt_bytes).hexdigest()
    if receipt_hash != exact_sha256(
        expected_activation_receipt_sha256, label="activation receipt sha256"
    ):
        raise ValueError("Transportation activation receipt hash mismatch")
    try:
        preliminary_receipt = json.loads(receipt_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(
            "Transportation activation receipt must be valid UTF-8 JSON"
        ) from exc
    if not isinstance(preliminary_receipt, dict):
        raise ValueError("Transportation activation receipt must be a JSON object")
    bundle.evidence_seal.verify_snapshot(
        receipt_bytes,
        receipt_hash,
        preliminary_receipt,
    )
    preliminary_expected_receipt = {
        "schema_version": ACTIVATION_RECEIPT_SCHEMA,
        "family": "transportation",
        "policy_id": POLICY_ID,
        "activation_plan_sha256": plan_sha,
        "registered_source_sha256": source_hashes,
        "contract_identity_sha256": canonical_sha256(contract.identity()),
        "missed_original_first_signal": MISSED_ORIGINAL_FIRST_SIGNAL,
    }
    for field, expected in preliminary_expected_receipt.items():
        if preliminary_receipt.get(field) != expected:
            raise ValueError(
                f"Transportation activation receipt changed field: {field}"
            )
    timestamp_audit = validate_external_timestamp(
        subject_path=receipt_path,
        timestamp_receipt_path=activation_timestamp_receipt_path,
        expected_timestamp_receipt_sha256=(
            expected_activation_timestamp_receipt_sha256
        ),
        expected_subject_sha256=receipt_hash,
        bundle=bundle,
        expected_previous_log_head_sha256=bundle.genesis_log_head_sha256,
        expected_previous_log_sequence=bundle.genesis_log_sequence,
        expected_family="transportation",
        expected_policy_id=POLICY_ID,
        expected_subject_role="activation_receipt",
        expected_slot_id=f"transportation:{POLICY_ID}:activation:v1",
        subject_snapshot_bytes=receipt_bytes,
    )
    anchored_at = exact_utc(
        timestamp_audit["observed_at_utc"],
        label="Transportation activation external observation time",
    )
    effective_from = _exact_date(
        plan["effective_from"], label="Transportation effective_from"
    )
    if effective_from != anchored_at.date():
        raise ValueError(
            "Transportation effective date differs from external activation date"
        )
    first_signal = first_proven_month_end_after(
        source_paths["trading_calendar"],
        after_utc=max(anchored_at, bundle.activated_at_utc),
        trading_calendar_snapshot_bytes=source_snapshots["trading_calendar"],
    )
    if first_signal <= MISSED_ORIGINAL_FIRST_SIGNAL:
        raise ValueError("Transportation prospective restart would backfill the missed slot")
    if plan.get("first_signal_date") != first_signal:
        raise ValueError(
            "Transportation first signal is not the first future proven month-end"
        )
    score_replay_baseline_audit = validate_transport_score_replay_baseline(
        baseline_path=source_paths["score_replay_baseline"],
        score_input_availability_baseline_snapshot_path=source_paths[
            "score_input_availability_baseline_snapshot"
        ],
        score_input_availability_baseline_attestation_path=source_paths[
            "score_input_availability_baseline_attestation"
        ],
        v8_policy_path=source_paths["v8_policy"],
        activation_registered_at_utc=anchored_at.isoformat(),
        policy_id=POLICY_ID,
        canonical_trust_bundle=bundle,
        expected_sha256={
            role: source_hashes[role]
            for role in (
                "score_replay_baseline",
                "score_input_availability_baseline_snapshot",
                "score_input_availability_baseline_attestation",
                "v8_policy",
            )
        },
        source_snapshot_bytes={
            role: source_snapshots[role]
            for role in (
                "score_replay_baseline",
                "score_input_availability_baseline_snapshot",
                "score_input_availability_baseline_attestation",
                "v8_policy",
            )
        },
    )
    baseline_cutoff = exact_utc(
        score_replay_baseline_audit["baseline_cutoff_at_utc"],
        label="Transportation replay baseline cutoff",
    )
    baseline_cutoff_at_utc = baseline_cutoff.isoformat()
    baseline_asof = baseline_cutoff.date().isoformat()
    calendar_by_asof = {
        str(row["session_date"]): exact_utc(
            row["exit_execution_at_utc"],
            label="Transportation official calendar close",
        ).isoformat()
        for row in calendar_rows
    }
    if calendar_by_asof.get(baseline_asof) != baseline_cutoff_at_utc:
        raise ValueError(
            "Transportation replay baseline cutoff is not the bound official close"
        )
    score_input_availability_baseline_audit = score_replay_baseline_audit[
        "score_input_availability_audit"
    ]
    domain = {
        "domain_schema_version": "transportation_future_domain_contract_v6",
        "policy_id": POLICY_ID,
        "target_contract": TARGET_CONTRACT,
        "acceptance_thresholds": ACCEPTANCE_THRESHOLDS,
        "group_weights": GROUP_WEIGHTS,
        "group_modes": GROUP_MODES,
        "group_tickers": GROUP_TICKERS,
        "score_replay_contract": SCORE_REPLAY_CONTRACT,
        "score_replay_baseline_audit": score_replay_baseline_audit,
        "score_input_availability_contract": (
            transport_score_input_availability_contract()
        ),
        "score_input_availability_baseline_audit": (
            score_input_availability_baseline_audit
        ),
        "lifecycle_event_snapshot_contract": lifecycle_event_contract(
            LIFECYCLE_EVENT_SCHEMA_V6
        ),
        "registered_source_sha256": source_hashes,
        "production_activation_authorized": False,
        "portfolio_write_enabled": False,
        "optimizer_cap": 0.0,
    }
    domain_hash = domain_contract_sha256(contract, domain)
    if plan.get("domain_contract_sha256") != domain_hash:
        raise ValueError("Transportation activation domain contract hash mismatch")
    plan_archive = require_content_addressed_archive(
        activation_plan_path, expected_sha256=plan_sha
    )
    receipt_archive = require_content_addressed_archive(
        receipt_path, expected_sha256=receipt_hash
    )
    timestamp_receipt_archive = require_content_addressed_archive(
        activation_timestamp_receipt_path,
        expected_sha256=expected_activation_timestamp_receipt_sha256,
    )
    receipt = require_receipt_contract_binding(
        receipt_path,
        expected_domain_contract_sha256=domain_hash,
        receipt_snapshot_bytes=receipt_bytes,
    )
    if receipt != preliminary_receipt:
        raise ValueError("Transportation activation receipt snapshot changed")
    expected_receipt = {
        "schema_version": ACTIVATION_RECEIPT_SCHEMA,
        "family": "transportation",
        "policy_id": POLICY_ID,
        "activation_plan_sha256": plan_sha,
        "registered_source_sha256": source_hashes,
        "contract_identity_sha256": canonical_sha256(contract.identity()),
        "missed_original_first_signal": MISSED_ORIGINAL_FIRST_SIGNAL,
    }
    for field, expected in expected_receipt.items():
        if receipt.get(field) != expected:
            raise ValueError(f"Transportation activation receipt changed field: {field}")
    return plan, contract, bundle, {
        "canonical_trust": bundle.audit(),
        "domain_contract": domain,
        "domain_contract_sha256": domain_hash,
        "activation_receipt_sha256": receipt_hash,
        "registered_plan_sha256": plan_sha,
        "activation_external_timestamp": timestamp_audit,
        "official_calendar": calendar_audit,
        "v8_policy": policy_audit,
        "registered_source_archive_audit": source_archive_audit,
        "activation_plan_archive_audit": plan_archive,
        "activation_receipt_archive_audit": receipt_archive,
        "activation_timestamp_receipt_archive_audit": timestamp_receipt_archive,
        "missed_2026_08_24_capture_counted": 0,
        "no_backfill_pass": True,
        "trusted_activation_pass": True,
    }


__all__ = [
    "ACCEPTANCE_THRESHOLDS",
    "ACTIVATION_PLAN_SCHEMA",
    "GROUP_MODES",
    "GROUP_MINIMUM_CROSS_SECTIONS",
    "GROUP_TICKERS",
    "GROUP_WEIGHTS",
    "LIFECYCLE_EVENT_SCHEMA_V6",
    "MISSED_ORIGINAL_FIRST_SIGNAL",
    "POLICY_ID",
    "REQUIRED_PLAN_ROLES",
    "SCORE_REPLAY_CONTRACT",
    "TARGET_CONTRACT",
    "validate_activation_plan_v6",
    "validate_frozen_v8_policy",
]
