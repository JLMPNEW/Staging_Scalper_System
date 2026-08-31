from __future__ import annotations

import csv
import importlib.util
import sqlite3
from pathlib import Path

from consumer_defensive.core.historical_census_reconciliation_v2 import (
    reconcile_historical_candidates_v2,
)


ROOT = Path(__file__).resolve().parents[2]


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
           INSERT INTO dim_security VALUES(10,20,'KO');
           INSERT INTO dim_security VALUES(11,21,'PG');
           INSERT INTO dim_identifier VALUES('norgate_assetid','asset-ko',10);
           INSERT INTO dim_identifier VALUES('norgate_assetid','asset-pg',11);"""
    )
    return conn


def _script_module():
    path = (
        ROOT / 'consumer_defensive' / 'scripts'
        / '00f_reconcile_historical_candidate_census_v2.py'
    )
    spec = importlib.util.spec_from_file_location('census_00f_v2', path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reconciled_union_appends_loaded_identity_missing_from_discovery() -> None:
    rows, summary = reconcile_historical_candidates_v2(
        _connection(),
        [{
            'provider_symbol': 'KO',
            'provider_asset_id': 'asset-ko',
            'pit_index_membership_overlap_flag': '1',
            'candidate_only_field': 'preserved',
        }],
    )
    assert len(rows) == 2
    by_asset = {str(row['provider_asset_id']): row for row in rows}
    appended = by_asset['asset-pg']
    assert appended['provider_symbol'] == 'PG'
    assert appended['candidate_discovery_present_flag'] == 0
    assert appended['loaded_identity_missing_from_candidate_input_flag'] == 1
    assert appended['taxonomy_review_required'] == 1
    assert appended['production_or_calibration_use_allowed'] == 0
    assert appended['reconciliation_status'].startswith(
        'loaded_identity_absent_from_candidate_discovery'
    )
    assert summary['candidate_input_count'] == 1
    assert summary['reconciled_union_count'] == 2
    assert summary['missing_loaded_identity_count'] == 1
    assert summary['missing_loaded_asset_ids'] == ['asset-pg']
    assert summary['missing_loaded_identities'] == [{
        'provider_asset_id': 'asset-pg',
        'provider_symbol': 'PG',
        'loaded_ticker': 'PG',
        'loaded_company_id': 21,
        'loaded_security_id': 11,
    }]
    assert summary['survivorship_corrected_panel_ready'] is False


def test_v2_csv_export_uses_union_of_all_row_keys(tmp_path: Path) -> None:
    rows, _summary = reconcile_historical_candidates_v2(
        _connection(),
        [{
            'provider_symbol': 'KO',
            'provider_asset_id': 'asset-ko',
            'pit_index_membership_overlap_flag': '1',
            'candidate_only_field': 'preserved',
        }],
    )
    path = tmp_path / 'union.csv'
    _script_module().write_csv(path, rows)
    with path.open('r', encoding='utf-8', newline='') as handle:
        reader = csv.DictReader(handle)
        exported = list(reader)
        fieldnames = set(reader.fieldnames or [])
    assert len(exported) == 2
    assert 'candidate_only_field' in fieldnames
    assert 'loaded_identity_missing_from_candidate_input_flag' in fieldnames
    assert 'taxonomy_review_required' in fieldnames
