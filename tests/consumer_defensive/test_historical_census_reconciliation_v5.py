from __future__ import annotations

import sqlite3

import pytest

from consumer_defensive.core.historical_census_reconciliation_v5 import (
    reconcile_historical_candidates_v5,
    reviewed_pit_override_sha256,
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
        'delisted_date': '2021-09-01',
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
        'review_source': 'norgate_local_set_tieout_2026-08-25',
        'review_rationale': (
            'Loaded identity absent from discovery; PIT overlap verified. '
            'PIT Consumer Defensive taxonomy is not inferred.'
        ),
        'reviewed_flag': True,
        'point_in_time_taxonomy_verified': False,
        'taxonomy_review_required': True,
        'production_or_calibration_use_allowed': False,
    }
    row['record_sha256'] = reviewed_pit_override_sha256(row)
    return row


def test_unqueried_loaded_identity_is_unknown_not_verified_negative() -> None:
    rows, summary = reconcile_historical_candidates_v5(_connection(), [])
    assert rows[0]['pit_index_membership_overlap_flag'] is None
    assert rows[0]['pit_index_membership_query_status'] == (
        'not_queried_for_loaded_identity_absent_discovery'
    )
    assert summary['pit_membership_observed_identity_count'] == 0
    assert summary['unqueried_loaded_identity_count'] == 1


def test_csv_string_overlap_is_normalized_and_counted() -> None:
    rows, summary = reconcile_historical_candidates_v5(
        _connection(),
        [{
            'provider_asset_id': '210813',
            'provider_symbol': 'CORE-202109',
            'pit_index_membership_overlap_flag': '1',
            'pit_index_membership_query_error': '',
        }],
    )
    assert rows[0]['pit_index_membership_overlap_flag'] == 1
    assert summary['pit_membership_observed_identity_count'] == 1
    assert summary['pit_membership_overlap_count'] == 1


def test_query_error_is_unknown_and_counted_separately() -> None:
    rows, summary = reconcile_historical_candidates_v5(
        _connection(),
        [{
            'provider_asset_id': '210813',
            'provider_symbol': 'CORE-202109',
            'pit_index_membership_overlap_flag': '0',
            'pit_index_membership_query_error': 'provider timeout',
        }],
    )
    assert rows[0]['pit_index_membership_overlap_flag'] is None
    assert rows[0]['pit_index_membership_query_status'] == 'query_error'
    assert summary['pit_membership_query_error_count'] == 1
    assert summary['pit_membership_observed_identity_count'] == 0


def test_strict_review_override_preserves_provenance_and_stays_fail_closed() -> None:
    override = _override()
    rows, summary = reconcile_historical_candidates_v5(
        _connection(), [], reviewed_pit_overrides=[override]
    )
    core = rows[0]
    assert core['pit_index_membership_overlap_flag'] == 1
    assert core['pit_index_membership_query_status'] == (
        'verified_by_reviewed_pit_override'
    )
    assert core['local_norgate_snapshot_asof_date'] == '2026-08-25'
    assert core['review_rationale'] == override['review_rationale']
    assert core['point_in_time_taxonomy_verified'] is False
    assert core['taxonomy_review_required'] is True
    assert core['production_or_calibration_use_allowed'] is False
    assert summary['reviewed_override_record_sha256s'] == [
        override['record_sha256']
    ]
    assert summary['survivorship_corrected_panel_ready'] is False


def test_tampered_or_unknown_override_fields_fail_closed() -> None:
    tampered = _override()
    tampered['pit_session_count'] = 674
    with pytest.raises(ValueError, match='SHA-256 mismatch'):
        reconcile_historical_candidates_v5(
            _connection(), [], reviewed_pit_overrides=[tampered]
        )
    unknown = _override()
    unknown['unreviewed_claim'] = True
    with pytest.raises(ValueError, match='strict schema'):
        reconcile_historical_candidates_v5(
            _connection(), [], reviewed_pit_overrides=[unknown]
        )
