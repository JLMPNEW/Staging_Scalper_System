from __future__ import annotations

from pathlib import Path

import pytest

from technology.core.financial_filing_recovery import parse_explicit_financial_prose


def test_explicit_operating_cash_flow_prose_requires_matching_quarter(
    tmp_path: Path,
) -> None:
    filing = tmp_path / "results.htm"
    filing.write_text(
        """
        <html><body>
          <p>All financial figures are in United States dollars (USD).</p>
          <p>Financial results for the fourth quarter ended December 31, 2025.</p>
          <p>Cash flow from operating activities in the fourth quarter of 2025
             was ($11.6) million compared with ($8.7) million in 2024.</p>
        </body></html>
        """,
        encoding="utf-8",
    )

    recovered = parse_explicit_financial_prose(
        [filing],
        report_date="2026-03-31",
        taxonomy="ifrs-full",
        fallback_currency="USD",
    )
    mismatched = parse_explicit_financial_prose(
        [filing],
        report_date="2025-09-30",
        taxonomy="ifrs-full",
        fallback_currency="USD",
    )

    assert len(recovered) == 1
    assert recovered[0].concept == "CashFlowsFromUsedInOperatingActivities"
    assert recovered[0].value == pytest.approx(-11_600_000.0)
    assert recovered[0].start_date == "2025-10-01"
    assert recovered[0].source_detail == "filing_document_explicit_financial_prose"
    assert mismatched == []


def test_explicit_revenue_and_cash_prose_require_issuer_period_dates(
    tmp_path: Path,
) -> None:
    filing = tmp_path / "financial_highlights.htm"
    filing.write_text(
        """
        <html><body>
          <p>Financial results for the three months ended March 31, 2026.</p>
          <p>Revenue was $548,000 for the three months ended March 31, 2026,
             compared with $844,000 in 2025.</p>
          <p>$17.7 million in cash and cash equivalents as of March 31, 2026.</p>
        </body></html>
        """,
        encoding="utf-8",
    )

    recovered = parse_explicit_financial_prose(
        [filing],
        report_date="2026-05-27",
        taxonomy="ifrs-full",
        fallback_currency="USD",
    )

    by_concept = {fact.concept: fact for fact in recovered}
    assert by_concept["Revenue"].value == pytest.approx(548_000.0)
    assert by_concept["Revenue"].start_date == "2026-01-01"
    assert by_concept["CashAndCashEquivalents"].value == pytest.approx(17_700_000.0)
    assert by_concept["CashAndCashEquivalents"].start_date == ""
