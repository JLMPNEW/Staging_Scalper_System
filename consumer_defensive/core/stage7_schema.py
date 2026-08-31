from __future__ import annotations

import hashlib
import json
import sqlite3
from itertools import count
from typing import Any

from .db import utc_now


STAGE7_SCHEMA_VERSION = 1
STAGE7_MIGRATION_NAME = 'consumer_defensive_shadow_baseline_scoring_v1'

OUTPUT_COLUMNS: dict[str, str] = {
    'baseline_source_id': "TEXT NOT NULL DEFAULT ''",
    'baseline_input_observation_id': "TEXT NOT NULL DEFAULT ''",
    'calibration_cohort_id': "TEXT NOT NULL DEFAULT ''",
    'core_score': 'REAL',
    'final_percentile': 'REAL',
    'cohort_rank': 'INTEGER',
    'cohort_percentile': 'REAL',
    'data_quality_confidence': 'REAL',
    'full_data_quality_confidence': 'REAL',
    'rank_ready_flag': 'INTEGER NOT NULL DEFAULT 0 CHECK(rank_ready_flag IN (0,1))',
    'calibration_eligible_flag': (
        'INTEGER NOT NULL DEFAULT 0 CHECK(calibration_eligible_flag IN (0,1))'
    ),
    'model_status': "TEXT NOT NULL DEFAULT 'review_required'",
    'review_reason': 'TEXT',
    'component_weights_json': "TEXT NOT NULL DEFAULT '{}'",
    'component_scores_json': "TEXT NOT NULL DEFAULT '{}'",
    'component_quality_json': "TEXT NOT NULL DEFAULT '{}'",
    'model_contract_sha256': "TEXT NOT NULL DEFAULT ''",
    'lineage_json': "TEXT NOT NULL DEFAULT '{}'",
    'score_observation_id': "TEXT NOT NULL DEFAULT ''",
    'updated_at': "TEXT NOT NULL DEFAULT ''",
}

SCHEMA_STATEMENTS = (
    '''
    CREATE TABLE IF NOT EXISTS stage7_schema_migrations (
        migration_version INTEGER PRIMARY KEY,
        migration_name TEXT NOT NULL UNIQUE,
        migration_sha256 TEXT NOT NULL,
        applied_at TEXT NOT NULL
    )
    ''',
    '''
    CREATE TABLE IF NOT EXISTS stage7_model_contract (
        source_id TEXT PRIMARY KEY,
        model_family TEXT NOT NULL CHECK(model_family='consumer_defensive'),
        model_version TEXT NOT NULL UNIQUE,
        baseline_source_id TEXT NOT NULL,
        contract_sha256 TEXT NOT NULL UNIQUE,
        promotion_state TEXT NOT NULL CHECK(promotion_state='shadow_monitor'),
        neutral_score REAL NOT NULL,
        minimum_data_quality_confidence REAL NOT NULL,
        maximum_missing_component_weight REAL NOT NULL,
        minimum_rank_ready_fraction REAL NOT NULL,
        specialized_weight_policy TEXT NOT NULL,
        factor_validation_campaign_id TEXT NOT NULL,
        factor_validation_verdict TEXT NOT NULL,
        component_weights_json TEXT NOT NULL,
        contract_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(source_id) REFERENCES source_registry(source_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(baseline_source_id) REFERENCES source_registry(source_id)
            ON DELETE RESTRICT
    )
    ''',
    '''
    CREATE TABLE IF NOT EXISTS stage7_component_weight_contract (
        source_id TEXT NOT NULL,
        model_family TEXT NOT NULL CHECK(model_family='consumer_defensive'),
        model_version TEXT NOT NULL,
        calibration_cohort_id TEXT NOT NULL DEFAULT '*',
        component_name TEXT NOT NULL,
        component_group TEXT NOT NULL,
        component_weight REAL NOT NULL CHECK(component_weight>=0.0),
        weight_status TEXT NOT NULL,
        evidence_reference TEXT NOT NULL,
        contract_sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(
            source_id,model_family,model_version,
            calibration_cohort_id,component_name
        ),
        FOREIGN KEY(source_id) REFERENCES source_registry(source_id)
            ON DELETE RESTRICT
    )
    ''',
    '''
    CREATE TABLE IF NOT EXISTS stage7_score_snapshot (
        source_id TEXT NOT NULL,
        model_family TEXT NOT NULL CHECK(model_family='consumer_defensive'),
        model_version TEXT NOT NULL,
        asof_date TEXT NOT NULL,
        contract_sha256 TEXT NOT NULL,
        baseline_input_manifest_sha256 TEXT NOT NULL,
        output_manifest_sha256 TEXT NOT NULL,
        ticker_count INTEGER NOT NULL,
        rank_ready_count INTEGER NOT NULL,
        review_required_count INTEGER NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('shadow_complete')),
        created_at TEXT NOT NULL,
        PRIMARY KEY(source_id,model_family,asof_date),
        FOREIGN KEY(source_id) REFERENCES source_registry(source_id)
            ON DELETE RESTRICT
    )
    ''',
    '''
    CREATE UNIQUE INDEX IF NOT EXISTS idx_stage7_score_observation
        ON feature_scoring_model_output(score_observation_id)
        WHERE score_observation_id<>''
    ''',
    '''
    CREATE INDEX IF NOT EXISTS idx_stage7_output_asof_rank
        ON feature_scoring_model_output(
            source_id,model_family,asof_date,rank_ready_flag,final_rank
        )
    ''',
    '''
    CREATE INDEX IF NOT EXISTS idx_stage7_output_cohort_rank
        ON feature_scoring_model_output(
            source_id,model_family,asof_date,
            calibration_cohort_id,cohort_rank
        )
    ''',
)


def _manifest() -> dict[str, Any]:
    return {
        'version': STAGE7_SCHEMA_VERSION,
        'name': STAGE7_MIGRATION_NAME,
        'output_columns': OUTPUT_COLUMNS,
        'statements': [' '.join(statement.split()) for statement in SCHEMA_STATEMENTS],
    }


STAGE7_MIGRATION_SHA256 = hashlib.sha256(
    json.dumps(_manifest(), sort_keys=True, separators=(',', ':')).encode('utf-8')
).hexdigest()

_SAVEPOINT_SEQUENCE = count(1)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f'PRAGMA table_info({table})')}


def ensure_stage7_schema(conn: sqlite3.Connection) -> None:
    '''Apply the additive Stage 7 schema atomically and verify its ledger.'''

    nested = conn.in_transaction
    savepoint = f'stage7_schema_{next(_SAVEPOINT_SEQUENCE)}'
    if nested:
        conn.execute(f'SAVEPOINT {savepoint}')
    else:
        conn.execute('BEGIN IMMEDIATE')
    try:
        conn.execute(SCHEMA_STATEMENTS[0])
        ledger = conn.execute(
            '''SELECT migration_name,migration_sha256
               FROM stage7_schema_migrations WHERE migration_version=?''',
            (STAGE7_SCHEMA_VERSION,),
        ).fetchone()
        if ledger is not None and (
            str(ledger[0]) != STAGE7_MIGRATION_NAME
            or str(ledger[1]) != STAGE7_MIGRATION_SHA256
        ):
            raise RuntimeError('Stage 7 migration ledger checksum mismatch.')

        present = _columns(conn, 'feature_scoring_model_output')
        if not present:
            raise RuntimeError(
                'Stage 7 foundation table is missing: '
                'feature_scoring_model_output'
            )
        for column, ddl in OUTPUT_COLUMNS.items():
            if column not in present:
                conn.execute(
                    'ALTER TABLE feature_scoring_model_output '
                    f'ADD COLUMN {column} {ddl}'
                )

        for statement in SCHEMA_STATEMENTS[1:]:
            conn.execute(statement)

        if ledger is None:
            conn.execute(
                '''INSERT INTO stage7_schema_migrations(
                       migration_version,migration_name,migration_sha256,applied_at
                   ) VALUES (?,?,?,?)''',
                (
                    STAGE7_SCHEMA_VERSION,
                    STAGE7_MIGRATION_NAME,
                    STAGE7_MIGRATION_SHA256,
                    utc_now(),
                ),
            )

        missing = set(OUTPUT_COLUMNS) - _columns(
            conn, 'feature_scoring_model_output'
        )
        if missing:
            raise RuntimeError(
                'Stage 7 schema postcondition failed for model output: '
                f'{sorted(missing)}'
            )
        required_tables = {
            'stage7_schema_migrations',
            'stage7_model_contract',
            'stage7_component_weight_contract',
            'stage7_score_snapshot',
        }
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not required_tables.issubset(tables):
            raise RuntimeError(
                'Stage 7 schema postcondition failed: '
                f'missing={sorted(required_tables - tables)}'
            )
        if conn.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise RuntimeError(
                'Stage 7 schema migration introduced a foreign-key violation.'
            )

        if nested:
            conn.execute(f'RELEASE SAVEPOINT {savepoint}')
        else:
            conn.commit()
    except BaseException:
        if nested:
            conn.execute(f'ROLLBACK TO SAVEPOINT {savepoint}')
            conn.execute(f'RELEASE SAVEPOINT {savepoint}')
        elif conn.in_transaction:
            conn.rollback()
        raise
