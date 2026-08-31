from __future__ import annotations

import sqlite3

from consumer_defensive.core.historical_census_reconciliation_v3 import (
    reconcile_historical_candidates_v3,
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


def test_reviewed_core_overlap_is_retained_without_taxonomy_assertion() -> None:
    rows, summary = reconcile_historical_candidates_v3(
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
    assert len(rows) == 1
    core = rows[0]
    assert core['loaded_ticker'] == 'CORE'
    assert core['provider_symbol'] == 'CORE-202109'
    assert core['pit_index_membership_overlap_flag'] == 1
    assert core['pit_session_count'] == 673
    assert core['reviewed_pit_membership_override_flag'] == 1
    assert core['point_in_time_taxonomy_verified'] == 0
    assert core['taxonomy_review_required'] == 1
    assert core['production_or_calibration_use_allowed'] == 0
    assert summary['candidate_input_count'] == 0
    assert summary['reconciled_union_count'] == 1
    assert summary['reviewed_pit_override_count'] == 1
    assert summary['survivorship_corrected_panel_ready'] is False
