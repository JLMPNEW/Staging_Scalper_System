from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path

import pytest

from consumer_defensive.core import historical_features_v2 as module
from consumer_defensive.core.config import ConfigBundle, load_config
from consumer_defensive.core.scoring_features import CORE_COMPONENT_SPECS


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / 'consumer_defensive' / 'config.yaml'


def _bundle() -> ConfigBundle:
    original = load_config(CONFIG)
    return ConfigBundle(
        original.path, original.base_dir, copy.deepcopy(original.payload)
    )


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.executescript(
        '''
        CREATE TABLE fact_13f_positioning(
            ticker TEXT, publication_date TEXT, source_id TEXT,
            institutional_ownership_delta_pct REAL,
            source_observation_id TEXT
        );
        CREATE TABLE fact_short_interest(
            ticker TEXT, publication_date TEXT, source_id TEXT,
            short_float_pct REAL, days_to_cover REAL,
            source_observation_id TEXT
        );
        CREATE TABLE fact_borrow_snapshot(
            ticker TEXT, asof_date TEXT, source_id TEXT,
            borrow_fee REAL, source_observation_id TEXT
        );
        CREATE TABLE fact_sec_ownership_transaction(
            transaction_id TEXT, ticker TEXT, accepted_at TEXT,
            availability_date TEXT, transaction_date TEXT,
            acquired_disposed TEXT, shares REAL, price REAL,
            source_id TEXT, is_current_truth INTEGER,
            source_observation_id TEXT
        );
        '''
    )
    return conn


def test_positioning_v2_enforces_freshness_birthdates_and_pit_insiders() -> None:
    bundle = _bundle()
    conn = _connection()
    market_source = bundle.payload['positioning'][
        'market_positioning_source_id'
    ]
    ownership_source = bundle.payload['positioning']['ownership_source_id']
    conn.execute(
        'INSERT INTO fact_13f_positioning VALUES (?,?,?,?,?)',
        ('AAA', '2022-01-15', market_source, 0.12, '13f-fresh'),
    )
    conn.execute(
        'INSERT INTO fact_short_interest VALUES (?,?,?,?,?,?)',
        ('AAA', '2021-10-01', market_source, 0.09, 4.0, 'short-stale'),
    )
    ownership = [
        (
            'valid', 'AAA', '2022-01-20T12:00:00Z', '2022-01-20',
            '2022-01-19', 'A', 10.0, 5.0, ownership_source, 1, 'valid-id',
        ),
        (
            'future-acceptance', 'AAA', '2022-02-01T12:00:00Z',
            '2022-02-01', '2022-01-20', 'A', 100.0, 5.0,
            ownership_source, 1, 'future-acceptance-id',
        ),
        (
            'future-transaction', 'AAA', '2022-01-20T12:00:00Z',
            '2022-01-20', '2022-02-02', 'A', 100.0, 5.0,
            ownership_source, 1, 'future-transaction-id',
        ),
        (
            'superseded', 'AAA', '2022-01-21T12:00:00Z', '2022-01-21',
            '2022-01-20', 'A', 100.0, 5.0, ownership_source, 0,
            'superseded-id',
        ),
    ]
    conn.executemany(
        'INSERT INTO fact_sec_ownership_transaction VALUES '
        '(?,?,?,?,?,?,?,?,?,?,?)',
        ownership,
    )

    result = module.positioning_features_for_date_v2(
        conn, bundle, as_of='2022-01-31', tickers={'AAA'}
    )['AAA']

    assert result['institutional_flow'] == pytest.approx(0.12)
    assert result['short_float_pct'] is None
    assert result['short_days_to_cover'] is None
    assert result['insider_net_buying'] == pytest.approx(50.0)
    assert result['quality_status'] == 'partial'
    assert result['source_states']['institutional_13f'] == 'fresh'
    assert result['source_states']['short_interest'] == 'missing_or_stale'
    assert result['source_states']['sec_form4'] == 'fresh'

    pre_short = module.positioning_features_for_date_v2(
        conn, bundle, as_of='2020-01-31', tickers={'AAA'}
    )['AAA']
    assert pre_short['short_float_pct'] is None
    assert pre_short['source_states']['short_interest'] == (
        'structurally_unavailable'
    )


def test_stale_positioning_is_removed_before_peer_normalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _bundle()
    conn = _connection()
    as_of = '2022-01-31'
    tickers = [f'T{i}' for i in range(1, 7)]
    market_source = bundle.payload['positioning'][
        'market_positioning_source_id'
    ]
    for index, ticker in enumerate(tickers, start=1):
        institutional_value = 1000.0 if ticker == 'T6' else float(index)
        conn.execute(
            'INSERT INTO fact_13f_positioning VALUES (?,?,?,?,?)',
            (
                ticker, '2022-01-15', market_source,
                institutional_value, f'13f-{ticker}',
            ),
        )
        short_date = '2021-01-01' if ticker == 'T6' else '2022-01-20'
        conn.execute(
            'INSERT INTO fact_short_interest VALUES (?,?,?,?,?,?)',
            (
                ticker, short_date, market_source, 0.01 * index,
                float(index), f'short-{ticker}',
            ),
        )

    membership = [
        {
            'asof_date': as_of,
            'ticker': ticker,
            'cohort_id': 'beverages',
            'applicability_subtype': 'all_operating_issuers',
        }
        for ticker in tickers
    ]
    labels = [
        {
            **row,
            'membership_eligible_flag': 1,
            'investable_flag': 1,
            'sample_role': 'deep_replay_research',
            'market_regime': 'test',
            'terminal_event_status': '',
            'label_status': 'complete',
            'forward_xlp_residual_return_21d': 0.01,
            'forward_xlp_residual_return_63d': 0.02,
            'forward_xlp_residual_return_126d': 0.03,
        }
        for row in membership
    ]

    def _upstreams(group: str) -> dict[str, dict[str, object]]:
        quality_field = (
            'financial_quality_status'
            if group == 'financial' else 'quality_status'
        )
        quality_value = 'complete' if group == 'financial' else 'full'
        fields = [
            spec.source_field
            for spec in CORE_COMPONENT_SPECS
            if spec.group == group
        ]
        return {
            ticker: {
                quality_field: quality_value,
                'source_hash': f'{group}-{ticker}',
                **{
                    field: float(index)
                    for field in fields
                },
            }
            for index, ticker in enumerate(tickers, start=1)
        }

    market = _upstreams('market')
    financial = _upstreams('financial')
    monkeypatch.setattr(module, '_label_rows', lambda *_args, **_kwargs: labels)
    monkeypatch.setattr(
        module, '_specialized_rows', lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(
        module,
        '_price_selection_and_history',
        lambda *_args, **_kwargs: ({}, {}, 'selection-hash'),
    )
    monkeypatch.setattr(
        module,
        '_market_features_for_date',
        lambda **_kwargs: market,
    )
    monkeypatch.setattr(
        module,
        '_financial_features_for_date',
        lambda *_args, **_kwargs: financial,
    )
    monkeypatch.setattr(
        module,
        'load_institutional_history_v2',
        lambda *_args, **_kwargs: (None, {'fixture': 'local_positioning_rows'}),
    )

    panel, summary = module.build_historical_core_panel_v2(
        conn,
        bundle,
        stage6c_run_id=3,
        membership_rows=membership,
        accepted_factor_cells=[],
        market_policy=object(),  # type: ignore[arg-type]
    )
    by_ticker = {row['ticker']: row for row in panel}
    valid_scores = {
        ticker: json.loads(by_ticker[ticker]['component_scores_json'])[
            'institutional_flow'
        ]
        for ticker in tickers[:5]
    }
    stale_scores = json.loads(
        by_ticker['T6']['component_scores_json']
    )
    stale_quality = json.loads(
        by_ticker['T6']['component_quality_json']
    )

    assert valid_scores == {
        'T1': 0.0,
        'T2': 25.0,
        'T3': 50.0,
        'T4': 75.0,
        'T5': 100.0,
    }
    assert stale_scores['institutional_flow'] == 50.0
    assert stale_quality['institutional_flow'] == 0.0
    assert summary['positioning_quality_counts'] == {
        'complete': 5,
        'partial': 1,
    }
    assert summary['positioning_source_state_counts'][
        'short_interest:missing_or_stale'
    ] == 1

