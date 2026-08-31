from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

from consumer_defensive.core.stage7_historical_parity_v2 import (
    assess_provenance_binding,
    audit_stage7_artifact_seal,
    compare_reconstructed_scores,
)
from consumer_defensive.core.stage7_scoring import (
    OUTPUT_IDENTITY_FIELDS,
    score_observation_id,
)


def _sha256(value: object) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(',', ':'),
        ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _identity_row() -> dict[str, object]:
    row: dict[str, object] = {
        'ticker': 'AAA',
        'asof_date': '2026-08-14',
        'source_id': 'stage7-test-v1',
        'model_family': 'consumer_defensive',
        'model_version': 'stage7-test-v1',
        'baseline_source_id': 'baseline-test',
        'baseline_input_observation_id': 'b' * 64,
        'calibration_cohort_id': 'beverages',
        'core_score': 60.0,
        'final_score': 60.0,
        'final_rank': 1,
        'final_percentile': 50.0,
        'cohort_rank': 1,
        'cohort_percentile': 50.0,
        'component_weights_json': '{"signal":1.0}',
        'component_scores_json': '{"signal":60.0}',
        'component_quality_json': '{"signal":1.0}',
        'data_quality_confidence': 1.0,
        'full_data_quality_confidence': 1.0,
        'rank_ready_flag': 1,
        'calibration_eligible_flag': 1,
        'model_status': 'shadow_ready',
        'review_reason': None,
        'promotion_state': 'shadow_monitor',
        'portfolio_candidate_gate': 0,
        'oos_score_valid_flag': 0,
        'model_contract_sha256': 'c' * 64,
        'lineage_json': '{}',
    }
    assert set(row) == set(OUTPUT_IDENTITY_FIELDS)
    row['score_observation_id'] = score_observation_id(row)
    return row


def _write_stage7_artifact(root: Path) -> dict[str, object]:
    row = _identity_row()
    with (root / 'stage7_shadow_scores.csv').open(
        'w', encoding='utf-8', newline=''
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)
    output_manifest = _sha256([row['score_observation_id']])
    baseline_manifest = _sha256([row['baseline_input_observation_id']])
    manifest = {
        'build': {
            'status': 'PASS',
            'asof_date': row['asof_date'],
            'source_id': row['source_id'],
            'model_version': row['model_version'],
            'contract_sha256': row['model_contract_sha256'],
            'ticker_count': 1,
            'output_manifest_sha256': output_manifest,
            'baseline_input_manifest_sha256': baseline_manifest,
        },
        'validation': {
            'status': 'PASS',
            'source_id': row['source_id'],
            'contract_sha256': row['model_contract_sha256'],
            'checks': [{'check': 'synthetic', 'passed': True}],
        },
    }
    (root / 'stage7_build_manifest.json').write_text(
        json.dumps(manifest), encoding='utf-8'
    )
    return row


def test_stage7_artifact_seal_recomputes_row_and_manifests(tmp_path: Path) -> None:
    _write_stage7_artifact(tmp_path)
    audit = audit_stage7_artifact_seal(tmp_path)
    assert audit['stage7_output_identity_sealed']
    assert audit['ticker_count'] == 1
    assert audit['rank_ready_count'] == 1
    assert not audit['identity_error_tickers']


def test_stage7_artifact_seal_detects_semantic_csv_change(tmp_path: Path) -> None:
    row = _write_stage7_artifact(tmp_path)
    row['core_score'] = 61.0
    with (tmp_path / 'stage7_shadow_scores.csv').open(
        'w', encoding='utf-8', newline=''
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)
    audit = audit_stage7_artifact_seal(tmp_path)
    assert not audit['stage7_output_identity_sealed']
    assert audit['identity_error_tickers'] == ['AAA']


def test_current_asof_score_parity_is_field_exact() -> None:
    sealed = {
        'ticker': 'AAA',
        'component_weights_json': '{"a":1.0}',
        'component_scores_json': '{"a":60.0}',
        'component_quality_json': '{"a":1.0}',
        'core_score': 60.0,
        'data_quality_confidence': 1.0,
        'rank_ready_flag': 1,
        'calibration_eligible_flag': 1,
    }
    reconstructed = dict(sealed)
    result = compare_reconstructed_scores([sealed], [reconstructed])
    assert result['current_asof_score_arithmetic_parity']
    reconstructed['component_scores_json'] = '{"a":59.0}'
    result = compare_reconstructed_scores([sealed], [reconstructed])
    assert not result['current_asof_score_arithmetic_parity']
    assert result['component_score_errors'] == ['AAA']


def test_legacy_missing_manifests_stays_fail_closed_but_future_is_capable() -> None:
    stage7 = {
        'stage7_output_identity_sealed': True,
        'contract_sha256': 'contract',
        'source_id': 'source',
        'output_manifest_sha256': 'output',
    }
    decision = {
        'stage7_baseline': {'output_manifest_sha256': 'output'},
        'panel_summary': {'frozen_price_selection_sha256': 'prices'},
    }
    contract: dict[str, object] = {
        'stage7_contract_sha256': 'contract',
        'stage7_source_id': 'source',
    }
    reconstruction = {
        'frozen_price_selection_sha256': 'prices',
        'price_bar_manifest_sha256': 'bars',
        'component_source_manifest_sha256': 'sources',
        'score_panel_manifest_sha256': 'scores',
    }
    parity = {'current_asof_score_arithmetic_parity': True}
    methodology = {'methodology_files_exact': True}
    legacy = assess_provenance_binding(
        stage8_contract=contract,
        stage8_decision=decision,
        stage7_seal=stage7,
        methodology_identity=methodology,
        reconstruction=reconstruction,
        score_parity=parity,
    )
    assert legacy['fresh_future_source_identity_tie_capable']
    assert not legacy['historical_provenance_bound']
    assert not legacy['source_identity_tied']

    contract['provenance_manifests'] = legacy['observed_provenance_manifests']
    fresh = assess_provenance_binding(
        stage8_contract=contract,
        stage8_decision=decision,
        stage7_seal=stage7,
        methodology_identity=methodology,
        reconstruction=reconstruction,
        score_parity=parity,
    )
    assert fresh['historical_provenance_bound']
    assert fresh['source_identity_tied']
