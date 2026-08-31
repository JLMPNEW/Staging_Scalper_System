from __future__ import annotations

import runpy
import sqlite3
from datetime import date
from pathlib import Path

from industrials.core.db import init_db
from industrials.core.ticker_continuity import ticker_continuity_chain


ROOT = Path(__file__).resolve().parents[2]


def seed_alias(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO source_registry(
            source_id, stage, source_name, source_type, base_url,
            subsector_scope, status, created_at, updated_at
        ) VALUES ('test', 'stage_1', 'test', 'csv', '',
                  'defense', 'active', 'now', 'now')
        """
    )
    conn.execute(
        """
        INSERT INTO dim_ticker_alias(
            contract_ticker, active_ticker, predecessor_ticker,
            effective_date, issuer_id, reason, source_id, verified_flag,
            created_at, updated_at
        ) VALUES (
            'IA', 'IA', 'ISSC', '2026-08-18', '0000836690',
            'ticker_change', 'test', 1, 'now', 'now'
        )
        """
    )


def test_continuity_chain_is_date_effective() -> None:
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        init_db(conn)
        seed_alias(conn)
        assert ticker_continuity_chain(conn, "IA", asof=date(2026, 8, 17)) == ("IA",)
        assert ticker_continuity_chain(conn, "IA", asof=date(2026, 8, 18)) == ("IA", "ISSC")


def test_feature_readers_prefer_current_but_fall_back_to_predecessor() -> None:
    namespace = runpy.run_path(
        str(ROOT / "industrials" / "scripts" / "09_import_industrials_positioning.py")
    )
    latest_row = namespace["latest_row"]
    latest_short_row = namespace["latest_short_row"]

    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        init_db(conn)
        seed_alias(conn)
        conn.execute(
            """
            INSERT OR IGNORE INTO source_registry(
                source_id, stage, source_name, source_type, base_url,
                subsector_scope, status, created_at, updated_at
            ) VALUES ('market_positioning_upstream', 'stage_5', 'test', 'db', '',
                      'defense', 'active', 'now', 'now')
            """
        )
        conn.execute(
            """
            INSERT INTO fact_13f_positioning(
                ticker, asof_date, period_of_report, source_id,
                institutional_shares, created_at, updated_at
            ) VALUES ('ISSC', '2026-05-27', '2026-03-31',
                      'market_positioning_upstream', 123.0, 'now', 'now')
            """
        )
        conn.execute(
            """
            INSERT INTO fact_short_interest(
                ticker, settlement_date, source_id, publication_date,
                short_interest_shares, created_at, updated_at
            ) VALUES ('ISSC', '2026-07-31', 'market_positioning_upstream',
                      '2026-08-11', 45.0, 'now', 'now')
            """
        )
        lookup = ticker_continuity_chain(conn, "IA", asof=date(2026, 8, 28))
        inst = latest_row(
            conn,
            "fact_13f_positioning",
            "IA",
            "asof_date",
            "2026-08-28",
            source_ids=["market_positioning_upstream"],
            tiebreak_cols=("period_of_report",),
            lookup_tickers=lookup,
        )
        short = latest_short_row(
            conn,
            "IA",
            "2026-08-28",
            source_ids=["market_positioning_upstream"],
            lookup_tickers=lookup,
        )
        assert inst is not None and inst["ticker"] == "ISSC"
        assert short is not None and short["ticker"] == "ISSC"
