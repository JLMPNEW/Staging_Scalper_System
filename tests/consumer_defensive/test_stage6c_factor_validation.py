from __future__ import annotations

import copy
import json
from datetime import date, timedelta
from pathlib import Path

from consumer_defensive.adapters.factor_validation import (
    run_consumer_defensive_factor_validation,
    validate_consumer_defensive_factor_validation,
)
from consumer_defensive.core.config import ConfigBundle, load_config
from consumer_defensive.core.db import connect, init_db, utc_now
from consumer_defensive.core.market_data import (
    SELECTION_PURPOSE,
    YAHOO_SOURCE_ID,
    ensure_stage3_schema,
)
from consumer_defensive.core.metric_registry import (
    load_metric_registry,
    upsert_metric_registry,
)
from consumer_defensive.core.source_registry import (
    load_source_registry,
    upsert_source_registry,
)
from consumer_defensive.core.specialized_metrics import (
    bootstrap_stage6b,
    specialized_observation_sha256,
    stage6b_policy_sha256,
)
from consumer_defensive.core.stage6c_panel import (
    build_stage6c_panel,
    validate_stage6c_panel,
    write_stage6c_reports,
)
from consumer_defensive.core.stage6c_schema import (
    STAGE6C_MIGRATION_SHA256,
    ensure_stage6c_schema,
)


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / 'consumer_defensive' / 'config.yaml'
SOURCES = ROOT / 'consumer_defensive' / 'data' / 'free_source_registry.yaml'
METRICS = (
    ROOT / 'consumer_defensive' / 'data'
    / 'consumer_defensive_specialized_metric_registry.yaml'
)


def _bundle() -> ConfigBundle:
    source = load_config(CONFIG)
    return ConfigBundle(source.path, source.base_dir, copy.deepcopy(source.payload))


def _weekdays(start: str, end: str) -> list[str]:
    current = date.fromisoformat(start)
    final = date.fromisoformat(end)
    output: list[str] = []
    while current <= final:
        if current.weekday() < 5:
            output.append(current.isoformat())
        current += timedelta(days=1)
    return output


def _prepared(tmp_path: Path):
    bundle = _bundle()
    conn = connect(tmp_path / 'stage6c.sqlite')
    init_db(conn)
    ensure_stage3_schema(conn)
    upsert_source_registry(conn, load_source_registry(SOURCES))
    registry_version, metrics = load_metric_registry(METRICS)
    upsert_metric_registry(
        conn,
        registry_version=registry_version,
        metrics=metrics,
    )
    bootstrap_stage6b(conn, bundle)
    now = utc_now()
    tickers = [f'T{index:02d}' for index in range(1, 13)]
    trading_dates = _weekdays('2018-06-01', '2022-12-30')
    with conn:
        for index, ticker in enumerate(tickers, start=1):
            company_id = conn.execute(
                '''INSERT INTO dim_company(
                       primary_ticker,cik,company_name,universe_status,is_active,
                       first_seen_at,updated_at
                   ) VALUES (?,?,?,'current',1,?,?)''',
                (ticker, str(1000 + index), f'Company {index}', now, now),
            ).lastrowid
            security_id = conn.execute(
                '''INSERT INTO dim_security(
                       company_id,ticker,provider_price_symbol,exchange,
                       listing_status,is_primary_listing,currency,
                       listing_start_date,created_at,updated_at
                   ) VALUES (?,?,?,'NYSE','active',1,'USD','2010-01-01',?,?)''',
                (company_id, ticker, ticker, now, now),
            ).lastrowid
            conn.execute(
                '''INSERT INTO dim_consumer_defensive_taxonomy(
                       company_id,security_id,ticker,calibration_cohort_id,
                       calibration_cohort,applicability_subtype,
                       taxonomy_confidence,analyst_reviewed,updated_at
                   ) VALUES (?,?,?,'packaged_foods_agricultural_products',
                       'Packaged Foods','branded_food',1,1,?)''',
                (company_id, security_id, ticker, now),
            )
            conn.execute(
                '''INSERT INTO dim_universe_membership(
                       company_id,security_id,ticker,membership_basis,start_date,
                       membership_status,is_current_member,point_in_time_flag,
                       live_investable_flag,historical_calibration_eligible_flag,
                       created_at,updated_at
                   ) VALUES (?,?,?,'fixture','2019-01-02','active',1,1,1,1,?,?)''',
                (company_id, security_id, ticker, now, now),
            )
        for ticker in [*tickers, 'SPY', 'XLP']:
            conn.execute(
                '''INSERT INTO dim_price_series_selection(
                       ticker,purpose,selected_source_id,selection_asof_date,
                       first_bar_date,last_bar_date,bar_count,adjustment_basis,
                       selection_reason,expected_start_date,expected_end_date,
                       coverage_status,created_at,updated_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (
                    ticker, SELECTION_PURPOSE, YAHOO_SOURCE_ID, '2022-12-30',
                    trading_dates[0], trading_dates[-1], len(trading_dates),
                    'adjusted_total_return', 'fixture', trading_dates[0],
                    trading_dates[-1], 'complete', now, now,
                ),
            )
            slope = (
                0.00035 if ticker == 'SPY' else 0.00045 if ticker == 'XLP'
                else 0.0002 + int(ticker[1:]) * 0.000035
            )
            price = 50.0
            for trading_date in trading_dates:
                price *= 1.0 + slope
                conn.execute(
                    '''INSERT INTO fact_price_ohlcv(
                           ticker,bar_date,source_id,open,high,low,close,
                           adjusted_close,volume,dividend,split_factor,
                           total_return_basis,source_timestamp,created_at
                       ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (
                        ticker, trading_date, YAHOO_SOURCE_ID, price, price,
                        price, price, price, 1_000_000.0, 0.0, 1.0,
                        'adjusted_total_return', trading_date, now,
                    ),
                )
        for quarter, accepted_at in enumerate(
            (
                '2019-02-15T12:00:00Z', '2019-05-15T12:00:00Z',
                '2019-08-15T12:00:00Z', '2020-02-15T12:00:00Z',
                '2020-05-15T12:00:00Z', '2020-08-15T12:00:00Z',
                '2021-02-15T12:00:00Z', '2021-05-15T12:00:00Z',
                '2021-08-15T12:00:00Z', '2022-02-15T12:00:00Z',
                '2022-05-15T12:00:00Z',
            ),
            start=1,
        ):
            period_end = (
                date.fromisoformat(accepted_at[:10]) - timedelta(days=45)
            ).isoformat()
            for index, ticker in enumerate(tickers, start=1):
                observation = {
                    'ticker': ticker,
                    'metric_id': 'organic_revenue_growth_pct',
                    'period_start': '',
                    'period_end': period_end,
                    'accepted_at': accepted_at,
                    'numeric_value': float(index) + quarter / 100.0,
                    'unit': 'percent',
                    'definition_version': (
                        'consumer_defensive_specialized_measurements_v1'
                    ),
                    'applicability_status': 'applicable',
                    'evidence_status': 'accepted_measurement_only',
                    'evidence_key': f'{ticker}-{quarter}',
                    'source_id': 'shared_dedicated_sec_parser',
                    'source_document': f'{ticker}-{quarter}.htm',
                    'confidence': 0.99,
                    'extraction_method': 'fixture',
                    'scope': 'consolidated',
                    'lineage_json': '{}',
                    'production_status': 'measurement_only',
                    'parser_run_id': None,
                }
                observation['observation_sha256'] = (
                    specialized_observation_sha256(observation)
                )
                conn.execute(
                    '''INSERT INTO fact_specialized_metric_observation(
                           ticker,metric_id,period_start,period_end,accepted_at,
                           numeric_value,unit,definition_version,
                           applicability_status,evidence_status,evidence_key,
                           source_id,source_document,created_at,confidence,
                           extraction_method,scope,lineage_json,
                           observation_sha256,production_status,parser_run_id
                       ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (
                        observation['ticker'], observation['metric_id'],
                        observation['period_start'], observation['period_end'],
                        observation['accepted_at'], observation['numeric_value'],
                        observation['unit'], observation['definition_version'],
                        observation['applicability_status'],
                        observation['evidence_status'], observation['evidence_key'],
                        observation['source_id'], observation['source_document'], now,
                        observation['confidence'], observation['extraction_method'],
                        observation['scope'], observation['lineage_json'],
                        observation['observation_sha256'],
                        observation['production_status'], None,
                    ),
                )
        conn.execute(
            '''INSERT INTO stage6b_specialized_run(
                   asof_date,adapter_version,policy_sha256,
                   source_manifest_sha256,seal_manifest_sha256,
                   ingestion_config_sha256,issuer_scope_sha256,started_at,
                   completed_at,status,inventory_document_count,
                   accepted_observation_count,metadata_json
               ) VALUES ('2022-12-30','fixture',?,?,?,?,?,?,?,
                         'measurement_only_complete',0,?,?)''',
            (
                stage6b_policy_sha256(), 'a' * 64, 'b' * 64, 'c' * 64,
                'd' * 64, now, now, 12 * 11,
                json.dumps({'fixture': True}, sort_keys=True),
            ),
        )
    return bundle, conn


def test_stage6c_schema_panel_and_shared_factor_evidence(tmp_path: Path) -> None:
    bundle, conn = _prepared(tmp_path)
    try:
        ensure_stage6c_schema(conn)
        ensure_stage6c_schema(conn)
        ledger = conn.execute(
            'SELECT migration_version,migration_sha256 FROM stage6c_schema_migrations'
        ).fetchone()
        assert tuple(ledger) == (1, STAGE6C_MIGRATION_SHA256)

        built = build_stage6c_panel(
            conn,
            bundle,
            as_of='2022-12-30',
            history_start='2019-01-02',
        )
        assert built['status'] == 'PASS'
        assert built['evaluation_date_count'] >= 36
        assert built['numeric_row_count'] > 0
        repeated = build_stage6c_panel(
            conn,
            bundle,
            as_of='2022-12-30',
            history_start='2019-01-02',
        )
        assert repeated['panel_sha256'] == built['panel_sha256']
        market_share = conn.execute(
            '''SELECT factor_validation_eligible,exclusion_reason
               FROM stage6c_feature_manifest
               WHERE stage6c_run_id=?
                 AND factor_id='market_share_change_bps' ''',
            (built['stage6c_run_id'],),
        ).fetchone()
        assert tuple(market_share) == (
            0,
            'selective_disclosure_requires_coverage_bias_validation',
        )
        assert validate_stage6c_panel(
            conn, stage6c_run_id=built['stage6c_run_id']
        )['status'] == 'PASS'

        stage6c_dir = tmp_path / 'stage6c-output'
        report = write_stage6c_reports(
            conn,
            stage6c_run_id=built['stage6c_run_id'],
            output_dir=stage6c_dir,
        )
        assert report['status'] == 'PASS'
        output_root = tmp_path / 'factor-validation'
        campaign = run_consumer_defensive_factor_validation(
            conn,
            bundle,
            stage6c_run_id=built['stage6c_run_id'],
            panel_path=stage6c_dir / 'stage6c_specialized_factor_panel.csv',
            feature_manifest_path=stage6c_dir / 'stage6c_feature_manifest.csv',
            output_root=output_root,
            factor_ids=['organic_revenue_growth_pct'],
            horizons=[21],
        )
        assert campaign['mode'] == 'shadow'
        assert campaign['production_promotion_enabled'] is False
        assert campaign['portfolio_write_enabled'] is False
        verified = validate_consumer_defensive_factor_validation(
            output_root,
            campaign_id=campaign['campaign_id'],
        )
        assert verified['status'] == 'PASS'
        assert verified['cell_count'] == 4
    finally:
        conn.close()


def test_stage6c_rejects_future_acceptance_and_context_direction(tmp_path: Path) -> None:
    bundle, conn = _prepared(tmp_path)
    try:
        built = build_stage6c_panel(
            conn,
            bundle,
            as_of='2022-12-30',
            history_start='2019-01-02',
        )
        row = conn.execute(
            '''SELECT factor_validation_eligible,exclusion_reason
               FROM stage6c_feature_manifest
               WHERE stage6c_run_id=? AND factor_id='price_mix_growth_pct' ''',
            (built['stage6c_run_id'],),
        ).fetchone()
        assert tuple(row) == (
            0,
            'context_dependent_direction_requires_registered_policy',
        )
        with conn:
            conn.execute(
                '''UPDATE stage6c_specialized_factor_panel
                   SET source_accepted_at='2099-01-01T00:00:00Z'
                   WHERE rowid=(
                       SELECT rowid FROM stage6c_specialized_factor_panel
                       WHERE stage6c_run_id=?
                         AND source_accepted_at IS NOT NULL LIMIT 1
                   )''',
                (built['stage6c_run_id'],),
            )
        validation = validate_stage6c_panel(
            conn, stage6c_run_id=built['stage6c_run_id']
        )
        assert validation['status'] == 'FAIL'
        failed = {
            item['check'] for item in validation['checks']
            if item['status'] == 'FAIL'
        }
        assert 'accepted_at_point_in_time' in failed
        assert 'row_hashes_exact' in failed
    finally:
        conn.close()
