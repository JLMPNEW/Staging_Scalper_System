from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

from .stage8_monthly_target_v3 import (
    validate_preregistered_monthly_plan_v3,
)


STAGE8_MONTHLY_PREREGISTRATION_V5 = (
    'consumer_defensive_stage8_monthly_preregistration_v5'
)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _resolved_expected_hash(
    *,
    name: str,
    expected_sha256: str | None,
    path: Path | None,
) -> tuple[str, str | None]:
    if (expected_sha256 is None) == (path is None):
        raise ValueError(
            f'Provide exactly one of expected {name} SHA-256 or file path.'
        )
    if path is not None:
        resolved = path.expanduser().resolve()
        if not resolved.is_file():
            raise ValueError(f'{name} artifact does not exist: {resolved}')
        return _file_sha256(resolved), str(resolved)
    return str(expected_sha256).lower(), None


def validate_preregistered_monthly_plan_v5(
    plan: Mapping[str, Any],
    *,
    expected_candidate_registry_sha256: str | None = None,
    expected_split_manifest_sha256: str | None = None,
    candidate_registry_path: Path | None = None,
    split_manifest_path: Path | None = None,
) -> dict[str, Any]:
    """Bind valid-looking plan hashes to the actual frozen input bytes."""

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
    checks = {
        'candidate_registry_bytes_bound': (
            str(plan['candidate_registry_sha256']).lower() == registry_hash
        ),
        'split_manifest_bytes_bound': (
            str(plan['split_manifest_sha256']).lower() == split_hash
        ),
    }
    failed = sorted(name for name, value in checks.items() if not value)
    if failed:
        raise ValueError(
            'Preregistered monthly plan does not match frozen bytes: '
            f'{failed}'
        )
    return {
        **validated,
        'schema_version': STAGE8_MONTHLY_PREREGISTRATION_V5,
        'candidate_registry_actual_sha256': registry_hash,
        'split_manifest_actual_sha256': split_hash,
        'candidate_registry_path': registry_path,
        'split_manifest_path': split_path,
        'artifact_byte_binding_pass_flag': 1,
    }


__all__ = ['validate_preregistered_monthly_plan_v5']
