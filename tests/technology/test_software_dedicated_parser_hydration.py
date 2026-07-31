from __future__ import annotations

import ast
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from dedicated_parser.contracts import DocumentRef, FilingRef, NormalizedFact, WorkItem
from technology.core.db import init_db
from technology.core.dedicated_parser.planner_compat import (
    ensure_shared_planner_compatibility,
    validate_shared_planner_compatibility,
)
from technology.software_infrastructure.dedicated_parser_adapter import (
    ADAPTER_VERSION,
    extract_metric_evidence,
    get_registry,
    map_normalized_facts,
)
from technology.software_infrastructure.software_parser_hydration import (
    HydrationFiling,
    hydrate_filings,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class FakeResponse:
    def __init__(self, *, status_code: int, payload: bytes) -> None:
        self.status_code = status_code
        self.content = payload


def connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def seed_source(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO source_registry(
            source_id, stage, source_name, source_type, base_url,
            created_at, updated_at
        )
        VALUES ('sec_submissions', 'stage_4', 'SEC', 'api',
                'https://data.sec.gov', '2026-01-01', '2026-01-01')
        """
    )


def test_planner_aliases_backfill_and_follow_future_inserts() -> None:
    with connection() as conn:
        seed_source(conn)
        conn.execute(
            """
            INSERT INTO fact_sec_filing(
                ticker, cik, accession_number, source_id, form_type,
                filing_date, acceptance_datetime, created_at, updated_at
            )
            VALUES ('TEST', '0000001234', '0000001234-24-000001',
                    'sec_submissions', '10-K', '2024-02-01',
                    '2024-02-01T17:00:00Z', '2024-02-01', '2024-02-01')
            """
        )
        conn.execute(
            """
            INSERT INTO fact_sec_xbrl_fact(
                fact_key, ticker, cik, taxonomy, concept, metric_name, unit,
                accession_number, source_id, filing_date, start_date, end_date,
                value, created_at, updated_at
            )
            VALUES ('fact-1', 'TEST', '0000001234', 'us-gaap',
                    'RevenueRemainingPerformanceObligation',
                    'remaining_performance_obligation', 'USD',
                    '0000001234-24-000001', 'sec_submissions', '2024-02-01',
                    NULL, '2023-12-31', 100.0, '2024-02-01', '2024-02-01')
            """
        )
        ensure_shared_planner_compatibility(conn)
        validate_shared_planner_compatibility(conn)
        filing = conn.execute("SELECT accepted_at FROM fact_sec_filing WHERE ticker='TEST'").fetchone()
        fact = conn.execute(
            """
            SELECT canonical_metric, period_start, period_end, accepted_at
            FROM fact_sec_xbrl_fact WHERE fact_key='fact-1'
            """
        ).fetchone()
        assert filing["accepted_at"] == "2024-02-01T17:00:00Z"
        assert fact["canonical_metric"] == "remaining_performance_obligation"
        assert fact["period_start"] is None
        assert fact["period_end"] == "2023-12-31"
        assert fact["accepted_at"] == "2024-02-01T17:00:00Z"

        conn.execute(
            """
            INSERT INTO fact_sec_xbrl_fact(
                fact_key, ticker, cik, taxonomy, concept, metric_name, unit,
                accession_number, source_id, filing_date, start_date, end_date,
                value, created_at, updated_at
            )
            VALUES ('fact-2', 'TEST', '0000001234', 'test',
                    'AnnualRecurringRevenue', 'annual_recurring_revenue', 'USD',
                    '0000001234-24-000001', 'sec_submissions', '2024-02-01',
                    NULL, '2023-12-31', 75.0, '2024-02-01', '2024-02-01')
            """
        )
        inserted = conn.execute(
            "SELECT canonical_metric, period_end FROM fact_sec_xbrl_fact WHERE fact_key='fact-2'"
        ).fetchone()
        assert inserted["canonical_metric"] == "annual_recurring_revenue"
        assert inserted["period_end"] == "2023-12-31"


def test_hydration_fetches_index_primary_exhibit_support_and_full_submission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_payload = {
        "directory": {
            "item": [
                {"name": "issuer-8k.htm", "type": "8-K", "description": "8-K"},
                {
                    "name": "ex991.htm",
                    "type": "EX-99.1",
                    "description": "Earnings release",
                },
                {
                    "name": "0000001234-24-000001-index-headers.html",
                    "type": "",
                    "description": "SEC accession navigation",
                },
                {"name": "issuer-2024.xsd", "type": "EX-101.SCH", "description": ""},
                {"name": "logo.jpg", "type": "GRAPHIC", "description": ""},
            ]
        }
    }
    payloads = {
        "index.json": json.dumps(index_payload).encode(),
        "issuer-8k.htm": b"<html>Primary filing</html>",
        "ex991.htm": b"<html>Annual recurring revenue was $500 million.</html>",
        "issuer-2024.xsd": b"<schema/>",
        "0000001234-24-000001.txt": b"<SEC-DOCUMENT>full submission",
    }

    def fake_get(url: str, **_: Any) -> FakeResponse:
        name = url.rsplit("/", 1)[-1]
        payload = payloads.get(name)
        return FakeResponse(
            status_code=200 if payload is not None else 404,
            payload=payload or b"",
        )

    monkeypatch.setattr(
        "technology.software_infrastructure.software_parser_hydration.requests.get",
        fake_get,
    )
    filing = HydrationFiling(
        ticker="TEST",
        cik="0000001234",
        accession_number="0000001234-24-000001",
        form_type="8-K",
        filing_date="2024-02-01",
        accepted_at="2024-02-01T17:00:00Z",
        report_date="2024-02-01",
        primary_document="issuer-8k.htm",
        source_id="sec_submissions",
    )
    output_dir = tmp_path / "output"
    manifest = hydrate_filings(
        [filing],
        cache_dir=tmp_path / "cache",
        output_dir=output_dir,
        user_agent="Test Research test@example.com",
        timeout_sec=1.0,
        max_retries=1,
        request_spacing_sec=0.1,
        execute=True,
    )
    assert manifest["complete_filing_count"] == 1
    assert manifest["parser_execution_allowed_flag"] == 1
    accession_dir = tmp_path / "cache" / "sec_archive_xbrl" / "CIK0000001234" / "000000123424000001"
    assert (accession_dir / "issuer-8k.htm").is_file()
    assert (accession_dir / "ex991.htm").is_file()
    assert (accession_dir / "issuer-2024.xsd").is_file()
    assert (accession_dir / "0000001234-24-000001.txt").is_file()
    assert not (accession_dir / "logo.jpg").exists()
    assert not (accession_dir / "0000001234-24-000001-index-headers.html").exists()
    sealed = (output_dir / "software_parser_hydrated_source_manifest.csv").read_text(encoding="utf-8")
    assert "issuer-8k.htm" in sealed
    assert "ex991.htm" in sealed
    assert "0000001234-24-000001.txt" in sealed


def test_hydration_blocks_parser_when_any_selected_filing_is_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_payload = {"directory": {"item": [{"name": "primary.htm", "type": "10-K", "description": "10-K"}]}}

    def fake_get(url: str, **_: Any) -> FakeResponse:
        if url.endswith("index.json"):
            return FakeResponse(status_code=200, payload=json.dumps(index_payload).encode())
        if "000000123424000001" in url:
            return FakeResponse(status_code=200, payload=b"complete")
        return FakeResponse(status_code=404, payload=b"")

    monkeypatch.setattr(
        "technology.software_infrastructure.software_parser_hydration.requests.get",
        fake_get,
    )
    filings = [
        HydrationFiling(
            ticker="COMPLETE",
            cik="0000001234",
            accession_number="0000001234-24-000001",
            form_type="10-K",
            filing_date="2024-02-01",
            accepted_at="2024-02-01T17:00:00Z",
            report_date="2023-12-31",
            primary_document="primary.htm",
            source_id="sec_submissions",
        ),
        HydrationFiling(
            ticker="INCOMPLETE",
            cik="0000001234",
            accession_number="0000001234-24-000002",
            form_type="10-K",
            filing_date="2024-02-02",
            accepted_at="2024-02-02T17:00:00Z",
            report_date="2023-12-31",
            primary_document="primary.htm",
            source_id="sec_submissions",
        ),
    ]
    manifest = hydrate_filings(
        filings,
        cache_dir=tmp_path / "cache",
        output_dir=tmp_path / "output",
        user_agent="Test Research test@example.com",
        timeout_sec=1.0,
        max_retries=1,
        request_spacing_sec=0.1,
        execute=True,
    )
    assert manifest["selected_filing_count"] == 2
    assert manifest["complete_filing_count"] == 1
    assert manifest["incomplete_filing_count"] == 1
    assert manifest["sealed_document_count"] > 0
    assert manifest["parser_execution_allowed_flag"] == 0


def work_item(
    path: Path,
    *,
    document_name: str,
    filing_date: str = "2024-02-01",
    report_date: str = "2023-12-31",
    form_type: str = "8-K",
) -> WorkItem:
    filing = FilingRef(
        ticker="TEST",
        cik="0000001234",
        accession_number="0000001234-24-000001",
        form_type=form_type,
        filing_date=filing_date,
        accepted_at=f"{filing_date}T17:00:00Z",
        report_date=report_date,
        primary_document=document_name,
        source_id="sec_submissions",
    )
    stat = path.stat()
    return WorkItem(
        model_family="software_infrastructure",
        adapter_path=("technology.software_infrastructure.dedicated_parser_adapter:extract_metric_evidence"),
        adapter_version=ADAPTER_VERSION,
        filing=filing,
        documents=(
            DocumentRef(
                name=document_name,
                path=str(path),
                content_sha256="a" * 64,
                file_size=stat.st_size,
                modified_ns=stat.st_mtime_ns,
                is_primary=True,
            ),
        ),
        requested_metrics=get_registry().source_metrics,
        enable_arelle=False,
        enable_edgartools=False,
    )


def test_8k_prose_candidates_are_review_required(tmp_path: Path) -> None:
    path = tmp_path / "ex991.htm"
    path.write_text(
        """
        <html><body>
        <p>Annual recurring revenue was $500 million as of year end.</p>
        <p>Dollar-based net retention was 112 percent.</p>
        </body></html>
        """,
        encoding="utf-8",
    )
    evidence = extract_metric_evidence(work_item(path, document_name=path.name))
    by_metric = {row.metric_name: row for row in evidence}
    assert by_metric["annual_recurring_revenue"].value == 500_000_000
    assert by_metric["annual_recurring_revenue"].status == "REVIEW_REQUIRED"
    assert by_metric["net_revenue_retention"].value == pytest.approx(1.12)
    assert by_metric["net_revenue_retention"].status == "REVIEW_REQUIRED"


def test_prose_period_and_amount_are_anchored_to_metric(tmp_path: Path) -> None:
    path = tmp_path / "earnings.htm"
    path.write_text(
        """
        <html><body>
        <h2>Financial results for the three months ending April 30, 2026</h2>
        <p>Total revenue was $765 million. Subscription revenue was $750 million.</p>
        <p>Annual recurring revenue grew 24 percent to $1.032 billion
        as of April 30, 2026.</p>
        </body></html>
        """,
        encoding="utf-8",
    )
    evidence = extract_metric_evidence(
        work_item(
            path,
            document_name=path.name,
            filing_date="2026-05-30",
            report_date="2026-05-30",
        )
    )
    by_metric = {row.metric_name: row for row in evidence}
    assert by_metric["subscription_revenue"].value == 750_000_000
    assert by_metric["annual_recurring_revenue"].value == 1_032_000_000
    assert by_metric["annual_recurring_revenue"].period_end == "2026-04-30"
    assert by_metric["annual_recurring_revenue"].provenance["period_source"] == ("local_period_cue")


def test_preceding_deferred_revenue_value_is_tightly_linked(tmp_path: Path) -> None:
    path = tmp_path / "purchase.htm"
    path.write_text(
        """
        <html><body><p>
        RPO was $2 million, of which $0.38 million is recorded as deferred revenue
        and $1.62 million will be recognized later.
        </p></body></html>
        """,
        encoding="utf-8",
    )
    evidence = extract_metric_evidence(work_item(path, document_name=path.name))
    deferred = next(row for row in evidence if row.metric_name == "deferred_revenue_total")
    assert deferred.value == 380_000


def test_total_and_current_rpo_are_separate_metrics(tmp_path: Path) -> None:
    path = tmp_path / "rpo.htm"
    path.write_text(
        """
        <html><body>
        <p>RPO was $1,458.6 million and cRPO was $766.3 million as of January 31, 2024.</p>
        <p>RPO was $441 million. We expect to recognize approximately $316 million
        over the next 12 months.</p>
        </body></html>
        """,
        encoding="utf-8",
    )
    evidence = extract_metric_evidence(work_item(path, document_name=path.name))
    values = {(row.metric_name, row.value) for row in evidence}
    assert ("remaining_performance_obligation", 1_458_600_000) in values
    assert ("current_remaining_performance_obligation", 766_300_000) in values
    assert ("remaining_performance_obligation", 441_000_000) in values
    assert ("current_remaining_performance_obligation", 316_000_000) in values


def test_non_level_and_non_consolidated_arr_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "arr_policy.htm"
    path.write_text(
        """
        <html><body>
        <p>Customers with ARR over $100,000 increased during the quarter.</p>
        <p>Booked annual recurring revenue was $25 million.</p>
        <p>We expect ARR to be in the range of $900 million and $950 million.</p>
        <p>Cylance ARR was $80 million.</p>
        </body></html>
        """,
        encoding="utf-8",
    )
    evidence = extract_metric_evidence(work_item(path, document_name=path.name))
    rejected_reasons = {row.reason for row in evidence if row.status == "REJECTED_POLICY"}
    assert "customer_threshold_not_arr_level" in rejected_reasons
    assert "booked_arr_flow_not_ending_arr" in rejected_reasons
    assert "forward_guidance_not_actual" in rejected_reasons
    assert "segment_value_not_consolidated_metric" in rejected_reasons


def test_deferred_revenue_table_uses_its_own_row_and_scale(tmp_path: Path) -> None:
    path = tmp_path / "balance_sheet.htm"
    path.write_text(
        """
        <html><body>
        <p>Dollar amounts in thousands</p>
        <table>
          <tr><th></th><th>December 31, 2023</th></tr>
          <tr><td>Accounts receivable</td><td>28,393</td></tr>
          <tr><td>Deferred revenue (current)</td><td>152,434</td></tr>
          <tr><td>Deferred revenue (non-current)</td><td>10,597</td></tr>
          <tr><td>Deferred revenue</td><td>163,031</td></tr>
        </table>
        </body></html>
        """,
        encoding="utf-8",
    )
    evidence = extract_metric_evidence(work_item(path, document_name=path.name))
    by_metric = {row.metric_name: row for row in evidence}
    assert by_metric["deferred_revenue_current"].value == 152_434_000
    assert by_metric["deferred_revenue_noncurrent"].value == 10_597_000
    assert by_metric["deferred_revenue_total"].value == 163_031_000


def test_duplicate_prose_and_filing_date_proxy_are_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.htm"
    path.write_text(
        """
        <html><body>
        <p>ARR was $500 million.</p>
        <p>Annual recurring revenue totaled $500 million.</p>
        </body></html>
        """,
        encoding="utf-8",
    )
    evidence = extract_metric_evidence(
        work_item(
            path,
            document_name=path.name,
            filing_date="2024-02-01",
            report_date="2024-02-01",
        )
    )
    arr = [row for row in evidence if row.metric_name == "annual_recurring_revenue"]
    assert {row.status for row in arr} == {
        "REVIEW_REQUIRED",
        "SUPPRESSED_SEMANTIC_DUPLICATE",
    }
    assert all(row.period_end == "" for row in arr)


def test_standard_dimensionless_xbrl_is_accepted(tmp_path: Path) -> None:
    path = tmp_path / "issuer.htm"
    path.write_text("<html/>", encoding="utf-8")
    item = work_item(path, document_name=path.name)
    evidence = map_normalized_facts(
        item,
        (
            NormalizedFact(
                taxonomy="us-gaap",
                concept_name="RevenueRemainingPerformanceObligation",
                value_text="1000000",
                numeric_value=1_000_000.0,
                unit="USD",
                period_start="",
                period_end="2023-12-31",
                context_id="c1",
                dimensions_json="{}",
                scope="consolidated",
                source_document=path.name,
                provider="arelle",
            ),
        ),
    )
    assert len(evidence) == 1
    assert evidence[0].metric_name == "remaining_performance_obligation"
    assert evidence[0].status == "ACCEPTED"


def test_new_technology_parser_modules_do_not_import_industrials() -> None:
    paths = [
        PROJECT_ROOT / "technology" / "core" / "dedicated_parser" / "planner_compat.py",
        PROJECT_ROOT / "technology" / "software_infrastructure" / "software_parser_hydration.py",
        PROJECT_ROOT / "technology" / "software_infrastructure" / "dedicated_parser_adapter.py",
    ]
    modules: set[str] = set()
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)
    assert not any(module == "industrials" or module.startswith("industrials.") for module in modules)
