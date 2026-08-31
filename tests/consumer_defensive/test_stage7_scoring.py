from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from consumer_defensive.core.config import ConfigBundle
from consumer_defensive.core.scoring_features import (
    build_scoring_features,
)
from consumer_defensive.core.specialized_metrics import (
    apply_stage6b_measurement_overlays,
)
from consumer_defensive.core.stage6b_schema import ensure_stage6b_schema
from consumer_defensive.core.stage7_schema import (
    STAGE7_MIGRATION_SHA256,
    ensure_stage7_schema,
)
from consumer_defensive.core.stage7_scoring import (
    _baseline_inputs,
    _components,
    _verify_atomic_inputs,
    build_stage7_scores,
    stage7_component_weights,
    validate_stage7_scores,
)
from tests.consumer_defensive.test_stage6a_scoring_features import _prepared


def _stage7_bundle(bundle: ConfigBundle) -> ConfigBundle:
    payload = copy.deepcopy(bundle.payload)
    payload['stage7_scoring']['minimum_rank_ready_fraction'] = 0.8
    return ConfigBundle(bundle.path, bundle.base_dir, payload)


def _mark_stage6b_overlay(conn) -> None:
    ensure_stage6b_schema(conn)
    with conn:
        conn.execute(
            '''INSERT INTO stage6b_specialized_run(
                   asof_date,parser_run_id,adapter_version,policy_sha256,
                   source_manifest_sha256,seal_manifest_sha256,
                   ingestion_config_sha256,issuer_scope_sha256,started_at,
                   completed_at,status,inventory_document_count,
                   accepted_observation_count,metadata_json
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (
                '2024-12-31', None, 'fixture', 'a' * 64, 'b' * 64,
                'c' * 64, 'd' * 64, 'e' * 64,
                '2025-01-01T00:00:00Z', '2025-01-01T00:00:00Z',
                'measurement_only_complete', 0, 0,
                json.dumps({'observation_sha256s': []}),
            )
        )
    result = apply_stage6b_measurement_overlays(
        conn, as_of='2024-12-31'
    )
    assert result['stage6b_run_id'] == 1


def _ready(tmp_path: Path, *, missing_borrow: bool = False):
    bundle, conn = _prepared(tmp_path)
    bundle = _stage7_bundle(bundle)
    if missing_borrow:
        with conn:
            conn.execute(
                "UPDATE feature_positioning SET borrow_fee=NULL WHERE ticker='T5'"
            )
    build_scoring_features(conn, bundle, as_of='2024-12-31')
    _mark_stage6b_overlay(conn)
    return bundle, conn


def test_stage7_schema_is_checksummed_and_idempotent(tmp_path: Path) -> None:
    bundle, conn = _ready(tmp_path)
    try:
        ensure_stage7_schema(conn)
        ensure_stage7_schema(conn)
        row = conn.execute(
            '''SELECT migration_version,migration_sha256
               FROM stage7_schema_migrations'''
        ).fetchone()
        assert tuple(row) == (1, STAGE7_MIGRATION_SHA256)
        columns = {
            str(item[1])
            for item in conn.execute(
                'PRAGMA table_info(feature_scoring_model_output)'
            )
        }
        assert {
            'baseline_input_observation_id',
            'calibration_cohort_id',
            'cohort_rank',
            'model_contract_sha256',
            'score_observation_id',
        }.issubset(columns)
        assert stage7_component_weights(bundle)
    finally:
        conn.close()


def test_stage7_build_is_shadow_only_and_deterministic(tmp_path: Path) -> None:
    bundle, conn = _ready(tmp_path)
    try:
        first = build_stage7_scores(conn, bundle, as_of='2024-12-31')
        before = [
            tuple(row)
            for row in conn.execute(
                '''SELECT ticker,final_score,final_rank,cohort_rank,
                          score_observation_id,lineage_json
                   FROM feature_scoring_model_output
                   WHERE source_id='consumer_defensive_stage7_baseline_v4'
                   ORDER BY ticker'''
            )
        ]
        second = build_stage7_scores(conn, bundle, as_of='2024-12-31')
        validation = validate_stage7_scores(
            conn, bundle, as_of='2024-12-31'
        )
        after = [
            tuple(row)
            for row in conn.execute(
                '''SELECT ticker,final_score,final_rank,cohort_rank,
                          score_observation_id,lineage_json
                   FROM feature_scoring_model_output
                   WHERE source_id='consumer_defensive_stage7_baseline_v4'
                   ORDER BY ticker'''
            )
        ]
        assert first == second
        assert before == after
        assert first['ticker_count'] == 6
        assert first['rank_ready_count'] == 5
        assert first['specialized_nonzero_weight_count'] == 0
        assert validation['status'] == 'PASS'
        assert validation['summary']['passed_checks'] == validation['summary']['total_checks']
        assert conn.execute(
            '''SELECT COUNT(*) FROM feature_scoring_model_output
               WHERE portfolio_candidate_gate<>0 OR oos_score_valid_flag<>0
                  OR promotion_state<>'shadow_monitor' '''
        ).fetchone()[0] == 0
        assert conn.execute(
            '''SELECT COUNT(*) FROM stage7_component_weight_contract
               WHERE component_group='specialized' AND component_weight<>0'''
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_missing_component_is_neutral_without_weight_redistribution(
    tmp_path: Path,
) -> None:
    bundle, conn = _ready(tmp_path, missing_borrow=True)
    try:
        build_stage7_scores(conn, bundle, as_of='2024-12-31')
        row = conn.execute(
            '''SELECT * FROM feature_scoring_model_output
               WHERE source_id='consumer_defensive_stage7_baseline_v4'
                 AND ticker='T5' '''
        ).fetchone()
        scores = json.loads(str(row['component_scores_json']))
        weights = json.loads(str(row['component_weights_json']))
        assert scores['borrow_fee'] == pytest.approx(50.0)
        assert row['data_quality_confidence'] == pytest.approx(0.97)
        expected_score = sum(
            float(weights[name]) * float(scores[name]) for name in weights
        )
        assert row['final_score'] == pytest.approx(expected_score)
        assert row['rank_ready_flag'] == 1
        assert 'borrow_fee' in json.loads(str(row['lineage_json']))[
            'missing_components'
        ]
    finally:
        conn.close()


def test_unknown_or_renormalized_weights_fail_closed(tmp_path: Path) -> None:
    bundle, conn = _ready(tmp_path)
    try:
        payload = copy.deepcopy(bundle.payload)
        payload['stage7_scoring']['component_weights']['unknown_signal'] = 0.0
        unknown = ConfigBundle(bundle.path, bundle.base_dir, payload)
        with pytest.raises(ValueError, match='must contain exactly'):
            stage7_component_weights(unknown)

        payload = copy.deepcopy(bundle.payload)
        payload['stage7_scoring']['component_weights']['gross_margin'] = 0.09
        wrong_sum = ConfigBundle(bundle.path, bundle.base_dir, payload)
        with pytest.raises(ValueError, match='sum exactly to 1.0'):
            stage7_component_weights(wrong_sum)
    finally:
        conn.close()


def test_missing_stage6b_overlay_blocks_all_stage7_outputs(tmp_path: Path) -> None:
    bundle, conn = _prepared(tmp_path)
    bundle = _stage7_bundle(bundle)
    try:
        build_scoring_features(conn, bundle, as_of='2024-12-31')
        with pytest.raises(RuntimeError, match='requires the accepted Stage 6B'):
            build_stage7_scores(conn, bundle, as_of='2024-12-31')
        assert conn.execute(
            '''SELECT COUNT(*) FROM feature_scoring_model_output
               WHERE source_id='consumer_defensive_stage7_baseline_v4' '''
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_core_only_production_contract_can_skip_stage6b_overlay(
    tmp_path: Path,
) -> None:
    bundle, conn = _prepared(tmp_path)
    bundle = _stage7_bundle(bundle)
    try:
        build_scoring_features(conn, bundle, as_of='2024-12-31')
        inputs = _baseline_inputs(
            conn,
            as_of='2024-12-31',
            baseline_source_id=str(
                bundle.payload['stage7_scoring']['baseline_source_id']
            ),
        )
        components = _components(conn, as_of='2024-12-31')
        _verify_atomic_inputs(
            conn,
            bundle,
            as_of='2024-12-31',
            inputs=inputs,
            components=components,
            require_stage6b_overlay=False,
        )
        with pytest.raises(RuntimeError, match='requires the accepted Stage 6B'):
            _verify_atomic_inputs(
                conn,
                bundle,
                as_of='2024-12-31',
                inputs=inputs,
                components=components,
            )
    finally:
        conn.close()


def test_output_and_component_tampering_fail_validation(tmp_path: Path) -> None:
    bundle, conn = _ready(tmp_path)
    try:
        build_stage7_scores(conn, bundle, as_of='2024-12-31')
        with conn:
            conn.execute(
                '''UPDATE feature_scoring_model_output SET final_score=99.0
                   WHERE source_id='consumer_defensive_stage7_baseline_v4'
                     AND ticker='T1' '''
            )
        result = validate_stage7_scores(conn, bundle, as_of='2024-12-31')
        assert result['status'] == 'FAIL'
        failed = {row['check'] for row in result['checks'] if not row['passed']}
        assert 'score_rows_exact_and_deterministic' in failed
        assert 'weighted_score_arithmetic_exact' in failed

        with conn:
            conn.execute(
                '''UPDATE feature_scoring_component SET component_score=99.0
                   WHERE ticker='T2'
                     AND component_name='residual_momentum_63d' '''
            )
        result = validate_stage7_scores(conn, bundle, as_of='2024-12-31')
        assert result['status'] == 'FAIL'
        failed = {row['check'] for row in result['checks'] if not row['passed']}
        assert 'atomic_inputs_and_stage6b_overlay_exact' in failed
    finally:
        conn.close()
