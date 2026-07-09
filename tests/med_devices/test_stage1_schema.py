from __future__ import annotations

import sqlite3
import importlib.util
import sys
from datetime import date
from pathlib import Path
from types import ModuleType

import pytest

from med_devices.core.db import connect, init_db
from med_devices.core.market_policy import is_adjusted_price_row
from med_devices.core.source_registry import load_source_registry, upsert_source_registry


REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_REGISTRY = REPO_ROOT / "med_devices" / "data" / "free_source_registry.yaml"
SCRIPT_DIR = REPO_ROOT / "med_devices" / "scripts"


def table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {str(row["name"]) for row in rows}


def load_script_module(filename: str, module_name: str) -> ModuleType:
    path = SCRIPT_DIR / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load module spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_finra_short_interest_parse_asof_rejects_malformed_dates() -> None:
    module = load_script_module("65_update_med_device_finra_short_interest.py", "med_device_finra_parse_asof_test")

    assert module.parse_asof("2026-06-30", field_name="history") == date(2026, 6, 30)
    with pytest.raises(ValueError, match="Invalid history"):
        module.parse_asof("not-a-date", field_name="history")


def test_stage1_schema_creates_independent_med_device_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "med_devices.sqlite"

    with connect(db_path) as conn:
        init_db(conn)
        names = table_names(conn)
        price_columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(fact_price_ohlcv)").fetchall()
        }

    assert "source_registry" in names
    assert "raw_api_responses" in names
    assert "dim_company" in names
    assert "fact_fda_approval" in names
    assert "fact_reimbursement_policy" in names
    assert "med_device_daily_scores" in names
    assert "daily_scores" not in names
    assert "trials" not in names
    assert "price_adjustment" in price_columns


def test_free_source_registry_loads_core_free_sources(tmp_path: Path) -> None:
    sources = load_source_registry(SOURCE_REGISTRY)
    source_ids = {str(row["source_id"]) for row in sources}

    assert {
        "sec_company_tickers",
        "sec_companyfacts",
        "ib_market_data",
        "yahoo_finance_backup",
        "openfda_device",
        "accessgudid",
        "cms_coverage_api",
        "clinicaltrials_v2",
        "fred",
    }.issubset(source_ids)
    assert "stooq_daily_prices" not in source_ids
    assert "alpha_vantage_daily_prices" not in source_ids

    db_path = tmp_path / "med_devices.sqlite"
    with connect(db_path) as conn:
        init_db(conn)
        count = upsert_source_registry(conn, sources)
        row = conn.execute("SELECT COUNT(*) AS source_count FROM source_registry").fetchone()

    assert count == len(sources)
    assert row is not None
    assert int(row["source_count"]) == len(sources)


def test_source_registry_preserves_zero_priority(tmp_path: Path) -> None:
    registry = tmp_path / "sources.yaml"
    registry.write_text(
        "\n".join(
            [
                "sources:",
                "  - source_id: first_source",
                "    stage: stage_1",
                "    source_name: First",
                "    source_type: api",
                "    base_url: https://example.com/first",
                "    priority: 0",
                "  - source_id: second_source",
                "    stage: stage_1",
                "    source_name: Second",
                "    source_type: api",
                "    base_url: https://example.com/second",
                "    priority: 10",
            ]
        ),
        encoding="utf-8",
    )

    sources = load_source_registry(registry)

    assert [source["source_id"] for source in sources] == ["first_source", "second_source"]
    assert sources[0]["priority"] == 0


def test_market_policy_treats_zero_adjusted_close_as_present() -> None:
    assert is_adjusted_price_row({"is_adjusted": 1, "adj_close": 0.0})
    assert not is_adjusted_price_row({"is_adjusted": 0, "adj_close": 0.0})
    assert not is_adjusted_price_row({"is_adjusted": 0, "adj_close": "nan"})
    assert not is_adjusted_price_row({"is_adjusted": 0, "adj_close": None})


def test_med_device_universe_loader_accepts_clean_keep_shape(tmp_path: Path) -> None:
    module = load_script_module("01_load_med_device_universe.py", "med_device_universe_loader_test")
    universe_csv = tmp_path / "med_dev_tickers_clean_keep.csv"
    universe_csv.write_text(
        "\n".join(
            [
                "Name,Company_Name,Industry,Index,CIK,Exchange,SecurityType,ListingStatus,IsPrimaryListing,Country,Currency,CompanyName,MatchedTicker,MatchType,Source,IdentityDataSources,MissingIdentityFields,ManualInclude,ManualExclude,ManualReview,Notes",
                "ISRG,\"Intuitive Surgical, Inc.\",Healthcare,Medical Instruments & Supplies,0001035267,Nasdaq,Common Stock,active,TRUE,United States,USD,INTUITIVE SURGICAL INC,ISRG,exact,sec,nasdaqtrader,,false,false,false,",
                "MDT,Medtronic plc,Healthcare,Medical Devices,0001613103,NYSE,Ordinary Shares,active,TRUE,United States,USD,Medtronic plc,MDT,exact,sec,nasdaqtrader,,,,,",
            ]
        ),
        encoding="utf-8",
    )
    companies = module.parse_universe_rows(universe_csv)
    assert [company.ticker for company in companies] == ["ISRG", "MDT"]
    assert companies[0].cik == "0001035267"
    assert companies[0].subsector == "medical_instruments_and_supplies"
    assert companies[0].medtech_pure_play_flag == 1

    db_path = tmp_path / "med_devices.sqlite"
    with connect(db_path) as conn:
        init_db(conn)
        module.upsert_universe(conn, companies)
        row = conn.execute("SELECT COUNT(*) AS company_count FROM dim_company").fetchone()
        security_row = conn.execute(
            "SELECT security_type FROM dim_security WHERE ticker = ?",
            ("MDT",),
        ).fetchone()
        company_row = conn.execute("SELECT company_id FROM dim_company WHERE ticker = ?", ("ISRG",)).fetchone()

    assert row is not None
    assert int(row["company_count"]) == 2
    assert security_row is not None
    assert security_row["security_type"] == "Ordinary Shares"
    assert company_row is not None


def test_yahoo_adjusted_parser_builds_adjusted_price_rows() -> None:
    module = load_script_module("04_sync_med_device_yahoo_adjusted_prices.py", "med_device_yahoo_sync_test")
    payload = {
        "chart": {
            "result": [
                {
                    "timestamp": [1704067200, 1704153600],
                    "indicators": {
                        "quote": [
                            {
                                "open": [10.0, 20.0],
                                "high": [11.0, 22.0],
                                "low": [9.0, 18.0],
                                "close": [10.0, 20.0],
                                "volume": [1000, 2000],
                            }
                        ],
                        "adjclose": [{"adjclose": [5.0, 20.0]}],
                    },
                    "events": {
                        "dividends": {"1704153600": {"amount": 0.12}},
                        "splits": {"1704067200": {"numerator": 2.0, "denominator": 1.0}},
                    },
                }
            ]
        }
    }

    bars = module.parse_bars("AAA", payload, source_id="yahoo_finance_backup")

    assert len(bars) == 2
    assert bars[0].ticker == "AAA"
    assert bars[0].close == 10.0
    assert bars[0].adj_close == 5.0
    assert bars[0].open == 10.0
    assert bars[0].price_adjustment == "adjusted"
    assert bars[0].is_adjusted == 1
    assert bars[0].split_factor == 2.0
    assert bars[1].dividend_amount == 0.12


def test_sec_ingestion_parses_filings_and_companyfacts() -> None:
    module = load_script_module("05_sync_med_device_sec_fundamentals.py", "med_device_sec_sync_test")
    company = module.Company(company_id=1, ticker="AAA", cik="0000000001", company_name="AAA Medical")
    submissions = {
        "filings": {
            "recent": {
                "accessionNumber": ["0000000001-26-000001", "0000000001-26-000002"],
                "form": ["10-K", "8-K"],
                "filingDate": ["2026-02-15", "2026-03-01"],
                "reportDate": ["2025-12-31", "2026-02-28"],
                "primaryDocument": ["aaa-20251231.htm", "aaa-8k.htm"],
            }
        }
    }
    filings = module.parse_recent_filings(company, submissions, {"10-K", "8-K"})
    assert len(filings) == 2
    assert filings[0]["accession_nodash"] == "000000000126000001"
    assert filings[0]["source_id"] == "sec_submissions"

    companyfacts = {
        "facts": {
            "us-gaap": {
                "RevenueFromContractWithCustomerExcludingAssessedTax": {
                    "units": {
                        "USD": [
                            {
                                "start": "2025-01-01",
                                "end": "2025-12-31",
                                "fy": 2025,
                                "fp": "FY",
                                "form": "10-K",
                                "filed": "2026-02-15",
                                "accn": "0000000001-26-000001",
                                "val": 1000,
                            }
                        ]
                    }
                },
                "NetCashProvidedByUsedInOperatingActivities": {
                    "units": {
                        "USD": [
                            {
                                "start": "2025-01-01",
                                "end": "2025-12-31",
                                "fy": 2025,
                                "fp": "FY",
                                "form": "10-K",
                                "filed": "2026-02-15",
                                "accn": "0000000001-26-000001",
                                "val": 120,
                            }
                        ]
                    }
                },
                "PaymentsToAcquirePropertyPlantAndEquipment": {
                    "units": {
                        "USD": [
                            {
                                "start": "2025-01-01",
                                "end": "2025-12-31",
                                "fy": 2025,
                                "fp": "FY",
                                "form": "10-K",
                                "filed": "2026-02-15",
                                "accn": "0000000001-26-000001",
                                "val": 20,
                            }
                        ]
                    }
                },
                "CashAndCashEquivalentsAtCarryingValue": {
                    "units": {
                        "USD": [
                            {
                                "end": "2025-12-31",
                                "fy": 2025,
                                "fp": "FY",
                                "form": "10-K",
                                "filed": "2026-02-15",
                                "accn": "0000000001-26-000001",
                                "val": 300,
                            }
                        ]
                    }
                },
            }
        }
    }
    rows = module.build_financial_statement_rows(company, companyfacts)
    assert len(rows) == 1
    assert rows[0]["period_end"] == "2025-12-31"
    assert rows[0]["revenue"] == 1000
    assert rows[0]["free_cash_flow"] == 100
    assert rows[0]["cash_and_investments"] == 300


def test_sec_metric_sort_ignores_malformed_filed_dates() -> None:
    module = load_script_module("05_sync_med_device_sec_fundamentals.py", "med_device_sec_sort_test")
    valid = module.FactObservation(
        metric="revenue",
        concept="Revenue",
        unit="USD",
        value=100.0,
        period_start="2025-01-01",
        period_end="2025-12-31",
        fiscal_year=2025,
        fiscal_period="FY",
        form="10-K",
        filed_date="2026-02-15",
        accession_nodash="valid",
        frame="",
        concept_rank=0,
    )
    malformed = module.FactObservation(
        metric="revenue",
        concept="Revenue",
        unit="USD",
        value=200.0,
        period_start="2025-01-01",
        period_end="2025-12-31",
        fiscal_year=2025,
        fiscal_period="FY",
        form="10-K",
        filed_date="not-a-date",
        accession_nodash="malformed",
        frame="",
        concept_rank=0,
    )

    assert module.sortable_filed_date("not-a-date") == ""
    assert module.observation_sort_key(valid) > module.observation_sort_key(malformed)


def test_financial_feature_builder_computes_ttm_and_valuation(tmp_path: Path) -> None:
    module = load_script_module("06_build_med_device_financial_features.py", "med_device_financial_features_test")
    db_path = tmp_path / "med_devices.sqlite"
    with connect(db_path) as conn:
        init_db(conn)
        conn.execute(
            """
            INSERT INTO source_registry(
                source_id, stage, source_name, source_type, base_url, created_at, updated_at
            )
            VALUES
                ('yahoo_finance_backup', 'stage_1', 'Yahoo Finance', 'api', 'https://query1.finance.yahoo.com', '2026-01-01', '2026-01-01'),
                ('sec_companyfacts', 'stage_1', 'SEC companyfacts', 'api', 'https://data.sec.gov', '2026-01-01', '2026-01-01')
            """
        )
        conn.execute(
            """
            INSERT INTO dim_company(
                company_id, ticker, cik, company_name, exchange, subsector, country, currency,
                universe_status, is_active, first_seen_at, updated_at
            )
            VALUES (1, 'AAA', '0000000001', 'AAA Medical', 'NYSE', 'medical_devices',
                    'United States', 'USD', 'active', 1, '2026-01-01', '2026-01-01')
            """
        )
        conn.execute(
            """
            INSERT INTO fact_price_ohlcv(
                ticker, bar_date, source_id, open, high, low, close, adj_close, volume,
                price_adjustment, is_adjusted, created_at, updated_at
            )
            VALUES ('AAA', '2026-05-22', 'yahoo_finance_backup', 50, 50, 50, 50, 50,
                    1000000, 'adjusted', 1, '2026-05-22', '2026-05-22')
            """
        )
        rows = [
            ("2025-03-31", 2025, "Q1", "10-Q", "2025-04-25", 250, 150, 45, 30, 45, 8, 37, 80, 190, 100),
            ("2024-12-31", 2024, "FY", "10-K", "2025-02-01", 800, 480, 120, 90, 150, 35, 115, 90, 180, 100),
            ("2026-03-31", 2026, "Q1", "10-Q", "2026-04-25", 300, 180, 60, 45, 50, 10, 40, 100, 200, 100),
            ("2025-12-31", 2025, "FY", "10-K", "2026-02-01", 1000, 600, 200, 150, 180, 40, 140, 95, 210, 100),
        ]
        conn.executemany(
            """
            INSERT INTO fact_financial_statement(
                company_id, period_end, fiscal_year, fiscal_period, form, filed_date,
                revenue, gross_profit, operating_income, net_income, operating_cash_flow,
                capital_expenditures, free_cash_flow, cash_and_investments, total_debt,
                shares_outstanding, source_id, created_at, updated_at
            )
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'sec_companyfacts', '2026-01-01', '2026-01-01')
            """,
            rows,
        )
        companies = [module.Company(company_id=1, ticker="AAA", company_name="AAA Medical", subsector="medical_devices")]
        policy = module.FinancialFeaturePolicy(
            market_sources=["yahoo_finance_backup"],
            share_count_sources=["yahoo_finance_backup", "sec_companyfacts"],
            share_count_max_staleness_days=30,
            allow_sec_weighted_average_share_fallback=True,
            max_staleness_days=7,
            require_adjusted=True,
            core_min_years=1.0,
            core_min_group_years=1.0,
            short_min_years=0.5,
            neutral_component_score=module.DEFAULT_NEUTRAL_COMPONENT_SCORE,
            fundamental_weights=module.DEFAULT_FUNDAMENTAL_COMPONENT_WEIGHTS,
            valuation_weights=module.DEFAULT_VALUATION_COMPONENT_WEIGHTS,
            subsector_blend_weight=0.60,
            winsor_low_pct=0.05,
            winsor_high_pct=0.95,
            ttm_sanity_min_annual_ratio=0.20,
            ttm_sanity_max_annual_ratio=3.00,
        )
        feature_rows = module.build_features(
            conn,
            companies,
            asof=module.parse_date("2026-05-22"),
            policy=policy,
        )
        assert len(feature_rows) == 1
        feature = feature_rows[0]
        assert feature.revenue_ttm == 1050
        assert feature.free_cash_flow_ttm == 143
        assert feature.market_cap == 5000
        assert feature.enterprise_value == 5100
        assert round(feature.ev_to_sales or 0, 4) == round(5100 / 1050, 4)
        assert feature.data_quality_status == "pass"
        assert feature.fundamental_quality_score_v1 is not None
        assert feature.valuation_score_v1 is not None

        module.upsert_feature_rows(conn, feature_rows)
        detail_row = conn.execute("SELECT revenue_ttm FROM feature_financial_valuation WHERE ticker = 'AAA'").fetchone()
        quality_row = conn.execute("SELECT score FROM feature_fundamental_quality WHERE company_id = 1").fetchone()

    assert detail_row is not None
    assert detail_row["revenue_ttm"] == 1050
    assert quality_row is not None
    assert quality_row["score"] is not None


def test_financial_read_csv_flexible_reports_decode_failure(tmp_path: Path) -> None:
    module = load_script_module("06_build_med_device_financial_features.py", "med_device_financial_csv_test")
    bad_csv = tmp_path / "bad.csv"
    bad_csv.write_bytes(b"ticker,shares\nAAA,\x81\n")

    try:
        module.read_csv_flexible(bad_csv)
    except ValueError as exc:
        assert "Could not decode CSV" in str(exc)
    else:
        raise AssertionError("Expected read_csv_flexible to raise ValueError for undecodable CSV")


def test_fda_core_parser_populates_canonical_tables(tmp_path: Path) -> None:
    module = load_script_module("08_sync_med_device_fda_core.py", "med_device_fda_core_sync_test")
    key_file = tmp_path / "secrets.local.yaml"
    key_file.write_text('openfda_api_key: "test_key"\n', encoding="utf-8")
    policy = module.FdaPolicy(
        source_id="openfda_device",
        base_url="https://api.fda.gov/device",
        api_key_env="OPENFDA_API_KEY_NOT_SET",
        api_key_file=str(key_file),
        api_key_file_field="openfda_api_key",
        timeout_sec=30.0,
        max_retries=3,
        parallel_workers=1,
        sleep_sec=0.15,
        page_limit=1000,
        commit_every_pages=10,
        user_agent="test",
        endpoints=[],
    )
    assert module.resolve_api_key({}, policy=policy, base_dir=tmp_path) == "test_key"

    db_path = tmp_path / "med_devices.sqlite"
    with connect(db_path) as conn:
        init_db(conn)
        upsert_source_registry(conn, load_source_registry(SOURCE_REGISTRY))
        module.upsert_classification(
            conn,
            {
                "product_code": "ABC",
                "device_name": "Cardiac Monitor",
                "medical_specialty": "Cardiovascular",
                "device_class": "2",
                "regulation_number": "870.2300",
            },
            source_id="openfda_device",
        )
        module.upsert_approval(
            conn,
            {
                "k_number": "K260001",
                "applicant": "Example Devices Inc.",
                "decision_date": "20260501",
                "date_received": "20260115",
                "decision_description": "Substantially Equivalent",
                "device_name": "Cardiac Monitor",
                "product_code": "ABC",
            },
            endpoint_name="approvals_510k",
            source_id="openfda_device",
        )
        module.upsert_recall(
            conn,
            {
                "recall_number": "Z-0001-2026",
                "recalling_firm": "Example Devices Inc.",
                "classification": "Class I",
                "recall_initiation_date": "20260415",
                "reason_for_recall": "Test recall",
                "product_code": "ABC",
            },
            endpoint_name="enforcement",
            source_id="openfda_device",
        )
        module.upsert_adverse_event(
            conn,
            {
                "mdr_report_key": "123",
                "date_received": "20260420",
                "date_of_event": "20260418",
                "event_type": "Injury",
                "device": [
                    {
                        "manufacturer_d_name": "Example Devices Inc.",
                        "device_report_product_code": "ABC",
                        "brand_name": "Cardiac Monitor",
                    }
                ],
            },
            source_id="openfda_device",
        )
        product_row = conn.execute("SELECT product_code FROM dim_fda_product_code WHERE product_code = 'ABC'").fetchone()
        approval_row = conn.execute("SELECT decision_date FROM fact_fda_approval WHERE submission_number = 'K260001'").fetchone()
        recall_row = conn.execute("SELECT severity_weight FROM fact_fda_recall WHERE recall_number = 'Z-0001-2026'").fetchone()
        event_row = conn.execute("SELECT injury_count FROM fact_fda_adverse_event WHERE adverse_event_id = '123'").fetchone()

    assert product_row is not None
    assert approval_row is not None
    assert approval_row["decision_date"] == "2026-05-01"
    assert recall_row is not None
    assert recall_row["severity_weight"] == 5.0
    assert event_row is not None
    assert event_row["injury_count"] == 1


def test_raw_api_responses_are_run_scoped_after_legacy_migration(tmp_path: Path) -> None:
    module = load_script_module("08_sync_med_device_fda_core.py", "med_device_fda_raw_response_test")
    db_path = tmp_path / "med_devices.sqlite"
    legacy_conn = sqlite3.connect(db_path)
    legacy_conn.execute(
        """
        CREATE TABLE raw_api_responses (
            raw_response_id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            query_params_json TEXT,
            request_time_utc TEXT NOT NULL,
            response_status INTEGER,
            response_hash TEXT NOT NULL,
            asof_date TEXT,
            payload_text TEXT,
            ingestion_run_id INTEGER,
            created_at TEXT NOT NULL,
            UNIQUE(source_id, endpoint, response_hash)
        )
        """
    )
    legacy_conn.commit()
    legacy_conn.close()

    with connect(db_path) as conn:
        init_db(conn)
        table_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'raw_api_responses'"
        ).fetchone()
        index_names = {
            str(row["name"])
            for row in conn.execute("PRAGMA index_list(raw_api_responses)").fetchall()
        }
        upsert_source_registry(conn, load_source_registry(SOURCE_REGISTRY))
        run_1 = module.start_ingestion_run(conn, "openfda_device")
        run_2 = module.start_ingestion_run(conn, "openfda_device")
        for run_id in (run_1, run_2):
            module.store_raw_response(
                conn,
                source_id="openfda_device",
                endpoint="https://api.fda.gov/device/recall.json",
                query_params={"limit": 1000, "skip": 0},
                response_status=200,
                payload_text='{"results":[{"recall_number":"Z-0001-2026"}]}',
                ingestion_run_id=run_id,
            )
        rows = conn.execute(
            """
            SELECT ingestion_run_id, response_hash
            FROM raw_api_responses
            WHERE source_id = 'openfda_device'
            ORDER BY ingestion_run_id
            """
        ).fetchall()

    assert table_sql is not None
    assert "UNIQUE(source_id, endpoint, response_hash)" not in str(table_sql["sql"])
    assert "idx_raw_api_responses_run_query" in index_names
    assert [int(row["ingestion_run_id"]) for row in rows] == [run_1, run_2]
    assert len({str(row["response_hash"]) for row in rows}) == 1


def test_fda_recall_lookup_uses_partial_index(tmp_path: Path) -> None:
    module = load_script_module("08_sync_med_device_fda_core.py", "med_device_fda_recall_plan_test")
    db_path = tmp_path / "med_devices.sqlite"
    with connect(db_path) as conn:
        init_db(conn)
        exact_plan = conn.execute(
            "EXPLAIN QUERY PLAN " + module.FDA_RECALL_LOOKUP_SQL,
            ("recall_number:Z00012026", "openfda_device", "recall"),
        ).fetchall()
        legacy_plan = conn.execute(
            "EXPLAIN QUERY PLAN " + module.FDA_RECALL_LEGACY_ENDPOINT_LOOKUP_SQL,
            ("recall_number:Z00012026", "openfda_device"),
        ).fetchall()

    details = " ".join(str(row["detail"]) for row in [*exact_plan, *legacy_plan])
    assert "idx_fact_fda_recall_key_endpoint" in details
    assert "SCAN fact_fda_recall" not in details


def test_fda_linker_and_feature_builder_scores_mapped_records(tmp_path: Path) -> None:
    fda_module = load_script_module("08_sync_med_device_fda_core.py", "med_device_fda_core_for_features_test")
    link_module = load_script_module("09_link_med_device_fda_to_companies.py", "med_device_fda_link_test")
    feature_module = load_script_module("10_build_med_device_fda_features.py", "med_device_fda_features_test")
    db_path = tmp_path / "med_devices.sqlite"
    with connect(db_path) as conn:
        init_db(conn)
        upsert_source_registry(conn, load_source_registry(SOURCE_REGISTRY))
        conn.execute(
            """
            INSERT INTO dim_company(
                company_id, ticker, cik, company_name, exchange, subsector, country, currency,
                universe_status, is_active, first_seen_at, updated_at
            )
            VALUES (1, 'EXMD', '0000000001', 'Example Devices Inc.', 'NYSE', 'monitoring',
                    'United States', 'USD', 'active', 1, '2026-01-01', '2026-01-01')
            """
        )
        fda_module.upsert_approval(
            conn,
            {
                "k_number": "K260001",
                "applicant": "Example Devices Incorporated",
                "decision_date": "20260501",
                "device_name": "Cardiac Monitor",
                "product_code": "ABC",
            },
            endpoint_name="approvals_510k",
            source_id="openfda_device",
        )
        fda_module.upsert_recall(
            conn,
            {
                "recall_number": "Z-0001-2026",
                "recalling_firm": "Example Devices Incorporated",
                "classification": "Class I",
                "recall_initiation_date": "20260415",
                "product_code": "ABC",
            },
            endpoint_name="enforcement",
            source_id="openfda_device",
        )
        aliases = link_module.build_aliases(conn)
        manufacturers = conn.execute("SELECT fda_manufacturer_id, manufacturer_name FROM dim_fda_manufacturer").fetchall()
        for manufacturer in manufacturers:
            match = link_module.best_match(
                str(manufacturer["manufacturer_name"]),
                aliases,
                token_score_weight=100.0,
                min_confidence=75.0,
                edit_distance_max_normalized=0.20,
                edit_distance_score=70.0,
            )
            conn.execute(
                """
                UPDATE dim_fda_manufacturer
                SET parent_company_id = ?, mapping_confidence = ?, mapping_method = ?
                WHERE fda_manufacturer_id = ?
                """,
                (match.company_id, match.confidence, match.method, int(manufacturer["fda_manufacturer_id"])),
            )
        link_module.update_fact_company_ids(conn, min_confidence=75.0)
        feature_module.refresh_canonical_recalls(conn)
        companies = [feature_module.Company(company_id=1, ticker="EXMD", company_name="Example Devices Inc.")]
        policy = feature_module.FdaFeaturePolicy(
            source_id="openfda_device",
            short_months=12,
            medium_months=24,
            long_months=36,
            no_data_innovation_score=20.0,
            no_data_risk_score=65.0,
            revenue_floor=100000000.0,
            recall_decay_half_life_days=730.0,
            innovation_base_score=25.0,
            innovation_approval_log_weight=18.0,
            innovation_pma_log_weight=16.0,
            innovation_product_code_log_weight=12.0,
            risk_recall_severity_weight=4.0,
            risk_class_i_recall_weight=20.0,
            risk_death_per_billion_weight=5.0,
            risk_injury_per_billion_weight=0.5,
            risk_malfunction_per_billion_weight=0.1,
            risk_adverse_acceleration_per_billion_weight=0.5,
            min_mapping_confidence=75.0,
            class_i_lookback_months=36,
            death_lookback_months=24,
            death_event_min_count=1,
            class_i_hard_min_count=5,
            class_i_hard_min_severity_per_billion=10.0,
            death_event_hard_min_count=3,
            death_event_min_rate_per_billion=1.0,
            low_mapping_confidence_is_hard_red=False,
            regulatory_risk_weight=0.60,
            regulatory_innovation_weight=0.40,
        )
        asof = feature_module.parse_date("2026-05-22")
        assert asof is not None
        rows = feature_module.build_rows(conn, companies, asof=asof, policy=policy)

    assert len(rows) == 1
    assert rows[0].approval_count_12m == 1
    assert rows[0].class_i_recall_count_36m == 1
    assert rows[0].hard_red_flag == 1
    assert rows[0].fda_product_score is not None


def test_reimbursement_feature_builder_is_conservative_without_cms_data(tmp_path: Path) -> None:
    module = load_script_module("11_build_med_device_reimbursement_features.py", "med_device_reimbursement_features_test")
    db_path = tmp_path / "med_devices.sqlite"
    with connect(db_path) as conn:
        init_db(conn)
        conn.execute(
            """
            INSERT INTO dim_company(
                company_id, ticker, cik, company_name, exchange, subsector, country, currency,
                universe_status, is_active, first_seen_at, updated_at
            )
            VALUES (1, 'EXMD', '0000000001', 'Example Devices Inc.', 'NYSE', 'monitoring',
                    'United States', 'USD', 'active', 1, '2026-01-01', '2026-01-01')
            """
        )
        policy = module.ReimbursementPolicy(
            source_ids=["cms_coverage_api", "cms_payment_files"],
            no_data_score=25.0,
            no_data_coverage_clarity_score=25.0,
            no_data_payment_adequacy_score=25.0,
            company_mention_score=45.0,
            policy_evidence_score=60.0,
            rate_evidence_score=65.0,
            coverage_weight=0.50,
            payment_weight=0.50,
            mention_count_boost_per_hit=2.0,
            mention_count_boost_cap=10.0,
            low_confidence_hard_flag=False,
            use_fallback_policy_scan_when_unmapped=True,
            valid_no_rate_statuses={"not_applicable", "bundled", "unknown"},
        )
        rows = module.build_rows(
            conn,
            [module.Company(company_id=1, ticker="EXMD", company_name="Example Devices Inc.")],
            asof="2026-05-22",
            policy=policy,
        )

    assert len(rows) == 1
    assert rows[0].score == 25.0
    assert rows[0].review_reason == "cms_reimbursement_data_not_loaded"


def test_daily_scores_durable_proxy_uses_canonical_fcf_margin_field() -> None:
    module = load_script_module("13_build_med_device_daily_scores.py", "med_device_daily_scores_proxy_test")
    assert "fcf_margin_ttm" in module.DURABLE_GROWTH_PROXY_INPUT_FIELDS
    assert "free_cash_flow_margin_ttm" not in module.DURABLE_GROWTH_PROXY_INPUT_FIELDS
    assert module.durable_proxy_available({"fcf_margin_ttm": "10", "gross_margin_ttm": "55"})
    row = module.ScoreRow(
        asof_date="2026-06-01",
        scoring_model_version="test",
        rank=0,
        company_id=1,
        ticker="AAA",
        company_name="AAA Medical",
        subsector="medical_devices",
    )
    assert row.durable_growth_validation_status == module.DURABLE_GROWTH_PRODUCTION_DISABLED
    assert row.durable_growth_production_state == module.DURABLE_GROWTH_PRODUCTION_DISABLED


def test_cohort_neutral_backtest_loads_fda_mapping_confidence_alias(tmp_path: Path) -> None:
    module = load_script_module(
        "23_backtest_med_device_cohort_neutral_scores.py",
        "med_device_cohort_neutral_scores_test",
    )
    db_path = tmp_path / "med_devices.sqlite"
    with connect(db_path) as conn:
        init_db(conn)
        conn.execute(
            """
            INSERT INTO dim_company(
                company_id, ticker, cik, company_name, exchange, subsector, country, currency,
                universe_status, is_active, first_seen_at, updated_at
            )
            VALUES (1, 'AAA', '0000000001', 'AAA Medical', 'NYSE', 'medical_devices',
                    'United States', 'USD', 'active', 1, '2026-01-01', '2026-01-01')
            """
        )
        conn.execute(
            """
            INSERT INTO med_device_daily_scores(
                asof_date, company_id, scoring_model_version, composite_score, raw_composite_score,
                composite_percentile, created_at, updated_at
            )
            VALUES ('2026-06-01', 1, 'test', 55, 55, 50, '2026-06-01', '2026-06-01')
            """
        )
        conn.execute(
            """
            INSERT INTO feature_fda_product_risk(
                asof_date, company_id, avg_mapping_confidence, created_at, updated_at
            )
            VALUES ('2026-06-01', 1, 91.5, '2026-06-01', '2026-06-01')
            """
        )
        scores = module.load_scores(conn, asofs={"2026-06-01"})

    assert scores[("2026-06-01", "AAA")]["avg_fda_mapping_confidence"] == 91.5


def test_daily_score_template_tier1_metadata_is_explicit() -> None:
    module = load_script_module("13_build_med_device_daily_scores.py", "med_device_daily_scores_tier1_template_test")

    safe_template = module.parse_score_template(
        {
            "template_id": "safe_quality_value",
            "tier1_role": "safe_core",
            "tier1_eligible": True,
            "components": [
                {"field": "fundamental_quality_score", "direction": "positive", "weight": 0.6},
                {"field": "valuation_score", "direction": "positive", "weight": 0.4},
            ],
        },
        context="test.safe_template",
    )
    special_template = module.parse_score_template(
        {
            "template_id": "pullback_research",
            "tier1_role": "special_situation",
            "tier1_eligible": True,
            "components": [
                {"field": "technical_pullback_score", "direction": "positive", "weight": 1.0},
            ],
        },
        context="test.special_template",
    )

    assert safe_template.tier1_role == module.TIER1_TEMPLATE_ROLE_SAFE_CORE
    assert safe_template.tier1_eligible is True
    assert "role=safe_core;tier1_eligible=1" in module.score_template_spec(safe_template)
    assert special_template.tier1_role == module.TIER1_TEMPLATE_ROLE_SPECIAL_SITUATION
    assert special_template.tier1_eligible is False


def test_daily_score_tier1_safety_gate_routes_special_situations() -> None:
    module = load_script_module("13_build_med_device_daily_scores.py", "med_device_daily_scores_tier1_safety_test")
    row = module.ScoreRow(
        asof_date="2026-06-01",
        scoring_model_version="test",
        rank=0,
        company_id=1,
        ticker="TLSI",
        company_name="TriSalus Life Sciences",
        subsector="medical_devices",
        raw_composite_score=85.0,
        composite_percentile=95.0,
        cohort_percentile=95.0,
        calibration_cohort="implantable_interventional_devices_procedure_bundled",
        cohort_score_template_id="procedure_bundled_pullback_fda_risk_only",
        cohort_score_template_spec=(
            "role=special_situation;tier1_eligible=0;"
            "technical_pullback_score:positive:0.45;valuation_score:inverse:0.20"
        ),
        cohort_score_template_tier1_role=module.TIER1_TEMPLATE_ROLE_SPECIAL_SITUATION,
        cohort_score_template_tier1_eligible=0,
        single_product_risk_flag=1,
        binary_event_risk_flag=1,
        fundamental_quality_score=80.0,
        durable_growth_score=70.0,
        fda_product_score=80.0,
        fda_event_risk_score=10.0,
        reimbursement_score=70.0,
        reimbursement_status="direct_payment_evidence",
        unknown_reimbursement_flag=0,
        valuation_score=80.0,
        technical_entry_score=70.0,
        technical_entry_status_score=70.0,
        value_trap_score=5.0,
        data_completeness_score=100.0,
        avg_dollar_volume_60d=10_000_000.0,
        market_cap=2_000_000_000.0,
        fda_data_available=1,
    )
    gates = {
        "composite_min": 75.0,
        "cohort_percentile_min": 0.0,
        "fundamental_quality_min": 70.0,
        "durable_growth_min": 60.0,
        "fda_product_min": 60.0,
        "reimbursement_min": 45.0,
        "valuation_min": 60.0,
        "technical_entry_min": 55.0,
        "data_completeness_min": 90.0,
        "min_avg_dollar_volume_60d": 1_000_000.0,
        "watchlist_min": 60.0,
        "value_trap_max": 20.0,
        "value_trap_hard_max": 85.0,
    }
    policy = module.Tier1SafetyPolicy(
        min_market_cap=500_000_000.0,
        min_avg_dollar_volume_60d=2_000_000.0,
        ticker_denylist=("tlsi",),
    )

    module.classify(row, gates=gates, tier1_policy=policy)

    assert row.passed_tier1_safety_gate == 0
    assert row.tier1_safety_status == module.TIER1_SAFETY_STATUS_FAIL
    assert row.final_investability_gate == 0
    assert row.classification == "special_situation_or_binary_risk_watchlist"
    assert "template_not_safe_core" in row.tier1_safety_reason
    assert "single_product_risk" in row.tier1_safety_reason
    assert "ticker_denylist" in row.tier1_safety_reason
