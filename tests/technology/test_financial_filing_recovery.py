from __future__ import annotations

from pathlib import Path

import pytest

from technology.core.financial_filing_recovery import (
    parse_explicit_statement_tables,
    parse_inline_document_set,
)


def test_split_inline_xbrl_document_set_joins_contexts_units_and_facts(
    tmp_path: Path,
) -> None:
    metadata = tmp_path / "contexts.xml"
    metadata.write_text(
        """
        <xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
                     xmlns:iso4217="http://www.xbrl.org/2003/iso4217">
          <xbrli:context id="D1">
            <xbrli:entity><xbrli:identifier scheme="test">TEST</xbrli:identifier></xbrli:entity>
            <xbrli:period>
              <xbrli:startDate>2026-04-01</xbrli:startDate>
              <xbrli:endDate>2026-06-30</xbrli:endDate>
            </xbrli:period>
          </xbrli:context>
          <xbrli:unit id="USD"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
        </xbrli:xbrl>
        """,
        encoding="utf-8",
    )
    facts = tmp_path / "facts.htm"
    facts.write_text(
        """
        <html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL">
          <body>
            <ix:nonFraction name="ifrs-full:Revenue" contextRef="D1"
                            unitRef="USD" scale="6">7.5</ix:nonFraction>
            <ix:nonFraction name="ifrs-full:ProfitLoss" contextRef="D1"
                            unitRef="USD" scale="6" sign="-">1.2</ix:nonFraction>
          </body>
        </html>
        """,
        encoding="utf-8",
    )

    recovered = parse_inline_document_set([facts, metadata])

    values = {(fact.taxonomy, fact.concept): fact.value for fact in recovered}
    assert values[("ifrs-full", "Revenue")] == pytest.approx(7_500_000.0)
    assert values[("ifrs-full", "ProfitLoss")] == pytest.approx(-1_200_000.0)
    assert {fact.end_date for fact in recovered} == {"2026-06-30"}
    assert {fact.start_date for fact in recovered} == {"2026-04-01"}


def test_explicit_statement_table_uses_preamble_scale_and_period(tmp_path: Path) -> None:
    filing = tmp_path / "results.htm"
    filing.write_text(
        """
        <html><body>
          <p>TSMC's second quarter consolidated results</p>
          <p>Three months ended June 30, 2026</p>
          <p>(Unit: NT$ million)</p>
          <table>
            <tr><th></th><th>2Q26 Amount</th></tr>
            <tr><td>Revenue</td><td>7.5</td></tr>
            <tr><td>Net loss</td><td>(9.8)</td></tr>
          </table>
        </body></html>
        """,
        encoding="utf-8",
    )

    recovered = parse_explicit_statement_tables(
        [filing],
        report_date="2026-07-16",
        taxonomy="ifrs-full",
        fallback_currency="TWD",
    )

    values = {fact.concept: fact for fact in recovered}
    assert values["Revenue"].value == pytest.approx(7_500_000.0)
    assert values["ProfitLoss"].value == pytest.approx(-9_800_000.0)
    assert values["Revenue"].start_date == "2026-04-01"
    assert values["Revenue"].end_date == "2026-06-30"
    assert values["Revenue"].unit == "TWD"
    assert values["Revenue"].source_detail == "filing_document_explicit_statement_table"


def test_explicit_interim_table_uses_current_period_column_not_note(
    tmp_path: Path,
) -> None:
    filing = tmp_path / "foreign_interim_results.htm"
    filing.write_text(
        """
        <html><body>
          <p>Unaudited condensed consolidated interim financial statements</p>
          <p>(Unaudited in thousands of US dollars)</p>
          <p>Results for the three months ended December 31, 2025</p>
          <table>
            <tr><th></th><th></th><th>Three month periods ended</th><th></th>
                <th>Six month periods ended</th></tr>
            <tr><th></th><th>Note</th><th>2025</th><th>2024</th>
                <th>2025</th><th>2024</th></tr>
            <tr><td>Revenue</td><td>18</td><td>51,450</td><td>59,113</td>
                <td>102,268</td><td>119,263</td></tr>
            <tr><td>Gross profit</td><td></td><td>38,246</td><td>40,488</td>
                <td>75,051</td><td>81,669</td></tr>
            <tr><td>Net loss</td><td></td><td>(1,996)</td><td>(1,881)</td>
                <td>(4,333)</td><td>(3,791)</td></tr>
          </table>
        </body></html>
        """,
        encoding="utf-8",
    )

    recovered = parse_explicit_statement_tables(
        [filing],
        report_date="2025-12-31",
        taxonomy="ifrs-full",
        fallback_currency="USD",
    )

    values = {fact.concept: fact for fact in recovered}
    assert values["Revenue"].value == pytest.approx(51_450_000.0)
    assert values["GrossProfit"].value == pytest.approx(38_246_000.0)
    assert values["ProfitLoss"].value == pytest.approx(-1_996_000.0)
    assert values["Revenue"].start_date == "2025-10-01"
    assert values["Revenue"].end_date == "2025-12-31"


def test_explicit_statement_table_does_not_use_6k_event_date_as_period(
    tmp_path: Path,
) -> None:
    filing = tmp_path / "event_date_only.htm"
    filing.write_text(
        """
        <html><body>
          <p>Fourth quarter financial results</p>
          <table>
            <tr><th></th><th>Three Months Ended</th></tr>
            <tr><th></th><th>Current Period</th></tr>
            <tr><td>Revenue</td><td>744.9</td></tr>
            <tr><td>Net income</td><td>304.5</td></tr>
          </table>
        </body></html>
        """,
        encoding="utf-8",
    )

    recovered = parse_explicit_statement_tables(
        [filing],
        report_date="2026-02-12",
        taxonomy="us-gaap",
        fallback_currency="USD",
    )

    assert recovered == []


def test_explicit_statement_table_recognizes_us_dollar_000_scale(
    tmp_path: Path,
) -> None:
    filing = tmp_path / "earnings_release.htm"
    filing.write_text(
        """
        <html><body>
          <p>Second quarter consolidated financial results</p>
          <table>
            <tr><th>in US $000</th><th>Three month periods ended December 31</th></tr>
            <tr><th></th><th>2025</th></tr>
            <tr><td>Revenue</td><td>51,450</td></tr>
            <tr><td>Net loss</td><td>(1,996)</td></tr>
          </table>
        </body></html>
        """,
        encoding="utf-8",
    )

    recovered = parse_explicit_statement_tables(
        [filing],
        report_date="2025-12-31",
        taxonomy="ifrs-full",
        fallback_currency="USD",
    )

    values = {fact.concept: fact for fact in recovered}
    assert values["Revenue"].value == pytest.approx(51_450_000.0)
    assert values["ProfitLoss"].value == pytest.approx(-1_996_000.0)
