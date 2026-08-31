from __future__ import annotations

import hashlib
import json

import pytest

from consumer_defensive.core.stage8_monthly_preregistration_v6 import (
    split_partitions_sha256,
    validate_preregistered_monthly_plan_v6,
)
from consumer_defensive.core.stage8_monthly_target_v3 import (
    MONTHLY_TARGET_FIELD,
    monthly_plan_sha256,
)


def _file_sha256(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _partitions() -> dict[str, list[str]]:
    return {
        'train_dates': ['2024-01-31', '2024-02-29'],
        'first_embargo_dates': ['2024-03-31'],
        'validation_dates': ['2024-04-30'],
        'second_embargo_dates': ['2024-05-31'],
        'holdout_dates': ['2024-06-30'],
    }


def _artifacts(tmp_path):
    registry = tmp_path / 'registry.json'
    split = tmp_path / 'split.json'
    ledger = tmp_path / 'target_access_ledger.json'
    registry.write_text('{"registry":"frozen"}\n', encoding='utf-8')
    partitions = _partitions()
    split.write_text(json.dumps(partitions) + '\n', encoding='utf-8')
    ledger.write_text(json.dumps({
        'schema_version': 'consumer_defensive_target_access_ledger_v1',
        'first_target_access_at_utc': '2024-01-02T00:00:00Z',
        'ledger_sealed_at_utc': '2024-01-02T00:05:00Z',
        'target_artifact_sha256': 'c' * 64,
    }) + '\n', encoding='utf-8')
    return registry, split, ledger


def _plan(registry, split) -> dict[str, object]:
    partitions = _partitions()
    plan: dict[str, object] = {
        'plan_id': 'future-monthly-byte-semantic-time-bound-v6',
        'candidate_registry_sha256': _file_sha256(registry),
        'split_manifest_sha256': _file_sha256(split),
        'split_partitions_sha256': split_partitions_sha256(partitions),
        'registered_at_utc': '2024-01-01T00:00:00Z',
        'target_field': MONTHLY_TARGET_FIELD,
        'scoring_frequency': 'monthly',
        'rebalance_frequency': 'monthly',
        'primary_objective': 'mean_rank_ic',
        'holdout_provenance': 'fresh_forward_oos',
        'registered_before_target_access': True,
        'holdout_sealed': True,
        'legacy_holdout_reuse_allowed': False,
        **partitions,
    }
    plan['plan_sha256'] = monthly_plan_sha256(plan)
    return plan


def _validate(plan, registry, split, ledger):
    return validate_preregistered_monthly_plan_v6(
        plan,
        candidate_registry_path=registry,
        split_manifest_path=split,
        target_access_ledger_path=ledger,
        expected_target_access_ledger_sha256=_file_sha256(ledger),
    )


def test_actual_bytes_partitions_and_access_chronology_pass(tmp_path) -> None:
    registry, split, ledger = _artifacts(tmp_path)
    result = _validate(_plan(registry, split), registry, split, ledger)
    assert result['artifact_byte_binding_pass_flag'] == 1
    assert result['split_semantic_binding_pass_flag'] == 1
    assert result['registration_chronology_pass_flag'] == 1


def test_same_split_file_hash_cannot_hide_tampered_plan_dates(tmp_path) -> None:
    registry, split, ledger = _artifacts(tmp_path)
    plan = _plan(registry, split)
    plan['holdout_dates'] = ['2024-07-31']
    plan['split_partitions_sha256'] = split_partitions_sha256(plan)
    plan['plan_sha256'] = monthly_plan_sha256(plan)
    with pytest.raises(ValueError, match='do not equal split manifest'):
        _validate(plan, registry, split, ledger)


def test_late_registration_fails_chronological_proof(tmp_path) -> None:
    registry, split, ledger = _artifacts(tmp_path)
    plan = _plan(registry, split)
    plan['registered_at_utc'] = '2024-01-03T00:00:00Z'
    plan['plan_sha256'] = monthly_plan_sha256(plan)
    with pytest.raises(ValueError, match='not strictly earlier'):
        _validate(plan, registry, split, ledger)


def test_hash_only_split_requires_semantic_partition_digest(tmp_path) -> None:
    registry, split, ledger = _artifacts(tmp_path)
    plan = _plan(registry, split)
    with pytest.raises(ValueError, match='expected partition digest'):
        validate_preregistered_monthly_plan_v6(
            plan,
            expected_candidate_registry_sha256=_file_sha256(registry),
            expected_split_manifest_sha256=_file_sha256(split),
            target_access_ledger_path=ledger,
            expected_target_access_ledger_sha256=_file_sha256(ledger),
        )
