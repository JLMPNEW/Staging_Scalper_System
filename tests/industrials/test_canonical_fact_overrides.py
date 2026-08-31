from __future__ import annotations

import runpy
import sqlite3
from datetime import date
from pathlib import Path

from industrials.core.canonical_fact_overrides import (
    PREFERRED_CONCEPT_PRIORITY,
    canonical_selection_priority,
    load_canonical_concept_overrides,
)
from industrials.core.db import init_db, utc_now


PROJECT_ROOT = Path(__file__).resolve().parents[2]
OVERRIDE_PATH = (
    PROJECT_ROOT
    / "industrials"
    / "transportation"
    / "system_csvs"
    / "transportation_canonical_fact_concept_overrides.csv"
)


def _insert_source(connection: sqlite3.Connection, source_id: str) -> None:
    now = utc_now()
    connection.execute(
        """
        INSERT INTO source_registry(
            source_id, stage, source_name, source_type, base_url,
            status, created_at, updated_at
        ) VALUES (?, 'financials', ?, 'test', 'https://example.com', 'active', ?, ?)
        """,
        (source_id, source_id, now, now),
    )


def test_transportation_override_is_family_and_date_scoped() -> None:
    overrides = load_canonical_concept_overrides(
        OVERRIDE_PATH,
        model_family="transportation",
        asof=date(2026, 8, 13),
    )
    override = overrides[("HTLD", "revenue")]
    assert override.concept_name == "Revenues"
    assert canonical_selection_priority(
        20,
        override=override,
        taxonomy="us-gaap",
        concept_name="Revenues",
    ) == PREFERRED_CONCEPT_PRIORITY
    assert canonical_selection_priority(
        15,
        override=override,
        taxonomy="us-gaap",
        concept_name="RevenueFromContractWithCustomerIncludingAssessedTax",
    ) == 15
    assert load_canonical_concept_overrides(
        OVERRIDE_PATH,
        model_family="defense",
        asof=date(2026, 8, 13),
    ) == {}


def test_preferred_concept_wins_within_same_accession() -> None:
    namespace = runpy.run_path(
        str(PROJECT_ROOT / "industrials" / "scripts" / "08_build_industrials_financial_features.py")
    )
    refresh = namespace["refresh_canonical_facts"]
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    init_db(connection)
    _insert_source(connection, "sec_companyfacts")
    now = utc_now()
    connection.execute(
        """
        INSERT INTO dim_company(
            ticker, cik, company_name, currency, first_seen_at, updated_at
        ) VALUES ('HTLD', '0000799233', 'Heartland Express', 'USD', ?, ?)
        """,
        (now, now),
    )
    common = {
        "ticker": "HTLD",
        "cik": "0000799233",
        "source_id": "sec_companyfacts",
        "accession_number": "0000799233-26-000006",
        "form_type": "10-K",
        "filing_date": "2026-02-20",
        "accepted_at": "2026-02-20T12:00:00Z",
        "fiscal_year": 2025,
        "fiscal_period": "FY",
        "period_start": "2025-01-01",
        "period_end": "2025-12-31",
        "frame": "CY2025",
        "taxonomy": "us-gaap",
        "canonical_metric": "revenue",
        "financial_statement": "income_statement",
        "period_type": "duration",
        "unit": "usd",
        "sign_policy": "as_reported",
        "source_detail": "test",
        "created_at": now,
        "updated_at": now,
    }
    for concept_name, value, priority in (
        ("RevenueFromContractWithCustomerIncludingAssessedTax", 58_100_000.0, 15),
        ("Revenues", 805_709_000.0, 20),
    ):
        row = dict(common, concept_name=concept_name, value=value, source_priority=priority)
        columns = ",".join(row)
        placeholders = ",".join("?" for _ in row)
        connection.execute(
            f"INSERT INTO fact_sec_xbrl_fact({columns}) VALUES ({placeholders})",
            tuple(row.values()),
        )
    overrides = load_canonical_concept_overrides(
        OVERRIDE_PATH,
        model_family="transportation",
        asof=date(2026, 8, 13),
    )
    refresh(
        connection,
        source_id="sec_companyfacts",
        model_family="transportation",
        tickers=["HTLD"],
        asof=date(2026, 8, 13),
        canonical_concept_overrides=overrides,
    )
    selected = connection.execute(
        """
        SELECT concept_name, value, source_priority, canonical_quality
        FROM fact_financial_statement_canonical
        WHERE ticker='HTLD' AND canonical_metric='revenue'
        """
    ).fetchone()
    assert selected is not None
    assert selected["concept_name"] == "Revenues"
    assert selected["value"] == 805_709_000.0
    assert selected["source_priority"] == PREFERRED_CONCEPT_PRIORITY
    assert selected["canonical_quality"] == "mapped_xbrl_preferred_concept"


def test_transportation_profile_keeps_configured_reviewed_taxonomy() -> None:
    namespace = runpy.run_path(
        str(PROJECT_ROOT / "industrials" / "scripts" / "08_build_industrials_financial_features.py")
    )
    rows_for_profile = namespace["rows_for_reporting_profile"]
    rows = [
        {"taxonomy": "us-gaap", "canonical_metric": "revenue", "value": 100.0},
        {
            "taxonomy": "transportation-reviewed",
            "canonical_metric": "capex",
            "value": 25.0,
        },
    ]
    selected, taxonomy = rows_for_profile(
        rows,
        {"reporting_profile": "SEC_XBRL_US_GAAP"},
        model_family="transportation",
    )
    assert taxonomy == "us-gaap"
    assert selected == rows


def test_transportation_interest_coverage_marks_explicit_zero_debt_na() -> None:
    namespace = runpy.run_path(
        str(
            PROJECT_ROOT
            / "industrials"
            / "transportation"
            / "scripts"
            / "08a_build_transportation_specialized_metrics.py"
        )
    )
    resolved = namespace["explicit_zero_debt_interest_na"]
    assert resolved({"total_debt_usd": 0.0, "interest_expense_ttm_usd": None})
    assert not resolved({"total_debt_usd": None, "interest_expense_ttm_usd": None})
    assert not resolved({"total_debt_usd": 10.0, "interest_expense_ttm_usd": 1.0})


def test_transportation_validator_accepts_explicit_zero_debt_interest_na() -> None:
    namespace = runpy.run_path(
        str(
            PROJECT_ROOT
            / "industrials"
            / "transportation"
            / "scripts"
            / "08a_validate_transportation_specialized_metrics.py"
        )
    )
    resolved = namespace["metric_is_conditionally_inapplicable"]
    financial = {
        "total_debt_usd": 0.0,
        "interest_expense_ttm_usd": None,
        "cash_burn_ttm_usd": None,
        "net_income_ttm_usd": 1.0,
    }
    assert resolved("interest_coverage", financial, reviewed_inapplicable=False)
    financial["total_debt_usd"] = 1.0
    assert not resolved("interest_coverage", financial, reviewed_inapplicable=False)
