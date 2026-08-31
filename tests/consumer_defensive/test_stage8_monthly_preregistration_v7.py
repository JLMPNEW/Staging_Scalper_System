from __future__ import annotations

import hashlib
import json

import pytest

from consumer_defensive.core.stage8_monthly_preregistration_v6 import (
    split_partitions_sha256,
)
from consumer_defensive.core.stage8_monthly_preregistration_v7 import (
    validate_preregistered_monthly_plan_v7,
)
from consumer_defensive.core.stage8_monthly_target_v3 import (
    MONTHLY_TARGET_FIELD,
    monthly_plan_sha256,
)


def _hash(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _trusted(_path, _sha256, payload) -> bool:
    return payload.get('trusted_test_authority') is not False


def _artifacts(tmp_path):
    registry = tmp_path / 'registry.json'
    split = tmp_path / 'split.json'
    target = tmp_path / 'target.csv'
    ledger = tmp_path / 'ledger.json'
    anchor = tmp_path / 'anchor.json'
    registry.write_text('{"registry":"frozen"}\n', encoding='utf-8')
    partitions = {
        'train_dates': ['2024-01-31', '2024-02-29'],
        'first_embargo_dates': ['2024-03-31'],
        'validation_dates': ['2024-04-30'],
        'second_embargo_dates': ['2024-05-31'],
        'holdout_dates': ['2024-06-30'],
    }
    split.write_text(json.dumps(partitions) + '\n', encoding='utf-8')
    target.write_text('date,ticker,target\n2024-01-31,A,0.1\n', encoding='utf-8')
    plan: dict[str, object] = {
        'plan_id': 'trusted-future-monthly-v7',
        'candidate_registry_sha256': _hash(registry),
        'split_manifest_sha256': _hash(split),
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
    ledger.write_text(json.dumps({
        'schema_version': 'consumer_defensive_target_access_ledger_v1',
        'first_target_access_at_utc': '2024-01-02T00:00:00Z',
        'ledger_sealed_at_utc': '2024-01-02T00:05:00Z',
        'target_artifact_sha256': _hash(target),
    }) + '\n', encoding='utf-8')
    anchor.write_text(json.dumps({
        'schema_version': 'consumer_defensive_registration_anchor_v1',
        'plan_sha256': plan['plan_sha256'],
        'registered_at_utc': plan['registered_at_utc'],
        'anchor_created_at_utc': '2024-01-01T00:01:00Z',
        'registration_authority': 'trusted-test-anchor',
        'anchor_id': 'anchor-001',
    }) + '\n', encoding='utf-8')
    return plan, registry, split, target, ledger, anchor


def _validate(
    plan, registry, split, target, ledger, anchor, *, trusted=True
):
    verifier = _trusted if trusted else None
    return validate_preregistered_monthly_plan_v7(
        plan,
        candidate_registry_path=registry,
        split_manifest_path=split,
        target_artifact_path=target,
        target_access_ledger_path=ledger,
        expected_target_access_ledger_sha256=_hash(ledger),
        target_access_ledger_verifier=verifier,
        registration_anchor_path=anchor,
        expected_registration_anchor_sha256=_hash(anchor),
        registration_anchor_verifier=verifier,
    )


def test_trusted_anchor_ledger_and_actual_target_bytes_pass(tmp_path) -> None:
    values = _artifacts(tmp_path)
    result = _validate(*values)
    assert result['target_bytes_binding_pass_flag'] == 1
    assert result['target_access_ledger_trust_pass_flag'] == 1
    assert result['registration_anchor_trust_pass_flag'] == 1
    assert result['registration_chronology_pass_flag'] == 1


def test_self_authored_runtime_hashes_without_trust_verifier_fail(tmp_path) -> None:
    values = _artifacts(tmp_path)
    with pytest.raises(ValueError, match='trusted artifact verifier'):
        _validate(*values, trusted=False)


def test_tampered_target_bytes_fail_even_with_unchanged_ledger(tmp_path) -> None:
    values = _artifacts(tmp_path)
    values[3].write_text('tampered\n', encoding='utf-8')
    with pytest.raises(ValueError, match='actual target bytes'):
        _validate(*values)


def test_anchor_created_after_first_access_fails(tmp_path) -> None:
    plan, registry, split, target, ledger, anchor = _artifacts(tmp_path)
    payload = json.loads(anchor.read_text(encoding='utf-8'))
    payload['anchor_created_at_utc'] = '2024-01-03T00:00:00Z'
    anchor.write_text(json.dumps(payload) + '\n', encoding='utf-8')
    with pytest.raises(ValueError, match='not established before'):
        _validate(plan, registry, split, target, ledger, anchor)
