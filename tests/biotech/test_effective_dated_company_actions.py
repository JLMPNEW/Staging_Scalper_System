from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

from tests.biotech.conftest import load_script_module


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_recent_company_status_overrides_apply_only_on_effective_date() -> None:
    module = load_script_module("02_build_company_master.py", "company_master_effective_status")
    overrides = module.load_status_overrides(
        PROJECT_ROOT / "biotech_index" / "data" / "company_status_overrides.csv"
    )

    expected = {
        "LIXT": "2026-07-06",
        "ESPR": "2026-07-13",
        "NUVL": "2026-07-15",
    }
    for ticker, effective_date in expected.items():
        override = overrides[ticker]
        prior_date = date.fromisoformat(effective_date) - timedelta(days=1)
        assert module.status_override_is_effective(override, asof_date=prior_date.isoformat()) is False
        assert module.status_override_is_effective(override, asof_date=effective_date) is True


def test_historical_universe_ends_nonretained_membership_on_effective_date() -> None:
    module = load_script_module("57_build_historical_scoring_universe.py", "historical_membership_end_actions")
    actions = module.load_nonretained_ticker_actions(
        PROJECT_ROOT / "biotech_index" / "data" / "company_ticker_actions.csv"
    )
    root_rows = [{"ticker": "NUVL", "company_name": "Nuvalent, Inc.", "scoring_include": "true"}]
    companies = {
        "NUVL": {
            "company_id": 1,
            "ticker": "NUVL",
            "company_name": "Nuvalent, Inc.",
            "is_active": 0,
        }
    }
    prices = {
        "NUVL": {
            "first_price_date": "2021-07-29",
            "last_price_date": "2026-07-14",
            "latest_price_date": "2026-07-14",
        }
    }

    before_rows, before_audit = module.live_universe_rows(
        root_rows,
        companies=companies,
        prices=prices,
        asof=date(2026, 7, 14),
        max_price_staleness_days=10,
        nonretained_ticker_actions=actions,
    )
    after_rows, after_audit = module.live_universe_rows(
        root_rows,
        companies=companies,
        prices=prices,
        asof=date(2026, 7, 15),
        max_price_staleness_days=10,
        nonretained_ticker_actions=actions,
    )

    assert [row["ticker"] for row in before_rows] == ["NUVL"]
    assert before_audit[0]["reason"] == "inactive_now_but_priced_on_asof"
    assert after_rows == []
    assert after_audit == [
        {
            "ticker": "NUVL",
            "decision": "exclude",
            "reason": "membership_ended:acquired:2026-07-15",
        }
    ]


def test_offline_ib_company_loader_can_use_inactive_pit_members() -> None:
    module = load_script_module("17_sync_market_data_ib.py", "ib_historical_inactive_company_loader")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE companies(
            company_id INTEGER PRIMARY KEY,
            ticker TEXT NOT NULL,
            company_name TEXT,
            currency TEXT,
            is_active INTEGER NOT NULL
        );
        INSERT INTO companies VALUES (1, 'ACTIVE', 'Active Co', 'USD', 1);
        INSERT INTO companies VALUES (2, 'HIST', 'Historical Co', 'USD', 0);
        """
    )

    active_only = module.load_companies(
        conn,
        scoring_tickers={"ACTIVE", "HIST"},
        ticker_filter=set(),
        max_tickers=0,
    )
    pit_members = module.load_companies(
        conn,
        scoring_tickers={"ACTIVE", "HIST"},
        ticker_filter=set(),
        max_tickers=0,
        include_inactive=True,
    )

    assert [company.ticker for company in active_only] == ["ACTIVE"]
    assert [company.ticker for company in pit_members] == ["ACTIVE", "HIST"]


def test_security_identity_registry_bounds_history_and_validates_rows(tmp_path: Path) -> None:
    from biotech_index.core.security_identity import load_security_identity_rules, security_history_start

    registry = tmp_path / "identities.csv"
    registry.write_text(
        "ticker,company_name,cik,historical_ciks,calibration_cohort,membership_start_date,"
        "membership_end_date,historical_price_ticker,institutional_13f_issuer_alias,cusip,source_reference,approved\n"
        "NEW,New Bio,1234,9999,platform,2024-06-03,,NEW,NEW BIO INC,123456789,test,true\n",
        encoding="utf-8",
    )
    rules = load_security_identity_rules(registry)

    assert rules["NEW"].cik == "0000001234"
    assert rules["NEW"].historical_ciks == ("0000009999",)
    assert rules["NEW"].contains(date(2024, 6, 2)) is False
    assert rules["NEW"].contains(date(2024, 6, 3)) is True
    assert security_history_start(rules, "NEW", default=date(2019, 1, 4)) == date(2024, 6, 3)


def test_historical_additions_use_neutral_metadata_and_respect_pit_root() -> None:
    from biotech_index.core.security_identity import SecurityIdentityRule

    module = load_script_module("57_build_historical_scoring_universe.py", "historical_addition_registry")
    rule = SecurityIdentityRule(
        ticker="NEW",
        company_name="New Bio",
        cik="0000001234",
        historical_ciks=(),
        calibration_cohort="platform_partnered_modality_pipeline",
        membership_start_date=date(2024, 6, 3),
        membership_end_date=None,
        historical_price_ticker="NEW",
        institutional_13f_issuer_aliases=("NEW BIO INC",),
        cusip="",
        source_reference="test",
    )
    companies = {"NEW": {"company_id": 7, "company_name": "New Bio", "is_active": 1}}
    prices = {"NEW": {"latest_price_date": "2024-06-03", "first_price_date": "2024-06-03"}}

    before, _ = module.historical_addition_rows(
        {"NEW": rule},
        pit_root_tickers=set(),
        companies=companies,
        prices=prices,
        asof=date(2024, 5, 31),
        max_price_staleness_days=10,
    )
    active, _ = module.historical_addition_rows(
        {"NEW": rule},
        pit_root_tickers=set(),
        companies=companies,
        prices=prices,
        asof=date(2024, 6, 3),
        max_price_staleness_days=10,
    )
    owned_by_pit_root, audit = module.historical_addition_rows(
        {"NEW": rule},
        pit_root_tickers={"NEW"},
        companies=companies,
        prices=prices,
        asof=date(2024, 6, 3),
        max_price_staleness_days=10,
    )

    assert before == []
    assert len(active) == 1
    assert active[0]["historical_universe_source"] == "active_biotech_pit_membership_registry"
    assert active[0].get("primary_nct", "") == ""
    assert active[0]["biotech_calibration_cohort"] == "platform_partnered_modality_pipeline"
    assert owned_by_pit_root == []
    assert audit[0]["reason"] == "dated_pit_universe_owns_membership"


def test_companyfacts_historical_cik_merge_preserves_first_reported_value() -> None:
    module = load_script_module("15_sync_sec_companyfacts_history.py", "companyfacts_cik_lineage_merge")
    primary = module.Company(1, "ATAI", "0002081043", "AtaiBeckley Inc.")
    predecessor = module.Company(1, "ATAI", "0001840904", "AtaiBeckley Inc.")

    def observation(*, cik: str, filed: str, value: float, accession: str) -> dict[str, object]:
        return {
            "company_id": 1,
            "cik": cik,
            "taxonomy": "us-gaap",
            "concept": "CashAndCashEquivalentsAtCarryingValue",
            "label": "Cash",
            "unit": "USD",
            "value": value,
            "period_start": "",
            "period_end": "2025-09-30",
            "fiscal_year": 2025,
            "fiscal_period": "Q3",
            "form": "10-Q",
            "filed_date": filed,
            "accession_nodash": accession,
            "frame": "",
            "source": "sec_companyfacts",
            "confidence": 1.0,
        }

    merged = module.merge_companyfacts_results(
        [
            module.CompanyFactsFetchResult(
                company=primary,
                latest_source_filing_date="2026-03-01",
                payload_hash="current",
                observations=(observation(cik=primary.cik, filed="2026-03-01", value=90.0, accession="later"),),
            ),
            module.CompanyFactsFetchResult(
                company=predecessor,
                latest_source_filing_date="2025-11-01",
                payload_hash="historical",
                observations=(observation(cik=predecessor.cik, filed="2025-11-01", value=100.0, accession="first"),),
            ),
        ],
        primary_companies={1: primary},
    )

    assert len(merged) == 1
    assert merged[0].company.cik == primary.cik
    assert len(merged[0].observations) == 2
    assert len(merged[0].normalized) == 1
    assert merged[0].normalized[0]["cash"] == 100.0
    assert merged[0].normalized[0]["filed_date"] == "2025-11-01"

def test_current_root_reconstruction_strips_non_pit_ctgov_metadata() -> None:
    module = load_script_module("57_build_historical_scoring_universe.py", "historical_neutral_survivor_root")
    root_rows = [
        {
            "ticker": "SURV",
            "company_name": "Survivor Bio",
            "scoring_include": "true",
            "primary_nct": "NCT_FUTURE",
            "active_qualifying_trial_count": "4",
            "ctgov_review_bucket": "active_study_exists",
        }
    ]
    companies = {"SURV": {"company_id": 1, "ticker": "SURV", "company_name": "Survivor Bio", "is_active": 1}}
    prices = {
        "SURV": {
            "first_price_date": "2018-01-01",
            "last_price_date": "2026-01-01",
            "latest_price_date": "2019-01-04",
        }
    }

    rows, _ = module.live_universe_rows(
        root_rows,
        companies=companies,
        prices=prices,
        asof=date(2019, 1, 4),
        max_price_staleness_days=10,
        root_universe_is_pit=False,
    )

    assert len(rows) == 1
    assert rows[0]["historical_universe_source"] == "current_survivor_price_window_reconstruction"
    assert "primary_nct" not in rows[0]
    assert "active_qualifying_trial_count" not in rows[0]
    assert "ctgov_review_bucket" not in rows[0]
