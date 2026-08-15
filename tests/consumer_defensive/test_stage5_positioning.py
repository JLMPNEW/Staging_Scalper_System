from __future__ import annotations

import copy
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from consumer_defensive.core.config import ConfigBundle, load_config
from consumer_defensive.core.db import connect, init_db, utc_now
from consumer_defensive.core.source_registry import load_source_registry, upsert_source_registry
from consumer_defensive.core.stage5 import (
    bootstrap_stage5,
    build_positioning_universe_rows,
    validate_stage5,
)
from consumer_defensive.core.stage5_import import (
    build_positioning_features,
    import_market_positioning,
    import_sec_insider_transactions,
)
from consumer_defensive.core.stage5_schema import (
    STAGE5_MIGRATION_SHA256,
    ensure_stage5_schema,
)
from market_positioning import api_collectors
from market_positioning.core import connect as connect_positioning
from market_positioning.core import init_db as init_positioning_db


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "consumer_defensive" / "config.yaml"
SOURCES = ROOT / "consumer_defensive" / "data" / "free_source_registry.yaml"


def _bundle(tmp_path: Path) -> ConfigBundle:
    source = load_config(CONFIG)
    payload = copy.deepcopy(source.payload)
    payload["positioning"]["form4_upstream_db"] = str(tmp_path / "form4.sqlite")
    payload["positioning"]["market_positioning_upstream_db"] = str(tmp_path / "positioning.sqlite")
    identifier_map = tmp_path / "positioning_identifiers.csv"
    identifier_map.write_text(
        "ticker,cusip,finra_symbol,review_status,review_reason\n"
        "KO,191216100,KO,reviewed,test fixture\n",
        encoding="utf-8",
    )
    payload["positioning"]["source_identifier_map"] = str(identifier_map)
    payload["positioning"]["minimum_current_coverage"] = {
        "institutional_13f": 1.0,
        "short_interest": 1.0,
        "borrow": 1.0,
    }
    payload["positioning"]["maximum_age_days"] = {
        "sec_form4": None,
        "institutional_13f": 500,
        "short_interest": 500,
        "borrow": 500,
    }
    payload['positioning']['lookback_days']['insider'] = 400
    return ConfigBundle(source.path, source.base_dir, payload)


def _prepared(tmp_path: Path) -> tuple[ConfigBundle, sqlite3.Connection]:
    bundle = _bundle(tmp_path)
    conn = connect(tmp_path / "consumer.sqlite")
    init_db(conn)
    upsert_source_registry(conn, load_source_registry(SOURCES))
    now = utc_now()
    with conn:
        company_id = conn.execute(
            """INSERT INTO dim_company(
                   primary_ticker,cik,company_name,universe_status,is_active,
                   first_seen_at,updated_at
               ) VALUES ('KO','21344','Coca-Cola','current',1,?,?)""",
            (now, now),
        ).lastrowid
        security_id = conn.execute(
            """INSERT INTO dim_security(
                   company_id,ticker,provider_price_symbol,exchange,listing_status,
                   is_primary_listing,currency,listing_start_date,created_at,updated_at
               ) VALUES (?,'KO','KO','NYSE','active',1,'USD','1919-09-05',?,?)""",
            (company_id, now, now),
        ).lastrowid
        conn.execute(
            """INSERT INTO dim_consumer_defensive_taxonomy(
                   company_id,security_id,ticker,calibration_cohort_id,
                   calibration_cohort,applicability_subtype,taxonomy_confidence,
                   analyst_reviewed,updated_at
               ) VALUES (?,?,'KO','beverages','Beverages','non_alcohol',1,1,?)""",
            (company_id, security_id, now),
        )
        conn.execute(
            """INSERT INTO dim_universe_membership(
                   company_id,security_id,ticker,membership_basis,start_date,
                   membership_status,is_current_member,point_in_time_flag,
                   live_investable_flag,historical_calibration_eligible_flag,
                   created_at,updated_at
               ) VALUES (?,?,'KO','fixture','2019-01-02','active',1,1,1,1,?,?)""",
            (company_id, security_id, now, now),
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS dim_issuer_reporting_profile(
                   ticker TEXT PRIMARY KEY,
                   foreign_issuer_flag INTEGER NOT NULL
               )"""
        )
        conn.execute(
            "INSERT INTO dim_issuer_reporting_profile VALUES ('KO',0)"
        )
    return bundle, conn


def _form4_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE form4_events_tier1(
               event_key TEXT,is_current_truth INTEGER,accession_number TEXT,
               document_type TEXT,filing_date TEXT,filing_date_sort TEXT,
               accepted_ts_utc TEXT,issuer_cik TEXT,rptowner_cik TEXT,
               rptowner_name TEXT,rptowner_relationship TEXT,rptowner_title TEXT,
               security_title TEXT,trans_date TEXT,trans_code TEXT,
               trans_shares REAL,trans_price_per_share REAL,
               trans_acquired_disp_cd TEXT
           )"""
    )
    conn.executemany(
        "INSERT INTO form4_events_tier1 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("past", 1, "0001-24-000001", "4", "2024-01-10", "2024-01-10", "2024-01-10T16:00:00Z", "21344", "1", "Owner", "Officer", "CEO", "Common Stock", "2024-01-09", "P", 10.0, 2.0, "A"),
            ("future", 1, "0001-25-000001", "4", "2025-01-10", "2025-01-10", "2025-01-10T16:00:00Z", "21344", "1", "Owner", "Officer", "CEO", "Common Stock", "2025-01-09", "S", 5.0, 3.0, "D"),
        ],
    )
    conn.commit()
    conn.close()


def _positioning_db(path: Path, *, malformed_short: bool = False) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE institutional_13f_ownership_snapshots(
               ticker TEXT,asof_date TEXT,period_of_report TEXT,
               institutional_shares REAL,institutional_value REAL,manager_count INTEGER,
               new_buyer_count INTEGER,exiting_holder_count INTEGER,net_buyer_count INTEGER,
               institutional_ownership_delta_pct REAL,source TEXT
           )"""
    )
    conn.execute(
        "INSERT INTO institutional_13f_ownership_snapshots VALUES ('KO','2024-02-14','2023-12-31',100,1000,5,2,1,1,0.1,'sec_13f_data_sets')"
    )
    conn.execute(
        "INSERT INTO institutional_13f_ownership_snapshots VALUES ('KO','2024-02-14','2023-12-31',999,999,99,9,9,0,9.9,'unapproved_13f')"
    )
    if malformed_short:
        conn.execute("CREATE TABLE short_interest_snapshots(ticker TEXT)")
    else:
        conn.execute(
            """CREATE TABLE short_interest_snapshots(
                   ticker TEXT,asof_date TEXT,settlement_date TEXT,publication_date TEXT,
                   short_interest_shares REAL,short_interest_pct_float REAL,
                   days_to_cover REAL,source TEXT
               )"""
        )
        conn.execute(
            "INSERT INTO short_interest_snapshots VALUES ('KO','2024-02-12','2024-01-31','2024-02-12',50,0.05,2.5,'finra_equity_short_interest_files')"
        )
        conn.execute(
            "INSERT INTO short_interest_snapshots VALUES ('KO','2024-02-12','2024-01-31','2024-02-12',999,0.99,99,'finra_equity_short_interest')"
        )
    conn.execute(
        "CREATE TABLE ibkr_borrow_fee_rate_daily(ticker TEXT,asof_date TEXT,borrow_fee_rate REAL,source TEXT)"
    )
    conn.execute("INSERT INTO ibkr_borrow_fee_rate_daily VALUES ('KO','2024-02-15',0.02,'interactive_brokers')")
    conn.execute("INSERT INTO ibkr_borrow_fee_rate_daily VALUES ('KO','2024-02-15',0.99,'unapproved_borrow')")
    conn.execute(
        "CREATE TABLE ibkr_shortable_shares_snapshots(ticker TEXT,asof_date TEXT,shortable_shares REAL,source TEXT)"
    )
    conn.execute("INSERT INTO ibkr_shortable_shares_snapshots VALUES ('KO','2024-02-15',5000,'interactive_brokers')")
    conn.execute("INSERT INTO ibkr_shortable_shares_snapshots VALUES ('KO','2024-02-15',999,'unapproved_borrow')")
    conn.commit()
    conn.close()


def test_stage5_schema_is_checksummed_and_idempotent(tmp_path: Path) -> None:
    _, conn = _prepared(tmp_path)
    try:
        ensure_stage5_schema(conn)
        ensure_stage5_schema(conn)
        row = conn.execute(
            "SELECT migration_version,migration_sha256 FROM stage5_schema_migrations"
        ).fetchone()
        assert tuple(row) == (1, STAGE5_MIGRATION_SHA256)
        assert "source_observation_id" in {
            str(item[1]) for item in conn.execute("PRAGMA table_info(fact_short_interest)")
        }
        assert "short_days_to_cover" in {
            str(item[1]) for item in conn.execute("PRAGMA table_info(feature_positioning)")
        }
    finally:
        conn.close()


def test_stage5_imports_are_pit_read_only_and_build_complete_features(tmp_path: Path) -> None:
    bundle, conn = _prepared(tmp_path)
    _form4_db(tmp_path / "form4.sqlite")
    _positioning_db(tmp_path / "positioning.sqlite")
    try:
        bootstrap_stage5(conn, bundle)
        before_form4 = (tmp_path / "form4.sqlite").stat().st_size
        before_positioning = (tmp_path / "positioning.sqlite").stat().st_size
        ownership = import_sec_insider_transactions(conn, bundle, as_of="2024-12-31")
        imported = import_market_positioning(conn, bundle, as_of="2024-12-31")
        features = build_positioning_features(conn, bundle, as_of="2024-12-31")

        assert ownership["rows"] == 1
        assert imported["institutional_rows"] == 1
        assert imported["short_interest_rows"] == 1
        assert imported["borrow_rows"] == 1
        assert features["quality_status_counts"] == {"complete": 1}
        row = conn.execute("SELECT * FROM feature_positioning WHERE ticker='KO'").fetchone()
        assert row["insider_net_buying"] == pytest.approx(20.0)
        assert row["institutional_flow"] == pytest.approx(0.1)
        assert row["short_float_pct"] == pytest.approx(0.05)
        assert row["short_days_to_cover"] == pytest.approx(2.5)
        assert row["borrow_fee"] == pytest.approx(0.02)
        assert (tmp_path / "form4.sqlite").stat().st_size == before_form4
        assert (tmp_path / "positioning.sqlite").stat().st_size == before_positioning
        assert validate_stage5(conn, bundle, as_of="2024-12-31")["status"] == "PASS"
    finally:
        conn.close()


def test_malformed_upstream_fails_before_existing_slice_is_replaced(tmp_path: Path) -> None:
    bundle, conn = _prepared(tmp_path)
    _positioning_db(tmp_path / "positioning.sqlite", malformed_short=True)
    bootstrap_stage5(conn, bundle)
    now = utc_now()
    with conn:
        conn.execute(
            """INSERT INTO fact_13f_positioning(
                   ticker,asof_date,publication_date,source_id,created_at,
                   source_birthdate,source_observation_id
               ) VALUES ('KO','2023-01-01','2023-01-01','market_positioning_upstream',?,
                         '2019-01-02',?)""",
            (now, "a" * 64),
        )
    try:
        with pytest.raises(RuntimeError, match="missing columns"):
            import_market_positioning(conn, bundle, as_of="2024-12-31")
        assert conn.execute("SELECT COUNT(*) FROM fact_13f_positioning").fetchone()[0] == 1
    finally:
        conn.close()


def test_before_birthdate_missing_sources_remain_null_not_zero(tmp_path: Path) -> None:
    bundle, conn = _prepared(tmp_path)
    try:
        bootstrap_stage5(conn, bundle)
        result = build_positioning_features(conn, bundle, as_of="2019-03-01")
        assert result["quality_status_counts"] == {"missing": 1}
        row = conn.execute("SELECT * FROM feature_positioning WHERE ticker='KO'").fetchone()
        assert row["institutional_flow"] is None
        assert row["short_float_pct"] is None
        assert row["borrow_fee"] is None
        assert row["quality_status"] == "missing"
    finally:
        conn.close()


def test_short_float_uses_pit_sec_share_proxy_with_lineage(tmp_path: Path) -> None:
    bundle, conn = _prepared(tmp_path)
    _positioning_db(tmp_path / "positioning.sqlite")
    upstream = sqlite3.connect(tmp_path / "positioning.sqlite")
    upstream.execute(
        "UPDATE short_interest_snapshots SET short_interest_pct_float=NULL"
    )
    upstream.commit()
    upstream.close()
    now = utc_now()
    with conn:
        conn.execute(
            """INSERT INTO fact_sec_xbrl_fact_raw(
                   ticker,taxonomy,concept,numeric_value,unit,period_end,
                   accepted_at,dimensions_json,source_id,created_at
               ) VALUES (
                   'KO','dei','EntityCommonStockSharesOutstanding',1000,'shares',
                   '2024-01-31','2024-02-01T12:00:00Z','{}',
                   'sec_companyfacts',?
               )""",
            (now,),
        )
    try:
        imported = import_market_positioning(conn, bundle, as_of="2024-12-31")
        assert imported["short_interest_rows"] == 1
        row = conn.execute(
            "SELECT * FROM fact_short_interest WHERE ticker='KO'"
        ).fetchone()
        assert row["short_float_pct"] == pytest.approx(0.05)
        assert row["float_shares_proxy"] == pytest.approx(1000.0)
        assert row["float_proxy_concept"] == "EntityCommonStockSharesOutstanding"
        assert row["float_proxy_accepted_at"] == "2024-02-01T12:00:00Z"
        assert row["float_proxy_method"] == "sec_xbrl_pit_share_proxy_v1"
    finally:
        conn.close()


def test_days_to_cover_keeps_short_signal_complete_without_unsafe_float_proxy(
    tmp_path: Path,
) -> None:
    bundle, conn = _prepared(tmp_path)
    _positioning_db(tmp_path / "positioning.sqlite")
    upstream = sqlite3.connect(tmp_path / "positioning.sqlite")
    upstream.execute(
        "UPDATE short_interest_snapshots SET short_interest_pct_float=NULL"
    )
    upstream.commit()
    upstream.close()
    try:
        bootstrap_stage5(conn, bundle)
        import_market_positioning(conn, bundle, as_of="2024-12-31")
        result = build_positioning_features(conn, bundle, as_of="2024-12-31")
        row = conn.execute(
            "SELECT * FROM feature_positioning WHERE ticker='KO'"
        ).fetchone()
        assert result["quality_status_counts"] == {"complete": 1}
        assert row["short_float_pct"] is None
        assert row["short_days_to_cover"] == pytest.approx(2.5)
    finally:
        conn.close()


def test_foreign_private_issuer_is_explicitly_not_applicable_to_form4(
    tmp_path: Path,
) -> None:
    bundle, conn = _prepared(tmp_path)
    _form4_db(tmp_path / "form4.sqlite")
    _positioning_db(tmp_path / "positioning.sqlite")
    now = utc_now()
    with conn:
        company_id = conn.execute(
            """INSERT INTO dim_company(
                   primary_ticker,cik,company_name,universe_status,is_active,
                   first_seen_at,updated_at
               ) VALUES ('ABEV','1565025','Ambev','current',1,?,?)""",
            (now, now),
        ).lastrowid
        security_id = conn.execute(
            """INSERT INTO dim_security(
                   company_id,ticker,provider_price_symbol,exchange,listing_status,
                   is_primary_listing,currency,listing_start_date,created_at,updated_at
               ) VALUES (?,'ABEV','ABEV','NYSE','active',1,'USD','2013-11-11',?,?)""",
            (company_id, now, now),
        ).lastrowid
        conn.execute(
            """INSERT INTO dim_consumer_defensive_taxonomy(
                   company_id,security_id,ticker,calibration_cohort_id,
                   calibration_cohort,applicability_subtype,taxonomy_confidence,
                   analyst_reviewed,updated_at
               ) VALUES (?,?,'ABEV','beverages','Beverages','alcohol',1,1,?)""",
            (company_id, security_id, now),
        )
        conn.execute(
            """INSERT INTO dim_universe_membership(
                   company_id,security_id,ticker,membership_basis,start_date,
                   membership_status,is_current_member,point_in_time_flag,
                   live_investable_flag,historical_calibration_eligible_flag,
                   created_at,updated_at
               ) VALUES (?,?,'ABEV','fixture','2019-01-02','active',1,1,1,1,?,?)""",
            (company_id, security_id, now, now),
        )
        conn.execute(
            "INSERT INTO dim_issuer_reporting_profile VALUES ('ABEV',1)"
        )
    upstream = sqlite3.connect(tmp_path / "positioning.sqlite")
    upstream.execute(
        "INSERT INTO institutional_13f_ownership_snapshots VALUES "
        "('ABEV','2024-02-14','2023-12-31',100,1000,5,2,1,1,0.1,'sec_13f_data_sets')"
    )
    upstream.execute(
        "INSERT INTO short_interest_snapshots VALUES "
        "('ABEV','2024-02-12','2024-01-31','2024-02-12',50,0.03,2.0,"
        "'finra_equity_short_interest_files')"
    )
    upstream.execute(
        "INSERT INTO ibkr_borrow_fee_rate_daily VALUES "
        "('ABEV','2024-02-15',0.02,'interactive_brokers')"
    )
    upstream.execute(
        "INSERT INTO ibkr_shortable_shares_snapshots VALUES "
        "('ABEV','2024-02-15',5000,'interactive_brokers')"
    )
    upstream.commit()
    upstream.close()
    try:
        bootstrap_stage5(conn, bundle)
        import_sec_insider_transactions(conn, bundle, as_of="2024-12-31")
        import_market_positioning(conn, bundle, as_of="2024-12-31")
        build_positioning_features(conn, bundle, as_of="2024-12-31")
        result = validate_stage5(conn, bundle, as_of="2024-12-31")
        form4 = next(
            row
            for row in result["checks"]
            if row["check"] == "sec_form4_applicable_current_coverage"
        )
        assert result["status"] == "PASS"
        assert form4["covered"] == 1
        assert form4["eligible"] == 1
        assert form4["foreign_private_issuer_not_applicable"] == ["ABEV"]
    finally:
        conn.close()


def test_positioning_handoff_uses_only_reviewed_source_identifiers(
    tmp_path: Path,
) -> None:
    bundle, conn = _prepared(tmp_path)
    try:
        row = build_positioning_universe_rows(conn, bundle)[0]
        assert row["ticker"] == "KO"
        assert row["cusip"] == "191216100"
        assert row["finra_symbol"] == "KO"
    finally:
        conn.close()


def test_neutral_finra_cache_only_never_calls_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        api_collectors,
        "http_request",
        lambda *args, **kwargs: pytest.fail("network called in cache-only mode"),
    )
    url = "https://example.test/finra/FNSQ20260810.txt"
    assert api_collectors.download_finra_equity_short_interest_file(
        url=url,
        cache_dir=tmp_path,
        user_agent="fixture",
        timeout_sec=1.0,
        cache_only=True,
    ) is None
    corrupt = tmp_path / "FNSQ20260810.txt"
    corrupt.write_text("not-a-delimited-feed", encoding="utf-8")
    with pytest.raises(RuntimeError, match="failed integrity"):
        api_collectors.download_finra_equity_short_interest_file(
            url=url,
            cache_dir=tmp_path,
            user_agent="fixture",
            timeout_sec=1.0,
            cache_only=True,
        )
    assert corrupt.read_text(encoding="utf-8") == "not-a-delimited-feed"


def test_neutral_13f_cache_only_requires_local_archives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    universe = tmp_path / "universe.csv"
    universe.write_text(
        "ticker,company_name,cusip\nKO,Coca Cola,191216100\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        api_collectors,
        "discover_sec_13f_archives",
        lambda **kwargs: pytest.fail("archive discovery called in cache-only mode"),
    )
    conn = connect_positioning(tmp_path / "positioning.sqlite")
    try:
        init_positioning_db(conn)
        with pytest.raises(RuntimeError, match="No cached SEC 13F archives"):
            api_collectors.sync_sec_13f_data_sets(
                conn,
                tickers_csv=universe,
                cusip_ticker_map_csv=universe,
                history_start_date=date(2019, 1, 2),
                end_date=date(2026, 8, 10),
                cache_dir=tmp_path / "empty-cache",
                cache_only=True,
            )
    finally:
        conn.close()
