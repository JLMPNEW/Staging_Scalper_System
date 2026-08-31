"""Out-of-band-authority Consumer prospective plan contract."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from future_only_evidence.authority_config import (
    DEFAULT_AUTHORITY_REGISTRY,
    load_pinned_authority,
)
from future_only_evidence.official_calendar import validate_official_xnys_calendar
from future_only_evidence.protocol import file_sha256
from future_only_evidence.prospective_contracts import ProspectiveContract, read_calendar

from .future_oos_plan_v3 import (
    CANONICAL_TARGET_CONTRACT,
    CANONICAL_THRESHOLDS,
    REQUIRED_PLAN_ROLES_V3,
)
from .future_oos_protocol_v1 import (
    DEFAULT_MINIMUM_COUNTS,
    REQUIRED_PLAN_ROLES,
    validate_registered_plan,
)


CANONICAL_SELECTION_CONTRACT = {
    "selection_fraction": 0.20,
    "selection_policy": "top_and_bottom_ceiling_fraction_minimum_one_per_cohort",
}
CANONICAL_POLICY_ID = "consumer_defensive_frozen_baseline_future_gate_v1"
CANONICAL_MINIMUM_CROSS_SECTIONS = {
    "beverages": 8,
    "consumer_staples_distribution_retail": 8,
    "household_personal_tobacco": 8,
    "packaged_foods_agricultural_products": 8,
}


def validate_registered_plan_v4(
    plan_path: Path,
    *,
    source_paths: Mapping[str, Path],
    registration_receipt_path: Path,
    expected_registration_receipt_sha256: str,
    trusted_public_key_path: Path,
    authority_registry_path: Path = DEFAULT_AUTHORITY_REGISTRY,
) -> tuple[dict[str, Any], ProspectiveContract, Any, dict[str, Any]]:
    if set(source_paths) != REQUIRED_PLAN_ROLES_V3:
        raise ValueError("Consumer registered plan roles must include the exact calendar")
    authority, authority_audit = load_pinned_authority(
        "consumer_defensive",
        public_key_path=trusted_public_key_path,
        registry_path=authority_registry_path,
    )
    plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    if not isinstance(plan, dict):
        raise ValueError("Consumer registered plan must be a JSON object")
    for field, expected in CANONICAL_TARGET_CONTRACT.items():
        if plan.get(field) != expected:
            raise ValueError(f"Consumer canonical target contract changed: {field}")
    for field, expected in CANONICAL_SELECTION_CONTRACT.items():
        if plan.get(field) != expected:
            raise ValueError(f"Consumer canonical selection contract changed: {field}")
    if plan.get("minimum_nonoverlapping_outcomes") != {
        str(key): value for key, value in DEFAULT_MINIMUM_COUNTS.items()
    }:
        raise ValueError("Consumer future counts must remain exact 12/6/4")
    if plan.get("acceptance_thresholds") != CANONICAL_THRESHOLDS:
        raise ValueError("Consumer canonical thresholds changed")
    if plan.get("policy_id") != CANONICAL_POLICY_ID:
        raise ValueError("Consumer prospective policy id changed")
    if plan.get("minimum_cross_sections") != CANONICAL_MINIMUM_CROSS_SECTIONS:
        raise ValueError("Consumer cohort minimum cross-sections changed")
    if plan.get("registered_before_target_access") is not True:
        raise ValueError("Consumer plan was not registered before target access")
    if plan.get("trusted_receipt_authority") != {
        key: authority_audit[key]
        for key in ("authority_id", "public_key_sha256", "algorithm")
    }:
        raise ValueError("Consumer plan authority differs from the out-of-band trust root")
    validated, policy = validate_registered_plan(
        plan_path,
        source_paths={role: source_paths[role] for role in REQUIRED_PLAN_ROLES},
        registration_receipt_path=registration_receipt_path,
        expected_registration_receipt_sha256=expected_registration_receipt_sha256,
        registration_receipt_verifier=authority.verify,
    )
    receipt = json.loads(Path(registration_receipt_path).read_text(encoding="utf-8"))
    registered_at = datetime.fromisoformat(
        str(receipt.get("registered_at_utc")).replace("Z", "+00:00")
    )
    if registered_at.tzinfo is None or registered_at.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError("Consumer registered_at_utc must be timezone-aware UTC")
    calendar_audit = validate_official_xnys_calendar(source_paths["trading_calendar"])
    calendar_rows, _ = read_calendar(source_paths["trading_calendar"])
    month_ends: dict[tuple[int, int], str] = {}
    for row in calendar_rows:
        session = str(row["session_date"])[:10]
        parsed = datetime.fromisoformat(session).date()
        month_ends[(parsed.year, parsed.month)] = session
    proven_month_ends = [
        value for _, value in sorted(month_ends.items())[:-1]
    ]
    eligible_first = [
        value
        for value in proven_month_ends
        if datetime.fromisoformat(value).date() > registered_at.date()
    ]
    if not eligible_first:
        raise ValueError("Consumer calendar has no proven future month-end after registration")
    first_signal_date = eligible_first[0]
    if str(plan.get("effective_from")) != registered_at.date().isoformat():
        raise ValueError("Consumer effective_from must equal signed registration date")
    if str(plan.get("first_signal_date")) != first_signal_date:
        raise ValueError("Consumer first signal must be the first proven month-end after registration")
    calendar_hash = file_sha256(source_paths["trading_calendar"])
    if plan.get("registered_trading_calendar_sha256") != calendar_hash:
        raise ValueError("Consumer plan does not bind exact trading-calendar bytes")
    if receipt.get("trading_calendar_sha256") != calendar_hash:
        raise ValueError("Consumer registration receipt does not bind exact calendar bytes")
    if receipt.get("canonical_target_contract_sha256") != __import__(
        "future_only_evidence.protocol", fromlist=["canonical_sha256"]
    ).canonical_sha256({**CANONICAL_TARGET_CONTRACT, **CANONICAL_SELECTION_CONTRACT}):
        raise ValueError("Consumer registration receipt does not bind the target/selection contract")
    contract = ProspectiveContract(
        family="consumer_defensive",
        policy_id=CANONICAL_POLICY_ID,
        effective_from=registered_at.date(),
        first_signal_date=datetime.fromisoformat(first_signal_date).date(),
        horizons=(21, 63, 126),
        minimum_counts=DEFAULT_MINIMUM_COUNTS,
        benchmark_ticker="XLP",
        cadence_id="monthly_true_month_end_v1",
        minimum_ic=0.0,
        minimum_efficacy=0.0,
        minimum_top_minus_bottom=0.0,
        minimum_hit_rate=0.55,
        transaction_cost_bps=20.0,
        top_minus_bottom_basis="net",
    )
    return validated, contract, authority, {
        **authority_audit,
        "authority_registry_sha256": file_sha256(authority_registry_path),
        "registered_plan_sha256": file_sha256(plan_path),
        "registration_receipt_sha256": file_sha256(registration_receipt_path),
        "trusted_out_of_band_authority_pass": True,
        "canonical_target_contract_pass": True,
        "official_calendar_audit": calendar_audit,
    }


__all__ = [
    "CANONICAL_MINIMUM_CROSS_SECTIONS",
    "CANONICAL_POLICY_ID",
    "CANONICAL_SELECTION_CONTRACT",
    "validate_registered_plan_v4",
]
