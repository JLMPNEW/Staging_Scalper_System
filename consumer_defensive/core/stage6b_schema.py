from __future__ import annotations

import hashlib
import json
import sqlite3
from itertools import count
from typing import Any

from .db import utc_now


STAGE6B_SCHEMA_VERSION = 6
STAGE6B_MIGRATION_NAME = 'historical_inventory_and_coverage_v2'
STAGE6B_V3_MIGRATION_NAME = 'immutable_historical_document_snapshot_v3'
STAGE6B_V4_MIGRATION_NAME = 'immutable_measurement_observation_history_v4'
STAGE6B_V5_MIGRATION_NAME = 'event_exhibit_inventory_and_coverage_status_v5'
STAGE6B_V6_MIGRATION_NAME = 'metric_history_depth_and_diagnostics_v6'

OBSERVATION_COLUMNS: dict[str, str] = {
    'confidence': 'REAL',
    'extraction_method': "TEXT NOT NULL DEFAULT ''",
    'scope': "TEXT NOT NULL DEFAULT 'unknown'",
    'lineage_json': "TEXT NOT NULL DEFAULT '{}'",
    'observation_sha256': "TEXT NOT NULL DEFAULT ''",
    'production_status': "TEXT NOT NULL DEFAULT 'measurement_only'",
    'parser_run_id': 'INTEGER',
}

SCHEMA_STATEMENTS = (
    '''
    CREATE TABLE IF NOT EXISTS stage6b_schema_migrations (
        migration_version INTEGER PRIMARY KEY,
        migration_name TEXT NOT NULL UNIQUE,
        migration_sha256 TEXT NOT NULL,
        applied_at TEXT NOT NULL
    )
    ''',
    '''
    CREATE TABLE IF NOT EXISTS stage6b_metric_policy (
        metric_id TEXT PRIMARY KEY,
        adapter_version TEXT NOT NULL,
        registry_version TEXT NOT NULL,
        term_registry_version TEXT NOT NULL,
        unit_family TEXT NOT NULL,
        cohorts_json TEXT NOT NULL,
        applicability_subtypes_json TEXT NOT NULL,
        terms_json TEXT NOT NULL,
        production_status TEXT NOT NULL CHECK(production_status='measurement_only'),
        production_weight REAL NOT NULL DEFAULT 0.0 CHECK(production_weight=0.0),
        policy_sha256 TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(metric_id) REFERENCES dim_specialized_metric(metric_id)
            ON DELETE RESTRICT
    )
    ''',
    '''
    CREATE TABLE IF NOT EXISTS stage6b_document_inventory (
        asof_date TEXT NOT NULL,
        ticker TEXT NOT NULL,
        accession_number TEXT NOT NULL,
        document_name TEXT NOT NULL,
        form_type TEXT NOT NULL,
        filing_date TEXT,
        accepted_at TEXT NOT NULL,
        report_date TEXT,
        content_sha256 TEXT NOT NULL,
        bytes INTEGER NOT NULL,
        seal_manifest_sha256 TEXT NOT NULL,
        ingestion_config_sha256 TEXT NOT NULL,
        issuer_scope_sha256 TEXT NOT NULL,
        requested_metrics_json TEXT NOT NULL,
        inventory_status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(asof_date,ticker,accession_number,document_name)
    )
    ''',
    '''
    CREATE TABLE IF NOT EXISTS stage6b_specialized_run (
        stage6b_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
        asof_date TEXT NOT NULL,
        parser_run_id INTEGER,
        adapter_version TEXT NOT NULL,
        policy_sha256 TEXT NOT NULL,
        source_manifest_sha256 TEXT NOT NULL,
        seal_manifest_sha256 TEXT NOT NULL,
        ingestion_config_sha256 TEXT NOT NULL,
        issuer_scope_sha256 TEXT NOT NULL,
        started_at TEXT NOT NULL,
        completed_at TEXT,
        status TEXT NOT NULL,
        inventory_document_count INTEGER NOT NULL DEFAULT 0,
        accepted_observation_count INTEGER NOT NULL DEFAULT 0,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        UNIQUE(asof_date,adapter_version,source_manifest_sha256)
    )
    ''',
    '''
    CREATE TABLE IF NOT EXISTS stage6b_metric_coverage (
        stage6b_run_id INTEGER NOT NULL,
        scope_name TEXT NOT NULL,
        cohort_id TEXT NOT NULL,
        applicability_subtype TEXT NOT NULL,
        metric_id TEXT NOT NULL,
        applicable_issuer_count INTEGER NOT NULL,
        hydrated_document_issuer_count INTEGER NOT NULL,
        census_term_hit_issuer_count INTEGER NOT NULL,
        parser_candidate_issuer_count INTEGER NOT NULL,
        parser_accepted_issuer_count INTEGER NOT NULL,
        measurement_issuer_count INTEGER NOT NULL,
        review_required_issuer_count INTEGER NOT NULL,
        rejected_issuer_count INTEGER NOT NULL,
        parser_failure_issuer_count INTEGER NOT NULL,
        measurement_coverage REAL NOT NULL,
        coverage_tier TEXT NOT NULL,
        recommended_action TEXT NOT NULL,
        uncovered_tickers_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(
            stage6b_run_id,scope_name,cohort_id,applicability_subtype,metric_id
        ),
        FOREIGN KEY(stage6b_run_id)
            REFERENCES stage6b_specialized_run(stage6b_run_id) ON DELETE CASCADE,
        FOREIGN KEY(metric_id) REFERENCES dim_specialized_metric(metric_id)
            ON DELETE RESTRICT
    )
    ''',
    '''
    CREATE UNIQUE INDEX IF NOT EXISTS idx_stage6b_observation_sha
        ON fact_specialized_metric_observation(observation_sha256)
        WHERE observation_sha256<>''
    ''',
    '''
    CREATE INDEX IF NOT EXISTS idx_stage6b_observation_pit
        ON fact_specialized_metric_observation(
            ticker,metric_id,accepted_at,period_end,production_status
        )
    ''',
    '''
    CREATE INDEX IF NOT EXISTS idx_stage6b_inventory_ticker_metric
        ON stage6b_document_inventory(asof_date,ticker,form_type)
    ''',
    '''
    CREATE INDEX IF NOT EXISTS idx_stage6b_coverage_metric
        ON stage6b_metric_coverage(stage6b_run_id,metric_id,scope_name)
    ''',
)

MIGRATION_V2_STATEMENTS = (
    '''
    CREATE TABLE IF NOT EXISTS stage6b_historical_inventory_run (
        inventory_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
        generated_asof TEXT NOT NULL,
        history_start TEXT NOT NULL,
        history_end TEXT NOT NULL,
        maximum_documents_per_issuer INTEGER NOT NULL,
        selection_policy_sha256 TEXT NOT NULL,
        status TEXT NOT NULL,
        replay_cutoff_count INTEGER NOT NULL DEFAULT 0,
        target_filing_count INTEGER NOT NULL DEFAULT 0,
        uncovered_target_count INTEGER NOT NULL DEFAULT 0,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        UNIQUE(
            generated_asof,history_start,history_end,
            maximum_documents_per_issuer,selection_policy_sha256
        )
    )
    ''',
    '''
    CREATE TABLE IF NOT EXISTS stage6b_historical_filing_inventory (
        inventory_run_id INTEGER NOT NULL,
        ticker TEXT NOT NULL,
        accession_number TEXT NOT NULL,
        form_type TEXT NOT NULL,
        form_family TEXT NOT NULL,
        filing_date TEXT,
        accepted_at TEXT NOT NULL,
        report_date TEXT,
        primary_document TEXT NOT NULL,
        replay_sequence INTEGER,
        replay_asof_date TEXT,
        capture_rank INTEGER,
        target_reason TEXT NOT NULL,
        existing_hydration_status TEXT NOT NULL,
        inventory_status TEXT NOT NULL,
        requires_index_discovery INTEGER NOT NULL DEFAULT 0
            CHECK(requires_index_discovery IN (0,1)),
        requested_metrics_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(inventory_run_id,ticker,accession_number),
        FOREIGN KEY(inventory_run_id)
            REFERENCES stage6b_historical_inventory_run(inventory_run_id)
            ON DELETE CASCADE
    )
    ''',
    '''
    CREATE INDEX IF NOT EXISTS idx_stage6b_historical_inventory_replay
        ON stage6b_historical_filing_inventory(
            inventory_run_id,replay_sequence,ticker,accepted_at
        )
    ''',
    '''
    CREATE INDEX IF NOT EXISTS idx_stage6b_historical_inventory_status
        ON stage6b_historical_filing_inventory(
            inventory_run_id,inventory_status,ticker
        )
    ''',
)

MIGRATION_V3_STATEMENTS = (
    '''
    CREATE TABLE IF NOT EXISTS stage6b_historical_document_snapshot_run (
        snapshot_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
        inventory_run_id INTEGER NOT NULL,
        asof_date TEXT NOT NULL,
        history_start TEXT NOT NULL,
        history_end TEXT NOT NULL,
        status TEXT NOT NULL,
        target_document_count INTEGER NOT NULL DEFAULT 0,
        hydrated_document_count INTEGER NOT NULL DEFAULT 0,
        manifest_sha256 TEXT NOT NULL DEFAULT '',
        manifest_json TEXT NOT NULL DEFAULT '[]',
        seal_relative_path TEXT NOT NULL DEFAULT '',
        ingestion_config_sha256 TEXT NOT NULL,
        issuer_scope_sha256 TEXT NOT NULL,
        started_at TEXT NOT NULL,
        completed_at TEXT,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        UNIQUE(inventory_run_id,asof_date),
        FOREIGN KEY(inventory_run_id)
            REFERENCES stage6b_historical_inventory_run(inventory_run_id)
            ON DELETE RESTRICT
    )
    ''',
    '''
    CREATE TABLE IF NOT EXISTS stage6b_historical_document_snapshot (
        snapshot_run_id INTEGER NOT NULL,
        ticker TEXT NOT NULL,
        accession_number TEXT NOT NULL,
        document_name TEXT NOT NULL,
        form_type TEXT NOT NULL,
        filing_date TEXT,
        accepted_at TEXT NOT NULL,
        report_date TEXT,
        archive_cik TEXT NOT NULL,
        company_currency TEXT NOT NULL,
        source_id TEXT NOT NULL,
        source_url TEXT NOT NULL,
        logical_path TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,
        bytes INTEGER NOT NULL,
        object_path TEXT NOT NULL,
        requested_metrics_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(snapshot_run_id,ticker,accession_number,document_name),
        FOREIGN KEY(snapshot_run_id)
            REFERENCES stage6b_historical_document_snapshot_run(snapshot_run_id)
            ON DELETE CASCADE
    )
    ''',
    '''
    CREATE INDEX IF NOT EXISTS idx_stage6b_historical_document_pit
        ON stage6b_historical_document_snapshot(
            snapshot_run_id,ticker,accepted_at,accession_number
        )
    ''',
)

MIGRATION_V4_OBSERVATION_TABLE_SQL = '''
CREATE TABLE fact_specialized_metric_observation (
    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    metric_id TEXT NOT NULL,
    period_start TEXT,
    period_end TEXT NOT NULL,
    accepted_at TEXT NOT NULL,
    numeric_value REAL,
    unit TEXT,
    definition_version TEXT NOT NULL,
    applicability_status TEXT NOT NULL,
    evidence_status TEXT NOT NULL,
    evidence_key TEXT,
    source_id TEXT NOT NULL,
    source_document TEXT,
    created_at TEXT NOT NULL,
    confidence REAL,
    extraction_method TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT 'unknown',
    lineage_json TEXT NOT NULL DEFAULT '{}',
    observation_sha256 TEXT NOT NULL DEFAULT '',
    production_status TEXT NOT NULL DEFAULT 'measurement_only',
    parser_run_id INTEGER,
    FOREIGN KEY(metric_id) REFERENCES dim_specialized_metric(metric_id)
        ON DELETE RESTRICT,
    FOREIGN KEY(source_id) REFERENCES source_registry(source_id)
        ON DELETE RESTRICT
)
'''

MIGRATION_V5_STATEMENTS = (
    '''
    CREATE TABLE IF NOT EXISTS stage6b_event_document_snapshot_run (
        event_snapshot_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
        inventory_run_id INTEGER NOT NULL,
        asof_date TEXT NOT NULL,
        history_start TEXT NOT NULL,
        history_end TEXT NOT NULL,
        status TEXT NOT NULL,
        target_filing_count INTEGER NOT NULL DEFAULT 0,
        indexed_filing_count INTEGER NOT NULL DEFAULT 0,
        selected_document_count INTEGER NOT NULL DEFAULT 0,
        manifest_sha256 TEXT NOT NULL DEFAULT '',
        manifest_json TEXT NOT NULL DEFAULT '[]',
        seal_relative_path TEXT NOT NULL DEFAULT '',
        ingestion_config_sha256 TEXT NOT NULL,
        issuer_scope_sha256 TEXT NOT NULL,
        started_at TEXT NOT NULL,
        completed_at TEXT,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        UNIQUE(inventory_run_id,asof_date),
        FOREIGN KEY(inventory_run_id)
            REFERENCES stage6b_historical_inventory_run(inventory_run_id)
            ON DELETE RESTRICT
    )
    ''',
    '''
    CREATE TABLE IF NOT EXISTS stage6b_event_document_snapshot (
        event_snapshot_run_id INTEGER NOT NULL,
        ticker TEXT NOT NULL,
        accession_number TEXT NOT NULL,
        document_name TEXT NOT NULL,
        document_role TEXT NOT NULL,
        sec_document_type TEXT NOT NULL,
        document_sequence TEXT NOT NULL DEFAULT '',
        document_description TEXT NOT NULL DEFAULT '',
        content_type TEXT NOT NULL DEFAULT '',
        form_type TEXT NOT NULL,
        filing_date TEXT,
        accepted_at TEXT NOT NULL,
        report_date TEXT,
        archive_cik TEXT NOT NULL,
        company_currency TEXT NOT NULL,
        source_id TEXT NOT NULL,
        source_url TEXT NOT NULL,
        logical_path TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,
        bytes INTEGER NOT NULL,
        object_path TEXT NOT NULL,
        requested_metrics_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(
            event_snapshot_run_id,ticker,accession_number,document_name
        ),
        FOREIGN KEY(event_snapshot_run_id)
            REFERENCES stage6b_event_document_snapshot_run(event_snapshot_run_id)
            ON DELETE CASCADE
    )
    ''',
    '''
    CREATE INDEX IF NOT EXISTS idx_stage6b_event_document_pit
        ON stage6b_event_document_snapshot(
            event_snapshot_run_id,ticker,accepted_at,accession_number,
            document_role
        )
    ''',
    '''
    CREATE TABLE IF NOT EXISTS stage6b_metric_coverage_status (
        stage6b_run_id INTEGER NOT NULL,
        scope_name TEXT NOT NULL,
        cohort_id TEXT NOT NULL,
        applicability_subtype TEXT NOT NULL,
        metric_id TEXT NOT NULL,
        evidence_state TEXT NOT NULL,
        issuer_count INTEGER NOT NULL,
        tickers_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(
            stage6b_run_id,scope_name,cohort_id,applicability_subtype,
            metric_id,evidence_state
        ),
        FOREIGN KEY(stage6b_run_id)
            REFERENCES stage6b_specialized_run(stage6b_run_id) ON DELETE CASCADE,
        FOREIGN KEY(metric_id) REFERENCES dim_specialized_metric(metric_id)
            ON DELETE RESTRICT
    )
    ''',
    '''
    CREATE INDEX IF NOT EXISTS idx_stage6b_coverage_status_metric
        ON stage6b_metric_coverage_status(
            stage6b_run_id,metric_id,scope_name,evidence_state
        )
    ''',
)

MIGRATION_V6_STATEMENTS = (
    '''
    CREATE TABLE IF NOT EXISTS stage6b_metric_history_depth (
        stage6b_run_id INTEGER NOT NULL,
        scope_name TEXT NOT NULL,
        cohort_id TEXT NOT NULL,
        applicability_subtype TEXT NOT NULL,
        metric_id TEXT NOT NULL,
        measured_issuer_count INTEGER NOT NULL,
        observation_count INTEGER NOT NULL,
        issuer_period_count INTEGER NOT NULL,
        multi_period_issuer_count INTEGER NOT NULL,
        median_periods_per_measured_issuer REAL NOT NULL,
        earliest_period_end TEXT NOT NULL,
        latest_period_end TEXT NOT NULL,
        periods_per_issuer_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(
            stage6b_run_id,scope_name,cohort_id,applicability_subtype,metric_id
        ),
        FOREIGN KEY(stage6b_run_id)
            REFERENCES stage6b_specialized_run(stage6b_run_id) ON DELETE CASCADE,
        FOREIGN KEY(metric_id) REFERENCES dim_specialized_metric(metric_id)
            ON DELETE RESTRICT
    )
    ''',
    '''
    CREATE INDEX IF NOT EXISTS idx_stage6b_history_depth_metric
        ON stage6b_metric_history_depth(
            stage6b_run_id,metric_id,scope_name
        )
    ''',
)


def _manifest_v1() -> dict[str, Any]:
    return {
        'version': 1,
        'name': 'specialized_measurement_overlay_v1',
        'observation_columns': OBSERVATION_COLUMNS,
        'statements': [' '.join(statement.split()) for statement in SCHEMA_STATEMENTS],
    }


STAGE6B_V1_MIGRATION_SHA256 = hashlib.sha256(
    json.dumps(_manifest_v1(), sort_keys=True, separators=(',', ':')).encode('utf-8')
).hexdigest()
if STAGE6B_V1_MIGRATION_SHA256 != (
    'ed173d4bb4623c799c4bfc88a922d82305723bb1d8253d1d6093f4dcac5e0502'
):
    raise RuntimeError('Frozen Stage 6B v1 migration manifest changed.')

STAGE6B_MIGRATION_SHA256 = hashlib.sha256(
    json.dumps(
        {
            # Historical v2 was originally sealed while the module-level
            # schema version was 3. Keep that operation manifest immutable;
            # later migrations have their own independently frozen hashes.
            'version': 3,
            'name': STAGE6B_MIGRATION_NAME,
            'columns': {
                'dim_specialized_metric': {
                    'source_availability_class': 'TEXT NOT NULL'
                },
                'stage6b_metric_policy': {
                    'source_availability_class': 'TEXT NOT NULL'
                },
                'stage6b_metric_coverage': {
                    'source_availability_class': 'TEXT NOT NULL',
                    'denominator_kind': 'TEXT NOT NULL',
                },
            },
            'statements': [
                ' '.join(statement.split())
                for statement in MIGRATION_V2_STATEMENTS
            ],
        },
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
).hexdigest()
if STAGE6B_MIGRATION_SHA256 != (
    '5e5174ce4e7dce0ebf480ecf0bd7d81f29fd3643b34c94062a77658b47c2e98c'
):
    raise RuntimeError('Frozen Stage 6B v2 migration manifest changed.')

STAGE6B_V3_MIGRATION_SHA256 = hashlib.sha256(
    json.dumps(
        {
            'version': 3,
            'name': STAGE6B_V3_MIGRATION_NAME,
            'statements': [
                ' '.join(statement.split())
                for statement in MIGRATION_V3_STATEMENTS
            ],
            'contract': (
                'one_fetch_per_historical_primary_document;immutable_cas;'
                'exact_inventory_keyset;pit_by_filing_acceptance'
            ),
        },
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
).hexdigest()
if STAGE6B_V3_MIGRATION_SHA256 != (
    '9b7f53c8eb6cca500b2b23a99df35bc60d5d3bef1077c36acd3529a8d92ce6be'
):
    raise RuntimeError('Frozen Stage 6B v3 migration manifest changed.')

STAGE6B_V4_MIGRATION_SHA256 = hashlib.sha256(
    json.dumps(
        {
            'version': 4,
            'name': STAGE6B_V4_MIGRATION_NAME,
            'table_sql': ' '.join(
                MIGRATION_V4_OBSERVATION_TABLE_SQL.split()
            ),
            'contract': (
                'remove_legacy_semantic_unique_key;'
                'retain_immutable_observations_by_sha256;'
                'preserve_all_existing_rows_and_foreign_keys'
            ),
        },
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
).hexdigest()
if STAGE6B_V4_MIGRATION_SHA256 != (
    '365d2fc92153c70899b83bb122b0ec054a9ee04f808f4e84b4875b528d84f1b6'
):
    raise RuntimeError('Frozen Stage 6B v4 migration manifest changed.')

STAGE6B_V5_MIGRATION_SHA256 = hashlib.sha256(
    json.dumps(
        {
            'version': 5,
            'name': STAGE6B_V5_MIGRATION_NAME,
            'columns': {
                'stage6b_document_inventory': {
                    'document_role': "TEXT NOT NULL DEFAULT 'primary_filing'",
                    'sec_document_type': "TEXT NOT NULL DEFAULT ''",
                    'document_sequence': "TEXT NOT NULL DEFAULT ''",
                    'document_description': "TEXT NOT NULL DEFAULT ''",
                    'content_type': "TEXT NOT NULL DEFAULT ''",
                    'source_kind': "TEXT NOT NULL DEFAULT ''",
                },
            },
            'statements': [
                ' '.join(statement.split())
                for statement in MIGRATION_V5_STATEMENTS
            ],
            'contract': (
                'stage6b_owned_event_index_and_exhibit_inventory;'
                'role_selected_multi_document_seals;'
                'granular_coverage_evidence_states;'
                'zero_stage4_mutation'
            ),
        },
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
).hexdigest()
if STAGE6B_V5_MIGRATION_SHA256 != (
    'f4ee181a9ac70d00242c4b5218028a381a7b7d1239b08367c3a345d849bbd378'
):
    raise RuntimeError('Frozen Stage 6B v5 migration manifest changed.')

STAGE6B_V6_MIGRATION_SHA256 = hashlib.sha256(
    json.dumps(
        {
            'version': 6,
            'name': STAGE6B_V6_MIGRATION_NAME,
            'statements': [
                ' '.join(statement.split())
                for statement in MIGRATION_V6_STATEMENTS
            ],
            'contract': (
                'persist_observation_depth_separately_from_pair_breadth;'
                'retain_immutable_run_scoped_period_counts;'
                'honest_metric_targeted_missing_candidate_diagnostics'
            ),
        },
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
).hexdigest()
if STAGE6B_V6_MIGRATION_SHA256 != (
    '4e601167ec0747fbfc3847a268ec104a90aece97ec8909dab67f97949f49afef'
):
    raise RuntimeError(
        'Frozen Stage 6B v6 migration manifest changed: '
        f'{STAGE6B_V6_MIGRATION_SHA256}'
    )

_SAVEPOINT_SEQUENCE = count(1)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f'PRAGMA table_info({table})')}


def _migrate_observation_history_v4(conn: sqlite3.Connection) -> None:
    source = 'fact_specialized_metric_observation'
    legacy = 'fact_specialized_metric_observation_v3'
    if _columns(conn, legacy):
        raise RuntimeError(
            'Stage 6B v4 found an unexpected legacy observation table.'
        )
    columns = (
        'observation_id,ticker,metric_id,period_start,period_end,accepted_at,'
        'numeric_value,unit,definition_version,applicability_status,'
        'evidence_status,evidence_key,source_id,source_document,created_at,'
        'confidence,extraction_method,scope,lineage_json,observation_sha256,'
        'production_status,parser_run_id'
    )
    before = int(conn.execute(
        f'SELECT COUNT(*) FROM {source}'
    ).fetchone()[0])
    conn.execute(f'ALTER TABLE {source} RENAME TO {legacy}')
    conn.execute(MIGRATION_V4_OBSERVATION_TABLE_SQL)
    conn.execute(
        f'INSERT INTO {source}({columns}) SELECT {columns} FROM {legacy}'
    )
    conn.execute(f'DROP TABLE {legacy}')
    for statement in SCHEMA_STATEMENTS[5:7]:
        conn.execute(statement)
    after = int(conn.execute(
        f'SELECT COUNT(*) FROM {source}'
    ).fetchone()[0])
    if after != before:
        raise RuntimeError(
            'Stage 6B v4 observation history row parity failed: '
            f'{before} != {after}.'
        )


def ensure_stage6b_schema(conn: sqlite3.Connection) -> None:
    nested = conn.in_transaction
    savepoint = f'stage6b_schema_{next(_SAVEPOINT_SEQUENCE)}'
    if nested:
        conn.execute(f'SAVEPOINT {savepoint}')
    else:
        conn.execute('BEGIN IMMEDIATE')
    try:
        conn.execute(SCHEMA_STATEMENTS[0])
        expected_history = {
            1: (
                'specialized_measurement_overlay_v1',
                STAGE6B_V1_MIGRATION_SHA256,
            ),
            2: (STAGE6B_MIGRATION_NAME, STAGE6B_MIGRATION_SHA256),
            3: (STAGE6B_V3_MIGRATION_NAME, STAGE6B_V3_MIGRATION_SHA256),
            4: (STAGE6B_V4_MIGRATION_NAME, STAGE6B_V4_MIGRATION_SHA256),
            5: (STAGE6B_V5_MIGRATION_NAME, STAGE6B_V5_MIGRATION_SHA256),
            6: (STAGE6B_V6_MIGRATION_NAME, STAGE6B_V6_MIGRATION_SHA256),
        }
        ledger_rows = list(conn.execute(
            '''SELECT migration_version,migration_name,migration_sha256
               FROM stage6b_schema_migrations ORDER BY migration_version'''
        ))
        observed_versions = [int(row[0]) for row in ledger_rows]
        if observed_versions != list(range(1, len(observed_versions) + 1)):
            raise RuntimeError('Stage 6B migration ledger is not an exact prefix.')
        for version, name, digest in ledger_rows:
            expected = expected_history.get(int(version))
            if expected is None or (str(name), str(digest)) != expected:
                raise RuntimeError('Stage 6B migration ledger checksum mismatch.')

        applied = set(observed_versions)
        present = _columns(conn, 'fact_specialized_metric_observation')
        if not present:
            raise RuntimeError('Stage 6B specialized observation foundation is missing.')
        if 1 not in applied:
            for column, ddl in OBSERVATION_COLUMNS.items():
                if column not in present:
                    conn.execute(
                        f'ALTER TABLE fact_specialized_metric_observation '
                        f'ADD COLUMN {column} {ddl}'
                    )
            for statement in SCHEMA_STATEMENTS[1:]:
                conn.execute(statement)
            conn.execute(
                '''INSERT INTO stage6b_schema_migrations(
                       migration_version,migration_name,migration_sha256,applied_at
                   ) VALUES (1,?,?,?)''',
                (
                    'specialized_measurement_overlay_v1',
                    STAGE6B_V1_MIGRATION_SHA256,
                    utc_now(),
                ),
            )
        if 2 not in applied:
            column_specs = {
                'dim_specialized_metric': (
                    'source_availability_class',
                    "TEXT NOT NULL DEFAULT 'sec_direct'",
                ),
                'stage6b_metric_policy': (
                    'source_availability_class',
                    "TEXT NOT NULL DEFAULT 'sec_direct'",
                ),
                'stage6b_metric_coverage': (
                    'source_availability_class',
                    "TEXT NOT NULL DEFAULT 'sec_direct'",
                ),
            }
            for table, (column, ddl) in column_specs.items():
                if column not in _columns(conn, table):
                    conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {ddl}')
            if 'denominator_kind' not in _columns(conn, 'stage6b_metric_coverage'):
                conn.execute(
                    'ALTER TABLE stage6b_metric_coverage ADD COLUMN '
                    "denominator_kind TEXT NOT NULL DEFAULT 'registered_applicable'"
                )
            for statement in MIGRATION_V2_STATEMENTS:
                conn.execute(statement)
            conn.execute(
                '''INSERT INTO stage6b_schema_migrations(
                       migration_version,migration_name,migration_sha256,applied_at
                   ) VALUES (?,?,?,?)''',
                (
                    2,
                    STAGE6B_MIGRATION_NAME,
                    STAGE6B_MIGRATION_SHA256,
                    utc_now(),
                ),
            )

        if 3 not in applied:
            for statement in MIGRATION_V3_STATEMENTS:
                conn.execute(statement)
            conn.execute(
                '''INSERT INTO stage6b_schema_migrations(
                       migration_version,migration_name,migration_sha256,applied_at
                   ) VALUES (3,?,?,?)''',
                (
                    STAGE6B_V3_MIGRATION_NAME,
                    STAGE6B_V3_MIGRATION_SHA256,
                    utc_now(),
                ),
            )

        if 4 not in applied:
            _migrate_observation_history_v4(conn)
            conn.execute(
                '''INSERT INTO stage6b_schema_migrations(
                       migration_version,migration_name,migration_sha256,applied_at
                   ) VALUES (4,?,?,?)''',
                (
                    STAGE6B_V4_MIGRATION_NAME,
                    STAGE6B_V4_MIGRATION_SHA256,
                    utc_now(),
                ),
            )

        if 5 not in applied:
            inventory_columns = {
                'document_role': "TEXT NOT NULL DEFAULT 'primary_filing'",
                'sec_document_type': "TEXT NOT NULL DEFAULT ''",
                'document_sequence': "TEXT NOT NULL DEFAULT ''",
                'document_description': "TEXT NOT NULL DEFAULT ''",
                'content_type': "TEXT NOT NULL DEFAULT ''",
                'source_kind': "TEXT NOT NULL DEFAULT ''",
            }
            present_inventory = _columns(conn, 'stage6b_document_inventory')
            for column, ddl in inventory_columns.items():
                if column not in present_inventory:
                    conn.execute(
                        'ALTER TABLE stage6b_document_inventory '
                        f'ADD COLUMN {column} {ddl}'
                    )
            for statement in MIGRATION_V5_STATEMENTS:
                conn.execute(statement)
            conn.execute(
                '''INSERT INTO stage6b_schema_migrations(
                       migration_version,migration_name,migration_sha256,applied_at
                   ) VALUES (5,?,?,?)''',
                (
                    STAGE6B_V5_MIGRATION_NAME,
                    STAGE6B_V5_MIGRATION_SHA256,
                    utc_now(),
                ),
            )

        if 6 not in applied:
            for statement in MIGRATION_V6_STATEMENTS:
                conn.execute(statement)
            conn.execute(
                '''INSERT INTO stage6b_schema_migrations(
                       migration_version,migration_name,migration_sha256,applied_at
                   ) VALUES (6,?,?,?)''',
                (
                    STAGE6B_V6_MIGRATION_NAME,
                    STAGE6B_V6_MIGRATION_SHA256,
                    utc_now(),
                ),
            )

        missing = set(OBSERVATION_COLUMNS) - _columns(
            conn, 'fact_specialized_metric_observation'
        )
        if missing:
            raise RuntimeError(
                f'Stage 6B observation schema postcondition failed: {sorted(missing)}'
            )
        required_columns = {
            'dim_specialized_metric': {'source_availability_class'},
            'stage6b_metric_policy': {'source_availability_class'},
            'stage6b_metric_coverage': {
                'source_availability_class', 'denominator_kind'
            },
        }
        for table, required in required_columns.items():
            missing_columns = required - _columns(conn, table)
            if missing_columns:
                raise RuntimeError(
                    f'Stage 6B v2 postcondition failed for {table}: '
                    f'{sorted(missing_columns)}'
                )
        final_versions = [
            int(row[0]) for row in conn.execute(
                'SELECT migration_version FROM stage6b_schema_migrations '
                'ORDER BY migration_version'
            )
        ]
        for table in (
            'stage6b_historical_document_snapshot_run',
            'stage6b_historical_document_snapshot',
        ):
            if not _columns(conn, table):
                raise RuntimeError(
                    f'Stage 6B v3 postcondition failed: missing {table}'
                )
        observation_sql = str(conn.execute(
            '''SELECT sql FROM sqlite_master
               WHERE type='table'
                 AND name='fact_specialized_metric_observation' '''
        ).fetchone()[0])
        if 'UNIQUE(ticker, metric_id, period_end, accepted_at' in observation_sql:
            raise RuntimeError(
                'Stage 6B v4 legacy observation uniqueness remains active.'
            )
        event_inventory_columns = {
            'document_role', 'sec_document_type', 'document_sequence',
            'document_description', 'content_type', 'source_kind',
        }
        if event_inventory_columns - _columns(
            conn, 'stage6b_document_inventory'
        ):
            raise RuntimeError(
                'Stage 6B v5 document inventory postcondition failed.'
            )
        for table in (
            'stage6b_event_document_snapshot_run',
            'stage6b_event_document_snapshot',
            'stage6b_metric_coverage_status',
        ):
            if not _columns(conn, table):
                raise RuntimeError(
                    f'Stage 6B v5 postcondition failed: missing {table}'
                )
        if not _columns(conn, 'stage6b_metric_history_depth'):
            raise RuntimeError(
                'Stage 6B v6 postcondition failed: missing history-depth table.'
            )
        if final_versions != [1, 2, 3, 4, 5, 6]:
            raise RuntimeError('Stage 6B migration history is incomplete.')
        if conn.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise RuntimeError('Stage 6B migration introduced a foreign-key violation.')
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
