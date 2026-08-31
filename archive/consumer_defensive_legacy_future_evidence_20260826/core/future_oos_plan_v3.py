"""Canonical registered Consumer target and trust-authority contract."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from future_only_evidence.trusted_receipts import (
    PinnedEd25519Authority,
    registered_authority,
)

from .future_oos_protocol_v1 import (
    DEFAULT_MINIMUM_COUNTS,
    REQUIRED_PLAN_ROLES,
    validate_registered_plan,
)


REQUIRED_PLAN_ROLES_V3 = frozenset({*REQUIRED_PLAN_ROLES, "trading_calendar"})
CANONICAL_TARGET_CONTRACT = {
    "benchmark": "XLP",
    "target_field": "forward_xlp_residual_return",
    "target_horizons_sessions": [21, 63, 126],
    "horizon_weights": {"21": 0.20, "63": 0.50, "126": 0.30},
    "primary_objective": "weighted_mean_rank_ic",
    "scoring_frequency": "monthly",
    "rebalance_frequency": "monthly",
    "entry_policy": "next_frozen_calendar_session_after_true_month_end",
    "exit_policy": "fixed_trading_session_horizons_21_63_126",
}
CANONICAL_THRESHOLDS = {
    "minimum_ic": 0.0,
    "minimum_top_xlp_residual_net": 0.0,
    "minimum_top_minus_benchmark_net": 0.0,
    "minimum_top_minus_bottom_net": 0.0,
    "minimum_sign_hit_rate": 0.55,
    "transaction_cost_bps": 20.0,
}


def validate_registered_plan_v3(
    plan_path: Path,
    *,
    source_paths: Mapping[str, Path],
    registration_receipt_path: Path,
    expected_registration_receipt_sha256: str,
    trusted_public_key_path: Path,
) -> tuple[dict[str, Any], Any, PinnedEd25519Authority]:
    if set(source_paths) != REQUIRED_PLAN_ROLES_V3:
        raise ValueError("Consumer registered plan roles must include the exact trading calendar")
    plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    if not isinstance(plan, dict):
        raise ValueError("Consumer plan must be a JSON object")
    for field, expected in CANONICAL_TARGET_CONTRACT.items():
        if plan.get(field) != expected:
            raise ValueError(f"Consumer canonical target contract changed: {field}")
    if plan.get("minimum_nonoverlapping_outcomes") != {
        str(key): value for key, value in DEFAULT_MINIMUM_COUNTS.items()
    }:
        raise ValueError("Consumer registered counts must remain exact 12/6/4")
    if plan.get("acceptance_thresholds") != CANONICAL_THRESHOLDS:
        raise ValueError("Consumer canonical acceptance thresholds changed")
    authority = registered_authority(plan, public_key_path=trusted_public_key_path)
    plan_without_calendar = {role: source_paths[role] for role in REQUIRED_PLAN_ROLES}
    validated_plan, policy = validate_registered_plan(
        plan_path,
        source_paths=plan_without_calendar,
        registration_receipt_path=registration_receipt_path,
        expected_registration_receipt_sha256=expected_registration_receipt_sha256,
        registration_receipt_verifier=authority.verify,
    )
    calendar_hash = __import__(
        "future_only_evidence.protocol",
        fromlist=["file_sha256"],
    ).file_sha256(source_paths["trading_calendar"])
    if plan.get("registered_trading_calendar_sha256") != calendar_hash:
        raise ValueError("Consumer plan does not bind exact trading-calendar bytes")
    receipt = json.loads(Path(registration_receipt_path).read_text(encoding="utf-8"))
    if receipt.get("trading_calendar_sha256") != calendar_hash:
        raise ValueError("Consumer registration receipt does not bind exact trading-calendar bytes")
    return validated_plan, policy, authority


__all__ = [
    "CANONICAL_TARGET_CONTRACT",
    "CANONICAL_THRESHOLDS",
    "REQUIRED_PLAN_ROLES_V3",
    "validate_registered_plan_v3",
]
