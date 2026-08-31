from __future__ import annotations

import hashlib

import pytest

from consumer_defensive.core.stage8_monthly_preregistration_v5 import (
    validate_preregistered_monthly_plan_v5,
)
from consumer_defensive.core.stage8_monthly_target_v3 import (
    MONTHLY_TARGET_FIELD,
    monthly_plan_sha256,
)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _plan(registry_hash: str, split_hash: str) -> dict[str, object]:
    plan: dict[str, object] = {
        'plan_id': 'future-monthly-byte-bound-v5',
        'candidate_registry_sha256': registry_hash,
        'split_manifest_sha256': split_hash,
        'target_field': MONTHLY_TARGET_FIELD,
        'scoring_frequency': 'monthly',
        'rebalance_frequency': 'monthly',
        'primary_objective': 'mean_rank_ic',
        'holdout_provenance': 'fresh_forward_oos',
        'registered_before_target_access': True,
        'holdout_sealed': True,
        'legacy_holdout_reuse_allowed': False,
        'train_dates': ['2024-01-31', '2024-02-29'],
        'first_embargo_dates': ['2024-03-31'],
        'validation_dates': ['2024-04-30'],
        'second_embargo_dates': ['2024-05-31'],
        'holdout_dates': ['2024-06-30'],
    }
    plan['plan_sha256'] = monthly_plan_sha256(plan)
    return plan


def test_valid_looking_arbitrary_hashes_fail_actual_byte_binding(tmp_path) -> None:
    registry = tmp_path / 'registry.json'
    split = tmp_path / 'split.json'
    registry.write_bytes(b'{"registry":"frozen"}\n')
    split.write_bytes(b'{"split":"frozen"}\n')
    plan = _plan('a' * 64, 'b' * 64)

    with pytest.raises(ValueError, match='does not match frozen bytes'):
        validate_preregistered_monthly_plan_v5(
            plan,
            candidate_registry_path=registry,
            split_manifest_path=split,
        )


def test_actual_frozen_bytes_or_exact_expected_hashes_pass(tmp_path) -> None:
    registry_bytes = b'{"registry":"frozen"}\n'
    split_bytes = b'{"split":"frozen"}\n'
    registry = tmp_path / 'registry.json'
    split = tmp_path / 'split.json'
    registry.write_bytes(registry_bytes)
    split.write_bytes(split_bytes)
    registry_hash = _sha256(registry_bytes)
    split_hash = _sha256(split_bytes)
    plan = _plan(registry_hash, split_hash)

    file_bound = validate_preregistered_monthly_plan_v5(
        plan,
        candidate_registry_path=registry,
        split_manifest_path=split,
    )
    assert file_bound['artifact_byte_binding_pass_flag'] == 1
    assert file_bound['candidate_registry_actual_sha256'] == registry_hash
    assert file_bound['split_manifest_actual_sha256'] == split_hash

    hash_bound = validate_preregistered_monthly_plan_v5(
        plan,
        expected_candidate_registry_sha256=registry_hash,
        expected_split_manifest_sha256=split_hash,
    )
    assert hash_bound['artifact_byte_binding_pass_flag'] == 1


def test_exactly_one_binding_source_per_artifact_is_required(tmp_path) -> None:
    registry = tmp_path / 'registry.json'
    registry.write_bytes(b'registry')
    plan = _plan(_sha256(b'registry'), _sha256(b'split'))
    with pytest.raises(ValueError, match='exactly one'):
        validate_preregistered_monthly_plan_v5(
            plan,
            expected_candidate_registry_sha256=_sha256(b'registry'),
            candidate_registry_path=registry,
            expected_split_manifest_sha256=_sha256(b'split'),
        )
