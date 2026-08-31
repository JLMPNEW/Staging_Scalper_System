from __future__ import annotations

import sqlite3

from industrials.core.source_coverage import audit_industrials_source_coverage


ASOF = "2026-08-13"


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE dim_company (
            company_id INTEGER PRIMARY KEY,
            ticker TEXT,
            is_active INTEGER
        );
        CREATE TABLE dim_industrials_taxonomy (
            company_id INTEGER,
            model_family TEXT
        );
        CREATE TABLE dim_universe_membership (
            company_id INTEGER,
            ticker TEXT,
            model_family TEXT,
            membership_basis TEXT,
            is_current_member INTEGER
        );
        CREATE TABLE fact_price_ohlcv (ticker TEXT, bar_date TEXT);
        CREATE TABLE fact_market_snapshot (ticker TEXT, asof_date TEXT);
        CREATE TABLE feature_market_technical (ticker TEXT, asof_date TEXT);
        CREATE TABLE feature_financial_statement (ticker TEXT, asof_date TEXT);
        CREATE TABLE feature_positioning (ticker TEXT, asof_date TEXT);
        CREATE TABLE fact_fx_rate (currency_pair TEXT, rate_date TEXT);
        """
    )
    conn.executemany(
        "INSERT INTO dim_company VALUES (?,?,1)",
        [(1, "AAA"), (2, "BBB"), (3, "STALE")],
    )
    conn.executemany(
        "INSERT INTO dim_industrials_taxonomy VALUES (?,?)",
        [(1, "machinery"), (2, "machinery"), (3, "machinery")],
    )
    conn.executemany(
        "INSERT INTO dim_universe_membership VALUES (?,?,'machinery','current_source_of_truth',1)",
        [(1, "AAA"), (2, "BBB")],
    )
    for table, date_column in (
        ("fact_price_ohlcv", "bar_date"),
        ("fact_market_snapshot", "asof_date"),
        ("feature_market_technical", "asof_date"),
        ("feature_financial_statement", "asof_date"),
        ("feature_positioning", "asof_date"),
    ):
        conn.executemany(
            f"INSERT INTO {table}(ticker,{date_column}) VALUES (?,?)",
            [("AAA", ASOF), ("BBB", ASOF)],
        )
    conn.execute("INSERT INTO fact_fx_rate VALUES ('USD/EUR', ?)", (ASOF,))
    return conn


def test_source_coverage_passes_only_with_every_active_ticker() -> None:
    result = audit_industrials_source_coverage(
        _connection(),
        model_family="machinery",
        asof=ASOF,
    )

    assert result.acceptance == "PASS"
    assert result.active_ticker_count == 2
    assert result.errors == ()


def test_raw_market_sources_allow_a_prior_print_when_exact_features_exist() -> None:
    conn = _connection()
    for table, date_column in (
        ("fact_price_ohlcv", "bar_date"),
        ("fact_market_snapshot", "asof_date"),
    ):
        conn.execute(
            f"DELETE FROM {table} WHERE ticker='BBB' AND {date_column}=?",
            (ASOF,),
        )
        conn.execute(
            f"INSERT INTO {table}(ticker,{date_column}) VALUES ('BBB','2026-08-12')"
        )

    result = audit_industrials_source_coverage(
        conn,
        model_family="machinery",
        asof=ASOF,
    )

    assert result.acceptance == "PASS"
    raw = [
        observation
        for observation in result.observations
        if observation.table in {"fact_price_ohlcv", "fact_market_snapshot"}
    ]
    assert {observation.coverage_mode for observation in raw} == {"point_in_time"}
    assert all(observation.active_tickers_on_asof == 2 for observation in raw)


def test_source_coverage_fails_when_one_ticker_is_missing() -> None:
    conn = _connection()
    conn.execute(
        "DELETE FROM feature_financial_statement WHERE ticker='BBB' AND asof_date=?",
        (ASOF,),
    )

    result = audit_industrials_source_coverage(
        conn,
        model_family="machinery",
        asof=ASOF,
    )

    assert result.acceptance == "FAIL"
    assert result.errors == (
        "feature_financial_statement.asof_date exact active coverage=1/2;missing=BBB",
    )
    financial = next(
        observation
        for observation in result.observations
        if observation.table == "feature_financial_statement"
    )
    assert financial.missing_active_tickers == ("BBB",)
