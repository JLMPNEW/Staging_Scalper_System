from __future__ import annotations

import json
import copy
from pathlib import Path
import pytest

from consumer_defensive.core.config import ConfigBundle, load_config
from consumer_defensive.core.db import connect
from consumer_defensive.core.stage4 import (
    apply_applicability,
    bootstrap_stage4,
    build_financial_features,
    load_applicability,
    run_disclosure_census,
    sync_sec_fundamentals,
    sync_fx_rates,
    _feature_values,
    validate_stage4,
)
from consumer_defensive.core.universe import load_current_universe, load_policy


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "consumer_defensive" / "config.yaml"
POLICY = ROOT / "consumer_defensive" / "data" / "consumer_defensive_universe_policy.yaml"
APPLICABILITY = ROOT / "consumer_defensive" / "data" / "consumer_defensive_metric_applicability.csv"


def _prepared_db(tmp_path: Path):
    source_bundle = load_config(CONFIG)
    payload = copy.deepcopy(source_bundle.payload)
    payload["sec_fundamentals"]["cache_dir"] = str(tmp_path / "sec_cache")
    payload["fx_rates"]["cache_dir"] = str(tmp_path / "fx_cache")
    bundle = ConfigBundle(source_bundle.path, source_bundle.base_dir, payload)
    conn = connect(tmp_path / "stage4.sqlite")
    bootstrap_stage4(conn, bundle)
    load_current_universe(conn, load_policy(POLICY))
    apply_applicability(conn, APPLICABILITY)
    return bundle, conn


def test_applicability_is_complete_unique_and_matches_current_universe(tmp_path: Path) -> None:
    mapping = load_applicability(APPLICABILITY)
    assert len(mapping) == 119
    bundle, conn = _prepared_db(tmp_path)
    try:
        rows = conn.execute(
            "SELECT ticker, calibration_cohort_id, applicability_subtype "
            "FROM dim_consumer_defensive_taxonomy"
        ).fetchall()
        assert len(rows) == 108
        assert all(mapping[str(row[0])] == (str(row[1]), str(row[2])) for row in rows)
        assert mapping["KO"][1] == "non_alcohol"
        assert mapping["SYY"][1] == "food_distribution"
        assert mapping["NUS"][1] == "direct_selling_wellness"
        assert mapping["ADM"][1] == "agricultural_processor"
    finally:
        conn.close()


def test_sec_bulk_counts_only_committed_facts_after_transaction_rollback(
    tmp_path: Path,
) -> None:
    bundle, conn = _prepared_db(tmp_path)
    accession = "0000000000-24-000001"
    submissions = {
        "cik": "21344",
        "filings": {
            "recent": {
                "accessionNumber": [accession],
                "filingDate": ["2024-02-20"],
                "acceptanceDateTime": ["2024-02-20T16:30:00Z"],
                "reportDate": ["2023-12-31"],
                "form": ["10-K"],
                "primaryDocument": ["ko-20231231.htm"],
            },
            "files": [],
        }
    }
    companyfacts = {
        "cik": "21344",
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            {
                                "start": "2023-01-01",
                                "end": "2023-12-31",
                                "val": 1_000,
                                "accn": accession,
                                "form": "10-K",
                                "filed": "2024-02-20",
                            }
                        ]
                    }
                }
            }
        }
    }

    def fake_fetch(url: str) -> bytes:
        if "companyfacts" in url:
            return json.dumps(companyfacts).encode()
        if "submissions" in url:
            return json.dumps(submissions).encode()
        if "Archives" in url:
            return b"<html><body>annual filing</body></html>"
        raise AssertionError(url)

    try:
        with conn:
            conn.execute(
                """
                CREATE TRIGGER force_reporting_profile_rollback
                BEFORE INSERT ON dim_issuer_reporting_profile
                BEGIN
                    SELECT RAISE(ABORT, 'forced_profile_failure');
                END
                """
            )
        result = sync_sec_fundamentals(
            conn,
            bundle,
            tickers=["KO"],
            as_of="2024-12-31",
            fetch=fake_fetch,
        )

        assert result["filings_processed"] == 1
        assert result["filings_stored_unique"] == 1
        assert result["raw_facts"] == 0
        assert result["documents"] == 0
        assert len(result["failures"]) == 1
        assert "forced_profile_failure" in result["failures"][0]["error"]
        assert conn.execute(
            "SELECT COUNT(*) FROM fact_sec_xbrl_fact_raw WHERE ticker='KO'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM fact_sec_filing_document WHERE ticker='KO'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_sec_acceptance_time_financial_normalization_and_routed_census(tmp_path: Path) -> None:
    bundle, conn = _prepared_db(tmp_path)
    conn.execute("DELETE FROM dim_consumer_defensive_taxonomy WHERE ticker<>'KO'")
    conn.commit()
    accession = "0000000000-24-000001"
    observations = {
        "RevenueFromContractWithCustomerExcludingAssessedTax": 1000.0,
        "CostOfRevenue": 600.0,
        "OperatingIncomeLoss": 180.0,
        "NetCashProvidedByUsedInOperatingActivities": 210.0,
        "PaymentsToAcquirePropertyPlantAndEquipment": 40.0,
        "CashAndCashEquivalentsAtCarryingValue": 100.0,
        "InventoryNet": 80.0,
        "LongTermDebtNoncurrent": 300.0,
        "DepreciationDepletionAndAmortization": 20.0,
        "IncomeTaxExpenseBenefit": 30.0,
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest": 150.0,
    }
    facts = {}
    for concept, value in observations.items():
        instant = concept in {"CashAndCashEquivalentsAtCarryingValue", "InventoryNet", "LongTermDebtNoncurrent"}
        obs = {"end": "2023-12-31", "val": value, "accn": accession, "fy": 2023, "form": "10-K", "filed": "2024-02-20"}
        if not instant:
            obs["start"] = "2023-01-01"
        facts[concept] = {"units": {"USD": [obs]}}
    submissions = {
        "cik": "21344",
        "filings": {"recent": {
            "accessionNumber": [accession], "filingDate": ["2024-02-20"],
            "acceptanceDateTime": ["2024-02-20T16:30:00.000Z"], "reportDate": ["2023-12-31"],
            "form": ["10-K"], "primaryDocument": ["ko-20231231.htm"],
        }, "files": []}
    }
    companyfacts = {"cik": "21344", "facts": {"us-gaap": facts}}
    filing_html = b"<html><body>Organic sales growth was led by unit case volume growth and price mix.</body></html>"

    def fake_fetch(url: str) -> bytes:
        if "companyfacts" in url:
            return json.dumps(companyfacts).encode()
        if "submissions" in url:
            return json.dumps(submissions).encode()
        if "Archives" in url:
            return filing_html
        raise AssertionError(url)

    try:
        sync = sync_sec_fundamentals(conn, bundle, as_of="2024-12-31", fetch=fake_fetch)
        assert not sync["failures"]
        assert sync["raw_facts"] == len(observations)
        accepted = conn.execute("SELECT DISTINCT accepted_at FROM fact_sec_xbrl_fact_raw WHERE ticker='KO'").fetchall()
        assert {str(row[0]) for row in accepted} == {"2024-02-20T16:30:00.000Z"}

        conn.execute(
            """INSERT INTO fact_sec_xbrl_fact_raw(
                   ticker,cik,accession_number,taxonomy,concept,value_text,numeric_value,unit,
                   period_start,period_end,filed_date,accepted_at,form_type,frame,dimensions_json,
                   source_id,source_detail,created_at
               ) VALUES(
                   'KO','21344',NULL,'us-gaap','Revenues','2000',2000,'USD',
                   '2026-01-01','2026-12-31','2027-02-20','2027-02-20T16:30:00Z','10-K',NULL,'{}',
                   'sec_companyfacts','future_fact_guard','2027-02-20T16:30:00Z'
               )"""
        )
        with pytest.raises(RuntimeError, match="reverse replay rejected"):
            sync_sec_fundamentals(
                conn,
                bundle,
                tickers=["KO"],
                as_of="2023-12-31",
                fetch=fake_fetch,
            )
        preserved = conn.execute(
            "SELECT COUNT(*) FROM fact_sec_xbrl_fact_raw WHERE ticker='KO' AND accepted_at>'2023-12-31T23:59:59Z'"
        ).fetchone()[0]
        assert preserved == len(observations) + 1
        profile = conn.execute(
            "SELECT latest_filing_accepted_at FROM dim_issuer_reporting_profile WHERE ticker='KO'"
        ).fetchone()
        assert profile[0] == "2024-02-20T16:30:00.000Z"

        conn.execute(
            """INSERT INTO fact_sec_xbrl_fact_raw(ticker,cik,accession_number,taxonomy,concept,value_text,numeric_value,unit,period_start,period_end,filed_date,accepted_at,form_type,frame,dimensions_json,source_id,source_detail,created_at)
               VALUES('KO','21344',?,'us-gaap','InventoryNet','999',999,'GAL',NULL,'2023-12-31','2024-02-20','2024-02-20T16:30:00.000Z','10-K',NULL,'{}','sec_companyfacts','test_nonmonetary_unit','2024-02-20T16:30:00Z')""",
            (accession,),
        )
        fx = sync_fx_rates(
            conn,
            bundle,
            start="2024-01-01",
            end="2024-01-03",
            fetch=lambda _: (_ for _ in ()).throw(AssertionError("no FX fetch expected")),
        )
        assert fx["currencies"] == []
        assert not fx["ignored_non_monetary_units"]

        built = build_financial_features(conn, bundle, as_of="2024-12-31")
        assert built["canonical_facts"] == len(observations)
        row = conn.execute(
            "SELECT revenue_ttm_usd,gross_margin,operating_margin,free_cash_flow_margin,financial_quality_status "
            "FROM feature_financial_statement WHERE ticker='KO' AND asof_date='2024-12-31'"
        ).fetchone()
        assert tuple(round(float(value), 6) for value in row[:4]) == (1000.0, 0.4, 0.18, 0.17)
        assert row[4] == "partial"
        inventory = conn.execute(
            "SELECT value_usd FROM fact_financial_statement_canonical "
            "WHERE ticker='KO' AND canonical_metric='inventory'"
        ).fetchall()
        assert [float(value[0]) for value in inventory] == [80.0]

        census = run_disclosure_census(conn, bundle, as_of="2024-12-31", tickers=["KO"])
        metric_count = conn.execute("SELECT COUNT(*) FROM dim_specialized_metric").fetchone()[0]
        assert census["summary_rows"] == metric_count
        assert census["documents_parsed"] == 1
        assert census["document_parse_failures"] == []
        disclosed = conn.execute(
            "SELECT disclosure_status FROM fact_specialized_metric_disclosure_summary "
            "WHERE ticker='KO' AND metric_id='non_alcohol_unit_case_growth_pct'"
        ).fetchone()
        assert disclosed[0] == "applicable_term_hit"
        not_applicable = conn.execute(
            "SELECT disclosure_status FROM fact_specialized_metric_disclosure_summary "
            "WHERE ticker='KO' AND metric_id='tobacco_shipment_volume_growth_pct'"
        ).fetchone()
        assert not_applicable[0] == "not_applicable"
        summary_before = [tuple(row) for row in conn.execute(
            "SELECT * FROM fact_specialized_metric_disclosure_summary "
            "WHERE ticker='KO' ORDER BY metric_id"
        )]
        conn.execute(
            "UPDATE bridge_sec_filing_document_company SET content_sha256=? "
            "WHERE issuer_ticker='KO'",
            ('0' * 64,),
        )
        conn.commit()
        reconciliation = conn.execute(
            "SELECT trust_state,quarantine_reason "
            "FROM consumer_defensive_sec_reconciliation_state "
            "WHERE asof_date='2024-12-31'"
        ).fetchone()
        assert tuple(reconciliation) == (
            'invalidated_by_mutation', 'document_bridge_update'
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM consumer_defensive_sec_cache_snapshot "
            "WHERE asof_date='2024-12-31'"
        ).fetchone()[0] == 1
        with pytest.raises(RuntimeError, match="reconciliation"):
            run_disclosure_census(
                conn, bundle, as_of="2024-12-31", tickers=["KO"]
            )
        assert [tuple(row) for row in conn.execute(
            "SELECT * FROM fact_specialized_metric_disclosure_summary "
            "WHERE ticker='KO' ORDER BY metric_id"
        )] == summary_before
    finally:
        conn.close()


def test_financial_features_preserve_duration_sum_debt_components_and_use_true_invested_capital(tmp_path: Path) -> None:
    bundle, conn = _prepared_db(tmp_path)
    accepted = "2025-02-20T16:30:00Z"
    facts = [
        ("RevenueFromContractWithCustomerExcludingAssessedTax", 1000, "2024-01-01", "2024-12-31"),
        ("RevenueFromContractWithCustomerExcludingAssessedTax", 260, "2024-10-01", "2024-12-31"),
        ("OperatingIncomeLoss", 200, "2024-01-01", "2024-12-31"),
        ("DepreciationDepletionAndAmortization", 50, "2024-01-01", "2024-12-31"),
        ("IncomeTaxExpenseBenefit", 50, "2024-01-01", "2024-12-31"),
        ("IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest", 200, "2024-01-01", "2024-12-31"),
        ("CashAndCashEquivalentsAtCarryingValue", 500, None, "2024-12-31"),
        ("StockholdersEquity", 1000, None, "2024-12-31"),
        ("ShortTermBorrowings", 100, None, "2024-12-31"),
        ("LongTermDebtCurrent", 50, None, "2024-12-31"),
        ("LongTermDebtNoncurrent", 200, None, "2024-12-31"),
        ("CashAndCashEquivalentsAtCarryingValue", 500, None, "2023-12-31"),
        ("StockholdersEquity", 1000, None, "2023-12-31"),
        ("ShortTermBorrowings", 100, None, "2023-12-31"),
        ("LongTermDebtCurrent", 50, None, "2023-12-31"),
        ("LongTermDebtNoncurrent", 200, None, "2023-12-31"),
    ]
    try:
        conn.execute(
            """INSERT INTO fact_sec_filing(
                   accession_number,ticker,cik,form_type,filing_date,accepted_at,report_date,
                   primary_document,source_id,source_url,content_sha256,created_at,updated_at
               ) VALUES(
                   '0000000000-25-000001','KO','21344','10-K','2025-02-20',?,
                   '2024-12-31','ko.htm','sec_submissions','test://filing',NULL,?,?
               )""",
            (accepted, accepted, accepted),
        )
        company_id = conn.execute(
            "SELECT company_id FROM dim_company WHERE primary_ticker='KO'"
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO bridge_sec_filing_company(
                   accession_number,issuer_company_id,issuer_ticker,issuer_cik,
                   relationship,relationship_evidence,form_type,filing_date,
                   accepted_at,report_date,primary_document,source_id,source_url,
                   created_at,updated_at)
               VALUES('0000000000-25-000001',?,'KO','0000021344',
                      'associated_via_submissions','test_fixture','10-K',
                      '2025-02-20',?,'2024-12-31','ko.htm','sec_submissions',
                      'https://www.sec.gov/Archives/edgar/data/21344/000000000025000001/ko.htm',
                      ?,?)""",
            (company_id, accepted, accepted, accepted),
        )
        for position, (concept, value, start, end) in enumerate(facts):
            conn.execute(
                """INSERT INTO fact_sec_xbrl_fact_raw(ticker,cik,accession_number,taxonomy,concept,value_text,numeric_value,unit,period_start,period_end,filed_date,accepted_at,form_type,frame,dimensions_json,source_id,source_detail,created_at)
                   VALUES('KO','21344','0000000000-25-000001','us-gaap',?,?,?,?,?,?,'2025-02-20',?,'10-K',NULL,'{}','sec_companyfacts','test','2025-02-20T16:30:00Z')""",
                (concept, str(value), value, "USD", start, end, accepted),
            )
        result = build_financial_features(conn, bundle, as_of="2025-03-01")
        assert result["definition_version"] == "consumer_defensive_financial_v3"
        conn.execute(
            """INSERT INTO fact_financial_statement_canonical(
                   ticker,canonical_metric,canonical_component,statement_type,period_start,period_end,
                   accepted_at,frequency,value,reported_currency,value_usd,fx_rate,source_raw_fact_id,
                   source_id,definition_version,quality_status,created_at
               ) VALUES(
                   'KO','future_guard','total','income','2025-01-01','2025-12-31',
                   '2026-02-20T16:30:00Z','annual',1,'USD',1,1,NULL,
                   'sec_companyfacts','consumer_defensive_financial_v3','complete','2026-02-20T16:30:00Z'
               )"""
        )
        build_financial_features(conn, bundle, as_of="2025-03-01")
        assert conn.execute(
            "SELECT COUNT(*) FROM fact_financial_statement_canonical WHERE canonical_metric='future_guard'"
        ).fetchone()[0] == 1
        revenue = conn.execute(
            "SELECT period_start,frequency,value_usd FROM fact_financial_statement_canonical WHERE ticker='KO' AND canonical_metric='revenue' ORDER BY period_start"
        ).fetchall()
        assert [(row[0], row[1], row[2]) for row in revenue] == [
            ("2024-01-01", "annual", 1000.0), ("2024-10-01", "quarterly", 260.0)
        ]
        row = conn.execute(
            "SELECT return_on_invested_capital,net_debt_to_ebitda FROM feature_financial_statement WHERE ticker='KO' AND asof_date='2025-03-01'"
        ).fetchone()
        assert row[0] == pytest.approx(150.0 / 850.0)
        assert row[1] == pytest.approx(-150.0 / 250.0)
    finally:
        conn.close()


def test_ttm_rejects_noncontiguous_quarters_and_missing_debt_stays_unknown() -> None:
    rows = [
        ("revenue", "total", "2023-01-01", "2023-12-31", "2024-02-01", "annual", 1000.0),
        ("revenue", "total", "2024-01-01", "2024-03-31", "2024-05-01", "quarterly", 100.0),
        ("revenue", "total", "2024-04-01", "2024-06-30", "2024-08-01", "quarterly", 100.0),
        ("revenue", "total", "2024-10-01", "2024-12-31", "2025-02-01", "quarterly", 100.0),
        ("revenue", "total", "2025-01-01", "2025-03-31", "2025-05-01", "quarterly", 100.0),
        ("cash", "total", None, "2025-03-31", "2025-05-01", "instant", 500.0),
        ("equity", "total", None, "2025-03-31", "2025-05-01", "instant", 1000.0),
    ]
    values = _feature_values(rows)
    assert values["revenue_ttm_usd"] == 1000.0
    assert values["net_debt_to_ebitda"] is None
    assert values["return_on_invested_capital"] is None


def test_missing_acceptance_is_not_loaded_and_fx_failure_is_a_stage4_gate(tmp_path: Path) -> None:
    bundle, conn = _prepared_db(tmp_path)
    submissions = {"filings": {"recent": {
        "accessionNumber": ["missing-time"], "filingDate": ["2024-02-20"],
        "acceptanceDateTime": [""], "reportDate": ["2023-12-31"],
        "form": ["10-K"], "primaryDocument": ["ko.htm"],
    }, "files": []}}
    companyfacts = {"facts": {"us-gaap": {"Revenues": {"units": {"EUR": [{
        "end": "2023-12-31", "start": "2023-01-01", "val": 10,
        "accn": "missing-time", "form": "10-K", "filed": "2024-02-20",
    }]}}}}}
    def fetch(url: str) -> bytes:
        return json.dumps(companyfacts if "companyfacts" in url else submissions).encode()
    try:
        result = sync_sec_fundamentals(conn, bundle, tickers=["KO"], as_of="2024-12-31", fetch=fetch)
        assert result["filings_processed"] == 0 and result["filings_stored_unique"] == 0
        assert result["raw_facts"] == 0
        conn.execute(
            """INSERT INTO fact_financial_statement_canonical(ticker,canonical_metric,canonical_component,statement_type,period_start,period_end,accepted_at,frequency,value,reported_currency,value_usd,fx_rate,source_raw_fact_id,source_id,definition_version,quality_status,created_at)
               VALUES('KO','revenue','total','income','2023-01-01','2023-12-31','2024-02-20T00:00:00Z','annual',10,'EUR',NULL,NULL,NULL,'sec_companyfacts','consumer_defensive_financial_v3','fx_missing','2024-02-20T00:00:00Z')"""
        )
        validation = validate_stage4(conn, bundle, as_of="2024-12-31")
        assert validation["checks"]["canonical_fx_conversion_complete"] is False
    finally:
        conn.close()
