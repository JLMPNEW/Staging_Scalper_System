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
