from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from dedicated_parser.contracts import file_sha256
from industrials.transportation.ocr_recovery import (
    build_recovered_source_rows,
    inventory_sha256,
    isolate_document,
    summarize_ocr_results,
    tesseract_candidates,
)


def test_tesseract_candidates_include_portable_user_install(
    tmp_path: Path,
) -> None:
    candidates = tesseract_candidates(
        python_executable=tmp_path / "env" / "python.exe",
        home_dir=tmp_path,
    )

    assert (
        tmp_path
        / "AppData"
        / "Local"
        / "Programs"
        / "Tesseract-OCR-Portable"
        / "tesseract.exe"
    ) in candidates
    assert len({str(path).lower() for path in candidates}) == len(
        candidates
    )


def test_isolate_document_is_hash_checked_and_idempotent(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.7\nsealed")
    expected = file_sha256(source)
    target = tmp_path / "isolated" / "document.pdf"

    first = isolate_document(
        source_path=source,
        target_path=target,
        expected_sha256=expected,
    )
    second = isolate_document(
        source_path=source,
        target_path=target,
        expected_sha256=expected,
    )

    assert first in {"NTFS_HARDLINK", "FILE_COPY"}
    assert second == "EXISTING_HASH_VERIFIED"
    assert file_sha256(target) == expected
    with pytest.raises(ValueError, match="Source hash changed"):
        isolate_document(
            source_path=source,
            target_path=tmp_path / "other.pdf",
            expected_sha256="0" * 64,
        )


def test_inventory_sha256_changes_with_cache_inventory(
    tmp_path: Path,
) -> None:
    empty = inventory_sha256(tmp_path)
    assert empty == (0, hashlib.sha256().hexdigest())
    cache = tmp_path / "aa" / "one.json.gz"
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"cached")

    first = inventory_sha256(tmp_path)
    second = inventory_sha256(tmp_path)

    assert first == second
    assert first[0] == 1
    assert first[1] != empty[1]


def test_recovered_rows_and_summary_preserve_context_fanout(
    tmp_path: Path,
) -> None:
    isolated = tmp_path / "document.pdf"
    isolated.write_bytes(b"%PDF")
    base_rows = [
        {
            "ticker": ticker,
            "accession_number": f"NONSEC-{ticker}-one",
            "document_name": "one.pdf",
            "content_sha256": "a" * 64,
            "local_path": "original.pdf",
            "cache_status": "CACHE_VALIDATED_EMPTY_PYMUPDF",
            "source_kind": "old",
        }
        for ticker in ("TOO", "TGP")
    ]

    recovered = build_recovered_source_rows(
        base_rows=base_rows,
        recovered_paths={"a" * 64: isolated},
    )
    summary = summarize_ocr_results(
        [
            {
                "cache_status": "RECOVERED_OCR",
                "page_count": 3,
                "text_character_count": 120,
                "isolation_method": "NTFS_HARDLINK",
            },
            {
                "cache_status": "OCR_FAILED",
                "page_count": 0,
                "text_character_count": 0,
                "isolation_method": "NTFS_HARDLINK",
            },
        ]
    )

    assert [row["ticker"] for row in recovered] == ["TGP", "TOO"]
    assert all(
        row["cache_status"] == "CACHED_HASHED"
        and row["source_kind"]
        == "transportation_non_sec_primary_document"
        for row in recovered
    )
    assert summary["ocr_document_count"] == 2
    assert summary["ocr_recovered_document_count"] == 1
    assert summary["ocr_recovered_page_count"] == 3
    assert summary["ocr_recovered_text_character_count"] == 120
    assert summary["ocr_status_counts"] == {
        "OCR_FAILED": 1,
        "RECOVERED_OCR": 1,
    }
