from __future__ import annotations

import hashlib
import json
import sqlite3
from itertools import count
from typing import Any

from .db import utc_now


STAGE6A_SCHEMA_VERSION = 1
STAGE6A_MIGRATION_NAME = 'atomic_scoring_feature_contract_v1'

INPUT_COLUMNS: dict[str, str] = {
    'source_id': "TEXT NOT NULL DEFAULT 'consumer_defensive_scoring_contract'",
    'feature_status': "TEXT NOT NULL DEFAULT 'unbuilt'",
    'calibration_eligible_flag': 'INTEGER NOT NULL DEFAULT 0 CHECK(calibration_eligible_flag IN (0,1))',
    'core_available_component_count': 'INTEGER NOT NULL DEFAULT 0',
    'core_missing_component_count': 'INTEGER NOT NULL DEFAULT 0',
    'core_data_quality_confidence': 'REAL',
    'full_data_quality_confidence': 'REAL',
    'definition_version': "TEXT NOT NULL DEFAULT 'consumer_defensive_scoring_features_v1'",
    'contract_sha256': "TEXT NOT NULL DEFAULT ''",
    'lineage_json': "TEXT NOT NULL DEFAULT '{}'",
    'input_observation_id': "TEXT NOT NULL DEFAULT ''",
}

COMPONENT_COLUMNS: dict[str, str] = {
    'component_group': "TEXT NOT NULL DEFAULT 'unclassified'",
    'direction': "TEXT NOT NULL DEFAULT 'higher'",
    'rank_requirement': "TEXT NOT NULL DEFAULT 'optional'",
    'unit': "TEXT NOT NULL DEFAULT 'unknown'",
    'definition_version': "TEXT NOT NULL DEFAULT 'consumer_defensive_scoring_features_v1'",
    'contract_sha256': "TEXT NOT NULL DEFAULT ''",
    'source_id': 'TEXT',
    'source_table': "TEXT NOT NULL DEFAULT ''",
    'source_field': "TEXT NOT NULL DEFAULT ''",
    'exclusion_reason': 'TEXT',
    'lineage_json': "TEXT NOT NULL DEFAULT '{}'",
    'component_observation_id': "TEXT NOT NULL DEFAULT ''",
    'production_status': "TEXT NOT NULL DEFAULT 'research_candidate'",
}

SCHEMA_STATEMENTS = (
    '''
    CREATE TABLE IF NOT EXISTS stage6a_schema_migrations (
        migration_version INTEGER PRIMARY KEY,
        migration_name TEXT NOT NULL UNIQUE,
        migration_sha256 TEXT NOT NULL,
        applied_at TEXT NOT NULL
    )
    ''',
    '''
    CREATE TABLE IF NOT EXISTS stage6a_component_contract (
        component_name TEXT PRIMARY KEY,
        component_group TEXT NOT NULL,
        source_table TEXT NOT NULL,
        source_field TEXT NOT NULL,
        direction TEXT NOT NULL CHECK(direction IN ('higher','lower','none')),
        rank_requirement TEXT NOT NULL
            CHECK(rank_requirement IN ('required','any_financial','any_short','optional','specialized')),
        unit TEXT NOT NULL,
        production_status TEXT NOT NULL,
        definition_version TEXT NOT NULL,
        contract_sha256 TEXT NOT NULL,
        description TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    ''',
    '''
    CREATE UNIQUE INDEX IF NOT EXISTS idx_stage6a_input_observation
        ON feature_scoring_input(input_observation_id)
        WHERE input_observation_id <> ''
    ''',
    '''
    CREATE UNIQUE INDEX IF NOT EXISTS idx_stage6a_component_observation
        ON feature_scoring_component(component_observation_id)
        WHERE component_observation_id <> ''
    ''',
    '''
    CREATE INDEX IF NOT EXISTS idx_stage6a_component_asof_group
        ON feature_scoring_component(asof_date, component_group, component_name)
    ''',
)


def _manifest() -> dict[str, Any]:
    return {
        'version': STAGE6A_SCHEMA_VERSION,
        'name': STAGE6A_MIGRATION_NAME,
        'input_columns': INPUT_COLUMNS,
        'component_columns': COMPONENT_COLUMNS,
        'statements': [' '.join(statement.split()) for statement in SCHEMA_STATEMENTS],
    }


STAGE6A_MIGRATION_SHA256 = hashlib.sha256(
    json.dumps(_manifest(), sort_keys=True, separators=(',', ':')).encode('utf-8')
).hexdigest()

_SAVEPOINT_SEQUENCE = count(1)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f'PRAGMA table_info({table})')}


def ensure_stage6a_schema(conn: sqlite3.Connection) -> None:
    """Apply the additive Stage 6A schema atomically and verify its ledger."""

    nested = conn.in_transaction
    savepoint = f'stage6a_schema_{next(_SAVEPOINT_SEQUENCE)}'
    if nested:
        conn.execute(f'SAVEPOINT {savepoint}')
    else:
        conn.execute('BEGIN IMMEDIATE')
    try:
        conn.execute(SCHEMA_STATEMENTS[0])
        ledger = conn.execute(
            '''
            SELECT migration_name,migration_sha256
            FROM stage6a_schema_migrations
            WHERE migration_version=?
            ''',
            (STAGE6A_SCHEMA_VERSION,),
        ).fetchone()
        if ledger is not None and (
            str(ledger[0]) != STAGE6A_MIGRATION_NAME
            or str(ledger[1]) != STAGE6A_MIGRATION_SHA256
        ):
            raise RuntimeError('Stage 6A migration ledger checksum mismatch.')

        for table, definitions in (
            ('feature_scoring_input', INPUT_COLUMNS),
            ('feature_scoring_component', COMPONENT_COLUMNS),
        ):
            present = _columns(conn, table)
            if not present:
                raise RuntimeError(f'Stage 6A foundation table is missing: {table}')
            for column, ddl in definitions.items():
                if column not in present:
                    conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {ddl}')

        for statement in SCHEMA_STATEMENTS[1:]:
            conn.execute(statement)

        if ledger is None:
            conn.execute(
                '''
                INSERT INTO stage6a_schema_migrations(
                    migration_version,migration_name,migration_sha256,applied_at
                ) VALUES (?,?,?,?)
                ''',
                (
                    STAGE6A_SCHEMA_VERSION,
                    STAGE6A_MIGRATION_NAME,
                    STAGE6A_MIGRATION_SHA256,
                    utc_now(),
                ),
            )

        for table, expected in (
            ('feature_scoring_input', set(INPUT_COLUMNS)),
            ('feature_scoring_component', set(COMPONENT_COLUMNS)),
        ):
            missing = expected - _columns(conn, table)
            if missing:
                raise RuntimeError(
                    f'Stage 6A schema postcondition failed for {table}: {sorted(missing)}'
                )
        if conn.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise RuntimeError('Stage 6A schema migration introduced a foreign-key violation.')

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
