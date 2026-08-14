from __future__ import annotations

import importlib
import sqlite3


recovery = importlib.import_module(
    "industrials.scripts.07c_recover_industrials_financial_lineage"
)


def filing(
    ticker: str,
    accession: str,
    form_type: str,
    filing_date: str,
    report_date: str,
    *,
    accepted_at: str = "",
) -> tuple[str, ...]:
    return (
        ticker,
        accession,
        form_type,
        filing_date,
        accepted_at,
        report_date,
        f"{ticker.lower()}.htm",
    )


def lineage(
    ticker: str,
    *,
    accession: str,
    form_type: str,
    filing_date: str,
    report_date: str,
    classification: str = "CANONICALIZATION_GAP",
) -> dict[str, str]:
    return {
        "ticker": ticker,
        "financial_lineage_classification": classification,
        "latest_material_financial_accession": accession,
        "latest_material_financial_form": form_type,
        "latest_material_financial_filing_date": filing_date,
        "latest_material_financial_report_date": report_date,
    }


def test_recovery_scope_is_exact_bounded_and_companion_aware() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE fact_sec_filing (
            ticker TEXT,
            accession_number TEXT,
            form_type TEXT,
            filing_date TEXT,
            accepted_at TEXT,
            report_date TEXT,
            primary_document TEXT
        )
        """
    )
    conn.executemany(
        "INSERT INTO fact_sec_filing VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            filing("AAA", "latest", "6-K", "2026-08-10", "2026-06-30"),
            filing("AAA", "companion", "6-K", "2026-08-10", "2026-06-30"),
            filing("AAA", "same-day", "20-F", "2026-08-10", "2025-12-31"),
            filing("AAA", "unrelated", "6-K", "2026-07-01", "2026-03-31"),
            filing("AAA", "future", "6-K", "2026-08-13", "2026-06-30", accepted_at="2026-08-14"),
            filing("BBB", "already-safe", "10-Q", "2026-08-09", "2026-06-30"),
            filing("CCC", "event", "8-K", "2026-08-10", ""),
            filing("CCC", "bounded-periodic", "10-Q", "2026-08-09", "2026-06-30"),
            filing("CCC", "too-old", "10-Q", "2026-08-06", "2026-03-31"),
        ],
    )

    scope = recovery.build_recovery_scope(
        conn,
        lineage_rows=[
            lineage(
                "AAA",
                accession="latest",
                form_type="6-K",
                filing_date="2026-08-10",
                report_date="2026-06-30",
            ),
            lineage(
                "BBB",
                accession="already-safe",
                form_type="10-Q",
                filing_date="2026-08-09",
                report_date="2026-06-30",
                classification="INCORPORATED",
            ),
            lineage(
                "CCC",
                accession="event",
                form_type="8-K",
                filing_date="2026-08-10",
                report_date="",
            ),
        ],
        asof="2026-08-13",
        max_accessions_per_ticker=3,
    )

    assert [(row["ticker"], row["accession_number"]) for row in scope] == [
        ("AAA", "latest"),
        ("AAA", "companion"),
        ("AAA", "same-day"),
        ("CCC", "event"),
        ("CCC", "bounded-periodic"),
    ]
    reasons = {row["accession_number"]: row["scope_reason"] for row in scope}
    assert reasons == {
        "latest": "latest_material_canonicalization_gap",
        "companion": "same_report_date_companion",
        "same-day": "same_filing_date_companion",
        "event": "latest_material_canonicalization_gap",
        "bounded-periodic": "bounded_periodic_companion",
    }


def test_recovery_scope_rejects_invalid_bound() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        recovery.build_recovery_scope(
            conn,
            lineage_rows=[],
            asof="2026-08-13",
            max_accessions_per_ticker=0,
        )
    except ValueError as exc:
        assert "at least 1" in str(exc)
    else:
        raise AssertionError("zero accession bound unexpectedly accepted")
