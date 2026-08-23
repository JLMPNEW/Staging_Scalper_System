from __future__ import annotations

import json
import runpy
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from dedicated_parser.cli import (
    _plan_summary_payload,
    main,
    reassess_existing_run,
)
from dedicated_parser.comparison import compare_shadow_run
from dedicated_parser.benchmark import (
    load_cohort_tickers,
    rank_missing_metric_tickers,
    write_benchmark_cohort,
)
from dedicated_parser.contracts import (
    AdapterRegistry,
    DocumentRef,
    FilingRef,
    MetricEvidence,
    MetricRequest,
    PlanSummary,
    WorkItem,
    WorkResult,
    file_sha256,
)
from dedicated_parser.funnel import build_extraction_funnel
from dedicated_parser.policy import (
    apply_review_policies,
    export_policy_golden_corpus,
    load_review_policies,
)
from dedicated_parser.providers.edgartools_provider import (
    inspect_full_submission,
)
from dedicated_parser.storage import (
    connect_database,
    finish_run,
    mark_work_started,
    persist_result,
    register_work,
    start_run,
)
from dedicated_parser.runtime import validate_provider_dependencies


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ADAPTER = "industrials.machinery.dedicated_parser_adapter:extract_metric_evidence"
POLICY_HEADER = (
    "policy_id,policy_version,enabled,model_family,ticker,accession_number,"
    "source_document,metric_name,concept_name,candidate_value,value_tolerance,"
    "unit,period_start,period_end,decision,status_reason,scope_override,"
    "confidence_override,reviewed_by,reviewed_at,period_start_override,"
    "period_end_override,value_override\n"
)


def test_resume_link_tables_index_work_key(tmp_path: Path) -> None:
    with connect_database(tmp_path / "parser.sqlite") as conn:
        fact_indexes = {
            str(row["name"]) for row in conn.execute("PRAGMA index_list(sec_parser_normalized_fact_shadow)")
        }
        evidence_indexes = {
            str(row["name"]) for row in conn.execute("PRAGMA index_list(sec_parser_metric_evidence_shadow)")
        }

    assert "idx_sec_parser_normalized_fact_shadow_work_key" in fact_indexes
    assert "idx_sec_parser_metric_evidence_shadow_work_key" in evidence_indexes


def test_shadow_comparison_scales_past_sqlite_expression_depth(
    tmp_path: Path,
) -> None:
    with connect_database(tmp_path / "parser.sqlite") as conn:
        conn.executescript(
            """
            CREATE TABLE fact_sec_metric_disclosure_candidate(
                ticker TEXT,
                metric_name TEXT,
                candidate_value REAL,
                unit TEXT,
                period_start TEXT,
                period_end TEXT,
                accession_number TEXT,
                model_family TEXT,
                candidate_status TEXT,
                accepted_at TEXT,
                filing_date TEXT
            );
            CREATE TABLE fact_sec_xbrl_fact(
                ticker TEXT,
                canonical_metric TEXT,
                value REAL,
                unit TEXT,
                period_start TEXT,
                period_end TEXT,
                accession_number TEXT,
                accepted_at TEXT,
                filing_date TEXT
            );
            """
        )
        run_id = start_run(
            conn,
            model_family="defense",
            asof_date="2026-07-24",
            adapter_version="test",
            mode="shadow",
            worker_count=1,
        )
        ledger_rows = [
            (
                f"work-{index}",
                run_id,
                "defense",
                f"T{index:05d}",
                "0000000001",
                f"0000000001-26-{index:06d}",
                "test",
                "test",
                "[]",
                "{}",
                "COMPLETED",
            )
            for index in range(10_001)
        ]
        conn.executemany(
            """
            INSERT INTO sec_parser_work_ledger(
                work_key, run_id, model_family, ticker, cik,
                accession_number, parser_release, adapter_version,
                requested_metrics_json, input_hashes_json, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ledger_rows,
        )
        conn.executemany(
            """
            INSERT INTO sec_parser_run_work(
                run_id, work_key, ticker, accession_number
            ) VALUES (?, ?, ?, ?)
            """,
            [(run_id, row[0], row[3], row[5]) for row in ledger_rows],
        )

        rows = compare_shadow_run(
            conn,
            run_id=run_id,
            model_family="defense",
            asof_date="2026-07-24",
            requested_metrics=("orders",),
        )

    assert rows == []


def test_shadow_comparison_allows_sector_without_legacy_tables(
    tmp_path: Path,
) -> None:
    with connect_database(tmp_path / "consumer-defensive.sqlite") as conn:
        run_id = start_run(
            conn,
            model_family="consumer_defensive",
            asof_date="2026-08-14",
            adapter_version="consumer_defensive_test",
            mode="shadow",
            worker_count=1,
        )
        conn.execute(
            """
            INSERT INTO sec_parser_work_ledger(
                work_key, run_id, model_family, ticker, cik,
                accession_number, parser_release, adapter_version,
                requested_metrics_json, input_hashes_json, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "consumer-defensive-work", run_id, "consumer_defensive",
                "KO", "0000021344", "0000021344-26-000001", "test",
                "consumer_defensive_test", "[]", "{}", "COMPLETED",
            ),
        )
        conn.execute(
            """INSERT INTO sec_parser_run_work(
                   run_id, work_key, ticker, accession_number
               ) VALUES (?, ?, ?, ?)""",
            (
                run_id, "consumer-defensive-work", "KO",
                "0000021344-26-000001",
            ),
        )

        rows = compare_shadow_run(
            conn,
            run_id=run_id,
            model_family="consumer_defensive",
            asof_date="2026-08-14",
            requested_metrics=("organic_revenue_growth_pct",),
        )

    assert rows == []


def test_edgartools_stderr_is_captured_as_provider_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeFilingSGML:
        @staticmethod
        def from_source(path: Path) -> SimpleNamespace:
            assert path.is_file()
            print(
                "Subheader 'COMPANY DATA' not found in header '] IRS NUMBER'",
                file=sys.stderr,
            )
            return SimpleNamespace(
                accession_number="0000000001-26-000001",
                cik="0000000001",
                form="10-K",
                attachments=[],
            )

    edgar_module = ModuleType("edgar")
    edgar_module.__path__ = []  # type: ignore[attr-defined]
    sgml_module = ModuleType("edgar.sgml")
    sgml_module.FilingSGML = FakeFilingSGML  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "edgar", edgar_module)
    monkeypatch.setitem(sys.modules, "edgar.sgml", sgml_module)
    source = tmp_path / "filing.txt"
    source.write_text("test", encoding="utf-8")

    metadata = inspect_full_submission(
        source,
        state_dir=tmp_path / "state",
    )

    assert capsys.readouterr().err == ""
    assert metadata["status"] == "parsed"
    assert metadata["stderr_warning_count"] == 1
    assert metadata["stderr_messages"] == ["Subheader 'COMPANY DATA' not found in header '] IRS NUMBER'"]


def _filing() -> FilingRef:
    return FilingRef(
        ticker="TEST",
        cik="0000000001",
        accession_number="0000000001-26-000001",
        form_type="10-Q",
        filing_date="2026-04-30",
        accepted_at="2026-04-30T12:00:00Z",
        report_date="2026-03-31",
        primary_document="test.htm",
        source_id="sec_submissions",
    )


def _document(tmp_path: Path) -> DocumentRef:
    path = tmp_path / "test.htm"
    path.write_text("<html><p>Backlog was $100 million.</p></html>", encoding="utf-8")
    stat = path.stat()
    return DocumentRef(
        name=path.name,
        path=str(path),
        content_sha256=file_sha256(path),
        file_size=stat.st_size,
        modified_ns=stat.st_mtime_ns,
        is_primary=True,
    )


def _evidence() -> MetricEvidence:
    return MetricEvidence(
        metric_name="reported_backlog",
        concept_name="ReportedBacklog",
        value=100_000_000.0,
        unit="USD",
        period_start="",
        period_end="2026-03-31",
        scope="unknown",
        confidence=0.5,
        status="REVIEW_REQUIRED",
        reason="unreviewed",
        evidence_text="Backlog was $100 million.",
        source_document="test.htm",
        extraction_method="dedicated_parser:semantic_html_table",
    )


def _policy_path(
    tmp_path: Path,
    *,
    decision: str = "ACCEPTED",
    period_start_override: str = "",
    period_end_override: str = "",
    value_override: str = "",
) -> Path:
    path = tmp_path / "policy.csv"
    path.write_text(
        POLICY_HEADER
        + (
            "test_policy,1.0.0,true,machinery,TEST,"
            "0000000001-26-000001,test.htm,reported_backlog,,100000000,1,"
            f"USD,,2026-03-31,{decision},reviewed_exact_total,"
            "consolidated,0.99,tester,2026-07-24T00:00:00Z,"
            f"{period_start_override},{period_end_override},"
            f"{value_override}\n"
        ),
        encoding="utf-8",
    )
    return path


def _work_item(
    tmp_path: Path,
    *,
    policy_path: Path | None = None,
) -> WorkItem:
    return WorkItem(
        model_family="machinery",
        adapter_path=ADAPTER,
        adapter_version="test_v1",
        filing=_filing(),
        documents=(_document(tmp_path),),
        requested_metrics=(MetricRequest("reported_backlog", ("Backlog",)),),
        review_policy_path=str(policy_path or ""),
        review_policy_sha256=(file_sha256(policy_path) if policy_path is not None else ""),
        enable_arelle=False,
        enable_edgartools=False,
    )


def _registry() -> AdapterRegistry:
    return AdapterRegistry(
        model_family="machinery",
        adapter_version="test_v1",
        supported_forms=("10-Q",),
        source_metrics=(MetricRequest("reported_backlog", ("Backlog",)),),
        metric_dependencies={},
        document_keywords=("backlog",),
    )


def _persist_completed_result(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    item: WorkItem,
) -> None:
    register_work(conn, run_id=run_id, item=item)
    mark_work_started(conn, item=item)
    persist_result(
        conn,
        run_id=run_id,
        result=WorkResult(
            work_key=item.work_key,
            model_family=item.model_family,
            adapter_version=item.adapter_version,
            filing=item.filing,
            parser_release=item.parser_release,
            status="COMPLETED",
            metric_evidence=(_evidence(),),
            provider_metadata={
                "edgartools": {
                    "status": "parsed",
                    "stderr_warning_count": 1,
                    "stderr_messages": ["known legacy header warning"],
                }
            },
        ),
    )
    conn.commit()


def test_persisted_evidence_is_immutable_across_work_versions(
    tmp_path: Path,
) -> None:
    with connect_database(tmp_path / "parser.sqlite") as conn:
        first_item = _work_item(tmp_path)
        first_run = start_run(
            conn,
            model_family="machinery",
            asof_date="2026-07-24",
            adapter_version=first_item.adapter_version,
            mode="shadow",
            worker_count=1,
        )
        register_work(conn, run_id=first_run, item=first_item)
        mark_work_started(conn, item=first_item)
        persist_result(
            conn,
            run_id=first_run,
            result=WorkResult(
                work_key=first_item.work_key,
                model_family=first_item.model_family,
                adapter_version=first_item.adapter_version,
                filing=first_item.filing,
                parser_release=first_item.parser_release,
                status="COMPLETED",
                metric_evidence=(_evidence(),),
            ),
        )

        second_item = replace(
            first_item,
            adapter_version="test_v2",
        )
        second_run = start_run(
            conn,
            model_family="machinery",
            asof_date="2026-07-24",
            adapter_version=second_item.adapter_version,
            mode="shadow",
            worker_count=1,
        )
        register_work(conn, run_id=second_run, item=second_item)
        mark_work_started(conn, item=second_item)
        persist_result(
            conn,
            run_id=second_run,
            result=WorkResult(
                work_key=second_item.work_key,
                model_family=second_item.model_family,
                adapter_version=second_item.adapter_version,
                filing=second_item.filing,
                parser_release=second_item.parser_release,
                status="COMPLETED",
                metric_evidence=(
                    replace(
                        _evidence(),
                        status="REJECTED_POLICY",
                        reason="new_policy_rejection",
                    ),
                ),
            ),
        )
        conn.commit()

        rows = conn.execute(
            """
            SELECT relation.run_id, evidence.adapter_version,
                   evidence.candidate_status, evidence.status_reason
            FROM sec_parser_run_metric_evidence AS relation
            JOIN sec_parser_metric_evidence_shadow AS evidence
              ON evidence.evidence_key = relation.evidence_key
            WHERE relation.run_id IN (?, ?)
            ORDER BY relation.run_id
            """,
            (first_run, second_run),
        ).fetchall()

    assert [tuple(row) for row in rows] == [
        (first_run, "test_v1", "REVIEW_REQUIRED", "unreviewed"),
        (
            second_run,
            "test_v2",
            "REJECTED_POLICY",
            "new_policy_rejection",
        ),
    ]


def test_exact_review_policy_changes_status_and_work_hash(tmp_path: Path) -> None:
    policy_path = _policy_path(tmp_path)
    item = _work_item(tmp_path, policy_path=policy_path)
    policies = load_review_policies(policy_path)
    assert len(policies) == 1
    reviewed = apply_review_policies(item, (_evidence(),))
    assert len(reviewed) == 1
    assert reviewed[0].status == "ACCEPTED"
    assert reviewed[0].scope == "consolidated"
    assert reviewed[0].confidence == 0.99
    assert reviewed[0].provenance["review_policy"]["policy_id"] == "test_policy"
    assert item.work_key != _work_item(tmp_path).work_key


def test_review_policy_materializes_missing_reviewed_observation(
    tmp_path: Path,
) -> None:
    policy_path = _policy_path(tmp_path)
    item = _work_item(tmp_path, policy_path=policy_path)

    reviewed = apply_review_policies(item, ())

    assert len(reviewed) == 1
    assert reviewed[0].status == "ACCEPTED"
    assert reviewed[0].value == 100_000_000.0
    assert reviewed[0].confidence == 0.99
    assert reviewed[0].extraction_method == "dedicated_parser:review_policy_registry"
    assert reviewed[0].provenance["review_policy"]["materialized"] is True
    assert reviewed[0].provenance["source_document"]["content_sha256"] == item.documents[0].content_sha256


def test_review_policy_matches_natively_corrected_override_period(
    tmp_path: Path,
) -> None:
    policy_path = _policy_path(
        tmp_path,
        period_start_override="2026-01-01",
        period_end_override="2026-03-31",
    )
    item = _work_item(tmp_path, policy_path=policy_path)
    corrected_evidence = replace(
        _evidence(),
        period_start="2026-01-01",
        period_end="2026-03-31",
    )

    reviewed = apply_review_policies(item, (corrected_evidence,))

    assert reviewed[0].status == "ACCEPTED"
    assert reviewed[0].period_start == "2026-01-01"
    assert reviewed[0].period_end == "2026-03-31"
    assert reviewed[0].provenance["review_policy"]["matched_period_start"] == "2026-01-01"


def test_plan_summary_payload_compacts_completed_work_lineage() -> None:
    summary = PlanSummary(
        asof_date="2026-07-22",
        model_family="machinery",
        requested_tickers=2,
        unresolved_metric_pairs=10,
        database_satisfied_pairs=0,
        scheduled_accessions=0,
        scheduled_documents=0,
        skipped_completed_accessions=2,
        missing_cache_accessions=0,
        skipped_completed_work=(
            {
                "work_key": "one",
                "ticker": "IEX",
                "accession_number": "a",
            },
            {
                "work_key": "two",
                "ticker": "INIO",
                "accession_number": "b",
            },
        ),
    )

    payload = _plan_summary_payload(summary)

    assert payload["linked_completed_work_count"] == 2
    assert "skipped_completed_work" not in payload


def test_rejected_policy_generates_decision_and_absence_gates(
    tmp_path: Path,
) -> None:
    policy_path = _policy_path(tmp_path, decision="REJECTED_POLICY")
    output_path = tmp_path / "generated.json"
    payload = export_policy_golden_corpus(
        load_review_policies(policy_path),
        output_path=output_path,
        corpus_id="test",
    )
    expectations = payload["expectations"]
    assert len(expectations) == 2
    assert expectations[0]["candidate_status"] == "REJECTED_POLICY"
    assert expectations[1]["candidate_status"] == "ACCEPTED"
    assert expectations[1]["expect_absent"] is True
    assert json.loads(output_path.read_text(encoding="utf-8")) == payload


def test_review_policy_can_correct_the_economic_period(tmp_path: Path) -> None:
    policy_path = _policy_path(
        tmp_path,
        period_start_override="2025-01-01",
        period_end_override="2025-12-31",
    )
    item = _work_item(tmp_path, policy_path=policy_path)
    reviewed = apply_review_policies(item, (_evidence(),))
    assert reviewed[0].period_start == "2025-01-01"
    assert reviewed[0].period_end == "2025-12-31"
    golden = export_policy_golden_corpus(
        load_review_policies(policy_path),
        output_path=tmp_path / "period-golden.json",
        corpus_id="test",
    )
    assert golden["expectations"][0]["period_start"] == "2025-01-01"
    assert golden["expectations"][0]["period_end"] == "2025-12-31"


def test_review_policy_can_apply_a_reviewed_value_scale(tmp_path: Path) -> None:
    policy_path = _policy_path(
        tmp_path,
        value_override="100000000000",
    )
    item = _work_item(tmp_path, policy_path=policy_path)
    reviewed = apply_review_policies(item, (_evidence(),))
    assert reviewed[0].value == 100_000_000_000.0
    assert reviewed[0].provenance["review_policy"]["matched_value"] == (100_000_000.0)
    golden = export_policy_golden_corpus(
        load_review_policies(policy_path),
        output_path=tmp_path / "value-golden.json",
        corpus_id="test",
    )
    assert golden["expectations"][0]["candidate_value"] == 100_000_000_000.0


def test_assessment_only_preserves_evidence_and_attempt_count(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "shadow.sqlite"
    item = _work_item(tmp_path)
    with connect_database(db_path) as conn:
        run_id = start_run(
            conn,
            model_family="machinery",
            asof_date="2026-07-22",
            adapter_version="test_v1",
            mode="shadow",
            worker_count=1,
            metadata={
                "plan": {
                    "missing_cache_accessions": 0,
                    "missing_cache_details": [],
                }
            },
        )
        _persist_completed_result(conn, run_id=run_id, item=item)
        finish_run(
            conn,
            run_id=run_id,
            status="COMPLETED",
            planned=1,
            completed=1,
            failed=0,
            metadata={
                "plan": {
                    "missing_cache_accessions": 0,
                    "missing_cache_details": [],
                }
            },
        )
        before = conn.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM sec_parser_metric_evidence_shadow),
              (SELECT attempt_count FROM sec_parser_work_ledger
               WHERE work_key = ?)
            """,
            (item.work_key,),
        ).fetchone()
        _, assessments, funnel = reassess_existing_run(
            conn,
            run_id=run_id,
            registry=_registry(),
            tickers=["TEST"],
        )
        after = conn.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM sec_parser_metric_evidence_shadow),
              (SELECT attempt_count FROM sec_parser_work_ledger
               WHERE work_key = ?)
            """,
            (item.work_key,),
        ).fetchone()
    assert tuple(before) == tuple(after)
    assert len(assessments) == 1
    assert funnel["evidence_stage_counts"] == {"semantic_table": 1}


def test_funnel_reports_provider_stage_status_and_assessment(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "funnel.sqlite"
    item = _work_item(tmp_path)
    with connect_database(db_path) as conn:
        run_id = start_run(
            conn,
            model_family="machinery",
            asof_date="2026-07-22",
            adapter_version="test_v1",
            mode="shadow",
            worker_count=1,
            metadata={
                "plan": {
                    "scheduled_accessions": 1,
                    "scheduled_documents": 1,
                    "missing_cache_accessions": 0,
                    "missing_cache_details": [],
                }
            },
        )
        _persist_completed_result(conn, run_id=run_id, item=item)
        funnel = build_extraction_funnel(conn, run_id=run_id)
    assert funnel["cache"]["complete"] is True
    assert funnel["work_status_counts"] == {"COMPLETED": 1}
    assert funnel["provider_execution_status_counts"] == {"edgartools": {"parsed": 1}}
    assert funnel["provider_stderr_warning_counts"] == {"edgartools": 1}
    assert funnel["provider_stderr_messages"] == {"edgartools": ["known legacy header warning"]}
    assert funnel["evidence_stage_counts"] == {"semantic_table": 1}
    assert funnel["evidence_status_counts"] == {"REVIEW_REQUIRED": 1}
    assert funnel["detail_rows"][0]["distinct_tickers"] == 1


def test_complete_cache_gate_stops_before_run_allocation(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "missing.sqlite"
    cache_dir = tmp_path / "cache"
    with connect_database(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE dim_universe_membership (
                ticker TEXT, model_family TEXT, start_date TEXT, end_date TEXT
            );
            CREATE TABLE dim_company (ticker TEXT, currency TEXT);
            CREATE TABLE fact_sec_filing (
                ticker TEXT, cik TEXT, accession_number TEXT, form_type TEXT,
                filing_date TEXT, accepted_at TEXT, report_date TEXT,
                primary_document TEXT, source_id TEXT
            );
            CREATE TABLE feature_financial_metric_availability (
                ticker TEXT, model_family TEXT, asof_date TEXT,
                metric_name TEXT, availability_status TEXT
            );
            CREATE TABLE fact_sec_xbrl_fact (
                ticker TEXT, canonical_metric TEXT, accepted_at TEXT,
                filing_date TEXT, value REAL
            );
            INSERT INTO dim_universe_membership
            VALUES ('TEST', 'machinery', '2020-01-01', NULL);
            INSERT INTO dim_company VALUES ('TEST', 'USD');
            INSERT INTO fact_sec_filing VALUES (
                'TEST', '0000000001', '0000000001-26-000001', '10-Q',
                '2026-04-30', '2026-04-30T12:00:00Z', '2026-03-31',
                'test.htm', 'sec_submissions'
            );
            INSERT INTO feature_financial_metric_availability
            VALUES (
                'TEST', 'machinery', '2026-07-22',
                'reported_backlog', 'NOT_DISCLOSED'
            );
            """
        )
        conn.commit()
    output_path = tmp_path / "gate.json"
    dedicated_gate_path = tmp_path / "cache-gate.json"
    status = main(
        [
            "--db",
            str(db_path),
            "--cache-dir",
            str(cache_dir),
            "--adapter",
            ("industrials.machinery.dedicated_parser_adapter:extract_metric_evidence"),
            "--asof",
            "2026-07-22",
            "--tickers",
            "TEST",
            "--plan-only",
            "--require-complete-cache",
            "--output-json",
            str(output_path),
            "--cache-gate-output-json",
            str(dedicated_gate_path),
        ]
    )
    with connect_database(db_path, readonly=True) as conn:
        run_count = conn.execute("SELECT COUNT(*) FROM sec_parser_run").fetchone()[0]
    assert status == 2
    assert run_count == 0
    assert not output_path.exists()
    assert json.loads(dedicated_gate_path.read_text(encoding="utf-8"))["mode"] == "cache_gate_failed"


def test_policy_corpus_is_not_exported_before_sector_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import dedicated_parser.cli as cli

    registry = AdapterRegistry(
        model_family='consumer_defensive', adapter_version='test',
        supported_forms=('10-K',), source_metrics=(MetricRequest('metric'),),
        metric_dependencies={}, document_keywords=(),
        review_policy_path=str(tmp_path / 'policy.yaml'),
        review_policy_golden_path=str(tmp_path / 'golden.json'),
    )
    exported: list[bool] = []
    monkeypatch.setattr(cli, 'load_registry', lambda _path: registry)
    monkeypatch.setattr(
        cli, '_export_policy_corpus', lambda _registry: exported.append(True)
    )

    def fail_preflight(*_args: object, **_kwargs: object):
        raise RuntimeError('simulated invalid sector seal')

    monkeypatch.setattr(cli, 'build_plan', fail_preflight)
    with pytest.raises(RuntimeError, match='invalid sector seal'):
        cli.main([
            '--db', str(tmp_path / 'empty.sqlite'),
            '--cache-dir', str(tmp_path / 'cache'),
            '--adapter', 'test.adapter:extract',
            '--asof', '2026-08-12', '--plan-only',
        ])
    assert exported == []
    assert not (tmp_path / 'golden.json').exists()


def test_defense_hydration_scope_preserves_missing_reason() -> None:
    namespace = runpy.run_path(
        str(
            PROJECT_ROOT
            / "industrials"
            / "defense"
            / "scripts"
            / "08d_run_defense_dedicated_parser_shadow.py"
        )
    )
    assert namespace["HYDRATION_SCOPE_FIELDS"] == [
        "ticker",
        "accession_number",
        "form_type",
        "filing_date",
        "reason",
    ]

def test_machinery_hydration_command_is_bounded_and_cache_reusing() -> None:
    namespace = runpy.run_path(
        str(PROJECT_ROOT / "industrials" / "machinery" / "scripts" / "08d_run_machinery_dedicated_parser_shadow.py")
    )
    command = namespace["build_hydration_command"](
        python_executable=Path("python.exe"),
        config_path=Path("machinery/config.yaml"),
        db_path=Path("industrials.sqlite"),
        asof_date="2026-07-22",
        tickers=["SHMD", "BLDP", "SHMD"],
    )
    assert command[command.index("--tickers") + 1] == "BLDP,SHMD"
    assert "--include-historical" in command
    assert "--archive-bootstrap" in command
    assert "--allow-partial" in command
    assert "--force" not in command
    assert "--force-archive" not in command
    validate_cache_window_config = namespace["validate_cache_window_config"]
    validate_cache_window_config(
        parser_max_filings=40,
        archive_max_filings=40,
    )
    validate_cache_window_config(
        parser_max_filings=40,
        archive_max_filings=0,
    )
    try:
        validate_cache_window_config(
            parser_max_filings=40,
            archive_max_filings=32,
        )
    except ValueError as exc:
        assert "archive=32" in str(exc)
    else:
        raise AssertionError("Expected cache-window mismatch to fail")


def test_machinery_shadow_explicit_zero_limit_means_unlimited() -> None:
    namespace = runpy.run_path(
        str(PROJECT_ROOT / "industrials" / "machinery" / "scripts" / "08d_run_machinery_dedicated_parser_shadow.py")
    )
    resolve_limit = namespace["resolve_limit"]
    assert resolve_limit(None, 40) == 40
    assert resolve_limit(0, 40) == 0
    assert resolve_limit(12, 40) == 12
    with pytest.raises(ValueError, match="zero or positive"):
        resolve_limit(-1, 40)


def test_enabled_provider_dependencies_fail_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_import(module_name: str) -> object:
        if module_name == "arelle.Cntlr":
            raise ImportError("missing")
        return object()

    monkeypatch.setattr("dedicated_parser.runtime.import_module", fake_import)
    with pytest.raises(RuntimeError, match="Arelle.*--disable-arelle"):
        validate_provider_dependencies(
            enable_arelle=True,
            enable_edgartools=True,
        )
    validate_provider_dependencies(
        enable_arelle=False,
        enable_edgartools=True,
    )


def test_planned_pdf_dependencies_fail_before_run_allocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_pdf_import(module_name: str) -> object:
        if module_name in {'pypdf', 'fitz'}:
            raise ImportError('missing')
        return object()

    monkeypatch.setattr(
        'dedicated_parser.runtime.import_module', missing_pdf_import
    )
    with pytest.raises(RuntimeError, match='PDF native text extraction'):
        validate_provider_dependencies(
            enable_arelle=False,
            enable_edgartools=False,
            require_pdf_native=True,
        )
    validate_provider_dependencies(
        enable_arelle=False,
        enable_edgartools=False,
        require_pdf_native=False,
    )


def test_benchmark_cohort_ranks_supported_missing_metrics(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "benchmark.sqlite"
    registry = AdapterRegistry(
        model_family="test_family",
        adapter_version="test_v1",
        supported_forms=("10-Q",),
        source_metrics=(
            MetricRequest("metric_a"),
            MetricRequest("metric_b"),
        ),
        metric_dependencies={},
        document_keywords=(),
    )
    with connect_database(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE feature_financial_metric_availability (
                ticker TEXT, model_family TEXT, asof_date TEXT,
                metric_name TEXT, availability_status TEXT
            );
            CREATE TABLE fact_sec_xbrl_fact (
                ticker TEXT, canonical_metric TEXT, accepted_at TEXT,
                filing_date TEXT, value REAL
            );
            INSERT INTO feature_financial_metric_availability VALUES
                ('AAA', 'test_family', '2026-07-22',
                 'metric_a', 'NOT_DISCLOSED'),
                ('AAA', 'test_family', '2026-07-22',
                 'metric_b', 'DISCLOSED_UNPARSED'),
                ('BBB', 'test_family', '2026-07-22',
                 'metric_a', 'NOT_DISCLOSED'),
                ('BBB', 'test_family', '2026-07-22',
                 'metric_b', 'REPORTED'),
                ('CCC', 'test_family', '2026-07-22',
                 'metric_a', 'NOT_DISCLOSED'),
                ('CCC', 'test_family', '2026-07-22',
                 'metric_b', 'NOT_DISCLOSED');
            INSERT INTO fact_sec_xbrl_fact VALUES (
                'CCC', 'metric_a', '2026-04-30T12:00:00Z',
                '2026-04-30', 1.0
            );
            """
        )
        payload = rank_missing_metric_tickers(
            conn,
            registry=registry,
            adapter_path=ADAPTER,
            asof_date="2026-07-22",
            limit=2,
            tickers=["AAA", "BBB", "CCC"],
        )
    assert payload["selected_tickers"] == ["AAA", "BBB"]
    assert payload["rows"][0]["missing_metrics"] == [
        "metric_a",
        "metric_b",
    ]
    assert payload["rows"][1]["missing_metric_count"] == 1

    json_path = tmp_path / "cohort.json"
    csv_path = tmp_path / "cohort.csv"
    write_benchmark_cohort(
        payload=payload,
        json_path=json_path,
        csv_path=csv_path,
    )
    assert load_cohort_tickers(json_path) == ["AAA", "BBB"]
    assert load_cohort_tickers(csv_path) == ["AAA", "BBB"]
