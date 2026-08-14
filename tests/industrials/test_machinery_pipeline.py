from __future__ import annotations

import csv
import hashlib
import json
import runpy
import sqlite3
import subprocess
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from industrials.core import reports
from industrials.core.config import cfg_get, load_yaml
from industrials.core.db import (
    connect,
    init_db,
    seed_xbrl_concept_map,
    utc_now,
    xbrl_concept_seed_is_current,
)
from industrials.core.reports import write_csv_atomic
from industrials.machinery.build_contract import (
    HISTORICAL_BUILD_METADATA_FILENAME,
    historical_build_metadata,
)
from industrials.machinery.historical_coverage import build_combined_historical_coverage
from industrials.core.policy_loader import load_eligibility_policy
from industrials.machinery.contracts import resolve_norgate_mappings
from industrials.machinery.disclosure_candidates import extract_machinery_prose_candidates
from industrials.machinery.financial_contract import required_metric_names
from industrials.machinery.scoring import (
    FINAL_RANK_FIELDS,
    PORTFOLIO_REQUIRED_FIELDS,
    AVAILABILITY_STATUS_FIELDS,
    build_scoring_feature_rows,
    publish_dashboard,
    read_rows,
    survivorship_sidecar,
    validate_metric_availability_contract,
)
from portfolio_layer.core.contracts import read_csv
from portfolio_layer.core.db import connect as portfolio_connect
from portfolio_layer.core.db import init_db as init_portfolio_db
from portfolio_layer.scores.adapters import run_adapter


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MACHINERY_CONFIG = PROJECT_ROOT / "industrials" / "machinery" / "config.yaml"
ASOF = "2026-07-09"


class FakeNorgate:
    def __init__(self) -> None:
        self.names = {
            "CAT": "Caterpillar Inc Common",
            "PRCP-202012": "Perceptron Inc Common",
            "RIDE-199907": "Ride Inc Common",
        }
        self.first = {"CAT": "1962-01-02", "PRCP-202012": "1992-08-21", "RIDE-199907": "1997-01-02"}
        self.last = {"CAT": ASOF, "PRCP-202012": "2020-12-18", "RIDE-199907": "1999-07-07"}

    def database_symbols(self, database: str) -> list[str]:
        return ["CAT"] if database == "US Equities" else ["PRCP-202012", "RIDE-199907"]

    def security_name(self, symbol: str) -> str:
        return self.names[symbol]

    def first_quoted_date(self, symbol: str) -> str:
        return self.first[symbol]

    def last_quoted_date(self, symbol: str) -> str:
        return self.last[symbol]


def run_script(script: str, *args: str) -> None:
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / script), *args],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"Script failed: {script}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"


def load_machinery_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "industrials.sqlite"
    run_script(
        "industrials/machinery/scripts/01_load_machinery_universe.py",
        "--config",
        str(MACHINERY_CONFIG),
        "--db",
        str(db_path),
    )
    run_script(
        "industrials/machinery/scripts/01b_load_machinery_historical_membership.py",
        "--config",
        str(MACHINERY_CONFIG),
        "--db",
        str(db_path),
    )
    return db_path


def insert_defense_sentinel(db_path: Path) -> None:
    now = utc_now()
    with connect(db_path) as conn, conn:
        conn.execute(
            """
            INSERT INTO dim_company(
                ticker, cik, company_name, sector, industry, subsector, country, currency,
                universe_status, is_active, data_quality_status, first_seen_at, updated_at
            )
            VALUES ('DEFTEST', '9999999999', 'Defense Sentinel', 'Industrials', 'Aerospace & Defense',
                    'Defense', 'United States', 'USD', 'investable', 1, 'test', ?, ?)
            """,
            (now, now),
        )
        company_id = int(conn.execute("SELECT company_id FROM dim_company WHERE ticker='DEFTEST'").fetchone()[0])
        conn.execute(
            """
            INSERT INTO dim_industrials_taxonomy(
                company_id, ticker, model_family, sector, industry, subsector,
                calibration_cohort_id, calibration_cohort, calibration_use, development_stage,
                taxonomy_confidence, taxonomy_source, analyst_reviewed, updated_at
            )
            VALUES (?, 'DEFTEST', 'defense', 'Industrials', 'Aerospace & Defense', 'Defense',
                    'defense_test', 'Defense test', 'core', 'operating', 1.0,
                    'defense_cohort_policy', 1, ?)
            """,
            (company_id, now),
        )
        conn.execute(
            """
            INSERT INTO dim_universe_membership(
                company_id, ticker, model_family, membership_source_id, membership_basis,
                start_date, end_date, membership_status, is_current_member,
                point_in_time_flag, confidence, reason, created_at, updated_at
            )
            VALUES (?, 'DEFTEST', 'defense', 'defense_ticker_seed', 'current_source_of_truth',
                    '2019-01-02', NULL, 'active', 1, 1, 1.0, 'isolation sentinel', ?, ?)
            """,
            (company_id, now, now),
        )


def seed_scoring_features(db_path: Path, *, count: int = 12) -> None:
    now = utc_now()
    with connect(db_path) as conn, conn:
        tickers = [
            str(row["ticker"])
            for row in conn.execute(
                """
                SELECT ticker
                FROM dim_industrials_taxonomy
                WHERE model_family='machinery' AND calibration_use <> 'historical_research'
                ORDER BY ticker
                LIMIT ?
                """,
                (count,),
            ).fetchall()
        ]
        for index, ticker in enumerate(tickers, start=1):
            conn.execute(
                """
                INSERT INTO feature_market_technical(
                    ticker, asof_date, source_id, model_family, latest_close, latest_adj_close,
                    trading_days_available, latest_bar_date, stale_days, stale_flag,
                    low_history_flag, low_liquidity_flag, ret_1m, ret_3m, ret_6m,
                    ret_12m_ex_1m, rel_strength_bench_3m, avg_dollar_volume_60d,
                    realized_vol_60d, max_drawdown_12m, distance_from_52w_high,
                    above_ma_50d, above_ma_200d, market_data_quality, created_at, updated_at
                )
                VALUES (?, ?, 'yahoo_finance_adjusted', 'machinery', ?, ?, 500, ?, 0, 0, 0, 0,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, 'complete', ?, ?)
                """,
                (
                    ticker,
                    ASOF,
                    50.0 + index,
                    50.0 + index,
                    ASOF,
                    0.01 * index,
                    0.015 * index,
                    0.02 * index,
                    0.03 * index,
                    0.005 * index,
                    20_000_000.0 + index * 1_000_000.0,
                    0.18 + index * 0.005,
                    -0.30 + index * 0.01,
                    -0.20 + index * 0.01,
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO feature_financial_statement(
                    ticker, asof_date, source_id, model_family, revenue_ttm_usd, gross_margin,
                    operating_margin, fcf_margin, net_cash_to_assets, fcf_to_net_income,
                    revenue_yoy_growth, gross_profit_yoy_growth, operating_income_yoy_growth,
                    free_cash_flow_yoy_growth, revenue_acceleration, fcf_yield,
                    ev_gross_profit, ev_operating_income, market_cap, inventory_days,
                    book_to_bill, funded_backlog, remaining_performance_obligation,
                    capex_usd, financial_confidence, financial_fallback_status,
                    created_at, updated_at
                )
                VALUES (?, ?, 'sec_companyfacts', 'machinery', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, 0.90, 'none', ?, ?)
                """,
                (
                    ticker,
                    ASOF,
                    1_000_000_000.0 + index * 50_000_000.0,
                    0.25 + index * 0.005,
                    0.10 + index * 0.004,
                    0.08 + index * 0.003,
                    -0.05 + index * 0.01,
                    0.8 + index * 0.02,
                    0.02 + index * 0.005,
                    0.02 + index * 0.004,
                    0.01 + index * 0.004,
                    0.01 + index * 0.003,
                    -0.01 + index * 0.002,
                    0.03 + index * 0.001,
                    8.0 - index * 0.1,
                    14.0 - index * 0.2,
                    5_000_000_000.0 + index * 100_000_000.0,
                    80.0 - index,
                    0.8 + index * 0.03,
                    300_000_000.0 + index * 10_000_000.0,
                    250_000_000.0 + index * 8_000_000.0,
                    -50_000_000.0 - index * 1_000_000.0,
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                UPDATE feature_financial_statement
                SET reporting_profile = 'SEC_XBRL_US_GAAP',
                    data_quality_status = 'complete',
                    roic = ?, asset_turnover = ?, incremental_operating_margin = ?,
                    inventory_growth = ?, inventory_sales_growth_spread = ?,
                    cash_conversion_cycle_change = ?, net_debt_to_ebitda = ?,
                    interest_coverage = ?, orders_yoy_growth = ?, backlog_yoy_growth = ?,
                    backlog_to_revenue = ?, cash_runway_years = ?, capital_raise_dependence = ?,
                    diluted_shares_yoy_growth = ?, sbc_pct_revenue = ?,
                    orders_ttm_usd = ?, funded_backlog_usd = ?
                WHERE ticker = ? AND asof_date = ? AND source_id = 'sec_companyfacts'
                  AND model_family = 'machinery'
                """,
                (
                    0.08 + index * 0.005,
                    0.7 + index * 0.02,
                    0.05 + index * 0.004,
                    0.02 + index * 0.002,
                    0.01 - index * 0.001,
                    -2.0 + index * 0.1,
                    2.5 - index * 0.1,
                    4.0 + index * 0.2,
                    0.01 + index * 0.003,
                    0.02 + index * 0.002,
                    0.3 + index * 0.01,
                    1.0 + index * 0.2,
                    0.8 - index * 0.03,
                    0.20 - index * 0.01,
                    0.12 - index * 0.005,
                    900_000_000.0 + index * 40_000_000.0,
                    300_000_000.0 + index * 10_000_000.0,
                    ticker,
                    ASOF,
                ),
            )
            conn.execute(
                """
                INSERT INTO feature_positioning(
                    ticker, asof_date, source_id, model_family, insider_purchase_count_90d,
                    insider_sale_count_90d, insider_cluster_buyers_90d, insider_net_value_90d,
                    institutional_ownership_delta_pct, short_interest_change_3m,
                    latest_days_to_cover, latest_borrow_fee_rate, positioning_quality,
                    created_at, updated_at
                )
                VALUES (?, ?, 'industrials_positioning_composite', 'machinery', ?, 0, ?, ?, ?, ?, ?, ?,
                        'complete', ?, ?)
                """,
                (
                    ticker,
                    ASOF,
                    index,
                    index % 4,
                    100_000.0 * index,
                    0.001 * index,
                    0.01 - index * 0.001,
                    4.0 - index * 0.1,
                    0.02 + index * 0.001,
                    now,
                    now,
                ),
            )
        availability_tickers = [
            str(row["ticker"])
            for row in conn.execute(
                """
                SELECT DISTINCT ticker
                FROM dim_universe_membership
                WHERE model_family = 'machinery'
                  AND start_date <= ?
                  AND COALESCE(end_date, '9999-12-31') >= ?
                ORDER BY ticker
                """,
                (ASOF, ASOF),
            ).fetchall()
        ]
        conn.executemany(
            """
            INSERT INTO feature_financial_metric_availability(
                ticker, asof_date, model_family, metric_name, availability_status,
                source_id, extraction_method, confidence, status_reason,
                created_at, updated_at
            )
            VALUES (?, ?, 'machinery', ?, 'NOT_DISCLOSED', 'sec_companyfacts',
                    'synthetic_test_fixture', 0.0, 'fixture_has_no_reported_value', ?, ?)
            """,
            [
                (ticker, ASOF, metric_name, now, now)
                for ticker in availability_tickers
                for metric_name in required_metric_names()
            ],
        )
        for ticker in availability_tickers:
            accession = f"TEST-{ticker}-2026Q2"
            conn.execute(
                """
                INSERT INTO fact_sec_filing(
                    ticker, source_id, accession_number, form_type, filing_date,
                    accepted_at, report_date, fiscal_year, fiscal_period,
                    primary_document, created_at, updated_at
                )
                VALUES (?, 'sec_companyfacts', ?, '10-Q', '2026-07-08',
                        '2026-07-08T12:00:00Z', '2026-06-30', 2026, 'Q2',
                        'synthetic-test.htm', ?, ?)
                """,
                (ticker, accession, now, now),
            )
            conn.executemany(
                """
                INSERT INTO fact_financial_statement_canonical(
                    ticker, source_id, model_family, canonical_metric, period_end,
                    filing_date, accepted_at, accession_number, form_type,
                    fiscal_year, fiscal_period, reporting_standard, taxonomy,
                    concept_name, unit, value, value_usd, source_priority,
                    canonical_quality, created_at, updated_at
                )
                VALUES (?, 'sec_companyfacts', 'machinery', ?, '2026-06-30',
                        '2026-07-08', '2026-07-08T12:00:00Z', ?, '10-Q',
                        2026, 'Q2', 'US_GAAP', 'us-gaap', ?, 'USD', ?, ?, 10,
                        'synthetic_test_fixture', ?, ?)
                """,
                [
                    (ticker, 'revenue', accession, 'Revenue', 1_000_000_000.0,
                     1_000_000_000.0, now, now),
                    (ticker, 'assets', accession, 'Assets', 2_000_000_000.0,
                     2_000_000_000.0, now, now),
                ],
            )


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({field for row in rows for field in row})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_portfolio_smoke_config(path: Path) -> None:
    path.write_text(
        """
paths:
  database_path: "db/portfolio_layer.sqlite"
  output_dir: "output"
  cache_dir: "output/cache"
  macro_serving_db_path: "macro.sqlite"
runtime:
  sqlite_timeout_sec: 30.0
score_contract:
  contract_version: "stocks_scores_v1"
  sector_output_root: "s"
  staleness_tolerance_days: 10
  min_successful_sectors: 2
  native_score_range: {min: 0.0, max: 100.0}
  max_abs_expected_alpha: 1.0
  rating_bands: {strong_buy: 90.0, buy: 70.0, hold: 40.0, reduce: 20.0, avoid: 0.0}
  sectors:
    - model_family: machinery
      adapter: industrial_family
      enabled: true
      required: true
      staleness_tolerance_days: 3
      sector: "Industrials"
      industry: "Machinery"
      industry_aggregate: "Machinery"
      file_mode: dated
      file_path: "industrials/machinery/dashboard/{yyyy-mm-dd}/machinery_final_rank_table.csv"
      require_oos_score_valid: true
      calibration: {neutral: 50.0, scale: 50.0, expected_alpha_at_full: 0.15}
    - model_family: med_devices
      adapter: med_devices
      enabled: true
      required: true
      staleness_tolerance_days: 3
      sector: "Health Care"
      industry: "Health Care Equipment"
      industry_aggregate: "Health Care Equipment & Services"
      file_mode: dated
      file_path: "med_devices/{yyyy-mm-dd}/med_device_daily_composite_scores.csv"
      require_oos_score_valid: true
      calibration: {neutral: 50.0, scale: 50.0, expected_alpha_at_full: 0.15}
""".lstrip(),
        encoding="utf-8",
    )


def write_med_fixture(path: Path) -> None:
    write_csv(
        path,
        [
            {
                "asof_date": ASOF,
                "ticker": "MDTEST",
                "portfolio_candidate_gate": "1",
                "portfolio_candidate_score": "75",
                "portfolio_candidate_reason": "ok",
                "analyst_review_decision": "approve",
                "rank": "1",
                "score_confidence": "0.9",
                "calibration_eligible_flag": "1",
                "research_calibration_input_eligible_flag": "1",
                "calibration_sample_role": "strict_oos",
                "oos_score_valid_flag": "1",
                "oos_score_asof_date": ASOF,
                "survivorship_corrected_panel_flag": "0",
            }
        ],
    )


def adapter_config() -> dict[str, object]:
    return {
        "model_family": "machinery",
        "adapter": "industrial_family",
        "file_mode": "dated",
        "file_path": "industrials/machinery/dashboard/{yyyy-mm-dd}/machinery_final_rank_table.csv",
        "sector": "Industrials",
        "industry": "Machinery",
        "industry_aggregate": "Machinery",
        "require_oos_score_valid": True,
    }


def test_norgate_contract_preserves_provider_symbol_and_rejects_reused_ticker() -> None:
    mappings = resolve_norgate_mappings(
        active_rows=[{"ticker": "CAT", "company_name": "Caterpillar Inc."}],
        delisted_rows=[
            {"ticker": "PRCP", "company": "Perceptron", "exit_year": "2020"},
            {"ticker": "RIDE", "company": "Lordstown Motors", "exit_year": "2023"},
        ],
        provider=FakeNorgate(),
        history_start="2019-01-02",
        known_exclusions={"RIDE": "RIDE in local Norgate belongs to a different issuer."},
        overrides={},
    )
    by_ticker = {mapping.internal_ticker: mapping for mapping in mappings}
    assert by_ticker["CAT"].norgate_symbol == "CAT"
    assert by_ticker["PRCP"].norgate_symbol == "PRCP-202012"
    assert by_ticker["PRCP"].calibration_usable_flag == "1"
    assert by_ticker["RIDE"].norgate_symbol == ""
    assert by_ticker["RIDE"].mapping_status == "excluded_known_unresolved"

    active_override = resolve_norgate_mappings(
        active_rows=[{"ticker": "CAT", "company_name": "Caterpillar Inc."}],
        delisted_rows=[],
        provider=FakeNorgate(),
        history_start="2019-01-02",
        known_exclusions={},
        overrides={
            "CAT": {
                "norgate_symbol": "CAT",
                "source_database": "US Equities",
                "mapping_reason": "approved_active_symbol_override",
                "review_status": "approved",
            }
        },
    )[0]
    assert active_override.mapping_status == "verified_override"
    assert active_override.mapping_reason == "approved_active_symbol_override"


def test_portfolio_registration_uses_active_machinery_industrial_adapter() -> None:
    config = load_yaml(PROJECT_ROOT / "portfolio_layer" / "config.yaml")
    sectors = {
        str(row["model_family"]): row for row in cfg_get(config, "score_contract.sectors", []) if isinstance(row, dict)
    }
    assert sectors["defense"]["adapter"] == "tech_family"
    assert sectors["machinery"]["adapter"] == "industrial_family"
    assert sectors["machinery"]["required"] is True
    assert cfg_get(config, "risk_panel.sector_etf_map.machinery") == "XLI"
    assert float(cfg_get(config, "optimizer.sector_weight_caps.machinery")) == 0.05
    assert (
        float(
            cfg_get(
                config,
                "black_litterman_fusion.strategic_sector_weights.machinery",
            )
        )
        == 0.0
    )
    assert cfg_get(config, "sleeves.sector_factor_etfs.machinery") == "XLI"


def test_machinery_orchestrator_orders_related_database_refreshes() -> None:
    namespace = runpy.run_path(
        str(PROJECT_ROOT / "industrials" / "machinery" / "scripts" / "17_run_machinery_refresh_pipeline.py")
    )
    build_steps = namespace["build_steps"]
    steps = build_steps(
        ASOF,
        force=False,
        include_norgate_backfill=False,
        refresh_sec_insider=True,
        full_positioning_refresh=True,
        bootstrap_sec_archives=True,
    )
    step_ids = [step.step_id for step in steps]
    assert step_ids.index("12_sync_sec_ownership") < step_ids.index("13_sync_positioning")
    assert step_ids.index("13_sync_positioning") < step_ids.index("09_import_positioning")
    assert step_ids.index("08b_scan_disclosures") < step_ids.index("08_build_financial")
    assert step_ids.index("08_validate_financial") < step_ids.index("08a_audit_special_metrics")
    assert step_ids.index("08a_audit_special_metrics") < step_ids.index("08b_audit_disclosures")
    assert step_ids.index("08a_audit_special_metrics") < step_ids.index("12_sync_sec_ownership")
    assert step_ids.index("06a_build_scoring") < step_ids.index("06a_validate_scoring")
    assert step_ids.index("06a_validate_scoring") < step_ids.index("10_build_scores")
    positioning = next(step for step in steps if step.step_id == "13_sync_positioning")
    assert "--daily-refresh" not in positioning.args
    assert positioning.pass_db is False
    assert next(step for step in steps if step.step_id == "12_sync_sec_ownership").pass_db is False
    assert next(step for step in steps if step.step_id == "06a_validate_scoring").pass_db is True
    assert "--archive-bootstrap" in next(step for step in steps if step.step_id == "07_sync_sec").args
    disclosure_scan = next(step for step in steps if step.step_id == "08b_scan_disclosures")
    assert "--scan-cache" in disclosure_scan.args
    assert "--resume" in disclosure_scan.args
    disclosure_audit = next(step for step in steps if step.step_id == "08b_audit_disclosures")
    assert "--scan-cache" not in disclosure_audit.args
    portfolio_validation = next(step for step in steps if step.step_id == "20_validate_portfolio")
    assert "--expect-research-eligible" in portfolio_validation.args

    historical_steps = build_steps(
        ASOF,
        force=True,
        include_norgate_backfill=True,
        refresh_sec_insider=True,
        full_positioning_refresh=True,
        bootstrap_sec_archives=True,
        include_historical_backfill=True,
        history_start_date="2019-01-02",
        history_frequency="daily",
    )
    historical_ids = [step.step_id for step in historical_steps]
    assert historical_ids.index("10_build_scores") < historical_ids.index("18_backfill_history")
    assert historical_ids.index("18_backfill_history") < historical_ids.index("10b_publish")
    historical_step = next(step for step in historical_steps if step.step_id == "18_backfill_history")
    assert historical_step.pass_db is True
    assert historical_step.args == [
        "--start-date",
        "2019-01-02",
        "--end-date",
        ASOF,
        "--frequency",
        "daily",
        "--exclude-end-date",
        "--rebuild-features",
        "--force",
    ]

    daily_steps = build_steps(
        ASOF,
        force=False,
        include_norgate_backfill=False,
        refresh_sec_insider=False,
        full_positioning_refresh=False,
        bootstrap_sec_archives=False,
    )
    assert "12_sync_sec_ownership" not in {step.step_id for step in daily_steps}
    daily_positioning = next(step for step in daily_steps if step.step_id == "13_sync_positioning")
    assert "--daily-refresh" in daily_positioning.args

    select_steps = namespace["select_steps"]
    with pytest.raises(ValueError, match="Unknown --skip-step"):
        select_steps(
            daily_steps,
            SimpleNamespace(
                from_step="",
                to_step="",
                only="",
                skip_step=["not_a_step"],
                skip_network=False,
            ),
        )
    with pytest.raises(ValueError, match="occurs after"):
        select_steps(
            daily_steps,
            SimpleNamespace(
                from_step="10b_publish",
                to_step="00_validate_seed",
                only="",
                skip_step=[],
                skip_network=False,
            ),
        )
    with pytest.raises(ValueError, match="selection is empty"):
        select_steps(
            daily_steps,
            SimpleNamespace(
                from_step="",
                to_step="",
                only=",".join(step.step_id for step in daily_steps if step.network),
                skip_step=[],
                skip_network=True,
            ),
        )


def test_disclosure_scan_priorities_use_latest_known_availability_snapshot() -> None:
    namespace = runpy.run_path(
        str(PROJECT_ROOT / "industrials" / "machinery" / "scripts" / "08b_audit_machinery_disclosure_candidates.py")
    )
    ticker_priorities = namespace["ticker_priorities"]
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE feature_financial_metric_availability(
            ticker TEXT NOT NULL,
            asof_date TEXT NOT NULL,
            model_family TEXT NOT NULL,
            metric_name TEXT NOT NULL,
            availability_status TEXT NOT NULL
        )
        """
    )
    connection.executemany(
        """
        INSERT INTO feature_financial_metric_availability(
            ticker, asof_date, model_family, metric_name, availability_status
        ) VALUES (?, ?, 'machinery', 'orders', ?)
        """,
        [
            ("CAT", "2026-07-20", "REPORTED"),
            ("DE", "2026-07-20", "NOT_DISCLOSED"),
            ("CAT", "2026-07-23", "NOT_DISCLOSED"),
            ("DE", "2026-07-23", "REPORTED"),
        ],
    )
    priorities = ticker_priorities(
        connection,
        asof="2026-07-22",
        members={"CAT": {}, "DE": {}},
    )
    assert [row["ticker"] for row in priorities] == ["CAT", "DE"]
    assert priorities[0]["covered_metric_count"] == 1
    assert priorities[1]["missing_metric_count"] == 1


def test_positioning_upstream_pins_nested_import_to_requested_family(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = runpy.run_path(
        str(PROJECT_ROOT / "industrials" / "scripts" / "13_sync_industrials_positioning_upstream.py")
    )
    captured: list[tuple[list[str], str | None, bool]] = []

    def fake_run(
        command: list[str],
        *,
        cwd: str | None = None,
        check: bool = False,
    ) -> None:
        captured.append((command, cwd, check))

    monkeypatch.setattr(subprocess, "run", fake_run)
    namespace["run_industrials_import"](
        PROJECT_ROOT / "industrials" / "machinery" / "config.yaml",
        model_family="machinery",
        asof=date(2026, 7, 21),
    )

    assert len(captured) == 1
    command, cwd, check = captured[0]
    assert command[command.index("--model-family") + 1] == "machinery"
    assert command[command.index("--asof") + 1] == "2026-07-21"
    assert cwd == str(PROJECT_ROOT)
    assert check is True


def test_machinery_current_refresh_rejects_regressive_asof(
    tmp_path: Path,
) -> None:
    namespace = runpy.run_path(
        str(PROJECT_ROOT / "industrials" / "machinery" / "scripts" / "17_run_machinery_refresh_pipeline.py")
    )
    db_path = tmp_path / "industrials.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE feature_financial_metric_availability(
                model_family TEXT NOT NULL,
                asof_date TEXT NOT NULL
            )
            """
        )
        conn.execute("INSERT INTO feature_financial_metric_availability VALUES ('machinery', '2026-07-21')")

    committed = namespace["latest_committed_asof"](
        db_path=db_path,
        dashboard_root=tmp_path / "dashboard",
        orchestration_root=tmp_path / "orchestration",
    )
    assert committed == "2026-07-21"
    with pytest.raises(ValueError, match="Refusing regressive"):
        namespace["validate_non_regressive_asof"](
            requested_asof="2026-07-20",
            committed_asof=committed,
        )
    namespace["validate_non_regressive_asof"](
        requested_asof="2026-07-22",
        committed_asof=committed,
    )


def test_machinery_failed_or_dry_run_does_not_replace_last_success(
    tmp_path: Path,
) -> None:
    namespace = runpy.run_path(
        str(PROJECT_ROOT / "industrials" / "machinery" / "scripts" / "17_run_machinery_refresh_pipeline.py")
    )
    root = tmp_path / "orchestration"
    root.mkdir()
    success_path = root / "machinery_refresh_manifest.json"
    success_path.write_text('{"asof_date":"2026-07-21","acceptance":"PASS"}\n')
    report_rows = [
        {
            "run_id": "dry",
            "step_number": 1,
            "step_id": "step",
            "stage": "stage",
            "script": "script.py",
            "network_flag": 0,
            "command": "python script.py",
            "log_path": "log",
            "status": "DRY_RUN",
            "return_code": "",
            "elapsed_sec": 0.0,
        }
    ]
    namespace["persist_orchestration_result"](
        orchestration_root=root,
        run_id="dry",
        asof="2026-07-22",
        db_path=tmp_path / "db.sqlite",
        config_path=tmp_path / "config.yaml",
        dry_run=True,
        latest_before_run="",
        planned_step_count=1,
        report_rows=report_rows,
        failures=[],
    )
    assert json.loads(success_path.read_text())["asof_date"] == "2026-07-21"
    assert (root / "runs" / "dry_manifest.json").exists()
    assert (root / "machinery_refresh_last_attempt.json").exists()


def test_historical_reuse_requires_current_build_signature(
    tmp_path: Path,
) -> None:
    namespace = runpy.run_path(
        str(
            PROJECT_ROOT
            / "industrials"
            / "machinery"
            / "scripts"
            / "18_backfill_machinery_historical_dashboard_reports.py"
        )
    )
    config = load_yaml(PROJECT_ROOT / "industrials" / "machinery" / "config.yaml")
    metadata = historical_build_metadata(
        config,
        policy_lock_date="2026-07-09",
        required_metrics=required_metric_names(),
    )
    builder_path = (
        PROJECT_ROOT
        / 'industrials'
        / 'scripts'
        / '08_build_industrials_financial_features.py'
    )
    assert metadata['semantic_source_sha256'][
        '08_build_industrials_financial_features.py'
    ] == hashlib.sha256(builder_path.read_bytes()).hexdigest()
    output_dir = tmp_path / "2026-07-21"
    output_dir.mkdir()

    with pytest.raises(ValueError, match="lacks build metadata"):
        namespace["validate_historical_build_metadata"](
            output_dir=output_dir,
            asof="2026-07-21",
            expected_build_signature=metadata["historical_build_signature"],
        )

    stale = {
        **metadata,
        "asof_date": "2026-07-21",
        "historical_build_signature": "stale",
    }
    (output_dir / HISTORICAL_BUILD_METADATA_FILENAME).write_text(json.dumps(stale))
    with pytest.raises(ValueError, match="signature is stale"):
        namespace["validate_historical_build_metadata"](
            output_dir=output_dir,
            asof="2026-07-21",
            expected_build_signature=metadata["historical_build_signature"],
        )

    current = {**metadata, "asof_date": "2026-07-21"}
    (output_dir / HISTORICAL_BUILD_METADATA_FILENAME).write_text(json.dumps(current))
    namespace["validate_historical_build_metadata"](
        output_dir=output_dir,
        asof="2026-07-21",
        expected_build_signature=metadata["historical_build_signature"],
    )


def test_historical_stage_reports_use_isolated_temporary_workspace(
    tmp_path: Path,
) -> None:
    namespace = runpy.run_path(
        str(
            PROJECT_ROOT
            / "industrials"
            / "machinery"
            / "scripts"
            / "18_backfill_machinery_historical_dashboard_reports.py"
        )
    )
    workspace = namespace["stage_report_workspace"]
    with workspace(
        report_root=tmp_path,
        asof="2026-07-21",
        retain_stage_reports=False,
    ) as scratch:
        scratch_path = Path(scratch)
        assert scratch_path.exists()
        assert tmp_path not in scratch_path.parents
        (scratch_path / "stages.log").write_text("complete", encoding="utf-8")
    assert not scratch_path.exists()

    with workspace(
        report_root=tmp_path,
        asof="2026-07-21",
        retain_stage_reports=True,
    ) as retained:
        retained_path = Path(retained)
        assert retained_path == tmp_path / "stage_reports" / "2026-07-21"


def test_machinery_disclosure_scan_honors_acceptance_date_lower_bound() -> None:
    namespace = runpy.run_path(
        str(PROJECT_ROOT / "industrials" / "machinery" / "scripts" / "08b_audit_machinery_disclosure_candidates.py")
    )
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE fact_sec_filing(
            ticker TEXT,
            source_id TEXT,
            accession_number TEXT,
            form_type TEXT,
            filing_date TEXT,
            accepted_at TEXT,
            report_date TEXT,
            fiscal_year INTEGER,
            fiscal_period TEXT,
            primary_document TEXT
        )
        """
    )
    conn.executemany(
        "INSERT INTO fact_sec_filing VALUES ('CAT', 'sec', ?, '10-K', ?, ?, ?, 2018, 'FY', 'cat.htm')",
        [
            ("old", "2017-12-31", "2017-12-31T12:00:00Z", "2017-12-31"),
            ("boundary", "2018-01-01", "2018-01-01T12:00:00Z", "2017-12-31"),
            ("current", "2026-02-01", "2026-02-01T12:00:00Z", "2025-12-31"),
        ],
    )
    rows = namespace["accepted_filing_rows"](
        conn,
        ticker="CAT",
        asof="2026-07-20",
        source_id="sec",
        max_filings=0,
        scan_start_date="2018-01-01",
    )
    assert [row["accession_number"] for row in rows] == ["current", "boundary"]
    namespace["ensure_scan_ledger_schema"](conn)
    conn.execute(
        """
        INSERT INTO fact_machinery_disclosure_cache_scan VALUES
        ('CAT', '2026-07-20', '2018-01-01', 0, ?, 2, 2, 1, 1, 1, 'now')
        """,
        (namespace["DISCLOSURE_PARSER_VERSION"],),
    )
    assert namespace["completed_scan_tickers"](
        conn,
        asof="2026-07-20",
        scan_start_date="2018-01-01",
        max_filings_per_ticker=0,
    ) == {"CAT"}
    assert (
        namespace["completed_scan_tickers"](
            conn,
            asof="2026-07-20",
            scan_start_date="2019-01-01",
            max_filings_per_ticker=0,
        )
        == set()
    )
    conn.close()


def test_machinery_special_metric_schema_and_text_labels(tmp_path: Path) -> None:
    db_path = load_machinery_db(tmp_path)
    required_columns = {
        "orders_yoy_growth",
        "book_to_bill",
        "backlog_yoy_growth",
        "backlog_to_revenue",
        "reported_backlog_yoy_growth",
        "reported_backlog_to_revenue",
        "rpo_yoy_growth",
        "rpo_to_revenue",
        "rpo_implied_orders",
        "rpo_implied_book_to_bill",
        "contract_load_proxy",
        "contract_load_proxy_usd",
        "contract_load_proxy_source",
        "contract_load_proxy_yoy_growth",
        "contract_load_proxy_to_revenue",
        "financial_metric_classified_fraction",
        "roic",
        "roic_not_meaningful_flag",
        "asset_turnover",
        "incremental_operating_margin",
        "inventory_sales_growth_spread",
        "cash_conversion_cycle_change",
        "net_debt_to_ebitda",
        "negative_ebitda_leverage_flag",
        "negative_profit_valuation_flag",
        "interest_coverage",
        "cash_runway_years",
        "capital_raise_dependence",
        "diluted_shares_yoy_growth",
    }
    with connect(db_path) as conn:
        columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(feature_financial_statement)")}
        financing_fallbacks = {
            (str(row["taxonomy"]), str(row["concept_name"]), str(row["canonical_metric"]))
            for row in conn.execute(
                """
                SELECT taxonomy, concept_name, canonical_metric
                FROM dim_xbrl_concept_map
                WHERE concept_name IN (
                    'ProceedsFromWarrantExercises',
                    'ProceedsFromLinesOfCredit',
                    'ProceedsFromExerciseOfOptions',
                    'ProceedsFromNoncurrentBorrowings',
                    'ProceedsFromIssuanceOfSharesUnderIncentiveAndShareBasedCompensationPlansIncludingStockOptions',
                    'ProceedsFromIssuanceOfMandatoryRedeemableCapitalSecurities'
                )
                """
            )
        }
    assert required_columns.issubset(columns)
    assert financing_fallbacks == {
        ("ifrs-full", "ProceedsFromExerciseOfOptions", "equity_issuance_proceeds"),
        ("ifrs-full", "ProceedsFromNoncurrentBorrowings", "debt_issuance_proceeds"),
        ("us-gaap", "ProceedsFromLinesOfCredit", "debt_issuance_proceeds"),
        ("us-gaap", "ProceedsFromWarrantExercises", "equity_issuance_proceeds"),
        (
            "us-gaap",
            "ProceedsFromIssuanceOfSharesUnderIncentiveAndShareBasedCompensationPlansIncludingStockOptions",
            "equity_issuance_proceeds",
        ),
        (
            "us-gaap",
            "ProceedsFromIssuanceOfMandatoryRedeemableCapitalSecurities",
            "debt_issuance_proceeds",
        ),
    }

    namespace = runpy.run_path(
        str(PROJECT_ROOT / "industrials" / "scripts" / "07_sync_industrials_sec_fundamentals.py")
    )
    label_concept = namespace["text_table_label_concept"]
    assert namespace["document_default_scale_info"](
        "<p>PART II</p><p>(Dollar amounts in thousands except per share data)</p>"
    ) == (1_000.0, "document_default:dollar amounts in thousands", "high")
    assert label_concept("New orders received") == ("Orders", "duration")
    assert label_concept("Total orders") == ("Orders", "duration")
    assert label_concept("Order intake") == ("Orders", "duration")
    assert label_concept("Funded backlog") == ("FundedBacklog", "instant")
    assert label_concept("Authorized backlog") == ("FundedBacklog", "instant")
    assert label_concept("Firm backlog") == ("ReportedBacklog", "instant")
    assert label_concept("Backlog") == ("ReportedBacklog", "instant")
    assert label_concept("Remaining performance obligation") == (
        "RemainingPerformanceObligation",
        "instant",
    )
    classify_footnote = namespace["classify_machinery_footnote_concept"]
    assert classify_footnote(
        "ExpectedRemainingPerformanceObligationsRecognizedRemainderThereafter",
        labels=["Amount of expected remaining performance obligations recognized remainder thereafter."],
        period_type="instant",
    ) == ""
    assert namespace["rpo_total_amount_from_text"](
        "As of March 31, 2026, the total RPO amounted to $96.8 million. "
        "The company expects to recognize $69.6 million during the next 12 months "
        "and the remaining $13.8 million thereafter."
    ) == 96_800_000.0
    parse_tables = namespace["parse_archive_text_table_facts"]
    family_concept_map: dict[tuple[str, str], list[dict[str, object]]] = {}
    namespace["add_family_concept_mappings"](family_concept_map, model_family="defense")
    assert family_concept_map == {}
    namespace["add_family_concept_mappings"](family_concept_map, model_family="machinery")
    assert ("us-gaap", "PaymentsToAcquireProductiveAssets") in family_concept_map
    assert ("us-gaap", "ReceivablesNetCurrent") in family_concept_map
    assert ("us-gaap", "AccountsPayableTradeCurrent") in family_concept_map
    assert ("us-gaap", "LongTermDebt") in family_concept_map
    assert ("us-gaap", "PaymentsToAcquireOtherPropertyPlantAndEquipment") in family_concept_map
    assert ("us-gaap", "ProceedsFromIssuanceOfPrivatePlacement") in family_concept_map
    assert ("us-gaap", "ProceedsFromDebtMaturingInMoreThanThreeMonths") in family_concept_map
    archive_candidates = namespace["archive_document_candidates"]
    assert archive_candidates(
        {
            "directory": {
                "item": [
                    {"name": "issuer-2025x10k.htm"},
                    {"name": "issuer-2025.xml"},
                    {"name": "exhibit99-1.htm"},
                ]
            }
        },
        primary_document="issuer-2025x10k.htm",
        max_documents=3,
        text_tables_only=True,
    ) == ["issuer-2025x10k.htm", "exhibit99-1.htm"]
    assert archive_candidates(
        {
            "directory": {
                "item": [
                    {"name": "issuer-cover.htm"},
                    {"name": "issuer-quarter.htm"},
                    {"name": "issuer-quarter.xsd"},
                    {"name": "R1.htm"},
                ]
            }
        },
        primary_document="issuer-cover.htm",
        max_documents=0,
        text_tables_only=True,
        machinery_targeted=True,
        event_filing=True,
    ) == ["issuer-cover.htm", "issuer-quarter.htm"]
    should_stop = namespace["should_stop_archive_document_scan"]
    assert not should_stop(
        model_family="machinery",
        form_type="6-K",
        mapped_estimate=1,
        special_metric_count=0,
        parse_all_documents=False,
    )
    assert archive_candidates(
        {
            "directory": {
                "item": [
                    {"name": "issuer-8k.htm"},
                    {"name": "ex99-release.htm"},
                    {"name": "ex99-presentation.pdf"},
                ]
            }
        },
        primary_document="issuer-8k.htm",
        max_documents=0,
        include_pdf=True,
    ) == ["issuer-8k.htm", "ex99-presentation.pdf", "ex99-release.htm"]
    filing_summary_report_documents = namespace["filing_summary_report_documents"]
    summary_reports = filing_summary_report_documents(
        """
        <FilingSummary><MyReports>
          <Report><ShortName>Revenue from Contracts with Customers (Details)</ShortName>
            <HtmlFileName>R17.htm</HtmlFileName></Report>
          <Report><ShortName>Income Taxes (Details)</ShortName>
            <HtmlFileName>R42.htm</HtmlFileName></Report>
        </MyReports></FilingSummary>
        """
    )
    assert summary_reports == {"R17.htm"}
    assert archive_candidates(
        {
            "directory": {
                "item": [
                    {"name": "issuer-2025x10k.htm"},
                    {"name": "issuer-2025_htm.xml"},
                    {"name": "R17.htm"},
                    {"name": "R42.htm"},
                    {"name": "exhibit31.htm"},
                    {"name": "full-report.pdf"},
                ]
            }
        },
        primary_document="issuer-2025x10k.htm",
        max_documents=0,
        include_pdf=False,
        targeted_report_documents=summary_reports,
        machinery_targeted=True,
    ) == ["issuer-2025x10k.htm", "issuer-2025_htm.xml", "R17.htm"]
    facts = parse_tables(
        """
        <p>Orders and backlog (in millions)</p>
        <table>
          <tr><th>Years ended December 31</th></tr>
          <tr><th></th><th>2025</th><th>2024</th></tr>
          <tr><td>New orders received</td><td>1,200</td><td>1,000</td></tr>
          <tr><td>Funded backlog</td><td>2,400</td><td>2,000</td></tr>
        </table>
        """,
        document_name="annual-report.htm",
        filing={"report_date": "2025-12-31", "filing_date": "2026-02-15", "form_type": "10-K"},
        company_currency="USD",
    )
    assert {fact.concept_name for fact in facts} == {"Orders", "FundedBacklog"}
    assert {fact.value for fact in facts} == {1_000_000_000.0, 1_200_000_000.0, 2_000_000_000.0, 2_400_000_000.0}

    conflicting_backlog_row = parse_tables(
        """
        <p>Intangible assets (in millions)</p>
        <table>
          <tr><th></th><th>March 31, 2026</th></tr>
          <tr><td>Backlog</td><td><ix:nonFraction
            name="us-gaap:IntangibleAssetsGrossExcludingGoodwill"
            contextRef="current" unitRef="USD">54.6</ix:nonFraction></td></tr>
        </table>
        """,
        document_name="intangible-assets.htm",
        filing={"report_date": "2026-03-31", "filing_date": "2026-05-01", "form_type": "10-Q"},
        company_currency="USD",
    )
    assert conflicting_backlog_row == []

    total_order_facts = parse_tables(
        """
        <p>Key financial measures (dollars in thousands)</p>
        <table>
          <tr><th>Three months ended March 31</th></tr>
          <tr><th></th><th>2026</th><th>2025</th></tr>
          <tr><td>Net revenues</td><td>34,057</td><td>36,838</td></tr>
          <tr><td>Net loss</td><td>(495)</td><td>(370)</td></tr>
          <tr><td>Adjusted EBITDA</td><td>2,209</td><td>2,368</td></tr>
          <tr><td>Capital expenditures</td><td>2,778</td><td>916</td></tr>
          <tr><td>Free cash flow</td><td>(1,430)</td><td>(8,100)</td></tr>
          <tr><td>Operating working capital</td><td>38,746</td><td>28,839</td></tr>
          <tr><td>Total debt</td><td>10,753</td><td>12,191</td></tr>
          <tr><td>Total orders</td><td>37,422</td><td>30,455</td></tr>
        </table>
        <p>Heavy Fabrications Segment</p>
        <table>
          <tr><th>Three months ended March 31</th></tr>
          <tr><th></th><th>2026</th><th>2025</th></tr>
          <tr><td>Orders</td><td>9,667</td><td>12,391</td></tr>
        </table>
        """,
        document_name="quarterly-report.htm",
        filing={"report_date": "2026-03-31", "filing_date": "2026-05-01", "form_type": "10-Q"},
        company_currency="USD",
    )
    assert {(fact.period_end, fact.value) for fact in total_order_facts if fact.concept_name == "Orders"} == {
        ("2025-03-31", 30_455_000.0),
        ("2026-03-31", 37_422_000.0),
    }

    mixed_column_facts = parse_tables(
        """
        <p>Orders by market (dollars in thousands)</p>
        <table>
          <tr><th>Year Ended</th></tr>
          <tr><th>March 31,</th><th>Change</th></tr>
          <tr><th>Market</th><th>2026</th><th>%</th><th>2025</th><th>%</th><th>$</th><th>%</th></tr>
          <tr><td>Total orders</td><td>$</td><td>359,442</td><td>100</td><td>%</td><td>$</td><td>231,112</td><td>100</td><td>%</td><td>$</td><td>128,330</td><td>56</td><td>%</td></tr>
        </table>
        """,
        document_name="orders-by-market.htm",
        filing={"report_date": "2026-03-31", "filing_date": "2026-05-01", "form_type": "10-K"},
        company_currency="USD",
    )
    assert {
        (fact.period_start, fact.period_end, fact.value) for fact in mixed_column_facts if fact.concept_name == "Orders"
    } == {
        ("2025-04-01", "2026-03-31", 359_442_000.0),
        ("2024-04-01", "2025-03-31", 231_112_000.0),
    }

    quarter_heading_facts = parse_tables(
        """
        <p>Unaudited; in millions</p>
        <table>
          <tr><th>For the Three Month Period Ended March 31,</th></tr>
          <tr><th></th><th>2026</th><th>2025</th></tr>
          <tr><td>Total Orders</td><td>$</td><td>1,978.0</td><td>$</td><td>1,882.3</td></tr>
        </table>
        """,
        document_name="earnings-exhibit.htm",
        filing={"report_date": "2026-03-31", "filing_date": "2026-05-01", "form_type": "8-K"},
        company_currency="USD",
    )
    assert {(fact.period_end, fact.value) for fact in quarter_heading_facts if fact.concept_name == "Orders"} == {
        ("2025-03-31", 1_882_300_000.0),
        ("2026-03-31", 1_978_000_000.0),
    }
    assert all(
        80 <= (date.fromisoformat(fact.period_end) - date.fromisoformat(fact.period_start)).days <= 100
        for fact in quarter_heading_facts
        if fact.concept_name == "Orders"
    )

    consolidated_dimension_facts = parse_tables(
        """
        <p>Backlog roll-forward (in millions)</p>
        <table>
          <tr><th>In millions</th><th>Freight Segment</th><th>Transit Segment</th><th>Consolidated</th></tr>
          <tr><td>Balance at December 31, 2024</td><td>$</td><td>17,986</td><td>$</td><td>4,286</td><td>$</td><td>22,272</td></tr>
          <tr><td>Less: 2025 Net sales</td><td>(8,036)</td><td>(3,131)</td><td>(11,167)</td></tr>
          <tr><td>New orders</td><td>11,911</td><td>3,587</td><td>15,498</td></tr>
          <tr><td>Balance at December 31, 2025</td><td>$</td><td>22,493</td><td>$</td><td>4,914</td><td>$</td><td>27,407</td></tr>
        </table>
        """,
        document_name="backlog-rollforward.htm",
        filing={"report_date": "2025-12-31", "filing_date": "2026-02-15", "form_type": "10-K"},
        company_currency="USD",
    )
    assert [
        (fact.period_start, fact.period_end, fact.value)
        for fact in consolidated_dimension_facts
        if fact.concept_name == "Orders"
    ] == [("2025-01-01", "2025-12-31", 15_498_000_000.0)]

    should_stop = namespace["should_stop_archive_document_scan"]
    assert not should_stop(
        model_family="machinery",
        form_type="8-K",
        mapped_estimate=5,
        special_metric_count=0,
        parse_all_documents=False,
    )
    assert not should_stop(
        model_family="machinery",
        form_type="8-K",
        mapped_estimate=5,
        special_metric_count=1,
        parse_all_documents=False,
    )
    assert should_stop(
        model_family="defense",
        form_type="8-K",
        mapped_estimate=5,
        special_metric_count=0,
        parse_all_documents=False,
    )

    financial_namespace = runpy.run_path(
        str(PROJECT_ROOT / "industrials" / "scripts" / "08_build_industrials_financial_features.py")
    )
    select_previous_comparable = financial_namespace["select_previous_comparable"]
    current_instant = {
        "canonical_metric": "assets",
        "value": 120.0,
        "period_start": "",
        "period_end": "2025-12-31",
        "filing_date": "2026-02-15",
        "source_priority": 100,
    }
    prior_instant = {
        "canonical_metric": "assets",
        "value": 100.0,
        "period_start": "",
        "period_end": "2024-12-31",
        "filing_date": "2025-02-15",
        "source_priority": 100,
    }
    assert (
        select_previous_comparable(
            [current_instant, prior_instant],
            "assets",
            current_instant,
            instant_metric=True,
        )
        is prior_instant
    )
    assert (
        select_previous_comparable(
            [current_instant, prior_instant],
            "assets",
            current_instant,
        )
        is None
    )

    positioning_namespace = runpy.run_path(
        str(PROJECT_ROOT / "industrials" / "scripts" / "13_sync_industrials_positioning_upstream.py")
    )
    source_path = tmp_path / "positioning_universe.csv"
    write_csv(
        source_path,
        [
            {"ticker": "CAT", "listing_status": "active", "membership_end_date": ""},
            {"ticker": "PRCP", "listing_status": "historical_delisted", "membership_end_date": "2020-12-18"},
        ],
    )
    borrow_path = positioning_namespace["build_active_borrow_universe_csv"](
        source_path,
        output_path=tmp_path / "borrow_universe.csv",
        asof=date.fromisoformat(ASOF),
    )
    assert [row["ticker"] for row in read_rows(borrow_path)] == ["CAT"]


def test_machinery_metric_gate_modes_are_explicit() -> None:
    namespace = runpy.run_path(
        str(PROJECT_ROOT / "industrials" / "machinery" / "scripts" / "08a_audit_machinery_financial_metrics.py")
    )
    gates = {gate.metric: gate for gate in namespace["METRIC_GATES"]}
    gate_status = namespace["gate_status"]
    for metric in ("funded_backlog", "backlog_yoy_growth", "backlog_to_revenue"):
        gate = gates[metric]
        assert gate.gate_mode == "limited_universe_diagnostic"
        assert gate.minimum_count == 1
        assert gate_status(gate, implemented=True, ready=False) == "LIMITED_UNIVERSE_PENDING_COVERAGE"
        assert gate_status(gate, implemented=True, ready=True) == "LIMITED_UNIVERSE_READY"
    assert gates["orders"].gate_mode == "calibration"
    assert gate_status(gates["orders"], implemented=True, ready=True) == "CALIBRATION_READY"


def test_machinery_complete_metric_audit_summarizes_each_ticker() -> None:
    namespace = runpy.run_path(
        str(PROJECT_ROOT / "industrials" / "machinery" / "scripts" / "08a_audit_machinery_financial_metrics.py")
    )
    rows = namespace["build_ticker_coverage_rows"](
        asof="2026-07-24",
        members={"CAT": "heavy_machinery"},
        observation_rows=[
            {
                "ticker": "CAT",
                "metric": "orders",
                "availability_status": "REPORTED",
                "applicable_flag": 1,
                "covered_flag": 1,
            },
            {
                "ticker": "CAT",
                "metric": "book_to_bill",
                "availability_status": "NOT_DISCLOSED",
                "applicable_flag": 1,
                "covered_flag": 0,
            },
            {
                "ticker": "CAT",
                "metric": "funded_backlog",
                "availability_status": "NOT_APPLICABLE",
                "applicable_flag": 0,
                "covered_flag": 0,
            },
        ],
    )
    assert rows == [
        {
            "asof_date": "2026-07-24",
            "ticker": "CAT",
            "calibration_cohort": "heavy_machinery",
            "required_metric_count": 3,
            "applicable_metric_count": 2,
            "covered_metric_count": 1,
            "missing_applicable_metric_count": 1,
            "excluded_metric_count": 1,
            "coverage_fraction": "0.500000",
            "reported_count": 1,
            "proxy_count": 0,
            "exempt_count": 0,
            "not_applicable_count": 1,
            "not_disclosed_count": 1,
            "disclosed_unparsed_count": 0,
            "parser_failure_count": 0,
            "missing_metrics": "book_to_bill",
        }
    ]


def test_gtls_is_retained_as_a_historical_member_after_acquisition() -> None:
    data_root = PROJECT_ROOT / "industrials" / "machinery" / "system_csvs"
    active = {row["ticker"] for row in read_rows(data_root / "machinery_tickers.csv")}
    delisted = {row["ticker"]: row for row in read_rows(data_root / "machinery_delisted.csv")}
    membership = {row["internal_ticker"]: row for row in read_rows(data_root / "machinery_historical_membership.csv")}
    listing_dates = {row["ticker"]: row for row in read_rows(data_root / "machinery_listing_dates.csv")}

    assert "GTLS" not in active
    assert delisted["GTLS"]["exit_year"] == "2026"
    assert membership["GTLS"]["membership_status"] == "historical_delisted"
    assert membership["GTLS"]["end_date"] == "2026-07-16"
    assert membership["GTLS"]["successor_ticker"] == "BKR"
    assert listing_dates["GTLS"]["last_eligible_date"] == "2026-07-16"


def test_historical_coverage_index_discovers_all_published_sidecars(tmp_path: Path) -> None:
    namespace = runpy.run_path(
        str(
            PROJECT_ROOT
            / "industrials"
            / "machinery"
            / "scripts"
            / "18_backfill_machinery_historical_dashboard_reports.py"
        )
    )
    for asof in ("2026-07-22", "2026-07-23", "2026-07-24"):
        output_dir = tmp_path / asof
        output_dir.mkdir()
        (output_dir / "machinery_stage11_survivorship_calibration_panel.csv").touch()
    (tmp_path / "notes").mkdir()
    (tmp_path / "2026-07-25").mkdir()

    assert namespace["published_dashboard_dates"](
        tmp_path,
        start_date="2026-07-23",
        end_date="2026-07-25",
    ) == ["2026-07-23", "2026-07-24"]


def test_historical_resume_rejects_membership_drift() -> None:
    namespace = runpy.run_path(
        str(
            PROJECT_ROOT
            / "industrials"
            / "machinery"
            / "scripts"
            / "18_backfill_machinery_historical_dashboard_reports.py"
        )
    )
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE dim_universe_membership (
            ticker TEXT,
            model_family TEXT,
            start_date TEXT,
            end_date TEXT,
            membership_status TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO dim_universe_membership
            (ticker, model_family, start_date, end_date, membership_status)
        VALUES ('CAT', 'machinery', '2019-01-02', NULL, 'active')
        """
    )

    namespace["validate_existing_membership"](
        conn,
        asof="2026-07-24",
        rows=[{"ticker": "CAT"}],
    )
    with pytest.raises(ValueError, match="extra=\\['GTLS'\\]"):
        namespace["validate_existing_membership"](
            conn,
            asof="2026-07-24",
            rows=[{"ticker": "CAT"}, {"ticker": "GTLS"}],
        )


def test_historical_membership_metadata_repair_is_scoped() -> None:
    namespace = runpy.run_path(
        str(
            PROJECT_ROOT
            / "industrials"
            / "machinery"
            / "scripts"
            / "18_backfill_machinery_historical_dashboard_reports.py"
        )
    )
    expected = {
        "CAT": {
            "membership_source_id": "history",
            "membership_basis": "survivorship_corrected_pit_contract",
            "membership_start_date": "2019-01-02",
            "membership_end_date": "",
            "membership_status": "active",
            "membership_confidence": "0.95",
        },
        "GTLS": {
            "membership_source_id": "history",
            "membership_basis": "survivorship_corrected_pit_contract",
            "membership_start_date": "2019-01-02",
            "membership_end_date": "2026-07-16",
            "membership_status": "historical_delisted",
            "membership_confidence": "0.95",
        },
    }
    rows = [
        {"ticker": "CAT", **expected["CAT"], "final_score": "80"},
        {
            "ticker": "GTLS",
            **expected["GTLS"],
            "membership_end_date": "",
            "membership_status": "active",
            "final_score": "70",
        },
    ]
    updated, changed = namespace["reconcile_membership_metadata"](
        rows,
        expected=expected,
    )
    assert changed == ["GTLS"]
    assert updated[0] == rows[0]
    assert updated[1]["membership_status"] == "historical_delisted"
    assert updated[1]["membership_end_date"] == "2026-07-16"
    assert updated[1]["final_score"] == "70"


def test_machinery_review_overrides_and_zero_revenue_guardrails() -> None:
    override_path = PROJECT_ROOT / "industrials" / "machinery" / "system_csvs" / "machinery_sec_reporting_overrides.csv"
    overrides = {row["ticker"]: row for row in read_rows(override_path)}
    assert {overrides[ticker]["handling_type"] for ticker in ("AIRJ", "FISN", "NNE", "OKLO")} == {
        "Is_Development_Stage"
    }
    assert {overrides[ticker]["handling_type"] for ticker in ("INIO", "MAIR")} == {"Ingestion_Gap_Pending"}
    assert all(overrides[ticker]["usable_xbrl_flag"] == "true" for ticker in ("AIRJ", "FISN", "NNE", "OKLO"))
    assert all(overrides[ticker]["usable_xbrl_flag"] == "false" for ticker in ("INIO", "MAIR"))
    assert all(overrides[ticker]["reporting_profile"] == "SEC_RAW_ARCHIVE_REQUIRED" for ticker in ("INIO", "MAIR"))

    sec_namespace = runpy.run_path(
        str(PROJECT_ROOT / "industrials" / "scripts" / "07_sync_industrials_sec_fundamentals.py")
    )
    load_overrides = sec_namespace["load_reporting_overrides"]
    assert "FISN" not in load_overrides(override_path, asof="2026-06-17")
    assert load_overrides(override_path, asof="2026-06-18")["FISN"].handling_type == "Is_Development_Stage"
    assert "MAIR" not in load_overrides(override_path, asof="2026-04-15")
    assert load_overrides(override_path, asof="2026-04-16")["MAIR"].handling_type == "Ingestion_Gap_Pending"
    assert sec_namespace["should_attempt_archive"](load_overrides(override_path, asof="2026-04-16")["MAIR"])

    financial_namespace = runpy.run_path(
        str(PROJECT_ROOT / "industrials" / "scripts" / "08_build_industrials_financial_features.py")
    )
    should_default = financial_namespace["should_default_missing_revenue_to_zero"]
    development_company = {"development_stage": "development_stage"}
    partial_profile = {"reporting_profile": "SEC_XBRL_US_GAAP_PARTIAL"}
    assert should_default(
        company=development_company,
        profile=partial_profile,
        operating_cash_flow=-1.0,
    )
    assert not should_default(
        company=development_company,
        profile=partial_profile,
        operating_cash_flow=0.0,
    )
    assert not should_default(
        company=development_company,
        profile=partial_profile,
        operating_cash_flow=None,
    )
    assert not should_default(
        company={"development_stage": "operating"},
        profile={"reporting_profile": "NO_FINANCIALS_REVIEW"},
        operating_cash_flow=-1.0,
    )

    listing_rows = {
        row["ticker"]: row
        for row in read_rows(PROJECT_ROOT / "industrials" / "machinery" / "system_csvs" / "machinery_listing_dates.csv")
    }
    assert listing_rows["FISN"]["first_eligible_date"] == "2026-06-18"
    assert listing_rows["MAIR"]["first_eligible_date"] == "2026-04-16"

    membership_rows = {
        row["internal_ticker"]: row
        for row in read_rows(
            PROJECT_ROOT / "industrials" / "machinery" / "system_csvs" / "machinery_historical_membership.csv"
        )
    }
    assert membership_rows["FISN"]["start_date"] == "2026-06-18"
    assert membership_rows["MAIR"]["start_date"] == "2026-04-16"


def test_machinery_inline_xbrl_footnote_and_custom_label_extraction() -> None:
    namespace = runpy.run_path(
        str(PROJECT_ROOT / "industrials" / "scripts" / "07_sync_industrials_sec_fundamentals.py")
    )
    labels = namespace["parse_xbrl_label_linkbase"](
        """
        <link:linkbase xmlns:link="http://www.xbrl.org/2003/linkbase"
                       xmlns:xlink="http://www.w3.org/1999/xlink">
          <link:labelLink>
            <link:loc xlink:label="loc_backlog" xlink:href="issuer.xsd#mach_OrderBacklog"/>
            <link:label xlink:label="lab_backlog">Total order backlog</link:label>
            <link:labelArc xlink:from="loc_backlog" xlink:to="lab_backlog"/>
            <link:loc xlink:label="loc_orders" xlink:href="issuer.xsd#mach_NewOrdersReceived"/>
            <link:label xlink:label="lab_orders">New orders received</link:label>
            <link:labelArc xlink:from="loc_orders" xlink:to="lab_orders"/>
            <link:loc xlink:label="loc_rpo" xlink:href="issuer.xsd#mach_ContractedRevenue"/>
            <link:label xlink:label="lab_rpo">Remaining performance obligations</link:label>
            <link:labelArc xlink:from="loc_rpo" xlink:to="lab_rpo"/>
          </link:labelLink>
        </link:linkbase>
        """
    )
    assert labels["OrderBacklog"] == ["Total order backlog"]
    facts = namespace["parse_machinery_footnote_facts"](
        """
        <html xmlns="http://www.w3.org/1999/xhtml"
              xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
              xmlns:xbrli="http://www.xbrl.org/2003/instance"
              xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
              xmlns:iso4217="http://www.xbrl.org/2003/iso4217"
              xmlns:mach="http://example.com/machinery/2026">
          <body>
            <xbrli:context id="instant"><xbrli:entity><xbrli:identifier scheme="cik">1</xbrli:identifier></xbrli:entity><xbrli:period><xbrli:instant>2026-03-31</xbrli:instant></xbrli:period></xbrli:context>
            <xbrli:context id="duration"><xbrli:entity><xbrli:identifier scheme="cik">1</xbrli:identifier></xbrli:entity><xbrli:period><xbrli:startDate>2025-04-01</xbrli:startDate><xbrli:endDate>2026-03-31</xbrli:endDate></xbrli:period></xbrli:context>
            <xbrli:context id="quarter"><xbrli:entity><xbrli:identifier scheme="cik">1</xbrli:identifier></xbrli:entity><xbrli:period><xbrli:startDate>2026-01-01</xbrli:startDate><xbrli:endDate>2026-03-31</xbrli:endDate></xbrli:period></xbrli:context>
            <xbrli:context id="consolidated-quarter"><xbrli:entity><xbrli:identifier scheme="cik">1</xbrli:identifier><xbrli:segment><xbrldi:explicitMember dimension="mach:StatementBusinessSegmentsAxis">mach:ConsolidatedMember</xbrldi:explicitMember></xbrli:segment></xbrli:entity><xbrli:period><xbrli:startDate>2025-10-01</xbrli:startDate><xbrli:endDate>2025-12-31</xbrli:endDate></xbrli:period></xbrli:context>
            <xbrli:context id="segment-quarter"><xbrli:entity><xbrli:identifier scheme="cik">1</xbrli:identifier><xbrli:segment><xbrldi:explicitMember dimension="mach:StatementBusinessSegmentsAxis">mach:HeavyFabricationsMember</xbrldi:explicitMember></xbrli:segment></xbrli:entity><xbrli:period><xbrli:startDate>2025-10-01</xbrli:startDate><xbrli:endDate>2025-12-31</xbrli:endDate></xbrli:period></xbrli:context>
            <xbrli:unit id="USD"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
            <p>The company applies the practical expedient and does not disclose remaining performance obligations for contracts of one year or less.</p>
            <ix:nonFraction name="mach:OrderBacklog" contextRef="instant" unitRef="USD" scale="6">500</ix:nonFraction>
            <ix:nonFraction name="mach:NewOrdersReceived" contextRef="duration" unitRef="USD" scale="6">900</ix:nonFraction>
            <ix:nonFraction name="mach:NewOrdersReceived" contextRef="quarter" unitRef="USD" scale="6">250</ix:nonFraction>
            <ix:nonFraction name="mach:Orders" contextRef="consolidated-quarter" unitRef="USD" scale="6">333</ix:nonFraction>
            <ix:nonFraction name="mach:Orders" contextRef="segment-quarter" unitRef="USD" scale="6">111</ix:nonFraction>
            <ix:nonFraction name="mach:ContractedRevenue" contextRef="instant" unitRef="USD" scale="6">300</ix:nonFraction>
          </body>
        </html>
        """,
        document_name="issuer-20260331.htm",
        filing={"report_date": "2026-03-31", "filing_date": "2026-05-01"},
        concept_labels=labels,
    )
    values = {fact.concept_name: fact.value for fact in facts}
    assert values["ReportedBacklog"] == 500_000_000.0
    assert values["RemainingPerformanceObligation"] == 300_000_000.0
    assert values["RPOPracticalExpedient"] == 1.0
    assert {(fact.period_start, fact.value) for fact in facts if fact.concept_name == "Orders"} == {
        ("2025-04-01", 900_000_000.0),
        ("2026-01-01", 250_000_000.0),
        ("2025-10-01", 333_000_000.0),
    }
    assert all(fact.source_detail == "sec_archive_footnote_xbrl" for fact in facts)


def test_machinery_prose_disclosure_candidates_are_scope_safe() -> None:
    from industrials.machinery.disclosure_candidates import (
        extract_machinery_prose_candidates,
    )

    facts = extract_machinery_prose_candidates(
        """
        <html><body>
          <h2>Consolidated Results</h2>
          <p>Backlog of $2.9 billion at March 31, 2026 increased from December 31, 2025.</p>
          <h2>Flowserve Pump Division</h2>
          <p>Backlog of $2.1 billion at March 31, 2026 increased by $31.0 million.</p>
          <h2>Flow Control Division</h2>
          <p>Backlog of $876.4 million at March 31, 2026 increased by $47.8 million.</p>
        </body></html>
        """,
        filing={"report_date": "2026-03-31", "filing_date": "2026-05-01"},
        company_currency="USD",
    )
    accepted = [item for item in facts if item.candidate_status == "ACCEPTED"]
    assert len(accepted) == 1
    assert accepted[0].concept_name == "ReportedBacklog"
    assert accepted[0].value == 2_900_000_000.0
    assert accepted[0].period_end == "2026-03-31"

    canadian = extract_machinery_prose_candidates(
        """
        <p>At March 31, 2026, total company backlog was $1,958 million.</p>
        <p>At December 31, 2025, total company backlog was US$1,500 million.</p>
        <p>At September 30, 2025, total company backlog was C$1,400 million.</p>
        <p>At June 30, 2025, total company backlog was EUR 1,300 million.</p>
        <p>At March 31, 2025, total company backlog was GBP 1,200 million.</p>
        """,
        filing={"report_date": "2026-03-31", "filing_date": "2026-05-01"},
        company_currency="CAD",
    )
    units_by_value = {item.value: item.unit for item in canadian}
    assert units_by_value[1_958_000_000.0] == "CAD"
    assert units_by_value[1_500_000_000.0] == "USD"
    assert units_by_value[1_400_000_000.0] == "CAD"
    assert units_by_value[1_300_000_000.0] == "EUR"
    assert units_by_value[1_200_000_000.0] == "GBP"

    suffix_scale = extract_machinery_prose_candidates(
        "<p>At March 31, 2026, total company backlog was $1.8B.</p>",
        filing={"report_date": "2026-03-31", "filing_date": "2026-05-01"},
        company_currency="USD",
    )
    assert [(item.value, item.unit) for item in suffix_scale] == [(1_800_000_000.0, "USD")]

    comparative = extract_machinery_prose_candidates(
        """
        <p>Total backlog was $4,712 million at March 31, 2026, as compared to
        December 31, 2025 backlog of $4,615 million.</p>
        """,
        filing={"report_date": "2026-03-31", "filing_date": "2026-05-01"},
        company_currency="USD",
    )
    assert {(item.period_end, item.value) for item in comparative} == {
        ("2026-03-31", 4_712_000_000.0),
        ("2025-12-31", 4_615_000_000.0),
    }

    ambiguous = extract_machinery_prose_candidates(
        """
        <html><body>
          <h2>Alpha Segment</h2><p>Backlog of $600 million at March 31, 2026.</p>
          <h2>Beta Segment</h2><p>Backlog of $400 million at March 31, 2026.</p>
        </body></html>
        """,
        filing={"report_date": "2026-03-31", "filing_date": "2026-05-01"},
        company_currency="USD",
    )
    assert ambiguous
    assert {item.candidate_status for item in ambiguous} == {"REVIEW_REQUIRED"}

    dated_orders = extract_machinery_prose_candidates(
        "<p>Orders totaled $800 million for the three months ended June 30, 2026.</p>",
        filing={"report_date": "2026-03-31", "filing_date": "2026-05-01"},
        company_currency="USD",
    )
    assert len(dated_orders) == 1
    assert dated_orders[0].candidate_status == "ACCEPTED"
    assert dated_orders[0].period_start == "2026-04-01"

    component_checked_orders = extract_machinery_prose_candidates(
        """
        <p>Total orders for the three months ended March 31, 2026 were $623 million.
        Environmental Solutions total orders for the three months ended March 31, 2026
        were $534 million. Safety Systems orders for the three months ended March 31,
        2026 were $89 million.</p>
        """,
        filing={"report_date": "2026-03-31", "filing_date": "2026-04-29"},
        company_currency="USD",
    )
    accepted_orders = [item for item in component_checked_orders if item.candidate_status == "ACCEPTED"]
    assert len(accepted_orders) == 1
    assert accepted_orders[0].value == 623_000_000.0
    assert accepted_orders[0].period_start == "2026-01-01"

    assert (
        extract_machinery_prose_candidates(
            "<p>For the year ended October 31, 2025, we executed three change orders totaling $5 million.</p>",
            filing={"report_date": "2025-10-31", "filing_date": "2025-12-18"},
            company_currency="USD",
        )
        == []
    )

    fcel_candidates = extract_machinery_prose_candidates(
        "<p>Overall, backlog increased by approximately 2.6% to $1.19 billion "
        "as of October 31, 2025, compared to $1.16 billion as of October 31, 2024.</p>",
        filing={"report_date": "2025-10-31", "filing_date": "2025-12-18"},
        company_currency="USD",
    )
    assert len(fcel_candidates) == 1
    assert fcel_candidates[0].value == 1_190_000_000.0
    assert fcel_candidates[0].period_end == "2025-10-31"
    assert fcel_candidates[0].scope == "consolidated"
    assert fcel_candidates[0].candidate_status == "ACCEPTED"


def test_machinery_prose_disclosure_comparatives_use_measurement_dates() -> None:
    from industrials.machinery.disclosure_candidates import (
        extract_machinery_prose_candidates,
    )

    def values(
        text: str,
        *,
        report_date: str,
    ) -> set[tuple[str, float]]:
        candidates = extract_machinery_prose_candidates(
            f"<p>{text}</p>",
            filing={"report_date": report_date, "filing_date": "2026-07-23"},
            company_currency="USD",
        )
        return {(item.period_end, item.value) for item in candidates}

    assert values(
        "Our product sales backlog was $1.0 billion as of December 31, 2020. "
        "Our product sales backlog was $1.1 billion as of December 31, 2019.",
        report_date="2020-12-31",
    ) == {
        ("2020-12-31", 1_000_000_000.0),
        ("2019-12-31", 1_100_000_000.0),
    }
    assert values(
        "Backlog totaled $670.6 million at December 31, 2018, an increase of "
        "11%, from the prior year ending backlog of $606.6 million.",
        report_date="2018-12-31",
    ) == {
        ("2018-12-31", 670_600_000.0),
        ("2017-12-31", 606_600_000.0),
    }
    assert values(
        "Total Company backlog as of June 30, 2020 was approximately $750 "
        "million compared to backlog of approximately $1.0 billion ending "
        "March 2020.",
        report_date="2020-06-30",
    ) == {
        ("2020-06-30", 750_000_000.0),
        ("2020-03-31", 1_000_000_000.0),
    }
    assert values(
        "At December 31, 2020, our backlog was approximately $39.4 million. "
        "At December 31, 2019, our backlog was approximately $31.1 million.",
        report_date="2020-12-31",
    ) == {
        ("2020-12-31", 39_400_000.0),
        ("2019-12-31", 31_100_000.0),
    }
    assert values(
        "At the end of 2022, Proterra Transit backlog was approximately $0.6 billion.",
        report_date="2023-03-15",
    ) == {("2022-12-31", 600_000_000.0)}


def test_machinery_prose_excludes_acquisition_backlog_fair_values() -> None:
    from industrials.machinery.disclosure_candidates import (
        extract_machinery_prose_candidates,
    )

    candidates = extract_machinery_prose_candidates(
        """
        <p>Identifiable intangible assets consisted of developed technology,
        non-compete agreements, backlog, tradename, and customer relationships.
        The fair value of the non-compete agreements and backlog was
        $3.9 million.</p>
        """,
        filing={"report_date": "2022-12-31", "filing_date": "2023-03-01"},
        company_currency="USD",
    )
    assert candidates == []


def test_reviewed_machinery_disclosure_semantics_are_deterministic() -> None:
    from industrials.machinery.disclosure_candidates import (
        DisclosureCandidate,
        resolve_machinery_disclosure_candidates,
    )

    def candidate(
        concept_name: str,
        value: float,
        *,
        period_end: str = "2026-03-31",
        period_start: str | None = None,
        scope: str = "unknown",
        status: str = "REVIEW_REQUIRED",
        evidence: str = "",
        block_index: int = 1,
    ) -> DisclosureCandidate:
        metric_name = {
            "Orders": "orders",
            "ReportedBacklog": "reported_backlog",
            "RemainingPerformanceObligation": "remaining_performance_obligation",
        }[concept_name]
        return DisclosureCandidate(
            concept_name=concept_name,
            metric_name=metric_name,
            value=value,
            unit="USD",
            period_start=(
                period_start if period_start is not None else "2026-01-01" if concept_name == "Orders" else ""
            ),
            period_end=period_end,
            scope=scope,
            confidence=0.85,
            candidate_status=status,
            status_reason="fixture",
            evidence_text=evidence,
            block_index=block_index,
        )

    wab = resolve_machinery_disclosure_candidates(
        [
            candidate(
                "RemainingPerformanceObligation",
                27_400_000_000.0,
                status="ACCEPTED",
                evidence="Remaining performance obligations were $27.4 billion",
            ),
            candidate(
                "ReportedBacklog",
                27_400_000_000.0,
                status="ACCEPTED",
                evidence="Total backlog was $27.4 billion",
                block_index=2,
            ),
            candidate(
                "ReportedBacklog",
                1_050_000_000.0,
                status="ACCEPTED",
                evidence="The 12-month backlog was $1.05 billion",
                block_index=3,
            ),
            candidate(
                "ReportedBacklog",
                3_400_000_000.0,
                scope="consolidated",
                status="ACCEPTED",
                evidence="The increase in backlog was $3.4 billion",
                block_index=4,
            ),
        ],
        ticker="WAB",
        filing={"form_type": "10-K"},
    )
    assert [item.concept_name for item in wab if item.candidate_status == "ACCEPTED"] == [
        "RemainingPerformanceObligation"
    ]
    assert sum(item.candidate_status == "SUPPRESSED_SEMANTIC_DUPLICATE" for item in wab) == 1
    assert sum(item.candidate_status == "REJECTED_POLICY" for item in wab) == 2

    crane = resolve_machinery_disclosure_candidates(
        [
            candidate("ReportedBacklog", 1_075_500_000.0, scope="segment"),
            candidate(
                "ReportedBacklog",
                359_900_000.0,
                scope="segment",
                block_index=2,
            ),
            candidate(
                "ReportedBacklog",
                58_000_000.0,
                evidence="Estimated fair value of backlog intangible assets",
                block_index=3,
            ),
        ],
        ticker="CR",
        filing={"form_type": "8-K"},
    )
    crane_total = [item for item in crane if item.candidate_status == "ACCEPTED"]
    assert len(crane_total) == 1
    assert crane_total[0].value == 1_435_400_000.0
    assert crane_total[0].scope == "consolidated"

    crane_direct = resolve_machinery_disclosure_candidates(
        [
            candidate(
                "ReportedBacklog",
                1_435_400_000.0,
                scope="segment",
                status="ACCEPTED",
                evidence="As of December 31, 2025, backlog was $1,435.4 million",
            ),
            candidate("ReportedBacklog", 1_075_500_000.0, scope="segment", block_index=2),
            candidate("ReportedBacklog", 359_900_000.0, scope="segment", block_index=3),
        ],
        ticker="CR",
        filing={"form_type": "10-K"},
    )
    assert [item.value for item in crane_direct if item.candidate_status == "ACCEPTED"] == [1_435_400_000.0]
    assert sum(item.candidate_status == "CONSUMED_BY_CONSOLIDATED_TOTAL" for item in crane_direct) == 2

    cir = resolve_machinery_disclosure_candidates(
        [
            candidate("ReportedBacklog", 157_200_000.0, period_end="2020-01-31"),
            candidate(
                "ReportedBacklog",
                191_100_000.0,
                period_end="2020-01-31",
                block_index=2,
            ),
            candidate(
                "ReportedBacklog",
                61_400_000.0,
                period_end="2020-01-31",
                block_index=3,
            ),
        ],
        ticker="CIR",
        filing={"form_type": "10-K"},
    )
    assert [item.value for item in cir if item.candidate_status == "ACCEPTED"] == [409_700_000.0]

    cxt = resolve_machinery_disclosure_candidates(
        [
            candidate(
                "ReportedBacklog",
                304_800_000.0,
                period_end="2020-09-30",
                scope="segment",
            )
        ],
        ticker="CXT",
        filing={"form_type": "10-Q", "filing_date": "2020-10-28"},
    )
    assert [item.candidate_status for item in cxt] == ["REJECTED_POLICY"]

    midd = resolve_machinery_disclosure_candidates(
        [
            candidate(
                "ReportedBacklog",
                260_400_000.0,
                scope="segment",
                evidence="Commercial Foodservice Equipment Group backlog",
            ),
            candidate(
                "ReportedBacklog",
                409_900_000.0,
                scope="segment",
                evidence="Food Processing Equipment Group backlog",
                block_index=2,
            ),
        ],
        ticker="MIDD",
        filing={"form_type": "10-K"},
    )
    midd_total = [item for item in midd if item.candidate_status == "ACCEPTED"]
    assert len(midd_total) == 1
    assert midd_total[0].value == 670_300_000.0
    assert midd_total[0].scope == "consolidated"

    midd_incomplete = resolve_machinery_disclosure_candidates(
        [candidate("ReportedBacklog", 260_400_000.0, scope="segment")],
        ticker="MIDD",
        filing={"form_type": "10-K"},
    )
    assert [item.candidate_status for item in midd_incomplete] == ["REJECTED_POLICY"]

    midd_direct = resolve_machinery_disclosure_candidates(
        [
            candidate(
                "ReportedBacklog",
                670_300_000.0,
                status="ACCEPTED",
                evidence="Total company backlog was $670.3 million",
            ),
            candidate("ReportedBacklog", 260_400_000.0, scope="segment", block_index=2),
            candidate("ReportedBacklog", 409_900_000.0, scope="segment", block_index=3),
        ],
        ticker="MIDD",
        filing={"form_type": "10-K"},
    )
    assert [item.value for item in midd_direct if item.candidate_status == "ACCEPTED"] == [670_300_000.0]
    assert sum(item.candidate_status == "CONSUMED_BY_CONSOLIDATED_TOTAL" for item in midd_direct) == 2

    fcel = resolve_machinery_disclosure_candidates(
        [
            candidate(
                "ReportedBacklog",
                1_190_000_000.0,
                status="ACCEPTED",
                scope="consolidated",
                period_end="2025-10-31",
                evidence="Overall, backlog increased to $1.19 billion",
            ),
            candidate(
                "ReportedBacklog",
                945_200_000.0,
                scope="segment",
                period_end="2025-10-31",
                evidence="Generation backlog totaled $945.2 million",
                block_index=2,
            ),
        ],
        ticker="FCEL",
        filing={"form_type": "10-K"},
    )
    assert [item.value for item in fcel if item.candidate_status == "ACCEPTED"] == [1_190_000_000.0]
    assert sum(item.candidate_status == "CONSUMED_BY_CONSOLIDATED_TOTAL" for item in fcel) == 1

    mcrn = resolve_machinery_disclosure_candidates(
        [
            candidate(
                "Orders",
                1_142_700_000.0,
                period_start="2018-01-01",
                period_end="2018-12-31",
            ),
            candidate(
                "Orders",
                1_116_400_000.0,
                period_start="2018-01-01",
                period_end="2018-12-31",
                block_index=2,
            ),
        ],
        ticker="MCRN",
        filing={"form_type": "8-K"},
    )
    assert [item.value for item in mcrn if item.candidate_status == "ACCEPTED"] == [1_142_700_000.0]
    assert sum(item.candidate_status == "REJECTED_POLICY" for item in mcrn) == 1

    mcrn_single = resolve_machinery_disclosure_candidates(
        [
            candidate(
                "Orders",
                900_000_000.0,
                status="ACCEPTED",
                scope="consolidated",
            )
        ],
        ticker="MCRN",
        filing={"form_type": "10-Q"},
    )
    assert [item.candidate_status for item in mcrn_single] == ["ACCEPTED"]

    leu = resolve_machinery_disclosure_candidates(
        [
            candidate(
                "RemainingPerformanceObligation",
                600_000_000.0,
                period_end="2025-12-31",
                scope="segment",
                evidence="RPO in the LEU segment",
            ),
            candidate(
                "RemainingPerformanceObligation",
                79_100_000.0,
                period_end="2025-12-31",
                scope="segment",
                evidence="RPO in the Technical Solutions segment",
                block_index=2,
            ),
            candidate(
                "ReportedBacklog",
                2_900_000_000.0,
                period_end="2025-12-31",
                evidence="Backlog included contingent options",
                block_index=3,
            ),
        ],
        ticker="LEU",
        filing={"form_type": "10-K"},
    )
    leu_total = [item for item in leu if item.candidate_status == "ACCEPTED"]
    assert len(leu_total) == 1
    assert leu_total[0].value == 679_100_000.0
    assert any(item.candidate_status == "REJECTED_POLICY" and item.concept_name == "ReportedBacklog" for item in leu)

    leu_incomplete = resolve_machinery_disclosure_candidates(
        [
            candidate(
                "RemainingPerformanceObligation",
                58_000_000.0,
                period_end="2024-03-31",
                scope="segment",
                evidence="RPO in the Technical Solutions segment",
            )
        ],
        ticker="LEU",
        filing={"form_type": "10-Q"},
    )
    assert [item.candidate_status for item in leu_incomplete] == ["REJECTED_POLICY"]

    mir = resolve_machinery_disclosure_candidates(
        [
            candidate(
                "RemainingPerformanceObligation",
                98_400_000.0,
                status="ACCEPTED",
                evidence="RPO in our backlog for Russian-related projects",
            ),
            candidate(
                "RemainingPerformanceObligation",
                1_120_600_000.0,
                scope="segment",
                evidence="RPO representing committed but undelivered contracts and purchase orders",
                block_index=2,
            ),
        ],
        ticker="MIR",
        filing={"form_type": "10-Q"},
    )
    assert [item.value for item in mir if item.candidate_status == "ACCEPTED"] == [1_120_600_000.0]
    assert any(item.candidate_status == "REJECTED_POLICY" for item in mir)

    gtls = resolve_machinery_disclosure_candidates(
        [
            candidate(
                "Orders",
                1_280_300_000.0,
                evidence="orders of $1,280.3 million for the three months ended March 31, 2026",
            ),
            candidate(
                "Orders",
                164_700_000.0,
                period_end="2025-03-31",
                evidence="current orders compared to the historical period",
                block_index=2,
            ),
        ],
        ticker="GTLS",
        filing={"form_type": "10-Q"},
    )
    assert [item.value for item in gtls if item.candidate_status == "ACCEPTED"] == [1_280_300_000.0]
    assert any(item.candidate_status == "REJECTED_POLICY" for item in gtls)

    fss = resolve_machinery_disclosure_candidates(
        [
            candidate(
                "Orders",
                623_000_000.0,
                evidence="Total orders for the three months ended March 31, 2026 were $623 million",
            ),
            candidate(
                "Orders",
                534_000_000.0,
                evidence="total orders of $534 million in the three months ended March 31, 2026",
                block_index=2,
            ),
        ],
        ticker="FSS",
        filing={"form_type": "10-Q"},
    )
    assert [item.value for item in fss if item.candidate_status == "ACCEPTED"] == [623_000_000.0]

    equal_value_durations = resolve_machinery_disclosure_candidates(
        [
            candidate(
                "Orders",
                100_000_000.0,
                period_start="2026-01-01",
                status="ACCEPTED",
            ),
            candidate(
                "Orders",
                100_000_000.0,
                period_start="2026-03-01",
                status="ACCEPTED",
                block_index=2,
            ),
        ],
        ticker="TEST",
        filing={"form_type": "10-Q"},
    )
    assert sum(item.candidate_status == "ACCEPTED" for item in equal_value_durations) == 2


def test_reviewed_machinery_batch_03_disclosure_semantics() -> None:
    from industrials.machinery.disclosure_candidates import (
        DisclosureCandidate,
        resolve_machinery_disclosure_candidates,
    )

    def candidate(
        concept_name: str,
        value: float,
        *,
        period_end: str,
        period_start: str = "",
        scope: str = "unknown",
        evidence: str = "",
        block_index: int = 1,
    ) -> DisclosureCandidate:
        metric_name = {
            "Orders": "orders",
            "ReportedBacklog": "reported_backlog",
            "RemainingPerformanceObligation": "remaining_performance_obligation",
        }[concept_name]
        return DisclosureCandidate(
            concept_name=concept_name,
            metric_name=metric_name,
            value=value,
            unit="USD",
            period_start=period_start,
            period_end=period_end,
            scope=scope,
            confidence=0.85,
            candidate_status="ACCEPTED",
            status_reason="fixture",
            evidence_text=evidence,
            block_index=block_index,
        )

    nvt = resolve_machinery_disclosure_candidates(
        [
            candidate(
                "RemainingPerformanceObligation",
                2_300_000_000.0,
                period_end="2025-12-31",
                evidence="Remaining performance obligations were $2.3 billion",
            ),
            candidate(
                "ReportedBacklog",
                2_300_000_000.0,
                period_end="2025-12-31",
                evidence="Committed backlog was $2.3 billion",
                block_index=2,
            ),
        ],
        ticker="NVT",
        filing={"form_type": "10-K"},
    )
    assert [item.concept_name for item in nvt if item.candidate_status == "ACCEPTED"] == [
        "RemainingPerformanceObligation"
    ]
    assert sum(item.candidate_status == "SUPPRESSED_SEMANTIC_DUPLICATE" for item in nvt) == 1

    bldp = resolve_machinery_disclosure_candidates(
        [
            candidate(
                "ReportedBacklog",
                112_900_000.0,
                period_end="2026-03-31",
                evidence="Order Backlog of approximately $112.9 million as of March 31, 2026",
            ),
            candidate(
                "ReportedBacklog",
                85_000_000.0,
                period_end="2026-03-31",
                evidence="12-month operating backlog was $85 million",
                block_index=2,
            ),
        ],
        ticker="BLDP",
        filing={"form_type": "6-K"},
    )
    assert [item.value for item in bldp if item.candidate_status == "ACCEPTED"] == [112_900_000.0]
    assert any(item.status_reason == "twelve_month_operating_backlog_separate_from_total" for item in bldp)

    mtw = resolve_machinery_disclosure_candidates(
        [
            candidate(
                "ReportedBacklog",
                793_500_000.0,
                period_end="2025-12-31",
                scope="segment",
                evidence="backlog as of December 31, 2025 was $793.5 million",
            ),
            candidate(
                "ReportedBacklog",
                100_000_000.0,
                period_end="2025-12-31",
                scope="segment",
                evidence="APAC regional backlog was $100 million",
                block_index=2,
            ),
        ],
        ticker="MTW",
        filing={"form_type": "10-K"},
    )
    assert [item.value for item in mtw if item.candidate_status == "ACCEPTED"] == [793_500_000.0]
    assert any(item.status_reason == "regional_or_component_backlog_rejected" for item in mtw)

    twin = resolve_machinery_disclosure_candidates(
        [
            candidate(
                "ReportedBacklog",
                133_700_000.0,
                period_end="2024-06-30",
            ),
            candidate(
                "ReportedBacklog",
                150_500_000.0,
                period_end="2025-06-30",
                block_index=2,
            ),
        ],
        ticker="TWIN",
        filing={"form_type": "10-K"},
    )
    assert [(item.period_end, item.value) for item in twin if item.candidate_status == "ACCEPTED"] == [
        ("2024-06-30", 133_700_000.0),
        ("2025-06-30", 150_500_000.0),
    ]
    assert all(item.scope == "consolidated" for item in twin)

    final_prospectus = {
        "form_type": "424B4",
        "accession_number": "0001193125-26-160250",
        "filing_date": "2026-04-17",
        "accepted_at": "2026-04-17T16:00:00Z",
    }
    mair = resolve_machinery_disclosure_candidates(
        [
            candidate(
                "Orders",
                1_653_400_000.0,
                period_start="2025-10-01",
                period_end="2025-12-31",
            ),
            candidate(
                "Orders",
                1_512_200_000.0,
                period_start="2025-10-01",
                period_end="2025-12-31",
                block_index=2,
            ),
            candidate(
                "ReportedBacklog",
                57_500_000.0,
                period_end="2024-12-31",
                scope="segment",
                block_index=3,
            ),
        ],
        ticker="MAIR",
        filing=final_prospectus,
    )
    assert [item.value for item in mair if item.candidate_status == "ACCEPTED"] == [1_653_400_000.0]
    assert sum(item.candidate_status == "REJECTED_POLICY" for item in mair) == 2

    q1_mair = resolve_machinery_disclosure_candidates(
        [
            candidate(
                "ReportedBacklog",
                2_520_200_000.0,
                period_end="2026-03-31",
            )
        ],
        ticker="MAIR",
        filing={"form_type": "10-Q", "filing_date": "2026-05-13"},
    )
    assert q1_mair[0].candidate_status == "ACCEPTED"
    assert q1_mair[0].scope == "consolidated"


def test_reviewed_machinery_batch_04_disclosure_semantics() -> None:
    from industrials.machinery.disclosure_candidates import (
        DisclosureCandidate,
        resolve_machinery_disclosure_candidates,
    )

    def backlog(
        value: float,
        *,
        period_end: str,
        block_index: int,
        evidence: str,
    ) -> DisclosureCandidate:
        return DisclosureCandidate(
            concept_name="ReportedBacklog",
            metric_name="reported_backlog",
            value=value,
            unit="USD",
            period_start="",
            period_end=period_end,
            scope="unknown",
            confidence=0.85,
            candidate_status="REVIEW_REQUIRED",
            status_reason="fixture",
            evidence_text=evidence,
            block_index=block_index,
        )

    astec = resolve_machinery_disclosure_candidates(
        [
            backlog(
                246_100_000.0,
                period_end="2019-06-30",
                block_index=1,
                evidence="The Company's backlog at June 30, 2019 was $246.1 million",
            ),
            backlog(
                84_500_000.0,
                period_end="2019-06-30",
                block_index=2,
                evidence="International backlog at June 30, 2019 was $84.5 million",
            ),
        ],
        ticker="ASTE",
        filing={"form_type": "8-K"},
    )
    assert [item.value for item in astec if item.candidate_status == "ACCEPTED"] == [246_100_000.0]
    assert any(item.status_reason == "reviewed_international_backlog_component" for item in astec)

    eose = resolve_machinery_disclosure_candidates(
        [
            backlog(
                463_800_000.0,
                period_end="2022-12-31",
                block_index=1,
                evidence="Orders backlog was $463.8 million as of December 31, 2022",
            ),
            backlog(
                147_500_000.0,
                period_end="2022-12-31",
                block_index=2,
                evidence="Compared with $147.5 million as of December 31, 2021",
            ),
        ],
        ticker="EOSE",
        filing={
            "form_type": "8-K",
            "accession_number": "0001628280-23-005669",
        },
    )
    assert {(item.period_end, item.value) for item in eose if item.candidate_status == "ACCEPTED"} == {
        ("2021-12-31", 147_500_000.0),
        ("2022-12-31", 463_800_000.0),
    }

    jbtm = resolve_machinery_disclosure_candidates(
        [
            backlog(
                662_000_000.0,
                period_end="2022-10-26",
                block_index=1,
                evidence="FoodTech backlog was $662 million",
            ),
            backlog(
                387_000_000.0,
                period_end="2022-10-26",
                block_index=2,
                evidence="AeroTech backlog was $387 million",
            ),
        ],
        ticker="JBTM",
        filing={
            "form_type": "8-K",
            "accession_number": "0001433660-22-000034",
        },
    )
    jbtm_total = [item for item in jbtm if item.candidate_status == "ACCEPTED"]
    assert [(item.period_end, item.value) for item in jbtm_total] == [("2022-09-30", 1_049_000_000.0)]

    proterra = resolve_machinery_disclosure_candidates(
        [
            backlog(
                1_000_000_000.0,
                period_end="2023-03-15",
                block_index=1,
                evidence="Proterra Powered & Energy backlog totaled $1 billion",
            ),
            backlog(
                600_000_000.0,
                period_end="2023-03-15",
                block_index=2,
                evidence="Proterra Transit backlog was $0.6 billion",
            ),
        ],
        ticker="PTRA",
        filing={
            "form_type": "8-K",
            "accession_number": "0001628280-23-008121",
        },
    )
    assert [item.value for item in proterra if item.candidate_status == "ACCEPTED"] == [1_600_000_000.0]

    mair = resolve_machinery_disclosure_candidates(
        [
            backlog(
                987_400_000.0,
                period_end="2026-04-17",
                block_index=1,
                evidence="As of December 31, 2024, backlog totaled $987.4 million",
            ),
            backlog(
                2_160_800_000.0,
                period_end="2026-04-17",
                block_index=2,
                evidence="As of December 31, 2025, backlog was $2,160.8 million",
            ),
        ],
        ticker="MAIR",
        filing={
            "form_type": "424B4",
            "accession_number": "0001193125-26-160250",
            "filing_date": "2026-04-17",
        },
    )
    assert {(item.period_end, item.value) for item in mair if item.candidate_status == "ACCEPTED"} == {
        ("2024-12-31", 987_400_000.0),
        ("2025-12-31", 2_160_800_000.0),
    }

    vertiv = resolve_machinery_disclosure_candidates(
        [
            backlog(
                1_400_800_000.0,
                period_end="2020-02-07",
                block_index=1,
                evidence="Combined backlog values reported respectively",
            ),
            backlog(
                1_502_000_000.0,
                period_end="2020-02-07",
                block_index=2,
                evidence="Combined backlog values reported respectively",
            ),
        ],
        ticker="VRT",
        filing={
            "form_type": "S-1",
            "accession_number": "0001193125-20-028316",
        },
    )
    assert {(item.period_end, item.value) for item in vertiv if item.candidate_status == "ACCEPTED"} == {
        ("2019-09-30", 1_400_800_000.0),
        ("2018-12-31", 1_502_000_000.0),
    }


def test_machinery_prose_extracts_date_first_and_fiscal_year_disclosures() -> None:
    from industrials.machinery.disclosure_candidates import (
        extract_machinery_prose_candidates,
        is_known_by_asof,
    )

    filing = {
        "report_date": "2026-03-31",
        "filing_date": "2026-05-13",
        "accepted_at": "2026-05-13T16:00:00Z",
    }
    candidates = extract_machinery_prose_candidates(
        """
        <p>As of March 31, 2026 our consolidated backlog was $2,520.2 million.</p>
        <p>Unfilled open orders for the next six months of $150.5 million at June 30, 2025.</p>
        <p>For the year ended December 31, 2025, total orders were $4,530.8 million.</p>
        """,
        filing=filing,
        company_currency="USD",
    )
    assert {(item.concept_name, item.period_start, item.period_end, item.value) for item in candidates} == {
        ("ReportedBacklog", "", "2025-06-30", 150_500_000.0),
        ("ReportedBacklog", "", "2026-03-31", 2_520_200_000.0),
        ("Orders", "2025-01-01", "2025-12-31", 4_530_800_000.0),
    }

    final_prospectus = {
        "filing_date": "2026-04-17",
        "accepted_at": "2026-04-17T16:00:00Z",
    }
    assert not is_known_by_asof(final_prospectus, "2026-04-16")
    assert is_known_by_asof(final_prospectus, "2026-04-17")


def test_cached_prose_candidate_promotion_replaces_stale_document_facts(tmp_path: Path) -> None:
    from industrials.machinery.disclosure_candidates import (
        extract_machinery_prose_candidates,
        replace_document_candidates_and_facts,
    )

    db_path = tmp_path / "industrials.sqlite"
    filing = {
        "accession_number": "0000000000-26-000001",
        "form_type": "10-Q",
        "filing_date": "2026-05-01",
        "accepted_at": "2026-05-01T16:00:00Z",
        "report_date": "2026-03-31",
        "fiscal_year": 2026,
        "fiscal_period": "Q1",
    }
    with connect(db_path) as conn, conn:
        init_db(conn)
        now = "2026-07-19T00:00:00Z"
        conn.execute(
            """
            INSERT INTO source_registry(
                source_id, stage, source_name, source_type, base_url, status, created_at, updated_at
            ) VALUES ('sec_companyfacts', 'stage_4', 'SEC CompanyFacts', 'api',
                      'https://data.sec.gov', 'active', ?, ?)
            """,
            (now, now),
        )
        accepted = extract_machinery_prose_candidates(
            "<p>Our backlog was $2.9 billion as of March 31, 2026.</p>",
            filing=filing,
        )
        counts = replace_document_candidates_and_facts(
            conn,
            ticker="FLS",
            cik="30625",
            source_id="sec_companyfacts",
            model_family="machinery",
            filing=filing,
            document_name="fls-20260331.htm",
            candidates=accepted,
            now=now,
        )
        assert counts == (1, 1, 1)
        mapped = conn.execute(
            """
            SELECT canonical_metric, value, source_detail
            FROM fact_sec_xbrl_fact
            WHERE ticker = 'FLS' AND source_detail = 'sec_archive_prose_metric_mapped'
            """
        ).fetchall()
        assert [tuple(row) for row in mapped] == [
            ("reported_backlog", 2_900_000_000.0, "sec_archive_prose_metric_mapped")
        ]

        ambiguous = extract_machinery_prose_candidates(
            """
            <h2>Flowserve Pump Division</h2>
            <p>Segment backlog was $2.1 billion as of March 31, 2026.</p>
            <h2>Flow Control Division</h2>
            <p>Segment backlog was $876.4 million as of March 31, 2026.</p>
            """,
            filing=filing,
        )
        counts = replace_document_candidates_and_facts(
            conn,
            ticker="FLS",
            cik="30625",
            source_id="sec_companyfacts",
            model_family="machinery",
            filing=filing,
            document_name="fls-20260331.htm",
            candidates=ambiguous,
            now=now,
        )
        assert counts == (2, 0, 0)
        assert conn.execute("SELECT COUNT(*) FROM fact_sec_xbrl_fact WHERE ticker = 'FLS'").fetchone()[0] == 0
        statuses = conn.execute(
            """
            SELECT candidate_status
            FROM fact_sec_metric_disclosure_candidate
            WHERE ticker = 'FLS'
            ORDER BY candidate_value
            """
        ).fetchall()
        assert [row[0] for row in statuses] == ["REVIEW_REQUIRED", "REVIEW_REQUIRED"]


def test_reviewed_policy_replay_updates_stored_submission_candidates(
    tmp_path: Path,
) -> None:
    from industrials.machinery.disclosure_candidates import (
        DisclosureCandidate,
        reapply_reviewed_disclosure_policies,
        replace_document_candidates_and_facts,
    )

    db_path = tmp_path / "industrials.sqlite"
    filing = {
        "accession_number": "0001628280-23-005669",
        "form_type": "8-K",
        "filing_date": "2023-03-01",
        "accepted_at": "2023-02-28T16:00:00Z",
    }
    with connect(db_path) as conn, conn:
        init_db(conn)
        now = "2026-07-23T00:00:00Z"
        conn.execute(
            """
            INSERT INTO source_registry(
                source_id, stage, source_name, source_type, base_url, status,
                created_at, updated_at
            ) VALUES ('sec_companyfacts', 'stage_4', 'SEC CompanyFacts', 'api',
                      'https://data.sec.gov', 'active', ?, ?)
            """,
            (now, now),
        )
        stale = [
            DisclosureCandidate(
                concept_name="ReportedBacklog",
                metric_name="reported_backlog",
                value=value,
                unit="USD",
                period_start="",
                period_end="2022-12-31",
                scope="unknown",
                confidence=0.65,
                candidate_status="REVIEW_REQUIRED",
                status_reason="ambiguous_multiple_scope_values",
                evidence_text=evidence,
                block_index=index,
            )
            for index, value, evidence in (
                (
                    1,
                    463_800_000.0,
                    "Backlog was $463.8 million as of December 31, 2022",
                ),
                (
                    2,
                    147_500_000.0,
                    "Compared with backlog of $147.5 million",
                ),
            )
        ]
        assert replace_document_candidates_and_facts(
            conn,
            ticker="EOSE",
            cik="1805077",
            source_id="sec_companyfacts",
            model_family="machinery",
            filing=filing,
            document_name="0001628280-23-005669.txt",
            candidates=stale,
            now=now,
        ) == (2, 0, 0)

        replayed = reapply_reviewed_disclosure_policies(
            conn,
            tickers=["EOSE"],
            model_family="machinery",
            now="2026-07-23T01:00:00Z",
        )
        assert replayed == {
            "documents_replayed": 1,
            "candidate_rows": 2,
            "promoted_raw": 2,
            "promoted_mapped": 2,
        }
        candidates = conn.execute(
            """
            SELECT period_end, candidate_value, candidate_status
            FROM fact_sec_metric_disclosure_candidate
            WHERE ticker = 'EOSE'
            ORDER BY period_end
            """
        ).fetchall()
        assert [tuple(row) for row in candidates] == [
            ("2021-12-31", 147_500_000.0, "ACCEPTED"),
            ("2022-12-31", 463_800_000.0, "ACCEPTED"),
        ]


def test_machinery_disclosure_reconciliation_prefers_periodic_filing(tmp_path: Path) -> None:
    from industrials.machinery.disclosure_candidates import (
        DisclosureCandidate,
        reconcile_machinery_disclosure_facts,
        replace_document_candidates_and_facts,
    )

    db_path = tmp_path / "industrials.sqlite"
    now = "2026-07-19T00:00:00Z"
    with connect(db_path) as conn, conn:
        init_db(conn)
        conn.execute(
            """
            INSERT INTO source_registry(
                source_id, stage, source_name, source_type, base_url, status, created_at, updated_at
            ) VALUES ('sec_companyfacts', 'stage_4', 'SEC CompanyFacts', 'api',
                      'https://data.sec.gov', 'active', ?, ?)
            """,
            (now, now),
        )
        disclosure = DisclosureCandidate(
            concept_name="ReportedBacklog",
            metric_name="reported_backlog",
            value=1_800_000_000.0,
            unit="USD",
            period_start="",
            period_end="2026-03-31",
            scope="consolidated",
            confidence=0.85,
            candidate_status="ACCEPTED",
            status_reason="reviewed_issuer_consolidated_disclosure",
            evidence_text="Backlog was $1.8 billion as of March 31, 2026",
            block_index=1,
        )
        for form_type, accession, document, accepted_at in (
            ("8-K", "0000000001-26-000001", "powell-8k.htm", "2026-04-30T16:00:00Z"),
            ("10-Q", "0000000001-26-000002", "powell-10q.htm", "2026-05-01T16:00:00Z"),
            (
                "10-Q",
                "0000000001-26-000003",
                "powell-repeat-10q.htm",
                "2026-06-01T16:00:00Z",
            ),
        ):
            replace_document_candidates_and_facts(
                conn,
                ticker="POWL",
                cik="0000000001",
                source_id="sec_companyfacts",
                model_family="machinery",
                filing={
                    "accession_number": accession,
                    "form_type": form_type,
                    "filing_date": "2026-05-01",
                    "accepted_at": accepted_at,
                    "report_date": "2026-03-31",
                },
                document_name=document,
                candidates=[disclosure],
                now=now,
            )
        stats = reconcile_machinery_disclosure_facts(
            conn,
            ticker="POWL",
            source_id="sec_companyfacts",
            model_family="machinery",
            now=now,
        )
        assert stats["candidate_suppressions"] == 2
        remaining = conn.execute(
            """
            SELECT form_type, accepted_at, value
            FROM fact_sec_xbrl_fact
            WHERE ticker = 'POWL' AND canonical_metric = 'reported_backlog'
            """
        ).fetchall()
        assert [tuple(row) for row in remaining] == [("10-Q", "2026-05-01T16:00:00Z", 1_800_000_000.0)]
        statuses = conn.execute(
            """
            SELECT accession_number, form_type, candidate_status
            FROM fact_sec_metric_disclosure_candidate
            WHERE ticker = 'POWL'
            ORDER BY accession_number
            """
        ).fetchall()
        assert [tuple(row) for row in statuses] == [
            ("0000000001-26-000001", "8-K", "SUPPRESSED_DUPLICATE_PROVENANCE"),
            ("0000000001-26-000002", "10-Q", "ACCEPTED"),
            ("0000000001-26-000003", "10-Q", "SUPPRESSED_DUPLICATE_PROVENANCE"),
        ]

        sec_namespace = runpy.run_path(
            str(PROJECT_ROOT / "industrials" / "scripts" / "07_sync_industrials_sec_fundamentals.py")
        )
        archive_fact = sec_namespace["ArchiveFact"]
        upsert_archive_facts = sec_namespace["upsert_archive_facts"]
        concept_map = {
            ("sec-text", "Orders"): [
                {
                    "canonical_metric": "orders",
                    "financial_statement": "orders",
                    "period_type": "duration",
                    "sign_policy": "positive_abs",
                    "priority": 200,
                }
            ]
        }
        for form_type, accession, accepted_at in (
            ("8-K", "0000000001-26-000010", "2026-04-30T16:00:00Z"),
            ("10-Q", "0000000001-26-000011", "2026-05-01T16:00:00Z"),
        ):
            upsert_archive_facts(
                conn,
                ticker="BWEN",
                cik="0000000001",
                source_id="sec_companyfacts",
                filing={
                    "accession_number": accession,
                    "form_type": form_type,
                    "filing_date": accepted_at[:10],
                    "accepted_at": accepted_at,
                },
                document_name=f"bwen-{form_type.lower()}.htm",
                facts=[
                    archive_fact(
                        taxonomy="sec-text",
                        concept_name="Orders",
                        unit="USD",
                        value=37_422_000.0,
                        period_start="2026-01-01",
                        period_end="2026-03-31",
                        frame=f"orders:{form_type}",
                        decimals="",
                        payload_json="{}",
                        source_detail="sec_archive_text_table",
                    )
                ],
                concept_map=concept_map,
                start_date="",
            )
        structured_stats = reconcile_machinery_disclosure_facts(
            conn,
            ticker="BWEN",
            source_id="sec_companyfacts",
            model_family="machinery",
            now=now,
        )
        assert structured_stats["duplicate_mapped_facts_suppressed"] == 1
        assert [
            tuple(row)
            for row in conn.execute(
                """
                SELECT form_type, value
                FROM fact_sec_xbrl_fact
                WHERE ticker = 'BWEN' AND canonical_metric = 'orders'
                """
            ).fetchall()
        ] == [("10-Q", 37_422_000.0)]
        assert conn.execute("SELECT COUNT(*) FROM fact_sec_xbrl_fact_raw WHERE ticker = 'BWEN'").fetchone()[0] == 2


def test_disclosure_candidate_and_metric_applicability_classification(tmp_path: Path) -> None:
    from industrials.machinery.disclosure_candidates import (
        DisclosureCandidate,
        upsert_disclosure_candidates,
    )

    namespace = runpy.run_path(
        str(PROJECT_ROOT / "industrials" / "scripts" / "08_build_industrials_financial_features.py")
    )
    db_path = tmp_path / "industrials.sqlite"
    with connect(db_path) as conn:
        init_db(conn)
        now = "2026-07-19T00:00:00+00:00"
        conn.execute(
            """
            INSERT INTO source_registry(
                source_id, stage, source_name, source_type, base_url, status, created_at, updated_at
            ) VALUES ('sec_companyfacts', 'stage_4', 'SEC CompanyFacts', 'api',
                      'https://data.sec.gov', 'active', ?, ?)
            """,
            (now, now),
        )
        upsert_disclosure_candidates(
            conn,
            ticker="TEST",
            cik="0000000001",
            source_id="sec_companyfacts",
            model_family="machinery",
            filing={
                "accession_number": "0000000001-26-000001",
                "form_type": "10-Q",
                "filing_date": "2026-05-01",
                "accepted_at": "2026-05-01T16:00:00",
            },
            document_name="test.htm",
            candidates=[
                DisclosureCandidate(
                    concept_name="ReportedBacklog",
                    metric_name="reported_backlog",
                    value=500_000_000.0,
                    unit="USD",
                    period_start="",
                    period_end="2026-03-31",
                    scope="unknown",
                    confidence=0.65,
                    candidate_status="REVIEW_REQUIRED",
                    status_reason="ambiguous_multiple_scope_values",
                    evidence_text="Backlog of $500 million at March 31, 2026",
                    block_index=1,
                )
            ],
            now=now,
        )
        availability = namespace["classify_financial_metric_availability"](
            conn,
            feature={
                "revenue": 100.0,
                "cash_burn_ttm_usd": 0.0,
                "total_debt_usd": 0.0,
                "interest_expense_ttm_usd": None,
            },
            rows=[],
            company={"ticker": "TEST", "development_stage": "operating"},
            source_id="sec_companyfacts",
            model_family="machinery",
            asof=date(2026, 7, 9),
            availability_policy={
                "structural_no_backlog_tickers": ["TEST"],
                "structural_no_backlog_valid_from": "2026-07-09",
            },
        )
        upsert_disclosure_candidates(
            conn,
            ticker="PARSED",
            cik="0000000002",
            source_id="sec_companyfacts",
            model_family="machinery",
            filing={
                "accession_number": "0000000002-26-000001",
                "form_type": "10-Q",
                "filing_date": "2026-05-01",
                "accepted_at": "2026-05-01T16:00:00",
            },
            document_name="parsed.htm",
            candidates=[
                DisclosureCandidate(
                    concept_name="ReportedBacklog",
                    metric_name="reported_backlog",
                    value=500_000_000.0,
                    unit="USD",
                    period_start="",
                    period_end="2026-03-31",
                    scope="consolidated",
                    confidence=0.85,
                    candidate_status="ACCEPTED",
                    status_reason="reviewed_consolidated_value",
                    evidence_text="Backlog was $500 million as of March 31, 2026",
                    block_index=1,
                )
            ],
            now=now,
        )
        parsed_availability = namespace["classify_financial_metric_availability"](
            conn,
            feature={
                "revenue": 100.0,
                "reported_backlog": 500_000_000.0,
                "contract_load_proxy": 500_000_000.0,
                "contract_load_proxy_source": "reported_backlog",
            },
            rows=[
                {
                    "canonical_metric": "reported_backlog",
                    "value": 500_000_000.0,
                    "unit": "USD",
                    "period_start": "",
                    "period_end": "2026-03-31",
                    "filing_date": "2026-05-01",
                    "source_priority": 40,
                }
            ],
            company={"ticker": "PARSED", "development_stage": "operating"},
            source_id="sec_companyfacts",
            model_family="machinery",
            asof=date(2026, 7, 9),
            availability_policy={},
        )
    by_metric = {row["metric_name"]: row for row in availability}
    assert by_metric["reported_backlog"]["availability_status"] == "DISCLOSED_UNPARSED"
    assert by_metric["reported_backlog_yoy_growth"]["availability_status"] == "DISCLOSED_UNPARSED"
    assert by_metric["capital_raise_dependence"]["availability_status"] == "NOT_APPLICABLE"
    assert by_metric["interest_coverage"]["availability_status"] == "NOT_APPLICABLE"
    parsed_by_metric = {row["metric_name"]: row for row in parsed_availability}
    assert parsed_by_metric["reported_backlog"]["availability_status"] == "REPORTED"
    assert parsed_by_metric["reported_backlog_yoy_growth"]["availability_status"] == "NOT_DISCLOSED"
    assert parsed_by_metric["contract_load_proxy"]["availability_status"] == "PROXY"
    assert parsed_by_metric["contract_load_proxy"]["concept_name"] is None
    assert parsed_by_metric["contract_load_proxy"]["period_end"] == "2026-03-31"
    assert parsed_by_metric["contract_load_proxy"]["status_reason"] == (
        "canonical_contract_load_proxy_from_reported_backlog"
    )


def test_latest_comparable_pair_preserves_older_valid_yoy_history() -> None:
    namespace = runpy.run_path(
        str(PROJECT_ROOT / "industrials" / "scripts" / "08_build_industrials_financial_features.py")
    )
    rows = [
        {
            "canonical_metric": "reported_backlog",
            "value": 130.0,
            "period_start": "",
            "period_end": "2026-03-31",
            "filing_date": "2026-05-01",
            "source_priority": 40,
        },
        {
            "canonical_metric": "reported_backlog",
            "value": 110.0,
            "period_start": "",
            "period_end": "2025-12-31",
            "filing_date": "2026-02-01",
            "source_priority": 40,
        },
        {
            "canonical_metric": "reported_backlog",
            "value": 100.0,
            "period_start": "",
            "period_end": "2024-12-31",
            "filing_date": "2025-02-01",
            "source_priority": 40,
        },
    ]
    current, previous = namespace["select_latest_comparable_pair"](
        rows,
        "reported_backlog",
        instant_metric=True,
    )
    assert current is not None and current["period_end"] == "2025-12-31"
    assert previous is not None and previous["period_end"] == "2024-12-31"


def test_diluted_share_growth_uses_latest_comparable_period_and_rejects_scale_outliers() -> None:
    namespace = runpy.run_path(
        str(PROJECT_ROOT / "industrials" / "scripts" / "08_build_industrials_financial_features.py")
    )
    rows = [
        {
            "canonical_metric": "diluted_shares",
            "value": 80_230_000.0,
            "period_start": "2025-01-01",
            "period_end": "2025-09-30",
            "filing_date": "2025-11-01",
            "source_priority": 10,
        },
        {
            "canonical_metric": "diluted_shares",
            "value": 70_670_000.0,
            "period_start": "2024-01-01",
            "period_end": "2024-09-30",
            "filing_date": "2024-11-01",
            "source_priority": 10,
        },
        {
            "canonical_metric": "diluted_shares",
            "value": 66_491_000.0,
            "period_start": "2022-01-01",
            "period_end": "2022-12-31",
            "filing_date": "2023-03-01",
            "source_priority": 10,
        },
        {
            "canonical_metric": "diluted_shares",
            "value": 63_471.0,
            "period_start": "2021-01-01",
            "period_end": "2021-12-31",
            "filing_date": "2022-03-01",
            "source_priority": 10,
        },
    ]
    current, previous = namespace["select_latest_comparable_pair"](rows, "diluted_shares")
    value, outlier = namespace["validated_diluted_share_growth"](current, previous)
    assert current is not None and current["period_end"] == "2025-09-30"
    assert previous is not None and previous["period_end"] == "2024-09-30"
    assert value == pytest.approx(0.1352766379)
    assert outlier is False

    annual_current, annual_previous = namespace["select_latest_comparable_pair"](
        rows,
        "diluted_shares",
        prefer_annual=True,
    )
    annual_value, annual_outlier = namespace["validated_diluted_share_growth"](
        annual_current,
        annual_previous,
    )
    assert annual_value is None
    assert annual_outlier is True


def test_inventory_sales_spread_uses_one_aligned_period_pair() -> None:
    namespace = runpy.run_path(
        str(PROJECT_ROOT / "industrials" / "scripts" / "08_build_industrials_financial_features.py")
    )

    def fact(
        metric: str,
        value: float,
        end: str,
        *,
        start: str = "",
        form_type: str = "10-K",
    ) -> dict[str, object]:
        return {
            "canonical_metric": metric,
            "value": value,
            "period_start": start,
            "period_end": end,
            "filing_date": end,
            "form_type": form_type,
            "fiscal_period": "FY" if form_type == "10-K" else "Q1",
            "source_priority": 10,
        }

    rows = [
        # Latest inventory has no prior-year quarter and must not be combined
        # with the annual revenue growth below.
        fact("inventory", 170.0, "2026-03-31", form_type="10-Q"),
        fact("inventory", 150.0, "2025-12-31"),
        fact("inventory", 100.0, "2024-12-31"),
        fact("revenue", 1_200.0, "2025-12-31", start="2025-01-01"),
        fact("revenue", 1_000.0, "2024-12-31", start="2024-01-01"),
    ]
    inventory_growth, revenue_growth, spread = namespace["select_aligned_inventory_revenue_growth"](rows)
    assert inventory_growth == pytest.approx(0.5)
    assert revenue_growth == pytest.approx(0.2)
    assert spread == pytest.approx(0.3)


def test_ifrs_basic_shares_only_proxy_diluted_when_eps_matches() -> None:
    namespace = runpy.run_path(
        str(PROJECT_ROOT / "industrials" / "scripts" / "08_build_industrials_financial_features.py")
    )

    def duration(metric: str, value: float, start: str, end: str) -> dict[str, object]:
        return {
            "canonical_metric": metric,
            "value": value,
            "period_start": start,
            "period_end": end,
            "filing_date": end,
            "form_type": "20-F",
            "fiscal_period": "FY",
            "source_priority": 10,
        }

    rows = [
        duration("basic_shares", 110.0, "2025-01-01", "2025-12-31"),
        duration("eps_basic", -1.0, "2025-01-01", "2025-12-31"),
        duration("eps_diluted", -1.0, "2025-01-01", "2025-12-31"),
        duration("basic_shares", 100.0, "2024-01-01", "2024-12-31"),
        duration("eps_basic", -0.8, "2024-01-01", "2024-12-31"),
        duration("eps_diluted", -0.8, "2024-01-01", "2024-12-31"),
    ]
    current, previous = namespace["select_basic_share_pair_when_eps_equal"](rows)
    assert current is not None and current["value"] == 110.0
    assert previous is not None and previous["value"] == 100.0

    rows[-1]["value"] = -0.7
    assert namespace["select_basic_share_pair_when_eps_equal"](rows) == (None, None)


def test_recent_public_metric_policies_preserve_proxy_and_not_applicable_semantics(
    tmp_path: Path,
) -> None:
    namespace = runpy.run_path(
        str(PROJECT_ROOT / "industrials" / "scripts" / "08_build_industrials_financial_features.py")
    )
    db_path = tmp_path / "industrials.sqlite"
    with connect(db_path) as conn:
        init_db(conn)
        availability = namespace["classify_financial_metric_availability"](
            conn,
            feature={
                "revenue": 100.0,
                "asset_turnover": 0.5,
                "canonical_quality": ("mapped_xbrl;asset_turnover_proxy_ttm_revenue_over_ending_assets"),
            },
            rows=[],
            company={"ticker": "XE", "development_stage": "operating"},
            source_id="sec_companyfacts",
            model_family="machinery",
            asof=date(2026, 7, 20),
            availability_policy={
                "structural_no_inventory_valid_from": "2026-07-20",
                "structural_no_inventory_tickers": ["XE"],
                "recent_public_share_basis_valid_from": "2026-07-20",
                "recent_public_share_basis_transition_tickers": ["XE"],
            },
        )
    by_metric = {row["metric_name"]: row for row in availability}
    assert by_metric["asset_turnover"]["availability_status"] == "PROXY"
    assert by_metric["asset_turnover"]["status_reason"] == "asset_turnover_proxy_ttm_revenue_over_ending_assets"
    assert by_metric["inventory_sales_growth_spread"]["availability_status"] == "NOT_APPLICABLE"
    assert by_metric["diluted_shares_yoy_growth"]["availability_status"] == "NOT_APPLICABLE"


def test_recent_public_asset_turnover_uses_labeled_ending_assets_proxy(tmp_path: Path) -> None:
    namespace = runpy.run_path(
        str(PROJECT_ROOT / "industrials" / "scripts" / "08_build_industrials_financial_features.py")
    )

    def fact(metric: str, value: float, *, duration: bool) -> dict[str, object]:
        return {
            "canonical_metric": metric,
            "period_start": "2025-01-01" if duration else "",
            "period_end": "2025-12-31",
            "filing_date": "2026-04-15",
            "accepted_at": "2026-04-15T12:00:00Z",
            "accession_number": "IPO-1",
            "form_type": "424B4",
            "fiscal_year": 2025,
            "fiscal_period": "FY",
            "reporting_standard": "US_GAAP",
            "taxonomy": "sec-text",
            "concept_name": metric,
            "unit": "USD",
            "value": value,
            "value_usd": value,
            "source_priority": 200,
        }

    db_path = tmp_path / "asset_proxy.sqlite"
    with connect(db_path) as conn:
        init_db(conn)
        feature = namespace["build_feature_from_facts"](
            conn,
            ticker="IPO",
            asof=date(2026, 7, 20),
            source_id="sec_companyfacts",
            model_family="machinery",
            company={
                "ticker": "IPO",
                "country": "United States",
                "currency": "USD",
                "membership_start_date": "2026-04-15",
                "development_stage": "operating",
            },
            profile={
                "reporting_profile": "SEC_ARCHIVE_TEXT_TABLE",
                "reporting_standard": "US_GAAP",
                "financial_confidence": 0.55,
                "fallback_status": "text_table_extracted",
                "review_reason": "",
            },
            rows=[fact("revenue", 1_000.0, duration=True), fact("assets", 2_000.0, duration=False)],
            market_source_ids=["yahoo_finance_adjusted"],
            fx_max_staleness_days=7,
        )
    assert feature["asset_turnover"] == pytest.approx(0.5)
    assert "asset_turnover_proxy_ttm_revenue_over_ending_assets" in str(feature["canonical_quality"])


def test_registration_statement_heading_survives_long_provenance_context() -> None:
    namespace = runpy.run_path(
        str(PROJECT_ROOT / "industrials" / "scripts" / "07_sync_industrials_sec_fundamentals.py")
    )
    filler = "<p>Historical financial information.</p>" * 42
    facts = namespace["parse_archive_text_table_facts"](
        f"""
        <h2>C onsolidated Balance Sheets</h2>
        {filler}
        <p>(in millions)</p>
        <table>
          <tr><th></th><th>December 31, 2025</th><th>December 31, 2024</th></tr>
          <tr><td>Cash and cash equivalents</td><td>100</td><td>80</td></tr>
          <tr><td>Inventories (Note 9)</td><td>408.4</td><td>293.3</td></tr>
          <tr><td>Total assets</td><td>8,177.1</td><td>5,453.0</td></tr>
          <tr><td>Total liabilities</td><td>4,000</td><td>3,000</td></tr>
        </table>
        """,
        document_name="issuer-424b4.htm",
        filing={
            "report_date": "2025-12-31",
            "filing_date": "2026-04-15",
            "form_type": "424B4",
        },
        company_currency="USD",
        strict_registration_statements=True,
    )
    inventory_facts = [fact for fact in facts if fact.concept_name == "Inventory"]
    assert {fact.period_end for fact in inventory_facts} == {"2024-12-31", "2025-12-31"}
    assert {fact.value for fact in inventory_facts} == {293_300_000.0, 408_400_000.0}


def test_normal_filing_text_tables_do_not_map_cash_flow_inventory_changes() -> None:
    namespace = runpy.run_path(
        str(PROJECT_ROOT / "industrials" / "scripts" / "07_sync_industrials_sec_fundamentals.py")
    )
    parse_tables = namespace["parse_archive_text_table_facts"]
    filing = {
        "report_date": "2025-12-31",
        "filing_date": "2026-02-15",
        "form_type": "10-K",
    }
    cash_flow_facts = parse_tables(
        """
        <h2>Consolidated Statements of Cash Flows</h2>
        <p>(in millions)</p>
        <table>
          <tr><th></th><th>2025</th><th>2024</th></tr>
          <tr><td>Inventories</td><td>(29.7)</td><td>33.0</td></tr>
          <tr><td>Net cash provided by operating activities</td><td>1,000</td><td>900</td></tr>
        </table>
        """,
        document_name="annual-report.htm",
        filing=filing,
        company_currency="USD",
    )
    assert all(fact.concept_name != "Inventory" for fact in cash_flow_facts)

    balance_sheet_facts = parse_tables(
        """
        <h2>Consolidated Balance Sheets</h2>
        <p>(in millions)</p>
        <table>
          <tr><th></th><th>2025</th><th>2024</th></tr>
          <tr><td>Inventories</td><td>2,187.5</td><td>2,367.1</td></tr>
          <tr><td>Total assets</td><td>40,000</td><td>38,000</td></tr>
        </table>
        """,
        document_name="annual-report.htm",
        filing=filing,
        company_currency="USD",
    )
    inventory_values = {fact.value for fact in balance_sheet_facts if fact.concept_name == "Inventory"}
    assert inventory_values == {2_187_500_000.0, 2_367_100_000.0}


def test_share_concept_seed_uses_counts_not_eps_abstracts() -> None:
    from industrials.core.db import XBRL_CONCEPT_MAP_SEED

    mappings = {
        (str(row["taxonomy"]), str(row["concept_name"])): str(row["canonical_metric"]) for row in XBRL_CONCEPT_MAP_SEED
    }
    assert mappings[("ifrs-full", "AdjustedWeightedAverageShares")] == "diluted_shares"
    assert mappings[("ifrs-full", "WeightedAverageShares")] == "basic_shares"
    assert mappings[("dei", "EntityCommonStockSharesOutstanding")] == "shares_outstanding"
    assert ("ifrs-full", "DilutedEarningsPerShareAbstract") not in mappings


def test_structural_backlog_policy_does_not_leak_before_valid_from(tmp_path: Path) -> None:
    namespace = runpy.run_path(
        str(PROJECT_ROOT / "industrials" / "scripts" / "08_build_industrials_financial_features.py")
    )
    db_path = tmp_path / "industrials.sqlite"
    with connect(db_path) as conn:
        init_db(conn)
        historical = namespace["classify_financial_metric_availability"](
            conn,
            feature={"revenue": 100.0},
            rows=[],
            company={"ticker": "SHORT", "development_stage": "operating"},
            source_id="sec_companyfacts",
            model_family="machinery",
            asof=date(2025, 12, 31),
            availability_policy={
                "structural_no_backlog_tickers": ["SHORT"],
                "structural_no_backlog_valid_from": "2026-07-09",
            },
        )
        current = namespace["classify_financial_metric_availability"](
            conn,
            feature={"revenue": 100.0},
            rows=[],
            company={"ticker": "SHORT", "development_stage": "operating"},
            source_id="sec_companyfacts",
            model_family="machinery",
            asof=date(2026, 7, 9),
            availability_policy={
                "structural_no_backlog_tickers": ["SHORT"],
                "structural_no_backlog_valid_from": "2026-07-09",
            },
        )
    historical_by_metric = {row["metric_name"]: row for row in historical}
    current_by_metric = {row["metric_name"]: row for row in current}
    assert historical_by_metric["orders"]["availability_status"] == "NOT_DISCLOSED"
    assert current_by_metric["orders"]["availability_status"] == "NOT_APPLICABLE"
    assert current_by_metric["remaining_performance_obligation"]["availability_status"] == ("NOT_APPLICABLE")
    assert current_by_metric["rpo_implied_book_to_bill"]["availability_status"] == ("NOT_APPLICABLE")
    assert current_by_metric["contract_load_proxy"]["availability_status"] == ("NOT_APPLICABLE")


def test_machinery_rpo_timing_dimensions_and_text_percentage() -> None:
    namespace = runpy.run_path(
        str(PROJECT_ROOT / "industrials" / "scripts" / "07_sync_industrials_sec_fundamentals.py")
    )
    parse_facts = namespace["parse_machinery_footnote_facts"]
    timing_facts = parse_facts(
        """
        <html xmlns="http://www.w3.org/1999/xhtml"
              xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
              xmlns:xbrli="http://www.xbrl.org/2003/instance"
              xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
              xmlns:iso4217="http://www.xbrl.org/2003/iso4217"
              xmlns:us-gaap="http://fasb.org/us-gaap/2026">
          <body>
            <xbrli:context id="total"><xbrli:entity><xbrli:identifier scheme="cik">1</xbrli:identifier></xbrli:entity><xbrli:period><xbrli:instant>2026-03-31</xbrli:instant></xbrli:period></xbrli:context>
            <xbrli:context id="current"><xbrli:entity><xbrli:identifier scheme="cik">1</xbrli:identifier><xbrli:segment><xbrldi:typedMember dimension="us-gaap:RevenueRemainingPerformanceObligationExpectedTimingOfSatisfactionStartDateAxis"><us-gaap:StartDate>2026-04-01</us-gaap:StartDate></xbrldi:typedMember></xbrli:segment></xbrli:entity><xbrli:period><xbrli:instant>2026-03-31</xbrli:instant></xbrli:period></xbrli:context>
            <xbrli:context id="later"><xbrli:entity><xbrli:identifier scheme="cik">1</xbrli:identifier><xbrli:segment><xbrldi:typedMember dimension="us-gaap:RevenueRemainingPerformanceObligationExpectedTimingOfSatisfactionStartDateAxis"><us-gaap:StartDate>2027-04-01</us-gaap:StartDate></xbrldi:typedMember></xbrli:segment></xbrli:entity><xbrli:period><xbrli:instant>2026-03-31</xbrli:instant></xbrli:period></xbrli:context>
            <xbrli:unit id="USD"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
            <ix:nonFraction name="us-gaap:RevenueRemainingPerformanceObligation" contextRef="total" unitRef="USD">1000</ix:nonFraction>
            <ix:nonFraction name="us-gaap:RevenueRemainingPerformanceObligation" contextRef="current" unitRef="USD">400</ix:nonFraction>
            <ix:nonNumeric name="us-gaap:RevenueRemainingPerformanceObligationExpectedTimingOfSatisfactionPeriod1" contextRef="current">P1Y</ix:nonNumeric>
            <ix:nonFraction name="us-gaap:RevenueRemainingPerformanceObligation" contextRef="later" unitRef="USD">600</ix:nonFraction>
            <ix:nonNumeric name="us-gaap:RevenueRemainingPerformanceObligationExpectedTimingOfSatisfactionPeriod1" contextRef="later">P2Y</ix:nonNumeric>
          </body>
        </html>
        """,
        document_name="timing.htm",
        filing={"report_date": "2026-03-31", "filing_date": "2026-05-01"},
    )
    values = {fact.concept_name: fact.value for fact in timing_facts}
    assert values["RemainingPerformanceObligation"] == 1000.0
    assert values["RemainingPerformanceObligationCurrent"] == 400.0

    text_document = """
        <html xmlns="http://www.w3.org/1999/xhtml"
              xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
              xmlns:xbrli="http://www.xbrl.org/2003/instance"
              xmlns:iso4217="http://www.xbrl.org/2003/iso4217"
              xmlns:us-gaap="http://fasb.org/us-gaap/2026">
          <body>
            <xbrli:context id="total"><xbrli:entity><xbrli:identifier scheme="cik">1</xbrli:identifier></xbrli:entity><xbrli:period><xbrli:instant>2026-03-31</xbrli:instant></xbrli:period></xbrli:context>
            <xbrli:unit id="USD"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
            <p>Remaining performance obligations were $1 billion. We expect to recognize 41% over the following 12 months.</p>
            <ix:nonFraction name="us-gaap:RevenueRemainingPerformanceObligation" contextRef="total" unitRef="USD">1000</ix:nonFraction>
          </body>
        </html>
        """
    text_facts = parse_facts(
        text_document,
        document_name="timing-text.htm",
        filing={"report_date": "2026-03-31", "filing_date": "2026-05-01"},
    )
    text_values = {fact.concept_name: fact.value for fact in text_facts}
    assert text_values["RemainingPerformanceObligationCurrent"] == pytest.approx(410.0)

    otis_facts = parse_facts(
        text_document,
        document_name="timing-text.htm",
        filing={"report_date": "2026-03-31", "filing_date": "2026-05-01"},
        ticker="OTIS",
    )
    assert all(fact.concept_name != "RemainingPerformanceObligationCurrent" for fact in otis_facts)

    preceding_percentage_document = """
        <p>We expect to recognize approximately 94% of our remaining performance
        obligations as revenue within the next twelve months.</p>
    """
    assert namespace["rpo_timing_percentage_from_text"](preceding_percentage_document) == pytest.approx(0.94)
    assert (
        namespace["rpo_timing_percentage_from_text"](
            preceding_percentage_document
            + "<p>We expect to recognize 25% of remaining performance obligations within the next year.</p>"
        )
        is None
    )

    explicit_amount_document = """
        <p>Remaining performance obligations were $1.0 billion. Of that amount,
        $410 million is expected to be recognized within the next 12 months.</p>
    """
    assert namespace["rpo_current_amount_from_text"](explicit_amount_document) == pytest.approx(410_000_000.0)
    assert (
        namespace["rpo_current_amount_from_text"](
            explicit_amount_document
            + "<p>We expect to recognize $275 million of remaining performance obligations within the next year.</p>"
        )
        is None
    )

    explicit_amount_facts = parse_facts(
        """
        <html xmlns="http://www.w3.org/1999/xhtml"
              xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
              xmlns:xbrli="http://www.xbrl.org/2003/instance"
              xmlns:iso4217="http://www.xbrl.org/2003/iso4217"
              xmlns:us-gaap="http://fasb.org/us-gaap/2026">
          <body>
            <xbrli:context id="total"><xbrli:entity><xbrli:identifier scheme="cik">1</xbrli:identifier></xbrli:entity><xbrli:period><xbrli:instant>2026-03-31</xbrli:instant></xbrli:period></xbrli:context>
            <xbrli:unit id="USD"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
            <p>Remaining performance obligations were $1.0 billion. Of that amount, $410 million is expected to be recognized within the next 12 months.</p>
            <ix:nonFraction name="us-gaap:RevenueRemainingPerformanceObligation" contextRef="total" unitRef="USD">1000000000</ix:nonFraction>
          </body>
        </html>
        """,
        document_name="timing-amount.htm",
        filing={"report_date": "2026-03-31", "filing_date": "2026-05-01"},
    )
    explicit_amount_values = {fact.concept_name: fact.value for fact in explicit_amount_facts}
    assert explicit_amount_values["RemainingPerformanceObligationCurrent"] == pytest.approx(410_000_000.0)

    archive_fact = namespace["ArchiveFact"]
    cross_source_facts = namespace["derive_cross_source_rpo_current_facts"](
        [
            archive_fact(
                taxonomy="sec-text",
                concept_name="RemainingPerformanceObligation",
                unit="USD",
                value=1_000.0,
                period_start="",
                period_end="2026-03-31",
                frame="text:current",
                decimals="",
                payload_json="{}",
                source_detail="sec_archive_text_table",
            ),
            archive_fact(
                taxonomy="sec-text",
                concept_name="RemainingPerformanceObligation",
                unit="USD",
                value=900.0,
                period_start="",
                period_end="2025-03-31",
                frame="text:comparison",
                decimals="",
                payload_json="{}",
                source_detail="sec_archive_text_table",
            ),
        ],
        document_text=preceding_percentage_document,
        document_name="rpo-cross-source.htm",
        filing={"report_date": "2026-03-31", "filing_date": "2026-05-01"},
        ticker="WAB",
    )
    derived = [fact for fact in cross_source_facts if fact.concept_name == "RemainingPerformanceObligationCurrent"]
    assert [(fact.period_end, fact.value) for fact in derived] == [("2026-03-31", pytest.approx(940.0))]


def test_exact_funded_backlog_prose_without_date_uses_filing_report_date() -> None:
    candidates = extract_machinery_prose_candidates(
        "<p>Our funded backlog stood at $2.4 billion.</p>",
        filing={"report_date": "2026-03-31", "filing_date": "2026-05-01"},
        company_currency="USD",
    )
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.concept_name == "FundedBacklog"
    assert candidate.value == 2_400_000_000.0
    assert candidate.period_end == "2026-03-31"
    assert candidate.candidate_status == "ACCEPTED"


def test_machinery_ccc_uses_aligned_ttm_windows() -> None:
    namespace = runpy.run_path(
        str(PROJECT_ROOT / "industrials" / "scripts" / "08_build_industrials_financial_features.py")
    )
    sanitize_proceeds = namespace["sanitize_gross_proceeds_ttm"]
    assert sanitize_proceeds("debt_issuance_proceeds", 125.0) == (125.0, "")
    assert sanitize_proceeds("debt_issuance_proceeds", -1.0) == (
        None,
        "ttm_debt_issuance_proceeds_negative_gross_proceeds_discarded",
    )

    def duration(metric: str, start: str, end: str, value: float) -> dict[str, object]:
        return {
            "canonical_metric": metric,
            "period_start": start,
            "period_end": end,
            "filing_date": end,
            "form_type": "10-K",
            "fiscal_period": "FY",
            "source_priority": 10,
            "value": value,
        }

    def instant(metric: str, end: str, value: float) -> dict[str, object]:
        return {
            "canonical_metric": metric,
            "period_start": "",
            "period_end": end,
            "filing_date": end,
            "form_type": "10-K",
            "fiscal_period": "FY",
            "source_priority": 10,
            "value": value,
        }

    rows = [
        duration("revenue", "2025-04-01", "2026-03-31", 1200.0),
        duration("gross_profit", "2025-04-01", "2026-03-31", 400.0),
        duration("revenue", "2024-04-01", "2025-03-31", 1000.0),
        duration("gross_profit", "2024-04-01", "2025-03-31", 300.0),
        instant("inventory", "2026-03-31", 200.0),
        instant("accounts_receivable", "2026-03-31", 150.0),
        instant("accounts_payable", "2026-03-31", 100.0),
        instant("inventory", "2025-03-31", 180.0),
        instant("accounts_receivable", "2025-03-31", 120.0),
        instant("accounts_payable", "2025-03-31", 90.0),
    ]
    current, change, flags = namespace["build_machinery_ccc"](rows)
    assert current is not None
    assert current.inventory_days == pytest.approx(200.0 / 800.0 * 365.0)
    assert current.days_sales_outstanding == pytest.approx(150.0 / 1200.0 * 365.0)
    assert current.days_payables_outstanding == pytest.approx(100.0 / 800.0 * 365.0)
    assert change is not None
    assert "ccc_cost_of_sales_ttm_derived_from_revenue_less_gross_profit" in flags


def test_invested_capital_never_infers_missing_debt_as_zero() -> None:
    namespace = runpy.run_path(
        str(PROJECT_ROOT / "industrials" / "scripts" / "08_build_industrials_financial_features.py")
    )
    capital_at_instant = namespace["capital_at_instant"]
    base = [
        {"canonical_metric": "equity", "period_end": ASOF, "value": 100.0, "source_priority": 1},
        {
            "canonical_metric": "cash_and_equivalents",
            "period_end": ASOF,
            "value": 20.0,
            "source_priority": 1,
        },
    ]
    assert capital_at_instant(base, ASOF) is None
    assert (
        capital_at_instant(
            [*base, {"canonical_metric": "debt_noncurrent", "period_end": ASOF, "value": 10.0, "source_priority": 1}],
            ASOF,
        )
        is None
    )
    assert capital_at_instant(
        [
            *base,
            {"canonical_metric": "debt_current", "period_end": ASOF, "value": 0.0, "source_priority": 1},
            {"canonical_metric": "debt_noncurrent", "period_end": ASOF, "value": 10.0, "source_priority": 1},
        ],
        ASOF,
    ) == pytest.approx(90.0)


def test_missing_debt_and_issuance_facts_preserve_cash_generative_signal(tmp_path: Path) -> None:
    namespace = runpy.run_path(
        str(PROJECT_ROOT / "industrials" / "scripts" / "08_build_industrials_financial_features.py")
    )

    def fact(metric: str, value: float, *, duration: bool) -> dict[str, object]:
        return {
            "canonical_metric": metric,
            "period_start": "2025-07-01" if duration else "",
            "period_end": "2026-06-30",
            "filing_date": "2026-07-01",
            "accepted_at": "2026-07-01T12:00:00Z",
            "accession_number": "TEST-1",
            "form_type": "10-K",
            "fiscal_year": 2026,
            "fiscal_period": "FY",
            "reporting_standard": "US_GAAP",
            "taxonomy": "us-gaap",
            "concept_name": metric,
            "unit": "USD",
            "value": value,
            "value_usd": value,
            "source_priority": 1,
        }

    rows = [
        fact("revenue", 1_000.0, duration=True),
        fact("gross_profit", 300.0, duration=True),
        fact("operating_income", 100.0, duration=True),
        fact("net_income", 80.0, duration=True),
        fact("operating_cash_flow", 120.0, duration=True),
        fact("capex", 40.0, duration=True),
        fact("assets", 900.0, duration=False),
        fact("liabilities", 400.0, duration=False),
        fact("equity", 500.0, duration=False),
        fact("cash_and_equivalents", 200.0, duration=False),
    ]
    db_path = tmp_path / "missing_facts.sqlite"
    with connect(db_path) as conn:
        init_db(conn)
        feature = namespace["build_feature_from_facts"](
            conn,
            ticker="NODEBT",
            asof=date(2026, 7, 9),
            source_id="sec_companyfacts",
            model_family="machinery",
            company={"ticker": "NODEBT", "country": "United States", "currency": "USD"},
            profile={
                "reporting_profile": "SEC_XBRL_US_GAAP",
                "reporting_standard": "US_GAAP",
                "financial_confidence": 0.9,
                "fallback_status": "none",
                "review_reason": "",
            },
            rows=rows,
            market_source_ids=["yahoo_finance_adjusted"],
            fx_max_staleness_days=7,
        )
    assert feature["total_debt"] is None
    assert feature["net_cash"] is None
    assert feature["equity_issuance_proceeds_ttm_usd"] is None
    assert feature["debt_issuance_proceeds_ttm_usd"] is None
    assert feature["cash_burn_ttm_usd"] == 0.0
    assert feature["capital_raise_dependence"] == 0.0
    assert "defaulted_zero" not in str(feature["canonical_quality"])


def test_machinery_archive_event_filings_have_separate_cap() -> None:
    namespace = runpy.run_path(
        str(PROJECT_ROOT / "industrials" / "scripts" / "07_sync_industrials_sec_fundamentals.py")
    )
    rows = [
        {"form_type": "8-K", "filing_date": "2026-07-01", "accession_number": "4"},
        {"form_type": "10-Q", "filing_date": "2026-06-01", "accession_number": "3"},
        {"form_type": "8-K", "filing_date": "2026-05-01", "accession_number": "2"},
        {"form_type": "10-K", "filing_date": "2026-04-01", "accession_number": "1"},
    ]
    selected = namespace["select_archive_filing_rows"](
        rows,
        max_filings=2,
        supplemental_forms={"8-K", "8-K/A"},
        max_supplemental_filings=1,
    )
    assert [row["form_type"] for row in selected].count("8-K") == 1
    assert {row["form_type"] for row in selected} >= {"10-Q", "10-K"}
    all_supplemental = namespace["select_archive_filing_rows"](
        rows,
        max_filings=2,
        supplemental_forms={"8-K", "8-K/A"},
        max_supplemental_filings=-1,
    )
    assert [row["form_type"] for row in all_supplemental].count("8-K") == 2
    config = load_yaml(MACHINERY_CONFIG)
    forms = {str(form).upper() for form in cfg_get(config, "sec_fundamentals.forms", [])}
    assert {"424B3", "424B4", "F-1", "F-4", "10-12B", "10-12G", "8-K"} <= forms
    assert cfg_get(config, "sec_archive.max_supplemental_filings_per_ticker") == -1
    assert cfg_get(config, "sec_archive.max_documents_per_filing") == 0


def test_reporting_profile_snapshots_are_point_in_time(tmp_path: Path) -> None:
    namespace = runpy.run_path(
        str(PROJECT_ROOT / "industrials" / "scripts" / "07_sync_industrials_sec_fundamentals.py")
    )
    classify = namespace["classify_reporting_profile"]
    db_path = tmp_path / "profiles.sqlite"
    now = utc_now()
    with connect(db_path) as conn, conn:
        init_db(conn)
        conn.execute(
            """
            INSERT INTO source_registry(
                source_id, stage, source_name, source_type, base_url, status, created_at, updated_at
            )
            VALUES ('sec_companyfacts', 'stage_4', 'SEC CompanyFacts', 'api',
                    'https://data.sec.gov', 'active', ?, ?)
            """,
            (now, now),
        )
        filings = [
            ("OLD", "2026-05-01", "2026-05-01T12:00:00Z", "10-Q"),
            ("FUTURE", "2026-07-15", "2026-07-15T12:00:00Z", "10-Q"),
        ]
        conn.executemany(
            """
            INSERT INTO fact_sec_filing(
                ticker, cik, source_id, accession_number, form_type, filing_date,
                accepted_at, created_at, updated_at
            )
            VALUES ('PIT', '0000000001', 'sec_companyfacts', ?, ?, ?, ?, ?, ?)
            """,
            [
                (accession, form_type, filing_date, accepted, now, now)
                for accession, filing_date, accepted, form_type in filings
            ],
        )
        facts = [
            ("OLD", "2026-05-01", "2026-05-01T12:00:00Z", "Assets", "assets", 100.0),
            ("OLD", "2026-05-01", "2026-05-01T12:00:00Z", "StockholdersEquity", "equity", 80.0),
            ("FUTURE", "2026-07-15", "2026-07-15T12:00:00Z", "Revenue", "revenue", 50.0),
        ]
        conn.executemany(
            """
            INSERT INTO fact_sec_xbrl_fact(
                ticker, cik, source_id, accession_number, form_type, filing_date,
                accepted_at, period_end, taxonomy, concept_name, canonical_metric,
                value, source_priority, created_at, updated_at
            )
            VALUES ('PIT', '0000000001', 'sec_companyfacts', ?, '10-Q', ?, ?, ?,
                    'us-gaap', ?, ?, ?, 1, ?, ?)
            """,
            [
                (accession, filing_date, accepted, filing_date, concept, metric, value, now, now)
                for accession, filing_date, accepted, concept, metric, value in facts
            ],
        )
        old_profile = classify(
            conn,
            ticker="PIT",
            cik="0000000001",
            country="United States",
            model_family="machinery",
            source_id="sec_companyfacts",
            asof=ASOF,
        )
        assert old_profile["reporting_profile"] == "SEC_XBRL_US_GAAP_PARTIAL"
        assert old_profile["latest_filing_date"] == "2026-05-01"
        future_profile = classify(
            conn,
            ticker="PIT",
            cik="0000000001",
            country="United States",
            model_family="machinery",
            source_id="sec_companyfacts",
            asof="2026-07-20",
        )
        assert future_profile["reporting_profile"] == "SEC_XBRL_US_GAAP"
        classify(
            conn,
            ticker="PIT",
            cik="0000000001",
            country="United States",
            model_family="machinery",
            source_id="sec_companyfacts",
            asof=ASOF,
        )
        current = conn.execute(
            "SELECT reporting_profile, profile_asof_date FROM dim_issuer_reporting_profile WHERE ticker='PIT'"
        ).fetchone()
        assert current is not None
        assert dict(current) == {
            "reporting_profile": "SEC_XBRL_US_GAAP",
            "profile_asof_date": "2026-07-20",
        }
        snapshot_count = conn.execute(
            "SELECT COUNT(*) FROM dim_issuer_reporting_profile_history WHERE ticker='PIT'"
        ).fetchone()[0]
        assert snapshot_count == 2


def test_reporting_profile_prefers_recent_core_taxonomy_over_stale_mixed_facts(
    tmp_path: Path,
) -> None:
    namespace = runpy.run_path(
        str(PROJECT_ROOT / "industrials" / "scripts" / "07_sync_industrials_sec_fundamentals.py")
    )
    classify = namespace["classify_reporting_profile"]
    db_path = tmp_path / "mixed_taxonomy.sqlite"
    now = utc_now()
    with connect(db_path) as conn, conn:
        init_db(conn)
        conn.execute(
            """
            INSERT INTO source_registry(
                source_id, stage, source_name, source_type, base_url, status, created_at, updated_at
            )
            VALUES ('sec_companyfacts', 'stage_4', 'SEC CompanyFacts', 'api',
                    'https://data.sec.gov', 'active', ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO fact_sec_filing(
                ticker, cik, source_id, accession_number, form_type, filing_date,
                accepted_at, created_at, updated_at
            )
            VALUES ('MIXED', '0000000002', 'sec_companyfacts', 'IFRS-NEW',
                    '20-F', '2026-04-01', '2026-04-01T12:00:00Z', ?, ?)
            """,
            (now, now),
        )
        facts = [
            ("US-OLD", "2019-12-31", "us-gaap", "Assets", "assets", 100.0),
            ("US-OLD", "2019-12-31", "us-gaap", "Revenue", "revenue", 50.0),
            ("IFRS-NEW", "2025-12-31", "ifrs-full", "Assets", "assets", 200.0),
            ("IFRS-NEW", "2025-12-31", "ifrs-full", "Revenue", "revenue", 120.0),
        ]
        conn.executemany(
            """
            INSERT INTO fact_sec_xbrl_fact(
                ticker, cik, source_id, accession_number, form_type, filing_date,
                accepted_at, period_end, taxonomy, concept_name, canonical_metric,
                value, source_priority, created_at, updated_at
            )
            VALUES ('MIXED', '0000000002', 'sec_companyfacts', ?, '20-F',
                    '2026-04-01', '2026-04-01T12:00:00Z', ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            [
                (accession, period_end, taxonomy, concept, metric, value, now, now)
                for accession, period_end, taxonomy, concept, metric, value in facts
            ],
        )
        profile = classify(
            conn,
            ticker="MIXED",
            cik="0000000002",
            country="Chile",
            model_family="machinery",
            source_id="sec_companyfacts",
            asof="2026-07-09",
        )
        stored = conn.execute(
            """
            SELECT reporting_profile, primary_taxonomy
            FROM dim_issuer_reporting_profile
            WHERE ticker='MIXED' AND model_family='machinery'
            """
        ).fetchone()
    assert profile["reporting_profile"] == "SEC_XBRL_IFRS"
    assert stored is not None
    assert dict(stored) == {
        "reporting_profile": "SEC_XBRL_IFRS",
        "primary_taxonomy": "ifrs-full",
    }


def test_stale_cik_purge_removes_derived_machinery_rows_only(tmp_path: Path) -> None:
    namespace = runpy.run_path(
        str(PROJECT_ROOT / "industrials" / "scripts" / "07_sync_industrials_sec_fundamentals.py")
    )
    db_path = tmp_path / "stale_cik.sqlite"
    now = utc_now()
    with connect(db_path) as conn, conn:
        init_db(conn)
        conn.execute(
            """
            INSERT INTO source_registry(
                source_id, stage, source_name, source_type, base_url, status, created_at, updated_at
            )
            VALUES ('sec_companyfacts', 'stage_4', 'SEC CompanyFacts', 'api',
                    'https://data.sec.gov', 'active', ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO fact_sec_xbrl_fact_raw(
                fact_key, ticker, cik, source_id, taxonomy, concept_name,
                created_at, updated_at
            )
            VALUES ('old-fact', 'REUSE', '0000000001', 'sec_companyfacts',
                    'us-gaap', 'Assets', ?, ?)
            """,
            (now, now),
        )
        conn.executemany(
            """
            INSERT INTO fact_financial_statement_canonical(
                ticker, source_id, model_family, canonical_metric, period_end,
                accession_number, unit, created_at, updated_at
            )
            VALUES ('REUSE', 'sec_companyfacts', ?, 'assets', '2026-03-31',
                    'OLD', 'USD', ?, ?)
            """,
            [("machinery", now, now), ("defense", now, now)],
        )
        namespace["purge_stale_cik_artifacts"](
            conn,
            ticker="REUSE",
            cik="0000000002",
            submissions_source_id="sec_companyfacts",
            companyfacts_source_id="sec_companyfacts",
            model_family="machinery",
        )
        machinery_count = conn.execute(
            "SELECT COUNT(*) FROM fact_financial_statement_canonical WHERE ticker='REUSE' AND model_family='machinery'"
        ).fetchone()[0]
        defense_count = conn.execute(
            "SELECT COUNT(*) FROM fact_financial_statement_canonical WHERE ticker='REUSE' AND model_family='defense'"
        ).fetchone()[0]
        assert machinery_count == 0
        assert defense_count == 1


def test_undefined_roic_and_leverage_are_classified_not_applicable(tmp_path: Path) -> None:
    namespace = runpy.run_path(
        str(PROJECT_ROOT / "industrials" / "scripts" / "08_build_industrials_financial_features.py")
    )
    db_path = tmp_path / "industrials.sqlite"
    with connect(db_path) as conn:
        init_db(conn)
        availability = namespace["classify_financial_metric_availability"](
            conn,
            feature={
                "revenue": 100.0,
                "reporting_profile": "SEC_XBRL_US_GAAP",
                "data_quality_status": "complete",
                "roic_not_meaningful_flag": 1,
                "negative_ebitda_leverage_flag": 1,
            },
            rows=[],
            company={"ticker": "TEST", "development_stage": "operating"},
            source_id="sec_companyfacts",
            model_family="machinery",
            asof=date(2026, 7, 9),
        )
    by_metric = {row["metric_name"]: row for row in availability}
    assert by_metric["roic"]["availability_status"] == "NOT_APPLICABLE"
    assert by_metric["net_debt_to_ebitda"]["availability_status"] == "NOT_APPLICABLE"
    assert by_metric["funded_backlog"]["availability_status"] == "NOT_APPLICABLE"
    assert by_metric["backlog_yoy_growth"]["availability_status"] == "NOT_APPLICABLE"
    assert by_metric["backlog_to_revenue"]["availability_status"] == "NOT_APPLICABLE"


def test_historical_feature_reports_are_date_scoped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = runpy.run_path(
        str(
            PROJECT_ROOT
            / "industrials"
            / "machinery"
            / "scripts"
            / "18_backfill_machinery_historical_dashboard_reports.py"
        )
    )
    calls: list[list[str]] = []

    def capture_run(command: list[str], **_: object) -> None:
        calls.append(command)

    monkeypatch.setattr(namespace["subprocess"], "run", capture_run)
    report_root = tmp_path / "historical_backfill" / "stage_reports" / ASOF
    namespace["rebuild_features"](
        config_path=MACHINERY_CONFIG,
        db_path=tmp_path / "industrials.sqlite",
        asof=ASOF,
        report_root=report_root,
    )

    assert len(calls) == 4
    flattened = [str(item) for command in calls for item in command]
    assert str(report_root / "reporting_profile_snapshot.csv") in flattened
    assert str(report_root / "market_feature_coverage.csv") in flattened
    assert str(report_root / "financial_feature_coverage.csv") in flattened
    assert str(report_root / "financial_metric_availability.csv") in flattened
    assert str(report_root / "positioning_import_coverage.csv") in flattened
    assert "--availability-output-csv" in flattened
    assert "--suppress-data-quality-issues" in flattened
    assert "--profiles-only" in flattened
    assert "--features-only" in flattened

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE dim_universe_membership(
            ticker TEXT,
            model_family TEXT,
            start_date TEXT,
            end_date TEXT
        );
        CREATE TABLE fact_sec_filing(
            ticker TEXT,
            accepted_at TEXT,
            filing_date TEXT
        );
        CREATE TABLE dim_issuer_reporting_profile_history(
            ticker TEXT,
            model_family TEXT,
            profile_asof_date TEXT
        );
        INSERT INTO dim_universe_membership VALUES
            ('LIVE', 'machinery', '2019-01-02', NULL),
            ('FUTURE', 'machinery', '2020-01-02', NULL);
        INSERT INTO fact_sec_filing VALUES
            ('LIVE', '2019-01-03T12:00:00Z', '2019-01-03'),
            ('FUTURE', '2019-01-03T12:00:00Z', '2019-01-03');
        """
    )
    profile_dates = namespace["profile_rebuild_tickers"](
        conn,
        dates=["2019-01-02", "2019-01-03", "2020-01-02"],
    )
    assert profile_dates["2019-01-03"] == {"LIVE"}
    assert profile_dates["2020-01-02"] == {"FUTURE"}
    conn.close()


def test_xbrl_concept_seed_fast_path_detects_and_repairs_drift(tmp_path: Path) -> None:
    with connect(tmp_path / "seed.sqlite") as conn:
        init_db(conn)
        assert xbrl_concept_seed_is_current(conn)
        before = conn.total_changes
        seed_xbrl_concept_map(conn)
        assert conn.total_changes == before
        with conn:
            conn.execute(
                """
                UPDATE dim_xbrl_concept_map
                SET priority = priority + 1
                WHERE taxonomy = 'us-gaap'
                  AND concept_name = 'Revenues'
                  AND canonical_metric = 'revenue'
                """
            )
        assert not xbrl_concept_seed_is_current(conn)
        seed_xbrl_concept_map(conn)
        assert xbrl_concept_seed_is_current(conn)


def test_atomic_report_writer_retries_transient_onedrive_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "report.json"
    real_replace = reports.os.replace
    attempts = 0

    def flaky_replace(source: Path, destination: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError("transient OneDrive lock")
        real_replace(source, destination)

    monkeypatch.setattr(reports.os, "replace", flaky_replace)
    reports.write_text_atomic(target, '{"acceptance":"PASS"}\n')
    assert attempts == 3
    assert target.read_text(encoding="utf-8") == '{"acceptance":"PASS"}\n'


def test_combined_historical_coverage_reconciles_active_and_delisted(
    tmp_path: Path,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE dim_universe_membership(
            ticker TEXT,
            model_family TEXT,
            start_date TEXT,
            end_date TEXT,
            membership_status TEXT
        );
        CREATE TABLE dim_delisted_calibration_seed(
            ticker TEXT,
            model_family TEXT,
            exit_year INTEGER
        );
        INSERT INTO dim_universe_membership VALUES
            ('LIVE', 'machinery', '2019-01-02', NULL, 'active'),
            ('OLD', 'machinery', '2019-01-02', '2019-01-03', 'historical_delisted');
        INSERT INTO dim_delisted_calibration_seed VALUES
            ('OLD', 'machinery', 2019),
            ('MISS', 'machinery', 2020),
            ('EARLY', 'machinery', 2018);
        """
    )
    dashboard_root = tmp_path / "dashboard"
    fields = [
        "asof_date",
        "ticker",
        "membership_status",
        "rank_ready_flag",
        "stage11_calibration_input_eligible_flag",
        "market_feature_asof_date",
        "financial_feature_asof_date",
        "positioning_feature_asof_date",
        "financial_metric_classified_fraction",
        "financial_metric_reported_count",
        "financial_metric_proxy_count",
        "financial_metric_unavailable_count",
        "survivorship_corrected_panel_flag",
        *AVAILABILITY_STATUS_FIELDS,
    ]
    for asof in ("2019-01-02", "2019-01-03"):
        rows = []
        for ticker, membership_status in (("LIVE", "active"), ("OLD", "historical_delisted")):
            row = {
                "asof_date": asof,
                "ticker": ticker,
                "membership_status": membership_status,
                "rank_ready_flag": "1",
                "stage11_calibration_input_eligible_flag": "1",
                "market_feature_asof_date": asof,
                "financial_feature_asof_date": asof,
                "positioning_feature_asof_date": asof,
                "financial_metric_classified_fraction": "1",
                "financial_metric_reported_count": str(len(AVAILABILITY_STATUS_FIELDS)),
                "financial_metric_proxy_count": "0",
                "financial_metric_unavailable_count": "0",
                "survivorship_corrected_panel_flag": "1",
            }
            row.update({field: "REPORTED" for field in AVAILABILITY_STATUS_FIELDS})
            rows.append(row)
        output_dir = dashboard_root / asof
        output_dir.mkdir(parents=True)
        write_csv_atomic(
            output_dir / "machinery_stage11_survivorship_calibration_panel.csv",
            fields,
            rows,
        )
    report_root = tmp_path / "history"
    summary = build_combined_historical_coverage(
        conn,
        dates=["2019-01-02", "2019-01-03"],
        dashboard_root=dashboard_root,
        report_root=report_root,
        start_date="2019-01-02",
        end_date="2019-01-03",
    )
    assert summary["acceptance"] == "PASS"
    assert summary["published_observation_count"] == 4
    assert summary["delisted_seed_count"] == 3
    assert summary["delisted_resolved_membership_count"] == 1
    assert summary["delisted_unresolved_tickers"] == ["MISS"]
    metric_rows = read_rows(report_root / "machinery_combined_historical_metric_coverage.csv")
    combined_orders = next(
        row for row in metric_rows if row["universe_class"] == "combined" and row["metric_name"] == "orders"
    )
    assert combined_orders["observation_count"] == "4"
    assert combined_orders["coverage_fraction"] == "1.00000000"
    conn.close()


def test_machinery_reporting_profile_carries_forward_without_lookahead() -> None:
    namespace = runpy.run_path(
        str(PROJECT_ROOT / "industrials" / "scripts" / "08_build_industrials_financial_features.py")
    )
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE dim_issuer_reporting_profile_history(
            ticker TEXT,
            model_family TEXT,
            profile_asof_date TEXT,
            reporting_profile TEXT,
            reporting_standard TEXT,
            financial_confidence REAL,
            usable_xbrl_flag INTEGER
        );
        INSERT INTO dim_issuer_reporting_profile_history VALUES
            ('CAT', 'machinery', '2020-02-14', 'EARLIER', 'US_GAAP', 0.8, 1),
            ('CAT', 'machinery', '2020-05-01', 'FUTURE', 'US_GAAP', 0.9, 1);
        """
    )
    profile = namespace["load_profile"](
        conn,
        ticker="CAT",
        model_family="machinery",
        company={"ticker": "CAT", "country": "US"},
        source_id="sec_companyfacts",
        asof=date(2020, 3, 31),
    )
    assert profile["reporting_profile"] == "EARLIER"
    missing = namespace["load_profile"](
        conn,
        ticker="CAT",
        model_family="machinery",
        company={"ticker": "CAT", "country": "US"},
        source_id="sec_companyfacts",
        asof=date(2020, 1, 31),
    )
    assert missing["review_reason"] == "reporting_profile_snapshot_missing_for_asof"
    conn.close()


def test_scoring_enforces_financial_policy_and_recomputes_staleness(tmp_path: Path) -> None:
    db_path = load_machinery_db(tmp_path)
    seed_scoring_features(db_path)
    config = load_yaml(MACHINERY_CONFIG)
    policies = load_eligibility_policy(
        PROJECT_ROOT / "industrials" / "machinery" / "system_csvs" / "machinery_scoring_eligibility_policy.csv",
        asof=ASOF,
    )
    weights = cfg_get(config, "machinery_scoring.component_weights", {})
    assert isinstance(weights, dict)
    with connect(db_path) as conn, conn:
        primary_market_row = conn.execute(
            """
            SELECT * FROM feature_market_technical
            WHERE model_family = 'machinery' AND source_id = 'yahoo_finance_adjusted'
            ORDER BY ticker LIMIT 1
            """
        ).fetchone()
        assert primary_market_row is not None
        fallback_values = dict(primary_market_row)
        fallback_values["source_id"] = "norgate_us_equities_total_return"
        fallback_values["latest_adj_close"] = 999.0
        columns = list(fallback_values)
        conn.execute(
            f"INSERT INTO feature_market_technical({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
            tuple(fallback_values[column] for column in columns),
        )
        baseline = build_scoring_feature_rows(
            conn,
            asof=ASOF,
            eligibility_policies=policies,
            market_source_priority=(
                "yahoo_finance_adjusted",
                "norgate_us_equities_total_return",
            ),
            component_weights=weights,
            min_score_confidence=0.40,
            max_staleness_days=7,
            min_avg_dollar_volume=5_000_000.0,
        )
        rank_ready = [row for row in baseline if row["rank_ready_flag"] == "1"]
        assert rank_ready
        primary_ticker_row = next(row for row in baseline if row["ticker"] == str(primary_market_row["ticker"]))
        assert primary_ticker_row["market_feature_source_id"] == "yahoo_finance_adjusted"
        assert primary_ticker_row["latest_adj_close"] != "999"
        blocked_ticker = rank_ready[0]["ticker"]
        conn.execute(
            """
            UPDATE feature_financial_statement
            SET reporting_profile = 'SEC_ARCHIVE_TEXT_TABLE'
            WHERE ticker = ? AND model_family = 'machinery' AND asof_date = ?
            """,
            (blocked_ticker, ASOF),
        )
        policy_blocked = build_scoring_feature_rows(
            conn,
            asof=ASOF,
            eligibility_policies=policies,
            component_weights=weights,
            min_score_confidence=0.40,
            max_staleness_days=7,
            min_avg_dollar_volume=5_000_000.0,
        )
        blocked_row = next(row for row in policy_blocked if row["ticker"] == blocked_ticker)
        assert blocked_row["rank_ready_flag"] == "0"
        assert "financial_policy_not_rank_ready" in blocked_row["rank_ready_reason"]
        stale_rows = build_scoring_feature_rows(
            conn,
            asof="2026-07-19",
            eligibility_policies=policies,
            component_weights=weights,
            min_score_confidence=0.40,
            max_staleness_days=7,
            min_avg_dollar_volume=5_000_000.0,
        )
    old_market_rows = [row for row in stale_rows if row["market_feature_asof_date"] == ASOF]
    assert old_market_rows
    assert {row["stale_days"] for row in old_market_rows} == {"10"}
    assert all("stale_market_features" in row["rank_ready_reason"] for row in old_market_rows)


def test_machinery_strict_registration_statement_parser() -> None:
    namespace = runpy.run_path(
        str(PROJECT_ROOT / "industrials" / "scripts" / "07_sync_industrials_sec_fundamentals.py")
    )
    label_concept = namespace["registration_text_table_label_concept"]
    assert label_concept("Equipment Order Intake") == ("Orders", "duration")
    assert label_concept("Orders (15)") == ("Orders", "duration")
    assert label_concept("Net cash flows provided by (used in) operating activities") == (
        "OperatingCashFlow",
        "duration",
    )
    assert label_concept("Net sales growth rate") is None
    assert label_concept("Gross Profit Margin") is None
    assert label_concept("Income (loss) from continuing operations before income taxes") == (
        "PretaxIncome",
        "duration",
    )
    assert label_concept("Current maturities of long-term debt (Note 10)") == (
        "DebtCurrentComponent",
        "instant",
    )

    parse_tables = namespace["parse_archive_text_table_facts"]
    filing = {
        "report_date": "2026-03-31",
        "filing_date": "2026-05-01",
        "form_type": "S-1/A",
    }
    mixed_facts = parse_tables(
        """
        <p>Issuer Consolidated Statements of Operations</p>
        <table>
          <tr><th>Three Months Ended March 31</th><th>Years Ended December 31</th></tr>
          <tr><th>(in millions of $)</th><th>2026</th><th>2025</th><th>2025</th><th>2024</th><th>2023</th></tr>
          <tr><td>Net sales</td><td>668.6</td><td>494.0</td><td>2,636.8</td><td>2,159.1</td><td>2,015.0</td></tr>
          <tr><td>Operating income</td><td>63.1</td><td>74.9</td><td>346.5</td><td>297.8</td><td>238.7</td></tr>
        </table>
        """,
        document_name="issuer-registration.htm",
        filing=filing,
        company_currency="USD",
        strict_registration_statements=True,
    )
    revenue_periods = {(fact.period_end, fact.value) for fact in mixed_facts if fact.concept_name == "Revenue"}
    assert revenue_periods == {
        ("2026-03-31", 668_600_000.0),
        ("2025-03-31", 494_000_000.0),
        ("2025-12-31", 2_636_800_000.0),
        ("2024-12-31", 2_159_100_000.0),
        ("2023-12-31", 2_015_000_000.0),
    }

    actual_facts = parse_tables(
        """
        <p>Issuer Consolidated Statements of Operations</p>
        <table>
          <tr><th>Actual</th><th>Pro Forma (Unaudited)</th></tr>
          <tr><th>Years Ended December 31</th><th>Years Ended December 31</th></tr>
          <tr><th>(in millions of $)</th><th>2025</th><th>2024</th><th>2023</th><th>2025</th></tr>
          <tr><td>Net sales</td><td>3,340.1</td><td>2,624.7</td><td>2,556.2</td><td>3,518.9</td></tr>
        </table>
        """,
        document_name="issuer-actual-pro-forma.htm",
        filing=filing,
        company_currency="USD",
        strict_registration_statements=True,
    )
    assert [(fact.period_end, fact.value) for fact in actual_facts] == [
        ("2025-12-31", 3_340_100_000.0),
        ("2024-12-31", 2_624_700_000.0),
        ("2023-12-31", 2_556_200_000.0),
    ]

    balance_facts = parse_tables(
        """
        <p>Issuer Condensed Consolidated Balance Sheets</p>
        <table>
          <tr><th>(in millions of $)</th><th>Note</th><th>March 31, 2026</th><th>December 31, 2025</th></tr>
          <tr><td>Inventories</td><td>12</td><td>815.2</td><td>601.2</td></tr>
          <tr><td>Current maturities of long-term debt (Note 10)</td><td>27.7</td><td>27.8</td></tr>
          <tr><td>Long-term debt (Note 10)</td><td>5,621.0</td><td>5,622.6</td></tr>
          <tr><td>Total assets</td><td>5,290.1</td><td>4,902.5</td></tr>
        </table>
        """,
        document_name="issuer-balance.htm",
        filing=filing,
        company_currency="USD",
        strict_registration_statements=True,
    )
    assert [(fact.period_end, fact.value) for fact in balance_facts if fact.concept_name == "Inventory"] == [
        ("2026-03-31", 815_200_000.0),
        ("2025-12-31", 601_200_000.0),
    ]
    assert [(fact.period_end, fact.value) for fact in balance_facts if fact.concept_name == "DebtTotal"] == [
        ("2026-03-31", 5_648_700_000.0),
        ("2025-12-31", 5_650_400_000.0),
    ]

    pro_forma_facts = parse_tables(
        """
        <p>Unaudited Pro Forma Condensed Combined Statements of Operations</p>
        <table>
          <tr><th>(in millions of $)</th><th>2025</th><th>2024</th></tr>
          <tr><td>Net sales</td><td>9,999</td><td>8,999</td></tr>
        </table>
        """,
        document_name="issuer-pro-forma.htm",
        filing=filing,
        company_currency="USD",
        strict_registration_statements=True,
    )
    assert pro_forma_facts == []

    financial_namespace = runpy.run_path(
        str(PROJECT_ROOT / "industrials" / "scripts" / "08_build_industrials_financial_features.py")
    )
    rows = [
        {"taxonomy": "us-gaap", "canonical_metric": "assets", "value": 0.0},
        {"taxonomy": "sec-text", "canonical_metric": "assets", "value": 8_309_200_000.0},
    ]
    filtered, taxonomy = financial_namespace["rows_for_reporting_profile"](
        rows,
        {"reporting_profile": "SEC_ARCHIVE_TEXT_TABLE"},
        model_family="machinery",
    )
    assert taxonomy == "sec-text"
    assert filtered == [rows[1]]
    archive_supplemental = [
        *rows,
        {
            "taxonomy": "dedicated-parser",
            "canonical_metric": "orders",
            "value": 12.0,
        },
        {
            "taxonomy": "dedicated-parser",
            "canonical_metric": "assets",
            "value": 11.0,
        },
    ]
    filtered, taxonomy = financial_namespace["rows_for_reporting_profile"](
        archive_supplemental,
        {"reporting_profile": "SEC_ARCHIVE_TEXT_TABLE"},
        model_family="machinery",
    )
    assert taxonomy == "sec-text"
    assert filtered == [rows[1], archive_supplemental[2]]
    defense_rows, defense_taxonomy = financial_namespace["rows_for_reporting_profile"](
        rows,
        {"reporting_profile": "SEC_ARCHIVE_TEXT_TABLE"},
        model_family="defense",
    )
    assert defense_taxonomy == "sec-text"
    assert defense_rows == [rows[1]]

    supplemental_rows = [
        {"taxonomy": "us-gaap", "canonical_metric": "assets", "value": 10.0},
        {"taxonomy": "sec-text", "canonical_metric": "assets", "value": 999.0},
        {"taxonomy": "sec-footnote", "canonical_metric": "reported_backlog", "value": 50.0},
        {"taxonomy": "sec-text", "canonical_metric": "orders", "value": 25.0},
        {
            "taxonomy": "dedicated-parser",
            "canonical_metric": "remaining_performance_obligation",
            "value": 75.0,
        },
        {
            "taxonomy": "dedicated-parser",
            "canonical_metric": "debt_total",
            "value": 0.0,
        },
    ]
    machinery_rows, machinery_taxonomy = financial_namespace["rows_for_reporting_profile"](
        supplemental_rows,
        {"reporting_profile": "SEC_XBRL_US_GAAP"},
        model_family="machinery",
    )
    assert machinery_taxonomy == "us-gaap"
    assert supplemental_rows[0] in machinery_rows
    assert supplemental_rows[1] not in machinery_rows
    assert supplemental_rows[2:] == machinery_rows[1:]
    defense_rows, defense_taxonomy = financial_namespace["rows_for_reporting_profile"](
        supplemental_rows,
        {"reporting_profile": "SEC_XBRL_US_GAAP"},
        model_family="defense",
    )
    assert defense_taxonomy == "us-gaap"
    assert defense_rows == [
        supplemental_rows[0],
        *supplemental_rows[2:],
    ]


def test_machinery_ttm_uses_four_unlabeled_discrete_quarters() -> None:
    financial_namespace = runpy.run_path(
        str(PROJECT_ROOT / "industrials" / "scripts" / "08_build_industrials_financial_features.py")
    )
    rows = [
        {
            "canonical_metric": "orders",
            "value": 75.0,
            "period_start": "2025-01-01",
            "period_end": "2025-12-31",
            "fiscal_period": "FY",
            "form_type": "10-K",
        },
        *[
            {
                "canonical_metric": "orders",
                "value": value,
                "period_start": start,
                "period_end": end,
                "fiscal_period": "",
                "form_type": "10-Q",
            }
            for value, start, end in (
                (20.0, "2025-07-01", "2025-09-30"),
                (21.0, "2025-10-01", "2025-12-31"),
                (25.0, "2026-01-01", "2026-03-31"),
                (23.0, "2026-04-01", "2026-06-30"),
            )
        ],
    ]
    result = financial_namespace["ttm_metric_result"](rows, "orders")
    assert result.value == pytest.approx(89.0)
    assert result.window_start.isoformat() == "2025-07-01"
    assert result.window_end.isoformat() == "2026-06-30"


def test_machinery_book_to_bill_uses_reviewed_aligned_annual_window() -> None:
    financial_namespace = runpy.run_path(
        str(PROJECT_ROOT / "industrials" / "scripts" / "08_build_industrials_financial_features.py")
    )
    rows = [
        {
            "canonical_metric": "orders",
            "taxonomy": "dedicated-parser",
            "value": 120.0,
            "period_start": "2025-01-01",
            "period_end": "2025-12-31",
            "fiscal_period": "FY",
            "form_type": "10-K",
        },
        {
            "canonical_metric": "revenue",
            "taxonomy": "us-gaap",
            "value": 100.0,
            "period_start": "2025-01-01",
            "period_end": "2025-12-31",
            "fiscal_period": "FY",
            "form_type": "10-K",
        },
        {
            "canonical_metric": "revenue",
            "taxonomy": "us-gaap",
            "value": 30.0,
            "period_start": "2026-01-01",
            "period_end": "2026-03-31",
            "fiscal_period": "Q1",
            "form_type": "10-Q",
        },
        {
            "canonical_metric": "revenue",
            "taxonomy": "us-gaap",
            "value": 20.0,
            "period_start": "2025-01-01",
            "period_end": "2025-03-31",
            "fiscal_period": "Q1",
            "form_type": "10-Q",
        },
    ]
    orders = financial_namespace["ttm_metric_result"](rows, "orders")
    latest_revenue = financial_namespace["ttm_metric_result"](
        rows,
        "revenue",
    )
    value, quality = financial_namespace["calculate_book_to_bill"](
        rows,
        orders=orders,
        revenue=latest_revenue,
    )
    assert value == pytest.approx(1.2)
    assert quality == "book_to_bill_aligned_to_latest_reported_orders_window"

    stale_orders = financial_namespace["TtmResult"](
        120.0,
        "",
        window_start=date(2024, 1, 1),
        window_end=date(2024, 12, 31),
    )
    value, quality = financial_namespace["calculate_book_to_bill"](
        rows,
        orders=stale_orders,
        revenue=latest_revenue,
    )
    assert value is None
    assert quality == "stale_orders_window_book_to_bill"

    rows[0]["taxonomy"] = "sec-footnote"
    value, quality = financial_namespace["calculate_book_to_bill"](
        rows,
        orders=orders,
        revenue=latest_revenue,
    )
    assert value is None
    assert quality == "period_mismatch_book_to_bill"


def test_machinery_loaders_preserve_defense_rows(tmp_path: Path) -> None:
    db_path = load_machinery_db(tmp_path)
    insert_defense_sentinel(db_path)
    run_script(
        "industrials/machinery/scripts/01_load_machinery_universe.py",
        "--config",
        str(MACHINERY_CONFIG),
        "--db",
        str(db_path),
    )
    run_script(
        "industrials/machinery/scripts/01b_load_machinery_historical_membership.py",
        "--config",
        str(MACHINERY_CONFIG),
        "--db",
        str(db_path),
    )
    run_script(
        "industrials/machinery/scripts/02_validate_machinery_universe.py",
        "--config",
        str(MACHINERY_CONFIG),
        "--db",
        str(db_path),
    )
    with connect(db_path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM dim_industrials_taxonomy WHERE model_family='defense' AND ticker='DEFTEST'"
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM dim_universe_membership WHERE model_family='defense' AND ticker='DEFTEST'"
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM dim_industrials_taxonomy WHERE model_family='machinery'").fetchone()[0]
            == 136
        )


def test_machinery_end_to_end_smoke(tmp_path: Path) -> None:
    db_path = load_machinery_db(tmp_path)
    seed_scoring_features(db_path)
    feature_path = tmp_path / "machinery_scoring_features.csv"
    score_path = tmp_path / "machinery_scores.csv"
    sector_root = tmp_path / "s"
    output_dir = sector_root / "industrials" / "machinery" / "dashboard" / ASOF
    run_script(
        "industrials/machinery/scripts/06a_build_machinery_scoring_features.py",
        "--config",
        str(MACHINERY_CONFIG),
        "--db",
        str(db_path),
        "--asof",
        ASOF,
        "--output-csv",
        str(feature_path),
        "--force",
    )
    run_script(
        "industrials/machinery/scripts/06a_validate_machinery_scoring_features.py",
        "--config",
        str(MACHINERY_CONFIG),
        "--db",
        str(db_path),
        "--asof",
        ASOF,
        "--input-csv",
        str(feature_path),
        "--output-json",
        str(tmp_path / "machinery_scoring_feature_validation.json"),
    )
    scoring_rows = read_rows(feature_path)
    seeded_scoring_rows = [row for row in scoring_rows if row["financial_feature_source_id"]]
    assert seeded_scoring_rows
    assert {row["financial_fallback_status"] for row in seeded_scoring_rows} == {"none"}
    assert {row["market_data_quality"] for row in seeded_scoring_rows} == {"complete"}
    assert {row["positioning_quality"] for row in seeded_scoring_rows} == {"complete"}
    assert {row["latest_bar_date"] for row in seeded_scoring_rows} == {ASOF}
    development_totals = {
        int(row["score_input_total_count"]) for row in scoring_rows if row["development_stage"] == "development_stage"
    }
    operating_totals = {
        int(row["score_input_total_count"]) for row in scoring_rows if row["development_stage"] == "operating"
    }
    assert len(development_totals) == len(operating_totals) == 1
    assert next(iter(development_totals)) > next(iter(operating_totals))
    populated_backlog_rows = [row for row in scoring_rows if row["backlog_to_revenue"]]
    assert populated_backlog_rows
    with connect(db_path) as conn:
        expected_backlog_ratios = {
            str(row["ticker"]): float(row["backlog_to_revenue"])
            for row in conn.execute(
                """
                SELECT ticker, backlog_to_revenue
                FROM feature_financial_statement
                WHERE model_family = 'machinery' AND asof_date = ?
                  AND backlog_to_revenue IS NOT NULL
                """,
                (ASOF,),
            ).fetchall()
        }
    assert all(
        abs(float(row["backlog_to_revenue"]) - expected_backlog_ratios[row["ticker"]]) < 1e-9
        for row in populated_backlog_rows
    )
    run_script(
        "industrials/machinery/scripts/08a_audit_machinery_financial_metrics.py",
        "--config",
        str(MACHINERY_CONFIG),
        "--db",
        str(db_path),
        "--asof",
        ASOF,
        "--output-dir",
        str(tmp_path / "financial_metric_audit"),
    )
    run_script(
        "industrials/machinery/scripts/10_build_machinery_calibrated_scores.py",
        "--config",
        str(MACHINERY_CONFIG),
        "--db",
        str(db_path),
        "--asof",
        ASOF,
        "--input-csv",
        str(feature_path),
        "--output-csv",
        str(score_path),
        "--force",
    )
    run_script(
        "industrials/machinery/scripts/10b_publish_machinery_dashboard_reports.py",
        "--config",
        str(MACHINERY_CONFIG),
        "--asof",
        ASOF,
        "--input-csv",
        str(score_path),
        "--output-dir",
        str(output_dir),
    )
    run_script(
        "industrials/machinery/scripts/10b_validate_machinery_dashboard_reports.py",
        "--config",
        str(MACHINERY_CONFIG),
        "--asof",
        ASOF,
        "--input-dir",
        str(output_dir),
    )
    run_script(
        "industrials/machinery/scripts/20_validate_machinery_portfolio_adapter.py",
        "--asof",
        ASOF,
        "--sector-output-root",
        str(sector_root),
    )
    rank_rows = read_rows(output_dir / "machinery_final_rank_table.csv")
    assert len(rank_rows) == 114
    assert set(FINAL_RANK_FIELDS) == set(rank_rows[0])
    assert set(PORTFOLIO_REQUIRED_FIELDS).issubset(rank_rows[0])
    assert {row["financial_metric_availability_asof_date"] for row in rank_rows} == {ASOF}
    for metric_name in required_metric_names():
        status_field = f"{metric_name}_availability_status"
        assert {row[status_field] for row in rank_rows} == {"NOT_DISCLOSED"}
    assert sum(row["rank_ready_flag"] == "1" for row in rank_rows) == 12
    assert all(row["portfolio_candidate_gate"] == "0" for row in rank_rows)
    bad_availability_row = dict(rank_rows[0])
    bad_availability_row["financial_metric_reported_count"] = "99"
    assert any(
        "financial_metric_reported_count=99" in error
        for error in validate_metric_availability_contract([bad_availability_row], asof=ASOF)
    )

    manifest = load_yaml(output_dir / "machinery_final_rank_table_manifest.json")
    assert manifest["acceptance"] == "PASS"
    assert manifest["sidecar_calibration_eligible_count"] == 12
    validator_namespace = runpy.run_path(
        str(PROJECT_ROOT / "industrials" / "machinery" / "scripts" / "10b_validate_machinery_dashboard_reports.py")
    )
    manifest_path = output_dir / "machinery_final_rank_table_manifest.json"
    original_manifest_text = manifest_path.read_text(encoding="utf-8")
    tampered_manifest = json.loads(original_manifest_text)
    tampered_manifest["row_count"] = 1
    manifest_path.write_text(json.dumps(tampered_manifest), encoding="utf-8")
    tamper_errors, _ = validator_namespace["validate_dashboard_artifacts"](output_dir, asof=ASOF)
    assert "manifest row_count=1 expected=114" in tamper_errors
    manifest_path.write_text(original_manifest_text, encoding="utf-8")
    published = read_rows(output_dir / "machinery_final_rank_table.csv")
    assert set(FINAL_RANK_FIELDS) == set(published[0])

    adapted = run_adapter(adapter_config(), sector_root, ASOF)
    assert len(adapted.rows) == 114
    assert sum(row.investable_eligible for row in adapted.rows) == 0
    history_namespace = runpy.run_path(
        str(
            PROJECT_ROOT
            / "industrials"
            / "machinery"
            / "scripts"
            / "18_backfill_machinery_historical_dashboard_reports.py"
        )
    )
    assert (
        history_namespace["validate_portfolio_handoff"](
            sector_output_root=sector_root,
            asof=ASOF,
        )
        == 114
    )
    assert sum(row.calibration_research_eligible for row in adapted.rows) == 0

    history_root = tmp_path / "h"
    historical_rows = survivorship_sidecar(rank_rows)
    publish_dashboard(
        output_dir=history_root / "industrials" / "machinery" / "dashboard" / ASOF,
        rows=historical_rows,
        asof=ASOF,
        allow_overwrite=False,
    )
    historical_adapted = run_adapter(adapter_config(), history_root, ASOF)
    assert sum(row.calibration_research_eligible for row in historical_adapted.rows) == 12
    assert {row.stage1_sample_role for row in historical_adapted.rows if row.calibration_research_eligible} == {
        "pre_lock_research"
    }
    run_script(
        "industrials/machinery/scripts/20_validate_machinery_portfolio_adapter.py",
        "--asof",
        ASOF,
        "--sector-output-root",
        str(history_root),
        "--expect-research-eligible",
    )

    portfolio_config = tmp_path / "portfolio_smoke.yaml"
    write_portfolio_smoke_config(portfolio_config)
    write_med_fixture(sector_root / "med_devices" / ASOF / "med_device_daily_composite_scores.csv")
    with portfolio_connect(tmp_path / "db" / "portfolio_layer.sqlite") as conn:
        init_portfolio_db(conn)
    for script in (
        "portfolio_layer/scores/01_collect_sector_scores.py",
        "portfolio_layer/scores/02_calibrate_cross_sector_scores.py",
        "portfolio_layer/scores/03_validate_score_contract.py",
    ):
        run_script(script, "--config", str(portfolio_config), "--as-of", ASOF, "--force")
    run_dir = tmp_path / "output" / "runs" / ASOF
    stocks = read_csv(run_dir / "stocks_scores.csv")
    machinery_stocks = [row for row in stocks if row["source_pipeline"] == "machinery"]
    assert len(machinery_stocks) == 114
    assert {row["investable_eligible"] for row in machinery_stocks} == {"0"}
    validation = read_csv(run_dir / "validation" / "score_contract_validation.csv")
    assert [row for row in validation if row["status"] not in {"PASS", "WARN", "DEFERRED"}] == []


def test_historical_promotion_is_scoped_idempotent_and_preserves_defense(
    tmp_path: Path,
) -> None:
    module = runpy.run_path(
        str(PROJECT_ROOT / "industrials" / "machinery" / "scripts" / "19_promote_machinery_historical_data.py")
    )
    promote_historical_data = module["promote_historical_data"]
    source = sqlite3.connect(tmp_path / "source.sqlite")
    target = sqlite3.connect(tmp_path / "target.sqlite")
    source.row_factory = sqlite3.Row
    target.row_factory = sqlite3.Row
    schema = """
        CREATE TABLE source_registry(source_id TEXT PRIMARY KEY);
        CREATE TABLE dim_universe_membership(
            ticker TEXT,
            model_family TEXT,
            point_in_time_flag INTEGER,
            start_date TEXT,
            end_date TEXT
        );
        CREATE TABLE dim_delisted_calibration_seed(
            ticker TEXT,
            internal_ticker TEXT,
            model_family TEXT
        );
        CREATE TABLE fact_financial_statement_canonical(
            ticker TEXT NOT NULL,
            source_id TEXT NOT NULL,
            model_family TEXT NOT NULL,
            canonical_metric TEXT NOT NULL,
            period_end TEXT NOT NULL,
            accession_number TEXT NOT NULL,
            unit TEXT NOT NULL,
            value REAL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(
                ticker, source_id, model_family, canonical_metric,
                period_end, accession_number, unit
            )
        );
        CREATE TABLE dim_issuer_reporting_profile_history(
            ticker TEXT NOT NULL,
            model_family TEXT NOT NULL,
            profile_asof_date TEXT NOT NULL,
            reporting_profile TEXT NOT NULL,
            source_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(ticker, model_family, profile_asof_date)
        );
        CREATE TABLE feature_market_technical(
            ticker TEXT, asof_date TEXT, model_family TEXT
        );
        CREATE TABLE feature_financial_statement(
            ticker TEXT, asof_date TEXT, model_family TEXT
        );
        CREATE TABLE feature_financial_metric_availability(
            ticker TEXT, asof_date TEXT, model_family TEXT
        );
        CREATE TABLE feature_positioning(
            ticker TEXT, asof_date TEXT, model_family TEXT
        );
    """
    source.executescript(schema)
    target.executescript(schema)
    source.execute("INSERT INTO source_registry VALUES ('sec')")
    target.execute("INSERT INTO source_registry VALUES ('sec')")
    source.executemany(
        "INSERT INTO dim_delisted_calibration_seed VALUES (?, ?, 'machinery')",
        [("OLD", "OLD"), ("PRE", "PRE")],
    )
    source.executemany(
        "INSERT INTO dim_universe_membership VALUES (?, 'machinery', 1, ?, ?)",
        [("OLD", "2019-01-02", "2021-06-30"), ("PRE", "2010-01-01", "2018-12-31")],
    )
    source.executemany(
        """
        INSERT INTO fact_financial_statement_canonical VALUES
        (?, 'sec', ?, 'revenue', '2020-12-31', ?, 'USD', ?, 'now', 'now')
        """,
        [
            ("OLD", "machinery", "old-accession", 100.0),
            ("OLD", "defense", "defense-accession", 999.0),
            ("PRE", "machinery", "pre-accession", 50.0),
        ],
    )
    source.execute(
        """
        INSERT INTO dim_issuer_reporting_profile_history VALUES
        ('OLD', 'machinery', '2020-02-01', 'us_gaap_xbrl', 'sec', 'now', 'now')
        """
    )
    for table in (
        "feature_market_technical",
        "feature_financial_statement",
        "feature_financial_metric_availability",
        "feature_positioning",
    ):
        target.executemany(
            f"INSERT INTO {table} VALUES (?, ?, ?)",
            [
                ("OLD", "2020-01-02", "machinery"),
                ("CAT", "2026-07-20", "machinery"),
                ("LMT", "2020-01-02", "defense"),
            ],
        )
    target.execute(
        """
        INSERT INTO fact_financial_statement_canonical VALUES
        ('OLD', 'sec', 'defense', 'revenue', '2020-12-31',
         'target-defense', 'USD', 777.0, 'now', 'now')
        """
    )
    source.commit()
    target.commit()

    result = promote_historical_data(
        source,
        target,
        start_date="2019-01-02",
        end_date="2026-07-20",
        compact_target_features=True,
        preserve_asof="2026-07-20",
        commit=True,
    )
    assert result["resolved_in_scope_tickers"] == ["OLD"]
    assert result["tables"]["fact_financial_statement_canonical"]["inserted_rows"] == 1
    assert result["tables"]["dim_issuer_reporting_profile_history"]["inserted_rows"] == 1
    assert (
        target.execute(
            "SELECT value FROM fact_financial_statement_canonical WHERE model_family='machinery'"
        ).fetchone()[0]
        == 100.0
    )
    assert (
        target.execute("SELECT value FROM fact_financial_statement_canonical WHERE model_family='defense'").fetchone()[
            0
        ]
        == 777.0
    )
    assert (
        target.execute("SELECT COUNT(*) FROM fact_financial_statement_canonical WHERE ticker='PRE'").fetchone()[0] == 0
    )
    for table in (
        "feature_market_technical",
        "feature_financial_statement",
        "feature_financial_metric_availability",
        "feature_positioning",
    ):
        assert target.execute(f"SELECT COUNT(*) FROM {table} WHERE model_family='machinery'").fetchone()[0] == 1
        assert target.execute(f"SELECT COUNT(*) FROM {table} WHERE model_family='defense'").fetchone()[0] == 1

    repeated = promote_historical_data(
        source,
        target,
        start_date="2019-01-02",
        end_date="2026-07-20",
        compact_target_features=False,
        preserve_asof="",
        commit=True,
    )
    assert repeated["tables"]["fact_financial_statement_canonical"]["inserted_rows"] == 0
    assert repeated["tables"]["dim_issuer_reporting_profile_history"]["inserted_rows"] == 0

    target.execute(
        """
        UPDATE fact_financial_statement_canonical
        SET value = 101.0
        WHERE model_family='machinery' AND ticker='OLD'
        """
    )
    target.commit()
    with pytest.raises(RuntimeError, match="immutable-key conflicts"):
        promote_historical_data(
            source,
            target,
            start_date="2019-01-02",
            end_date="2026-07-20",
            compact_target_features=False,
            preserve_asof="",
            commit=True,
        )
    assert (
        target.execute(
            "SELECT value FROM fact_financial_statement_canonical WHERE model_family='machinery'"
        ).fetchone()[0]
        == 101.0
    )
    source.close()
    target.close()


def test_financial_builder_rejects_sector_config_mismatch() -> None:
    namespace = runpy.run_path(
        str(PROJECT_ROOT / "industrials" / "scripts" / "08_build_industrials_financial_features.py")
    )
    resolve_model_family = namespace["resolve_model_family"]
    assert resolve_model_family("machinery", "machinery") == "machinery"
    with pytest.raises(ValueError, match="matching sector config"):
        resolve_model_family("machinery", "defense")
