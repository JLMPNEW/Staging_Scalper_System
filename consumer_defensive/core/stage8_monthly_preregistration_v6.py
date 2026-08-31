"""Byte-, split-, and chronology-bound monthly preregistration validation."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .stage8_monthly_target_v3 import (
    validate_preregistered_monthly_plan_v3,
)


STAGE8_MONTHLY_PREREGISTRATION_V6 = (
    'consumer_defensive_stage8_monthly_preregistration_v6'
)
_PARTITION_NAMES = (
    'train_dates', 'first_embargo_dates', 'validation_dates',
    'second_embargo_dates', 'holdout_dates',
)
_LEDGER_FIELDS = {
    'schema_version', 'first_target_access_at_utc',
    'ledger_sealed_at_utc', 'target_artifact_sha256',
}


def _is_sha256(value: Any) -> bool:
    text = str(value).lower()
    return len(text) == 64 and all(char in '0123456789abcdef' for char in text)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: Any) -> str:
    canonical = json.dumps(
        value,
        sort_keys=True,
        separators=(',', ':'),
        ensure_ascii=True,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def split_partitions_sha256(partitions: Mapping[str, Any]) -> str:
    normalized: dict[str, list[str]] = {}
    for name in _PARTITION_NAMES:
        raw = partitions.get(name)
        if not isinstance(raw, list):
            raise ValueError(f'Split partition is missing or not a list: {name}')
        values = [str(value) for value in raw]
        if values != sorted(set(values)):
            raise ValueError(f'Split partition is not sorted/unique: {name}')
        normalized[name] = values
    return _canonical_sha256(normalized)


def _resolved_expected_hash(
    *,
    name: str,
    expected_sha256: str | None,
    path: Path | None,
) -> tuple[str, Path | None]:
    if (expected_sha256 is None) == (path is None):
        raise ValueError(
            f'Provide exactly one of expected {name} SHA-256 or file path.'
        )
    if path is not None:
        resolved = path.expanduser().resolve()
        if not resolved.is_file():
            raise ValueError(f'{name} artifact does not exist: {resolved}')
        return _file_sha256(resolved), resolved
    if not _is_sha256(expected_sha256):
        raise ValueError(f'Expected {name} SHA-256 is not exact 64-hex.')
    return str(expected_sha256).lower(), None


def _utc_timestamp(value: Any, *, name: str) -> datetime:
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError(f'{name} must be an ISO-8601 timestamp.') from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError(f'{name} must be timezone-aware UTC.')
    return parsed


def _load_target_access_ledger(
    path: Path,
    *,
    expected_sha256: str,
) -> tuple[dict[str, Any], str]:
    resolved = path.expanduser().resolve()
    actual_hash = _file_sha256(resolved)
    if not _is_sha256(expected_sha256) or actual_hash != expected_sha256.lower():
        raise ValueError('Target-access ledger SHA-256 mismatch.')
    payload = json.loads(resolved.read_text(encoding='utf-8'))
    if not isinstance(payload, dict) or set(payload) != _LEDGER_FIELDS:
        raise ValueError('Target-access ledger does not match strict schema.')
    if payload['schema_version'] != 'consumer_defensive_target_access_ledger_v1':
        raise ValueError('Unsupported target-access ledger schema.')
    if not _is_sha256(payload['target_artifact_sha256']):
        raise ValueError('Target-access ledger target hash is not exact 64-hex.')
    return dict(payload), actual_hash


def validate_preregistered_monthly_plan_v6(
    plan: Mapping[str, Any],
    *,
    expected_candidate_registry_sha256: str | None = None,
    expected_split_manifest_sha256: str | None = None,
    candidate_registry_path: Path | None = None,
    split_manifest_path: Path | None = None,
    expected_split_partitions_sha256: str | None = None,
    target_access_ledger_path: Path,
    expected_target_access_ledger_sha256: str,
) -> dict[str, Any]:
    """Validate a fresh plan against actual inputs and access chronology."""

    unknown_partition_fields = sorted(
        key for key in plan
        if key.endswith('_dates') and key not in _PARTITION_NAMES
    )
    if unknown_partition_fields:
        raise ValueError(
            f'Unknown plan date partitions: {unknown_partition_fields}'
        )
    required = {'split_partitions_sha256', 'registered_at_utc'}
    missing = sorted(required - set(plan))
    if missing:
        raise ValueError(f'V6 monthly plan missing fields: {missing}')
    validated = validate_preregistered_monthly_plan_v3(plan)
    registry_hash, registry_path = _resolved_expected_hash(
        name='candidate registry',
        expected_sha256=expected_candidate_registry_sha256,
        path=candidate_registry_path,
    )
    split_hash, split_path = _resolved_expected_hash(
        name='split manifest',
        expected_sha256=expected_split_manifest_sha256,
        path=split_manifest_path,
    )
    if str(plan['candidate_registry_sha256']).lower() != registry_hash:
        raise ValueError('Candidate registry plan hash does not match bytes.')
    if str(plan['split_manifest_sha256']).lower() != split_hash:
        raise ValueError('Split manifest plan hash does not match bytes.')

    plan_partition_hash = split_partitions_sha256(plan)
    if split_path is not None:
        split_payload = json.loads(split_path.read_text(encoding='utf-8'))
        if not isinstance(split_payload, dict):
            raise ValueError('Split manifest must be a JSON object.')
        actual_partition_hash = split_partitions_sha256(split_payload)
        mismatches = [
            name for name in _PARTITION_NAMES
            if plan[name] != split_payload[name]
        ]
        if mismatches:
            raise ValueError(
                f'Plan partitions do not equal split manifest: {mismatches}'
            )
    else:
        if not _is_sha256(expected_split_partitions_sha256):
            raise ValueError(
                'Hash-only split binding requires exact expected partition digest.'
            )
        actual_partition_hash = str(
            expected_split_partitions_sha256
        ).lower()
    if (
        plan_partition_hash != actual_partition_hash
        or str(plan['split_partitions_sha256']).lower()
        != actual_partition_hash
    ):
        raise ValueError('Split partition semantic digest mismatch.')

    ledger, ledger_hash = _load_target_access_ledger(
        target_access_ledger_path,
        expected_sha256=expected_target_access_ledger_sha256,
    )
    registered = _utc_timestamp(
        plan['registered_at_utc'], name='registered_at_utc'
    )
    first_access = _utc_timestamp(
        ledger['first_target_access_at_utc'],
        name='first_target_access_at_utc',
    )
    sealed = _utc_timestamp(
        ledger['ledger_sealed_at_utc'], name='ledger_sealed_at_utc'
    )
    if not registered < first_access <= sealed:
        raise ValueError(
            'Registration is not strictly earlier than first target access '
            'or ledger seal chronology is invalid.'
        )
    return {
        **validated,
        'schema_version': STAGE8_MONTHLY_PREREGISTRATION_V6,
        'candidate_registry_actual_sha256': registry_hash,
        'split_manifest_actual_sha256': split_hash,
        'split_partitions_actual_sha256': actual_partition_hash,
        'candidate_registry_path': (
            str(registry_path) if registry_path is not None else None
        ),
        'split_manifest_path': (
            str(split_path) if split_path is not None else None
        ),
        'target_access_ledger_path': str(
            target_access_ledger_path.expanduser().resolve()
        ),
        'target_access_ledger_actual_sha256': ledger_hash,
        'first_target_access_at_utc': ledger['first_target_access_at_utc'],
        'artifact_byte_binding_pass_flag': 1,
        'split_semantic_binding_pass_flag': 1,
        'registration_chronology_pass_flag': 1,
    }


__all__ = [
    'split_partitions_sha256',
    'validate_preregistered_monthly_plan_v6',
]
