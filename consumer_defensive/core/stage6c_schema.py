from __future__ import annotations

import hashlib
import json
import sqlite3
from itertools import count
from typing import Any

from .db import utc_now


STAGE6C_SCHEMA_VERSION = 1
STAGE6C_MIGRATION_NAME = 'pit_specialized_factor_panel_v1'

SCHEMA_STATEMENTS = (
    '''
    CREATE TABLE IF NOT EXISTS stage6c_schema_migrations (
        migration_version INTEGER PRIMARY KEY,
        migration_name TEXT NOT NULL UNIQUE,
        migration_sha256 TEXT NOT NULL,
        applied_at TEXT NOT NULL
    )
    ''',
    '''
    CREATE TABLE IF NOT EXISTS stage6c_panel_run (
        stage6c_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
        asof_date TEXT NOT NULL,
        history_start TEXT NOT NULL,
        evaluation_frequency TEXT NOT NULL CHECK(evaluation_frequency='monthly'),
        entry_lag_trading_days INTEGER NOT NULL CHECK(entry_lag_trading_days>=0),
        horizons_json TEXT NOT NULL,
        freshness_days INTEGER NOT NULL CHECK(freshness_days>=1),
        config_sha256 TEXT NOT NULL,
        metric_policy_sha256 TEXT NOT NULL,
        source_stage6b_run_id INTEGER NOT NULL,
        status TEXT NOT NULL,
        evaluation_date_count INTEGER NOT NULL DEFAULT 0,
        panel_row_count INTEGER NOT NULL DEFAULT 0,
        numeric_row_count INTEGER NOT NULL DEFAULT 0,
        panel_sha256 TEXT NOT NULL DEFAULT '',
        manifest_json TEXT NOT NULL DEFAULT '{}',
        started_at TEXT NOT NULL,
        completed_at TEXT,
        UNIQUE(
            asof_date,history_start,evaluation_frequency,
            entry_lag_trading_days,horizons_json,freshness_days,
            config_sha256,metric_policy_sha256,source_stage6b_run_id
        ),
        FOREIGN KEY(source_stage6b_run_id)
            REFERENCES stage6b_specialized_run(stage6b_run_id)
            ON DELETE RESTRICT
    )
    ''',
    '''
    CREATE TABLE IF NOT EXISTS stage6c_feature_manifest (
        stage6c_run_id INTEGER NOT NULL,
        factor_id TEXT NOT NULL,
        source_availability_class TEXT NOT NULL,
        cohorts_json TEXT NOT NULL,
        applicability_subtypes_json TEXT NOT NULL,
        unit_family TEXT NOT NULL,
        direction_hint TEXT NOT NULL,
        factor_direction TEXT,
        production_status TEXT NOT NULL CHECK(production_status='measurement_only'),
        definition_versions_json TEXT NOT NULL,
        factor_validation_eligible INTEGER NOT NULL
            CHECK(factor_validation_eligible IN (0,1)),
        exclusion_reason TEXT,
        manifest_row_sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(stage6c_run_id,factor_id),
        FOREIGN KEY(stage6c_run_id)
            REFERENCES stage6c_panel_run(stage6c_run_id) ON DELETE CASCADE,
        FOREIGN KEY(factor_id)
            REFERENCES dim_specialized_metric(metric_id) ON DELETE RESTRICT
    )
    ''',
    '''
    CREATE TABLE IF NOT EXISTS stage6c_specialized_factor_panel (
        stage6c_run_id INTEGER NOT NULL,
        asof_date TEXT NOT NULL,
        ticker TEXT NOT NULL,
        cohort_id TEXT NOT NULL,
        applicability_subtype TEXT NOT NULL,
        factor_id TEXT NOT NULL,
        factor_value REAL,
        unit TEXT,
        direction_hint TEXT NOT NULL,
        availability_status TEXT NOT NULL,
        source_accepted_at TEXT,
        source_period_end TEXT,
        source_age_days INTEGER,
        source_observation_sha256 TEXT,
        source_definition_version TEXT,
        membership_eligible_flag INTEGER NOT NULL
            CHECK(membership_eligible_flag IN (0,1)),
        investable_flag INTEGER NOT NULL CHECK(investable_flag IN (0,1)),
        sample_role TEXT NOT NULL,
        market_regime TEXT NOT NULL,
        input_cost_regime TEXT NOT NULL,
        terminal_event_status TEXT NOT NULL,
        forward_total_return_21d REAL,
        forward_total_return_63d REAL,
        forward_total_return_126d REAL,
        forward_xlp_residual_return_21d REAL,
        forward_xlp_residual_return_63d REAL,
        forward_xlp_residual_return_126d REAL,
        forward_spy_beta_residual_return_21d REAL,
        forward_spy_beta_residual_return_63d REAL,
        forward_spy_beta_residual_return_126d REAL,
        row_sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(stage6c_run_id,asof_date,ticker,factor_id),
        FOREIGN KEY(stage6c_run_id)
            REFERENCES stage6c_panel_run(stage6c_run_id) ON DELETE CASCADE,
        FOREIGN KEY(factor_id)
            REFERENCES dim_specialized_metric(metric_id) ON DELETE RESTRICT
    )
    ''',
    '''
    CREATE INDEX IF NOT EXISTS idx_stage6c_panel_factor_date
        ON stage6c_specialized_factor_panel(
            stage6c_run_id,factor_id,cohort_id,asof_date,ticker
        )
    ''',
    '''
    CREATE INDEX IF NOT EXISTS idx_stage6c_panel_eligible_target
        ON stage6c_specialized_factor_panel(
            stage6c_run_id,availability_status,investable_flag,asof_date
        )
    ''',
)


def _manifest() -> dict[str, Any]:
    return {
        'version': STAGE6C_SCHEMA_VERSION,
        'name': STAGE6C_MIGRATION_NAME,
        'statements': [' '.join(statement.split()) for statement in SCHEMA_STATEMENTS],
    }


STAGE6C_MIGRATION_SHA256 = hashlib.sha256(
    json.dumps(_manifest(), sort_keys=True, separators=(',', ':')).encode('utf-8')
).hexdigest()

_SAVEPOINT_SEQUENCE = count(1)


def ensure_stage6c_schema(conn: sqlite3.Connection) -> None:
    """Apply the additive Stage 6C research-panel schema atomically."""

    nested = conn.in_transaction
    savepoint = f'stage6c_schema_{next(_SAVEPOINT_SEQUENCE)}'
    if nested:
        conn.execute(f'SAVEPOINT {savepoint}')
    else:
        conn.execute('BEGIN IMMEDIATE')
    try:
        for statement in SCHEMA_STATEMENTS:
            conn.execute(statement)
        ledger = conn.execute(
            '''SELECT migration_name,migration_sha256
               FROM stage6c_schema_migrations WHERE migration_version=?''',
            (STAGE6C_SCHEMA_VERSION,),
        ).fetchone()
        if ledger is not None and tuple(ledger) != (
            STAGE6C_MIGRATION_NAME,
            STAGE6C_MIGRATION_SHA256,
        ):
            raise RuntimeError('Stage 6C migration ledger checksum mismatch.')
        if ledger is None:
            conn.execute(
                '''INSERT INTO stage6c_schema_migrations(
                       migration_version,migration_name,migration_sha256,applied_at
                   ) VALUES (?,?,?,?)''',
                (
                    STAGE6C_SCHEMA_VERSION,
                    STAGE6C_MIGRATION_NAME,
                    STAGE6C_MIGRATION_SHA256,
                    utc_now(),
                ),
            )
        expected = {
            'stage6c_panel_run',
            'stage6c_feature_manifest',
            'stage6c_specialized_factor_panel',
        }
        present = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if missing := expected - present:
            raise RuntimeError(
                f'Stage 6C schema postcondition failed: missing={sorted(missing)}'
            )
        if conn.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise RuntimeError('Stage 6C migration introduced a foreign-key violation.')
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
