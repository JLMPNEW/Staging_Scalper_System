from __future__ import annotations

from pathlib import Path

from consumer_defensive.core.config import load_config
from consumer_defensive.core.db import connect, init_db
from consumer_defensive.core.stage4 import (
    STAGE4_MIGRATION_HISTORY,
    bootstrap_stage4,
    ensure_stage4_schema,
)


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "consumer_defensive" / "config.yaml"


def _index_columns(conn, index_name: str) -> tuple[str, ...]:
    return tuple(str(row[2]) for row in conn.execute(f"PRAGMA index_info('{index_name}')"))


def _plan_details(conn, sql: str, parameters: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(str(row[3]) for row in conn.execute("EXPLAIN QUERY PLAN " + sql, parameters))


def test_existing_database_bootstrap_migrates_and_uses_raw_delete_indexes(
    tmp_path: Path,
) -> None:
    bundle = load_config(CONFIG)
    with connect(tmp_path / "existing_stage4.sqlite") as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        init_db(conn)
        with conn:
            conn.execute(
                """
                CREATE INDEX idx_stage4_raw_ticker_accepted
                ON fact_sec_xbrl_fact_raw(ticker, accepted_at, concept)
                """
            )

        # Bootstrap twice: the first call migrates the legacy index and the
        # second proves the migration remains idempotent for an existing DB.
        bootstrap_stage4(conn, bundle)
        bootstrap_stage4(conn, bundle)

        index_names = {
            str(row[1])
            for row in conn.execute("PRAGMA index_list('fact_sec_xbrl_fact_raw')")
        }
        assert "idx_stage4_raw_ticker_accepted" not in index_names
        assert "idx_stage4_raw_ticker_source_accepted" in index_names
        assert _index_columns(conn, "idx_stage4_raw_ticker_source_accepted") == (
            "ticker",
            "source_id",
            "accepted_at",
        )
        assert _index_columns(conn, "idx_stage4_canonical_raw_fact_id") == (
            "source_raw_fact_id",
        )

        raw_delete_plan = _plan_details(
            conn,
            """
            DELETE FROM fact_sec_xbrl_fact_raw
            WHERE ticker=? AND source_id=? AND accepted_at<=?
            """,
            ("KO", "sec_companyfacts", "2026-08-11T23:59:59Z"),
        )
        assert any(
            "idx_stage4_raw_ticker_source_accepted " in detail
            and "ticker=? AND source_id=? AND accepted_at<?" in detail
            for detail in raw_delete_plan
        ), raw_delete_plan
        assert any(
            "idx_stage4_canonical_raw_fact_id" in detail
            and "source_raw_fact_id=?" in detail
            for detail in raw_delete_plan
        ), raw_delete_plan
        assert not any(
            detail == "SCAN fact_financial_statement_canonical"
            for detail in raw_delete_plan
        ), raw_delete_plan

        missing_identity_plan = _plan_details(
            conn,
            """SELECT raw_fact_id FROM fact_sec_xbrl_fact_raw
               WHERE raw_fact_id>?
                 AND source_observation_id IS NULL
               ORDER BY raw_fact_id LIMIT ?""",
            ("0", "2048"),
        )
        assert any(
            'idx_stage4_raw_missing_observation_identity' in detail
            or 'idx_stage4_raw_source_observation_id' in detail
            for detail in missing_identity_plan
        ), missing_identity_plan
        assert not any(
            detail.startswith('SCAN fact_sec_xbrl_fact_raw')
            or 'TEMP B-TREE' in detail
            for detail in missing_identity_plan
        ), missing_identity_plan


def test_shared_accession_reconciliation_uses_v9_indexes(tmp_path: Path) -> None:
    bundle = load_config(CONFIG)
    with connect(tmp_path / "shared_accession_plan.sqlite") as conn:
        bootstrap_stage4(conn, bundle)

        expected_indexes = {
            "idx_stage4_raw_accession_fact": (
                "accession_number",
                "raw_fact_id",
            ),
            "idx_stage4_canonical_accession": ("accession_number",),
            "idx_stage4_census_accession": ("accession_number",),
        }
        for index_name, columns in expected_indexes.items():
            assert _index_columns(conn, index_name) == columns

        accession = "0000000001-26-000001"
        plans = {
            "raw_acceptance_update": _plan_details(
                conn,
                """UPDATE fact_sec_xbrl_fact_raw SET accepted_at=?
                   WHERE accession_number=?""",
                ("2026-01-01T00:00:00Z", accession),
            ),
            "raw_identity_page": _plan_details(
                conn,
                """SELECT raw_fact_id FROM fact_sec_xbrl_fact_raw
                   WHERE accession_number=? AND raw_fact_id>?
                   ORDER BY raw_fact_id LIMIT ?""",
                (accession, "0", "2048"),
            ),
            "canonical_identity_update": _plan_details(
                conn,
                """UPDATE fact_financial_statement_canonical
                   SET source_observation_id=(SELECT r.source_observation_id
                       FROM fact_sec_xbrl_fact_raw r
                       WHERE r.raw_fact_id=
                         fact_financial_statement_canonical.source_raw_fact_id)
                   WHERE accession_number=? AND source_raw_fact_id IS NOT NULL""",
                (accession,),
            ),
            "canonical_delete": _plan_details(
                conn,
                """DELETE FROM fact_financial_statement_canonical
                   WHERE accession_number=?""",
                (accession,),
            ),
            "census_delete": _plan_details(
                conn,
                """DELETE FROM fact_specialized_metric_disclosure_census
                   WHERE accession_number=?""",
                (accession,),
            ),
            "feature_delete": _plan_details(
                conn,
                """DELETE FROM feature_financial_statement
                   WHERE model_family=? AND ticker=? AND asof_date>=?""",
                ("consumer_defensive", "KO", "2025-01-01"),
            ),
            "summary_delete": _plan_details(
                conn,
                """DELETE FROM fact_specialized_metric_disclosure_summary
                   WHERE ticker=? AND asof_date>=?""",
                ("KO", "2025-01-01"),
            ),
        }
        required = {
            "raw_acceptance_update": "idx_stage4_raw_accession_fact",
            "raw_identity_page": "idx_stage4_raw_accession_fact",
            "canonical_identity_update": "idx_stage4_canonical_accession",
            "canonical_delete": "idx_stage4_canonical_accession",
            "census_delete": "idx_stage4_census_accession",
            "feature_delete": "sqlite_autoindex_feature_financial_statement_1",
            "summary_delete": (
                "sqlite_autoindex_fact_specialized_metric_disclosure_summary_1"
            ),
        }
        table_names = {
            "raw_acceptance_update": "fact_sec_xbrl_fact_raw",
            "raw_identity_page": "fact_sec_xbrl_fact_raw",
            "canonical_identity_update": "fact_financial_statement_canonical",
            "canonical_delete": "fact_financial_statement_canonical",
            "census_delete": "fact_specialized_metric_disclosure_census",
            "feature_delete": "feature_financial_statement",
            "summary_delete": "fact_specialized_metric_disclosure_summary",
        }
        for operation, details in plans.items():
            assert any(required[operation] in detail for detail in details), details
            assert not any(
                detail == f"SCAN {table_names[operation]}" for detail in details
            ), details

        assert any(
            "INTEGER PRIMARY KEY" in detail
            for detail in plans["canonical_identity_update"]
        ), plans["canonical_identity_update"]


def test_existing_v8_database_upgrades_to_v9_once_and_preserves_history(
    tmp_path: Path,
) -> None:
    bundle = load_config(CONFIG)
    with connect(tmp_path / "v8_to_v9.sqlite") as conn:
        bootstrap_stage4(conn, bundle)
        history_before = tuple(
            tuple(row)
            for row in conn.execute(
                """SELECT migration_version,migration_name,migration_sha256,status
                   FROM consumer_defensive_stage4_schema_migration
                   WHERE migration_version<=8 ORDER BY migration_version"""
            )
        )
        version, name, checksum = next(
            row for row in STAGE4_MIGRATION_HISTORY if row[0] == 9
        )
        assert version == 9
        with conn:
            conn.execute(
                """DELETE FROM consumer_defensive_stage4_schema_migration
                   WHERE migration_version>=9"""
            )
            conn.execute("DROP INDEX idx_stage4_raw_accession_fact")
            conn.execute("DROP INDEX idx_stage4_canonical_accession")
            conn.execute("DROP INDEX idx_stage4_census_accession")

        ensure_stage4_schema(conn)
        ensure_stage4_schema(conn)

        assert tuple(
            tuple(row)
            for row in conn.execute(
                """SELECT migration_version,migration_name,migration_sha256,status
                   FROM consumer_defensive_stage4_schema_migration
                   WHERE migration_version<=8 ORDER BY migration_version"""
            )
        ) == history_before
        assert tuple(
            conn.execute(
                """SELECT migration_version,migration_name,migration_sha256,status
                   FROM consumer_defensive_stage4_schema_migration
                   WHERE migration_version=9"""
            ).fetchone()
        ) == (version, name, checksum, "complete")
        assert conn.execute(
            """SELECT COUNT(*) FROM consumer_defensive_stage4_schema_migration
               WHERE migration_version=9"""
        ).fetchone()[0] == 1
        assert _index_columns(conn, "idx_stage4_raw_accession_fact") == (
            "accession_number",
            "raw_fact_id",
        )
        assert _index_columns(conn, "idx_stage4_canonical_accession") == (
            "accession_number",
        )
        assert _index_columns(conn, "idx_stage4_census_accession") == (
            "accession_number",
        )


def test_fx_idempotent_delete_uses_existing_primary_key_index(tmp_path: Path) -> None:
    bundle = load_config(CONFIG)
    with connect(tmp_path / "fx_plan.sqlite") as conn:
        bootstrap_stage4(conn, bundle)
        fx_delete_plan = _plan_details(
            conn,
            """
            DELETE FROM fact_fx_rate
            WHERE base_currency=? AND quote_currency='USD'
              AND source_id=? AND rate_date BETWEEN ? AND ?
            """,
            ("EUR", "yahoo_fx_rates", "2024-01-01", "2024-12-31"),
        )
        assert any(
            "sqlite_autoindex_fact_fx_rate_1" in detail
            and "base_currency=? AND quote_currency=?" in detail
            and "rate_date>? AND rate_date<?" in detail
            for detail in fx_delete_plan
        ), fx_delete_plan
        assert not any(detail == "SCAN fact_fx_rate" for detail in fx_delete_plan)
