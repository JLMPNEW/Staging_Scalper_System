"""Real-document parser pilot, evidence-funnel, and cache-replay tests."""

from __future__ import annotations

from collections import defaultdict
import hashlib
from html import escape
import json
from pathlib import Path

from basic_materials.adapters.dedicated_parser_adapter import (
    CompiledBlock,
    CompiledSemanticDocument,
    find_table_family_candidates,
)
from basic_materials.core.config import load_config
from basic_materials.core.specialized_contract import load_specialized_metric_registry
from basic_materials.core.specialized_parser_contract import load_specialized_parser_policy
from basic_materials.core.specialized_parser_pilot import (
    GoldenExpectationBundle,
    PilotDocument,
    PilotDocumentManifest,
    load_golden_expectations,
    load_pilot_document_manifest,
    load_specialized_parser_pilot_policy,
    run_real_document_parser_pilot,
)


def _block(index: int, text: str) -> CompiledBlock:
    return CompiledBlock(
        block_key=hashlib.sha256(f"{index}:{text}".encode()).hexdigest(),
        index=index,
        kind="table_row",
        text=text,
        section_path=(),
        table_id=1,
        row_index=index,
        cells=(text,),
        header_cells=(),
        preamble_text="",
    )


def test_candidate_search_reports_each_fail_closed_funnel_stage() -> None:
    document = CompiledSemanticDocument(
        source_document="sha256:test",
        media_type="text/html",
        compiler="test",
        parser_release="test",
        warning="",
        blocks=(
            _block(0, "Production was 10 tonnes in 2024."),
            _block(1, "Industry production was 20 tonnes in 2024."),
            _block(2, "Production increased."),
        ),
    )
    result = find_table_family_candidates(
        document,
        match_terms=("production",),
        prohibited_terms=("industry production",),
        preferred_block_kinds=("table_row",),
        minimum_numeric_tokens=1,
        limit=10,
    )
    assert result.lexical_hit_count == 3
    assert result.numeric_eligible_hit_count == 2
    assert result.review_candidate_hit_count == 1
    assert result.prohibited_context_hit_count == 1
    assert [row["candidate_status"] for row in result.candidates] == [
        "review_required",
        "prohibited_context",
    ]


def test_committed_real_document_contract_is_complete_and_review_only() -> None:
    config = load_config()
    policy = load_specialized_parser_pilot_policy(
        config.paths.specialized_parser_pilot_policy
    )
    documents = load_pilot_document_manifest(
        config.paths.specialized_parser_pilot_documents
    )
    golden = load_golden_expectations(
        config.paths.specialized_parser_golden_expectations
    )
    assert len(documents.documents) == 10
    assert {
        document.cohort_id
        for document in documents.documents
        if document.history_role == "current"
    } == set(policy.required_current_cohorts)
    assert {document.media_type for document in documents.documents} >= set(
        policy.required_media_types
    )
    assert any(document.history_role == "historical" for document in documents.documents)
    assert golden.expectations == ()
    assert policy.payload["required_flags"] == {
        "production_source_hydration_allowed": False,
        "production_parser_execution_allowed": False,
        "accepted_observation_write_allowed": False,
        "database_write_allowed": False,
        "historical_pit_materialization_allowed": False,
        "scoring_allowed": False,
        "calibration_allowed": False,
        "portfolio_promotion_allowed": False,
    }


def _pilot_document(
    cache_root: Path,
    *,
    document_id: str,
    ticker: str,
    cohort_id: str,
    history_role: str,
    media_type: str,
    payload: bytes,
) -> PilotDocument:
    suffix = {"text/html": ".html", "application/xml": ".xml", "application/json": ".json"}[
        media_type
    ]
    relative = Path("raw") / f"{document_id}{suffix}"
    target = cache_root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    return PilotDocument(
        document_id=document_id,
        ticker=ticker,
        cohort_id=cohort_id,
        history_role=history_role,
        source_family="sec_filing",
        source_id=("sec_companyfacts" if media_type == "application/json" else "sec_audited_filing_html"),
        accession_number=f"TEST-{document_id}",
        form_type="TEST",
        document_name=target.name,
        document_role="test_fixture",
        period_end="2024-12-31",
        availability_timestamp="2025-02-01T00:00:00Z",
        source_url=f"https://example.test/{document_id}{suffix}",
        cache_relative_path=relative.as_posix(),
        media_type=media_type,
        byte_size=len(payload),
        content_sha256=digest,
    )


def test_compile_once_then_cache_only_replay_without_raw_access(tmp_path: Path) -> None:
    config = load_config()
    policy = load_specialized_parser_pilot_policy(
        config.paths.specialized_parser_pilot_policy
    )
    registry = load_specialized_metric_registry(config.paths.specialized_metric_registry)
    parser_policy = load_specialized_parser_policy(config.paths.specialized_parser_policy)
    physical_families = {
        family
        for metric in registry.metrics
        for family in metric.table_families
        if family not in policy.derived_only_table_families
    }
    rules = {rule.table_family: rule for rule in policy.table_family_rules}
    families_by_cohort: dict[str, set[str]] = defaultdict(set)
    for metric in registry.metrics:
        families_by_cohort[metric.cohort_id].update(
            family for family in metric.table_families if family in physical_families
        )

    cache_root = tmp_path / "cache"
    documents: list[PilotDocument] = []
    for index, cohort in enumerate(policy.required_current_cohorts):
        terms = [rules[family].match_terms[0] for family in sorted(families_by_cohort[cohort])]
        body = ". ".join(terms) + ". 10 USD tonnes percent for year ended 2024."
        documents.append(
            _pilot_document(
                cache_root,
                document_id=f"current_{index}",
                ticker=f"T{index}",
                cohort_id=cohort,
                history_role="current",
                media_type="text/html",
                payload=f"<html><body><p>{escape(body)}</p></body></html>".encode(),
            )
        )

    companyfacts = {
        "entityName": "Pilot Company",
        "facts": {
            "us-gaap": {
                "Revenue": {
                    "label": "Revenue by segment",
                    "description": "Segment revenue test fact",
                    "units": {
                        "USD": [
                            {
                                "val": 10,
                                "start": "2024-01-01",
                                "end": "2024-12-31",
                                "filed": "2025-02-01",
                                "form": "10-K",
                                "accn": "TEST-JSON",
                            }
                        ]
                    },
                }
            }
        },
    }
    documents.append(
        _pilot_document(
            cache_root,
            document_id="structured_json",
            ticker="TJ",
            cohort_id="specialty_chemicals_materials",
            history_role="current",
            media_type="application/json",
            payload=json.dumps(companyfacts).encode(),
        )
    )
    documents.append(
        _pilot_document(
            cache_root,
            document_id="historical_xml",
            ticker="TH",
            cohort_id="precious_metals_producers",
            history_role="historical",
            media_type="application/xml",
            payload=b"<?xml version='1.0'?><report><p>Production 10 ounces for year ended 2020.</p></report>",
        )
    )

    manifest = PilotDocumentManifest(
        path=tmp_path / "manifest.csv",
        checksum="test-manifest",
        documents=tuple(documents),
    )
    golden = GoldenExpectationBundle(
        path=tmp_path / "golden.csv",
        checksum="test-golden",
        expectations=(),
    )
    source_ids = {"sec_audited_filing_html", "sec_companyfacts"}
    compiled = run_real_document_parser_pilot(
        policy=policy,
        documents=manifest,
        golden=golden,
        registry=registry,
        parser_policy=parser_policy,
        cache_root=cache_root,
        registered_source_ids=source_ids,
    )
    assert compiled.document_gate_passed is True
    assert compiled.compiler_gate_passed is True
    assert compiled.ready_for_golden_review is True
    assert compiled.golden_gate_passed is False
    assert compiled.counts["physical_compiles_this_run"] == len(documents)
    assert compiled.counts["families_with_review_candidates"] == len(physical_families)
    assert compiled.counts["database_rows_written"] == 0
    assert compiled.counts["pit_rows_written"] == 0
    assert all(row["raw_accessed"] == 1 for row in compiled.document_rows)

    expected_candidates = tuple(sorted(row["candidate_id"] for row in compiled.candidate_rows))
    for document in documents:
        (cache_root / document.cache_relative_path).unlink()
    replay = run_real_document_parser_pilot(
        policy=policy,
        documents=manifest,
        golden=golden,
        registry=registry,
        parser_policy=parser_policy,
        cache_root=cache_root,
        registered_source_ids=source_ids,
        cache_only=True,
    )
    assert replay.document_gate_passed is True
    assert replay.compiler_gate_passed is True
    assert replay.counts["physical_compiles_this_run"] == 0
    assert replay.counts["semantic_cache_hits_or_deduplicated"] == len(documents)
    assert all(row["raw_accessed"] == 0 for row in replay.document_rows)
    assert tuple(sorted(row["candidate_id"] for row in replay.candidate_rows)) == expected_candidates
