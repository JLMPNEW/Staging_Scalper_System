from __future__ import annotations

import importlib
import sqlite3
from datetime import date
from pathlib import Path


graduation = importlib.import_module("industrials.scripts.09_evaluate_industrials_profile_graduation")
financial_features = importlib.import_module("industrials.scripts.08_build_industrials_financial_features")
sec_sync = importlib.import_module("industrials.scripts.07_sync_industrials_sec_fundamentals")


def fact(
    metric: str,
    value: float,
    *,
    period_start: str,
    period_end: str,
    form_type: str,
    fiscal_period: str,
    accession: str,
    filing_date: str,
    taxonomy: str = "us-gaap",
    unit: str = "USD",
) -> dict[str, object]:
    return {
        "canonical_metric": metric,
        "value": value,
        "period_start": period_start,
        "period_end": period_end,
        "form_type": form_type,
        "fiscal_period": fiscal_period,
        "accession_number": accession,
        "filing_date": filing_date,
        "taxonomy": taxonomy,
        "unit": unit,
        "source_priority": 10,
        "concept_name": metric,
    }


def subject(*, ticker: str, profile: str) -> dict[str, object]:
    return {
        "ticker": ticker,
        "reporting_profile": profile,
        "primary_taxonomy": "us-gaap",
        "development_stage": "development_stage",
        "trading_days_available": 400,
        "market_data_quality": "complete",
        "financial_data_quality_status": "complete",
    }


def pre_revenue_facts() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for metric, value in {
        "assets": 20_000_000.0,
        "cash_and_equivalents": 8_000_000.0,
        "net_income": -8_000_000.0,
        "operating_income": -9_000_000.0,
        "operating_cash_flow": -7_000_000.0,
        "capex": 200_000.0,
    }.items():
        rows.append(
            fact(
                metric,
                value,
                period_start="" if metric in {"assets", "cash_and_equivalents"} else "2024-06-01",
                period_end="2025-05-31",
                form_type="10-K",
                fiscal_period="FY",
                accession="annual",
                filing_date="2025-08-20",
            )
        )
    for metric, latest, prior in (
        ("operating_cash_flow", -9_000_000.0, -5_000_000.0),
        ("capex", 700_000.0, 100_000.0),
    ):
        rows.extend(
            [
                fact(
                    metric,
                    latest,
                    period_start="2025-06-01",
                    period_end="2026-02-28",
                    form_type="10-Q",
                    fiscal_period="Q3",
                    accession="current-q3",
                    filing_date="2026-04-14",
                ),
                fact(
                    metric,
                    prior,
                    period_start="2024-06-01",
                    period_end="2025-02-28",
                    form_type="10-Q",
                    fiscal_period="Q3",
                    accession="prior-q3",
                    filing_date="2025-04-14",
                ),
            ]
        )
    return rows


def test_pre_revenue_candidate_graduates_with_annual_and_valid_cash_flow_ttm() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE fact_market_snapshot(ticker TEXT, asof_date TEXT, source_id TEXT, market_cap REAL, regular_market_price REAL)"
    )
    conn.execute(
        "CREATE TABLE feature_market_technical(ticker TEXT, asof_date TEXT, source_id TEXT, model_family TEXT, latest_adj_close REAL)"
    )
    result = graduation.evaluate_candidate(
        conn,
        subject=subject(ticker="DEV", profile="RECENT_IPO_DEVELOPMENT_STAGE"),
        facts=pre_revenue_facts(),
        bridge_facts=[],
        asof=date(2026, 7, 9),
        min_trading_days=252,
        max_annual_age_days=550,
        min_periodic_filings=2,
        fx_max_staleness_days=7,
        source_id="sec_companyfacts",
        model_family="defense",
        market_source_ids=["yahoo_finance_adjusted"],
    )
    assert result["graduation_eligible_flag"] == 1
    assert result["target_reporting_profile"] == "SEC_XBRL_US_GAAP"
    assert result["revenue_mode"] == "pre_revenue"
    assert result["operating_cash_flow_ttm_status"] == "available"
    assert result["capex_ttm_status"] == "available"


def test_revenue_reporter_stays_blocked_without_annual_revenue_and_capex_ttm() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE fact_market_snapshot(ticker TEXT, asof_date TEXT, source_id TEXT, market_cap REAL, regular_market_price REAL)"
    )
    conn.execute(
        "CREATE TABLE feature_market_technical(ticker TEXT, asof_date TEXT, source_id TEXT, model_family TEXT, latest_adj_close REAL)"
    )
    rows = pre_revenue_facts()
    rows = [row for row in rows if row["canonical_metric"] != "capex"]
    rows.append(
        fact(
            "revenue",
            2_000_000.0,
            period_start="2026-01-01",
            period_end="2026-03-31",
            form_type="10-Q",
            fiscal_period="Q1",
            accession="current-q1",
            filing_date="2026-05-15",
        )
    )
    result = graduation.evaluate_candidate(
        conn,
        subject=subject(ticker="NEW", profile="RECENT_PUBLIC_STUB"),
        facts=rows,
        bridge_facts=[],
        asof=date(2026, 7, 9),
        min_trading_days=252,
        max_annual_age_days=550,
        min_periodic_filings=2,
        fx_max_staleness_days=7,
        source_id="sec_companyfacts",
        model_family="defense",
        market_source_ids=["yahoo_finance_adjusted"],
    )
    assert result["graduation_eligible_flag"] == 0
    assert "missing_annual_metrics=capex,revenue" in result["blocking_reasons"]
    assert "ttm_revenue_unavailable" in result["blocking_reasons"]
    assert "ttm_capex_unavailable" in result["blocking_reasons"]


def test_promoted_xbrl_profile_excludes_archive_text_rows() -> None:
    rows = [
        {"taxonomy": "us-gaap", "canonical_metric": "assets", "value": 20_000_000.0},
        {"taxonomy": "sec-text", "canonical_metric": "assets", "value": 20_000.0},
    ]
    filtered, taxonomy = financial_features.rows_for_reporting_profile(
        rows,
        {"reporting_profile": "SEC_XBRL_US_GAAP"},
    )
    assert taxonomy == "us-gaap"
    assert filtered == [rows[0]]


def test_archive_text_parser_maps_capex_and_reassembles_accounting_parentheses() -> None:
    assert sec_sync.text_table_label_concept("Additions of property and equipment") == ("Capex", "duration")
    assert sec_sync.row_values(
        ["Additions of property and equipment", "$", "(427", ")", "$", "(1,904", ")"]
    ) == [-427.0, -1904.0]
    statement_type, projection_flag, historical_flag = sec_sync.text_table_statement_provenance(
        "Merlin Labs, Inc. Consolidated Statements of Cash Flows",
        "Cash flows from investing activities",
    )
    assert (statement_type, projection_flag, historical_flag) == ("cash_flow", 0, 1)
    _, projection_flag, historical_flag = sec_sync.text_table_statement_provenance(
        "Unaudited Pro Forma Condensed Combined Statements of Operations",
        "Revenue",
    )
    assert (projection_flag, historical_flag) == (1, 0)


def test_revenue_reporter_can_use_certified_despac_predecessor_bridge() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE fact_market_snapshot(ticker TEXT, asof_date TEXT, source_id TEXT, market_cap REAL, regular_market_price REAL)"
    )
    conn.execute(
        "CREATE TABLE feature_market_technical(ticker TEXT, asof_date TEXT, source_id TEXT, model_family TEXT, latest_adj_close REAL)"
    )
    periodic: list[dict[str, object]] = []
    for metric, current, prior in (
        ("revenue", 1_002_000.0, 868_000.0),
        ("operating_cash_flow", -20_000_000.0, -12_000_000.0),
        ("capex", 2_159_000.0, 94_000.0),
    ):
        periodic.extend(
            [
                fact(
                    metric,
                    current,
                    period_start="2026-01-01",
                    period_end="2026-03-31",
                    form_type="10-Q",
                    fiscal_period="Q1",
                    accession="current-q1",
                    filing_date="2026-05-15",
                ),
                fact(
                    metric,
                    prior,
                    period_start="2025-01-01",
                    period_end="2025-03-31",
                    form_type="10-Q",
                    fiscal_period="Q1",
                    accession="current-q1",
                    filing_date="2026-05-15",
                ),
            ]
        )
    bridge: list[dict[str, object]] = []
    for metric, value in {
        "revenue": 7_551_000.0,
        "assets": 80_582_000.0,
        "cash_and_equivalents": 59_343_000.0,
        "net_income": -74_778_000.0,
        "operating_cash_flow": -59_868_000.0,
        "capex": 427_000.0,
    }.items():
        bridge.append(
            fact(
                metric,
                value,
                period_start="" if metric in {"assets", "cash_and_equivalents"} else "2025-01-01",
                period_end="2025-12-31",
                form_type="424B3",
                fiscal_period="FY",
                accession="audited-predecessor",
                filing_date="2026-05-13",
                taxonomy="sec-audited-predecessor",
            )
        )
    result = graduation.evaluate_candidate(
        conn,
        subject=subject(ticker="DSPC", profile="RECENT_PUBLIC_STUB"),
        facts=periodic,
        bridge_facts=bridge,
        asof=date(2026, 7, 9),
        min_trading_days=252,
        max_annual_age_days=550,
        min_periodic_filings=2,
        fx_max_staleness_days=7,
        source_id="sec_companyfacts",
        model_family="defense",
        market_source_ids=["yahoo_finance_adjusted"],
    )
    assert result["graduation_eligible_flag"] == 1
    assert result["target_reporting_profile"] == "SEC_XBRL_US_GAAP_DESPAC_BRIDGE"
    assert result["predecessor_bridge_used_flag"] == 1
    assert result["projected_revenue_ttm_usd"] == 7_685_000.0


def test_graduation_decision_supersedes_base_override_only_when_effective(tmp_path: Path) -> None:
    base = tmp_path / "base.csv"
    decisions = tmp_path / "decisions.csv"
    fields = graduation.DECISION_FIELDS
    base_row = {
        "ticker": "DEV",
        "handling_type": "recent_public_stub",
        "parent_ticker": "",
        "skip_sec_network": "false",
        "reporting_profile": "RECENT_PUBLIC_STUB",
        "reporting_standard": "recent_public_stub",
        "fallback_status": "stub_period_limited",
        "financial_confidence": "0.35",
        "usable_xbrl_flag": "0",
        "review_reason": "stub",
        "notes": "",
        "valid_from": "2026-07-02",
        "reviewed_at": "2026-07-02",
    }
    decision_row = base_row | {
        "handling_type": "controlled_profile_graduation",
        "reporting_profile": "SEC_XBRL_US_GAAP",
        "reporting_standard": "US_GAAP",
        "fallback_status": "none",
        "financial_confidence": "0.90",
        "usable_xbrl_flag": "1",
        "review_reason": "",
        "valid_from": "2026-07-10",
        "reviewed_at": "2026-07-11",
    }
    graduation.write_csv_atomic(base, fields, [base_row])
    graduation.write_csv_atomic(decisions, fields, [decision_row])

    before = sec_sync.load_reporting_override_sources([base, decisions], asof="2026-07-09")
    after = sec_sync.load_reporting_override_sources([base, decisions], asof="2026-07-10")
    assert before["DEV"].reporting_profile == "RECENT_PUBLIC_STUB"
    assert after["DEV"].reporting_profile == "SEC_XBRL_US_GAAP"


def test_append_decision_is_idempotent_across_review_dates(tmp_path: Path) -> None:
    path = tmp_path / "decisions.csv"
    audit = {
        "ticker": "DEV",
        "asof_date": "2026-07-09",
        "graduation_eligible_flag": 1,
        "target_reporting_profile": "SEC_XBRL_US_GAAP",
        "target_reporting_standard": "US_GAAP",
        "target_profile_confidence": 0.90,
        "target_taxonomy": "us-gaap",
        "annual_form_type": "10-K",
        "annual_period_end": "2025-12-31",
        "revenue_mode": "pre_revenue",
        "predecessor_bridge_used_flag": 0,
    }
    first = graduation.append_decisions(
        path,
        audit_rows=[audit],
        effective_date=date(2026, 7, 10),
        reviewed_at=date(2026, 7, 11),
    )
    second = graduation.append_decisions(
        path,
        audit_rows=[audit],
        effective_date=date(2026, 7, 10),
        reviewed_at=date(2026, 7, 12),
    )
    assert first == {"DEV"}
    assert second == {"DEV"}
    assert len(graduation.read_decisions(path)) == 1
