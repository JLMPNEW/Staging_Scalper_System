from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from consumer_defensive.core.db import init_db, utc_now
from consumer_defensive.core.market_data import NORGATE_SOURCE_ID, YAHOO_SOURCE_ID, PriceBar, upsert_price_bars
from consumer_defensive.core.source_registry import load_source_registry, upsert_source_registry
from consumer_defensive.core.terminal_events import (
    load_terminal_event_ledger,
    load_terminal_event_policy,
    load_norgate_successor_prices,
    reconcile_terminal_events,
    terminal_horizon_value,
    validate_terminal_events,
    norgate_successor_events,
    yahoo_successor_tickers,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "consumer_defensive"
POLICY_PATH = PACKAGE_ROOT / "data" / "consumer_defensive_terminal_event_policy.yaml"
SOURCE_REGISTRY = PACKAGE_ROOT / "data" / "free_source_registry.yaml"


def seed_delisted(conn: sqlite3.Connection, tickers: list[str]) -> None:
    now = utc_now()
    for ticker in tickers:
        company_id = conn.execute(
            """
            INSERT INTO dim_company(primary_ticker, company_name, universe_status, is_active,
                data_quality_status, first_seen_at, updated_at)
            VALUES (?, ?, 'historical', 0, 'terminal_reconciliation_pending', ?, ?)
            """,
            (ticker, ticker, now, now),
        ).lastrowid
        conn.execute(
            """
            INSERT INTO dim_security(company_id, ticker, provider_price_symbol, exchange,
                listing_country, security_type, listing_status, is_primary_listing,
                currency, listing_start_date, listing_end_date, created_at, updated_at)
            VALUES (?, ?, ?, 'NYSE', 'United States', 'Common Stock', 'delisted', 1,
                'USD', '2017-11-28', '2026-01-01', ?, ?)
            """,
            (company_id, ticker, f"{ticker}-HIST", now, now),
        )


def bar(ticker: str, bar_date: str, source_id: str, close: float, adjusted: float) -> PriceBar:
    return PriceBar(
        ticker=ticker,
        bar_date=bar_date,
        source_id=source_id,
        open=close,
        high=close,
        low=close,
        close=close,
        adjusted_close=adjusted,
        volume=1_000_000,
        dividend=None,
        split_factor=None,
        total_return_basis="test_total_return",
        source_timestamp=utc_now(),
    )


def initialized_connection() -> tuple[sqlite3.Connection, object, list[object]]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    upsert_source_registry(conn, load_source_registry(SOURCE_REGISTRY))
    policy = load_terminal_event_policy(POLICY_PATH)
    events = load_terminal_event_ledger(policy)
    seed_delisted(conn, [event.ticker for event in events])
    return conn, policy, events


def test_reviewed_ledger_has_exact_terms_and_one_explicit_exclusion() -> None:
    policy = load_terminal_event_policy(POLICY_PATH)
    events = {event.ticker: event for event in load_terminal_event_ledger(policy)}
    assert len(events) == 11
    assert {ticker for ticker, event in events.items() if not event.calibration_eligible} == {"WBA"}
    assert events["CORE"].cash_consideration == 23.875
    assert events["CORE"].successor_ticker == "PFGC"
    assert events["CORE"].successor_share_ratio == 0.44
    assert events["AVP"].successor_price_source_id == NORGATE_SOURCE_ID
    assert events["AVP"].successor_provider_symbol == "NTCOY-202408"
    assert events["TWNK"].successor_share_ratio == 0.03002
    assert events["DF"].economic_event_date == "2021-05-28"
    assert events["DF"].provider_last_quoted_date == "2021-06-02"
    assert events["DF"].fixed_terminal_value == 0


def test_successor_price_requests_share_one_point_in_time_predicate() -> None:
    events = load_terminal_event_ledger(load_terminal_event_policy(POLICY_PATH))
    assert norgate_successor_events(events, as_of="2019-12-31") == []
    assert [event.ticker for event in norgate_successor_events(events, as_of="2020-01-06")] == ["AVP"]
    assert yahoo_successor_tickers(events, as_of="2021-08-31") == []
    assert yahoo_successor_tickers(events, as_of="2021-09-01") == ["PFGC"]
    assert yahoo_successor_tickers(events, as_of="2023-11-06") == ["PFGC"]
    assert yahoo_successor_tickers(events, as_of="2023-11-07") == ["PFGC", "SJM"]


@pytest.mark.parametrize(
    "drift_database",
    ["US Equities", "US Equities Delisted"],
)
def test_norgate_successor_drift_publishes_no_price_rows(
    drift_database: str,
) -> None:
    class Adjustment:
        NONE = "none"
        TOTALRETURN = "totalreturn"

    class DriftingProvider:
        StockPriceAdjustmentType = Adjustment

        def __init__(self, component: str) -> None:
            self.component = component
            self.price_calls = 0
            self.drifted = False

        def last_database_update_time(self, database: str) -> str:
            stable = f"stable:{database}"
            if self.drifted and database == self.component:
                return stable + ":changed"
            return stable

        def price_timeseries(self, *args, **kwargs):
            pd = pytest.importorskip("pandas")
            frame = pd.DataFrame(
                {
                    "Open": [10.0, 10.5],
                    "High": [11.0, 11.5],
                    "Low": [9.0, 9.5],
                    "Close": [10.0, 10.5],
                    "Volume": [1000.0, 1100.0],
                    "Dividend": [0.0, 0.0],
                },
                index=pd.to_datetime(["2020-01-06", "2020-01-07"]),
            )
            self.price_calls += 1
            if self.price_calls >= 2:
                self.drifted = True
            return frame

    conn, _, events = initialized_connection()
    try:
        with pytest.raises(RuntimeError, match="provider databases changed"):
            load_norgate_successor_prices(
                conn,
                events,
                provider=DriftingProvider(drift_database),
                end="2020-01-07",
            )
        assert conn.execute(
            "SELECT COUNT(*) FROM fact_price_ohlcv WHERE source_id=?",
            (NORGATE_SOURCE_ID,),
        ).fetchone()[0] == 0
        run = conn.execute(
            """SELECT status,row_count,message FROM ingestion_runs
               WHERE source_id=? ORDER BY ingestion_run_id DESC LIMIT 1""",
            (NORGATE_SOURCE_ID,),
        ).fetchone()
        assert run is not None
        assert (str(run["status"]), int(run["row_count"])) == ("failed", 0)
        message = json.loads(str(run["message"]))
        assert message["changed_databases"] == [drift_database]
    finally:
        conn.close()


def test_reconciliation_upserts_canonical_event_contracts() -> None:
    conn, policy, _ = initialized_connection()
    try:
        now = utc_now()
        for row in conn.execute("SELECT company_id,security_id,ticker FROM dim_security").fetchall():
            conn.execute(
                """INSERT INTO dim_universe_membership(company_id,security_id,ticker,model_family,membership_source_id,membership_basis,recognized_vehicle,start_date,end_date,membership_status,is_current_member,point_in_time_flag,live_investable_flag,historical_calibration_eligible_flag,confidence,reason,created_at,updated_at)
                   VALUES(?,?,?,'consumer_defensive',NULL,'test','test','2019-01-02','2026-01-01','historical',0,1,0,0,1.0,'test',?,?)""",
                (row[0], row[1], row[2], now, now),
            )
        result = reconcile_terminal_events(conn, policy)
        assert result == {
            "events_loaded": 11,
            "survivorship_complete": 10,
            "calibration_eligible": 10,
            "explicitly_excluded": ["WBA"],
        }
        assert conn.execute("SELECT COUNT(*) FROM fact_terminal_event_reconciliation").fetchone()[0] == 11
        core = conn.execute(
            "SELECT cash_consideration, successor_ticker, successor_share_ratio FROM fact_terminal_event_reconciliation WHERE ticker='CORE'"
        ).fetchone()
        assert tuple(core) == (23.875, "PFGC", 0.44)
        wba = conn.execute(
            "SELECT terminal_value, survivorship_complete FROM fact_security_event WHERE ticker='WBA'"
        ).fetchone()
        assert tuple(wba) == (11.45, 0)
        flags = dict(conn.execute(
            "SELECT ticker,historical_calibration_eligible_flag FROM dim_universe_membership"
        ).fetchall())
        assert flags["WBA"] == 0
        assert sum(flags.values()) == 10
        validated = validate_terminal_events(conn, policy, as_of="2026-08-10")
        assert next(row for row in validated["checks"] if row["check"] == "membership_terminal_calibration_eligibility_consistent")["status"] == "PASS"
    finally:
        conn.close()


def test_terminal_value_resolver_handles_cash_wipeout_contingent_and_stock() -> None:
    conn, policy, _ = initialized_connection()
    try:
        reconcile_terminal_events(conn, policy)
        with conn:
            upsert_price_bars(
                conn,
                [
                    bar("PFGC", "2021-09-01", YAHOO_SOURCE_ID, 40.0, 35.0),
                    bar("PFGC", "2022-01-03", YAHOO_SOURCE_ID, 50.0, 42.0),
                ],
            )
        cash = terminal_horizon_value(conn, policy, ticker="SPTN", horizon_date="2025-10-01")
        assert cash["terminal_value"] == 26.9
        assert cash["calculation_status"] == "resolved_fixed_terminal_value"
        wipeout = terminal_horizon_value(conn, policy, ticker="DF", horizon_date="2021-06-02")
        assert wipeout["terminal_value"] == 0
        pending = terminal_horizon_value(conn, policy, ticker="WBA", horizon_date="2026-01-01")
        assert pending["terminal_value"] == 11.45
        assert pending["calibration_eligible"] == 0
        assert pending["calculation_status"] == "contingent_value_unresolved"
        pre_event = terminal_horizon_value(conn, policy, ticker="WBA", horizon_date="2025-08-27")
        assert pre_event["terminal_value"] is None
        assert pre_event["calibration_eligible"] == 1
        assert pre_event["calculation_status"] == "pre_terminal_event"
        stock = terminal_horizon_value(conn, policy, ticker="CORE", horizon_date="2022-01-03")
        expected = 23.875 + 0.44 * 40.0 * (42.0 / 35.0)
        assert stock["terminal_value"] == pytest.approx(expected)
        assert stock["stock_component"] == pytest.approx(expected - 23.875)
    finally:
        conn.close()


def test_full_validation_accepts_ten_complete_and_one_visible_exclusion() -> None:
    conn, policy, events = initialized_connection()
    try:
        reconcile_terminal_events(conn, policy)
        rows: list[PriceBar] = []
        for event in events:
            rows.append(
                bar(
                    event.ticker,
                    event.provider_last_quoted_date,
                    NORGATE_SOURCE_ID,
                    10.0,
                    10.0,
                )
            )
            if event.successor_ticker:
                start = date.fromisoformat(event.successor_reference_date)
                for offset in range(127):
                    current = (start + timedelta(days=offset)).isoformat()
                    rows.append(
                        bar(
                            event.successor_ticker,
                            current,
                            event.successor_price_source_id,
                            20.0 + offset / 100,
                            20.0 + offset / 100,
                        )
                    )
        with conn:
            upsert_price_bars(conn, rows)
        result = validate_terminal_events(conn, policy, as_of="2026-08-10")
        assert result["status"] == "PASS", result
        assert result["reconciliation_state"] == "PASS_WITH_EXCLUSION"
        assert result["counts"] == {
            "events": 11,
            "survivorship_complete": 10,
            "calibration_eligible": 10,
            "explicitly_excluded": 1,
        }
        assert any("unresolved_contingent_consideration" in warning for warning in result["warnings"])
    finally:
        conn.close()


def test_reconciliation_ignores_superseded_identity_corrections() -> None:
    conn, policy, _ = initialized_connection()
    try:
        now = utc_now()
        company_id = conn.execute(
            """INSERT INTO dim_company(
                   primary_ticker, company_name, universe_status, is_active,
                   data_quality_status, first_seen_at, updated_at
               ) VALUES ('DMC', 'Legacy Fresh Del Monte identity',
                   'superseded_identity', 0, 'reviewed', ?, ?)""",
            (now, now),
        ).lastrowid
        conn.execute(
            """INSERT INTO dim_security(
                   company_id, ticker, provider_price_symbol, exchange,
                   listing_country, security_type, listing_status,
                   is_primary_listing, currency, created_at, updated_at
               ) VALUES (?, 'DMC', 'DMC', 'NYSE', 'United States',
                   'Common Stock', 'superseded', 0, 'USD', ?, ?)""",
            (company_id, now, now),
        )
        result = reconcile_terminal_events(conn, policy)
        assert result["events_loaded"] == 11
        assert conn.execute(
            "SELECT COUNT(*) FROM fact_terminal_event_reconciliation WHERE ticker='DMC'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_terminal_validation_ignores_quotes_after_requested_asof() -> None:
    conn, policy, events = initialized_connection()
    as_of = '2021-05-29'
    try:
        reconcile_terminal_events(conn, policy)
        rows: list[PriceBar] = []
        for event in events:
            if (
                event.economic_event_date <= as_of
                and event.provider_last_quoted_date <= as_of
            ):
                rows.append(
                    bar(
                        event.ticker,
                        event.provider_last_quoted_date,
                        NORGATE_SOURCE_ID,
                        10.0,
                        10.0,
                    )
                )
        rows.append(bar('AVP', '2026-01-02', NORGATE_SOURCE_ID, 99.0, 99.0))
        with conn:
            upsert_price_bars(conn, rows)
        result = validate_terminal_events(conn, policy, as_of=as_of)
        provider_check = next(
            row for row in result['checks'] if row['check'] == 'provider_last_quote_reconciled'
        )
        assert provider_check['status'] == 'PASS', result
        assert any(
            row['check'] == 'provider_last_quote_not_yet_observable'
            for row in result['checks']
        )
    finally:
        conn.close()
