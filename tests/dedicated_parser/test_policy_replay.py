from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from dedicated_parser.adjudication import (
    build_ambiguous_adjudication_skeleton,
    build_ocr_adjudication_skeleton,
)
from dedicated_parser.contracts import (
    AdapterRegistry,
    DocumentRef,
    FilingRef,
    MetricEvidence,
    MetricRequest,
    WorkItem,
    WorkResult,
    file_sha256,
)
from dedicated_parser.policy_replay_cli import main as policy_replay_main
from dedicated_parser.review_replay import (
    REVIEW_EVALUATION_CONTRACT_VERSION,
    base_run_scope_hash,
    load_review_evidence,
    materialize_review_evaluation_run,
    replay_review_policies,
)
from dedicated_parser.storage import (
    catalog_documents,
    connect_database,
    finish_run,
    mark_work_started,
    persist_result,
    register_work,
    start_run,
)


POLICY_HEADER = (
    "policy_id,policy_version,enabled,model_family,ticker,accession_number,"
    "source_document,metric_name,concept_name,candidate_value,value_tolerance,"
    "unit,period_start,period_end,decision,status_reason,scope_override,"
    "confidence_override,reviewed_by,reviewed_at,period_start_override,"
    "period_end_override,value_override\n"
)
ADAPTER = "test.adapter:extract_metric_evidence"


def _filing() -> FilingRef:
    return FilingRef(
        ticker="TEST",
        cik="0000000001",
        accession_number="0000000001-26-000001",
        form_type="10-Q",
        filing_date="2026-04-30",
        accepted_at="2026-04-30T21:00:00Z",
        report_date="2026-03-31",
        primary_document="test.htm",
        source_id="sec_archive_xbrl",
    )


def _document(tmp_path: Path) -> DocumentRef:
    path = tmp_path / "test.htm"
    path.write_text(
        "<html><p>Our backlog was $100 million.</p></html>",
        encoding="utf-8",
    )
    stat = path.stat()
    return DocumentRef(
        name=path.name,
        path=str(path),
        content_sha256=file_sha256(path),
        file_size=stat.st_size,
        modified_ns=stat.st_mtime_ns,
        is_primary=True,
    )


def _base_evidence(*, provenance: dict | None = None) -> MetricEvidence:
    return MetricEvidence(
        metric_name="reported_backlog",
        concept_name="ReportedBacklog",
        value=100_000_000.0,
        unit="USD",
        period_start="",
        period_end="2026-03-31",
        scope="unknown",
        confidence=0.65,
        status="REVIEW_REQUIRED",
        reason="unreviewed",
        evidence_text="Our backlog was $100 million.",
        source_document="test.htm",
        extraction_method="dedicated_parser:test_fixture",
        provenance=provenance or {"semantic_block_index": 1},
    )


def _base_run(
    conn,
    *,
    tmp_path: Path,
    evidence: tuple[MetricEvidence, ...] | None = None,
) -> tuple[int, WorkItem, DocumentRef]:
    document = _document(tmp_path)
    item = WorkItem(
        model_family="test_family",
        adapter_path=ADAPTER,
        adapter_version="test_adapter_v1",
        filing=_filing(),
        documents=(document,),
        requested_metrics=(MetricRequest("reported_backlog", ("Backlog",)),),
        # A base replay run is evaluated with no review policy. Its evidence
        # is immutable input to every later policy evaluation.
        review_policy_path="",
        review_policy_sha256="",
        enable_arelle=False,
        enable_edgartools=False,
        enable_pdf_ocr=False,
    )
    run_id = start_run(
        conn,
        model_family="test_family",
        asof_date="2026-07-26",
        adapter_version="test_adapter_v1",
        mode="shadow",
        worker_count=1,
    )
    catalog_documents(conn, filing=item.filing, documents=item.documents)
    register_work(conn, run_id=run_id, item=item)
    mark_work_started(conn, item=item)
    selected_evidence = (_base_evidence(),) if evidence is None else evidence
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
            metric_evidence=selected_evidence,
        ),
    )
    finish_run(
        conn,
        run_id=run_id,
        status="COMPLETED",
        planned=1,
        completed=1,
        failed=0,
    )
    return run_id, item, document


def _policy(
    tmp_path: Path,
    *,
    decision: str = "ACCEPTED",
    candidate_value: str = "100000000",
    period_end: str = "2026-03-31",
    source_document: str = "test.htm",
    metric_name: str = "reported_backlog",
    period_end_override: str = "",
    value_override: str = "",
) -> Path:
    path = tmp_path / "policy.csv"
    path.write_text(
        POLICY_HEADER
        + (
            "review_1,1.0.0,true,test_family,TEST,"
            f"0000000001-26-000001,{source_document},{metric_name},"
            f"ReportedBacklog,{candidate_value},1,USD,,{period_end},"
            f"{decision},reviewed_exact_total,consolidated,0.99,"
            "reviewer,2026-07-26T00:00:00Z,,"
            f"{period_end_override},{value_override}\n"
        ),
        encoding="utf-8",
    )
    return path


def test_policy_replay_accepts_without_mutating_base_or_opening_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "parser.sqlite"
    with connect_database(db_path) as conn:
        run_id, _, document = _base_run(conn, tmp_path=tmp_path)
        before_hash = base_run_scope_hash(conn, base_run_id=run_id)
        base_before = dict(conn.execute("SELECT * FROM sec_parser_metric_evidence_shadow").fetchone())
        Path(document.path).unlink()

        def source_read_prohibited(*args, **kwargs):
            raise AssertionError("policy replay opened a source document")

        monkeypatch.setattr(Path, "read_bytes", source_read_prohibited)
        summary = replay_review_policies(
            conn,
            base_run_id=run_id,
            adapter_path=ADAPTER,
            policy_path=_policy(tmp_path),
            expected_model_family="test_family",
        )
        evaluated = load_review_evidence(
            conn,
            evaluation_id=summary.evaluation_id,
        )
        base_after = dict(conn.execute("SELECT * FROM sec_parser_metric_evidence_shadow").fetchone())

    assert summary.status == "COMPLETED"
    assert summary.evaluation_contract_version == (REVIEW_EVALUATION_CONTRACT_VERSION)
    assert summary.base_scope_hash_before == before_hash
    assert summary.base_scope_hash_after == before_hash
    assert summary.base_evidence_count == 1
    assert summary.evaluated_evidence_count == 1
    assert summary.changed_evidence_count == 1
    assert summary.materialized_evidence_count == 0
    assert summary.applied_policy_count == 1
    assert summary.source_document_open_count == 0
    assert summary.arelle_invocation_count == 0
    assert summary.edgartools_invocation_count == 0
    assert summary.ocr_invocation_count == 0
    assert base_after == base_before
    assert evaluated[0]["base_evidence_key"] == base_before["evidence_key"]
    assert evaluated[0]["candidate_status"] == "ACCEPTED"
    assert evaluated[0]["scope"] == "consolidated"
    assert evaluated[0]["confidence"] == pytest.approx(0.99)
    assert evaluated[0]["policy_id"] == "review_1"


def test_policy_replay_is_idempotent_and_supports_explicit_status_selection(
    tmp_path: Path,
) -> None:
    with connect_database(tmp_path / "parser.sqlite") as conn:
        run_id, _, _ = _base_run(conn, tmp_path=tmp_path)
        policy = _policy(tmp_path, decision="REJECTED_POLICY")
        first = replay_review_policies(
            conn,
            base_run_id=run_id,
            adapter_path=ADAPTER,
            policy_path=policy,
        )
        second = replay_review_policies(
            conn,
            base_run_id=run_id,
            adapter_path=ADAPTER,
            policy_path=policy,
        )
        rejected = load_review_evidence(
            conn,
            evaluation_id=first.evaluation_id,
            statuses=("REJECTED_POLICY",),
        )
        accepted = load_review_evidence(
            conn,
            evaluation_id=first.evaluation_id,
            statuses=("ACCEPTED",),
        )
        evaluation_count = conn.execute("SELECT COUNT(*) FROM sec_parser_review_evaluation").fetchone()[0]
        evidence_count = conn.execute("SELECT COUNT(*) FROM sec_parser_review_evidence").fetchone()[0]

    assert first.evaluation_id == second.evaluation_id
    assert first.idempotent_reuse is False
    assert second.idempotent_reuse is True
    assert evaluation_count == 1
    assert evidence_count == 1
    assert len(rejected) == 1
    assert accepted == []


def test_review_evaluation_materializes_idempotent_zero_provider_parser_run(
    tmp_path: Path,
) -> None:
    with connect_database(tmp_path / 'parser.sqlite') as conn:
        base_run_id, _, _ = _base_run(conn, tmp_path=tmp_path)
        base_before = dict(conn.execute(
            'SELECT * FROM sec_parser_metric_evidence_shadow'
        ).fetchone())
        evaluation = replay_review_policies(
            conn,
            base_run_id=base_run_id,
            adapter_path=ADAPTER,
            policy_path=_policy(tmp_path),
        )
        run_id = materialize_review_evaluation_run(
            conn, evaluation_id=evaluation.evaluation_id
        )
        reused = materialize_review_evaluation_run(
            conn, evaluation_id=evaluation.evaluation_id
        )
        run = dict(conn.execute(
            'SELECT * FROM sec_parser_run WHERE run_id=?', (run_id,)
        ).fetchone())
        reviewed = dict(conn.execute(
            '''
            SELECT evidence.* FROM sec_parser_run_metric_evidence AS relation
            JOIN sec_parser_metric_evidence_shadow AS evidence
              ON evidence.evidence_key=relation.evidence_key
            WHERE relation.run_id=?
            ''',
            (run_id,),
        ).fetchone())
        evaluation_row = dict(conn.execute(
            '''SELECT * FROM sec_parser_review_evaluation
               WHERE evaluation_id=?''',
            (evaluation.evaluation_id,),
        ).fetchone())
        base_after = dict(conn.execute(
            'SELECT * FROM sec_parser_metric_evidence_shadow '
            'WHERE evidence_key=?',
            (base_before['evidence_key'],),
        ).fetchone())

    assert reused == run_id
    assert run['mode'] == 'review_replay'
    assert run['status'] == 'COMPLETED'
    assert run['worker_count'] == 0
    assert run['failed_work_count'] == 0
    assert reviewed['candidate_status'] == 'ACCEPTED'
    assert reviewed['confidence'] == pytest.approx(0.99)
    assert reviewed['run_id'] == run_id
    assert evaluation_row['materialized_run_id'] == run_id
    assert base_after == base_before


def test_ambiguous_review_pack_is_exact_pair_and_policy_ready(
    tmp_path: Path,
) -> None:
    with connect_database(tmp_path / 'parser.sqlite') as conn:
        run_id, _, _ = _base_run(conn, tmp_path=tmp_path)
        evidence_key = str(conn.execute(
            'SELECT evidence_key FROM sec_parser_metric_evidence_shadow'
        ).fetchone()[0])
        conn.execute(
            '''
            INSERT INTO sec_parser_recovery_assessment(
                run_id,model_family,ticker,metric_name,asof_date,
                baseline_status,baseline_value,anchor_period_end,
                current_match_mode,current_evidence_period_end,
                current_evidence_age_days,recovery_class,predicted_status,
                accepted_current_count,accepted_historical_count,
                review_required_count,rejected_count,parser_failure_count,
                searched_filing_count,searched_document_count,
                failed_filing_count,missing_cache_filing_count,
                evidence_keys_json,status_reason,created_at
            ) VALUES (?,?,?,?,?,'missing',NULL,'','none','',NULL,
                      'FOUND_AMBIGUOUS','review_required',0,0,1,0,0,
                      1,1,0,0,?,'conflicting_candidate_values',?)
            ''',
            (
                run_id, 'test_family', 'TEST', 'reported_backlog',
                '2026-07-26', json.dumps([evidence_key]),
                '2026-07-26T00:00:00Z',
            ),
        )
        rows = build_ambiguous_adjudication_skeleton(conn, run_id=run_id)

    assert len(rows) == 1
    assert rows[0]['ticker'] == 'TEST'
    assert rows[0]['metric_name'] == 'reported_backlog'
    assert rows[0]['evidence_key'] == evidence_key
    assert rows[0]['recovery_class'] == 'FOUND_AMBIGUOUS'
    assert rows[0]['enabled'] == 'false'
    assert rows[0]['decision'] == 'REVIEW_REQUIRED'
    assert rows[0]['candidate_value'] == pytest.approx(100_000_000.0)
    assert rows[0]['unit'] == 'USD'
    assert rows[0]['period_end'] == '2026-03-31'
    assert rows[0]['evidence_text']
    assert rows[0]['provenance_json']


def test_ocr_review_pack_is_exact_review_only_evidence(
    tmp_path: Path,
) -> None:
    with connect_database(tmp_path / 'parser.sqlite') as conn:
        run_id, _, _ = _base_run(conn, tmp_path=tmp_path)
        evidence_key = str(conn.execute(
            'SELECT evidence_key FROM sec_parser_metric_evidence_shadow'
        ).fetchone()[0])
        conn.execute(
            '''
            UPDATE sec_parser_metric_evidence_shadow
            SET provenance_json=?, candidate_status='REVIEW_REQUIRED',
                status_reason='ocr_derived_requires_review'
            WHERE evidence_key=?
            ''',
            (json.dumps({'ocr_used': True, 'ocr_page_indices': [1]}),
             evidence_key),
        )
        conn.execute(
            '''
            INSERT INTO sec_parser_recovery_assessment(
                run_id,model_family,ticker,metric_name,asof_date,
                baseline_status,baseline_value,anchor_period_end,
                current_match_mode,current_evidence_period_end,
                current_evidence_age_days,recovery_class,predicted_status,
                accepted_current_count,accepted_historical_count,
                review_required_count,rejected_count,parser_failure_count,
                searched_filing_count,searched_document_count,
                failed_filing_count,missing_cache_filing_count,
                evidence_keys_json,status_reason,created_at
            ) VALUES (?,?,?,?,?,'missing',NULL,'','none','',NULL,
                      'FOUND_AMBIGUOUS','review_required',0,0,1,0,0,
                      1,1,0,0,?,'ocr_review_required',?)
            ''',
            (
                run_id, 'test_family', 'TEST', 'reported_backlog',
                '2026-07-26', json.dumps([evidence_key]),
                '2026-07-26T00:00:00Z',
            ),
        )
        rows = build_ocr_adjudication_skeleton(conn, run_id=run_id)

    assert len(rows) == 1
    assert rows[0]['evidence_key'] == evidence_key
    assert rows[0]['enabled'] == 'false'
    assert rows[0]['decision'] == 'REVIEW_REQUIRED'
    assert rows[0]['status_reason'] == 'ocr_derived_requires_review'
    assert rows[0]['suggested_action'] == (
        'compare_rendered_page_and_ocr_text_then_accept_or_reject'
    )


def test_idempotent_reuse_fails_if_base_evidence_changes(
    tmp_path: Path,
) -> None:
    with connect_database(tmp_path / "parser.sqlite") as conn:
        run_id, _, _ = _base_run(conn, tmp_path=tmp_path)
        policy = _policy(tmp_path)
        replay_review_policies(
            conn,
            base_run_id=run_id,
            adapter_path=ADAPTER,
            policy_path=policy,
        )
        conn.execute(
            """
            UPDATE sec_parser_metric_evidence_shadow
            SET confidence = 0.1
            """
        )
        conn.commit()
        with pytest.raises(
            RuntimeError,
            match="changed after the completed review evaluation",
        ):
            replay_review_policies(
                conn,
                base_run_id=run_id,
                adapter_path=ADAPTER,
                policy_path=policy,
            )


def test_policy_replay_supports_value_and_period_override(
    tmp_path: Path,
) -> None:
    with connect_database(tmp_path / "parser.sqlite") as conn:
        run_id, _, _ = _base_run(conn, tmp_path=tmp_path)
        summary = replay_review_policies(
            conn,
            base_run_id=run_id,
            adapter_path=ADAPTER,
            policy_path=_policy(
                tmp_path,
                period_end_override="2025-12-31",
                value_override="125000000",
            ),
        )
        row = load_review_evidence(
            conn,
            evaluation_id=summary.evaluation_id,
        )[0]
        provenance = json.loads(row["provenance_json"])

    assert row["candidate_value"] == pytest.approx(125_000_000.0)
    assert row["period_end"] == "2025-12-31"
    assert provenance["review_policy"]["matched_value"] == pytest.approx(100_000_000.0)
    assert provenance["review_policy"]["matched_period_end"] == "2026-03-31"


def test_policy_replay_materializes_only_requested_metric_and_sealed_document(
    tmp_path: Path,
) -> None:
    with connect_database(tmp_path / "parser.sqlite") as conn:
        run_id, _, _ = _base_run(
            conn,
            tmp_path=tmp_path,
            evidence=(),
        )
        summary = replay_review_policies(
            conn,
            base_run_id=run_id,
            adapter_path=ADAPTER,
            policy_path=_policy(tmp_path),
        )
        rows = load_review_evidence(
            conn,
            evaluation_id=summary.evaluation_id,
        )

    assert summary.base_evidence_count == 0
    assert summary.evaluated_evidence_count == 1
    assert summary.materialized_evidence_count == 1
    assert rows[0]["base_evidence_key"] is None
    assert rows[0]["extraction_method"] == "dedicated_parser:review_policy_registry"
    provenance = json.loads(rows[0]["provenance_json"])
    assert provenance["review_policy"]["materialized"] is True


@pytest.mark.parametrize(
    ("source_document", "metric_name"),
    [
        ("not-in-base.htm", "reported_backlog"),
        ("test.htm", "not_requested"),
    ],
)
def test_policy_replay_rejects_materialization_outside_base_scope(
    tmp_path: Path,
    source_document: str,
    metric_name: str,
) -> None:
    with connect_database(tmp_path / "parser.sqlite") as conn:
        run_id, _, _ = _base_run(
            conn,
            tmp_path=tmp_path,
            evidence=(),
        )
        with pytest.raises(
            ValueError,
            match="outside the sealed base run",
        ):
            replay_review_policies(
                conn,
                base_run_id=run_id,
                adapter_path=ADAPTER,
                policy_path=_policy(
                    tmp_path,
                    source_document=source_document,
                    metric_name=metric_name,
                ),
            )


def test_policy_replay_rejects_already_policy_mutated_base_evidence(
    tmp_path: Path,
) -> None:
    reviewed = _base_evidence(
        provenance={
            "review_policy": {
                "policy_id": "old",
                "registry_sha256": "a" * 64,
            }
        }
    )
    with connect_database(tmp_path / "parser.sqlite") as conn:
        run_id, _, _ = _base_run(
            conn,
            tmp_path=tmp_path,
            evidence=(reviewed,),
        )
        with pytest.raises(
            ValueError,
            match="not immutable pre-policy evidence",
        ):
            replay_review_policies(
                conn,
                base_run_id=run_id,
                adapter_path=ADAPTER,
                policy_path=_policy(tmp_path),
            )


def test_explicit_policy_replay_cli_writes_evaluation_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "parser.sqlite"
    with connect_database(db_path) as conn:
        run_id, _, _ = _base_run(conn, tmp_path=tmp_path)
    policy = _policy(tmp_path)
    output = tmp_path / "policy_replay.json"
    module = ModuleType("test_replay_adapter")
    module.get_registry = lambda: AdapterRegistry(  # type: ignore[attr-defined]
        model_family="test_family",
        adapter_version="test_adapter_v1",
        supported_forms=("10-Q",),
        source_metrics=(MetricRequest("reported_backlog", ("Backlog",)),),
        metric_dependencies={},
        document_keywords=("backlog",),
        review_policy_path=str(policy),
    )
    monkeypatch.setitem(sys.modules, "test_replay_adapter", module)

    exit_code = policy_replay_main(
        [
            "--db",
            str(db_path),
            "--adapter",
            "test_replay_adapter:extract_metric_evidence",
            "--policy-replay-run-id",
            str(run_id),
            "--output-json",
            str(output),
            '--materialize-parser-run',
        ]
    )
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert payload["mode"] == "policy_replay"
    assert payload["status"] == "COMPLETED"
    assert payload["source_document_open_count"] == 0
    assert payload["arelle_invocation_count"] == 0
    assert payload["edgartools_invocation_count"] == 0
    assert payload["ocr_invocation_count"] == 0
    assert payload['materialized_parser_run_id'] > 0
