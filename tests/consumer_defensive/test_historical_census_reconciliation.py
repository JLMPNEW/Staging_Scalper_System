from __future__ import annotations

import sqlite3

import pytest

from consumer_defensive.core.historical_census_reconciliation import (
    reconcile_historical_candidates,
)


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
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
           INSERT INTO dim_security VALUES(10,20,'KO');
           INSERT INTO dim_identifier VALUES('norgate_assetid','asset-ko',10);"""
    )
    return conn


def test_reconciliation_matches_asset_id_and_keeps_unloaded_review_only() -> None:
    conn = _connection()
    rows, summary = reconcile_historical_candidates(
        conn,
        [
            {
                "provider_symbol": "KO",
                "provider_asset_id": "asset-ko",
                "pit_index_membership_overlap_flag": "1",
            },
            {
                "provider_symbol": "OLD",
                "provider_asset_id": "asset-old",
                "pit_index_membership_overlap_flag": "1",
            },
            {
                "provider_symbol": "NOPE",
                "provider_asset_id": "asset-nope",
                "pit_index_membership_overlap_flag": "0",
            },
        ],
    )
    by_symbol = {row["provider_symbol"]: row for row in rows}
    assert by_symbol["KO"]["loaded_identity_match_flag"] == 1
    assert by_symbol["KO"]["loaded_ticker"] == "KO"
    assert by_symbol["OLD"]["unloaded_candidate_review_flag"] == 1
    assert by_symbol["OLD"]["production_or_calibration_use_allowed"] == 0
    assert by_symbol["NOPE"]["unloaded_candidate_review_flag"] == 0
    assert summary["pit_membership_overlap_count"] == 2
    assert summary["already_loaded_identity_count"] == 1
    assert summary["loaded_pit_overlap_identity_count"] == 1
    assert summary["unloaded_candidate_review_count"] == 1
    assert summary["unloaded_candidate_count_by_industry"] == {
        "UNCLASSIFIED": 1
    }
    assert summary["unloaded_candidate_count_by_catalog_status"] == {
        "UNKNOWN": 1
    }
    assert summary["survivorship_corrected_panel_ready"] is False
    assert summary["database_write_count"] == 0


def test_reconciliation_rejects_duplicate_candidate_asset_ids() -> None:
    with pytest.raises(RuntimeError, match="duplicate Norgate asset IDs"):
        reconcile_historical_candidates(
            _connection(),
            [
                {
                    "provider_asset_id": "asset-old",
                    "pit_index_membership_overlap_flag": "1",
                },
                {
                    "provider_asset_id": "asset-old",
                    "pit_index_membership_overlap_flag": "1",
                },
            ],
        )
