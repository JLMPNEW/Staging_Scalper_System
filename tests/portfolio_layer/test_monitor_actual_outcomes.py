from __future__ import annotations

import sqlite3

from portfolio_layer.expectations_monitor.monitor_common import (
    SCHEMA_SQL,
    _CURRENT_ACTUAL_OUTCOME_KEY,
    _LEGACY_ACTUAL_OUTCOME_KEY,
    _migrate_actual_outcome_key,
    _unique_index_columns,
    append_actual_outcomes,
    connect_monitor_db,
    verify_actual_outcome_chain,
)


def _actual_row(*, fiscal_period_end: str, actual_value: float) -> dict[str, object]:
    return {
        'provider': 'alpha_vantage',
        'endpoint_id': 'earnings_history',
        'ticker': 'SMR',
        'report_date': '2021-03-31',
        'fiscal_period_end': fiscal_period_end,
        'outcome_period_status': 'exact_provider_report_date_match',
        'metric': 'eps',
        'actual_value': actual_value,
        'reporting_currency': '',
        'metric_basis_id': '',
        'metric_basis_status': 'provider_internal_unverified',
        'provider_updated_at_raw': '',
        'provider_published_at_utc': '',
        'fetched_at_utc': '2026-08-08T15:48:00+00:00',
        'available_at_utc': '2026-08-08T15:48:00+00:00',
        'retrieval_cycle': 'test-cycle',
        'response_sha256': 'a' * 64,
        'entitlement_version': 'provider_entitlements_v1:provisional_retention_v1',
        'retention_class': 'provisional_user_authorized',
        'coverage_status': 'available',
        'evaluation_eligible': 0,
        'ineligibility_reasons': (
            'metric_basis_not_comparison_eligible,'
            'actual_publication_time_unverified'
        ),
    }


def test_actual_outcome_identity_includes_fiscal_period(tmp_path) -> None:
    conn = connect_monitor_db(tmp_path / 'monitor.sqlite', timeout_sec=1.0)
    try:
        rows = [
            _actual_row(fiscal_period_end='2020-12-31', actual_value=-0.4509),
            _actual_row(fiscal_period_end='2021-03-31', actual_value=0.3038),
        ]
        assert append_actual_outcomes(conn, rows) == (2, 0)
        assert append_actual_outcomes(conn, rows) == (0, 2)
        assert verify_actual_outcome_chain(conn) == []
        outcome_ids = {
            str(row['outcome_id'])
            for row in conn.execute(
                'SELECT outcome_id FROM provider_actual_outcomes_v2'
            ).fetchall()
        }
        assert len(outcome_ids) == 2
    finally:
        conn.close()


def test_legacy_actual_outcome_key_migrates_without_chain_or_fk_drift(tmp_path) -> None:
    current_clause = '''UNIQUE (
        provider, endpoint_id, ticker, report_date, fiscal_period_end,
        metric, retrieval_cycle
    )'''
    legacy_clause = (
        'UNIQUE (provider, endpoint_id, ticker, report_date, metric, retrieval_cycle)'
    )
    assert SCHEMA_SQL.count(current_clause) == 1
    legacy_schema = SCHEMA_SQL.replace(current_clause, legacy_clause, 1)

    conn = sqlite3.connect(str(tmp_path / 'legacy.sqlite'))
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(legacy_schema)
        assert _LEGACY_ACTUAL_OUTCOME_KEY in _unique_index_columns(
            conn, 'provider_actual_outcomes_v2'
        )
        first = _actual_row(
            fiscal_period_end='2020-12-31', actual_value=-0.4509
        )
        assert append_actual_outcomes(conn, [first]) == (1, 0)
        before = conn.execute(
            'SELECT outcome_id,row_sequence,row_sha256 FROM provider_actual_outcomes_v2'
        ).fetchone()

        _migrate_actual_outcome_key(conn)

        assert _CURRENT_ACTUAL_OUTCOME_KEY in _unique_index_columns(
            conn, 'provider_actual_outcomes_v2'
        )
        after = conn.execute(
            'SELECT outcome_id,row_sequence,row_sha256 FROM provider_actual_outcomes_v2'
        ).fetchone()
        assert tuple(before) == tuple(after)
        assert verify_actual_outcome_chain(conn) == []
        foreign_key_targets = {
            str(row['table'])
            for row in conn.execute(
                'PRAGMA foreign_key_list(provider_forecast_outcome_links_v2)'
            ).fetchall()
        }
        assert 'provider_actual_outcomes_v2' in foreign_key_targets

        second = _actual_row(
            fiscal_period_end='2021-03-31', actual_value=0.3038
        )
        assert append_actual_outcomes(conn, [second]) == (1, 0)
        assert verify_actual_outcome_chain(conn) == []
    finally:
        conn.close()
