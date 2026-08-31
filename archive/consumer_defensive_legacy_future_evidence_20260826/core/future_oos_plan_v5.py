"""Canonical Consumer prospective registration with out-of-band trust roots."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from future_only_evidence.canonical_domain import (
    domain_contract_sha256,
    first_proven_month_end_after,
    require_receipt_contract_binding,
)
from future_only_evidence.lifecycle_snapshot import lifecycle_event_contract
from future_only_evidence.score_input_availability import (
    score_input_availability_contract,
)
from future_only_evidence.canonical_archive import require_content_addressed_archive
from future_only_evidence.canonical_values import exact_utc
from future_only_evidence.canonical_trust import (
    CanonicalTrustBundle,
    load_canonical_trust_bundle,
    validate_external_timestamp,
)
from future_only_evidence.official_calendar import validate_official_xnys_calendar_bytes
from future_only_evidence.protocol import canonical_sha256, exact_sha256
from future_only_evidence.prospective_contracts import ProspectiveContract, read_json_snapshot

from .future_oos_score_lineage_v2 import validate_frozen_baseline_spec


PLAN_SCHEMA = "consumer_defensive_future_oos_registered_plan_v5"
REGISTRATION_RECEIPT_SCHEMA = "consumer_defensive_registration_receipt_v2"
POLICY_ID = "consumer_defensive_frozen_baseline_future_gate_v2"
LIFECYCLE_EVENT_SCHEMA_V5 = "consumer_defensive_future_lifecycle_event_snapshot_v1"
REQUIRED_PLAN_ROLES = frozenset(
    {
        "candidate_registry",
        "universe_contract",
        "frozen_baseline_spec",
        "source_registry",
        "terminal_event_policy",
        "trading_calendar",
    }
)
COHORT_MINIMUMS = {
    "beverages": 8,
    "consumer_staples_distribution_retail": 8,
    "household_personal_tobacco": 8,
    "packaged_foods_agricultural_products": 8,
}
TARGET_CONTRACT = {
    "benchmark_ticker": "XLP",
    "target_field": "forward_xlp_residual_return_at_fixed_session_horizon",
    "residual_formula": "arithmetic_stock_total_return_minus_benchmark_total_return_v1",
    "horizons_sessions": [21, 63, 126],
    "horizon_weights": {"21": 0.20, "63": 0.50, "126": 0.30},
    "primary_objective": "weighted_mean_rank_ic",
    "scoring_frequency": "monthly_true_month_end",
    "entry_policy": "next_official_xnys_session_open",
    "exit_policy": "official_xnys_open_after_exact_21_63_126_sessions",
    "decision_window_policy": "first_n_nonoverlapping_once_v1",
    "spread_gate_interpretation": "turnover_cost_net_research_spread_excludes_borrow_and_is_not_tradable_short_pnl_v1",
    "cost_convention": "20bps_per_one_way_turnover_initial_entry_rebalance_terminal_liquidation_v1",
    "pre_entry_nonexecution_policy_id": "governed_pre_entry_nonexecution_cash_carry_with_intended_turnover_cost_v1",
    "pre_entry_nonexecution_return_policy": "captured_name_retained_stock_return_zero_residual_minus_benchmark_no_reselection_v1",
    "pre_entry_nonexecution_cost_policy": "normal_intended_one_way_turnover_cost_charged_despite_nonexecution_v1",
}
ACCEPTANCE_THRESHOLDS = {
    "minimum_nonoverlapping_outcomes": {"21": 12, "63": 6, "126": 4},
    "minimum_mean_rank_ic": 0.0,
    "minimum_weighted_mean_rank_ic": 0.0,
    "minimum_top_xlp_residual_net": 0.0,
    "minimum_top_minus_cohort_net": 0.0,
    "minimum_top_minus_bottom_turnover_cost_net": 0.0,
    "minimum_sign_hit_rate": 0.55,
    "maximum_ic_sign_pvalue": 0.10,
    "transaction_cost_bps": 20.0,
}
CANDIDATE_SCHEMA = "consumer_defensive_prospective_candidate_registry_v1"
UNIVERSE_SCHEMA = "consumer_defensive_prospective_universe_contract_v1"


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


def _candidate_census(
    candidate_path: Path,
    universe_path: Path,
) -> dict[str, Any]:
    candidate, candidate_sha, _, _ = read_json_snapshot(
        candidate_path,
        label="Consumer candidate registry",
    )
    universe, universe_sha, _, _ = read_json_snapshot(
        universe_path,
        label="Consumer universe contract",
    )
    if candidate.get("schema_version") != CANDIDATE_SCHEMA:
        raise ValueError("Consumer candidate registry is not the prospective schema")
    if universe.get("schema_version") != UNIVERSE_SCHEMA:
        raise ValueError("Consumer universe contract is not the prospective schema")
    rows = candidate.get("rows")
    if not isinstance(rows, list) or candidate.get("rows_sha256") != canonical_sha256(rows):
        raise ValueError("Consumer candidate registry rows are absent or hash-inconsistent")
    seen: set[str] = set()
    cohorts: dict[str, list[str]] = {cohort: [] for cohort in COHORT_MINIMUMS}
    for row in rows:
        ticker = row.get("ticker")
        cohort = row.get("cohort_id")
        if (
            type(ticker) is not str
            or not ticker
            or ticker.strip() != ticker
            or ticker.upper() != ticker
            or ticker in seen
            or type(cohort) is not str
            or cohort not in cohorts
        ):
            raise ValueError("Consumer candidate ticker/cohort census is invalid")
        if type(row.get("candidate_flag")) is not int or row["candidate_flag"] != 1:
            raise ValueError("Consumer candidate registry may contain only frozen candidates")
        seen.add(ticker)
        cohorts[cohort].append(ticker)
    if any(len(cohorts[key]) < minimum for key, minimum in COHORT_MINIMUMS.items()):
        raise ValueError("Consumer registered cohort is below its frozen minimum census")
    expected_universe = {
        "cohort_minimum_cross_sections": COHORT_MINIMUMS,
        "candidate_registry_sha256": candidate_sha,
        "membership_policy": "exact_registered_candidates_minus_governed_terminal_events_v1",
        "cohort_assignment_policy": "one_ticker_one_frozen_cohort_v1",
    }
    for field, expected in expected_universe.items():
        if universe.get(field) != expected:
            raise ValueError(f"Consumer universe contract changed canonical field: {field}")
    return {
        "candidate_count": len(seen),
        "candidate_tickers": sorted(seen),
        "cohort_tickers": {key: sorted(value) for key, value in cohorts.items()},
        "candidate_rows_sha256": candidate["rows_sha256"],
        "source_snapshot_sha256": {
            "candidate_registry": candidate_sha,
            "universe_contract": universe_sha,
        },
        "candidate_census_pass": True,
    }


def _contract(plan: Mapping[str, Any]) -> ProspectiveContract:
    return ProspectiveContract(
        family="consumer_defensive",
        policy_id=POLICY_ID,
        effective_from=_exact_date(plan["effective_from"], label="effective_from"),
        first_signal_date=_exact_date(
            plan["first_signal_date"], label="first_signal_date"
        ),
        horizons=(21, 63, 126),
        minimum_counts={21: 12, 63: 6, 126: 4},
        benchmark_ticker="XLP",
        cadence_id="monthly_true_month_end_v1",
        minimum_ic=0.0,
        minimum_efficacy=0.0,
        minimum_top_minus_bottom=0.0,
        minimum_hit_rate=0.55,
        transaction_cost_bps=20.0,
        top_minus_bottom_basis="net",
        maximum_ic_sign_pvalue=0.10,
    )


def canonical_domain_contract(
    *,
    registered_source_sha256: Mapping[str, str],
    candidate_audit: Mapping[str, Any],
    frozen_baseline_audit: Mapping[str, Any],
) -> dict[str, Any]:
    frozen_contract = {
        key: value
        for key, value in frozen_baseline_audit.items()
        if key != "source_snapshot"
    }
    return {
        "domain_schema_version": "consumer_defensive_future_domain_contract_v5",
        "policy_id": POLICY_ID,
        "cohort_minimum_cross_sections": COHORT_MINIMUMS,
        "target_contract": TARGET_CONTRACT,
        "acceptance_thresholds": ACCEPTANCE_THRESHOLDS,
        "selection_fraction": 0.20,
        "selection_policy": "ceil_fraction_minimum_one_per_cohort_v1",
        "registered_source_sha256": dict(registered_source_sha256),
        "registered_candidate_tickers": list(candidate_audit["candidate_tickers"]),
        "registered_cohort_tickers": dict(candidate_audit["cohort_tickers"]),
        "frozen_score_replay_contract": frozen_contract,
        "lifecycle_event_snapshot_contract": lifecycle_event_contract(
            LIFECYCLE_EVENT_SCHEMA_V5
        ),
        "score_input_availability_contract": (
            score_input_availability_contract()
        ),
        "score_reestimation_policy": "prohibited_after_registration_v1",
        "production_activation_authorized": False,
        "portfolio_write_enabled": False,
        "optimizer_cap": 0.0,
    }


def validate_registered_plan_v5(
    plan_path: Path,
    *,
    source_paths: Mapping[str, Path],
    registration_receipt_path: Path,
    expected_registration_receipt_sha256: str,
    registration_timestamp_receipt_path: Path,
    expected_registration_timestamp_receipt_sha256: str,
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
        raise ValueError("Consumer plan source role census changed")
    bundle = load_canonical_trust_bundle(
        "consumer_defensive",
        evidence_public_key_path=evidence_public_key_path,
        timestamp_public_key_path=timestamp_public_key_path,
        market_data_public_key_path=market_data_public_key_path,
    )
    plan, plan_sha, _, _ = read_json_snapshot(
        plan_path,
        label="Consumer canonical future plan",
    )
    exact_claims = {
        "schema_version": PLAN_SCHEMA,
        "status": "registered_external_timestamp_pending_first_signal",
        "evidence_class": "prospective_future_only",
        "baseline_state": "frozen_no_reestimation",
        "policy_id": POLICY_ID,
        "target_contract": TARGET_CONTRACT,
        "acceptance_thresholds": ACCEPTANCE_THRESHOLDS,
        "cohort_minimum_cross_sections": COHORT_MINIMUMS,
        "historical_results_can_authorize_production": False,
        "production_activation_authorized": False,
        "portfolio_write_enabled": False,
        "optimizer_cap": 0.0,
    }
    for field, expected in exact_claims.items():
        if plan.get(field) != expected:
            raise ValueError(f"Consumer canonical plan changed field: {field}")
    optimizer_cap = plan.get("optimizer_cap")
    if (
        type(optimizer_cap) not in {int, float}
        or not math.isfinite(float(optimizer_cap))
        or float(optimizer_cap) != 0.0
    ):
        raise ValueError("Consumer canonical plan optimizer cap must be explicit numeric zero")
    candidate_audit = _candidate_census(
        source_paths["candidate_registry"], source_paths["universe_contract"]
    )
    frozen_baseline_audit = validate_frozen_baseline_spec(
        source_paths["frozen_baseline_spec"],
        expected_cohorts=sorted(COHORT_MINIMUMS),
    )
    calendar_bytes = (
        Path(source_paths["trading_calendar"]).expanduser().resolve().read_bytes()
    )
    _, calendar_audit = validate_official_xnys_calendar_bytes(calendar_bytes)
    calendar_sha = hashlib.sha256(calendar_bytes).hexdigest()
    source_hashes = {
        role: (
            candidate_audit["source_snapshot_sha256"][role]
            if role in candidate_audit["source_snapshot_sha256"]
            else frozen_baseline_audit["source_snapshot"]["sha256"]
            if role == "frozen_baseline_spec"
            else calendar_sha
            if role == "trading_calendar"
            else hashlib.sha256(Path(path).read_bytes()).hexdigest()
        )
        for role, path in sorted(source_paths.items())
    }
    source_archive_audit = {
        role: require_content_addressed_archive(path, expected_sha256=source_hashes[role])
        for role, path in sorted(source_paths.items())
    }
    if plan.get("registered_source_sha256") != source_hashes:
        raise ValueError("Consumer plan does not bind exact registered source bytes")
    contract = _contract(plan)
    domain = canonical_domain_contract(
        registered_source_sha256=source_hashes,
        candidate_audit=candidate_audit,
        frozen_baseline_audit=frozen_baseline_audit,
    )
    domain_hash = domain_contract_sha256(contract, domain)
    if plan.get("domain_contract_sha256") != domain_hash:
        raise ValueError("Consumer plan domain contract hash mismatch")
    receipt_path = Path(registration_receipt_path).expanduser().resolve()
    receipt_bytes = receipt_path.read_bytes()
    receipt_hash = hashlib.sha256(receipt_bytes).hexdigest()
    if receipt_hash != exact_sha256(
        expected_registration_receipt_sha256, label="registration receipt sha256"
    ):
        raise ValueError("Consumer registration receipt hash mismatch")
    plan_archive = require_content_addressed_archive(
        plan_path, expected_sha256=plan_sha
    )
    receipt_archive = require_content_addressed_archive(
        receipt_path, expected_sha256=receipt_hash
    )
    timestamp_receipt_archive = require_content_addressed_archive(
        registration_timestamp_receipt_path,
        expected_sha256=expected_registration_timestamp_receipt_sha256,
    )
    receipt = require_receipt_contract_binding(
        receipt_path,
        expected_domain_contract_sha256=domain_hash,
        receipt_snapshot_bytes=receipt_bytes,
    )
    bundle.evidence_seal.verify_snapshot(receipt_bytes, receipt_hash, receipt)
    expected_receipt = {
        "schema_version": REGISTRATION_RECEIPT_SCHEMA,
        "family": "consumer_defensive",
        "policy_id": POLICY_ID,
        "plan_sha256": plan_sha,
        "registered_source_sha256": source_hashes,
        "contract_identity_sha256": canonical_sha256(contract.identity()),
        "trading_calendar_sha256": source_hashes["trading_calendar"],
    }
    for field, expected in expected_receipt.items():
        if receipt.get(field) != expected:
            raise ValueError(f"Consumer registration receipt changed field: {field}")
    timestamp_audit = validate_external_timestamp(
        subject_path=receipt_path,
        timestamp_receipt_path=registration_timestamp_receipt_path,
        expected_timestamp_receipt_sha256=expected_registration_timestamp_receipt_sha256,
        expected_subject_sha256=receipt_hash,
        bundle=bundle,
        expected_previous_log_head_sha256=bundle.genesis_log_head_sha256,
        expected_previous_log_sequence=bundle.genesis_log_sequence,
        expected_family="consumer_defensive",
        expected_policy_id=POLICY_ID,
        expected_subject_role="registration_receipt",
        expected_slot_id=f"consumer_defensive:{POLICY_ID}:registration:v1",
        subject_snapshot_bytes=receipt_bytes,
    )
    anchored_at = exact_utc(
        timestamp_audit["observed_at_utc"],
        label="Consumer registration external observation time",
    )
    first_signal = first_proven_month_end_after(
        source_paths["trading_calendar"],
        after_utc=max(anchored_at, bundle.activated_at_utc),
        trading_calendar_snapshot_bytes=calendar_bytes,
    )
    if plan.get("effective_from") != anchored_at.date().isoformat():
        raise ValueError("Consumer effective date must equal external registration date")
    if plan.get("first_signal_date") != first_signal:
        raise ValueError("Consumer first signal is not the first future proven month-end")
    return plan, contract, bundle, {
        "canonical_trust": bundle.audit(),
        "domain_contract": domain,
        "domain_contract_sha256": domain_hash,
        "registration_receipt_sha256": receipt_hash,
        "registered_plan_sha256": plan_sha,
        "registration_external_timestamp": timestamp_audit,
        "official_calendar": calendar_audit,
        "candidate_census": candidate_audit,
        "frozen_baseline_replay_contract": frozen_baseline_audit,
        "registered_source_archive_audit": source_archive_audit,
        "registered_plan_archive_audit": plan_archive,
        "registration_receipt_archive_audit": receipt_archive,
        "registration_timestamp_receipt_archive_audit": timestamp_receipt_archive,
        "trusted_registration_pass": True,
    }


__all__ = [
    "ACCEPTANCE_THRESHOLDS",
    "CANDIDATE_SCHEMA",
    "COHORT_MINIMUMS",
    "LIFECYCLE_EVENT_SCHEMA_V5",
    "PLAN_SCHEMA",
    "POLICY_ID",
    "REQUIRED_PLAN_ROLES",
    "TARGET_CONTRACT",
    "UNIVERSE_SCHEMA",
    "canonical_domain_contract",
    "validate_registered_plan_v5",
]
