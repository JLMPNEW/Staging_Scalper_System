"""Signed-authority Consumer capture with exact pre-entry chronology."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from future_only_evidence.capture_integrity import validate_capture_receipt_timing
from future_only_evidence.protocol import build_capture_payload, canonical_sha256, file_sha256

from .future_oos_capture_v2 import (
    REQUIRED_CAPTURE_ROLES_V2,
    validate_capture_sources,
)
from .future_oos_plan_v3 import REQUIRED_PLAN_ROLES_V3, validate_registered_plan_v3


def capture_signal(
    *,
    plan_path: Path,
    plan_source_paths: Mapping[str, Path],
    registration_receipt_path: Path,
    expected_registration_receipt_sha256: str,
    trusted_public_key_path: Path,
    asof_date: str,
    signal_rows: Sequence[Mapping[str, Any]],
    capture_source_paths: Mapping[str, Path],
    expected_capture_source_sha256: Mapping[str, str],
    trusted_capture_receipt_path: Path,
    expected_trusted_capture_receipt_sha256: str,
) -> dict[str, Any]:
    plan, policy, authority = validate_registered_plan_v3(
        plan_path,
        source_paths=plan_source_paths,
        registration_receipt_path=registration_receipt_path,
        expected_registration_receipt_sha256=expected_registration_receipt_sha256,
        trusted_public_key_path=trusted_public_key_path,
    )
    if set(plan_source_paths) != REQUIRED_PLAN_ROLES_V3:
        raise ValueError("Consumer plan roles changed")
    if set(capture_source_paths) != REQUIRED_CAPTURE_ROLES_V2:
        raise ValueError("Consumer capture roles changed")
    for role in REQUIRED_PLAN_ROLES_V3:
        if file_sha256(capture_source_paths[role]) != file_sha256(plan_source_paths[role]):
            raise ValueError(f"Consumer capture changed registered source: {role}")
    source_audit = validate_capture_sources(
        asof_date=asof_date,
        signal_rows=signal_rows,
        capture_source_paths=capture_source_paths,
    )
    timing = validate_capture_receipt_timing(
        receipt_path=trusted_capture_receipt_path,
        authority=authority,
        asof_date=asof_date,
        trading_calendar_path=capture_source_paths["trading_calendar"],
    )
    payload = build_capture_payload(
        policy=policy,
        asof_date=asof_date,
        capture_date=asof_date,
        signal_rows=signal_rows,
        source_paths=capture_source_paths,
        expected_source_sha256=expected_capture_source_sha256,
        required_source_roles=REQUIRED_CAPTURE_ROLES_V2,
        trusted_receipt_path=trusted_capture_receipt_path,
        expected_trusted_receipt_sha256=expected_trusted_capture_receipt_sha256,
        trusted_receipt_verifier=authority.verify,
    )
    payload.pop("capture_id")
    payload.pop("payload_sha256")
    payload.update(
        domain_schema_version="consumer_defensive_future_only_signal_capture_v3",
        registration_plan_sha256=file_sha256(plan_path),
        registration_receipt_sha256=file_sha256(registration_receipt_path),
        baseline_state=plan["baseline_state"],
        canonical_target_contract={
            "benchmark": plan["benchmark"],
            "target_field": plan["target_field"],
            "target_horizons_sessions": plan["target_horizons_sessions"],
            "entry_policy": plan["entry_policy"],
            "exit_policy": plan["exit_policy"],
        },
        source_freshness_audit=source_audit,
        trusted_capture_timing=timing,
        prospective_membership_tracking_required=True,
        terminal_event_tracking_required=True,
        exact_session_calendar_required=True,
    )
    payload["capture_id"] = canonical_sha256(payload)
    payload["payload_sha256"] = canonical_sha256(payload)
    return payload


__all__ = ["capture_signal"]
