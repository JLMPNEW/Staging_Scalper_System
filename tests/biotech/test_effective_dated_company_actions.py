from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

from tests.biotech.conftest import load_script_module


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_recent_company_status_overrides_apply_only_on_effective_date() -> None:
    module = load_script_module("02_build_company_master.py", "company_master_effective_status")
    overrides = module.load_status_overrides(
        PROJECT_ROOT / "biotech_index" / "data" / "company_status_overrides.csv"
    )

    expected = {
        "LIXT": "2026-07-06",
        "ESPR": "2026-07-13",
        "NUVL": "2026-07-15",
    }
    for ticker, effective_date in expected.items():
        override = overrides[ticker]
        prior_date = date.fromisoformat(effective_date) - timedelta(days=1)
        assert module.status_override_is_effective(override, asof_date=prior_date.isoformat()) is False
        assert module.status_override_is_effective(override, asof_date=effective_date) is True


def test_historical_universe_ends_nonretained_membership_on_effective_date() -> None:
    module = load_script_module("57_build_historical_scoring_universe.py", "historical_membership_end_actions")
    actions = module.load_nonretained_ticker_actions(
        PROJECT_ROOT / "biotech_index" / "data" / "company_ticker_actions.csv"
    )
    root_rows = [{"ticker": "NUVL", "company_name": "Nuvalent, Inc.", "scoring_include": "true"}]
    companies = {
        "NUVL": {
            "company_id": 1,
            "ticker": "NUVL",
            "company_name": "Nuvalent, Inc.",
            "is_active": 0,
        }
    }
    prices = {
        "NUVL": {
            "first_price_date": "2021-07-29",
            "last_price_date": "2026-07-14",
            "latest_price_date": "2026-07-14",
        }
    }

    before_rows, before_audit = module.live_universe_rows(
        root_rows,
        companies=companies,
        prices=prices,
        asof=date(2026, 7, 14),
        max_price_staleness_days=10,
        nonretained_ticker_actions=actions,
    )
    after_rows, after_audit = module.live_universe_rows(
        root_rows,
        companies=companies,
        prices=prices,
        asof=date(2026, 7, 15),
        max_price_staleness_days=10,
        nonretained_ticker_actions=actions,
    )

    assert [row["ticker"] for row in before_rows] == ["NUVL"]
    assert before_audit[0]["reason"] == "inactive_now_but_priced_on_asof"
    assert after_rows == []
    assert after_audit == [
        {
            "ticker": "NUVL",
            "decision": "exclude",
            "reason": "membership_ended:acquired:2026-07-15",
        }
    ]


def test_offline_ib_company_loader_can_use_inactive_pit_members() -> None:
    module = load_script_module("17_sync_market_data_ib.py", "ib_historical_inactive_company_loader")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE companies(
            company_id INTEGER PRIMARY KEY,
            ticker TEXT NOT NULL,
            company_name TEXT,
            currency TEXT,
            is_active INTEGER NOT NULL
        );
        INSERT INTO companies VALUES (1, 'ACTIVE', 'Active Co', 'USD', 1);
        INSERT INTO companies VALUES (2, 'HIST', 'Historical Co', 'USD', 0);
        """
    )

    active_only = module.load_companies(
        conn,
        scoring_tickers={"ACTIVE", "HIST"},
        ticker_filter=set(),
        max_tickers=0,
    )
    pit_members = module.load_companies(
        conn,
        scoring_tickers={"ACTIVE", "HIST"},
        ticker_filter=set(),
        max_tickers=0,
        include_inactive=True,
    )

    assert [company.ticker for company in active_only] == ["ACTIVE"]
    assert [company.ticker for company in pit_members] == ["ACTIVE", "HIST"]
