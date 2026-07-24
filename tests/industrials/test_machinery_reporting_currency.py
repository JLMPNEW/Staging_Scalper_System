from __future__ import annotations

import sqlite3

from industrials.core.db import init_db
from industrials.machinery.reporting_currency import resolve_reporting_currency


def test_reporting_currency_prefers_latest_pit_feature_then_canonical() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    now = "2026-07-20T00:00:00Z"
    conn.execute(
        """
        INSERT INTO source_registry(
            source_id, stage, source_name, source_type, base_url,
            status, created_at, updated_at
        ) VALUES ('sec_companyfacts', 'fundamentals', 'SEC', 'api', 'sec.gov',
                  'active', ?, ?)
        """,
        (now, now),
    )
    conn.execute(
        """
        INSERT INTO fact_financial_statement_canonical(
            ticker, source_id, model_family, canonical_metric, period_end,
            period_start, filing_date, accepted_at, accession_number, unit,
            value, source_priority, canonical_quality, created_at, updated_at
        ) VALUES ('ATS', 'sec_companyfacts', 'machinery', 'revenue', '2025-03-31',
                  '2024-04-01', '2025-05-20', '2025-05-20', 'a', 'CAD',
                  100.0, 10, 'test', ?, ?)
        """,
        (now, now),
    )
    assert resolve_reporting_currency(
        conn,
        ticker="ATS",
        model_family="machinery",
        asof="2025-07-01",
        fallback="USD",
    ) == "CAD"
    conn.execute(
        """
        INSERT INTO feature_financial_statement(
            ticker, asof_date, source_id, model_family, reported_currency,
            created_at, updated_at
        ) VALUES ('ATS', '2026-06-30', 'sec_companyfacts', 'machinery', 'EUR', ?, ?)
        """,
        (now, now),
    )
    assert resolve_reporting_currency(
        conn,
        ticker="ATS",
        model_family="machinery",
        asof="2026-07-20",
        fallback="USD",
    ) == "EUR"
    assert resolve_reporting_currency(
        conn,
        ticker="NEW",
        model_family="machinery",
        asof="2026-07-20",
        fallback="usd",
    ) == "USD"
