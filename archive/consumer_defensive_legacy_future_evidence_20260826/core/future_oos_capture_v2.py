"""Freshness- and calendar-bound Consumer prospective signal capture."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from future_only_evidence.protocol import (
    TrustedReceiptVerifier,
    build_capture_payload,
    canonical_sha256,
    file_sha256,
)

from .future_oos_protocol_v1 import (
    REQUIRED_CAPTURE_ROLES,
    REQUIRED_PLAN_ROLES,
    validate_registered_plan,
)


REQUIRED_CAPTURE_ROLES_V2 = frozenset({*REQUIRED_CAPTURE_ROLES, "trading_calendar"})
REQUIRED_FRESHNESS_GATES = frozenset(
    {
        "market_data_fresh",
        "financial_data_fresh",
        "positioning_data_fresh",
        "membership_current",
        "terminal_events_current",
    }
)


def _json(path: Path, *, label: str) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    return dict(payload)


def validate_capture_sources(
    *,
    asof_date: str,
    signal_rows: Sequence[Mapping[str, Any]],
    capture_source_paths: Mapping[str, Path],
) -> dict[str, Any]:
    manifest = _json(capture_source_paths["source_manifest"], label="Consumer future source manifest")
    if manifest.get("schema_version") != "consumer_defensive_future_source_manifest_v1":
        raise ValueError("unsupported Consumer future source manifest")
    role = str(manifest.get("evidence_role") or "").lower()
    if not role or any(token in role for token in ("historical", "diagnostic", "revealed", "posthoc")):
        raise ValueError("historical/revealed Consumer source manifests cannot start the future clock")
    if str(manifest.get("asof_date") or "")[:10] != str(asof_date)[:10]:
        raise ValueError("Consumer source manifest asof mismatch")
    if manifest.get("historical_results_can_authorize_production") is not False:
        raise ValueError("Consumer source manifest must preserve historical fail-closed governance")
    if manifest.get("production_activation_authorized") is not False:
        raise ValueError("Consumer source generation cannot self-authorize production")
    freshness = manifest.get("freshness_gates")
    if not isinstance(freshness, dict) or set(freshness) != REQUIRED_FRESHNESS_GATES:
        raise ValueError("Consumer source manifest freshness gates are incomplete")
    failed = sorted(field for field in REQUIRED_FRESHNESS_GATES if freshness.get(field) is not True)
    if failed:
        raise ValueError(f"Consumer source freshness failed={failed}")
    hashes = manifest.get("artifact_sha256")
    bound_roles = REQUIRED_CAPTURE_ROLES_V2 - {"source_manifest"}
    if not isinstance(hashes, dict) or set(hashes) != bound_roles:
        raise ValueError("Consumer source manifest artifact hash roles are incomplete")
    for role_name in sorted(bound_roles):
        if hashes[role_name] != file_sha256(capture_source_paths[role_name]):
            raise ValueError(f"Consumer source manifest hash mismatch: {role_name}")
    membership = _json(capture_source_paths["membership_snapshot"], label="Consumer membership snapshot")
    if membership.get("schema_version") != "consumer_defensive_future_membership_snapshot_v1":
        raise ValueError("unsupported Consumer membership snapshot")
    if str(membership.get("asof_date") or "")[:10] != str(asof_date)[:10]:
        raise ValueError("Consumer membership snapshot asof mismatch")
    member_rows = membership.get("rows")
    if not isinstance(member_rows, list) or not all(isinstance(row, dict) for row in member_rows):
        raise ValueError("Consumer membership rows are invalid")
    member_index: dict[str, dict[str, Any]] = {}
    for row in member_rows:
        ticker = str(row.get("ticker") or "").strip().upper()
        if not ticker or ticker in member_index:
            raise ValueError("Consumer membership contains blank/duplicate ticker")
        member_index[ticker] = dict(row)
    signal_index = {
        str(row.get("ticker") or "").strip().upper(): row
        for row in signal_rows
        if int(row.get("eligible_flag") or 0) == 1
    }
    if set(member_index) != set(signal_index):
        raise ValueError("Consumer membership ticker census does not match eligible rank signals")
    for ticker, signal in signal_index.items():
        member = member_index[ticker]
        if str(member.get("cohort_id")) != str(signal.get("sleeve_id")):
            raise ValueError(f"{ticker}: Consumer cohort membership mismatch")
        if str(member.get("eligible_at_entry_flag")) not in {"1", "True", "true"}:
            raise ValueError(f"{ticker}: Consumer membership not eligible at entry")
        if str(member.get("terminal_event_status")) not in {"none", "pending_governed_disposition"}:
            raise ValueError(f"{ticker}: Consumer terminal-event tracking is invalid")
    return {
        "source_manifest_sha256": file_sha256(capture_source_paths["source_manifest"]),
        "freshness_gates": freshness,
        "eligible_ticker_count": len(signal_index),
        "eligible_ticker_census_sha256": canonical_sha256(sorted(signal_index)),
    }


def capture_signal(
    *,
    plan_path: Path,
    plan_source_paths: Mapping[str, Path],
    registration_receipt_path: Path,
    expected_registration_receipt_sha256: str,
    registration_receipt_verifier: TrustedReceiptVerifier | None,
    asof_date: str,
    signal_rows: Sequence[Mapping[str, Any]],
    capture_source_paths: Mapping[str, Path],
    expected_capture_source_sha256: Mapping[str, str],
    trusted_capture_receipt_path: Path,
    expected_trusted_capture_receipt_sha256: str,
    trusted_capture_receipt_verifier: TrustedReceiptVerifier | None,
) -> dict[str, Any]:
    plan, policy = validate_registered_plan(
        plan_path,
        source_paths=plan_source_paths,
        registration_receipt_path=registration_receipt_path,
        expected_registration_receipt_sha256=expected_registration_receipt_sha256,
        registration_receipt_verifier=registration_receipt_verifier,
    )
    if set(capture_source_paths) != REQUIRED_CAPTURE_ROLES_V2:
        raise ValueError("calendar-bound Consumer source roles do not exactly match the contract")
    for role in REQUIRED_PLAN_ROLES:
        if file_sha256(capture_source_paths[role]) != file_sha256(plan_source_paths[role]):
            raise ValueError(f"Consumer capture changed registered source: {role}")
    source_audit = validate_capture_sources(
        asof_date=asof_date,
        signal_rows=signal_rows,
        capture_source_paths=capture_source_paths,
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
        trusted_receipt_verifier=trusted_capture_receipt_verifier,
    )
    payload.pop("capture_id")
    payload.pop("payload_sha256")
    payload.update(
        domain_schema_version="consumer_defensive_future_only_signal_capture_v2",
        registration_plan_sha256=file_sha256(plan_path),
        registration_receipt_sha256=file_sha256(registration_receipt_path),
        baseline_state=plan["baseline_state"],
        source_freshness_audit=source_audit,
        prospective_membership_tracking_required=True,
        terminal_event_tracking_required=True,
        exact_session_calendar_required=True,
    )
    payload["capture_id"] = canonical_sha256(payload)
    payload["payload_sha256"] = canonical_sha256(payload)
    return payload


__all__ = [
    "REQUIRED_CAPTURE_ROLES_V2",
    "REQUIRED_FRESHNESS_GATES",
    "capture_signal",
    "validate_capture_sources",
]
