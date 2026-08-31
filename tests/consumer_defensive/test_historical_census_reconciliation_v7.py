from __future__ import annotations

import sqlite3

import pytest

from consumer_defensive.core.historical_census_reconciliation_v5 import (
    reviewed_pit_override_sha256,
)
from consumer_defensive.core.historical_census_reconciliation_v7 import (
    reconcile_historical_candidates_v7,
)


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(':memory:')
    conn.executescript(
        """CREATE TABLE dim_security(
               security_id INTEGER PRIMARY KEY,
               company_id INTEGER NOT NULL,
               ticker TEXT NOT NULL
           );
           CREATE TABLE dim_identifier(
               identifier_type TEXT NOT NULL,
               identifier_value TEXT NOT NULL,
               security_id INTEGER
           );
           INSERT INTO dim_security VALUES(112,112,'CORE');
           INSERT INTO dim_identifier
               VALUES('norgate_assetid','210813',112);"""
    )
    return conn


def _override() -> dict[str, object]:
    row: dict[str, object] = {
        'schema_version': 'consumer_defensive_reviewed_pit_override_v1',
        'provider_asset_id': '210813',
        'provider_symbol': 'CORE-202109',
        'loaded_ticker': 'CORE',
        'loaded_company_id': 112,
        'loaded_security_id': 112,
        'delisted_date': '2021-09-10',
        'pit_overlap_verified_flag': True,
        'pit_overlap_start': '2019-01-02',
        'pit_overlap_end': '2021-09-01',
        'pit_session_count': 673,
        'pit_index_memberships': [
            'nasdaq_composite', 'russell_3000', 'sp_composite_1500'
        ],
        'current_or_final_sector': 'Consumer Discretionary',
        'current_or_final_industry': 'Distributors',
        'local_norgate_snapshot_asof_date': '2026-08-25',
        'reviewed_at_date': '2026-08-25',
        'review_source': 'test',
        'review_rationale': 'Index exit verified before later delisting.',
        'reviewed_flag': True,
        'point_in_time_taxonomy_verified': False,
        'taxonomy_review_required': True,
        'production_or_calibration_use_allowed': False,
    }
    row['record_sha256'] = reviewed_pit_override_sha256(row)
    return row


def test_native_exit_before_delisting_preserves_exact_review_hash() -> None:
    override = _override()
    expected_hash = override['record_sha256']
    rows, summary = reconcile_historical_candidates_v7(
        _connection(), [], reviewed_pit_overrides=[override]
    )
    core = rows[0]
    assert core['pit_overlap_end'] == '2021-09-01'
    assert core['delisted_date'] == '2021-09-10'
    assert core['record_sha256'] == expected_hash
    assert summary['reviewed_override_record_sha256s'] == [expected_hash]
    assert summary['reviewed_record_hash_preserved_end_to_end_flag'] == 1
    assert summary['survivorship_corrected_panel_ready'] is False


def test_native_exit_equal_to_delisting_passes() -> None:
    override = _override()
    override['delisted_date'] = '2021-09-01'
    override['record_sha256'] = reviewed_pit_override_sha256(override)
    rows, _summary = reconcile_historical_candidates_v7(
        _connection(), [], reviewed_pit_overrides=[override]
    )
    assert rows[0]['record_sha256'] == override['record_sha256']


def test_native_exit_after_delisting_fails() -> None:
    override = _override()
    override['delisted_date'] = '2021-08-31'
    override['record_sha256'] = reviewed_pit_override_sha256(override)
    with pytest.raises(ValueError, match='fail-closed invariants'):
        reconcile_historical_candidates_v7(
            _connection(), [], reviewed_pit_overrides=[override]
        )
