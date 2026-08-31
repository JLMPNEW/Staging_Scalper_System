"""Exact PIT lifecycle evidence for canonical prospective signal captures."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping

from .canonical_trust import CanonicalTrustBundle
from .canonical_values import exact_utc
from .protocol import canonical_sha256, exact_sha256
from .prospective_contracts import PROSPECTIVE_ROLE


LIFECYCLE_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "evidence_role",
        "asof_date",
        "snapshot_generated_at_utc",
        "rows",
        "rows_sha256",
    }
)
LIFECYCLE_ROW_FIELDS = frozenset(
    {
        "asof_date",
        "ticker",
        "lifecycle_status_at_signal_cutoff",
        "terminal_event_type",
        "terminal_event_effective_at_utc",
        "terminal_event_reason_code",
        "source_available_at_utc",
        "source_observation_id",
        "source_locator",
        "source_record_sha256",
        "provider_id",
        "dataset_id",
    }
)
TERMINAL_EVENT_TYPE_TO_REASON = {
    "bankruptcy_terminal": "bankruptcy",
    "cash_liquidation": "cash_liquidation",
    "delisting": "delisting",
    "merger_cash": "cash_merger",
    "trading_halt_terminal": "exchange_halt_terminal",
}
LIFECYCLE_EVENT_CONTRACT_ID = "pit_lifecycle_event_snapshot_contract_v1"
LIFECYCLE_SOURCE_ATTESTATION_SCHEMA = "future_lifecycle_source_attestation_v1"
LIFECYCLE_STATUS_ASOF_POLICY = "official_close_pit_no_future_events_v1"
ACTIVE_SOURCE_MAX_AGE_HOURS = 24
LIFECYCLE_ATTESTATION_FIELDS = frozenset(
    {
        "schema_version",
        "authority_id",
        "signature_base64",
        "signed_payload_sha256",
        "family",
        "policy_id",
        "asof_date",
        "lifecycle_snapshot_sha256",
        "lifecycle_rows_sha256",
        "ticker_count",
        "ticker_census_sha256",
        "provider_id",
        "dataset_id",
        "source_max_information_at_utc",
        "status_effective_through_at_utc",
        "exported_at_utc",
        "status_asof_policy",
        "query_sha256",
        "observation_ids_sha256",
    }
)


def lifecycle_event_contract(schema_version: str) -> dict[str, Any]:
    return {
        "contract_id": LIFECYCLE_EVENT_CONTRACT_ID,
        "schema_version": schema_version,
        "evidence_role": PROSPECTIVE_ROLE,
        "top_level_fields": sorted(LIFECYCLE_TOP_LEVEL_FIELDS),
        "row_fields": sorted(LIFECYCLE_ROW_FIELDS),
        "active_terminal_fields": None,
        "active_reason_semantics": "terminal_fields_all_json_null_v1",
        "terminal_event_type_to_reason": TERMINAL_EVENT_TYPE_TO_REASON,
        "ticker_census": "exact_frozen_signal_candidate_census_v1",
        "information_time_policy": (
            "event_effective_le_source_available_le_signal_cutoff_v1"
        ),
        "active_source_max_age_hours": ACTIVE_SOURCE_MAX_AGE_HOURS,
        "source_attestation_schema": LIFECYCLE_SOURCE_ATTESTATION_SCHEMA,
        "status_asof_policy": LIFECYCLE_STATUS_ASOF_POLICY,
        "source_authority_role": "pinned_market_data_export_ed25519_authority",
        "membership_role": "assertion_reconciled_to_attested_lifecycle_source_v1",
    }


def _exact_date(value: Any, *, label: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{label} must be an exact YYYY-MM-DD string")
    text = value
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{label} must be exact YYYY-MM-DD") from exc
    if parsed.isoformat() != text:
        raise ValueError(f"{label} must be exact YYYY-MM-DD")
    return text


def _utc(value: Any, *, label: str) -> datetime:
    return exact_utc(value, label=label)


def _nonblank_exact(value: Any, *, label: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise ValueError(f"{label} must be a nonblank canonical string")
    return value


def validate_lifecycle_event_snapshot(
    path: Path,
    *,
    expected_schema_version: str,
    asof_date: str,
    expected_tickers: Iterable[str],
    signal_cutoff_at_utc: str,
    family: str,
    policy_id: str,
    attestation_path: Path,
    expected_attestation_sha256: str,
    bundle: CanonicalTrustBundle,
    snapshot_bytes: bytes | None = None,
    attestation_bytes: bytes | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    asof = _exact_date(asof_date, label="lifecycle snapshot asof")
    cutoff = _utc(signal_cutoff_at_utc, label="lifecycle signal cutoff")
    payload_bytes = (
        bytes(snapshot_bytes)
        if snapshot_bytes is not None
        else Path(path).expanduser().resolve().read_bytes()
    )
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("lifecycle event snapshot must be valid UTF-8 JSON") from exc
    if not isinstance(payload, dict) or set(payload) != LIFECYCLE_TOP_LEVEL_FIELDS:
        raise ValueError("lifecycle snapshot top-level field census changed")
    if (
        payload.get("schema_version") != expected_schema_version
        or payload.get("evidence_role") != PROSPECTIVE_ROLE
        or payload.get("asof_date") != asof
    ):
        raise ValueError("lifecycle snapshot identity/asof changed")
    generated = _utc(
        payload.get("snapshot_generated_at_utc"),
        label="lifecycle snapshot generation time",
    )
    rows = payload.get("rows")
    if not isinstance(rows, list) or payload.get("rows_sha256") != canonical_sha256(rows):
        raise ValueError("lifecycle snapshot rows are absent or hash-inconsistent")
    expected_list = list(expected_tickers)
    if (
        not expected_list
        or any(
            type(ticker) is not str
            or not ticker
            or ticker.strip() != ticker
            or ticker.upper() != ticker
            for ticker in expected_list
        )
        or len(set(expected_list)) != len(expected_list)
    ):
        raise ValueError("lifecycle expected ticker census is invalid")
    expected = set(expected_list)
    snapshot_sha = hashlib.sha256(payload_bytes).hexdigest()
    receipt_bytes = (
        bytes(attestation_bytes)
        if attestation_bytes is not None
        else Path(attestation_path).expanduser().resolve().read_bytes()
    )
    receipt_sha = hashlib.sha256(receipt_bytes).hexdigest()
    if receipt_sha != exact_sha256(
        expected_attestation_sha256,
        label="lifecycle source attestation sha256",
    ):
        raise ValueError("lifecycle source attestation SHA-256 mismatch")
    try:
        attestation = json.loads(receipt_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("lifecycle source attestation must be valid UTF-8 JSON") from exc
    if not isinstance(attestation, dict) or set(attestation) != LIFECYCLE_ATTESTATION_FIELDS:
        raise ValueError("lifecycle source attestation field census changed")
    bundle.market_data_export.verify_snapshot(receipt_bytes, receipt_sha, attestation)
    provider_id = _nonblank_exact(
        attestation.get("provider_id"), label="lifecycle provider id"
    )
    dataset_id = _nonblank_exact(
        attestation.get("dataset_id"), label="lifecycle dataset id"
    )
    if provider_id not in bundle.allowed_provider_ids or dataset_id not in bundle.allowed_dataset_ids:
        raise ValueError("lifecycle provider/dataset is not in canonical trust allowlists")
    exported = _utc(
        attestation.get("exported_at_utc"), label="lifecycle attestation export time"
    )
    attested_max = _utc(
        attestation.get("source_max_information_at_utc"),
        label="attested lifecycle max information time",
    )
    status_effective_through = _utc(
        attestation.get("status_effective_through_at_utc"),
        label="lifecycle status effective-through time",
    )
    expected_attestation = {
        "schema_version": LIFECYCLE_SOURCE_ATTESTATION_SCHEMA,
        "family": family,
        "policy_id": policy_id,
        "asof_date": asof,
        "lifecycle_snapshot_sha256": snapshot_sha,
        "lifecycle_rows_sha256": payload["rows_sha256"],
        "ticker_count": len(expected),
        "ticker_census_sha256": canonical_sha256(sorted(expected)),
        "status_asof_policy": LIFECYCLE_STATUS_ASOF_POLICY,
    }
    for field, expected_value in expected_attestation.items():
        if attestation.get(field) != expected_value:
            raise ValueError(f"lifecycle source attestation changed field: {field}")
    if type(attestation.get("ticker_count")) is not int:
        raise ValueError("lifecycle attestation ticker count must be a canonical integer")
    exact_sha256(attestation.get("query_sha256"), label="lifecycle query sha256")
    exact_sha256(
        attestation.get("observation_ids_sha256"),
        label="lifecycle observation-id census sha256",
    )
    if (
        status_effective_through != cutoff
        or not cutoff <= generated <= exported
        or not attested_max <= cutoff <= exported
    ):
        raise ValueError("lifecycle source attestation chronology is invalid")
    index: dict[str, dict[str, Any]] = {}
    observation_ids: set[str] = set()
    max_available: datetime | None = None
    terminal_count = 0
    for raw in rows:
        if not isinstance(raw, dict) or set(raw) != LIFECYCLE_ROW_FIELDS:
            raise ValueError("lifecycle snapshot row field census changed")
        row = dict(raw)
        if row.get("asof_date") != asof:
            raise ValueError("lifecycle snapshot row asof differs from capture")
        ticker = _nonblank_exact(row.get("ticker"), label="lifecycle ticker")
        if ticker.upper() != ticker:
            raise ValueError("lifecycle ticker must be canonical uppercase")
        if ticker in index:
            raise ValueError("lifecycle snapshot contains duplicate ticker evidence")
        status = row.get("lifecycle_status_at_signal_cutoff")
        available = _utc(
            row.get("source_available_at_utc"),
            label=f"{ticker} lifecycle source availability",
        )
        observation_id = _nonblank_exact(
            row.get("source_observation_id"),
            label=f"{ticker} lifecycle source observation id",
        )
        if observation_id in observation_ids:
            raise ValueError("lifecycle source observation ids must be unique")
        observation_ids.add(observation_id)
        _nonblank_exact(row.get("source_locator"), label=f"{ticker} source locator")
        exact_sha256(
            row.get("source_record_sha256"),
            label=f"{ticker} lifecycle source record sha256",
        )
        if row.get("provider_id") != provider_id or row.get("dataset_id") != dataset_id:
            raise ValueError(f"{ticker}: lifecycle row provider/dataset differs from attestation")
        if available > cutoff:
            raise ValueError(f"{ticker}: lifecycle evidence was available after signal cutoff")
        if available > generated:
            raise ValueError(f"{ticker}: lifecycle snapshot predates its source observation")
        if status == "active":
            if any(
                row.get(field) is not None
                for field in (
                    "terminal_event_type",
                    "terminal_event_effective_at_utc",
                    "terminal_event_reason_code",
                )
            ):
                raise ValueError(f"{ticker}: active lifecycle row has terminal fields")
            if available < cutoff - timedelta(hours=ACTIVE_SOURCE_MAX_AGE_HOURS):
                raise ValueError(f"{ticker}: active lifecycle assertion is stale")
        elif status == "governed_terminal_event":
            terminal_count += 1
            event_type = row.get("terminal_event_type")
            if event_type not in TERMINAL_EVENT_TYPE_TO_REASON:
                raise ValueError(f"{ticker}: terminal event type is outside policy")
            if row.get("terminal_event_reason_code") != TERMINAL_EVENT_TYPE_TO_REASON[event_type]:
                raise ValueError(f"{ticker}: terminal event reason differs from event type")
            effective = _utc(
                row.get("terminal_event_effective_at_utc"),
                label=f"{ticker} terminal event effective time",
            )
            if effective > available or effective > cutoff:
                raise ValueError(f"{ticker}: terminal event is post-cutoff knowledge")
        else:
            raise ValueError(f"{ticker}: lifecycle status is outside policy")
        index[ticker] = row
        max_available = available if max_available is None else max(max_available, available)
    if set(index) != expected or len(index) != len(expected):
        raise ValueError("lifecycle snapshot is not the exact frozen ticker census")
    if max_available is None or max_available != attested_max:
        raise ValueError("lifecycle attestation max-information time differs from rows")
    if attestation["observation_ids_sha256"] != canonical_sha256(
        sorted(observation_ids)
    ):
        raise ValueError("lifecycle attestation observation-id census differs from rows")
    return index, {
        "schema_version": expected_schema_version,
        "snapshot_sha256": snapshot_sha,
        "source_attestation_sha256": receipt_sha,
        "ticker_count": len(index),
        "terminal_event_count": terminal_count,
        "ticker_census_sha256": canonical_sha256(sorted(index)),
        "row_census_sha256": canonical_sha256(rows),
        "snapshot_generated_at_utc": generated.isoformat(),
        "max_source_available_at_utc": (
            max_available.isoformat() if max_available is not None else None
        ),
        "exact_ticker_census_pass": True,
        "exact_capture_asof_pass": True,
        "source_provider_id": provider_id,
        "source_dataset_id": dataset_id,
        "source_attestation_exported_at_utc": exported.isoformat(),
        "status_effective_through_at_utc": status_effective_through.isoformat(),
        "signal_cutoff_at_utc": cutoff.isoformat(),
        "exact_official_close_status_pass": True,
        "market_authority_attested_lifecycle_source_pass": True,
        "separate_source_reconciliation_pass": True,
        "active_source_freshness_pass": True,
        "no_post_cutoff_lifecycle_knowledge_pass": True,
    }


def validate_lifecycle_capture_chronology(
    lifecycle_audit: Mapping[str, Any],
    *,
    trusted_capture_timing: Mapping[str, Any],
    captured_at_utc: Any,
    label: str,
) -> dict[str, Any]:
    """Bind a replayed lifecycle audit to the capture's signed timing envelope."""
    if not isinstance(lifecycle_audit, Mapping) or not isinstance(
        trusted_capture_timing, Mapping
    ):
        raise ValueError(f"{label} lifecycle/capture timing audit is invalid")
    cutoff = _utc(
        trusted_capture_timing.get("signal_information_cutoff_at_utc"),
        label=f"{label} signed signal cutoff",
    )
    signed_max = _utc(
        trusted_capture_timing.get("source_max_information_at_utc"),
        label=f"{label} signed source max information time",
    )
    signed_generated = _utc(
        trusted_capture_timing.get("source_generated_at_utc"),
        label=f"{label} signed source generation time",
    )
    captured = _utc(captured_at_utc, label=f"{label} capture time")
    signed_captured = _utc(
        trusted_capture_timing.get("captured_at_utc"),
        label=f"{label} signed capture time",
    )
    entry = _utc(
        trusted_capture_timing.get("entry_execution_at_utc"),
        label=f"{label} entry time",
    )
    lifecycle_max = _utc(
        lifecycle_audit.get("max_source_available_at_utc"),
        label=f"{label} lifecycle max information time",
    )
    generated = _utc(
        lifecycle_audit.get("snapshot_generated_at_utc"),
        label=f"{label} lifecycle snapshot generation time",
    )
    exported = _utc(
        lifecycle_audit.get("source_attestation_exported_at_utc"),
        label=f"{label} lifecycle attestation export time",
    )
    effective_through = _utc(
        lifecycle_audit.get("status_effective_through_at_utc"),
        label=f"{label} lifecycle status effective-through time",
    )
    audit_cutoff = _utc(
        lifecycle_audit.get("signal_cutoff_at_utc"),
        label=f"{label} lifecycle audit signal cutoff",
    )
    if (
        lifecycle_audit.get("exact_official_close_status_pass") is not True
        or effective_through != cutoff
        or audit_cutoff != cutoff
        or captured != signed_captured
        or lifecycle_max > signed_max
        or generated > signed_generated
        or exported > signed_generated
        or not lifecycle_max <= cutoff <= exported <= captured < entry
    ):
        raise ValueError(f"{label} lifecycle evidence exceeds signed capture timing")
    return {
        "exact_official_close_status_pass": True,
        "lifecycle_within_signed_source_envelope_pass": True,
        "lifecycle_export_before_capture_pass": True,
        "capture_before_entry_pass": True,
    }


__all__ = [
    "LIFECYCLE_EVENT_CONTRACT_ID",
    "ACTIVE_SOURCE_MAX_AGE_HOURS",
    "LIFECYCLE_ATTESTATION_FIELDS",
    "LIFECYCLE_SOURCE_ATTESTATION_SCHEMA",
    "LIFECYCLE_STATUS_ASOF_POLICY",
    "LIFECYCLE_ROW_FIELDS",
    "LIFECYCLE_TOP_LEVEL_FIELDS",
    "TERMINAL_EVENT_TYPE_TO_REASON",
    "lifecycle_event_contract",
    "validate_lifecycle_capture_chronology",
    "validate_lifecycle_event_snapshot",
]
