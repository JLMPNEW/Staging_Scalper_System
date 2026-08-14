from __future__ import annotations

import csv
import importlib
import sqlite3
from pathlib import Path

import pytest


sec_sync = importlib.import_module("industrials.scripts.07_sync_industrials_sec_fundamentals")
financial_validator = importlib.import_module("industrials.scripts.08_validate_industrials_financial_stage")


def test_incremental_new_filing_forces_companyfacts_refetch() -> None:
    new_keys = {("10-Q", "2026-06-30", "0000000000-26-000001")}

    assert not sec_sync.should_skip_incremental_companyfacts(
        incremental=True,
        new_filing_keys=new_keys,
        force_companyfacts=False,
        force_archive=False,
        prior_sync_failed=False,
        has_existing_state=True,
    )
    assert sec_sync.should_force_companyfacts_payload_fetch(incremental=True, force_companyfacts=False)


def test_incremental_can_skip_only_when_current_and_unforced() -> None:
    assert sec_sync.should_skip_incremental_companyfacts(
        incremental=True,
        new_filing_keys=set(),
        force_companyfacts=False,
        force_archive=False,
        prior_sync_failed=False,
        has_existing_state=True,
    )
    assert not sec_sync.should_skip_incremental_companyfacts(
        incremental=True,
        new_filing_keys=set(),
        force_companyfacts=False,
        force_archive=False,
        prior_sync_failed=True,
        has_existing_state=True,
    )
    assert not sec_sync.should_skip_incremental_companyfacts(
        incremental=True,
        new_filing_keys=set(),
        force_companyfacts=True,
        force_archive=False,
        prior_sync_failed=False,
        has_existing_state=True,
    )


def test_incremental_archive_scope_only_contains_new_accessions() -> None:
    new_keys = {
        ("0000000000-26-000001", "2026-07-27", "10-Q"),
        ("0000000000-26-000002", "2026-07-27", "8-K"),
    }

    assert sec_sync.incremental_archive_accession_filter(
        incremental=True,
        pending_filing_keys=new_keys,
        force_archive=False,
        has_completed_profile=True,
    ) == {"0000000000-26-000001", "0000000000-26-000002"}
    assert (
        sec_sync.incremental_archive_accession_filter(
            incremental=True,
            pending_filing_keys=new_keys,
            force_archive=True,
            has_completed_profile=True,
        )
        is None
    )
    assert (
        sec_sync.incremental_archive_accession_filter(
            incremental=True,
            pending_filing_keys=new_keys,
            force_archive=False,
            has_completed_profile=False,
        )
        is None
    )

    interrupted_keys = {
        ("0000000000-26-000003", "2026-07-28", "8-K"),
    }
    assert sec_sync.incremental_archive_accession_filter(
        incremental=True,
        pending_filing_keys=interrupted_keys,
        force_archive=False,
        has_completed_profile=True,
    ) == {"0000000000-26-000003"}


def test_exact_accession_recovery_enables_core_statement_tables() -> None:
    assert not sec_sync.should_limit_archive_to_special_metrics(
        archive_all_family_members=True,
        archive_attempt_override=False,
        ticker="AIRJ",
        core_metric_recovery_tickers=set(),
        accession_filter={"0001193125-26-349663"},
    )


def test_broad_family_scan_remains_special_metrics_only() -> None:
    assert sec_sync.should_limit_archive_to_special_metrics(
        archive_all_family_members=True,
        archive_attempt_override=False,
        ticker="CAT",
        core_metric_recovery_tickers=set(),
        accession_filter=None,
    )
    assert not sec_sync.should_limit_archive_to_special_metrics(
        archive_all_family_members=True,
        archive_attempt_override=False,
        ticker="ATS",
        core_metric_recovery_tickers={"ATS"},
        accession_filter=None,
    )


def test_filing_keys_respects_pit_end_date() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE fact_sec_filing (
            ticker TEXT,
            source_id TEXT,
            accession_number TEXT,
            filing_date TEXT,
            form_type TEXT
        )
        """
    )
    conn.executemany(
        "INSERT INTO fact_sec_filing VALUES (?, ?, ?, ?, ?)",
        [
            ("CAT", "sec_submissions", "old", "2026-07-27", "8-K"),
            ("CAT", "sec_submissions", "future", "2026-07-28", "8-K"),
        ],
    )

    assert sec_sync.filing_keys(
        conn,
        ticker="CAT",
        source_id="sec_submissions",
        end_date="2026-07-27",
    ) == {("old", "2026-07-27", "8-K")}


def test_sec_user_agent_expands_env_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INDUSTRIALS_SEC_USER_AGENT", raising=False)
    config = {
        "sec_fundamentals": {
            "user_agent": "${INDUSTRIALS_SEC_USER_AGENT:-JL, Independent Research, jm.357@hotmail.com}"
        }
    }

    assert sec_sync.resolve_sec_user_agent(config) == "JL, Independent Research, jm.357@hotmail.com"


def test_sec_user_agent_rejects_unresolved_template_with_email_tripwire() -> None:
    config = {"sec_fundamentals": {"user_agent": "${BROKEN_TEMPLATE:-still_has_email@example.com"}}

    with pytest.raises(ValueError, match="must resolve"):
        sec_sync.resolve_sec_user_agent(config)


def test_transportation_reviewed_concept_alias_is_loaded() -> None:
    project_root = Path(__file__).resolve().parents[2]
    config_path = project_root / "industrials" / "config.yaml"
    config = sec_sync.load_yaml(config_path)
    concept_map: dict[tuple[str, str], list[dict[str, object]]] = {}

    sec_sync.add_family_concept_mappings(
        concept_map,
        model_family="transportation",
        config=config,
        base_dir=config_path.parent,
    )

    key = ("ifrs-full", "RevenueFromRenderingOfTransportServices")
    assert key in concept_map
    assert concept_map[key] == [
        {
            "taxonomy": "ifrs-full",
            "concept_name": "RevenueFromRenderingOfTransportServices",
            "canonical_metric": "revenue",
            "financial_statement": "income_statement",
            "period_type": "duration",
            "sign_policy": "as_reported",
            "priority": 20,
        }
    ]
    expected_capex_aliases = {
        ("us-gaap", "PaymentsToAcquireProductiveAssets"),
        ("us-gaap", "PaymentsToAcquireOtherProductiveAssets"),
        ("us-gaap", "PaymentsToAcquireOtherPropertyPlantAndEquipment"),
        ("us-gaap", "PaymentsForFlightEquipment"),
        (
            "ifrs-full",
            "PurchaseOfPropertyPlantAndEquipmentIntangibleAssetsOtherThanGoodwillInvestmentPropertyAndOtherNoncurrentAssets",
        ),
        ("us-gaap", "PaymentsToAcquireEquipmentOnLease"),
        ("us-gaap", "PaymentsToAcquireMachineryAndEquipment"),
    }
    for capex_key in expected_capex_aliases:
        assert capex_key in concept_map
        assert concept_map[capex_key][0]["canonical_metric"] == "capex"
        assert concept_map[capex_key][0]["period_type"] == "duration"
        assert concept_map[capex_key][0]["sign_policy"] == "positive_abs"
    debt_key = ("ifrs-full", "ProceedsFromCurrentBorrowings")
    assert debt_key in concept_map
    assert concept_map[debt_key][0]["canonical_metric"] == "debt_issuance_proceeds"
    assert concept_map[debt_key][0]["period_type"] == "duration"
    assert concept_map[debt_key][0]["sign_policy"] == "positive_abs"


def test_legacy_archive_uses_canonical_complete_submission_filename() -> None:
    assert (
        sec_sync.archive_raw_submission_document_name("0000890662-00-000017")
        == "0000890662-00-000017.txt"
    )
    assert sec_sync.archive_raw_submission_document_name("000089066200000017") == ""


def test_legacy_ascii_statement_parser_recovers_scaled_core_facts() -> None:
    facts = sec_sync.parse_archive_legacy_ascii_table_facts(
        """
        <TABLE>
        <CAPTION>
        CONDENSED CONSOLIDATED BALANCE SHEETS
        (In thousands)
        August 31,
        2000 1999
        <S> <C> <C>
        TOTAL ASSETS  $ 906,188  $ 825,232
        </TABLE>
        <TABLE>
        <CAPTION>
        CONDENSED CONSOLIDATED STATEMENTS OF OPERATIONS
        (In thousands)
        Three Months Ended August 31,
        2000 1999
        <S> <C> <C>
        REVENUE  $ 412,950  $ 391,651
        NET REVENUE $ 153,112 $ 150,650
        NET INCOME $ 5,847 $ 5,979
        </TABLE>
        """,
        document_name="0000890662-00-000017.txt",
        filing={
            "form_type": "10-Q",
            "report_date": "2000-08-31",
            "filing_date": "2000-10-11",
        },
        company_currency="USD",
    )
    values = {
        (fact.concept_name, fact.period_end): fact.value
        for fact in facts
    }
    assert values[("Assets", "2000-08-31")] == 906_188_000
    assert values[("Assets", "1999-08-31")] == 825_232_000
    assert values[("Revenue", "2000-08-31")] == 412_950_000
    assert values[("Revenue", "1999-08-31")] == 391_651_000
    assert not any(
        fact.concept_name == "Revenue" and fact.value == 153_112_000
        for fact in facts
    )
    assert all(fact.source_detail == sec_sync.TEXT_TABLE_SOURCE_DETAIL for fact in facts)


def test_text_table_statement_guard_keeps_ifrs_income_statement() -> None:
    """Regression: the statement-semantic guard must not drop income-statement
    concepts when the table heading is unrecognized (financial_summary).

    CAD IFRS foreign private issuers (e.g. MDA Space) title their income
    statement "Statement of Comprehensive Income"/"Statement of Earnings", which
    the classifier reports as financial_summary. Treating that default as a
    statement conflict silently dropped Revenue/OperatingIncome/NetIncome and
    failed the FPI-hybrid completeness gate.
    """
    # The IFRS heading is genuinely unrecognized -> financial_summary default.
    assert (
        sec_sync.text_table_statement_provenance(
            "Condensed Consolidated Statement of Comprehensive Income", ""
        )[0]
        == "financial_summary"
    )
    assert "financial_summary" not in sec_sync.RECOGNIZED_STATEMENT_TYPES

    facts = sec_sync.parse_archive_text_table_facts(
        """
        <p>Condensed Consolidated Statement of Comprehensive Income (in millions of Canadian dollars)</p>
        <table>
          <tr><th>Three months ended March 31</th></tr>
          <tr><th></th><th>2026</th><th>2025</th></tr>
          <tr><td>Revenue</td><td>464.1</td><td>351.0</td></tr>
          <tr><td>Gross profit</td><td>115.0</td><td>90.0</td></tr>
          <tr><td>Income from operations</td><td>40.1</td><td>35.3</td></tr>
          <tr><td>Net income</td><td>29.6</td><td>32.9</td></tr>
        </table>
        """,
        document_name="ex99-2.htm",
        filing={"report_date": "2026-03-31", "filing_date": "2026-05-01", "form_type": "6-K"},
        company_currency="CAD",
    )
    values_by_concept = {
        (fact.concept_name, fact.period_end): round(fact.value)
        for fact in facts
    }
    assert values_by_concept[("Revenue", "2026-03-31")] == 464_100_000
    assert values_by_concept[("Revenue", "2025-03-31")] == 351_000_000
    assert values_by_concept[("OperatingIncomeLoss", "2026-03-31")] == 40_100_000
    assert values_by_concept[("NetIncomeLoss", "2026-03-31")] == 29_600_000


def test_supplemental_earnings_summary_recovers_two_core_facts() -> None:
    facts = sec_sync.parse_archive_text_table_facts(
        """
        <p>National Presto announces increased second quarter 2026 sales and earnings</p>
        <table>
          <tr><th></th><th>July 5, 2026</th><th>June 29, 2025</th></tr>
          <tr><td>Net Sales</td><td>$</td><td>146,596,000</td><td>$</td><td>120,449,000</td></tr>
          <tr><td>Net Earnings</td><td>$</td><td>15,862,000</td><td>$</td><td>5,152,000</td></tr>
          <tr><td>Net Earnings Per Share</td><td>$</td><td>2.21</td><td>$</td><td>0.72</td></tr>
        </table>
        """,
        document_name="exhibit-99.htm",
        filing={
            "report_date": "2026-07-05",
            "filing_date": "2026-08-03",
            "form_type": "8-K",
        },
        company_currency="USD",
    )

    current = {
        fact.concept_name: fact.value
        for fact in facts
        if fact.period_end == "2026-07-05"
    }
    assert current["Revenue"] == 146_596_000
    assert current["NetIncomeLoss"] == 15_862_000
    assert "DilutedShares" not in current


def test_supplemental_two_row_table_without_results_context_is_rejected() -> None:
    facts = sec_sync.parse_archive_text_table_facts(
        """
        <p>Selected unaudited information</p>
        <table>
          <tr><th></th><th>July 5, 2026</th><th>June 29, 2025</th></tr>
          <tr><td>Net Sales</td><td>146,596,000</td><td>120,449,000</td></tr>
          <tr><td>Net Earnings</td><td>15,862,000</td><td>5,152,000</td></tr>
        </table>
        """,
        document_name="exhibit-99.htm",
        filing={
            "report_date": "2026-07-05",
            "filing_date": "2026-08-03",
            "form_type": "8-K",
        },
        company_currency="USD",
    )

    assert facts == []


def test_semantic_statement_recovers_paragraph_serialized_ifrs_facts() -> None:
    facts = sec_sync.parse_archive_semantic_statement_facts(
        """
        <p>FINANCIAL STATEMENTS</p>
        <p>Embraer S.A. Consolidated Income Statement
        (in millions of U.S. dollars, except earnings per share)</p>
        <p>2Q26 1Q26 2Q25 1H26 1H25 Revenue 2,235.3 1,446.7 1,819.2 3,682.0 2,922.2
        Cost of sales (1,790.5) (1,186.5) (1,466.3) (2,977.0) (2,380.2)
        Gross profit 444.8 260.2 352.9 705.0 542.0
        Operating income before financial result 285.8 81.7 179.5 367.5 230.7
        Income for the period 213.9 37.1 69.3 251.0 149.1</p>
        <p>Embraer S.A. Consolidated Balance Sheet (in millions of U.S. dollars)</p>
        <p>Assets 2Q26 1Q26 2Q25 Current Cash and cash equivalents 1,392.4 1,315.7 654.0
        Total Assets 13,237.4 12,958.6 12,073.5 Liabilities 2Q26 1Q26 2Q25
        Total Liabilities 9,406.5 9,294.7 8,465.7 Total Equity 3,830.9 3,663.9 3,607.8</p>
        <p>Reconciliation of IFRS and NON-GAAP information Revenue 99,999.0 99,999.0 99,999.0</p>
        """,
        document_name="embj-results.htm",
        filing={"report_date": "2026-06-30", "filing_date": "2026-08-10", "form_type": "6-K"},
        company_currency="USD",
    )

    current = {
        fact.concept_name: fact.value
        for fact in facts
        if fact.period_end == "2026-06-30" and fact.period_start in {"", "2026-04-01"}
    }
    assert current == {
        "Assets": 13_237_400_000,
        "Equity": 3_830_900_000,
        "GrossProfit": 444_800_000,
        "Liabilities": 9_406_500_000,
        "NetIncomeLoss": 213_900_000,
        "OperatingIncomeLoss": 285_800_000,
        "Revenue": 2_235_300_000,
    }


def test_semantic_statement_rejects_adjusted_summary_without_formal_statement() -> None:
    facts = sec_sync.parse_archive_semantic_statement_facts(
        """
        <p>Financial results and adjusted highlights (in millions of U.S. dollars)</p>
        <p>2Q26 2Q25 Revenue 2,235.3 1,819.2 Adjusted EBITDA 355.6 245.5</p>
        """,
        document_name="presentation.htm",
        filing={"report_date": "2026-06-30", "filing_date": "2026-08-10", "form_type": "6-K"},
        company_currency="USD",
    )

    assert facts == []


def test_semantic_statement_rejects_missing_scale() -> None:
    facts = sec_sync.parse_archive_semantic_statement_facts(
        """
        <p>FINANCIAL STATEMENTS</p>
        <p>Consolidated Income Statement</p>
        <p>2Q26 2Q25 Revenue 2,235.3 1,819.2 Gross profit 444.8 352.9
        Operating income before financial result 285.8 179.5 Income for the period 213.9 69.3</p>
        """,
        document_name="unscaled.htm",
        filing={"report_date": "2026-06-30", "filing_date": "2026-08-10", "form_type": "6-K"},
        company_currency="USD",
    )

    assert facts == []


def test_text_table_statement_guard_still_rejects_conflicting_statement() -> None:
    """The guard must remain active for POSITIVELY recognized statement headings:
    a balance-sheet concept appearing as a working-capital line on the cash-flow
    statement is not a balance and must be rejected.
    """
    facts = sec_sync.parse_archive_text_table_facts(
        """
        <p>Consolidated Statements of Cash Flows (in thousands)</p>
        <table>
          <tr><th>Year ended December 31</th></tr>
          <tr><th></th><th>2025</th><th>2024</th></tr>
          <tr><td>Net income</td><td>10,000</td><td>9,000</td></tr>
          <tr><td>Inventories</td><td>(5,000)</td><td>(3,000)</td></tr>
          <tr><td>Accounts payable</td><td>2,000</td><td>1,000</td></tr>
        </table>
        """,
        document_name="cash-flows.htm",
        filing={"report_date": "2025-12-31", "filing_date": "2026-02-15", "form_type": "10-K"},
        company_currency="USD",
    )
    concepts = {fact.concept_name for fact in facts}
    assert "Inventory" not in concepts
    assert "AccountsPayable" not in concepts


def test_targeted_sec_report_preserves_unselected_tickers(tmp_path: Path) -> None:
    path = tmp_path / "coverage.csv"
    original = [
        {field: "" for field in sec_sync.REPORT_FIELDS} | {"ticker": "AAA", "status": "success"},
        {field: "" for field in sec_sync.REPORT_FIELDS} | {"ticker": "BBB", "status": "review"},
    ]
    sec_sync.write_report(path, original)
    replacement = [
        {field: "" for field in sec_sync.REPORT_FIELDS} | {"ticker": "BBB", "status": "success"},
    ]
    sec_sync.write_report(path, replacement, preserve_existing_tickers=True)

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["ticker"] for row in rows] == ["AAA", "BBB"]
    assert {row["ticker"]: row["status"] for row in rows} == {"AAA": "success", "BBB": "success"}


def test_profiles_all_members_requires_local_historical_profile_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "sys.argv",
        ["sec_sync.py", "--profiles-all-members"],
    )
    args = sec_sync.parse_args()
    assert args.profiles_all_members
    assert not args.profiles_only


def test_sec_cache_atomic_write_retries_transient_file_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "cache" / "filing.htm"
    real_replace = sec_sync.os.replace
    attempts = 0

    def flaky_replace(source: Path, destination: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError("transient OneDrive lock")
        real_replace(source, destination)

    monkeypatch.setattr(sec_sync.os, "replace", flaky_replace)
    sec_sync.write_cache_atomic(target, "complete filing")

    assert attempts == 3
    assert target.read_text(encoding="utf-8") == "complete filing"
    assert list(target.parent.glob("*.tmp")) == []


def test_successful_sec_retry_resolves_prior_open_failure() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE data_quality_issues (
            stage TEXT,
            model_family TEXT,
            ticker TEXT,
            source_id TEXT,
            issue_type TEXT,
            resolution_status TEXT,
            updated_at TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO data_quality_issues VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            sec_sync.RUN_TYPE,
            "machinery",
            "TEST",
            "sec_companyfacts",
            "sec_sync_failed",
            "open",
            "old",
        ),
    )

    resolved = sec_sync.resolve_successful_sync_issues(
        conn,
        ticker="TEST",
        model_family="machinery",
        source_id="sec_companyfacts",
    )

    assert resolved == 1
    status = conn.execute(
        "SELECT resolution_status FROM data_quality_issues"
    ).fetchone()[0]
    assert status == "resolved_by_successful_retry"


def test_financial_review_issue_parity_is_scoped_to_model_family() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE data_quality_issues (
            stage TEXT,
            model_family TEXT,
            ticker TEXT,
            issue_type TEXT,
            resolution_status TEXT
        )
        """
    )
    conn.executemany(
        "INSERT INTO data_quality_issues VALUES (?, ?, ?, ?, ?)",
        [
            (financial_validator.FEATURE_STAGE, "defense", "LMT", "financial_feature_review", "open"),
            (financial_validator.FEATURE_STAGE, "defense", "OLDDEF", "financial_feature_review", "open"),
            (financial_validator.FEATURE_STAGE, "machinery", "XE", "financial_feature_review", "open"),
        ],
    )

    review, stale = financial_validator.load_open_financial_review_issue_tickers(
        conn,
        model_family="defense",
        universe=["LMT"],
    )

    assert review == {"LMT"}
    assert stale == ["OLDDEF"]
