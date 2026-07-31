from __future__ import annotations

import io
import hashlib
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from dedicated_parser.contracts import DocumentRef, file_sha256
from industrials.machinery.disclosure_documents import extract_document_text
from industrials.machinery.disclosure_documents import DocumentText
from industrials.transportation import content_text_cache as cache_module
from industrials.transportation.content_text_cache import (
    ExtractionOptions,
    extract_document_once,
    legacy_word_docx_path,
    repair_pdf_cache_if_better,
)


OPTIONS = ExtractionOptions(
    enable_pdf_ocr=True,
    max_pdf_pages=0,
    max_pdf_bytes=75_000_000,
    pdf_extraction_timeout_seconds=120.0,
)


def _docx_bytes(text: str) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(
            "word/document.xml",
            "<?xml version='1.0' encoding='UTF-8' standalone='yes'?>"
            "<w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'>"
            f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body>"
            "</w:document>",
        )
    return stream.getvalue()


def _document(path: Path, *, name: str) -> DocumentRef:
    stat = path.stat()
    return DocumentRef(
        name=name,
        path=str(path),
        content_sha256=file_sha256(path),
        file_size=stat.st_size,
        modified_ns=stat.st_mtime_ns,
        is_primary=True,
        source_kind="transportation_non_sec_primary_document",
    )


def test_docx_extractor_reads_paragraph_text() -> None:
    extracted = extract_document_text(
        _docx_bytes("Fleet utilization 94%"),
        document_name="presentation.docx",
    )
    assert extracted.extraction_method == "docx_xml"
    assert extracted.text == "Fleet utilization 94%"


def test_xlsx_extractor_reads_cell_values() -> None:
    openpyxl = pytest.importorskip("openpyxl")
    workbook = openpyxl.Workbook()
    workbook.active.append(["Operating ratio", 0.87])
    stream = io.BytesIO()
    workbook.save(stream)

    extracted = extract_document_text(
        stream.getvalue(),
        document_name="supplement.xlsx",
    )

    assert extracted.extraction_method == "xlsx_openpyxl"
    assert "Operating ratio" in extracted.text
    assert "0.87" in extracted.text


def test_content_addressed_text_cache_extracts_once(tmp_path: Path) -> None:
    root = tmp_path / "non_sec_primary_documents"
    path = root / "content_sha256" / "aa" / "document.bin"
    path.parent.mkdir(parents=True)
    path.write_text("Passenger load factor was 88 percent.", encoding="utf-8")
    document = _document(path, name="release.html")

    first, first_status = extract_document_once(
        document,
        content_type="text/html",
        options=OPTIONS,
    )
    second, second_status = extract_document_once(
        document,
        content_type="text/html",
        options=OPTIONS,
    )

    assert first_status == "EXTRACTED_AND_CACHED"
    assert second_status == "CACHE_HIT"
    assert second.text == first.text


def test_parallel_writes_in_same_hash_prefix_use_distinct_atomic_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collision: dict[str, tuple[bytes, str]] = {}
    pair: tuple[tuple[bytes, str], tuple[bytes, str]] | None = None
    for index in range(2_000):
        payload = f"parallel-document-{index}".encode()
        content_hash = hashlib.sha256(payload).hexdigest()
        prefix = content_hash[:2]
        if prefix in collision:
            pair = (collision[prefix], (payload, content_hash))
            break
        collision[prefix] = (payload, content_hash)
    assert pair is not None
    root = tmp_path / "non_sec_primary_documents"
    documents: list[DocumentRef] = []
    for offset, (payload, content_hash) in enumerate(pair):
        path = root / "content_sha256" / content_hash[:2] / f"{offset}.bin"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        documents.append(_document(path, name=f"{offset}.html"))
    barrier = threading.Barrier(2)

    def synchronized_extract(*args: object, **kwargs: object) -> DocumentText:
        barrier.wait(timeout=5)
        return DocumentText("parallel text", "test")

    monkeypatch.setattr(cache_module, "_extract", synchronized_extract)
    monkeypatch.setattr(cache_module.time, "time_ns", lambda: 1)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda document: extract_document_once(
                    document,
                    content_type="text/html",
                    options=OPTIONS,
                ),
                documents,
            )
        )

    assert [status for _, status in results] == [
        "EXTRACTED_AND_CACHED",
        "EXTRACTED_AND_CACHED",
    ]
    assert len(list((root / "extracted_text_sha256").rglob("*.json.gz"))) == 2


def test_legacy_word_uses_preconverted_docx(tmp_path: Path) -> None:
    root = tmp_path / "non_sec_primary_documents"
    path = root / "content_sha256" / "aa" / "legacy.bin"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xd0\xcf\x11\xe0" + b"legacy-body")
    document = _document(path, name="legacy.doc")
    converted = legacy_word_docx_path(root, document.content_sha256)
    converted.parent.mkdir(parents=True)
    converted.write_bytes(_docx_bytes("Vessel count 42"))

    extracted, status = extract_document_once(
        document,
        content_type="application/msword",
        options=OPTIONS,
    )

    assert status == "EXTRACTED_AND_CACHED"
    assert extracted.extraction_method == "legacy_word_wordconv_docx_xml"
    assert extracted.text == "Vessel count 42"


def test_targeted_pdf_repair_replaces_only_with_better_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "non_sec_primary_documents"
    path = root / "content_sha256" / "aa" / "limited.bin"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"%PDF-limited")
    document = _document(path, name="limited.pdf")
    monkeypatch.setattr(
        cache_module,
        "_extract",
        lambda *args, **kwargs: DocumentText(
            "",
            "pdf_pypdf_failed",
            warning="broken stream",
        ),
    )
    extract_document_once(document, options=OPTIONS)
    monkeypatch.setattr(
        cache_module,
        "extract_pdf_text_pymupdf",
        lambda *args, **kwargs: DocumentText(
            "Recovered operating ratio 87%",
            "pdf_pymupdf_targeted_recovery",
        ),
    )

    repaired, status = repair_pdf_cache_if_better(
        document,
        options=OPTIONS,
    )

    assert status == "CACHE_IMPROVED_PYMUPDF"
    assert repaired.text == "Recovered operating ratio 87%"
    monkeypatch.setattr(
        cache_module,
        "extract_pdf_text_pymupdf",
        lambda *args, **kwargs: DocumentText("short", "test"),
    )
    retained, retained_status = repair_pdf_cache_if_better(
        document,
        options=OPTIONS,
    )
    assert retained_status == "CACHE_LIMITATION_RETAINED"
    assert retained.text == repaired.text
