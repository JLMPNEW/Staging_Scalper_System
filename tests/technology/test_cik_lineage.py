from __future__ import annotations

import sqlite3

import pytest

from technology.core.cik_lineage import (
    configured_legacy_ciks,
    expand_company_cik_lineage,
    upsert_configured_cik_identifiers,
)


def test_expand_company_cik_lineage_keeps_primary_last() -> None:
    companies = [{"ticker": "DMRC", "cik": "2119322", "company_name": "Digimarc"}]
    aliases = {"DMRC": [{"cik": "1438231", "end_date": "2026-05-20"}]}

    expanded = expand_company_cik_lineage(companies, aliases)

    assert [(row["cik"], row["cik_role"]) for row in expanded] == [
        ("0001438231", "legacy"),
        ("0002119322", "primary"),
    ]
    assert all(row["primary_cik"] == "0002119322" for row in expanded)


def test_legacy_cik_configuration_rejects_invalid_values() -> None:
    with pytest.raises(ValueError, match="Invalid legacy CIK"):
        configured_legacy_ciks({"DMRC": [{"cik": ""}]}, "DMRC")


def test_upsert_configured_cik_identifiers_is_idempotent() -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE dim_company(company_id INTEGER PRIMARY KEY, ticker TEXT UNIQUE);
        CREATE TABLE dim_identifier(
            identifier_id INTEGER PRIMARY KEY,
            company_id INTEGER,
            identifier_type TEXT,
            identifier_value TEXT,
            source_id TEXT,
            confidence REAL,
            created_at TEXT,
            updated_at TEXT
        );
        INSERT INTO dim_company(company_id, ticker) VALUES (1, 'DMRC');
        """
    )
    expanded = expand_company_cik_lineage(
        [{"ticker": "DMRC", "cik": "2119322"}],
        {"DMRC": ["1438231"]},
    )

    upsert_configured_cik_identifiers(connection, expanded)
    upsert_configured_cik_identifiers(connection, expanded)

    assert connection.execute(
        "SELECT identifier_value FROM dim_identifier"
    ).fetchall() == [("0001438231",)]
