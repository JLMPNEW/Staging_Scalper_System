"""Regression tests for the 2026-07-20 exhaustive parser audit fixes."""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from dedicated_parser.golden import validate_corpus
from dedicated_parser.policy import export_policy_golden_corpus, load_review_policies
from dedicated_parser.providers.arelle_provider import _numeric_fact_value
from dedicated_parser.semantic import parse_semantic_document
from industrials.machinery.dedicated_parser_adapter import _date_from_text, _money_from_text

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REGISTRY_CSV = (
    PROJECT_ROOT
    / "industrials"
    / "machinery"
    / "review_policies"
    / "dedicated_parser_review_policy.csv"
)
GENERATED_CORPUS = (
    PROJECT_ROOT
    / "dedicated_parser"
    / "golden_corpus"
    / "machinery_policy_generated.json"
)


def test_policy_generated_corpus_has_no_drift(tmp_path: Path) -> None:
    """The committed generated corpus must equal a fresh regeneration."""
    regenerated_path = tmp_path / "regenerated.json"
    export_policy_golden_corpus(
        load_review_policies(REGISTRY_CSV),
        output_path=regenerated_path,
        corpus_id="machinery_review_policy_generated",
    )
    committed = json.loads(GENERATED_CORPUS.read_text(encoding="utf-8"))
    regenerated = json.loads(regenerated_path.read_text(encoding="utf-8"))
    assert committed == regenerated


def test_generated_expectations_carry_policy_tolerance() -> None:
    committed = json.loads(GENERATED_CORPUS.read_text(encoding="utf-8"))
    assert committed["expectations"], "corpus must not be empty"
    assert all(
        "value_tolerance" in expectation
        for expectation in committed["expectations"]
    )


def test_golden_validation_fails_on_zero_evaluated_expectations(
    tmp_path: Path,
) -> None:
    corpus_path = tmp_path / "empty.json"
    corpus_path.write_text(
        json.dumps({"corpus_id": "empty", "expectations": []}),
        encoding="utf-8",
    )
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE fact_sec_metric_disclosure_candidate (
            ticker TEXT, accession_number TEXT, document_name TEXT,
            metric_name TEXT, candidate_status TEXT, candidate_value REAL,
            unit TEXT, period_start TEXT, period_end TEXT, status_reason TEXT
        )
        """
    )
    errors = validate_corpus(
        conn,
        corpus_path=corpus_path,
        table="fact_sec_metric_disclosure_candidate",
    )
    assert any("zero expectations evaluated" in error for error in errors)


def test_golden_run_id_rejected_for_fact_table(tmp_path: Path) -> None:
    corpus_path = tmp_path / "corpus.json"
    corpus_path.write_text(
        json.dumps({"corpus_id": "x", "expectations": []}),
        encoding="utf-8",
    )
    conn = sqlite3.connect(":memory:")
    with pytest.raises(ValueError, match="run_id filtering"):
        validate_corpus(
            conn,
            corpus_path=corpus_path,
            table="fact_sec_metric_disclosure_candidate",
            run_id=7,
        )


def test_golden_validation_supports_review_evaluation_overlay(
    tmp_path: Path,
) -> None:
    corpus_path = tmp_path / "review-corpus.json"
    corpus_path.write_text(
        json.dumps(
            {
                "corpus_id": "review",
                "expectations": [
                    {
                        "id": "reviewed",
                        "ticker": "TEST",
                        "accession_number": "0000000001-26-000001",
                        "document_name": "test.htm",
                        "metric_name": "operating_ratio",
                        "candidate_status": "ACCEPTED",
                        "candidate_value": 0.72,
                        "unit": "ratio",
                        "period_start": "",
                        "period_end": "2025-12-31",
                        "reason_contains": "confirmed",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE sec_parser_review_evidence(
            evaluation_id INTEGER, ticker TEXT, accession_number TEXT,
            source_document TEXT, metric_name TEXT, candidate_status TEXT,
            candidate_value REAL, unit TEXT, period_start TEXT,
            period_end TEXT, status_reason TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO sec_parser_review_evidence
        VALUES(
            7, 'TEST', '0000000001-26-000001', 'test.htm',
            'operating_ratio', 'ACCEPTED', 0.72, 'ratio', '',
            '2025-12-31', 'confirmed'
        )
        """
    )

    assert (
        validate_corpus(
            conn,
            corpus_path=corpus_path,
            table="sec_parser_review_evidence",
            evaluation_id=7,
        )
        == []
    )
    with pytest.raises(ValueError, match="evaluation_id is required"):
        validate_corpus(
            conn,
            corpus_path=corpus_path,
            table="sec_parser_review_evidence",
        )


def test_nested_table_preserves_both_levels() -> None:
    document = parse_semantic_document(
        "<table><tr><td>A</td><td>"
        "<table><tr><td>X</td><td>1</td></tr></table>B"
        "</td></tr></table>",
        source_document="nested.htm",
    )
    rows = {block.cells for block in document.table_rows}
    assert ("X", "1") in rows
    assert ("A", "B") in rows
    table_ids = {block.table_id for block in document.table_rows}
    assert len(table_ids) == 2


def test_rowspan_label_carries_down_and_keeps_alignment() -> None:
    document = parse_semantic_document(
        "<table>"
        "<tr><th>Segment</th><th>2026</th><th>2025</th></tr>"
        '<tr><td rowspan="2">Industrial</td><td>10</td><td>9</td></tr>'
        "<tr><td>20</td><td>18</td></tr>"
        "</table>",
        source_document="rowspan.htm",
    )
    data_rows = [
        block for block in document.table_rows if block.header_cells
    ]
    assert [row.cells for row in data_rows] == [
        ("Industrial", "10", "9"),
        ("Industrial", "20", "18"),
    ]
    assert all(
        row.header_cells == ("Segment", "2026", "2025") for row in data_rows
    )


def test_th_row_label_beside_td_data_is_a_data_row() -> None:
    document = parse_semantic_document(
        "<table>"
        "<tr><th>Metric</th><th>Amount</th><th>Prior</th></tr>"
        "<tr><th>Backlog</th><td>1,800</td><td>1,500</td></tr>"
        "</table>",
        source_document="th_row.htm",
    )
    data_rows = [
        block for block in document.table_rows if block.header_cells
    ]
    assert len(data_rows) == 1
    assert data_rows[0].cells == ("Backlog", "1,800", "1,500")
    # Numeric values must not contaminate the table headers.
    assert "1,800" not in " ".join(data_rows[0].header_cells)


def test_truncated_document_keeps_trailing_row() -> None:
    document = parse_semantic_document(
        "<table><tr><td>Backlog</td><td>2,400",
        source_document="truncated.htm",
    )
    assert any(
        block.cells == ("Backlog", "2,400") for block in document.table_rows
    )


def test_split_multi_row_header_date_does_not_fabricate_year_end() -> None:
    assert (
        _date_from_text("Three Months Ended June 30, | 2025", fallback="")
        == "2025-06-30"
    )
    # A lone comparative-year header must not fabricate December 31.
    assert _date_from_text("2024", fallback="2026-03-31") == "2026-03-31"
    # Year-end context still resolves the bare year.
    assert (
        _date_from_text("Fiscal year 2024", fallback="") == "2024-12-31"
    )


def test_table_money_scale_letter_requires_word_boundary() -> None:
    parsed = _money_from_text(
        "1,234 based",
        context="backlog table",
        company_currency="USD",
        minimum_value=0.0,
    )
    assert parsed is not None
    assert parsed[0] == pytest.approx(1234.0)


def test_arelle_numeric_value_uses_scale_adjusted_xvalue() -> None:
    fact = SimpleNamespace(xValue=Decimal("9199000"))
    assert _numeric_fact_value(fact, "9,199") == 9_199_000.0


def test_arelle_numeric_value_falls_back_to_display_text() -> None:
    fact = SimpleNamespace(xValue=None)
    assert _numeric_fact_value(fact, "9,199") == 9_199.0
