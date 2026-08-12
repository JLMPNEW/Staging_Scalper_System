from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest

from dedicated_parser.contracts import DocumentRef, FilingRef, file_sha256
from dedicated_parser.catalog import build_document_refs
from dedicated_parser.planner import _apply_document_scope, _planning_scope
from dedicated_parser.source_manifest import load_source_manifest


def _manifest(path: Path, *, content_hash: str, cache_status: str = "CACHED_HASHED") -> Path:
    path.write_text(
        "ticker,accession_number,document_name,content_sha256,cache_status\n"
        f"TEST,0000000001-26-000001,filing.htm,{content_hash},{cache_status}\n",
        encoding="utf-8",
    )
    return path


def _filing() -> FilingRef:
    return FilingRef(
        ticker="TEST",
        cik="0000000001",
        accession_number="0000000001-26-000001",
        form_type="10-K",
        filing_date="2026-02-15",
        accepted_at="2026-02-15T21:00:00Z",
        report_date="2025-12-31",
        primary_document="filing.htm",
        source_id="sec_submissions",
    )


def test_load_source_manifest_seals_complete_document_rows(tmp_path: Path) -> None:
    document = tmp_path / "filing.htm"
    document.write_text("<p>sealed</p>", encoding="utf-8")
    manifest = load_source_manifest(_manifest(tmp_path / "manifest.csv", content_hash=file_sha256(document)))
    assert manifest.row_count == 1
    assert manifest.tickers == ("TEST",)
    assert manifest.accessions == ("0000000001-26-000001",)
    assert manifest.documents[("TEST", "0000000001-26-000001")] == {"filing.htm": file_sha256(document)}


def test_source_manifest_rejects_uncached_rows(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not cached and sealed"):
        load_source_manifest(
            _manifest(
                tmp_path / "manifest.csv",
                content_hash="0" * 64,
                cache_status="MISSING",
            )
        )


def test_source_manifest_loads_hash_validated_direct_documents_and_metric_scope(
    tmp_path: Path,
) -> None:
    document = tmp_path / "investor-deck.html"
    document.write_text("<p>Load factor 88%</p>", encoding="utf-8")
    content_hash = file_sha256(document)
    manifest_path = tmp_path / "direct.csv"
    manifest_path.write_text(
        "ticker,accession_number,document_name,content_sha256,cache_status,"
        "local_path,cik,form_type,filing_date,requested_metric_ids,source_kind\n"
        f"TEST,DIRECT-1,investor-deck.html,{content_hash},CACHED_HASHED,"
        f"{document},NONSEC,INVESTOR_PRESENTATION,2026-01-15,"
        "passenger_load_factor|passenger_yield,"
        "transportation_non_sec_primary_document\n",
        encoding="utf-8",
    )

    manifest = load_source_manifest(manifest_path)

    key = ("TEST", "DIRECT-1")
    assert manifest.direct_document_mode is True
    assert manifest.direct_filings[key].form_type == "INVESTOR_PRESENTATION"
    assert manifest.direct_documents[key][0].path == str(document.resolve())
    assert manifest.direct_documents[key][0].content_sha256 == content_hash
    assert manifest.metric_scope[key] == {
        "passenger_load_factor",
        "passenger_yield",
    }


def test_source_manifest_rejects_changed_direct_document(tmp_path: Path) -> None:
    document = tmp_path / "deck.html"
    document.write_text("changed", encoding="utf-8")
    manifest_path = tmp_path / "direct.csv"
    manifest_path.write_text(
        "ticker,accession_number,document_name,content_sha256,cache_status,local_path\n"
        f"TEST,DIRECT-1,deck.html,{'0' * 64},CACHED_HASHED,{document}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="local_path hash mismatch"):
        load_source_manifest(manifest_path)


def test_source_manifest_accepts_safe_nested_sec_document_and_rejects_traversal(
    tmp_path: Path,
) -> None:
    document = tmp_path / 'copied.xml'
    document.write_text('<xbrl/>', encoding='utf-8')
    digest = file_sha256(document)
    header = (
        'ticker,accession_number,document_name,primary_document,'
        'content_sha256,cache_status,local_path,cik,form_type,filing_date\n'
    )
    valid = tmp_path / 'nested.csv'
    valid.write_text(
        header + (
            'TEST,0000000001-26-000001,xslF345X06/doc4.xml,'
            f'xslF345X06/doc4.xml,{digest},CACHED_HASHED,{document},'
            '0000000001,10-K,2026-02-15\n'
        ), encoding='utf-8',
    )
    loaded = load_source_manifest(valid)
    key = ('TEST', '0000000001-26-000001')
    assert loaded.direct_filings[key].primary_document == 'xslF345X06/doc4.xml'
    assert loaded.direct_documents[key][0].name == 'xslF345X06/doc4.xml'

    for index, unsafe in enumerate(('../doc4.xml', r'xslF345X06\doc4.xml')):
        rejected = tmp_path / f'unsafe-{index}.csv'
        rejected.write_text(
            header + (
                f'TEST,0000000001-26-000001,{unsafe},{unsafe},'
                f'{digest},CACHED_HASHED,{document},0000000001,10-K,'
                '2026-02-15\n'
            ), encoding='utf-8',
        )
        with pytest.raises(ValueError):
            load_source_manifest(rejected)


@pytest.mark.parametrize(
    ("scope", "expected_reason"),
    [
        ({}, "filing_not_present_in_source_manifest"),
        (
            {
                ("TEST", "0000000001-26-000001"): {
                    "filing.htm": "1" * 64,
                }
            },
            "manifest_document_hash_mismatch:filing.htm",
        ),
        (
            {
                ("TEST", "0000000001-26-000001"): {
                    "filing.htm": "0" * 64,
                    "missing.htm": "2" * 64,
                }
            },
            "manifest_documents_missing:missing.htm",
        ),
    ],
)
def test_document_scope_fails_closed(
    scope: dict[tuple[str, str], dict[str, str]],
    expected_reason: str,
) -> None:
    document = DocumentRef(
        name="filing.htm",
        path="filing.htm",
        content_sha256="0" * 64,
        file_size=1,
        modified_ns=1,
        is_primary=True,
    )
    selected, reason = _apply_document_scope(
        filing=_filing(),
        documents=(document,),
        document_scope=scope,
    )
    assert selected == ()
    assert reason == expected_reason


def test_document_scope_rejects_unsealed_extra_document() -> None:
    documents = (
        DocumentRef(
            name="filing.htm",
            path="filing.htm",
            content_sha256="0" * 64,
            file_size=1,
            modified_ns=1,
            is_primary=True,
        ),
        DocumentRef(
            name="extra.htm",
            path="extra.htm",
            content_sha256="1" * 64,
            file_size=1,
            modified_ns=1,
        ),
    )
    selected, reason = _apply_document_scope(
        filing=_filing(),
        documents=documents,
        document_scope={
            ("TEST", "0000000001-26-000001"): {
                "filing.htm": "0" * 64,
            }
        },
    )
    assert selected == ()
    assert reason == "unsealed_documents_present:extra.htm"


def test_manifest_documents_bypass_filename_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filing = _filing()
    directory = tmp_path / "sec_archive_xbrl" / f"CIK{filing.cik}" / filing.accession_number.replace("-", "")
    directory.mkdir(parents=True)
    (directory / filing.primary_document).write_text(
        "<p>primary</p>",
        encoding="utf-8",
    )
    opaque = directory / "opaque-deck.pdf"
    opaque.write_bytes(b"%PDF-1.4\nsealed")
    monkeypatch.setattr(
        "dedicated_parser.catalog._known_hash",
        lambda *args, **kwargs: "",
    )
    documents = build_document_refs(
        sqlite3.connect(":memory:"),
        cache_dir=tmp_path,
        filing=filing,
        keywords=("earnings",),
        max_documents=1,
        required_documents={"opaque-deck.pdf": file_sha256(opaque)},
    )
    assert [document.name for document in documents] == ["opaque-deck.pdf"]


def test_planning_scope_does_not_cross_shared_cik_ticker_lifecycles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = FilingRef(
        ticker="CURRENT",
        cik="0000000001",
        accession_number="0000000001-26-000001",
        form_type="10-K",
        filing_date="2026-02-15",
        accepted_at="2026-02-15T21:00:00Z",
        report_date="2025-12-31",
        primary_document="filing.htm",
        source_id="sec_submissions",
    )
    predecessor_alias = FilingRef(
        ticker="OLD",
        cik=current.cik,
        accession_number=current.accession_number,
        form_type=current.form_type,
        filing_date=current.filing_date,
        accepted_at=current.accepted_at,
        report_date=current.report_date,
        primary_document=current.primary_document,
        source_id=current.source_id,
    )
    registry = type(
        "Registry",
        (),
        {
            "model_family": "transportation",
            "parser_metrics": (),
            "metric_requirements": {},
            "supported_forms": ("10-K",),
        },
    )()
    monkeypatch.setattr(
        "dedicated_parser.planner.load_ticker_selector",
        lambda _: None,
    )
    monkeypatch.setattr(
        "dedicated_parser.planner.unresolved_dependency_requirements",
        lambda *args, **kwargs: {},
    )
    monkeypatch.setattr(
        "dedicated_parser.planner.filing_rows",
        lambda *args, **kwargs: {
            "CURRENT": [current],
            "OLD": [predecessor_alias],
        },
    )

    _, _, _, filings, _ = _planning_scope(
        sqlite3.connect(":memory:"),
        registry=cast(Any, registry),
        adapter_path="adapter.py",
        asof_date="2026-07-22",
        tickers=("CURRENT", "OLD"),
        accessions=(current.accession_number,),
        max_filings_per_ticker=0,
        force=True,
        all_metrics=True,
        document_scope={
            ("CURRENT", current.accession_number): {
                current.primary_document: "0" * 64,
            }
        },
    )

    assert filings["CURRENT"] == [current]
    assert filings["OLD"] == []
