from __future__ import annotations

import json
import runpy
import sqlite3
from pathlib import Path

import pytest

from dedicated_parser.adapters import load_registry
from dedicated_parser.contracts import (
    AdapterRegistry,
    DOCUMENT_PARSER_RELEASE,
    DocumentRef,
    FilingRef,
    MetricEvidence,
    MetricRequest,
    NormalizedFact,
    WorkItem,
    WorkResult,
    file_sha256,
)
from dedicated_parser.golden import load_corpus, validate_corpus
from dedicated_parser.planner import build_plan
from dedicated_parser.recovery import (
    _classify,
    assessment_summary,
    build_recovery_assessments,
    persist_recovery_assessments,
)
from dedicated_parser.runtime import execute_plan
from dedicated_parser.semantic import parse_semantic_document
from dedicated_parser.storage import (
    catalog_documents,
    connect_database,
    mark_work_started,
    persist_result,
    register_work,
    start_run,
)
from industrials.machinery.dedicated_parser_adapter import (
    extract_metric_evidence,
    get_registry,
    map_normalized_facts,
    postprocess_metric_evidence,
)
from industrials.machinery.disclosure_candidates import (
    extract_machinery_prose_candidates,
    resolve_machinery_disclosure_candidates,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ADAPTER = (
    "industrials.machinery.dedicated_parser_adapter:"
    "extract_metric_evidence"
)


def _create_planner_source_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE dim_universe_membership (
            ticker TEXT, model_family TEXT, start_date TEXT, end_date TEXT
        );
        CREATE TABLE dim_industrials_taxonomy (
            ticker TEXT, model_family TEXT
        );
        CREATE TABLE dim_company (
            ticker TEXT, currency TEXT
        );
        CREATE TABLE fact_sec_filing (
            ticker TEXT, cik TEXT, accession_number TEXT, form_type TEXT,
            filing_date TEXT, accepted_at TEXT, report_date TEXT,
            primary_document TEXT, source_id TEXT
        );
        CREATE TABLE feature_financial_metric_availability (
            ticker TEXT, model_family TEXT, asof_date TEXT,
            metric_name TEXT, availability_status TEXT
        );
        CREATE TABLE feature_financial_statement (
            ticker TEXT, model_family TEXT, asof_date TEXT,
            reported_currency TEXT
        );
        CREATE TABLE fact_sec_xbrl_fact (
            ticker TEXT, canonical_metric TEXT, accepted_at TEXT,
            filing_date TEXT, period_end TEXT, value REAL
        );
        """
    )


def _seed_planner_source(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO dim_universe_membership VALUES ('TEST', 'machinery', '2020-01-01', NULL)"
    )
    conn.execute(
        "INSERT INTO dim_industrials_taxonomy VALUES ('TEST', 'machinery')"
    )
    conn.execute("INSERT INTO dim_company VALUES ('TEST', 'USD')")
    conn.execute(
        """
        INSERT INTO fact_sec_filing VALUES (
            'TEST', '0000000001', '0000000001-26-000001', '10-Q',
            '2026-04-30', '2026-04-30T12:00:00Z', '2026-03-31',
            'test-20260331.htm', 'sec_submissions'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO feature_financial_metric_availability VALUES (
            'TEST', 'machinery', '2026-07-22',
            'reported_backlog', 'NOT_DISCLOSED'
        )
        """
    )
    conn.commit()


def _cached_filing(cache_dir: Path) -> Path:
    accession_dir = (
        cache_dir
        / "sec_archive_xbrl"
        / "CIK0000000001"
        / "000000000126000001"
    )
    accession_dir.mkdir(parents=True)
    primary = accession_dir / "test-20260331.htm"
    primary.write_text(
        "<html><p>Total backlog was $100.0 million as of March 31, 2026.</p></html>",
        encoding="utf-8",
    )
    (accession_dir / "index.json").write_text(
        json.dumps({"directory": {"item": [{"name": primary.name}]}}),
        encoding="utf-8",
    )
    return primary


def test_work_key_is_deterministic(tmp_path: Path) -> None:
    document_path = tmp_path / "filing.htm"
    document_path.write_text("<html></html>", encoding="utf-8")
    stat = document_path.stat()
    filing = FilingRef(
        ticker="TEST",
        cik="0000000001",
        accession_number="0000000001-26-000001",
        form_type="10-Q",
        filing_date="2026-04-30",
        accepted_at="2026-04-30T12:00:00Z",
        report_date="2026-03-31",
        primary_document=document_path.name,
        source_id="sec_submissions",
    )
    document = DocumentRef(
        name=document_path.name,
        path=str(document_path),
        content_sha256=file_sha256(document_path),
        file_size=stat.st_size,
        modified_ns=stat.st_mtime_ns,
        is_primary=True,
    )
    first = WorkItem(
        model_family="machinery",
        adapter_path=ADAPTER,
        adapter_version="test_v1",
        filing=filing,
        documents=(document,),
        requested_metrics=(MetricRequest("reported_backlog", ("Backlog",)),),
    )
    second = WorkItem(
        model_family="machinery",
        adapter_path=ADAPTER,
        adapter_version="test_v1",
        filing=filing,
        documents=(document,),
        requested_metrics=(MetricRequest("reported_backlog", ("Backlog",)),),
    )
    assert first.work_key == second.work_key
    assert first.parser_release == DOCUMENT_PARSER_RELEASE
    non_usd_filing = FilingRef(
        ticker=filing.ticker,
        cik=filing.cik,
        accession_number=filing.accession_number,
        form_type=filing.form_type,
        filing_date=filing.filing_date,
        accepted_at=filing.accepted_at,
        report_date=filing.report_date,
        primary_document=filing.primary_document,
        source_id=filing.source_id,
        company_currency="CAD",
    )
    non_usd = WorkItem(
        model_family="machinery",
        adapter_path=ADAPTER,
        adapter_version="test_v1",
        filing=non_usd_filing,
        documents=(document,),
        requested_metrics=(MetricRequest("reported_backlog", ("Backlog",)),),
    )
    assert non_usd.work_key != first.work_key


def test_planner_is_database_and_cache_first(tmp_path: Path) -> None:
    db_path = tmp_path / "industrials.sqlite"
    cache_dir = tmp_path / "cache"
    _cached_filing(cache_dir)
    registry = load_registry(ADAPTER)
    with connect_database(db_path) as conn:
        _create_planner_source_schema(conn)
        _seed_planner_source(conn)
        conn.execute(
            """
            INSERT INTO feature_financial_statement VALUES (
                'TEST', 'machinery', '2026-07-22', 'CAD'
            )
            """
        )
        conn.commit()
        work, summary = build_plan(
            conn,
            registry=registry,
            adapter_path=ADAPTER,
            asof_date="2026-07-22",
            cache_dir=cache_dir,
            tickers=["TEST"],
            accessions=None,
            max_filings_per_ticker=2,
        )
        assert len(work) == 1
        assert work[0].filing.company_currency == "CAD"
        assert summary.scheduled_accessions == 1
        assert summary.missing_cache_accessions == 0
        catalog_count = conn.execute(
            "SELECT COUNT(*) FROM sec_parser_document_catalog"
        ).fetchone()[0]
        assert catalog_count == 1

        conn.execute(
            """
            INSERT INTO fact_sec_xbrl_fact VALUES (
                'TEST', 'reported_backlog', '2026-04-30T12:00:00Z',
                '2026-04-30', '2026-03-31', 100000000.0
            )
            """
        )
        conn.commit()
        no_work, satisfied = build_plan(
            conn,
            registry=registry,
            adapter_path=ADAPTER,
            asof_date="2026-07-22",
            cache_dir=cache_dir,
            tickers=["TEST"],
            accessions=None,
            max_filings_per_ticker=2,
        )
        assert no_work == []
        assert satisfied.database_satisfied_pairs == 1

        forced, _ = build_plan(
            conn,
            registry=registry,
            adapter_path=ADAPTER,
            asof_date="2026-07-22",
            cache_dir=cache_dir,
            tickers=["TEST"],
            accessions=["0000000001-26-000001"],
            max_filings_per_ticker=2,
            force=True,
        )
        assert len(forced) == 1

        all_metrics, all_metrics_summary = build_plan(
            conn,
            registry=registry,
            adapter_path=ADAPTER,
            asof_date="2026-07-22",
            cache_dir=cache_dir,
            tickers=["TEST"],
            accessions=["0000000001-26-000001"],
            max_filings_per_ticker=2,
            all_metrics=True,
        )
        assert len(all_metrics) == 1
        assert all_metrics_summary.database_satisfied_pairs == 0


def test_planner_requires_completed_orders_series_for_book_to_bill(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "industrials.sqlite"
    cache_dir = tmp_path / "cache"
    _cached_filing(cache_dir)
    registry = load_registry(ADAPTER)
    with connect_database(db_path) as conn:
        _create_planner_source_schema(conn)
        _seed_planner_source(conn)
        conn.execute(
            "ALTER TABLE feature_financial_statement "
            "ADD COLUMN fiscal_period_end TEXT"
        )
        conn.execute(
            "ALTER TABLE feature_financial_statement "
            "ADD COLUMN book_to_bill REAL"
        )
        conn.execute(
            "ALTER TABLE feature_financial_statement "
            "ADD COLUMN orders_ttm REAL"
        )
        conn.execute(
            "ALTER TABLE fact_sec_xbrl_fact ADD COLUMN period_start TEXT"
        )
        conn.execute(
            "DELETE FROM feature_financial_metric_availability"
        )
        conn.execute("DELETE FROM feature_financial_statement")
        conn.execute(
            """
            INSERT INTO feature_financial_metric_availability VALUES (
                'TEST', 'machinery', '2026-07-22',
                'book_to_bill', 'NOT_DISCLOSED'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO feature_financial_statement(
                ticker, model_family, asof_date, reported_currency,
                fiscal_period_end, book_to_bill, orders_ttm
            ) VALUES (
                'TEST', 'machinery', '2026-07-22', 'USD',
                '2026-03-31', NULL, NULL
            )
            """
        )
        for start, end in (
            ("2025-04-01", "2025-06-30"),
            ("2025-07-01", "2025-09-30"),
            ("2025-10-01", "2025-12-31"),
            ("2026-01-01", "2026-03-31"),
        ):
            conn.execute(
                """
                INSERT INTO fact_sec_xbrl_fact(
                    ticker, canonical_metric, accepted_at, filing_date,
                    period_end, value, period_start
                ) VALUES (
                    'TEST', 'revenue', '2026-04-30T12:00:00Z',
                    '2026-04-30', ?, 100.0, ?
                )
                """,
                (end, start),
            )
        conn.execute(
            """
            INSERT INTO fact_sec_xbrl_fact(
                ticker, canonical_metric, accepted_at, filing_date,
                period_end, value, period_start
            ) VALUES (
                'TEST', 'orders', '2026-04-30T12:00:00Z',
                '2026-04-30', '2026-03-31', 110.0, '2026-01-01'
            )
            """
        )
        conn.commit()

        work, summary = build_plan(
            conn,
            registry=registry,
            adapter_path=ADAPTER,
            asof_date="2026-07-22",
            cache_dir=cache_dir,
            tickers=["TEST"],
            accessions=None,
            max_filings_per_ticker=4,
        )
        assert work
        assert summary.series_gap_details
        gap = summary.series_gap_details[0]
        assert gap["facet"] == "series_incomplete_for_ttm"
        assert len(gap["missing_periods"]) == 3

        conn.execute(
            """
            UPDATE feature_financial_statement
            SET book_to_bill = 1.1, orders_ttm = 440.0
            WHERE ticker = 'TEST'
            """
        )
        conn.commit()
        no_work, satisfied = build_plan(
            conn,
            registry=registry,
            adapter_path=ADAPTER,
            asof_date="2026-07-22",
            cache_dir=cache_dir,
            tickers=["TEST"],
            accessions=None,
            max_filings_per_ticker=4,
        )
        assert no_work == []
        assert satisfied.database_satisfied_pairs == 1


def test_parallel_runtime_uses_single_writer(tmp_path: Path) -> None:
    db_path = tmp_path / "shadow.sqlite"
    document_path = tmp_path / "filing.htm"
    document_path.write_text(
        "<html><p>Total backlog was $100.0 million as of March 31, 2026.</p></html>",
        encoding="utf-8",
    )
    stat = document_path.stat()
    document = DocumentRef(
        name=document_path.name,
        path=str(document_path),
        content_sha256=file_sha256(document_path),
        file_size=stat.st_size,
        modified_ns=stat.st_mtime_ns,
        is_primary=True,
    )
    items = []
    for index in (1, 2):
        filing = FilingRef(
            ticker=f"T{index}",
            cik=f"{index:010d}",
            accession_number=f"{index:010d}-26-000001",
            form_type="10-Q",
            filing_date="2026-04-30",
            accepted_at="2026-04-30T12:00:00Z",
            report_date="2026-03-31",
            primary_document=document_path.name,
            source_id="sec_submissions",
        )
        items.append(
            WorkItem(
                model_family="machinery",
                adapter_path=ADAPTER,
                adapter_version="test_v1",
                filing=filing,
                documents=(document,),
                requested_metrics=(
                    MetricRequest("reported_backlog", ("Backlog",)),
                ),
                enable_arelle=False,
                enable_edgartools=False,
            )
        )
    with connect_database(db_path) as conn:
        run_id = start_run(
            conn,
            model_family="machinery",
            asof_date="2026-07-22",
            adapter_version="test_v1",
            mode="shadow",
            worker_count=2,
        )
        completed, failed = execute_plan(
            conn,
            run_id=run_id,
            work_items=items,
            worker_count=2,
            provider_state_dir=tmp_path / "provider",
            write_batch_size=2,
        )
        assert (completed, failed) == (2, 0)
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM sec_parser_metric_evidence_shadow"
            ).fetchone()[0]
            == 2
        )
        assert (
            conn.execute(
                """
                SELECT COUNT(*)
                FROM sec_parser_work_ledger
                WHERE status = 'COMPLETED'
                """
            ).fetchone()[0]
            == 2
        )
        parallel_rows = conn.execute(
            """
            SELECT ticker, metric_name, candidate_value, unit,
                   period_start, period_end, candidate_status, status_reason
            FROM sec_parser_metric_evidence_shadow
            ORDER BY ticker, metric_name, candidate_value
            """
        ).fetchall()

    serial_db_path = tmp_path / "serial.sqlite"
    with connect_database(serial_db_path) as conn:
        run_id = start_run(
            conn,
            model_family="machinery",
            asof_date="2026-07-22",
            adapter_version="test_v1",
            mode="shadow",
            worker_count=1,
        )
        completed, failed = execute_plan(
            conn,
            run_id=run_id,
            work_items=items,
            worker_count=1,
            provider_state_dir=tmp_path / "serial_provider",
            write_batch_size=2,
        )
        assert (completed, failed) == (2, 0)
        serial_rows = conn.execute(
            """
            SELECT ticker, metric_name, candidate_value, unit,
                   period_start, period_end, candidate_status, status_reason
            FROM sec_parser_metric_evidence_shadow
            ORDER BY ticker, metric_name, candidate_value
            """
        ).fetchall()
    assert [tuple(row) for row in parallel_rows] == [
        tuple(row) for row in serial_rows
    ]


def test_golden_corpus_is_structured_and_nonempty() -> None:
    path = PROJECT_ROOT / "dedicated_parser" / "golden_corpus" / "machinery_v1.json"
    corpus = load_corpus(path)
    assert corpus["corpus_id"] == "machinery_disclosure_traps_v1"
    assert len(corpus["expectations"]) >= 15
    assert {
        "ACCEPTED",
        "REJECTED_POLICY",
    } <= {row["candidate_status"] for row in corpus["expectations"]}


def test_bldp_total_backlog_wins_over_separate_twelve_month_book() -> None:
    filing = {
        "ticker": "BLDP",
        "accession_number": "0001628280-26-030150",
        "form_type": "6-K",
        "filing_date": "2026-05-05",
        "accepted_at": "2026-05-05T12:00:00Z",
        "report_date": "2026-03-31",
    }
    text = """
    <html><p>Our expectations for 2026 are in part supported by our 12-month
    Order Book of approximately $52.8 million (derived from Order Backlog of
    approximately $112.9 million as of March 31, 2026). Order Backlog
    represents contracted orders, and the 12-month Order Book reflects
    expected deliveries over the next 12 months.</p></html>
    """
    candidates = extract_machinery_prose_candidates(
        text,
        filing=filing,
        company_currency="USD",
    )
    resolved = resolve_machinery_disclosure_candidates(
        candidates,
        ticker="BLDP",
        filing=filing,
    )
    total = next(
        candidate
        for candidate in resolved
        if abs(candidate.value - 112_900_000.0) <= 1.0
    )
    assert total.candidate_status == "ACCEPTED"
    assert total.status_reason == "reviewed_total_order_backlog_usd"
    assert total.scope == "consolidated"


def test_golden_validator_matches_expected_candidate(tmp_path: Path) -> None:
    db_path = tmp_path / "golden.sqlite"
    expectation = load_corpus(
        PROJECT_ROOT / "dedicated_parser" / "golden_corpus" / "machinery_v1.json"
    )["expectations"][0]
    with connect_database(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE fact_sec_metric_disclosure_candidate (
                ticker TEXT, accession_number TEXT, document_name TEXT,
                metric_name TEXT, candidate_status TEXT, candidate_value REAL,
                unit TEXT, period_start TEXT, period_end TEXT,
                status_reason TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO fact_sec_metric_disclosure_candidate
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                expectation["ticker"],
                expectation["accession_number"],
                expectation["document_name"],
                expectation["metric_name"],
                expectation["candidate_status"],
                expectation["candidate_value"],
                expectation["unit"],
                expectation["period_start"],
                expectation["period_end"],
                "reviewed_exhaustive_operating_segment_sum",
            ),
        )
        errors = validate_corpus(
            conn,
            corpus_path=(
                PROJECT_ROOT
                / "dedicated_parser"
                / "golden_corpus"
                / "machinery_v1.json"
            ),
            table="fact_sec_metric_disclosure_candidate",
        )
    all_expectations = load_corpus(
        PROJECT_ROOT
        / "dedicated_parser"
        / "golden_corpus"
        / "machinery_v1.json"
    )["expectations"]
    assert len(errors) == sum(
        not item.get("expect_absent", False) for item in all_expectations
    ) - 1
    assert not any("cr_exhaustive_segment_sum_2024q4" in error for error in errors)


def test_golden_validator_supports_prohibited_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "golden-absence.sqlite"
    corpus_path = tmp_path / "corpus.json"
    corpus_path.write_text(
        json.dumps(
            {
                "expectations": [
                    {
                        "id": "not_operating_backlog",
                        "ticker": "WAB",
                        "accession_number": "test-accession",
                        "document_name": "filing.htm",
                        "metric_name": "remaining_performance_obligation",
                        "candidate_status": "ACCEPTED",
                        "candidate_value": 1_366_000_000.0,
                        "unit": "USD",
                        "period_start": "",
                        "period_end": "2026-03-31",
                        "expect_absent": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with connect_database(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE fact_sec_metric_disclosure_candidate (
                ticker TEXT, accession_number TEXT, document_name TEXT,
                metric_name TEXT, candidate_status TEXT, candidate_value REAL,
                unit TEXT, period_start TEXT, period_end TEXT,
                status_reason TEXT
            )
            """
        )
        assert not validate_corpus(
            conn,
            corpus_path=corpus_path,
            table="fact_sec_metric_disclosure_candidate",
        )
        conn.execute(
            """
            INSERT INTO fact_sec_metric_disclosure_candidate
            VALUES ('WAB', 'test-accession', 'filing.htm',
                    'remaining_performance_obligation', 'ACCEPTED',
                    1366000000, 'USD', '', '2026-03-31', 'incorrect')
            """
        )
        errors = validate_corpus(
            conn,
            corpus_path=corpus_path,
            table="fact_sec_metric_disclosure_candidate",
        )
    assert errors == [
        "not_operating_backlog: prohibited row found in "
        "fact_sec_metric_disclosure_candidate"
    ]


def test_orchestrator_shadow_stage_is_opt_in() -> None:
    namespace = runpy.run_path(
        str(
            PROJECT_ROOT
            / "industrials"
            / "machinery"
            / "scripts"
            / "17_run_machinery_refresh_pipeline.py"
        )
    )
    build_steps = namespace["build_steps"]
    default_steps = build_steps(
        "2026-07-22",
        force=False,
        include_norgate_backfill=False,
    )
    assert "08d_dedicated_parser_shadow" not in {
        step.step_id for step in default_steps
    }
    shadow_steps = build_steps(
        "2026-07-22",
        force=False,
        include_norgate_backfill=False,
        include_dedicated_parser_shadow=True,
    )
    ids = [step.step_id for step in shadow_steps]
    assert ids.index("08b_scan_disclosures") < ids.index(
        "08d_dedicated_parser_shadow"
    )
    assert ids.index("08d_dedicated_parser_shadow") < ids.index(
        "08_build_financial"
    )
    production_steps = build_steps(
        "2026-07-22",
        force=False,
        include_norgate_backfill=False,
        include_dedicated_parser_production=True,
    )
    production_ids = [step.step_id for step in production_steps]
    assert production_ids.index("08d_dedicated_parser_shadow") < (
        production_ids.index("08e_dedicated_parser_production")
    )
    assert production_ids.index("08e_dedicated_parser_production") < (
        production_ids.index("08_build_financial")
    )


def test_semantic_document_preserves_table_headers_and_sections() -> None:
    document = parse_semantic_document(
        """
        <html>
          <h2>Revenue Recognition</h2>
          <table>
            <tr><th>USD in millions</th><th>March 31, 2026</th><th>March 31, 2025</th></tr>
            <tr><td>Total remaining performance obligations</td><td>$1,800</td><td>$1,500</td></tr>
          </table>
        </html>
        """,
        source_document="filing.htm",
    )
    rows = document.table_rows
    assert len(rows) == 2
    assert rows[1].section_path == ("Revenue Recognition",)
    assert rows[1].header_cells == (
        "USD in millions",
        "March 31, 2026",
        "March 31, 2025",
    )
    assert "Total remaining performance obligations" in rows[1].search_text


def test_semantic_document_attaches_nearest_table_preamble() -> None:
    document = parse_semantic_document(
        """
        <html>
          <h2>Orders</h2>
          <p>Amounts in thousands</p>
          <table>
            <tr><th>Three months ended</th><th>March 31, 2026</th></tr>
            <tr><td>Total orders</td><td>1,500</td></tr>
          </table>
        </html>
        """,
        source_document="filing.htm",
    )
    row = document.table_rows[-1]
    assert row.preamble_text == "Amounts in thousands"
    assert "Amounts in thousands" in row.search_text


def test_orders_interval_arithmetic_derives_only_discrete_quarters(
    tmp_path: Path,
) -> None:
    document_path = tmp_path / "filing.htm"
    document_path.write_text("<html></html>", encoding="utf-8")
    filing, document = _filing_for_document(document_path)
    item = WorkItem(
        model_family="machinery",
        adapter_path=ADAPTER,
        adapter_version="test",
        filing=filing,
        documents=(document,),
        requested_metrics=(MetricRequest("orders"),),
    )

    def orders(
        value: float,
        period_start: str,
        period_end: str,
    ) -> MetricEvidence:
        return MetricEvidence(
            metric_name="orders",
            concept_name="Orders",
            value=value,
            unit="USD",
            period_start=period_start,
            period_end=period_end,
            scope="consolidated",
            confidence=0.95,
            status="ACCEPTED",
            reason="test",
            evidence_text="test",
            source_document=document.name,
            extraction_method="test",
        )

    processed = postprocess_metric_evidence(
        item,
        (
            orders(600.0, "2026-01-01", "2026-06-30"),
            orders(350.0, "2026-04-01", "2026-06-30"),
            orders(1_000.0, "2026-01-01", "2026-09-30"),
        ),
    )
    derived = [
        row
        for row in processed
        if row.extraction_method
        == "dedicated_parser:explicit_interval_arithmetic"
    ]
    assert any(
        row.period_start == "2026-01-01"
        and row.period_end == "2026-03-31"
        and row.value == 250.0
        for row in derived
    )
    assert not any(
        row.period_start == "2026-04-01"
        and row.period_end == "2026-09-30"
        for row in derived
    )


def _filing_for_document(path: Path, *, ticker: str = "TEST") -> tuple[FilingRef, DocumentRef]:
    filing = FilingRef(
        ticker=ticker,
        cik="0000000001",
        accession_number="0000000001-26-000001",
        form_type="10-Q",
        filing_date="2026-04-30",
        accepted_at="2026-04-30T12:00:00Z",
        report_date="2026-03-31",
        primary_document=path.name,
        source_id="sec_submissions",
    )
    stat = path.stat()
    document = DocumentRef(
        name=path.name,
        path=str(path),
        content_sha256=file_sha256(path),
        file_size=stat.st_size,
        modified_ns=stat.st_mtime_ns,
        is_primary=True,
    )
    return filing, document


def test_table_extraction_preserves_values_and_does_not_parse_header_dates(
    tmp_path: Path,
) -> None:
    path = tmp_path / "table.htm"
    path.write_text(
        """
        <html><h2>Revenue Recognition</h2><table>
        <tr><th>USD in millions</th><th>March 31, 2026</th><th>March 31, 2025</th></tr>
        <tr><td>Total remaining performance obligations</td><td>$1,800</td><td>$1,500</td></tr>
        </table></html>
        """,
        encoding="utf-8",
    )
    filing, document = _filing_for_document(path)
    evidence = extract_metric_evidence(
        WorkItem(
            model_family="machinery",
            adapter_path=ADAPTER,
            adapter_version="test_v2",
            filing=filing,
            documents=(document,),
            requested_metrics=get_registry().source_metrics,
            enable_arelle=False,
            enable_edgartools=False,
        )
    )
    rpo = [
        row
        for row in evidence
        if row.metric_name == "remaining_performance_obligation"
    ]
    assert {(row.period_end, row.value) for row in rpo} == {
        ("2025-03-31", 1_500_000_000.0),
        ("2026-03-31", 1_800_000_000.0),
    }


def test_explicit_current_rpo_percentage_requires_and_uses_accepted_total(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rpo.htm"
    path.write_text(
        """
        <html>
        <p>The Company's remaining performance obligations totaled
        $1.0 billion as of March 31, 2026.</p>
        <p>The Company expects to recognize 70% of these remaining
        performance obligations within the next 12 months.</p>
        </html>
        """,
        encoding="utf-8",
    )
    filing, document = _filing_for_document(path, ticker="JCI")
    evidence = extract_metric_evidence(
        WorkItem(
            model_family="machinery",
            adapter_path=ADAPTER,
            adapter_version="test_v2",
            filing=filing,
            documents=(document,),
            requested_metrics=get_registry().source_metrics,
            enable_arelle=False,
            enable_edgartools=False,
        )
    )
    current = [
        row
        for row in evidence
        if row.metric_name == "rpo_current"
    ]
    assert len(current) == 1
    assert current[0].value == 700_000_000.0
    assert current[0].status == "ACCEPTED"
    assert current[0].confidence == 0.93
    assert current[0].reason == "explicit_twelve_month_rpo_percentage"


def test_current_rpo_ignores_subcomponent_and_horizon_numbers(
    tmp_path: Path,
) -> None:
    path = tmp_path / "emr.htm"
    path.write_text(
        """
        <html><p>As of September 30, 2024, total backlog was $7.8 billion,
        of which $1.2 billion related to AspenTech. Approximately 75 percent
        of the total backlog is expected to be recognized over the next
        12 months.</p></html>
        """,
        encoding="utf-8",
    )
    filing, document = _filing_for_document(path, ticker="EMR")
    filing = FilingRef(
        **{
            **filing.__dict__,
            "report_date": "2024-09-30",
        }
    )
    evidence = extract_metric_evidence(
        WorkItem(
            model_family="machinery",
            adapter_path=ADAPTER,
            adapter_version="test_v2",
            filing=filing,
            documents=(document,),
            requested_metrics=get_registry().source_metrics,
            enable_arelle=False,
            enable_edgartools=False,
        )
    )
    current = [
        row for row in evidence if row.metric_name == "rpo_current"
    ]
    assert len(current) == 1
    assert current[0].value == 5_850_000_000.0
    assert current[0].status == "REVIEW_REQUIRED"
    assert current[0].confidence == 0.72
    assert (
        current[0].reason
        == "explicit_percentage_total_rpo_requires_review"
    )


def test_explicit_current_rpo_amount_is_not_treated_as_total(
    tmp_path: Path,
) -> None:
    path = tmp_path / "powl.htm"
    path.write_text(
        """
        <html><p>As of December 31, 2025, we had backlog of $1.6 billion,
        of which approximately $933 million is expected to be recognized
        as revenue within the next twelve months.</p></html>
        """,
        encoding="utf-8",
    )
    filing, document = _filing_for_document(path, ticker="POWL")
    filing = FilingRef(
        **{
            **filing.__dict__,
            "report_date": "2025-12-31",
        }
    )
    evidence = extract_metric_evidence(
        WorkItem(
            model_family="machinery",
            adapter_path=ADAPTER,
            adapter_version="test_v2",
            filing=filing,
            documents=(document,),
            requested_metrics=get_registry().source_metrics,
            enable_arelle=False,
            enable_edgartools=False,
        )
    )
    current = next(
        row for row in evidence if row.metric_name == "rpo_current"
    )
    assert current.value == 933_000_000.0
    assert current.status == "ACCEPTED"
    assert current.reason == "explicit_twelve_month_rpo_amount"


def test_table_date_fragments_are_not_monetary_candidates(
    tmp_path: Path,
) -> None:
    path = tmp_path / "wab.htm"
    path.write_text(
        """
        <html><table>
        <tr><td>Backlog $ in millions</td><td>March 31,</td></tr>
        </table></html>
        """,
        encoding="utf-8",
    )
    filing, document = _filing_for_document(path, ticker="WAB")
    evidence = extract_metric_evidence(
        WorkItem(
            model_family="machinery",
            adapter_path=ADAPTER,
            adapter_version="test_v2",
            filing=filing,
            documents=(document,),
            requested_metrics=get_registry().source_metrics,
            enable_arelle=False,
            enable_edgartools=False,
        )
    )
    assert not [
        row
        for row in evidence
        if row.metric_name
        in {"reported_backlog", "remaining_performance_obligation"}
    ]


def test_orders_table_accepts_only_explicit_consolidated_column(
    tmp_path: Path,
) -> None:
    path = tmp_path / "wab-orders.htm"
    path.write_text(
        """
        <html><table>
        <tr><td>In millions</td><td>Freight Segment</td>
        <td>Transit Segment</td><td>Consolidated</td></tr>
        <tr><td>New orders</td><td>11,911</td><td>3,587</td>
        <td>15,498</td></tr>
        </table></html>
        """,
        encoding="utf-8",
    )
    filing, document = _filing_for_document(path, ticker="WAB")
    filing = FilingRef(
        **{
            **filing.__dict__,
            "form_type": "10-K",
            "report_date": "2025-12-31",
        }
    )
    evidence = extract_metric_evidence(
        WorkItem(
            model_family="machinery",
            adapter_path=ADAPTER,
            adapter_version="test_v2",
            filing=filing,
            documents=(document,),
            requested_metrics=get_registry().source_metrics,
            enable_arelle=False,
            enable_edgartools=False,
        )
    )
    orders = [row for row in evidence if row.metric_name == "orders"]
    accepted = [row for row in orders if row.status == "ACCEPTED"]
    dimensional = [row for row in orders if row.status == "REVIEW_REQUIRED"]
    assert [(row.value, row.scope) for row in accepted] == [
        (15_498_000_000.0, "consolidated")
    ]
    assert {(row.value, row.scope) for row in dimensional} == {
        (3_587_000_000.0, "segment"),
        (11_911_000_000.0, "segment"),
    }
    assert accepted[0].period_start == "2025-01-01"
    assert accepted[0].period_end == "2025-12-31"


def test_current_rpo_does_not_use_unrelated_horizon_percentage(
    tmp_path: Path,
) -> None:
    path = tmp_path / "unrelated-percentage.htm"
    path.write_text(
        """
        <html>
        <p>Remaining performance obligations were $1.0 billion as of
        March 31, 2026.</p>
        <p>The tax agreement requires 70% of the payment within the next
        twelve months.</p>
        </html>
        """,
        encoding="utf-8",
    )
    filing, document = _filing_for_document(path, ticker="MIR")
    evidence = extract_metric_evidence(
        WorkItem(
            model_family="machinery",
            adapter_path=ADAPTER,
            adapter_version="test_v2",
            filing=filing,
            documents=(document,),
            requested_metrics=get_registry().source_metrics,
            enable_arelle=False,
            enable_edgartools=False,
        )
    )
    assert not [row for row in evidence if row.metric_name == "rpo_current"]


def test_multirow_total_backlog_headers_map_only_total_columns(
    tmp_path: Path,
) -> None:
    path = tmp_path / "multirow-backlog.htm"
    path.write_text(
        """
        <html><table>
        <tr><td>In thousands</td><td colspan="2">As of March 31, 2026</td>
        <td colspan="2">As of March 31, 2025</td></tr>
        <tr><td></td><td>Total Backlog</td><td>Backlog under 1 year</td>
        <td>Total Backlog</td><td>Backlog under 1 year</td></tr>
        <tr><td>Total</td><td>337,927</td><td>305,096</td>
        <td>270,310</td><td>249,908</td></tr>
        </table></html>
        """,
        encoding="utf-8",
    )
    filing, document = _filing_for_document(path, ticker="SXI")
    evidence = extract_metric_evidence(
        WorkItem(
            model_family="machinery",
            adapter_path=ADAPTER,
            adapter_version="test_v2",
            filing=filing,
            documents=(document,),
            requested_metrics=get_registry().source_metrics,
            enable_arelle=False,
            enable_edgartools=False,
        )
    )
    backlog = [
        row for row in evidence if row.metric_name == "reported_backlog"
    ]
    assert {
        (row.period_end, row.value, row.status) for row in backlog
    } == {
        ("2025-03-31", 270_310_000.0, "ACCEPTED"),
        ("2026-03-31", 337_927_000.0, "ACCEPTED"),
    }


@pytest.mark.parametrize(
    "row",
    (
        (
            "No judgment or order of a court with an aggregate dispute "
            "value of USD 5,000,000 has been made."
        ),
        "Ballard announces order for 15 MW in the stationary power market",
        (
            "Task Order No. R1MA-P2-OFS-90-K200 incorporated by reference "
            "to Exhibit 10.1"
        ),
    ),
)
def test_noncommercial_order_table_rows_are_rejected(
    tmp_path: Path,
    row: str,
) -> None:
    path = tmp_path / "noncommercial-order.htm"
    path.write_text(
        f"<html><table><tr><td>{row}</td></tr></table></html>",
        encoding="utf-8",
    )
    filing, document = _filing_for_document(path, ticker="TEST")
    evidence = extract_metric_evidence(
        WorkItem(
            model_family="machinery",
            adapter_path=ADAPTER,
            adapter_version="test_v2",
            filing=filing,
            documents=(document,),
            requested_metrics=get_registry().source_metrics,
            enable_arelle=False,
            enable_edgartools=False,
        )
    )
    assert not [
        row
        for row in evidence
        if row.metric_name == "orders" and row.status == "ACCEPTED"
    ]


def test_revenue_contract_narrative_is_not_accepted_as_orders(
    tmp_path: Path,
) -> None:
    path = tmp_path / "revenue-contract-detail.htm"
    path.write_text(
        """
        <html>
        <p>Revenue from Contracts with Customers (Details) - USD ($)
        $ in Millions</p>
        <table>
        <tr><th></th><th>March 31, 2026</th></tr>
        <tr><td>Revenue from Contract with Customer [Text Block].
        We recognize revenue when control transfers under customer orders
        and consolidated contract arrangements. This narrative describes
        contract assets and liabilities rather than commercial bookings.
        </td><td>7</td></tr>
        </table>
        </html>
        """,
        encoding="utf-8",
    )
    filing, document = _filing_for_document(path, ticker="MWA")
    evidence = extract_metric_evidence(
        WorkItem(
            model_family="machinery",
            adapter_path=ADAPTER,
            adapter_version="test_v2",
            filing=filing,
            documents=(document,),
            requested_metrics=get_registry().source_metrics,
            enable_arelle=False,
            enable_edgartools=False,
        )
    )
    orders = [row for row in evidence if row.metric_name == "orders"]
    assert orders
    assert {row.status for row in orders} == {"REJECTED_POLICY"}
    assert {
        row.reason for row in orders
    } == {"revenue_contract_narrative_is_not_orders"}


def test_intangible_asset_backlog_table_is_not_operating_backlog(
    tmp_path: Path,
) -> None:
    path = tmp_path / "intangible-backlog.htm"
    path.write_text(
        """
        <html><table>
        <tr><td>In millions</td><td>Gross Carrying Amount</td>
        <td>Accumulated Amortization</td><td>Net Carrying Amount</td></tr>
        <tr><td>Backlog</td><td>1,367</td><td>(665)</td><td>702</td></tr>
        </table></html>
        """,
        encoding="utf-8",
    )
    filing, document = _filing_for_document(path, ticker="WAB")
    evidence = extract_metric_evidence(
        WorkItem(
            model_family="machinery",
            adapter_path=ADAPTER,
            adapter_version="test_v2",
            filing=filing,
            documents=(document,),
            requested_metrics=get_registry().source_metrics,
            enable_arelle=False,
            enable_edgartools=False,
        )
    )
    backlog = [
        row
        for row in evidence
        if row.metric_name
        in {"reported_backlog", "remaining_performance_obligation"}
    ]
    assert backlog
    assert {row.status for row in backlog} == {"REJECTED_POLICY"}


def test_bldp_total_backlog_remains_valid_when_same_paragraph_has_horizon() -> None:
    filing = {
        "ticker": "BLDP",
        "accession_number": "0001628280-25-051863",
        "form_type": "6-K",
        "filing_date": "2025-11-13",
        "accepted_at": "2025-11-13T13:42:51Z",
        "report_date": "2025-09-30",
    }
    text = """
    <html><p>Our 12-month Order Book was approximately $71.6 million,
    derived from our Order Backlog of approximately $132.8 million as of
    September 30, 2025. Order Backlog represents contractual commitments.
    </p></html>
    """
    resolved = resolve_machinery_disclosure_candidates(
        extract_machinery_prose_candidates(
            text,
            filing=filing,
            company_currency="USD",
        ),
        ticker="BLDP",
        filing=filing,
    )
    total = next(
        row for row in resolved if abs(row.value - 132_800_000.0) <= 1.0
    )
    assert total.candidate_status == "ACCEPTED"
    assert total.status_reason == "reviewed_total_order_backlog_usd"


def test_xbrl_extension_label_is_discovered_but_fails_closed() -> None:
    filing = FilingRef(
        ticker="TEST",
        cik="0000000001",
        accession_number="0000000001-26-000001",
        form_type="10-Q",
        filing_date="2026-04-30",
        accepted_at="2026-04-30T12:00:00Z",
        report_date="2026-03-31",
        primary_document="filing.htm",
        source_id="sec_submissions",
    )
    fact = NormalizedFact(
        taxonomy="test",
        concept_name="ContractedOrderBook",
        value_text="1800000000",
        numeric_value=1_800_000_000.0,
        unit="USD",
        period_start="",
        period_end="2026-03-31",
        context_id="current",
        dimensions_json="{}",
        scope="consolidated",
        source_document="filing.htm",
        provider="arelle",
        concept_metadata_json=json.dumps(
            {
                "namespace_uri": "https://example.com/test/2026",
                "label": "Total reported backlog",
                "is_extension": True,
            }
        ),
    )
    evidence = map_normalized_facts(
        WorkItem(
            model_family="machinery",
            adapter_path=ADAPTER,
            adapter_version="test_v2",
            filing=filing,
            documents=(),
            requested_metrics=get_registry().source_metrics,
        ),
        (fact,),
    )
    assert len(evidence) == 1
    assert evidence[0].metric_name == "reported_backlog"
    assert evidence[0].status == "REVIEW_REQUIRED"


def test_reviewed_rpo_dimensions_require_and_sum_the_exhaustive_members() -> None:
    filing = FilingRef(
        ticker="AEBI",
        cik="0002048519",
        accession_number="0001140361-25-031010",
        form_type="10-Q",
        filing_date="2025-08-14",
        accepted_at="2025-08-14T10:34:03Z",
        report_date="2025-06-30",
        primary_document="aebi-20250630.htm",
        source_id="sec_submissions",
    )
    common = {
        "taxonomy": "us-gaap",
        "concept_name": "RevenueRemainingPerformanceObligation",
        "value_text": "",
        "unit": "USD",
        "period_start": "",
        "period_end": "2025-06-30",
        "scope": "segment",
        "source_document": "aebi-20250630.htm",
        "provider": "arelle",
        "concept_metadata_json": json.dumps(
            {
                "namespace_uri": "https://fasb.org/us-gaap/2025",
                "is_extension": False,
            }
        ),
    }
    north_america = NormalizedFact(
        **common,
        numeric_value=510_022_000.0,
        context_id="north-america",
        dimensions_json=json.dumps(
            {
                "srt:StatementGeographicalAxis": (
                    "srt:NorthAmericaMember"
                )
            }
        ),
    )
    europe = NormalizedFact(
        **common,
        numeric_value=235_364_000.0,
        context_id="europe-rest",
        dimensions_json=json.dumps(
            {
                "srt:StatementGeographicalAxis": (
                    "aebi:EuropeAndRestOfTheWorldMember"
                )
            }
        ),
    )
    evidence = map_normalized_facts(
        WorkItem(
            model_family="machinery",
            adapter_path=ADAPTER,
            adapter_version="test_v2",
            filing=filing,
            documents=(),
            requested_metrics=get_registry().source_metrics,
        ),
        (north_america, europe),
    )
    accepted = [
        row
        for row in evidence
        if row.metric_name == "remaining_performance_obligation"
        and row.status == "ACCEPTED"
    ]
    assert len(accepted) == 1
    assert accepted[0].value == 745_386_000.0
    assert accepted[0].scope == "consolidated"
    assert accepted[0].reason == "reviewed_exhaustive_dimension_aggregation"


def test_reviewed_consolidated_extension_is_accepted() -> None:
    filing = FilingRef(
        ticker="LNN",
        cik="0000836160",
        accession_number="0001193125-26-294516",
        form_type="10-Q",
        filing_date="2026-07-02",
        accepted_at="2026-07-02T12:00:00Z",
        report_date="2026-05-31",
        primary_document="lnn-20260531.htm",
        source_id="sec_submissions",
    )
    fact = NormalizedFact(
        taxonomy="lnn",
        concept_name=(
            "ContractWithCustomerUnsatisfiedPerformanceObligationAmount"
        ),
        value_text="40500000",
        numeric_value=40_500_000.0,
        unit="USD",
        period_start="",
        period_end="2026-05-31",
        context_id="current",
        dimensions_json="{}",
        scope="consolidated",
        source_document="lnn-20260531.htm",
        provider="arelle",
        concept_metadata_json=json.dumps(
            {
                "namespace_uri": "https://www.lindsay.com/2026",
                "is_extension": True,
            }
        ),
    )
    evidence = map_normalized_facts(
        WorkItem(
            model_family="machinery",
            adapter_path=ADAPTER,
            adapter_version="test_v2",
            filing=filing,
            documents=(),
            requested_metrics=get_registry().source_metrics,
        ),
        (fact,),
    )
    assert len(evidence) == 1
    assert evidence[0].status == "ACCEPTED"
    assert evidence[0].reason == "reviewed_consolidated_extension_fact"


def test_acquisition_backlog_fact_is_rejected() -> None:
    filing = FilingRef(
        ticker="ITT",
        cik="0000216228",
        accession_number="0000216228-26-000036",
        form_type="10-Q",
        filing_date="2026-05-06",
        accepted_at="2026-05-06T12:00:00Z",
        report_date="2026-04-04",
        primary_document="itt-20260404.htm",
        source_id="sec_submissions",
    )
    fact = NormalizedFact(
        taxonomy="itt",
        concept_name="BusinessCombinationBacklogAmortizationInProForma",
        value_text="23800000",
        numeric_value=23_800_000.0,
        unit="USD",
        period_start="2025-01-01",
        period_end="2025-03-29",
        context_id="pro-forma",
        dimensions_json=json.dumps(
            {"srt:StatementScenarioAxis": "srt:ProFormaMember"}
        ),
        scope="segment",
        source_document="itt-20260404.htm",
        provider="arelle",
        concept_metadata_json="{}",
    )
    evidence = map_normalized_facts(
        WorkItem(
            model_family="machinery",
            adapter_path=ADAPTER,
            adapter_version="test_v2",
            filing=filing,
            documents=(),
            requested_metrics=get_registry().source_metrics,
        ),
        (fact,),
    )
    assert len(evidence) == 1
    assert evidence[0].metric_name == "reported_backlog"
    assert evidence[0].status == "REJECTED_POLICY"
    assert evidence[0].reason == "non_operating_acquisition_or_intangible_fact"


def test_xbrl_percentage_without_horizon_does_not_derive_current_rpo() -> None:
    filing = FilingRef(
        ticker="TEST",
        cik="0000000001",
        accession_number="0000000001-26-000001",
        form_type="10-Q",
        filing_date="2026-04-30",
        accepted_at="2026-04-30T12:00:00Z",
        report_date="2026-03-31",
        primary_document="filing.htm",
        source_id="sec_submissions",
    )
    common = {
        "taxonomy": "us-gaap",
        "period_start": "",
        "period_end": "2026-03-31",
        "context_id": "current",
        "dimensions_json": "{}",
        "scope": "consolidated",
        "source_document": "filing.htm",
        "provider": "arelle",
        "concept_metadata_json": json.dumps(
            {
                "namespace_uri": "https://fasb.org/us-gaap/2026",
                "is_extension": False,
            }
        ),
    }
    total = NormalizedFact(
        **common,
        concept_name="RevenueRemainingPerformanceObligation",
        value_text="1000000000",
        numeric_value=1_000_000_000.0,
        unit="USD",
    )
    percentage = NormalizedFact(
        **common,
        concept_name="RevenueRemainingPerformanceObligationPercentage",
        value_text="0.7",
        numeric_value=0.7,
        unit="pure",
    )
    evidence = map_normalized_facts(
        WorkItem(
            model_family="machinery",
            adapter_path=ADAPTER,
            adapter_version="test_v2",
            filing=filing,
            documents=(),
            requested_metrics=get_registry().source_metrics,
        ),
        (total, percentage),
    )
    assert not [row for row in evidence if row.metric_name == "rpo_current"]


def test_xbrl_explicit_twelve_month_percentage_derives_current_rpo() -> None:
    filing = FilingRef(
        ticker="TEST",
        cik="0000000001",
        accession_number="0000000001-26-000001",
        form_type="10-Q",
        filing_date="2026-04-30",
        accepted_at="2026-04-30T12:00:00Z",
        report_date="2026-03-31",
        primary_document="filing.htm",
        source_id="sec_submissions",
    )
    common = {
        "taxonomy": "us-gaap",
        "period_start": "",
        "period_end": "2026-03-31",
        "context_id": "current",
        "dimensions_json": "{}",
        "scope": "consolidated",
        "source_document": "filing.htm",
        "provider": "arelle",
        "concept_metadata_json": json.dumps(
            {
                "namespace_uri": "https://fasb.org/us-gaap/2026",
                "is_extension": False,
            }
        ),
    }
    total = NormalizedFact(
        **common,
        concept_name="RevenueRemainingPerformanceObligation",
        value_text="1000000000",
        numeric_value=1_000_000_000.0,
        unit="USD",
    )
    percentage = NormalizedFact(
        **common,
        concept_name=(
            "RevenueRemainingPerformanceObligation"
            "NextTwelveMonthsPercentage"
        ),
        value_text="0.7",
        numeric_value=0.7,
        unit="pure",
    )
    evidence = map_normalized_facts(
        WorkItem(
            model_family="machinery",
            adapter_path=ADAPTER,
            adapter_version="test_v2",
            filing=filing,
            documents=(),
            requested_metrics=get_registry().source_metrics,
        ),
        (total, percentage),
    )
    current = next(row for row in evidence if row.metric_name == "rpo_current")
    assert current.value == 700_000_000.0
    assert current.status == "ACCEPTED"


def test_xbrl_timing_dimensions_produce_exhaustive_total_and_current_bucket() -> None:
    filing = FilingRef(
        ticker="FLS",
        cik="0000030625",
        accession_number="0000030625-26-000012",
        form_type="10-Q",
        filing_date="2026-04-24",
        accepted_at="2026-04-24T12:00:00Z",
        report_date="2026-03-31",
        primary_document="fls-20260331.htm",
        source_id="sec_submissions",
    )
    metadata = json.dumps(
        {
            "namespace_uri": "https://fasb.org/us-gaap/2026",
            "is_extension": False,
        }
    )
    axis = (
        "us-gaap:"
        "RevenueRemainingPerformanceObligationExpectedTimingOfSatisfaction"
        "StartDateAxis"
    )
    facts = tuple(
        NormalizedFact(
            taxonomy="us-gaap",
            concept_name="RevenueRemainingPerformanceObligation",
            value_text=str(value),
            numeric_value=value,
            unit="USD",
            period_start="",
            period_end="2026-03-31",
            context_id=f"timing-{member}",
            dimensions_json=json.dumps({axis: member}),
            scope="dimensional",
            source_document="fls-20260331.htm",
            provider="arelle",
            concept_metadata_json=metadata,
        )
        for member, value in (
            ("2025-07-01", 390_000_000.0),
            ("2026-07-01", 631_000_000.0),
        )
    )
    evidence = map_normalized_facts(
        WorkItem(
            model_family="machinery",
            adapter_path=ADAPTER,
            adapter_version="test_v2",
            filing=filing,
            documents=(),
            requested_metrics=get_registry().source_metrics,
        ),
        facts,
    )
    total = next(
        row
        for row in evidence
        if row.metric_name == "remaining_performance_obligation"
    )
    current = next(
        row for row in evidence if row.metric_name == "rpo_current"
    )
    assert total.value == 1_021_000_000.0
    assert total.status == "ACCEPTED"
    assert current.value == 390_000_000.0
    assert current.status == "ACCEPTED"


def test_dimensionless_rpo_total_supersedes_incomplete_timing_sum() -> None:
    filing = FilingRef(
        ticker="ENS",
        cik="0001289308",
        accession_number="0001289308-24-000018",
        form_type="10-Q",
        filing_date="2024-05-08",
        accepted_at="2024-05-08T12:00:00Z",
        report_date="2024-03-31",
        primary_document="ens-20240331.htm",
        source_id="sec_submissions",
    )
    metadata = json.dumps(
        {
            "namespace_uri": "https://fasb.org/us-gaap/2024",
            "is_extension": False,
        }
    )
    common = {
        "taxonomy": "us-gaap",
        "concept_name": "RevenueRemainingPerformanceObligation",
        "unit": "USD",
        "period_start": "",
        "period_end": "2024-03-31",
        "source_document": "ens-20240331.htm",
        "provider": "arelle",
        "concept_metadata_json": metadata,
    }
    axis = (
        "us-gaap:"
        "RevenueRemainingPerformanceObligationExpectedTimingOfSatisfaction"
        "StartDateAxis"
    )
    facts = (
        NormalizedFact(
            **common,
            value_text="147016000",
            numeric_value=147_016_000.0,
            context_id="dimensionless",
            dimensions_json="{}",
            scope="consolidated",
        ),
        NormalizedFact(
            **common,
            value_text="90000000",
            numeric_value=90_000_000.0,
            context_id="timing-current",
            dimensions_json=json.dumps({axis: "2024-04-01"}),
            scope="dimensional",
        ),
        NormalizedFact(
            **common,
            value_text="48685000",
            numeric_value=48_685_000.0,
            context_id="timing-tail",
            dimensions_json=json.dumps({axis: "2025-04-01"}),
            scope="dimensional",
        ),
    )
    evidence = map_normalized_facts(
        WorkItem(
            model_family="machinery",
            adapter_path=ADAPTER,
            adapter_version="test_v2",
            filing=filing,
            documents=(),
            requested_metrics=get_registry().source_metrics,
        ),
        facts,
    )
    totals = [
        row
        for row in evidence
        if row.metric_name == "remaining_performance_obligation"
    ]
    accepted = [row for row in totals if row.status == "ACCEPTED"]
    rejected = [row for row in totals if row.status == "REJECTED_POLICY"]
    assert [(row.value, row.reason) for row in accepted] == [
        (147_016_000.0, "standard_taxonomy_consolidated_semantic_fact")
    ]
    assert [(row.value, row.reason) for row in rejected] == [
        (138_685_000.0, "dimensionless_total_supersedes_timing_dimension_sum")
    ]
    current = next(
        row for row in evidence if row.metric_name == "rpo_current"
    )
    assert current.status == "ACCEPTED"
    assert current.value == 90_000_000.0


def test_tiny_timing_stub_is_not_twelve_month_current_rpo() -> None:
    filing = FilingRef(
        ticker="GNRC",
        cik="0001474735",
        accession_number="0001437749-25-033048",
        form_type="10-Q",
        filing_date="2025-11-04",
        accepted_at="2025-11-04T21:46:36Z",
        report_date="2025-09-30",
        primary_document="gnrc20250930_10q.htm",
        source_id="sec_submissions",
    )
    metadata = json.dumps(
        {
            "namespace_uri": "https://fasb.org/us-gaap/2025",
            "is_extension": False,
        }
    )
    common = {
        "taxonomy": "us-gaap",
        "concept_name": "RevenueRemainingPerformanceObligation",
        "unit": "USD",
        "period_start": "",
        "period_end": "2025-09-30",
        "source_document": "gnrc20250930_10q.htm",
        "provider": "arelle",
        "concept_metadata_json": metadata,
    }
    axis = (
        "us-gaap:"
        "RevenueRemainingPerformanceObligationExpectedTimingOfSatisfaction"
        "StartDateAxis"
    )
    facts = (
        NormalizedFact(
            **common,
            value_text="211430000",
            numeric_value=211_430_000.0,
            context_id="dimensionless",
            dimensions_json="{}",
            scope="consolidated",
        ),
        NormalizedFact(
            **common,
            value_text="9199",
            numeric_value=9_199.0,
            context_id="timing-stub",
            dimensions_json=json.dumps({axis: "2025-10-01"}),
            scope="dimensional",
        ),
        NormalizedFact(
            **common,
            value_text="38423000",
            numeric_value=38_423_000.0,
            context_id="timing-2026",
            dimensions_json=json.dumps({axis: "2026-10-01"}),
            scope="dimensional",
        ),
    )
    evidence = map_normalized_facts(
        WorkItem(
            model_family="machinery",
            adapter_path=ADAPTER,
            adapter_version="test_v2",
            filing=filing,
            documents=(),
            requested_metrics=get_registry().source_metrics,
        ),
        facts,
    )
    current = next(
        row for row in evidence if row.metric_name == "rpo_current"
    )
    assert current.value == 9_199.0
    assert current.status == "REJECTED_POLICY"
    assert current.confidence == 0.99
    assert (
        current.reason
        == "timing_dimension_current_fraction_outside_valid_range"
    )


def test_xbrl_timing_dimension_old_bucket_current_value_fails_closed() -> None:
    filing = FilingRef(
        ticker="FLS",
        cik="0000030625",
        accession_number="0000030625-26-000012",
        form_type="10-Q",
        filing_date="2026-04-24",
        accepted_at="2026-04-24T12:00:00Z",
        report_date="2026-03-31",
        primary_document="fls-20260331.htm",
        source_id="sec_submissions",
    )
    metadata = json.dumps(
        {
            "namespace_uri": "https://fasb.org/us-gaap/2026",
            "is_extension": False,
        }
    )
    axis = (
        "us-gaap:"
        "RevenueRemainingPerformanceObligationExpectedTimingOfSatisfaction"
        "StartDateAxis"
    )
    facts = tuple(
        NormalizedFact(
            taxonomy="us-gaap",
            concept_name="RevenueRemainingPerformanceObligation",
            value_text=str(value),
            numeric_value=value,
            unit="USD",
            period_start="",
            period_end="2026-03-31",
            context_id=f"timing-{member}",
            dimensions_json=json.dumps({axis: member}),
            scope="dimensional",
            source_document="fls-20260331.htm",
            provider="arelle",
            concept_metadata_json=metadata,
        )
        for member, value in (
            ("2020-01-01", 390_000_000.0),
            ("2026-07-01", 631_000_000.0),
        )
    )
    evidence = map_normalized_facts(
        WorkItem(
            model_family="machinery",
            adapter_path=ADAPTER,
            adapter_version="test_v2",
            filing=filing,
            documents=(),
            requested_metrics=get_registry().source_metrics,
        ),
        facts,
    )
    total = next(
        row
        for row in evidence
        if row.metric_name == "remaining_performance_obligation"
    )
    current = next(
        row for row in evidence if row.metric_name == "rpo_current"
    )
    assert total.value == 1_021_000_000.0
    assert total.status == "REVIEW_REQUIRED"
    assert (
        total.reason
        == "timing_dimension_incomplete_schedule_requires_sector_review"
    )
    assert current.value == 390_000_000.0
    assert current.status == "REJECTED_POLICY"
    assert current.confidence == 0.99
    assert (
        current.reason
        == "timing_dimension_current_bucket_not_twelve_months"
    )


def test_xbrl_partial_year_timing_stub_uses_ratio_before_spacing_gate() -> None:
    filing = FilingRef(
        ticker="GNRC",
        cik="0001474735",
        accession_number="0001437749-25-024879",
        form_type="10-Q",
        filing_date="2025-07-31",
        accepted_at="2025-07-31T12:00:00Z",
        report_date="2025-06-30",
        primary_document="gnrc20250630_10q.htm",
        source_id="sec_submissions",
    )
    metadata = json.dumps(
        {
            "namespace_uri": "https://fasb.org/us-gaap/2025",
            "is_extension": False,
        }
    )
    common = {
        "taxonomy": "us-gaap",
        "concept_name": "RevenueRemainingPerformanceObligation",
        "unit": "USD",
        "period_start": "",
        "period_end": "2025-06-30",
        "source_document": "gnrc20250630_10q.htm",
        "provider": "arelle",
        "concept_metadata_json": metadata,
    }
    axis = (
        "us-gaap:"
        "RevenueRemainingPerformanceObligationExpectedTimingOfSatisfaction"
        "StartDateAxis"
    )
    facts = (
        NormalizedFact(
            **common,
            value_text="202650000",
            numeric_value=202_650_000.0,
            context_id="dimensionless",
            dimensions_json="{}",
            scope="consolidated",
        ),
        NormalizedFact(
            **common,
            value_text="17994",
            numeric_value=17_994.0,
            context_id="remainder-2025",
            dimensions_json=json.dumps({axis: "2025-07-01"}),
            scope="dimensional",
        ),
        NormalizedFact(
            **common,
            value_text="37534000",
            numeric_value=37_534_000.0,
            context_id="calendar-2026",
            dimensions_json=json.dumps({axis: "2026-01-01"}),
            scope="dimensional",
        ),
    )
    evidence = map_normalized_facts(
        WorkItem(
            model_family="machinery",
            adapter_path=ADAPTER,
            adapter_version="test_v2",
            filing=filing,
            documents=(),
            requested_metrics=get_registry().source_metrics,
        ),
        facts,
    )
    current = next(
        row for row in evidence if row.metric_name == "rpo_current"
    )
    assert current.value == 17_994.0
    assert current.status == "REJECTED_POLICY"
    assert current.confidence == 0.99
    assert (
        current.reason
        == "timing_dimension_current_fraction_outside_valid_range"
    )


def test_xbrl_future_only_timing_dimensions_do_not_become_total_rpo() -> None:
    filing = FilingRef(
        ticker="VRT",
        cik="0001674101",
        accession_number="0001674101-26-000008",
        form_type="10-K",
        filing_date="2026-02-13",
        accepted_at="2026-02-13T12:00:00Z",
        report_date="2025-12-31",
        primary_document="vrt-20251231.htm",
        source_id="sec_submissions",
    )
    metadata = json.dumps(
        {
            "namespace_uri": "https://fasb.org/us-gaap/2025",
            "is_extension": False,
        }
    )
    axis = (
        "us-gaap:"
        "RevenueRemainingPerformanceObligationExpectedTimingOfSatisfaction"
        "StartDateAxis"
    )
    facts = tuple(
        NormalizedFact(
            taxonomy="us-gaap",
            concept_name="RevenueRemainingPerformanceObligation",
            value_text=str(value),
            numeric_value=value,
            unit="USD",
            period_start="",
            period_end="2025-12-31",
            context_id=f"timing-{member}",
            dimensions_json=json.dumps({axis: member}),
            scope="dimensional",
            source_document="vrt-20251231.htm",
            provider="arelle",
            concept_metadata_json=metadata,
        )
        for member, value in (
            ("2027-01-01", 55_200_000.0),
            ("2028-01-01", 27_900_000.0),
            ("2029-01-01", 24_500_000.0),
        )
    )
    evidence = map_normalized_facts(
        WorkItem(
            model_family="machinery",
            adapter_path=ADAPTER,
            adapter_version="test_v2",
            filing=filing,
            documents=(),
            requested_metrics=get_registry().source_metrics,
        ),
        facts,
    )
    total = next(
        row
        for row in evidence
        if row.metric_name == "remaining_performance_obligation"
    )
    current = next(
        row for row in evidence if row.metric_name == "rpo_current"
    )
    assert total.value == 107_600_000.0
    assert total.status == "REVIEW_REQUIRED"
    assert (
        total.reason
        == "timing_dimension_incomplete_schedule_requires_sector_review"
    )
    assert total.provenance["timing_schedule_complete"] is False
    assert current.status == "REJECTED_POLICY"
    assert current.confidence == 0.99
    assert (
        current.reason
        == "timing_dimension_current_bucket_not_twelve_months"
    )


def test_recovery_assessment_classifies_every_requested_source_metric(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "assessment.sqlite"
    filing_path = tmp_path / "filing.htm"
    filing_path.write_text("<html></html>", encoding="utf-8")
    filing, document = _filing_for_document(filing_path)
    registry = get_registry()
    with connect_database(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE feature_financial_metric_availability (
                ticker TEXT, model_family TEXT, asof_date TEXT,
                metric_name TEXT, availability_status TEXT,
                metric_value REAL, period_end TEXT, status_reason TEXT
            );
            CREATE TABLE feature_financial_statement (
                ticker TEXT, model_family TEXT, asof_date TEXT,
                fiscal_period_end TEXT
            );
            """
        )
        statuses = {
            "orders": "NOT_DISCLOSED",
            "funded_backlog": "NOT_APPLICABLE",
            "reported_backlog": "NOT_DISCLOSED",
            "remaining_performance_obligation": "NOT_DISCLOSED",
            "rpo_current": "NOT_DISCLOSED",
        }
        conn.executemany(
            """
            INSERT INTO feature_financial_metric_availability
            VALUES ('TEST', 'machinery', '2026-07-22', ?, ?, NULL, NULL, '')
            """,
            sorted(statuses.items()),
        )
        conn.execute(
            """
            INSERT INTO feature_financial_statement
            VALUES ('TEST', 'machinery', '2026-07-22', '2026-03-31')
            """
        )
        run_id = start_run(
            conn,
            model_family="machinery",
            asof_date="2026-07-22",
            adapter_version="test_v2",
            mode="shadow",
            worker_count=1,
        )
        item = WorkItem(
            model_family="machinery",
            adapter_path=ADAPTER,
            adapter_version="test_v2",
            filing=filing,
            documents=(document,),
            requested_metrics=registry.source_metrics,
        )
        catalog_documents(conn, filing=filing, documents=(document,))
        register_work(conn, run_id=run_id, item=item)
        mark_work_started(conn, item=item)
        persist_result(
            conn,
            run_id=run_id,
            result=WorkResult(
                work_key=item.work_key,
                model_family="machinery",
                adapter_version="test_v2",
                filing=filing,
                parser_release=item.parser_release,
                status="COMPLETED",
                metric_evidence=(
                    MetricEvidence(
                        metric_name="reported_backlog",
                        concept_name="ReportedBacklog",
                        value=1_000_000_000.0,
                        unit="USD",
                        period_start="",
                        period_end="2026-03-31",
                        scope="consolidated",
                        confidence=0.99,
                        status="ACCEPTED",
                        reason="fixture",
                        evidence_text="fixture",
                        source_document=document.name,
                        extraction_method="fixture",
                    ),
                    MetricEvidence(
                        metric_name="remaining_performance_obligation",
                        concept_name="RemainingPerformanceObligation",
                        value=800_000_000.0,
                        unit="USD",
                        period_start="",
                        period_end="2025-03-31",
                        scope="consolidated",
                        confidence=0.99,
                        status="ACCEPTED",
                        reason="fixture",
                        evidence_text="fixture",
                        source_document=document.name,
                        extraction_method="fixture",
                    ),
                    MetricEvidence(
                        metric_name="orders",
                        concept_name="Orders",
                        value=900_000_000.0,
                        unit="USD",
                        period_start="2026-04-01",
                        period_end="2026-06-30",
                        scope="consolidated",
                        confidence=0.99,
                        status="ACCEPTED",
                        reason="fixture",
                        evidence_text="fixture",
                        source_document=document.name,
                        extraction_method="fixture",
                    ),
                ),
            ),
        )
        conn.commit()
        assessments = build_recovery_assessments(
            conn,
            run_id=run_id,
            registry=registry,
            asof_date="2026-07-22",
            tickers=["TEST"],
            missing_cache_details=(
                {
                    "ticker": "TEST",
                    "accession_number": "missing-accession",
                    "form_type": "10-Q",
                    "filing_date": "2025-10-31",
                },
            ),
        )
        persist_recovery_assessments(
            conn,
            run_id=run_id,
            rows=assessments,
        )
        assert len(assessments) == len(registry.source_metrics)
        by_metric = {row["metric_name"]: row for row in assessments}
        assert (
            by_metric["reported_backlog"]["recovery_class"]
            == "RECOVERED_REPORTED"
        )
        assert (
            by_metric["remaining_performance_obligation"]["recovery_class"]
            == "HISTORICAL_RECOVERY_ONLY"
        )
        assert by_metric["funded_backlog"]["recovery_class"] == "STRUCTURAL_NA"
        assert by_metric["orders"]["recovery_class"] == "RECOVERED_REPORTED"
        assert by_metric["orders"]["anchor_period_end"] == "2026-06-30"
        assert by_metric["orders"]["accepted_current_count"] == 1
        assert (
            by_metric["rpo_current"]["recovery_class"]
            == "SOURCE_DOCUMENT_INCOMPLETE"
        )
        assert by_metric["rpo_current"]["missing_cache_filing_count"] == 1
        summary = assessment_summary(assessments)
        assert (
            summary["metric_coverage"]["reported_backlog"][
                "predicted_covered"
            ]
            == 1
        )


def test_policy_correction_is_not_counted_as_predicted_coverage() -> None:
    recovery_class, predicted_status, reason = _classify(
        baseline_status="REPORTED",
        accepted_current=0,
        accepted_historical=0,
        review_required=0,
        rejected=1,
        baseline_rejected_match=True,
        evidence_parser_failures=0,
        filing_count=1,
        document_count=1,
        failed_count=0,
        missing_cache_count=0,
    )
    assert recovery_class == "BASELINE_POLICY_CORRECTION"
    assert predicted_status == "NOT_DISCLOSED"
    assert "intentionally_suppressed" in reason


def test_recovery_document_counts_follow_cik_for_shared_ticker_aliases(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "shared_cik_recovery.sqlite"
    registry = AdapterRegistry(
        model_family="test_family",
        adapter_version="test_v1",
        supported_forms=("10-K",),
        source_metrics=(MetricRequest("reported_backlog"),),
        metric_dependencies={},
        document_keywords=("backlog",),
    )
    now = "2026-07-25T12:00:00Z"
    with connect_database(db_path) as conn:
        run_id = start_run(
            conn,
            model_family="test_family",
            asof_date="2026-07-24",
            adapter_version="test_v1",
            mode="shadow",
            worker_count=1,
        )
        conn.execute(
            """
            INSERT INTO sec_parser_work_ledger(
                work_key, run_id, model_family, ticker, cik,
                accession_number, parser_release, adapter_version,
                requested_metrics_json, input_hashes_json, status,
                attempt_count, completed_at
            )
            VALUES (
                'work-a', ?, 'test_family', 'CLASS-A', '0000001234',
                '0000001234-26-000001', 'test', 'test_v1',
                '[{"metric_name":"reported_backlog"}]', '{}',
                'COMPLETED', 1, ?
            )
            """,
            (run_id, now),
        )
        conn.execute(
            """
            INSERT INTO sec_parser_run_work(
                run_id, work_key, ticker, accession_number
            )
            VALUES (?, 'work-a', 'CLASS-A', '0000001234-26-000001')
            """,
            (run_id,),
        )
        conn.execute(
            """
            INSERT INTO sec_parser_document_catalog(
                cik, accession_number, document_name, ticker, form_type,
                filing_date, accepted_at, report_date, source_path,
                content_sha256, file_size, modified_ns, is_primary,
                is_full_submission, source_kind, cataloged_at
            )
            VALUES (
                '0000001234', '0000001234-26-000001', 'issuer.htm',
                'CLASS-B', '10-K', '2026-03-01', ?, '2025-12-31',
                'issuer.htm', 'hash', 100, 1, 1, 0,
                'sec_archive_document', ?
            )
            """,
            (now, now),
        )
        assessments = build_recovery_assessments(
            conn,
            run_id=run_id,
            registry=registry,
            asof_date="2026-07-24",
            tickers=["CLASS-A"],
        )

    assert len(assessments) == 1
    assert assessments[0]["searched_filing_count"] == 1
    assert assessments[0]["searched_document_count"] == 1
    assert (
        assessments[0]["recovery_class"]
        == "NOT_FOUND_IN_SEARCHED_DOCUMENTS"
    )
