from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from industrials.core.db import init_db, utc_now
from industrials.transportation.ticker_scoped_xbrl_backfill import (
    load_ticker_scoped_concept_rules,
    materialize_ticker_scoped_xbrl_facts,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RULES_PATH = (
    PROJECT_ROOT
    / "industrials"
    / "transportation"
    / "system_csvs"
    / "transportation_ticker_xbrl_concept_aliases.csv"
)


def _insert_raw(connection: sqlite3.Connection, ticker: str) -> None:
    now = utc_now()
    connection.execute(
        """
        INSERT INTO fact_sec_xbrl_fact_raw(
            fact_key,ticker,cik,source_id,accession_number,form_type,
            filing_date,accepted_at,fiscal_year,fiscal_period,period_start,
            period_end,frame,taxonomy,concept_name,unit,raw_value,decimals,
            source_detail,payload_json,created_at,updated_at
        ) VALUES (
            ?,?,'0000000001','sec_companyfacts',?,'20-F','2024-03-01',
            '2024-03-01T12:00:00Z',2023,'FY','2023-01-01','2023-12-31',
            'CY2023','ifrs-full',
            'RevenueFromRenderingOfCargoAndMailTransportServices','USD',
            100.0,'0','sec_companyfacts','{}',?,?
        )
        """,
        (f"raw-{ticker}", ticker, f"accession-{ticker}", now, now),
    )


def test_ticker_scoped_mapping_cannot_leak_to_other_issuer(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "ticker_scope.sqlite")
    connection.row_factory = sqlite3.Row
    init_db(connection)
    now = utc_now()
    connection.execute(
        """
        INSERT INTO source_registry(
            source_id,stage,source_name,source_type,base_url,status,
            created_at,updated_at
        ) VALUES (
            'sec_companyfacts','financials','SEC CompanyFacts','test',
            'https://data.sec.gov','active',?,?
        )
        """,
        (now, now),
    )
    _insert_raw(connection, "TRMD")
    _insert_raw(connection, "VLRS")
    rules = load_ticker_scoped_concept_rules(RULES_PATH)

    planned = materialize_ticker_scoped_xbrl_facts(
        connection, rules=rules, asof=date(2026, 7, 30), execute=False
    )
    assert planned["eligible_raw_fact_count"] == 1
    assert planned["database_change_count"] == 0

    executed = materialize_ticker_scoped_xbrl_facts(
        connection, rules=rules, asof=date(2026, 7, 30), execute=True
    )
    assert executed["database_change_count"] == 1
    rows = connection.execute(
        """
        SELECT ticker,canonical_metric,source_priority,source_detail
        FROM fact_sec_xbrl_fact
        WHERE concept_name='RevenueFromRenderingOfCargoAndMailTransportServices'
        """
    ).fetchall()
    assert [dict(row) for row in rows] == [
        {
            "ticker": "TRMD",
            "canonical_metric": "revenue",
            "source_priority": 30,
            "source_detail": "sec_companyfacts_ticker_scoped_reviewed",
        }
    ]
    connection.close()
