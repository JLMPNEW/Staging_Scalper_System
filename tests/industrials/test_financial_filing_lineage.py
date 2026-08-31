from __future__ import annotations

import sqlite3
from pathlib import Path

from industrials.core.financial_filing_lineage import (
    _document_has_financial_disclosure,
    apply_financial_lineage_gate,
    build_financial_filing_lineage,
    filing_market_availability_date,
    validate_financial_lineage_rank_rows,
)


ASOF = "2026-08-13"


def test_filing_market_availability_moves_friday_after_close_to_monday() -> None:
    assert filing_market_availability_date(
        {
            "accepted_at": "2026-08-28T21:15:00Z",
            "filing_date": "2026-08-28",
        }
    ) == "2026-08-31"


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE fact_sec_filing (
            ticker TEXT,
            accession_number TEXT,
            form_type TEXT,
            filing_date TEXT,
            accepted_at TEXT,
            report_date TEXT,
            primary_document TEXT,
            filing_url TEXT
        );
        CREATE TABLE fact_financial_statement_canonical (
            ticker TEXT,
            model_family TEXT,
            canonical_metric TEXT,
            accession_number TEXT,
            accepted_at TEXT,
            filing_date TEXT
        );
        CREATE TABLE feature_financial_statement (
            ticker TEXT,
            model_family TEXT,
            asof_date TEXT
        );
        CREATE TABLE sec_parser_document_catalog (
            accession_number TEXT,
            source_path TEXT,
            is_full_submission INTEGER,
            is_primary INTEGER,
            file_size INTEGER
        );
        """
    )
    return conn


def _filing(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    accession: str,
    form: str,
    filing_date: str,
    accepted_at: str,
    report_date: str,
) -> None:
    conn.execute(
        "INSERT INTO fact_sec_filing VALUES (?,?,?,?,?,?,?,?)",
        (ticker, accession, form, filing_date, accepted_at, report_date, "", ""),
    )


def _facts(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    accession: str,
    filing_date: str,
) -> None:
    conn.executemany(
        "INSERT INTO fact_financial_statement_canonical VALUES (?,?,?,?,?,?)",
        [
            (ticker, "machinery", metric, accession, filing_date, filing_date)
            for metric in ("revenue", "assets", "operating_income")
        ],
    )


def _document(
    conn: sqlite3.Connection,
    tmp_path: Path,
    *,
    accession: str,
    text: str,
) -> None:
    path = tmp_path / f"{accession}.html"
    path.write_text(text, encoding="utf-8")
    conn.execute(
        "INSERT INTO sec_parser_document_catalog VALUES (?,?,?,?,?)",
        (accession, str(path), 0, 1, path.stat().st_size),
    )


def test_paired_foreign_filer_cover_resolves_to_same_period_canonical_facts(
    tmp_path: Path,
) -> None:
    conn = _connection()
    conn.execute(
        "INSERT INTO feature_financial_statement VALUES (?,?,?)",
        ("CAE", "machinery", ASOF),
    )
    _filing(
        conn,
        ticker="CAE",
        accession="data",
        form="6-K",
        filing_date="2026-08-12",
        accepted_at="2026-08-12T08:00:00",
        report_date="2026-06-30",
    )
    _facts(conn, ticker="CAE", accession="data", filing_date="2026-08-12")
    _filing(
        conn,
        ticker="CAE",
        accession="cover",
        form="6-K",
        filing_date="2026-08-12",
        accepted_at="2026-08-12T09:00:00",
        report_date="2026-06-30",
    )
    _document(
        conn,
        tmp_path,
        accession="cover",
        text="Financial results for the three months ended June 30, 2026.",
    )

    lineage = build_financial_filing_lineage(
        conn,
        model_family="machinery",
        asof=ASOF,
        tickers=["CAE"],
    )["CAE"]

    assert lineage["financial_lineage_status"] == "INCORPORATED"
    assert lineage["financial_lineage_gate"] == "1"
    assert lineage["latest_material_financial_accession"] == "cover"
    assert lineage["incorporated_financial_accession"] == "data"
    assert lineage["incorporated_financial_core_metric_count"] == "3"


def test_same_day_earnings_cover_pairs_periodic_filing_with_different_report_date(
    tmp_path: Path,
) -> None:
    conn = _connection()
    conn.execute(
        "INSERT INTO feature_financial_statement VALUES (?,?,?)",
        ("XOS", "machinery", ASOF),
    )
    _filing(
        conn,
        ticker="XOS",
        accession="quarterly",
        form="10-Q",
        filing_date=ASOF,
        accepted_at=f"{ASOF}T08:00:00",
        report_date="2026-06-30",
    )
    _facts(conn, ticker="XOS", accession="quarterly", filing_date=ASOF)
    _filing(
        conn,
        ticker="XOS",
        accession="earnings-cover",
        form="8-K",
        filing_date=ASOF,
        accepted_at=f"{ASOF}T09:00:00",
        report_date=ASOF,
    )
    _document(
        conn,
        tmp_path,
        accession="earnings-cover",
        text="Item 2.02 Results of Operations and Financial Condition.",
    )

    lineage = build_financial_filing_lineage(
        conn,
        model_family="machinery",
        asof=ASOF,
        tickers=["XOS"],
    )["XOS"]

    assert lineage["financial_lineage_gate"] == "1"
    assert lineage["latest_material_financial_accession"] == "earnings-cover"
    assert lineage["incorporated_financial_accession"] == "quarterly"


def test_next_day_item_202_cover_pairs_latest_periodic_filing(
    tmp_path: Path,
) -> None:
    conn = _connection()
    _filing(
        conn,
        ticker="HY",
        accession="quarterly",
        form="10-Q",
        filing_date="2026-08-04",
        accepted_at="2026-08-04T12:00:00",
        report_date="2026-06-30",
    )
    _facts(conn, ticker="HY", accession="quarterly", filing_date="2026-08-04")
    _filing(
        conn,
        ticker="HY",
        accession="earnings-cover",
        form="8-K",
        filing_date="2026-08-05",
        accepted_at="2026-08-05T12:00:00",
        report_date="2026-08-05",
    )
    _document(
        conn,
        tmp_path,
        accession="earnings-cover",
        text="Item 2.02 Results of Operations and Financial Condition.",
    )

    lineage = build_financial_filing_lineage(
        conn,
        model_family="machinery",
        asof=ASOF,
        tickers=["HY"],
    )["HY"]

    assert lineage["financial_lineage_gate"] == "1"
    assert lineage["incorporated_financial_accession"] == "quarterly"


def test_item_202_event_date_can_identify_periodic_filing_date(
    tmp_path: Path,
) -> None:
    conn = _connection()
    _filing(
        conn,
        ticker="TEX",
        accession="quarterly",
        form="10-Q",
        filing_date="2026-07-30",
        accepted_at="2026-07-30T12:00:00",
        report_date="2026-06-30",
    )
    _facts(conn, ticker="TEX", accession="quarterly", filing_date="2026-07-30")
    _filing(
        conn,
        ticker="TEX",
        accession="earnings-cover",
        form="8-K",
        filing_date="2026-08-03",
        accepted_at="2026-08-03T12:00:00",
        report_date="2026-07-30",
    )
    _document(
        conn,
        tmp_path,
        accession="earnings-cover",
        text="Item 2.02 Results of Operations and Financial Condition.",
    )

    lineage = build_financial_filing_lineage(
        conn,
        model_family="machinery",
        asof=ASOF,
        tickers=["TEX"],
    )["TEX"]

    assert lineage["financial_lineage_gate"] == "1"
    assert lineage["incorporated_financial_accession"] == "quarterly"


def test_explicit_mismatched_period_blocks_proximity_fallback(
    tmp_path: Path,
) -> None:
    conn = _connection()
    _filing(
        conn,
        ticker="SAFE",
        accession="quarterly",
        form="10-Q",
        filing_date="2026-08-04",
        accepted_at="2026-08-04T12:00:00",
        report_date="2026-06-30",
    )
    _facts(conn, ticker="SAFE", accession="quarterly", filing_date="2026-08-04")
    _filing(
        conn,
        ticker="SAFE",
        accession="earnings-cover",
        form="8-K",
        filing_date="2026-08-05",
        accepted_at="2026-08-05T12:00:00",
        report_date="2026-08-05",
    )
    _document(
        conn,
        tmp_path,
        accession="earnings-cover",
        text=("Item 2.02 Results of Operations and Financial Condition. Results for the quarter ended July 31, 2026."),
    )

    lineage = build_financial_filing_lineage(
        conn,
        model_family="machinery",
        asof=ASOF,
        tickers=["SAFE"],
    )["SAFE"]

    assert lineage["financial_lineage_gate"] == "0"
    assert lineage["financial_lineage_classification"] == "CANONICALIZATION_GAP"


def test_unincorporated_financial_release_demotes_candidate(tmp_path: Path) -> None:
    conn = _connection()
    conn.execute(
        "INSERT INTO feature_financial_statement VALUES (?,?,?)",
        ("EVTL", "machinery", ASOF),
    )
    _filing(
        conn,
        ticker="EVTL",
        accession="release",
        form="6-K",
        filing_date=ASOF,
        accepted_at=f"{ASOF}T07:00:00",
        report_date="2026-06-30",
    )
    _document(
        conn,
        tmp_path,
        accession="release",
        text="Unaudited condensed consolidated financial results for the six months ended June 30, 2026.",
    )

    lineage = build_financial_filing_lineage(
        conn,
        model_family="machinery",
        asof=ASOF,
        tickers=["EVTL"],
    )
    rows = apply_financial_lineage_gate(
        [
            {
                "ticker": "EVTL",
                "portfolio_candidate_gate": "1",
                "portfolio_candidate_status": "eligible",
                "portfolio_candidate_reason": "ok",
            }
        ],
        lineage,
    )

    assert rows[0]["financial_lineage_status"] == "REVIEW_REQUIRED"
    assert rows[0]["financial_lineage_gate"] == "0"
    assert rows[0]["portfolio_candidate_gate"] == "0"
    assert rows[0]["portfolio_candidate_status"] == "data_review_required"
    assert validate_financial_lineage_rank_rows(rows) == []


def test_nonfinancial_supplemental_filing_does_not_mask_periodic_facts(
    tmp_path: Path,
) -> None:
    conn = _connection()
    conn.execute(
        "INSERT INTO feature_financial_statement VALUES (?,?,?)",
        ("MOB", "machinery", ASOF),
    )
    _filing(
        conn,
        ticker="MOB",
        accession="annual",
        form="20-F",
        filing_date="2026-03-15",
        accepted_at="2026-03-15T08:00:00",
        report_date="2025-12-31",
    )
    _facts(conn, ticker="MOB", accession="annual", filing_date="2026-03-15")
    _filing(
        conn,
        ticker="MOB",
        accession="agm",
        form="6-K",
        filing_date=ASOF,
        accepted_at=f"{ASOF}T09:00:00",
        report_date=ASOF,
    )
    _document(
        conn,
        tmp_path,
        accession="agm",
        text="Notice of annual general meeting and voting results.",
    )

    lineage = build_financial_filing_lineage(
        conn,
        model_family="machinery",
        asof=ASOF,
        tickers=["MOB"],
    )["MOB"]

    assert lineage["financial_lineage_status"] == "INCORPORATED"
    assert lineage["latest_material_financial_accession"] == "annual"
    assert lineage["incorporated_financial_accession"] == "annual"


def test_incidental_financial_results_phrase_does_not_mask_periodic_filing(
    tmp_path: Path,
) -> None:
    conn = _connection()
    _filing(
        conn,
        ticker="AIR",
        accession="annual",
        form="10-K",
        filing_date="2026-07-22",
        accepted_at="2026-07-22T12:00:00",
        report_date="2026-05-31",
    )
    _facts(conn, ticker="AIR", accession="annual", filing_date="2026-07-22")
    _filing(
        conn,
        ticker="AIR",
        accession="leadership",
        form="8-K",
        filing_date="2026-07-24",
        accepted_at="2026-07-24T12:00:00",
        report_date="2026-07-23",
    )
    _document(
        conn,
        tmp_path,
        accession="leadership",
        text="During the CEO's tenure the company delivered record financial results.",
    )

    lineage = build_financial_filing_lineage(
        conn,
        model_family="machinery",
        asof=ASOF,
        tickers=["AIR"],
    )["AIR"]

    assert lineage["financial_lineage_gate"] == "1"
    assert lineage["latest_material_financial_accession"] == "annual"


def test_later_earnings_cover_pairs_periodic_filing_by_explicit_period_end(
    tmp_path: Path,
) -> None:
    conn = _connection()
    _filing(
        conn,
        ticker="DDD",
        accession="quarterly",
        form="10-Q",
        filing_date="2026-08-03",
        accepted_at="2026-08-03T12:00:00",
        report_date="2026-06-30",
    )
    _facts(conn, ticker="DDD", accession="quarterly", filing_date="2026-08-03")
    _filing(
        conn,
        ticker="DDD",
        accession="earnings-cover",
        form="8-K",
        filing_date="2026-08-04",
        accepted_at="2026-08-04T12:00:00",
        report_date="2026-08-04",
    )
    _document(
        conn,
        tmp_path,
        accession="earnings-cover",
        text=(
            "Item 2.02 Results of Operations and Financial Condition. "
            "The company reported results for the quarter ended June 30, 2026."
        ),
    )

    lineage = build_financial_filing_lineage(
        conn,
        model_family="machinery",
        asof=ASOF,
        tickers=["DDD"],
    )["DDD"]

    assert lineage["financial_lineage_gate"] == "1"
    assert lineage["latest_material_financial_accession"] == "earnings-cover"
    assert lineage["incorporated_financial_accession"] == "quarterly"


def test_explicit_new_period_does_not_pair_stale_periodic_filing(tmp_path: Path) -> None:
    conn = _connection()
    _filing(
        conn,
        ticker="PH",
        accession="prior-quarter",
        form="10-Q",
        filing_date="2026-05-01",
        accepted_at="2026-05-01T12:00:00",
        report_date="2026-03-31",
    )
    _facts(conn, ticker="PH", accession="prior-quarter", filing_date="2026-05-01")
    _filing(
        conn,
        ticker="PH",
        accession="new-results",
        form="8-K",
        filing_date="2026-08-06",
        accepted_at="2026-08-06T12:00:00",
        report_date="2026-08-06",
    )
    _document(
        conn,
        tmp_path,
        accession="new-results",
        text=(
            "Item 2.02 Results of Operations and Financial Condition. "
            "The company reported results for the quarter ended June 30, 2026."
        ),
    )

    lineage = build_financial_filing_lineage(
        conn,
        model_family="machinery",
        asof=ASOF,
        tickers=["PH"],
    )["PH"]

    assert lineage["financial_lineage_gate"] == "0"
    assert lineage["financial_lineage_classification"] == "CANONICALIZATION_GAP"


def test_comparative_period_does_not_pair_stale_foreign_filing(
    tmp_path: Path,
) -> None:
    conn = _connection()
    _filing(
        conn,
        ticker="SANG",
        accession="prior-period",
        form="20-F",
        filing_date="2025-09-17",
        accepted_at="2025-09-17T12:00:00",
        report_date="2025-06-30",
    )
    _facts(
        conn,
        ticker="SANG",
        accession="prior-period",
        filing_date="2025-09-17",
    )
    _filing(
        conn,
        ticker="SANG",
        accession="latest-results",
        form="6-K",
        filing_date="2026-02-04",
        accepted_at="2026-02-04T12:00:00",
        report_date="2025-12-31",
    )
    _document(
        conn,
        tmp_path,
        accession="latest-results",
        text=(
            "Financial results for the year ended December 31, 2025, compared with the six months ended June 30, 2025."
        ),
    )

    lineage = build_financial_filing_lineage(
        conn,
        model_family="machinery",
        asof=ASOF,
        tickers=["SANG"],
    )["SANG"]

    assert lineage["latest_material_financial_accession"] == "latest-results"
    assert lineage["financial_lineage_gate"] == "0"
    assert lineage["financial_lineage_classification"] == "CANONICALIZATION_GAP"


def test_acquired_business_statement_reference_in_8k_does_not_mask_issuer_filing(
    tmp_path: Path,
) -> None:
    conn = _connection()
    _filing(
        conn,
        ticker="RKLB",
        accession="issuer-quarterly",
        form="10-Q",
        filing_date="2026-08-10",
        accepted_at="2026-08-10T12:00:00",
        report_date="2026-06-30",
    )
    _facts(conn, ticker="RKLB", accession="issuer-quarterly", filing_date="2026-08-10")
    _filing(
        conn,
        ticker="RKLB",
        accession="acquisition",
        form="8-K",
        filing_date=ASOF,
        accepted_at=f"{ASOF}T12:00:00",
        report_date=ASOF,
    )
    _document(
        conn,
        tmp_path,
        accession="acquisition",
        text=(
            "The unaudited condensed consolidated financial statements of the "
            "acquired business are incorporated by reference."
        ),
    )

    lineage = build_financial_filing_lineage(
        conn,
        model_family="machinery",
        asof=ASOF,
        tickers=["RKLB"],
    )["RKLB"]

    assert lineage["financial_lineage_gate"] == "1"
    assert lineage["latest_material_financial_accession"] == "issuer-quarterly"


def test_full_submission_contract_boilerplate_does_not_create_material_event(
    tmp_path: Path,
) -> None:
    conn = _connection()
    _filing(
        conn,
        ticker="EOSE",
        accession="quarterly",
        form="10-Q",
        filing_date="2026-08-05",
        accepted_at="2026-08-05T12:00:00",
        report_date="2026-06-30",
    )
    _facts(conn, ticker="EOSE", accession="quarterly", filing_date="2026-08-05")
    _filing(
        conn,
        ticker="EOSE",
        accession="credit-agreement",
        form="8-K",
        filing_date="2026-08-06",
        accepted_at="2026-08-06T12:00:00",
        report_date="2026-08-03",
    )
    document_path = tmp_path / "credit-agreement.txt"
    document_path.write_text(
        "Borrower shall provide unaudited consolidated financial statements each quarter.",
        encoding="utf-8",
    )
    conn.execute(
        "INSERT INTO sec_parser_document_catalog VALUES (?,?,?,?,?)",
        ("credit-agreement", str(document_path), 1, 0, document_path.stat().st_size),
    )

    lineage = build_financial_filing_lineage(
        conn,
        model_family="machinery",
        asof=ASOF,
        tickers=["EOSE"],
    )["EOSE"]

    assert lineage["financial_lineage_gate"] == "1"
    assert lineage["latest_material_financial_accession"] == "quarterly"


def test_future_quarter_end_6k_metadata_does_not_mask_prior_periodic_filing() -> None:
    conn = _connection()
    _filing(
        conn,
        ticker="ATS",
        accession="annual",
        form="40-F",
        filing_date="2026-05-28",
        accepted_at="2026-05-28T12:00:00",
        report_date="2026-03-31",
    )
    _facts(conn, ticker="ATS", accession="annual", filing_date="2026-05-28")
    _filing(
        conn,
        ticker="ATS",
        accession="future-metadata",
        form="6-K",
        filing_date="2026-08-11",
        accepted_at="2026-08-11T12:00:00",
        report_date="2026-09-30",
    )

    lineage = build_financial_filing_lineage(
        conn,
        model_family="machinery",
        asof=ASOF,
        tickers=["ATS"],
    )["ATS"]

    assert lineage["financial_lineage_gate"] == "1"
    assert lineage["latest_material_financial_accession"] == "annual"


def test_no_material_filing_with_feature_has_truthful_diagnostic() -> None:
    conn = _connection()
    conn.execute(
        "INSERT INTO feature_financial_statement VALUES (?,?,?)",
        ("EMPTY", "machinery", ASOF),
    )

    lineage = build_financial_filing_lineage(
        conn,
        model_family="machinery",
        asof=ASOF,
        tickers=["EMPTY"],
    )["EMPTY"]

    assert lineage["financial_lineage_status"] == "NO_MATERIAL_FINANCIAL_FILING"
    assert lineage["financial_lineage_gate"] == "0"
    assert lineage["financial_lineage_classification"] == "NO_MATERIAL_FILING_IDENTIFIED"
    assert lineage["financial_lineage_reason"] == "no_material_filing_identified_feature_snapshot_available"


def test_future_results_announcement_is_not_a_material_8k(tmp_path: Path) -> None:
    filing = tmp_path / "cfo_transition.htm"
    filing.write_text(
        """
        <html><body>
          <p>The company announced a chief financial officer transition.</p>
          <p>The company will report second quarter 2026 financial results on
             August 13, 2026.</p>
        </body></html>
        """,
        encoding="utf-8",
    )

    assert not _document_has_financial_disclosure(filing, form_type="8-K")
