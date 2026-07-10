from __future__ import annotations

import csv
import runpy
import subprocess
import sys
from datetime import date
from pathlib import Path

from industrials.core.config import cfg_get, load_yaml
from industrials.core.db import connect, utc_now
from industrials.machinery.contracts import resolve_norgate_mappings
from industrials.machinery.scoring import (
    FINAL_RANK_FIELDS,
    PORTFOLIO_REQUIRED_FIELDS,
    publish_dashboard,
    read_rows,
    survivorship_sidecar,
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
    assert result.returncode == 0, (
        f"Script failed: {script}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


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
                SET roic = ?, asset_turnover = ?, incremental_operating_margin = ?,
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


def test_portfolio_registration_keeps_defense_on_tech_adapter() -> None:
    config = load_yaml(PROJECT_ROOT / "portfolio_layer" / "config.yaml")
    sectors = {
        str(row["model_family"]): row
        for row in cfg_get(config, "score_contract.sectors", [])
        if isinstance(row, dict)
    }
    assert sectors["defense"]["adapter"] == "tech_family"
    assert sectors["machinery"]["adapter"] == "industrial_family"
    assert sectors["machinery"]["required"] is False
    assert cfg_get(config, "risk_panel.sector_etf_map.machinery") == "XLI"
    assert float(cfg_get(config, "optimizer.sector_weight_caps.machinery")) == 0.0
    assert float(cfg_get(config, "black_litterman_fusion.strategic_sector_weights.machinery")) == 0.0
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
    assert step_ids.index("08_validate_financial") < step_ids.index("08a_audit_special_metrics")
    assert step_ids.index("08a_audit_special_metrics") < step_ids.index("12_sync_sec_ownership")
    assert step_ids.index("06a_build_scoring") < step_ids.index("06a_validate_scoring")
    assert step_ids.index("06a_validate_scoring") < step_ids.index("10_build_scores")
    positioning = next(step for step in steps if step.step_id == "13_sync_positioning")
    assert "--daily-refresh" not in positioning.args
    assert positioning.pass_db is False
    assert next(step for step in steps if step.step_id == "12_sync_sec_ownership").pass_db is False
    assert next(step for step in steps if step.step_id == "06a_validate_scoring").pass_db is True
    assert "--archive-bootstrap" in next(step for step in steps if step.step_id == "07_sync_sec").args

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


def test_machinery_special_metric_schema_and_text_labels(tmp_path: Path) -> None:
    db_path = load_machinery_db(tmp_path)
    required_columns = {
        "orders_yoy_growth",
        "book_to_bill",
        "backlog_yoy_growth",
        "backlog_to_revenue",
        "roic",
        "asset_turnover",
        "incremental_operating_margin",
        "inventory_sales_growth_spread",
        "cash_conversion_cycle_change",
        "net_debt_to_ebitda",
        "interest_coverage",
        "cash_runway_years",
        "capital_raise_dependence",
        "diluted_shares_yoy_growth",
    }
    with connect(db_path) as conn:
        columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(feature_financial_statement)")}
    assert required_columns.issubset(columns)

    namespace = runpy.run_path(
        str(PROJECT_ROOT / "industrials" / "scripts" / "07_sync_industrials_sec_fundamentals.py")
    )
    label_concept = namespace["text_table_label_concept"]
    assert label_concept("New orders received") == ("Orders", "duration")
    assert label_concept("Funded backlog") == ("FundedBacklog", "instant")
    assert label_concept("Backlog") is None
    assert label_concept("Remaining performance obligation") is None
    parse_tables = namespace["parse_archive_text_table_facts"]
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
    assert select_previous_comparable(
        [current_instant, prior_instant],
        "assets",
        current_instant,
        instant_metric=True,
    ) is prior_instant
    assert select_previous_comparable(
        [current_instant, prior_instant],
        "assets",
        current_instant,
    ) is None

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
        assert conn.execute(
            "SELECT COUNT(*) FROM dim_industrials_taxonomy WHERE model_family='defense' AND ticker='DEFTEST'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM dim_universe_membership WHERE model_family='defense' AND ticker='DEFTEST'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM dim_industrials_taxonomy WHERE model_family='machinery'"
        ).fetchone()[0] == 136


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
    development_totals = {
        int(row["score_input_total_count"])
        for row in scoring_rows
        if row["development_stage"] == "development_stage"
    }
    operating_totals = {
        int(row["score_input_total_count"])
        for row in scoring_rows
        if row["development_stage"] == "operating"
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
    assert sum(row["rank_ready_flag"] == "1" for row in rank_rows) == 12
    assert all(row["portfolio_candidate_gate"] == "0" for row in rank_rows)

    manifest = load_yaml(output_dir / "machinery_final_rank_table_manifest.json")
    assert manifest["acceptance"] == "PASS"
    assert manifest["sidecar_calibration_eligible_count"] == 12
    published = read_rows(output_dir / "machinery_final_rank_table.csv")
    assert set(FINAL_RANK_FIELDS) == set(published[0])

    adapted = run_adapter(adapter_config(), sector_root, ASOF)
    assert len(adapted.rows) == 114
    assert sum(row.investable_eligible for row in adapted.rows) == 0
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
    assert {
        row.stage1_sample_role for row in historical_adapted.rows if row.calibration_research_eligible
    } == {"pre_lock_research"}
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
