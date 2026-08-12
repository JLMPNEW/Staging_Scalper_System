from __future__ import annotations

import json
import math
import statistics
import sqlite3
from datetime import date, datetime, time as datetime_time, timezone
from pathlib import Path

import pytest

from consumer_defensive.core.config import cfg_get, load_config, resolve_path
from consumer_defensive.core.db import connect, init_db, utc_now
from consumer_defensive.core.market_data import (
    NORGATE_SOURCE_ID,
    SELECTION_PURPOSE,
    YAHOO_SOURCE_ID,
    PriceBar,
    build_market_features,
    coverage_qualifies,
    ensure_stage3_schema,
    load_market_policy,
    select_price_sources,
    trading_calendar_coverage,
    upsert_price_bars,
    _aligned_residual_return,
    _annualized_volatilities,
)
from consumer_defensive.core.market_validation import validate_stage3_market_data
from consumer_defensive.core.norgate_prices import fetch_norgate_prices, load_norgate_prices
from consumer_defensive.core.source_registry import load_source_registry, upsert_source_registry
from consumer_defensive.core.universe import ensure_stage2_schema, load_policy, upsert_stage2_sources
from consumer_defensive.core.yahoo_prices import parse_yahoo_payload


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "consumer_defensive"
CONFIG_PATH = PACKAGE_ROOT / "config.yaml"
UNIVERSE_POLICY_PATH = PACKAGE_ROOT / "data" / "consumer_defensive_universe_policy.yaml"
MARKET_POLICY_PATH = PACKAGE_ROOT / "data" / "consumer_defensive_market_data_policy.yaml"
STAGE2_SOURCES = PACKAGE_ROOT / "data" / "stage2_source_registry.yaml"


def initialize(conn: sqlite3.Connection) -> None:
    bundle = load_config(CONFIG_PATH)
    init_db(conn)
    ensure_stage2_schema(conn)
    ensure_stage3_schema(conn)
    upsert_source_registry(
        conn,
        load_source_registry(resolve_path(cfg_get(bundle.payload, "source_registry.path"), base_dir=bundle.base_dir)),
    )
    upsert_stage2_sources(conn, load_source_registry(STAGE2_SOURCES))


def epoch(raw: str) -> int:
    return int(datetime.combine(date.fromisoformat(raw), datetime_time.min, tzinfo=timezone.utc).timestamp())


def test_market_policy_records_provider_roles_and_prohibits_splicing() -> None:
    policy = load_market_policy(MARKET_POLICY_PATH)
    assert policy.payload["sources"] == {
        "active_primary": YAHOO_SOURCE_ID,
        "historical_delisted_primary": NORGATE_SOURCE_ID,
        "active_whole_ticker_fallback": NORGATE_SOURCE_ID,
        "source_selection_granularity": "ticker",
        "cross_source_date_splicing_allowed": False,
    }
    assert policy.payload["benchmarks"]["required_source"] == YAHOO_SOURCE_ID
    assert policy.payload["history_buffer_calendar_days"] == 400
    assert policy.payload["selection"]["maximum_missing_trading_day_ratio"] == 0.02
    assert policy.payload["selection"]["missing_trading_day_warning_ratio"] == 0.01
    assert policy.payload["selection"]["maximum_consecutive_missing_trading_days"] == 5


def test_yahoo_payload_preserves_raw_ohlcv_and_adjusted_return_series() -> None:
    payload = json.dumps(
        {
            "chart": {
                "error": None,
                "result": [
                    {
                        "meta": {"symbol": "BF-B", "currency": "USD", "regularMarketTime": 123},
                        "timestamp": [epoch("2019-01-02"), epoch("2019-01-03")],
                        "indicators": {
                            "quote": [{"open": [10, 11], "high": [12, 13], "low": [9, 10], "close": [11, 12], "volume": [100, 200]}],
                            "adjclose": [{"adjclose": [8.5, 9.5]}],
                        },
                        "events": {
                            "dividends": {"one": {"date": epoch("2019-01-03"), "amount": 0.25}},
                            "splits": {"one": {"date": epoch("2019-01-02"), "numerator": 2, "denominator": 1}},
                        },
                    }
                ],
            }
        }
    )
    bars, actions, error = parse_yahoo_payload("BF.B", "BF-B", payload)
    assert error == ""
    assert len(bars) == 2
    assert bars[0].close == 11
    assert bars[0].adjusted_close == 8.5
    assert bars[0].split_factor == 2.0
    assert bars[1].dividend == 0.25
    assert {action.action_type for action in actions} == {"dividend", "split"}


def test_market_math_uses_63_returns_true_downside_deviation_and_date_alignment() -> None:
    log_returns = [0.03 if position % 3 == 0 else (-0.02 if position % 3 == 1 else 0.005) for position in range(63)]
    values = [100.0]
    for value in log_returns:
        values.append(values[-1] * math.exp(value))
    realized, downside = _annualized_volatilities(values, 63)
    assert realized == pytest.approx(statistics.stdev(log_returns) * math.sqrt(252.0))
    assert downside == pytest.approx(
        math.sqrt(statistics.fmean(min(value, 0.0) ** 2 for value in log_returns)) * math.sqrt(252.0)
    )
    assert downside != pytest.approx(statistics.stdev([min(value, 0.0) for value in log_returns]) * math.sqrt(252.0))

    ticker_rows = [("2024-01-02", 100.0), ("2024-01-03", 105.0), ("2024-01-04", 110.0)]
    benchmark = {"2024-01-02": 200.0, "2024-01-03": 202.0, "2024-01-04": 220.0}
    assert _aligned_residual_return(ticker_rows, benchmark, 2) == pytest.approx(0.0)
    benchmark_without_start = {key: value for key, value in benchmark.items() if key != "2024-01-02"}
    assert _aligned_residual_return(ticker_rows, benchmark_without_start, 2) is None


class FakeAdjustment:
    NONE = "none"
    TOTALRETURN = "totalreturn"


class FakeNorgatePrices:
    StockPriceAdjustmentType = FakeAdjustment

    def __init__(self, *, mismatch: bool = False) -> None:
        self.mismatch = mismatch

    def last_database_update_time(self, database: str) -> str:
        assert database in {"US Equities", "US Equities Delisted"}
        return f"stable-provider-snapshot:{database}"

    def price_timeseries(self, symbol: str, *, stock_price_adjustment_setting: str, **kwargs):
        pd = pytest.importorskip("pandas")
        dates = ["2019-01-02", "2019-01-03", "2019-01-04"]
        if self.mismatch and stock_price_adjustment_setting == FakeAdjustment.TOTALRETURN:
            dates = dates[:-1]
        multiplier = 0.8 if stock_price_adjustment_setting == FakeAdjustment.TOTALRETURN else 1.0
        return pd.DataFrame(
            {
                "Open": [10.0 * multiplier] * len(dates),
                "High": [12.0 * multiplier] * len(dates),
                "Low": [9.0 * multiplier] * len(dates),
                "Close": [11.0 * multiplier] * len(dates),
                "Volume": [100.0] * len(dates),
                "Dividend": [0.0, 0.25, 0.0][: len(dates)],
            },
            index=pd.to_datetime(dates),
        )


def test_norgate_loader_rejects_adjustment_date_mismatch() -> None:
    pytest.importorskip("pandas")
    good = fetch_norgate_prices(
        FakeNorgatePrices(),
        ticker="DEAD",
        symbol="DEAD-202001",
        listing_status="delisted",
        start="2019-01-02",
        end="2019-01-04",
    )
    assert good.error == ""
    assert len(good.bars) == 3
    assert good.bars[0].close == 11.0
    assert good.bars[0].adjusted_close == pytest.approx(8.8)
    assert len(good.actions) == 1

    mismatch = fetch_norgate_prices(
        FakeNorgatePrices(mismatch=True),
        ticker="DEAD",
        symbol="DEAD-202001",
        listing_status="delisted",
        start="2019-01-02",
        end="2019-01-04",
    )
    assert mismatch.bars == ()
    assert mismatch.error.startswith("norgate_raw_adjusted_date_mismatch")


def seed_security(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    active: bool,
    end: str | None,
) -> int:
    now = utc_now()
    company_id = conn.execute(
        """
        INSERT INTO dim_company(primary_ticker, company_name, universe_status, is_active,
            data_quality_status, first_seen_at, updated_at)
        VALUES (?, ?, ?, ?, 'reviewed', ?, ?)
        """,
        (ticker, ticker, "active" if active else "historical", int(active), now, now),
    ).lastrowid
    security_id = conn.execute(
        """
        INSERT INTO dim_security(company_id, ticker, provider_price_symbol, exchange,
            listing_country, security_type, listing_status, is_primary_listing,
            currency, listing_start_date, listing_end_date, created_at, updated_at)
        VALUES (?, ?, ?, 'NYSE', 'United States', 'Common Stock', ?, 1, 'USD', '2017-11-28', ?, ?, ?)
        """,
        (company_id, ticker, ticker if active else f"{ticker}-201904", "active" if active else "delisted", end, now, now),
    ).lastrowid
    conn.execute(
        """
        INSERT INTO dim_consumer_defensive_taxonomy(
            company_id, security_id, ticker, calibration_cohort_id, calibration_cohort,
            applicability_subtype, taxonomy_confidence, taxonomy_source,
            business_cohort_override_flag, analyst_reviewed, updated_at)
        VALUES (?, ?, ?, 'beverages', 'Beverages', 'non_alcohol', 1.0,
            'unit_test', 0, 1, ?)
        """,
        (company_id, security_id, ticker, now),
    )
    return int(security_id)


def seed_membership_interval(
    conn: sqlite3.Connection,
    *,
    security_id: int,
    ticker: str,
    start: str,
    end: str,
    calibration_eligible: bool = True,
) -> None:
    company_id = int(
        conn.execute(
            "SELECT company_id FROM dim_security WHERE security_id=?",
            (security_id,),
        ).fetchone()[0]
    )
    now = utc_now()
    conn.execute(
        """
        INSERT INTO dim_universe_membership(
            company_id, security_id, ticker, model_family, membership_source_id,
            membership_basis, recognized_vehicle, start_date, end_date,
            membership_status, is_current_member, point_in_time_flag,
            live_investable_flag, historical_calibration_eligible_flag,
            confidence, reason, created_at, updated_at
        ) VALUES (?, ?, ?, 'consumer_defensive',
            'norgate_us_equities_pit_membership', 'recognized_index_union',
            'unit_test', ?, ?, 'historical', 0, 1, 0, ?, 1.0,
            'unit_test', ?, ?)
        """,
        (
            company_id,
            security_id,
            ticker,
            start,
            end,
            int(calibration_eligible),
            now,
            now,
        ),
    )


@pytest.mark.parametrize(
    "drift_database",
    ["US Equities", "US Equities Delisted"],
)
def test_norgate_price_loader_records_failed_run_when_provider_changes(
    tmp_path: Path,
    drift_database: str,
) -> None:
    class DriftingNorgatePrices(FakeNorgatePrices):
        def __init__(self, component: str) -> None:
            super().__init__()
            self.component = component
            self.price_calls = 0
            self.drifted = False

        def last_database_update_time(self, database: str) -> str:
            stable = super().last_database_update_time(database)
            if self.drifted and database == self.component:
                return stable + ":changed"
            return stable

        def price_timeseries(self, *args, **kwargs):
            frame = super().price_timeseries(*args, **kwargs)
            self.price_calls += 1
            if self.price_calls >= 2:
                self.drifted = True
            return frame

    policy = load_market_policy(MARKET_POLICY_PATH)
    with connect(tmp_path / "norgate_drift.sqlite") as conn:
        initialize(conn)
        seed_security(conn, ticker="ACTIVE", active=True, end=None)
        with pytest.raises(RuntimeError, match="changed during price extraction"):
            load_norgate_prices(
                conn,
                policy,
                provider=DriftingNorgatePrices(drift_database),
                end="2019-01-04",
                tickers=["ACTIVE"],
            )
        run = conn.execute(
            "SELECT status,row_count,message FROM ingestion_runs WHERE source_id=? "
            "ORDER BY ingestion_run_id DESC LIMIT 1",
            (NORGATE_SOURCE_ID,),
        ).fetchone()
        assert run is not None
        assert (str(run["status"]), int(run["row_count"])) == ("failed", 0)
        message = json.loads(str(run["message"]))
        assert message["error"] == "norgate_provider_changed_midrun"
        assert message["changed_databases"] == [drift_database]
        assert conn.execute(
            "SELECT COUNT(*) FROM fact_price_ohlcv WHERE source_id=?",
            (NORGATE_SOURCE_ID,),
        ).fetchone()[0] == 0


def bars(ticker: str, source_id: str, dates: list[str], *, base: float) -> list[PriceBar]:
    result: list[PriceBar] = []
    for position, bar_date in enumerate(dates):
        close = base + position * 0.02
        result.append(
            PriceBar(
                ticker=ticker,
                bar_date=bar_date,
                source_id=source_id,
                open=close - 0.1,
                high=close + 0.2,
                low=close - 0.2,
                close=close,
                adjusted_close=close * (0.9 if source_id == YAHOO_SOURCE_ID else 0.8),
                volume=1_000_000,
                dividend=None,
                split_factor=None,
                total_return_basis="yahoo_adjusted_close" if source_id == YAHOO_SOURCE_ID else "norgate_total_return",
                source_timestamp=utc_now(),
            )
        )
    return result


def test_selection_feature_and_snapshot_validation_use_one_provider_per_ticker(tmp_path: Path) -> None:
    pd = pytest.importorskip("pandas")
    policy = load_market_policy(MARKET_POLICY_PATH)
    universe_policy = load_policy(UNIVERSE_POLICY_PATH)
    db_path = tmp_path / "stage3.sqlite"
    as_of = "2019-04-30"
    all_dates = [value.date().isoformat() for value in pd.bdate_range("2017-11-28", as_of)]
    dead_dates = [value for value in all_dates if value <= "2019-04-15"]
    with connect(db_path) as conn:
        initialize(conn)
        active_id = seed_security(conn, ticker="ACTIVE", active=True, end=None)
        dead_id = seed_security(conn, ticker="DEAD", active=False, end="2019-04-15")
        vehicle = universe_policy.payload["approved_membership_vehicles"][0]
        now = utc_now()
        conn.execute(
            """
            INSERT INTO dim_recognized_vehicle(vehicle_id, display_name, vehicle_type,
                provider_index_name, source_id, is_active, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'norgate_us_equities_pit_membership', 1, ?, ?)
            """,
            (vehicle["vehicle_id"], vehicle["display_name"], vehicle["vehicle_type"], vehicle["norgate_index_name"], now, now),
        )
        for security_id, ticker, interval_end, dates in (
            (active_id, "ACTIVE", as_of, ["2019-01-02", as_of]),
            (dead_id, "DEAD", "2019-04-15", ["2019-01-02", "2019-04-15"]),
        ):
            for membership_date in dates:
                conn.execute(
                    """
                    INSERT INTO fact_recognized_vehicle_membership_daily(
                        security_id, provider_asset_id, vehicle_id, membership_date,
                        member_flag, source_id, extracted_at)
                    VALUES (?, ?, ?, ?, 1, 'norgate_us_equities_pit_membership', ?)
                    """,
                    (security_id, f"asset-{security_id}", vehicle["vehicle_id"], membership_date, now),
                )
                conn.execute(
                    """
                    INSERT INTO fact_major_exchange_listing_daily(
                        security_id, provider_asset_id, listing_date,
                        major_exchange_listed_flag, source_id, extracted_at)
                    VALUES (?, ?, ?, 1, 'norgate_us_equities_pit_membership', ?)
                    """,
                    (security_id, f"asset-{security_id}", membership_date, now),
                )
            company_id = conn.execute("SELECT company_id FROM dim_security WHERE security_id=?", (security_id,)).fetchone()[0]
            conn.execute(
                """
                INSERT INTO dim_universe_membership(
                    company_id, security_id, ticker, model_family, membership_source_id,
                    membership_basis, recognized_vehicle, start_date, end_date,
                    membership_status, is_current_member, point_in_time_flag,
                    live_investable_flag, historical_calibration_eligible_flag,
                    confidence, reason, created_at, updated_at
                ) VALUES (?, ?, ?, 'consumer_defensive', 'norgate_us_equities_pit_membership',
                    'recognized_index_union', 'test', '2017-11-28', ?, 'historical',
                    ?, 1, ?, 1, 1.0, 'test', ?, ?)
                """,
                (company_id, security_id, ticker, interval_end, int(ticker == "ACTIVE"), int(ticker == "ACTIVE"), now, now),
            )
        with conn:
            upsert_price_bars(conn, bars("ACTIVE", YAHOO_SOURCE_ID, all_dates, base=20))
            upsert_price_bars(conn, bars("ACTIVE", NORGATE_SOURCE_ID, all_dates, base=200))
            upsert_price_bars(conn, bars("DEAD", NORGATE_SOURCE_ID, dead_dates, base=10))
            upsert_price_bars(conn, bars("XLP", YAHOO_SOURCE_ID, all_dates, base=40))
            upsert_price_bars(conn, bars("SPY", YAHOO_SOURCE_ID, all_dates, base=100))

        audit = select_price_sources(conn, policy, as_of=as_of)
        assert audit["status"] == "PASS", audit
        selected = dict(
            conn.execute(
                "SELECT ticker, selected_source_id FROM dim_price_series_selection WHERE purpose=?",
                (SELECTION_PURPOSE,),
            ).fetchall()
        )
        assert selected == {
            "ACTIVE": YAHOO_SOURCE_ID,
            "DEAD": NORGATE_SOURCE_ID,
            "SPY": YAHOO_SOURCE_ID,
            "XLP": YAHOO_SOURCE_ID,
        }
        feature_summary = build_market_features(conn, policy, as_of=as_of)
        assert feature_summary["features_written"] == 1
        historical_audit = select_price_sources(conn, policy, as_of="2019-01-02")
        assert historical_audit["status"] == "PASS", historical_audit
        historical_summary = build_market_features(conn, policy, as_of="2019-01-02")
        assert historical_summary["features_written"] == 2
        assert historical_summary["eligible_tickers"] == 2
        restored_audit = select_price_sources(conn, policy, as_of=as_of)
        assert restored_audit["status"] == "PASS", restored_audit
        feature_summary = build_market_features(conn, policy, as_of=as_of)
        feature = conn.execute(
            "SELECT source_id, quality_status FROM feature_market_technical WHERE ticker='ACTIVE'"
        ).fetchone()
        assert tuple(feature) == (YAHOO_SOURCE_ID, "full")

        validation = validate_stage3_market_data(conn, policy, as_of=as_of, expected_active=1)
        assert validation["status"] == "PASS", validation
        assert validation["counts"]["expected_pit_market_features"] == 1
        assert validation["counts"]["first_snapshot_pit_members"] == 2
        assert any("delisted_terminal_event_coverage" in warning for warning in validation["warnings"])


def test_delisted_yahoo_series_cannot_replace_mandatory_norgate(tmp_path: Path) -> None:
    pd = pytest.importorskip("pandas")
    policy = load_market_policy(MARKET_POLICY_PATH)
    dates = [value.date().isoformat() for value in pd.bdate_range("2017-11-28", "2019-04-15")]
    with connect(tmp_path / "no_fallback.sqlite") as conn:
        initialize(conn)
        dead_id = seed_security(conn, ticker="DEAD", active=False, end="2019-04-15")
        seed_membership_interval(
            conn,
            security_id=dead_id,
            ticker="DEAD",
            start="2017-11-28",
            end="2019-04-15",
        )
        with conn:
            upsert_price_bars(conn, bars("DEAD", YAHOO_SOURCE_ID, dates, base=10))
            upsert_price_bars(conn, bars("XLP", YAHOO_SOURCE_ID, dates, base=40))
            upsert_price_bars(conn, bars("SPY", YAHOO_SOURCE_ID, dates, base=100))
        audit = select_price_sources(conn, policy, as_of="2019-04-15")
        assert audit["status"] == "FAIL"
        assert "DEAD" not in {
            str(row[0])
            for row in conn.execute("SELECT ticker FROM dim_price_series_selection").fetchall()
        }
        assert any("mandatory Norgate" in error for error in audit["errors"])


def test_membership_aware_mama_window_passes_current_excludes_2019_and_rejects_real_gap(
    tmp_path: Path,
) -> None:
    pd = pytest.importorskip("pandas")
    policy = load_market_policy(MARKET_POLICY_PATH)
    as_of = "2026-08-10"
    all_dates = [
        value.date().isoformat()
        for value in pd.bdate_range("2017-11-28", as_of)
    ]
    required_dates = [value for value in all_dates if value >= "2020-06-10"]
    yahoo_dates = [value for value in all_dates if value >= "2021-07-19"]
    with connect(tmp_path / "mama_membership_window.sqlite") as conn:
        initialize(conn)
        mama_id = seed_security(conn, ticker="MAMA", active=True, end=None)
        seed_membership_interval(
            conn,
            security_id=mama_id,
            ticker="MAMA",
            start="2021-07-15",
            end=as_of,
        )
        with conn:
            upsert_price_bars(conn, bars("MAMA", NORGATE_SOURCE_ID, required_dates, base=20))
            upsert_price_bars(conn, bars("MAMA", YAHOO_SOURCE_ID, yahoo_dates, base=20))
            upsert_price_bars(conn, bars("XLP", YAHOO_SOURCE_ID, all_dates, base=40))
            upsert_price_bars(conn, bars("SPY", YAHOO_SOURCE_ID, all_dates, base=100))

        historical = select_price_sources(conn, policy, as_of="2019-01-02")
        assert historical["status"] == "PASS", historical
        assert "MAMA" not in {row["ticker"] for row in historical["rows"]}

        current = select_price_sources(conn, policy, as_of=as_of)
        assert current["status"] == "PASS", current
        mama = next(row for row in current["rows"] if row["ticker"] == "MAMA")
        assert mama["expected_start_date"] == "2020-06-10"
        assert mama["selected_source_id"] == NORGATE_SOURCE_ID
        assert mama["norgate_missing_trading_days"] == 0
        assert mama["norgate_longest_consecutive_missing_trading_days"] == 0

        sparse_dates = required_dates[20:-20:80]
        conn.executemany(
            "DELETE FROM fact_price_ohlcv WHERE ticker='MAMA' AND source_id=? AND bar_date=?",
            [(NORGATE_SOURCE_ID, value) for value in sparse_dates],
        )
        sparse = select_price_sources(conn, policy, as_of=as_of)
        assert sparse["status"] == "PASS", sparse
        mama_sparse = next(row for row in sparse["rows"] if row["ticker"] == "MAMA")
        assert 0.01 < mama_sparse["norgate_missing_trading_day_ratio"] < 0.02
        assert mama_sparse["norgate_longest_consecutive_missing_trading_days"] == 1
        assert mama_sparse["issue_detail"].startswith(
            "sparse_trading_calendar_coverage:"
        )
        assert any(
            warning.startswith("MAMA: sparse_trading_calendar_coverage:")
            for warning in sparse["warnings"]
        )
        upsert_price_bars(
            conn,
            bars("MAMA", NORGATE_SOURCE_ID, sparse_dates, base=20),
        )

        post_eligibility_gap = required_dates[500:506]
        assert len(post_eligibility_gap) == 6
        conn.executemany(
            "DELETE FROM fact_price_ohlcv WHERE ticker='MAMA' AND source_id=? AND bar_date=?",
            [(NORGATE_SOURCE_ID, value) for value in post_eligibility_gap],
        )
        gappy = select_price_sources(conn, policy, as_of=as_of)
        assert gappy["status"] == "FAIL"
        assert any(
            error == "MAMA: neither Yahoo nor Norgate has qualifying active coverage."
            for error in gappy["errors"]
        )
        mama_gappy = next(row for row in gappy["rows"] if row["ticker"] == "MAMA")
        assert mama_gappy["norgate_missing_trading_day_ratio"] < 0.02
        assert mama_gappy["norgate_longest_consecutive_missing_trading_days"] == 6


def test_sparse_calendar_ratio_is_configurable_but_never_waives_long_gap() -> None:
    pd = pytest.importorskip("pandas")
    expected = tuple(
        value.date().isoformat()
        for value in pd.bdate_range("2019-01-02", periods=1_000)
    )
    missing = set(expected[30::60])
    observed = tuple(value for value in expected if value not in missing)
    coverage = {
        "first": observed[0],
        "last": observed[-1],
        "rows": len(observed),
        "invalid_adjusted": 0,
        "observed_dates": observed,
    }
    diagnostics = trading_calendar_coverage(
        coverage,
        expected_start=expected[0],
        expected_end=expected[-1],
        expected_dates=expected,
    )
    assert 0.01 < diagnostics["missing_trading_day_ratio"] < 0.02
    assert diagnostics["longest_consecutive_missing_trading_days"] == 1
    assert coverage_qualifies(
        coverage,
        expected_start=expected[0],
        expected_end=expected[-1],
        start_tolerance_days=3,
        end_tolerance_days=3,
        minimum_rows=63,
        expected_dates=expected,
        maximum_missing_trading_day_ratio=0.02,
        maximum_consecutive_missing_trading_days=5,
    )
    assert not coverage_qualifies(
        coverage,
        expected_start=expected[0],
        expected_end=expected[-1],
        start_tolerance_days=3,
        end_tolerance_days=3,
        minimum_rows=63,
        expected_dates=expected,
        maximum_missing_trading_day_ratio=0.01,
        maximum_consecutive_missing_trading_days=5,
    )
