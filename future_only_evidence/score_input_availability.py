"""Signed point-in-time source availability for prospective score inputs."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from .canonical_trust import CanonicalTrustBundle
from .canonical_values import exact_utc
from .protocol import canonical_sha256
from .prospective_contracts import PROSPECTIVE_ROLE


SCORE_INPUT_AVAILABILITY_SCHEMA = "future_score_input_availability_snapshot_v1"
SCORE_INPUT_AVAILABILITY_ATTESTATION_SCHEMA = (
    "future_score_input_availability_source_attestation_v1"
)
SCORE_INPUT_AVAILABILITY_POLICY = (
    "component_information_availability_official_close_pit_v1"
)
SOURCE_OBSERVATION_REUSE_POLICY = (
    "shared_observation_id_requires_exact_record_identity_v1"
)
AVAILABILITY_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "evidence_role",
        "asof_date",
        "snapshot_generated_at_utc",
        "rows",
        "rows_sha256",
    }
)
AVAILABILITY_ROW_FIELDS = frozenset(
    {
        "asof_date",
        "ticker",
        "component_name",
        "component_observation_id",
        "availability_status",
        "source_required_flag",
        "source_table",
        "source_id",
        "source_field",
        "source_asof_date",
        "component_input_value_sha256",
        "source_available_at_utc",
        "source_observation_id",
        "source_locator",
        "source_record_sha256",
        "provider_id",
        "dataset_id",
    }
)
AVAILABILITY_ATTESTATION_FIELDS = frozenset(
    {
        "schema_version",
        "authority_id",
        "signature_base64",
        "signed_payload_sha256",
        "family",
        "policy_id",
        "asof_date",
        "availability_snapshot_sha256",
        "availability_rows_sha256",
        "component_count",
        "component_observation_ids_sha256",
        "source_required_count",
        "source_observation_ids_sha256",
        "provider_id",
        "dataset_id",
        "source_max_information_at_utc",
        "status_effective_through_at_utc",
        "exported_at_utc",
        "status_asof_policy",
        "query_sha256",
    }
)


def score_input_availability_contract() -> dict[str, Any]:
    return {
        "schema_version": SCORE_INPUT_AVAILABILITY_SCHEMA,
        "attestation_schema": SCORE_INPUT_AVAILABILITY_ATTESTATION_SCHEMA,
        "evidence_role": PROSPECTIVE_ROLE,
        "top_level_fields": sorted(AVAILABILITY_TOP_LEVEL_FIELDS),
        "row_fields": sorted(AVAILABILITY_ROW_FIELDS),
        "status_asof_policy": SCORE_INPUT_AVAILABILITY_POLICY,
        "source_authority_role": "pinned_market_data_export_ed25519_authority",
        "component_census": "exact_atomic_component_observation_id_census_v1",
        "information_time_policy": (
            "source_available_le_official_close_cutoff_with_attested_negative_rows_v1"
        ),
        "source_observation_reuse_policy": SOURCE_OBSERVATION_REUSE_POLICY,
    }


def _exact_date(value: Any, *, label: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{label} must be an exact YYYY-MM-DD string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be exact YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{label} must be exact YYYY-MM-DD")
    return value


def _utc(value: Any, *, label: str) -> datetime:
    return exact_utc(value, label=label)


def _nonblank(value: Any, *, label: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise ValueError(f"{label} must be a nonblank canonical string")
    return value


def _strict_flag(value: Any, *, label: str) -> int:
    if type(value) is not int or value not in (0, 1):
        raise ValueError(f"{label} must be the canonical integer flag 0/1")
    return value


def _strict_sha256(value: Any, *, label: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or value.lower() != value
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be exact lowercase 64-hex SHA-256")
    return value


def _read_bytes(path: Path, supplied: bytes | None) -> bytes:
    return (
        bytes(supplied)
        if supplied is not None
        else Path(path).expanduser().resolve().read_bytes()
    )


def _json(payload: bytes, *, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must be valid UTF-8 JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{label} must be a JSON object")
    return parsed


def validate_score_input_availability_snapshot(
    path: Path,
    *,
    asof_date: str,
    expected_component_observation_ids: Iterable[str],
    signal_cutoff_at_utc: str,
    family: str,
    policy_id: str,
    attestation_path: Path,
    expected_attestation_sha256: str,
    bundle: CanonicalTrustBundle,
    snapshot_bytes: bytes | None = None,
    attestation_bytes: bytes | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Validate one independently signed exact component-availability census."""

    asof = _exact_date(asof_date, label="score-input availability asof")
    cutoff = _utc(
        signal_cutoff_at_utc,
        label="score-input availability signal cutoff",
    )
    if cutoff.date().isoformat() != asof:
        raise ValueError("score-input availability cutoff date differs from asof")
    expected_ids = list(expected_component_observation_ids)
    if (
        not expected_ids
        or len(expected_ids) != len(set(expected_ids))
        or any(
            type(observation_id) is not str
            or _strict_sha256(
                observation_id,
                label="expected component observation id",
            )
            != observation_id
            for observation_id in expected_ids
        )
    ):
        raise ValueError("expected component observation-id census is invalid")

    payload_bytes = _read_bytes(path, snapshot_bytes)
    payload = _json(payload_bytes, label="score-input availability snapshot")
    if set(payload) != AVAILABILITY_TOP_LEVEL_FIELDS:
        raise ValueError("score-input availability top-level census changed")
    if (
        payload.get("schema_version") != SCORE_INPUT_AVAILABILITY_SCHEMA
        or payload.get("evidence_role") != PROSPECTIVE_ROLE
        or payload.get("asof_date") != asof
    ):
        raise ValueError("score-input availability identity/asof changed")
    generated = _utc(
        payload.get("snapshot_generated_at_utc"),
        label="score-input availability generation time",
    )
    rows = payload.get("rows")
    if not isinstance(rows, list) or payload.get("rows_sha256") != canonical_sha256(
        rows
    ):
        raise ValueError("score-input availability rows are absent/hash-inconsistent")

    snapshot_sha = hashlib.sha256(payload_bytes).hexdigest()
    receipt_bytes = _read_bytes(attestation_path, attestation_bytes)
    receipt_sha = hashlib.sha256(receipt_bytes).hexdigest()
    if receipt_sha != _strict_sha256(
        expected_attestation_sha256,
        label="score-input availability attestation sha256",
    ):
        raise ValueError("score-input availability attestation SHA-256 mismatch")
    attestation = _json(
        receipt_bytes,
        label="score-input availability attestation",
    )
    if set(attestation) != AVAILABILITY_ATTESTATION_FIELDS:
        raise ValueError("score-input availability attestation census changed")
    bundle.market_data_export.verify_snapshot(
        receipt_bytes,
        receipt_sha,
        attestation,
    )
    provider_id = _nonblank(
        attestation.get("provider_id"),
        label="score-input availability provider id",
    )
    dataset_id = _nonblank(
        attestation.get("dataset_id"),
        label="score-input availability dataset id",
    )
    if (
        provider_id not in bundle.allowed_provider_ids
        or dataset_id not in bundle.allowed_dataset_ids
    ):
        raise ValueError(
            "score-input availability provider/dataset is outside trust allowlists"
        )
    exported = _utc(
        attestation.get("exported_at_utc"),
        label="score-input availability export time",
    )
    effective_through = _utc(
        attestation.get("status_effective_through_at_utc"),
        label="score-input availability effective-through time",
    )
    expected_attestation = {
        "schema_version": SCORE_INPUT_AVAILABILITY_ATTESTATION_SCHEMA,
        "family": family,
        "policy_id": policy_id,
        "asof_date": asof,
        "availability_snapshot_sha256": snapshot_sha,
        "availability_rows_sha256": payload["rows_sha256"],
        "component_count": len(expected_ids),
        "component_observation_ids_sha256": canonical_sha256(sorted(expected_ids)),
        "status_asof_policy": SCORE_INPUT_AVAILABILITY_POLICY,
    }
    for field, expected in expected_attestation.items():
        if attestation.get(field) != expected:
            raise ValueError(
                f"score-input availability attestation changed field: {field}"
            )
    if type(attestation.get("component_count")) is not int:
        raise ValueError("score-input availability component count is not canonical")
    source_required_count = attestation.get("source_required_count")
    if type(source_required_count) is not int or source_required_count < 0:
        raise ValueError(
            "score-input availability source-required count is not canonical"
        )
    _strict_sha256(
        attestation.get("query_sha256"),
        label="score-input availability query sha256",
    )

    index: dict[str, dict[str, Any]] = {}
    source_observation_ids: list[str] = []
    source_identity_by_observation_id: dict[str, dict[str, Any]] = {}
    component_source_mapping: list[dict[str, Any]] = []
    max_available: datetime | None = None
    required_count = 0
    for raw in rows:
        if not isinstance(raw, dict) or set(raw) != AVAILABILITY_ROW_FIELDS:
            raise ValueError("score-input availability row census changed")
        row = dict(raw)
        if row.get("asof_date") != asof:
            raise ValueError("score-input availability row asof changed")
        ticker = _nonblank(row.get("ticker"), label="availability ticker")
        if ticker.upper() != ticker:
            raise ValueError("score-input availability ticker is not canonical uppercase")
        _nonblank(row.get("component_name"), label=f"{ticker} component name")
        component_id = _strict_sha256(
            row.get("component_observation_id"),
            label=f"{ticker} component observation id",
        )
        if component_id in index:
            raise ValueError("score-input availability component id is duplicated")
        _nonblank(row.get("availability_status"), label=f"{ticker} availability")
        _nonblank(row.get("source_table"), label=f"{ticker} source table")
        _nonblank(row.get("source_field"), label=f"{ticker} source field")
        _strict_sha256(
            row.get("component_input_value_sha256"),
            label=f"{ticker} component input value sha256",
        )
        if (
            row.get("provider_id") != provider_id
            or row.get("dataset_id") != dataset_id
        ):
            raise ValueError(
                f"{ticker}: availability provider/dataset differs from attestation"
            )
        required = _strict_flag(
            row.get("source_required_flag"),
            label=f"{ticker} source-required flag",
        )
        if required:
            required_count += 1
            _nonblank(row.get("source_id"), label=f"{ticker} source id")
            source_asof = _exact_date(
                row.get("source_asof_date"),
                label=f"{ticker} source asof",
            )
            if source_asof > asof:
                raise ValueError(f"{ticker}: score-input source asof is post-cutoff")
            available = _utc(
                row.get("source_available_at_utc"),
                label=f"{ticker} source availability",
            )
            observation_id = _nonblank(
                row.get("source_observation_id"),
                label=f"{ticker} source observation id",
            )
            source_observation_ids.append(observation_id)
            source_locator = _nonblank(
                row.get("source_locator"), label=f"{ticker} source locator"
            )
            source_record_sha256 = _strict_sha256(
                row.get("source_record_sha256"),
                label=f"{ticker} source record sha256",
            )
            source_identity = {
                "ticker": ticker,
                "provider_id": provider_id,
                "dataset_id": dataset_id,
                "source_table": row["source_table"],
                "source_id": row["source_id"],
                "source_asof_date": source_asof,
                "source_available_at_utc": available.isoformat(),
                "source_locator": source_locator,
                "source_record_sha256": source_record_sha256,
            }
            prior_identity = source_identity_by_observation_id.setdefault(
                observation_id,
                source_identity,
            )
            if prior_identity != source_identity:
                raise ValueError(
                    f"{ticker}: reused source observation id has inconsistent "
                    "record identity"
                )
            if available > cutoff or available > generated:
                raise ValueError(
                    f"{ticker}: score input source was available after cutoff"
                )
            max_available = (
                available if max_available is None else max(max_available, available)
            )
        elif any(
            row.get(field) is not None
            for field in (
                "source_id",
                "source_asof_date",
                "source_available_at_utc",
                "source_observation_id",
                "source_locator",
                "source_record_sha256",
            )
        ):
            raise ValueError(
                f"{ticker}: no-source availability row has source observation fields"
            )
        if row["availability_status"] == "available" and required != 1:
            raise ValueError(f"{ticker}: available score input lacks source provenance")
        component_source_mapping.append(
            {
                "component_observation_id": component_id,
                "source_required_flag": required,
                "source_observation_id": row.get("source_observation_id"),
                "source_record_sha256": row.get("source_record_sha256"),
                "source_field": row["source_field"],
                "component_input_value_sha256": row[
                    "component_input_value_sha256"
                ],
            }
        )
        index[component_id] = row

    if set(index) != set(expected_ids) or len(index) != len(expected_ids):
        raise ValueError(
            "score-input availability is not the exact component observation census"
        )
    if required_count != source_required_count:
        raise ValueError("score-input availability source-required count differs")
    expected_source_ids_sha = canonical_sha256(sorted(source_observation_ids))
    if attestation.get("source_observation_ids_sha256") != expected_source_ids_sha:
        raise ValueError("score-input availability source-id census differs")
    attested_max_raw = attestation.get("source_max_information_at_utc")
    attested_max = (
        None
        if attested_max_raw is None
        else _utc(
            attested_max_raw,
            label="attested score-input max information time",
        )
    )
    if attested_max != max_available:
        raise ValueError("score-input availability max information time differs")
    if (
        effective_through != cutoff
        or not cutoff <= generated <= exported
        or (max_available is not None and max_available > cutoff)
    ):
        raise ValueError("score-input availability attestation chronology is invalid")
    return index, {
        "schema_version": SCORE_INPUT_AVAILABILITY_SCHEMA,
        "source_attestation_schema": (
            SCORE_INPUT_AVAILABILITY_ATTESTATION_SCHEMA
        ),
        "snapshot_sha256": snapshot_sha,
        "source_attestation_sha256": receipt_sha,
        "component_count": len(index),
        "component_observation_ids_sha256": canonical_sha256(sorted(index)),
        "source_required_count": required_count,
        "unique_source_observation_count": len(
            source_identity_by_observation_id
        ),
        "source_observation_ids_sha256": expected_source_ids_sha,
        "source_observation_identity_map_sha256": canonical_sha256(
            source_identity_by_observation_id
        ),
        "component_source_mapping_sha256": canonical_sha256(
            sorted(
                component_source_mapping,
                key=lambda row: str(row["component_observation_id"]),
            )
        ),
        "source_observation_reuse_policy": SOURCE_OBSERVATION_REUSE_POLICY,
        "rows_sha256": canonical_sha256(rows),
        "snapshot_generated_at_utc": generated.isoformat(),
        "source_attestation_exported_at_utc": exported.isoformat(),
        "signal_cutoff_at_utc": cutoff.isoformat(),
        "status_effective_through_at_utc": effective_through.isoformat(),
        "max_source_available_at_utc": (
            max_available.isoformat() if max_available is not None else None
        ),
        "source_provider_id": provider_id,
        "source_dataset_id": dataset_id,
        "exact_component_census_pass": True,
        "exact_official_close_cutoff_pass": True,
        "market_authority_attested_source_availability_pass": True,
        "consistent_shared_source_observation_identity_pass": True,
        "no_post_cutoff_score_inputs_pass": True,
        "production_activation_authorized": False,
    }


def validate_score_input_availability_capture_chronology(
    availability_audit: Mapping[str, Any],
    *,
    trusted_capture_timing: Mapping[str, Any],
    captured_at_utc: Any,
    label: str,
) -> dict[str, Any]:
    """Bind independently attested input availability to signed capture timing."""

    if not isinstance(availability_audit, Mapping) or not isinstance(
        trusted_capture_timing, Mapping
    ):
        raise ValueError(f"{label} availability/capture timing audit is invalid")
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
    generated = _utc(
        availability_audit.get("snapshot_generated_at_utc"),
        label=f"{label} availability snapshot generation time",
    )
    exported = _utc(
        availability_audit.get("source_attestation_exported_at_utc"),
        label=f"{label} availability attestation export time",
    )
    effective_through = _utc(
        availability_audit.get("status_effective_through_at_utc"),
        label=f"{label} availability effective-through time",
    )
    audit_cutoff = _utc(
        availability_audit.get("signal_cutoff_at_utc"),
        label=f"{label} availability audit signal cutoff",
    )
    max_available_raw = availability_audit.get("max_source_available_at_utc")
    max_available = (
        None
        if max_available_raw is None
        else _utc(
            max_available_raw,
            label=f"{label} availability max information time",
        )
    )
    if (
        availability_audit.get("exact_official_close_cutoff_pass") is not True
        or availability_audit.get("no_post_cutoff_score_inputs_pass") is not True
        or effective_through != cutoff
        or audit_cutoff != cutoff
        or captured != signed_captured
        or (max_available is not None and max_available > signed_max)
        or generated > signed_generated
        or exported > signed_generated
        or not cutoff <= generated <= exported <= signed_generated <= captured < entry
    ):
        raise ValueError(
            f"{label} score-input availability exceeds signed capture timing"
        )
    return {
        "exact_official_close_availability_pass": True,
        "availability_within_signed_source_envelope_pass": True,
        "availability_export_before_capture_pass": True,
        "capture_before_entry_pass": True,
    }


__all__ = [
    "AVAILABILITY_ATTESTATION_FIELDS",
    "AVAILABILITY_ROW_FIELDS",
    "AVAILABILITY_TOP_LEVEL_FIELDS",
    "SCORE_INPUT_AVAILABILITY_ATTESTATION_SCHEMA",
    "SCORE_INPUT_AVAILABILITY_POLICY",
    "SCORE_INPUT_AVAILABILITY_SCHEMA",
    "SOURCE_OBSERVATION_REUSE_POLICY",
    "score_input_availability_contract",
    "validate_score_input_availability_capture_chronology",
    "validate_score_input_availability_snapshot",
]
