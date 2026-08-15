from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from industrials.core.pit_lineage_canary import (
    build_canary_snapshot,
    open_readonly_database,
    representative_dates,
    run_pit_lineage_canary,
)


ASOF = "2021-12-31"


def _database(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE dim_universe_membership (
            ticker TEXT, company_id INTEGER, model_family TEXT,
            membership_source_id TEXT, membership_status TEXT,
            start_date TEXT, end_date TEXT, point_in_time_flag INTEGER
        );
        CREATE TABLE fact_sec_filing (
            ticker TEXT, accession_number TEXT, form_type TEXT,
            filing_date TEXT, accepted_at TEXT, report_date TEXT,
            primary_document TEXT, filing_url TEXT
        );
        CREATE TABLE fact_financial_statement_canonical (
            ticker TEXT, model_family TEXT, canonical_metric TEXT,
            accession_number TEXT, accepted_at TEXT, filing_date TEXT
        );
        CREATE TABLE feature_financial_statement (
            ticker TEXT, model_family TEXT, asof_date TEXT
        );
        CREATE TABLE sec_parser_document_catalog (
            accession_number TEXT, source_path TEXT,
            is_full_submission INTEGER, is_primary INTEGER, file_size INTEGER
        );
        """
    )
    return conn


def _seed(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "INSERT INTO dim_universe_membership VALUES (?,?,?,?,?,?,?,?)",
        [
            ("AAA", 1, "machinery", "seed", "active", "2019-01-01", None, 1),
            ("OLD", 2, "machinery", "seed", "historical_delisted", "2019-01-01", "2020-12-31", 1),
            ("NEW", 3, "machinery", "seed", "active", "2022-01-01", None, 1),
        ],
    )
    conn.execute(
        "INSERT INTO fact_sec_filing VALUES (?,?,?,?,?,?,?,?)",
        ("AAA", "aaa-2021", "10-K", "2021-03-01", "2021-03-01T10:00:00", "2020-12-31", "", ""),
    )
    conn.executemany(
        "INSERT INTO fact_financial_statement_canonical VALUES (?,?,?,?,?,?)",
        [
            ("AAA", "machinery", metric, "aaa-2021", "2021-03-01T10:00:00", "2021-03-01")
            for metric in ("revenue", "assets", "operating_income")
        ],
    )
    conn.execute(
        "INSERT INTO feature_financial_statement VALUES (?,?,?)",
        ("AAA", "machinery", ASOF),
    )
    conn.commit()


def test_representative_dates_are_bounded_and_include_terminal_asof() -> None:
    assert representative_dates(start_year=2019, asof="2026-08-14") == [
        "2019-12-31",
        "2020-12-31",
        "2021-12-31",
        "2022-12-31",
        "2023-12-31",
        "2024-12-31",
        "2025-12-31",
        "2026-03-31",
        "2026-06-30",
        "2026-08-14",
    ]


def test_snapshot_uses_effective_pit_membership_and_passes_safe_lineage(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "test.sqlite"
    conn = _database(db_path)
    _seed(conn)

    rows, manifest = build_canary_snapshot(conn, model_family="machinery", asof=ASOF)

    assert [row["ticker"] for row in rows] == ["AAA"]
    assert rows[0]["financial_lineage_gate"] == "1"
    assert rows[0]["incorporated_availability_date"] == "2021-03-01"
    assert manifest["acceptance"] == "PASS"


def test_readonly_connection_rejects_mutation(tmp_path: Path) -> None:
    db_path = tmp_path / "test.sqlite"
    conn = _database(db_path)
    _seed(conn)
    conn.close()

    with open_readonly_database(db_path) as readonly:
        with pytest.raises(sqlite3.OperationalError):
            readonly.execute("DELETE FROM dim_universe_membership")


def test_canary_fails_closed_and_writes_isolated_artifacts(tmp_path: Path) -> None:
    db_path = tmp_path / "test.sqlite"
    conn = _database(db_path)
    _seed(conn)
    conn.execute(
        "INSERT INTO dim_universe_membership VALUES (?,?,?,?,?,?,?,?)",
        ("GAP", 4, "machinery", "seed", "active", "2021-01-01", None, 1),
    )
    conn.commit()
    conn.close()

    output_dir = tmp_path / "isolated"
    manifest = run_pit_lineage_canary(
        db_path=db_path,
        output_dir=output_dir,
        model_families=["machinery"],
        dates=[ASOF],
    )

    assert manifest["acceptance"] == "FAIL"
    assert manifest["database_access"] == "sqlite_read_only_query_only"
    assert (output_dir / "industrials_pit_financial_lineage_canary.csv").exists()
    assert (output_dir / "industrials_pit_financial_lineage_canary.json").exists()


def test_membership_conflict_cannot_be_overwritten_by_lineage_pass(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "test.sqlite"
    conn = _database(db_path)
    _seed(conn)
    conn.execute(
        "INSERT INTO dim_universe_membership VALUES (?,?,?,?,?,?,?,?)",
        (
            "AAA",
            1,
            "machinery",
            "second_seed",
            "historical_delisted",
            "2021-01-01",
            None,
            1,
        ),
    )
    conn.commit()

    rows, manifest = build_canary_snapshot(conn, model_family="machinery", asof=ASOF)

    assert rows[0]["financial_lineage_gate"] == "1"
    assert manifest["blocking_issue_count"] == 0
    assert manifest["membership_issue_count"] == 1
    assert manifest["acceptance"] == "FAIL"
