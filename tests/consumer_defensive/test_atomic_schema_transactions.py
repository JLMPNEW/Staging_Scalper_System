from __future__ import annotations

import sqlite3

import pytest

from consumer_defensive.core import db
from consumer_defensive.core.market_data import ensure_stage3_schema
from consumer_defensive.core.terminal_events import ensure_terminal_event_schema
from consumer_defensive.core.universe import ensure_stage2_schema


def _memory_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        is not None
    )


def test_schema_script_rolls_back_all_ddl_after_invalid_statement() -> None:
    conn = _memory_connection()
    try:
        with pytest.raises(sqlite3.OperationalError):
            db.execute_schema_script(
                conn,
                """
                CREATE TABLE should_be_rolled_back(value INTEGER);
                CREATE TABL invalid_sql(value INTEGER);
                """,
            )

        assert not _has_table(conn, "should_be_rolled_back")
        assert not conn.in_transaction
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_schema_script_handles_same_line_statements_and_rejects_txn_control() -> None:
    conn = _memory_connection()
    try:
        db.execute_schema_script(
            conn,
            "CREATE TABLE first_table(value INTEGER); "
            "CREATE TABLE second_table(value INTEGER); -- trailing comment",
        )
        assert _has_table(conn, "first_table")
        assert _has_table(conn, "second_table")

        with pytest.raises(sqlite3.OperationalError, match="transaction-control"):
            db.execute_schema_script(
                conn,
                "BEGIN; CREATE TABLE forbidden_table(value INTEGER); COMMIT;",
            )
        assert not _has_table(conn, "forbidden_table")
        assert not conn.in_transaction
    finally:
        conn.close()


def test_schema_script_uses_savepoint_without_committing_outer_transaction() -> None:
    conn = _memory_connection()
    try:
        conn.execute("CREATE TABLE caller_state(value INTEGER)")
        conn.commit()
        conn.execute("BEGIN")
        conn.execute("INSERT INTO caller_state VALUES(7)")

        with pytest.raises(sqlite3.OperationalError):
            db.execute_schema_script(
                conn,
                """
                CREATE TABLE failed_nested_schema(value INTEGER);
                THIS IS NOT SQL;
                """,
            )

        assert conn.in_transaction
        assert conn.execute("SELECT value FROM caller_state").fetchone()[0] == 7
        assert not _has_table(conn, "failed_nested_schema")

        db.execute_schema_script(
            conn,
            "CREATE TABLE successful_nested_schema(value INTEGER);",
        )
        assert conn.in_transaction
        assert _has_table(conn, "successful_nested_schema")

        conn.rollback()
        assert conn.execute("SELECT COUNT(*) FROM caller_state").fetchone()[0] == 0
        assert not _has_table(conn, "successful_nested_schema")
    finally:
        conn.close()


def test_init_db_failure_rolls_back_foundation_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _memory_connection()

    def fail_migration(_conn: sqlite3.Connection) -> None:
        raise RuntimeError("injected migration failure")

    monkeypatch.setattr(db, "_migrate_financial_semantic_columns", fail_migration)
    try:
        with pytest.raises(RuntimeError, match="injected migration failure"):
            db.init_db(conn)

        assert db.table_names(conn) == []
        assert not conn.in_transaction
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_init_db_is_idempotent_and_foreign_keys_are_clean() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0
        db.init_db(conn)
        db.init_db(conn)

        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            conn.execute("SELECT COUNT(*) FROM sector_database_identity").fetchone()[0]
            == 1
        )
        assert not conn.in_transaction
    finally:
        conn.close()


def test_stage_schema_initializers_are_idempotent_and_nested_safe() -> None:
    conn = _memory_connection()
    try:
        db.init_db(conn)
        conn.execute("CREATE TABLE caller_stage_state(value INTEGER)")
        conn.commit()
        conn.execute("BEGIN")
        conn.execute("INSERT INTO caller_stage_state VALUES(11)")

        for ensure_schema in (
            ensure_stage2_schema,
            ensure_stage3_schema,
            ensure_terminal_event_schema,
        ):
            ensure_schema(conn)
            ensure_schema(conn)
            assert conn.in_transaction

        assert _has_table(conn, "dim_recognized_vehicle")
        assert _has_table(conn, "dim_price_series_selection")
        assert _has_table(conn, "fact_terminal_event_reconciliation")

        conn.rollback()
        assert conn.execute("SELECT COUNT(*) FROM caller_stage_state").fetchone()[0] == 0
        assert not _has_table(conn, "dim_recognized_vehicle")
        assert not _has_table(conn, "dim_price_series_selection")
        # This table is also part of the foundation schema and predates the
        # outer transaction; ensuring it must not remove or recreate it.
        assert _has_table(conn, "fact_terminal_event_reconciliation")
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()
