"""Trusted preregistration proof layered over V6 structural validation.

V6 validates internal chronology but local timestamps and runtime-supplied
hashes are only claims.  V7 additionally verifies the actual target bytes and
requires independent verifiers for both a pre-access registration anchor and
the target-access ledger.  Without those trust hooks it fails closed.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Mapping

from .stage8_monthly_preregistration_v6 import (
    validate_preregistered_monthly_plan_v6,
)


STAGE8_MONTHLY_PREREGISTRATION_V7 = (
    'consumer_defensive_stage8_monthly_preregistration_v7'
)
TrustedArtifactVerifier = Callable[
    [Path, str, Mapping[str, Any]], bool
]
_ANCHOR_FIELDS = {
    'schema_version', 'plan_sha256', 'registered_at_utc',
    'anchor_created_at_utc', 'registration_authority', 'anchor_id',
}


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _exact_hash(value: Any) -> str:
    text = str(value).lower()
    if len(text) != 64 or any(char not in '0123456789abcdef' for char in text):
        raise ValueError('Expected SHA-256 must be exact 64-hex.')
    return text


def _utc(value: Any, *, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(
            str(value).replace('Z', '+00:00')
        )
    except ValueError as exc:
        raise ValueError(f'{name} must be ISO-8601.') from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError(f'{name} must be timezone-aware UTC.')
    return parsed


def _trusted_json(
    *,
    name: str,
    path: Path,
    expected_sha256: str,
    verifier: TrustedArtifactVerifier | None,
) -> tuple[Path, dict[str, Any], str]:
    if verifier is None:
        raise ValueError(
            f'{name} requires an independent trusted artifact verifier.'
        )
    resolved = path.expanduser().resolve()
    actual_hash = _file_sha256(resolved)
    if actual_hash != _exact_hash(expected_sha256):
        raise ValueError(f'{name} SHA-256 mismatch.')
    payload = json.loads(resolved.read_text(encoding='utf-8'))
    if not isinstance(payload, dict):
        raise ValueError(f'{name} must be a JSON object.')
    if verifier(resolved, actual_hash, payload) is not True:
        raise ValueError(f'{name} failed independent trust verification.')
    return resolved, dict(payload), actual_hash


def validate_preregistered_monthly_plan_v7(
    plan: Mapping[str, Any],
    *,
    expected_candidate_registry_sha256: str | None = None,
    expected_split_manifest_sha256: str | None = None,
    candidate_registry_path: Path | None = None,
    split_manifest_path: Path | None = None,
    expected_split_partitions_sha256: str | None = None,
    target_artifact_path: Path,
    target_access_ledger_path: Path,
    expected_target_access_ledger_sha256: str,
    target_access_ledger_verifier: TrustedArtifactVerifier | None,
    registration_anchor_path: Path,
    expected_registration_anchor_sha256: str,
    registration_anchor_verifier: TrustedArtifactVerifier | None,
) -> dict[str, Any]:
    """Return trusted preregistration proof or reject the plan."""

    ledger_path, ledger, ledger_hash = _trusted_json(
        name='target-access ledger',
        path=target_access_ledger_path,
        expected_sha256=expected_target_access_ledger_sha256,
        verifier=target_access_ledger_verifier,
    )
    anchor_path, anchor, anchor_hash = _trusted_json(
        name='registration anchor',
        path=registration_anchor_path,
        expected_sha256=expected_registration_anchor_sha256,
        verifier=registration_anchor_verifier,
    )
    if set(anchor) != _ANCHOR_FIELDS:
        raise ValueError('Registration anchor does not match strict schema.')
    if anchor['schema_version'] != 'consumer_defensive_registration_anchor_v1':
        raise ValueError('Unsupported registration anchor schema.')
    if (
        str(anchor['plan_sha256']).lower()
        != str(plan.get('plan_sha256') or '').lower()
        or str(anchor['registered_at_utc'])
        != str(plan.get('registered_at_utc') or '')
        or not str(anchor['registration_authority']).strip()
        or not str(anchor['anchor_id']).strip()
    ):
        raise ValueError('Registration anchor does not bind this exact plan.')

    target_path = target_artifact_path.expanduser().resolve()
    target_hash = _file_sha256(target_path)
    if target_hash != str(ledger.get('target_artifact_sha256') or '').lower():
        raise ValueError(
            'Target-access ledger does not bind the actual target bytes.'
        )
    registered = _utc(plan['registered_at_utc'], name='registered_at_utc')
    anchor_created = _utc(
        anchor['anchor_created_at_utc'], name='anchor_created_at_utc'
    )
    first_access = _utc(
        ledger.get('first_target_access_at_utc'),
        name='first_target_access_at_utc',
    )
    if not registered <= anchor_created < first_access:
        raise ValueError(
            'Trusted registration anchor was not established before target access.'
        )

    structural = validate_preregistered_monthly_plan_v6(
        plan,
        expected_candidate_registry_sha256=(
            expected_candidate_registry_sha256
        ),
        expected_split_manifest_sha256=expected_split_manifest_sha256,
        candidate_registry_path=candidate_registry_path,
        split_manifest_path=split_manifest_path,
        expected_split_partitions_sha256=(
            expected_split_partitions_sha256
        ),
        target_access_ledger_path=ledger_path,
        expected_target_access_ledger_sha256=ledger_hash,
    )
    return {
        **structural,
        'schema_version': STAGE8_MONTHLY_PREREGISTRATION_V7,
        'target_artifact_path': str(target_path),
        'target_artifact_actual_sha256': target_hash,
        'registration_anchor_path': str(anchor_path),
        'registration_anchor_actual_sha256': anchor_hash,
        'structural_chronology_claim_pass_flag': 1,
        'target_bytes_binding_pass_flag': 1,
        'target_access_ledger_trust_pass_flag': 1,
        'registration_anchor_trust_pass_flag': 1,
        'registration_chronology_pass_flag': 1,
    }


__all__ = ['validate_preregistered_monthly_plan_v7']
