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
