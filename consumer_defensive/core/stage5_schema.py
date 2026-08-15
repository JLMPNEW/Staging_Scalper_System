"""Additive Stage 5 schema and source-contract migrations."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from .db import utc_now


STAGE5_SCHEMA_VERSION = 1
STAGE5_MIGRATION_NAME = "positioning_pit_contract_v1"
STAGE5_MIGRATION_MANIFEST = {
    "version": STAGE5_SCHEMA_VERSION,
    "name": STAGE5_MIGRATION_NAME,
    "tables": ["stage5_schema_migrations", "stage5_source_contract"],
    "ownership_columns": [
        "accession_number",
        "owner_name",
        "owner_relationship",
        "security_title",
        "accepted_at",
        "availability_date",
        "is_current_truth",
        "source_observation_id",
    ],
    "13f_columns": [
        "period_of_report",
        "new_buyer_count",
        "exiting_holder_count",
        "net_buyer_count",
        "institutional_ownership_delta_pct",
        "source_birthdate",
        "source_observation_id",
    ],
    "short_columns": [
        "source_birthdate",
        "source_observation_id",
        "float_shares_proxy",
        "float_proxy_concept",
        "float_proxy_accepted_at",
        "float_proxy_method",
    ],
    "borrow_columns": ["source_observation_id"],
    "feature_columns": [
        "short_days_to_cover",
        "quality_reason",
        "lineage_json",
        "definition_version",
    ],
}
STAGE5_MIGRATION_SHA256 = hashlib.sha256(
    json.dumps(STAGE5_MIGRATION_MANIFEST, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS stage5_schema_migrations (
    migration_version INTEGER PRIMARY KEY,
    migration_name TEXT NOT NULL UNIQUE,
    migration_sha256 TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stage5_source_contract (
    source_key TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    source_birthdate TEXT NOT NULL,
    required_for_gate INTEGER NOT NULL CHECK(required_for_gate IN (0, 1)),
    maximum_age_days INTEGER,
    availability_semantics TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    FOREIGN KEY(source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);
"""


COLUMN_MIGRATIONS: dict[str, dict[str, str]] = {
    "fact_sec_ownership_transaction": {
        "accession_number": "TEXT",
        "owner_name": "TEXT",
        "owner_relationship": "TEXT",
        "security_title": "TEXT",
        "accepted_at": "TEXT",
        "availability_date": "TEXT",
        "is_current_truth": "INTEGER NOT NULL DEFAULT 1 CHECK(is_current_truth IN (0, 1))",
        "source_observation_id": "TEXT",
    },
    "fact_13f_positioning": {
        "period_of_report": "TEXT",
        "new_buyer_count": "INTEGER",
        "exiting_holder_count": "INTEGER",
        "net_buyer_count": "INTEGER",
        "institutional_ownership_delta_pct": "REAL",
        "source_birthdate": "TEXT",
        "source_observation_id": "TEXT",
    },
    "fact_short_interest": {
        "source_birthdate": "TEXT",
        "source_observation_id": "TEXT",
        "float_shares_proxy": "REAL",
        "float_proxy_concept": "TEXT",
        "float_proxy_accepted_at": "TEXT",
        "float_proxy_method": "TEXT",
    },
    "fact_borrow_snapshot": {
        "source_observation_id": "TEXT",
    },
    "feature_positioning": {
        "short_days_to_cover": "REAL",
        "quality_reason": "TEXT",
        "lineage_json": "TEXT NOT NULL DEFAULT '{}'",
        "definition_version": "TEXT NOT NULL DEFAULT 'consumer_defensive_positioning_v2'",
    },
}


INDEX_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_stage5_ownership_observation
    ON fact_sec_ownership_transaction(source_observation_id)
    WHERE source_observation_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_stage5_ownership_ticker_accepted
    ON fact_sec_ownership_transaction(ticker, accepted_at, source_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_stage5_13f_observation
    ON fact_13f_positioning(source_observation_id)
    WHERE source_observation_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_stage5_13f_ticker_available
    ON fact_13f_positioning(ticker, publication_date, source_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_stage5_short_observation
    ON fact_short_interest(source_observation_id)
    WHERE source_observation_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_stage5_short_ticker_available
    ON fact_short_interest(ticker, publication_date, source_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_stage5_borrow_observation
    ON fact_borrow_snapshot(source_observation_id)
    WHERE source_observation_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_stage5_borrow_ticker_asof
    ON fact_borrow_snapshot(ticker, asof_date, source_id);
CREATE INDEX IF NOT EXISTS idx_stage5_feature_asof
    ON feature_positioning(model_family, asof_date, ticker);
"""


def _statements(script: str) -> Iterator[str]:
    pending: list[str] = []
    for character in script:
        pending.append(character)
        if character == ";" and sqlite3.complete_statement("".join(pending)):
            statement = "".join(pending).strip()
            if statement:
                yield statement
            pending.clear()
    if "".join(pending).strip():
        raise sqlite3.OperationalError("Stage 5 schema SQL ended with an incomplete statement.")


@contextmanager
def _atomic(conn: sqlite3.Connection) -> Iterator[None]:
    nested = conn.in_transaction
    savepoint = "consumer_defensive_stage5_schema"
    conn.execute(f"SAVEPOINT {savepoint}" if nested else "BEGIN IMMEDIATE")
    try:
        yield
        conn.execute(f"RELEASE SAVEPOINT {savepoint}" if nested else "COMMIT")
    except BaseException:
        if conn.in_transaction:
            if nested:
                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            else:
                conn.execute("ROLLBACK")
        raise


def _execute(conn: sqlite3.Connection, script: str) -> None:
    for statement in _statements(script):
        conn.execute(statement)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def ensure_stage5_schema(conn: sqlite3.Connection) -> None:
    """Apply the immutable Stage 5 v1 migration atomically and idempotently."""

    with _atomic(conn):
        _execute(conn, SCHEMA_SQL)
        ledger = conn.execute(
            "SELECT migration_name, migration_sha256 FROM stage5_schema_migrations "
            "WHERE migration_version=?",
            (STAGE5_SCHEMA_VERSION,),
        ).fetchone()
        if ledger is not None and (
            str(ledger[0]) != STAGE5_MIGRATION_NAME
            or str(ledger[1]) != STAGE5_MIGRATION_SHA256
        ):
            raise RuntimeError("Stage 5 migration ledger checksum/name drift detected.")

        for table, additions in COLUMN_MIGRATIONS.items():
            existing = _columns(conn, table)
            if not existing:
                raise RuntimeError(f"Stage 5 foundation table is missing: {table}")
            for column, declaration in additions.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
                    existing.add(column)
        _execute(conn, INDEX_SQL)

        if ledger is None:
            conn.execute(
                "INSERT INTO stage5_schema_migrations VALUES (?, ?, ?, ?)",
                (
                    STAGE5_SCHEMA_VERSION,
                    STAGE5_MIGRATION_NAME,
                    STAGE5_MIGRATION_SHA256,
                    utc_now(),
                ),
            )

        for table, additions in COLUMN_MIGRATIONS.items():
            missing = sorted(set(additions) - _columns(conn, table))
            if missing:
                raise RuntimeError(f"Stage 5 migration postcondition failed for {table}: {missing}")
        violations = conn.execute("PRAGMA foreign_key_check").fetchmany(5)
        if violations:
            raise RuntimeError(f"Stage 5 migration introduced foreign-key violations: {violations}")
