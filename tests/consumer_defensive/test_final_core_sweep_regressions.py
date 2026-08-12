from __future__ import annotations

import json
from copy import deepcopy
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from consumer_defensive.core.db import connect, init_db, utc_now
from consumer_defensive.core.market_data import (
    MarketDataPolicy,
    coverage_qualifies,
    load_market_policy,
)
from consumer_defensive.core.metric_registry import (
    SpecializedMetric,
    upsert_metric_registry,
)
from consumer_defensive.core.source_registry import (
    SourceRegistryRow,
    load_source_registry,
    upsert_source_registry,
)
from consumer_defensive.core.universe import (
    _upsert_security,
    load_policy,
    read_csv,
    validate_current_rows,
)
from consumer_defensive.core.yahoo_prices import fetch_yahoo_job, load_yahoo_prices


ROOT = Path(__file__).resolve().parents[2]
MARKET_POLICY = ROOT / 'consumer_defensive' / 'data' / 'consumer_defensive_market_data_policy.yaml'


def _epoch(value: str) -> int:
    return int(
        datetime.combine(
            date.fromisoformat(value),
            datetime.min.time(),
            tzinfo=timezone.utc,
        ).timestamp()
    )


def _yahoo_payload(symbol: str, bar_date: str = '2019-01-02') -> str:
    return json.dumps(
        {
            'chart': {
                'error': None,
                'result': [
                    {
                        'meta': {
                            'symbol': symbol,
                            'currency': 'USD',
                            'regularMarketTime': _epoch(bar_date),
                        },
                        'timestamp': [_epoch(bar_date)],
                        'indicators': {
                            'quote': [
                                {
                                    'open': [10.0],
                                    'high': [11.0],
                                    'low': [9.0],
                                    'close': [10.5],
                                    'volume': [1000.0],
                                }
                            ],
                            'adjclose': [{'adjclose': [10.5]}],
                        },
                    }
                ],
            }
        }
    )


def _market_policy_with_cache(tmp_path: Path) -> MarketDataPolicy:
    original = load_market_policy(MARKET_POLICY)
    payload = deepcopy(original.payload)
    payload['yahoo']['cache_dir'] = str(tmp_path / 'cache')
    return MarketDataPolicy(path=original.path, payload=payload)


def test_yahoo_invalid_identity_never_enters_cache(tmp_path: Path) -> None:
    policy = _market_policy_with_cache(tmp_path)

    def wrong_fetcher(*_args: object, **_kwargs: object) -> tuple[int, str]:
        return 200, _yahoo_payload('PEP')

    result = fetch_yahoo_job(
        'KO',
        policy=policy,
        start=date(2019, 1, 2),
        end=date(2019, 1, 2),
        force_refresh=False,
        fetcher=wrong_fetcher,
    )
    assert result.error.startswith('yahoo_symbol_mismatch')
    assert not list((tmp_path / 'cache').glob('*.json'))

    def valid_fetcher(*_args: object, **_kwargs: object) -> tuple[int, str]:
        return 200, _yahoo_payload('KO')

    repaired = fetch_yahoo_job(
        'KO',
        policy=policy,
        start=date(2019, 1, 2),
        end=date(2019, 1, 2),
        force_refresh=False,
        fetcher=valid_fetcher,
    )
    assert repaired.error == ''
    assert len(list((tmp_path / 'cache').glob('*.json'))) == 1


@pytest.mark.parametrize(
    'ticker',
    [r'C:\outside', r'\\server\share', r'KO\outside', '../KO', '/KO', 'CON'],
)
def test_yahoo_unsafe_ticker_fails_before_provider_and_cache(
    tmp_path: Path, ticker: str,
) -> None:
    policy = _market_policy_with_cache(tmp_path)
    calls = 0

    def fetcher(*_args: object, **_kwargs: object) -> tuple[int, str]:
        nonlocal calls
        calls += 1
        return 200, _yahoo_payload('KO')

    with pytest.raises(ValueError, match='unsafe syntax|reserved'):
        fetch_yahoo_job(
            ticker, policy=policy, start=date(2019, 1, 2),
            end=date(2019, 1, 2), force_refresh=True, fetcher=fetcher,
        )
    assert calls == 0
    assert not (tmp_path / 'cache').exists()


def test_yahoo_batch_rejects_unsafe_ticker_before_ingestion_run(
    tmp_path: Path,
) -> None:
    policy = _market_policy_with_cache(tmp_path)
    conn = connect(tmp_path / 'yahoo-unsafe.sqlite')
    try:
        init_db(conn)
        with pytest.raises(ValueError, match='unsafe syntax'):
            load_yahoo_prices(
                conn, policy, start='2019-01-02', end='2019-01-03',
                tickers=[r'C:\outside'], force_refresh=True,
            )
        assert conn.execute('SELECT COUNT(*) FROM ingestion_runs').fetchone()[0] == 0
        assert not (tmp_path / 'cache').exists()
    finally:
        conn.close()


def test_yahoo_atomic_cache_publish_does_not_clobber_legacy_temp_hardlink(
    tmp_path: Path,
) -> None:
    policy = _market_policy_with_cache(tmp_path)
    cache = tmp_path / 'cache'
    cache.mkdir()
    outside = tmp_path / 'outside.txt'
    outside.write_text('last-good', encoding='utf-8')
    legacy_temp = cache / '.ticker_KO_2019-01-02_2019-01-02.json.tmp'
    try:
        legacy_temp.hardlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip('hardlinks unavailable')

    def fetcher(*_args: object, **_kwargs: object) -> tuple[int, str]:
        return 200, _yahoo_payload('KO')

    result = fetch_yahoo_job(
        'KO', policy=policy, start=date(2019, 1, 2),
        end=date(2019, 1, 2), force_refresh=True, fetcher=fetcher,
    )
    assert result.error == ''
    assert outside.read_text(encoding='utf-8') == 'last-good'
    assert legacy_temp.read_text(encoding='utf-8') == 'last-good'
    assert (cache / 'ticker_KO_2019-01-02_2019-01-02.json').is_file()


def test_current_universe_rejects_unsafe_ticker_syntax_before_load() -> None:
    policy = load_policy(
        ROOT / 'consumer_defensive' / 'data'
        / 'consumer_defensive_universe_policy.yaml'
    )
    rows = read_csv(policy.resolve('authoritative_current_csv'))
    rows[0] = {**rows[0], 'ticker': r'C:\outside'}
    with pytest.raises(ValueError, match='unsafe syntax'):
        validate_current_rows(rows, policy)


def test_coverage_rejects_long_internal_trading_gap() -> None:
    expected = tuple(f'2019-01-{day:02d}' for day in range(2, 22))
    observed = expected[:5] + expected[11:]
    coverage = {
        'first': expected[0],
        'last': expected[-1],
        'rows': len(observed),
        'invalid_adjusted': 0,
        'observed_dates': observed,
    }
    assert not coverage_qualifies(
        coverage,
        expected_start=expected[0],
        expected_end=expected[-1],
        start_tolerance_days=3,
        end_tolerance_days=3,
        minimum_rows=5,
        expected_dates=expected,
    )


def test_current_reload_preserves_provider_resolved_symbol(tmp_path: Path) -> None:
    conn = connect(tmp_path / 'provider_symbol.sqlite')
    try:
        init_db(conn)
        now = utc_now()
        company_id = int(
            conn.execute(
                '''INSERT INTO dim_company(
                       primary_ticker, company_name, universe_status, is_active,
                       data_quality_status, first_seen_at, updated_at
                   ) VALUES('BF-B', 'Brown Forman', 'keep', 1, 'reviewed', ?, ?)''',
                (now, now),
            ).lastrowid
        )
        conn.execute(
            '''INSERT INTO dim_security(
                   company_id, ticker, provider_price_symbol, exchange,
                   listing_country, security_type, listing_status,
                   is_primary_listing, currency, created_at, updated_at
               ) VALUES(?, 'BF-B', 'BF.B', 'NYSE', 'United States',
                        'Common Stock', 'active', 1, 'USD', ?, ?)''',
            (company_id, now, now),
        )
        _upsert_security(
            conn,
            {
                'ticker': 'BF-B',
                'exchange': 'NYSE',
                'country': 'United States',
                'security_type': 'Common Stock',
                'currency': 'USD',
            },
            company_id,
        )
        symbol = conn.execute(
            "SELECT provider_price_symbol FROM dim_security WHERE ticker='BF-B'"
        ).fetchone()[0]
        assert symbol == 'BF.B'
    finally:
        conn.close()


def test_source_registry_rejects_unknown_fields(tmp_path: Path) -> None:
    path = tmp_path / 'sources.yaml'
    path.write_text(
        '''sources:
  - source_id: example
    stage: market_data
    source_name: Example
    source_owner: Example Owner
    source_type: api
    base_url: https://example.test
    subsector_scope: consumer_defensive
    status: active
    unexpected_typo: true
''',
        encoding='utf-8',
    )
    with pytest.raises(ValueError, match='unknown fields'):
        load_source_registry(path)


def test_source_registry_retires_absent_rows_without_deleting(tmp_path: Path) -> None:
    conn = connect(tmp_path / 'sources.sqlite')
    try:
        init_db(conn)
        first = SourceRegistryRow(
            source_id='first',
            stage='market_data',
            source_name='First',
            source_owner='Owner',
            source_type='api',
            base_url='https://first.test',
            status='active',
        )
        stale = SourceRegistryRow(
            source_id='stale',
            stage='market_data',
            source_name='Stale',
            source_owner='Owner',
            source_type='api',
            base_url='https://stale.test',
            status='active',
        )
        upsert_source_registry(conn, [first, stale], retire_absent=True)
        upsert_source_registry(conn, [first], retire_absent=True)
        rows = {
            str(row[0]): str(row[1])
            for row in conn.execute(
                "SELECT source_id, status FROM source_registry WHERE source_id IN ('first','stale')"
            )
        }
        assert rows == {'first': 'active', 'stale': 'retired'}
    finally:
        conn.close()


def test_metric_registry_retires_absent_rows_without_deleting(tmp_path: Path) -> None:
    conn = connect(tmp_path / 'metrics.sqlite')
    try:
        init_db(conn)
        first = SpecializedMetric(
            metric_id='first',
            cohorts=('beverages',),
            applicability_subtypes=('all_operating_issuers',),
            unit_family='percent',
            direction_hint='positive',
            purpose='first test metric',
            initial_status='research_candidate',
            production_weight=0.0,
        )
        stale = SpecializedMetric(
            metric_id='stale',
            cohorts=('beverages',),
            applicability_subtypes=('all_operating_issuers',),
            unit_family='percent',
            direction_hint='positive',
            purpose='stale test metric',
            initial_status='research_candidate',
            production_weight=0.0,
        )
        upsert_metric_registry(conn, registry_version='v1', metrics=[first, stale])
        upsert_metric_registry(conn, registry_version='v2', metrics=[first])
        rows = {
            str(row[0]): (str(row[1]), float(row[2]))
            for row in conn.execute(
                '''SELECT metric_id, production_status, production_weight
                   FROM dim_specialized_metric'''
            )
        }
        assert rows == {
            'first': ('research_candidate', 0.0),
            'stale': ('retired', 0.0),
        }
    finally:
        conn.close()
