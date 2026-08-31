from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from biotech_index.core.source_windows import resolve_positioning_source_windows
from tests.biotech.conftest import load_script_module


def test_positioning_windows_are_bounded_and_borrow_covers_90_day_feature() -> None:
    windows = resolve_positioning_source_windows(
        asof="2026-08-14",
        configured_start="2019-01-01",
    )

    assert windows.short_interest_start == date(2026, 4, 16)
    assert windows.institutional_13f_start == date(2025, 2, 10)
    assert windows.borrow_start == date(2026, 4, 16)
    assert windows.float_denominator_start == date(2025, 2, 10)


def test_positioning_full_history_uses_configured_floor() -> None:
    windows = resolve_positioning_source_windows(
        asof="2026-08-14",
        configured_start="2019-01-01",
        full_history=True,
    )

    starts = {
        windows.short_interest_start,
        windows.institutional_13f_start,
        windows.borrow_start,
        windows.float_denominator_start,
    }
    assert starts == {date(2019, 1, 1)}


def score_row(*, ticker: str = "AAA", price_date: str = "2026-08-14") -> dict[str, str]:
    return {
        "company_id": "1",
        "ticker": ticker,
        "calibration_only": "0",
        "portfolio_candidate_gate": "1",
        "source_snapshot_asof_date": "2026-08-14",
        "feature_data_asof_date": "2026-08-14",
        "clinical_data_asof_date": "2026-08-14",
        "financial_data_asof_date": "2026-08-14",
        "insider_data_asof_date": "2026-08-14",
        "price_data_asof_date": price_date,
        "borrow_fee_data_available_flag": "1",
        "borrow_fee_stale_flag": "0",
        "borrow_fee_staleness_days": "1",
    }


def test_score_lineage_rejects_stale_candidate_price() -> None:
    module = load_script_module(
        "33_validate_biotech_source_acceptance.py",
        "biotech_source_acceptance_price_test",
    )
    checks: list[Any] = []

    module.validate_score_lineage(
        [score_row(price_date="2026-08-13")],
        checks,
        asof=date(2026, 8, 14),
    )

    result = next(check for check in checks if check.name == "candidate_price_freshness")
    assert result.status == "FAIL"
    assert result.evidence["stale_candidate_tickers"] == ["AAA"]


def test_score_lineage_accepts_latest_market_date_for_weekend_report() -> None:
    module = load_script_module(
        "33_validate_biotech_source_acceptance.py",
        "biotech_source_acceptance_weekend_price_test",
    )
    checks: list[Any] = []

    module.validate_score_lineage(
        [score_row(price_date="2026-08-21")],
        checks,
        asof=date(2026, 8, 22),
        expected_market_asof=date(2026, 8, 21),
    )

    result = next(check for check in checks if check.name == "candidate_price_freshness")
    assert result.status == "PASS"
    assert result.evidence["expected_market_asof_date"] == "2026-08-21"


def test_financial_lineage_requires_features_to_use_latest_core_period() -> None:
    module = load_script_module(
        "33_validate_biotech_source_acceptance.py",
        "biotech_source_acceptance_financial_test",
    )
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE sec_filings(company_id INTEGER, filing_date TEXT, form TEXT);
        CREATE TABLE company_facts_sync_state(
            company_id INTEGER, latest_source_filing_date TEXT, sync_status TEXT
        );
        CREATE TABLE company_facts_quarterly(
            company_id INTEGER, period_end TEXT, filed_date TEXT,
            cash_and_investments REAL, total_assets REAL, total_debt REAL,
            revenue REAL, operating_income REAL, net_income REAL,
            operating_cash_flow REAL, shares_outstanding REAL
        );
        CREATE TABLE financial_survival_features(
            asof_date TEXT, company_id INTEGER, latest_period_end TEXT
        );
        CREATE TABLE commercial_value_features_daily(
            asof_date TEXT, company_id INTEGER, latest_period_end TEXT
        );
        INSERT INTO sec_filings VALUES (1, '2026-08-08', '10-Q');
        INSERT INTO company_facts_sync_state VALUES (1, '2026-08-08', 'synced');
        INSERT INTO company_facts_quarterly VALUES (
            1, '2026-06-30', '2026-08-08', 10, 20, 0, 5, -1, -1, -2, 3
        );
        INSERT INTO financial_survival_features VALUES ('2026-08-14', 1, '2026-03-31');
        INSERT INTO commercial_value_features_daily VALUES ('2026-08-14', 1, '2026-03-31');
        """
    )
    checks: list[Any] = []

    module.validate_financial_lineage(
        conn,
        [score_row()],
        checks,
        asof=date(2026, 8, 14),
    )

    result = next(check for check in checks if check.name == "candidate_financial_lineage")
    assert result.status == "FAIL"
    assert result.evidence["issues"]["features_lag_latest_canonical_period"] == ["AAA"]


def test_form4_freshness_parses_non_sortable_date_text(tmp_path: Path) -> None:
    module = load_script_module(
        "33_validate_biotech_source_acceptance.py",
        "biotech_source_acceptance_form4_test",
    )
    db_path = tmp_path / "sec_insider.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE sec_form4_daily_state(last_index_date TEXT);
            CREATE TABLE sec_ownership_submission(filing_date TEXT);
            INSERT INTO sec_form4_daily_state VALUES ('2026-08-14');
            INSERT INTO sec_form4_daily_state VALUES ('2026-08-15');
            INSERT INTO sec_ownership_submission VALUES ('31-OCT-2025');
            INSERT INTO sec_ownership_submission VALUES ('14-AUG-2026');
            """
        )
    checks: list[Any] = []
    config = {
        "governance_events": {"form4_db_path": str(db_path)},
        "biotech_refresh": {"form4_preflight": {"max_raw_filing_lag_days": 5}},
    }

    module.validate_form4_source(
        config,
        base_dir=tmp_path,
        checks=checks,
        asof=date(2026, 8, 14),
    )

    result = next(check for check in checks if check.name == "form4_source_freshness")
    assert result.status == "PASS"
    assert result.evidence["latest_raw_filing_date"] == "2026-08-14"


def test_form4_freshness_uses_current_signal_snapshot_when_index_lags(
    tmp_path: Path,
) -> None:
    module = load_script_module(
        "33_validate_biotech_source_acceptance.py",
        "biotech_source_acceptance_form4_signal_snapshot_test",
    )
    db_path = tmp_path / "sec_insider.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE sec_form4_daily_state(last_index_date TEXT);
            CREATE TABLE stock_signal_snapshot_tier1(as_of_date TEXT);
            CREATE TABLE sec_ownership_submission(filing_date TEXT);
            INSERT INTO sec_form4_daily_state VALUES ('2026-08-20');
            INSERT INTO stock_signal_snapshot_tier1 VALUES ('2026-08-21');
            INSERT INTO sec_ownership_submission VALUES ('2026-08-21');
            """
        )
    checks: list[Any] = []
    config = {
        "governance_events": {"form4_db_path": str(db_path)},
        "biotech_refresh": {"form4_preflight": {"max_raw_filing_lag_days": 5}},
    }

    module.validate_form4_source(
        config,
        base_dir=tmp_path,
        checks=checks,
        asof=date(2026, 8, 21),
    )

    result = next(check for check in checks if check.name == "form4_source_freshness")
    assert result.status == "PASS"
    assert result.evidence["snapshot_date"] == "2026-08-21"
    assert result.evidence["snapshot_source"] == "stock_signal_snapshot_tier1.as_of_date"


def test_form4_freshness_accepts_latest_market_snapshot_for_weekend_report(tmp_path: Path) -> None:
    module = load_script_module(
        "33_validate_biotech_source_acceptance.py",
        "biotech_source_acceptance_form4_weekend_test",
    )
    db_path = tmp_path / "sec_insider.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE stock_signal_snapshot_tier1(as_of_date TEXT);
            CREATE TABLE sec_ownership_submission(filing_date TEXT);
            INSERT INTO stock_signal_snapshot_tier1 VALUES ('2026-08-21');
            INSERT INTO sec_ownership_submission VALUES ('2026-08-21');
            """
        )
    checks: list[Any] = []
    config = {
        "governance_events": {"form4_db_path": str(db_path)},
        "biotech_refresh": {"form4_preflight": {"max_raw_filing_lag_days": 5}},
    }

    module.validate_form4_source(
        config,
        base_dir=tmp_path,
        checks=checks,
        asof=date(2026, 8, 22),
        expected_snapshot_date=date(2026, 8, 21),
    )

    result = next(check for check in checks if check.name == "form4_source_freshness")
    assert result.status == "PASS"
    assert result.evidence["expected_snapshot_date"] == "2026-08-21"


def test_form4_freshness_accepts_snapshot_newer_than_price_floor(tmp_path: Path) -> None:
    module = load_script_module(
        "33_validate_biotech_source_acceptance.py",
        "biotech_source_acceptance_form4_newer_than_market_test",
    )
    db_path = tmp_path / "sec_insider.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE stock_signal_snapshot_tier1(as_of_date TEXT);
            CREATE TABLE sec_ownership_submission(filing_date TEXT);
            INSERT INTO stock_signal_snapshot_tier1 VALUES ('2026-08-28');
            INSERT INTO sec_ownership_submission VALUES ('2026-08-28');
            """
        )
    checks: list[Any] = []
    config = {
        "governance_events": {"form4_db_path": str(db_path)},
        "biotech_refresh": {"form4_preflight": {"max_raw_filing_lag_days": 5}},
    }

    module.validate_form4_source(
        config,
        base_dir=tmp_path,
        checks=checks,
        asof=date(2026, 8, 28),
        expected_snapshot_date=date(2026, 8, 27),
    )

    result = next(check for check in checks if check.name == "form4_source_freshness")
    assert result.status == "PASS"
    assert result.evidence["expected_snapshot_date"] == "2026-08-27"
    assert result.evidence["snapshot_date"] == "2026-08-28"


def test_trial_snapshot_lookup_is_bounded_by_requested_asof() -> None:
    module = load_script_module(
        "33_validate_biotech_source_acceptance.py",
        "biotech_source_acceptance_snapshot_test",
    )
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE trial_snapshot_daily(asof_date TEXT)")
    conn.executemany(
        "INSERT INTO trial_snapshot_daily VALUES (?)",
        [("2026-08-16",), ("2026-08-17",), ("2026-08-18",)],
    )

    assert module.latest_trial_snapshot_asof(conn, asof=date(2026, 8, 17)) == "2026-08-17"
    assert module.latest_trial_snapshot_asof(conn, asof=date(2026, 8, 15)) is None


def test_acceptance_manifests_preserve_each_same_date_attempt(tmp_path: Path) -> None:
    module = load_script_module(
        "33_validate_biotech_source_acceptance.py",
        "biotech_source_acceptance_archive_test",
    )
    latest = tmp_path / "biotech_refresh_acceptance_manifest.json"
    archive_root = tmp_path / "orchestration" / "source_acceptance"
    asof = date(2026, 8, 17)
    first = {
        "status": "FAIL",
        "asof_date": asof.isoformat(),
        "checks": [{"name": "candidate_price_freshness", "evidence": {"stale_candidate_tickers": ["AAA"]}}],
    }
    second = {
        "status": "PASS",
        "asof_date": asof.isoformat(),
        "checks": [{"name": "candidate_price_freshness", "evidence": {"stale_candidate_tickers": []}}],
    }

    first_paths = module.persist_acceptance_manifests(
        latest_path=latest,
        archive_root=archive_root,
        asof=asof,
        created_at_utc=datetime(2026, 8, 18, 4, 0, 0, 1, tzinfo=timezone.utc),
        payload=first,
    )
    second_paths = module.persist_acceptance_manifests(
        latest_path=latest,
        archive_root=archive_root,
        asof=asof,
        created_at_utc=datetime(2026, 8, 18, 4, 1, 0, 2, tzinfo=timezone.utc),
        payload=second,
    )

    assert first_paths["archive"].read_text(encoding="utf-8").find('"AAA"') >= 0
    assert first_paths["archive"] != second_paths["archive"]
    assert len(list((archive_root / "20260817").glob("*.json"))) == 2
    assert '"status": "PASS"' in latest.read_text(encoding="utf-8")
    assert second_paths["dated"] == tmp_path / "20260817" / latest.name
    assert '"status": "PASS"' in second_paths["dated"].read_text(encoding="utf-8")
