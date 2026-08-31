from __future__ import annotations

import sqlite3

from consumer_defensive.core.historical_census_reconciliation_v4 import (
    reconcile_historical_candidates_v4,
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


def test_unqueried_loaded_identity_is_unknown_not_negative_overlap() -> None:
    rows, summary = reconcile_historical_candidates_v4(_connection(), [])
    assert len(rows) == 1
    core = rows[0]
    assert core['provider_asset_id'] == '210813'
    assert core['pit_index_membership_overlap_flag'] is None
    assert core['pit_index_membership_query_status'] == (
        'not_queried_for_loaded_identity_absent_discovery'
    )
    assert summary['pit_membership_observed_identity_count'] == 0
    assert summary['pit_membership_overlap_count'] == 0
    assert summary['unqueried_loaded_identity_count'] == 1
    assert summary['unqueried_loaded_asset_ids'] == ['210813']
    assert summary['survivorship_corrected_panel_ready'] is False


def test_reviewed_core_overlap_is_observed_but_taxonomy_stays_unverified() -> None:
    rows, summary = reconcile_historical_candidates_v4(
        _connection(),
        [],
        reviewed_pit_overrides=[{
            'provider_asset_id': '210813',
            'provider_symbol': 'CORE-202109',
            'pit_overlap_start': '2019-01-02',
            'pit_overlap_end': '2021-09-01',
            'pit_session_count': 673,
            'pit_index_memberships': [
                'Nasdaq Composite', 'Russell 3000', 'S&P 1500'
            ],
            'current_or_final_sector': 'Consumer Discretionary',
            'review_source': 'norgate_local_set_tieout_2026-08-25',
            'reviewed_flag': True,
        }],
    )
    core = rows[0]
    assert core['pit_index_membership_overlap_flag'] == 1
    assert core['pit_index_membership_query_status'] == (
        'verified_by_reviewed_pit_override'
    )
    assert core['point_in_time_taxonomy_verified'] == 0
    assert core['production_or_calibration_use_allowed'] == 0
    assert summary['pit_membership_observed_identity_count'] == 1
    assert summary['pit_membership_overlap_count'] == 1
    assert summary['unqueried_loaded_identity_count'] == 0
    assert summary['survivorship_corrected_panel_ready'] is False
