from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from consumer_defensive.core.config import ConfigBundle, load_config
from consumer_defensive.core.db import connect, init_db, utc_now
from consumer_defensive.core.metric_registry import load_metric_registry
from consumer_defensive.core.scoring_features import (
    CORE_COMPONENT_SPECS,
    bootstrap_stage6a,
    build_scoring_features,
    validate_scoring_features,
)
from consumer_defensive.core.source_registry import (
    load_source_registry,
    upsert_source_registry,
)
from consumer_defensive.core.stage6a_schema import (
    STAGE6A_MIGRATION_SHA256,
    ensure_stage6a_schema,
)


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / 'consumer_defensive' / 'config.yaml'
SOURCES = ROOT / 'consumer_defensive' / 'data' / 'free_source_registry.yaml'


def _ticker_set_sha256(tickers: list[str]) -> str:
    encoded = json.dumps(
        sorted(tickers),
        sort_keys=True,
        separators=(',', ':'),
        ensure_ascii=True,
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _bundle() -> ConfigBundle:
    source = load_config(CONFIG)
    payload = copy.deepcopy(source.payload)
    payload['universe']['expected_current_rows'] = 6
    payload['calibration_scope']['expected_excluded_ticker_count'] = 0
    payload['calibration_scope']['expected_remaining_current_ticker_count'] = 6
    payload['calibration_scope'][
        'expected_remaining_current_tickers_sha256'
    ] = _ticker_set_sha256([f'T{i}' for i in range(1, 7)])
    payload['calibration_scope']['expected_remaining_current_by_cohort'] = {
        'beverages': 0,
        'consumer_staples_distribution_retail': 0,
        'household_personal_tobacco': 0,
        'packaged_foods_agricultural_products': 6,
    }
    payload['calibration_scope']['excluded_tickers_by_cohort'] = {
        'beverages': [],
        'consumer_staples_distribution_retail': [],
        'household_personal_tobacco': [],
        'packaged_foods_agricultural_products': [],
    }
    payload['scoring_features']['minimum_normalization_peer_count'] = 5
    payload['scoring_features']['minimum_rank_ready_fraction'] = 0.8
    return ConfigBundle(source.path, source.base_dir, payload)


def _prepared(tmp_path: Path):
    bundle = _bundle()
    conn = connect(tmp_path / 'stage6a.sqlite')
    init_db(conn)
    upsert_source_registry(conn, load_source_registry(SOURCES))
    bootstrap_stage6a(conn, bundle)
    now = utc_now()
    tickers = [f'T{i}' for i in range(1, 7)]
    with conn:
        for index, ticker in enumerate(tickers, start=1):
            company_id = conn.execute(
                '''
                INSERT INTO dim_company(
                    primary_ticker,cik,company_name,universe_status,is_active,
                    first_seen_at,updated_at
                ) VALUES (?,?,?,'current',1,?,?)
                ''',
                (ticker, str(1000 + index), f'Company {index}', now, now),
            ).lastrowid
            security_id = conn.execute(
                '''
                INSERT INTO dim_security(
                    company_id,ticker,provider_price_symbol,exchange,listing_status,
                    is_primary_listing,currency,listing_start_date,created_at,updated_at
                ) VALUES (?,?,?,'NYSE','active',1,'USD','2010-01-01',?,?)
                ''',
                (company_id, ticker, ticker, now, now),
            ).lastrowid
            conn.execute(
                '''
                INSERT INTO dim_consumer_defensive_taxonomy(
                    company_id,security_id,ticker,calibration_cohort_id,
                    calibration_cohort,applicability_subtype,taxonomy_confidence,
                    analyst_reviewed,updated_at
                ) VALUES (?,?,?,'packaged_foods_agricultural_products',
                          'Packaged Foods','branded_food',1,1,?)
                ''',
                (company_id, security_id, ticker, now),
            )
            conn.execute(
                '''
                INSERT INTO dim_universe_membership(
                    company_id,security_id,ticker,membership_basis,start_date,
                    membership_status,is_current_member,point_in_time_flag,
                    live_investable_flag,historical_calibration_eligible_flag,
                    created_at,updated_at
                ) VALUES (?,?,?,'fixture','2019-01-02','active',1,1,1,1,?,?)
                ''',
                (company_id, security_id, ticker, now, now),
            )
            value = float(index)
            conn.execute(
                '''
                INSERT INTO feature_market_technical(
                    ticker,asof_date,source_id,adjusted_close,avg_dollar_volume_63d,
                    residual_momentum_63d,residual_momentum_126d,
                    realized_volatility_63d,downside_volatility_63d,
                    max_drawdown_252d,history_days,quality_status,created_at
                ) VALUES (?,'2024-12-31','yahoo_finance_adjusted',?,?,?,?,?,?,?,?, 'full',?)
                ''',
                (
                    ticker, 100 + value, 1_000_000 * value, value / 100,
                    value / 80, value / 50, value / 55, -value / 20, 500, now,
                ),
            )
            financial_status = 'stale' if ticker == 'T6' else 'partial'
            conn.execute(
                '''
                INSERT INTO feature_financial_statement(
                    ticker,asof_date,source_id,revenue_ttm_usd,gross_margin,
                    operating_margin,free_cash_flow_margin,
                    return_on_invested_capital,net_debt_to_ebitda,
                    inventory_turnover,basis_period_end,lineage_json,
                    financial_quality_status,financial_quality_reason,created_at
                ) VALUES (?,'2024-12-31','sec_companyfacts',?,?,?,?,?,?,?,
                          '2024-09-30','{}',?,NULL,?)
                ''',
                (
                    ticker, value * 1_000_000, value / 10, value / 20,
                    value / 30, value / 25, 7 - value / 10,
                    value, financial_status, now,
                ),
            )
            conn.execute(
                '''
                INSERT INTO feature_positioning(
                    ticker,asof_date,source_id,insider_net_buying,
                    institutional_flow,short_float_pct,short_days_to_cover,
                    borrow_fee,source_birthdate,quality_status,quality_reason,
                    lineage_json,definition_version,created_at
                ) VALUES (?,'2024-12-31','consumer_defensive_positioning_composite',
                          ?,?,?,?,?, '2019-01-02','complete',NULL,'{}',
                          'consumer_defensive_positioning_v2',?)
                ''',
                (
                    ticker, value * 1000, value / 100, value / 200,
                    value / 2, value / 1000, now,
                ),
            )
    return bundle, conn


def test_stage6a_schema_is_checksummed_and_idempotent(tmp_path: Path) -> None:
    bundle, conn = _prepared(tmp_path)
    try:
        ensure_stage6a_schema(conn)
        ensure_stage6a_schema(conn)
        row = conn.execute(
            '''
            SELECT migration_version,migration_sha256
            FROM stage6a_schema_migrations
            '''
        ).fetchone()
        assert tuple(row) == (1, STAGE6A_MIGRATION_SHA256)
        assert {
            'contract_sha256',
            'lineage_json',
            'input_observation_id',
        }.issubset(
            {str(item[1]) for item in conn.execute('PRAGMA table_info(feature_scoring_input)')}
        )
        assert bootstrap_stage6a(conn, bundle)
    finally:
        conn.close()


def test_atomic_matrix_preserves_missingness_and_zero_weights(tmp_path: Path) -> None:
    bundle, conn = _prepared(tmp_path)
    try:
        build = build_scoring_features(conn, bundle, as_of='2024-12-31')
        result = validate_scoring_features(conn, bundle, as_of='2024-12-31')
        _, metrics = load_metric_registry(
            bundle.base_dir / bundle.payload['specialized_metrics']['registry_path']
        )
        assert build['status'] == 'PASS'
        assert result['status'] == 'PASS'
        assert build['ticker_count'] == 6
        assert build['source_live_ticker_count'] == 6
        assert build['excluded_ticker_count'] == 0
        assert build['component_count'] == 6 * (len(CORE_COMPONENT_SPECS) + len(metrics))
        assert build['rank_ready_count'] == 5
        assert conn.execute(
            'SELECT COUNT(*) FROM feature_scoring_component WHERE component_weight<>0'
        ).fetchone()[0] == 0
        stale = conn.execute(
            '''
            SELECT raw_value,normalized_value,availability_status,exclusion_reason
            FROM feature_scoring_component
            WHERE ticker='T6' AND component_name='gross_margin'
            '''
        ).fetchone()
        assert stale['raw_value'] == pytest.approx(0.6)
        assert stale['normalized_value'] is None
        assert stale['availability_status'] == 'quality_rejected'
        assert stale['exclusion_reason'] == 'quality_status:stale'
        review = conn.execute(
            "SELECT * FROM feature_scoring_input WHERE ticker='T6'"
        ).fetchone()
        assert review['rank_ready_flag'] == 0
        assert 'missing_requirement:any_financial' in review['review_reason']
        assert conn.execute(
            '''
            SELECT COUNT(*) FROM feature_scoring_component
            WHERE component_group='specialized'
              AND (raw_value IS NOT NULL OR normalized_value IS NOT NULL
                   OR component_score IS NOT NULL OR component_weight<>0)
            '''
        ).fetchone()[0] == 0
        assert conn.execute(
            '''
            SELECT COUNT(*) FROM feature_scoring_model_output
            WHERE asof_date='2024-12-31'
            '''
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_reviewed_exclusion_is_removed_before_normalization(tmp_path: Path) -> None:
    bundle, conn = _prepared(tmp_path)
    try:
        payload = copy.deepcopy(bundle.payload)
        payload['calibration_scope']['expected_excluded_ticker_count'] = 1
        payload['calibration_scope']['expected_remaining_current_ticker_count'] = 5
        payload['calibration_scope'][
            'expected_remaining_current_tickers_sha256'
        ] = _ticker_set_sha256([f'T{i}' for i in range(1, 6)])
        payload['calibration_scope']['expected_remaining_current_by_cohort'][
            'packaged_foods_agricultural_products'
        ] = 5
        payload['calibration_scope']['excluded_tickers_by_cohort'][
            'packaged_foods_agricultural_products'
        ] = ['T6']
        scoped = ConfigBundle(bundle.path, bundle.base_dir, payload)

        build = build_scoring_features(conn, scoped, as_of='2024-12-31')
        validation = validate_scoring_features(conn, scoped, as_of='2024-12-31')

        assert build['ticker_count'] == 5
        assert build['excluded_ticker_count'] == 1
        assert validation['status'] == 'PASS'
        assert conn.execute(
            "SELECT COUNT(*) FROM feature_scoring_input WHERE ticker='T6'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM feature_scoring_component WHERE ticker='T6'"
        ).fetchone()[0] == 0
        top = conn.execute(
            """SELECT normalized_value FROM feature_scoring_component
               WHERE ticker='T5' AND component_name='residual_momentum_63d'"""
        ).fetchone()[0]
        assert top == pytest.approx(100.0)
    finally:
        conn.close()


def test_duplicate_active_membership_fails_before_normalization(
    tmp_path: Path,
) -> None:
    bundle, conn = _prepared(tmp_path)
    try:
        with conn:
            conn.execute(
                '''
                INSERT INTO dim_universe_membership(
                    company_id,security_id,ticker,membership_source_id,
                    membership_basis,start_date,end_date,membership_status,
                    is_current_member,point_in_time_flag,live_investable_flag,
                    historical_calibration_eligible_flag,confidence,reason,
                    created_at,updated_at
                )
                SELECT company_id,security_id,ticker,membership_source_id,
                       'overlap_fixture','2020-01-02',end_date,
                       membership_status,is_current_member,point_in_time_flag,
                       live_investable_flag,historical_calibration_eligible_flag,
                       confidence,'overlapping active membership',created_at,updated_at
                FROM dim_universe_membership
                WHERE ticker='T1'
                LIMIT 1
                '''
            )
        with pytest.raises(ValueError, match='duplicate ticker T1'):
            build_scoring_features(conn, bundle, as_of='2024-12-31')
    finally:
        conn.close()


def test_stage6a_replay_is_deterministic_and_tamper_fails(tmp_path: Path) -> None:
    bundle, conn = _prepared(tmp_path)
    try:
        build_scoring_features(conn, bundle, as_of='2024-12-31')
        before_inputs = [
            tuple(row)
            for row in conn.execute(
                '''
                SELECT ticker,input_observation_id,contract_sha256,lineage_json
                FROM feature_scoring_input ORDER BY ticker
                '''
            )
        ]
        before_components = [
            tuple(row)
            for row in conn.execute(
                '''
                SELECT ticker,component_name,component_observation_id,
                       contract_sha256,lineage_json
                FROM feature_scoring_component ORDER BY ticker,component_name
                '''
            )
        ]
        build_scoring_features(conn, bundle, as_of='2024-12-31')
        assert before_inputs == [
            tuple(row)
            for row in conn.execute(
                '''
                SELECT ticker,input_observation_id,contract_sha256,lineage_json
                FROM feature_scoring_input ORDER BY ticker
                '''
            )
        ]
        assert before_components == [
            tuple(row)
            for row in conn.execute(
                '''
                SELECT ticker,component_name,component_observation_id,
                       contract_sha256,lineage_json
                FROM feature_scoring_component ORDER BY ticker,component_name
                '''
            )
        ]
        with conn:
            conn.execute(
                '''
                UPDATE feature_scoring_component SET component_weight=0.25
                WHERE ticker='T1' AND component_name='gross_margin'
                '''
            )
        result = validate_scoring_features(conn, bundle, as_of='2024-12-31')
        assert result['status'] == 'FAIL'
        failed = {row['check'] for row in result['checks'] if not row['passed']}
        assert 'all_component_weights_zero' in failed
        assert 'component_observation_ids_exact' in failed
    finally:
        conn.close()


def test_build_refuses_to_replace_inputs_after_model_output(tmp_path: Path) -> None:
    bundle, conn = _prepared(tmp_path)
    try:
        build_scoring_features(conn, bundle, as_of='2024-12-31')
        now = utc_now()
        with conn:
            conn.execute(
                '''
                INSERT INTO feature_scoring_model_output(
                    ticker,asof_date,source_id,promotion_state,
                    portfolio_candidate_gate,oos_score_valid_flag,created_at
                ) VALUES ('T1','2024-12-31','consumer_defensive_scoring_contract',
                          'deferred',0,0,?)
                ''',
                (now,),
            )
        before = conn.execute(
            'SELECT COUNT(*) FROM feature_scoring_component'
        ).fetchone()[0]
        with pytest.raises(RuntimeError, match='after a model output exists'):
            build_scoring_features(conn, bundle, as_of='2024-12-31')
        assert conn.execute(
            'SELECT COUNT(*) FROM feature_scoring_component'
        ).fetchone()[0] == before
    finally:
        conn.close()
