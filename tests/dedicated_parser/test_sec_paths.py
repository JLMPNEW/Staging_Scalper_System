from __future__ import annotations

from pathlib import Path

import pytest

from dedicated_parser.catalog import build_document_refs
from dedicated_parser.contracts import FilingRef
from dedicated_parser.sec_paths import (
    SEC_SUBMISSIONS_ARCHIVE_SUFFIXES,
    quote_sec_relative_document_path,
    resolve_sec_document_path,
    resolve_sec_relative_document_path,
    resolve_sec_seal_root,
    validate_sec_document_basename,
    validate_sec_relative_document_path,
)


@pytest.mark.parametrize(
    "name",
    [
        "", ".", "..", "../escape.htm", "folder/file.htm", r"folder\file.htm",
        r"C:\escape.htm", r"\\server\share.htm", "CON.htm", "COM1.old.htm",
        "LPT\u00b9.htm", "COM\u00b2.htm", "file.htm.", " file.htm",
        "file.htm ", "file.htm?x=1",
        "file.htm#x", "file\x00.htm", "file.exe", "file<name>.htm",
    ],
)
def test_document_basename_rejects_unsafe_names(name: str) -> None:
    with pytest.raises(ValueError):
        validate_sec_document_basename(name)


def test_relative_sec_path_accepts_real_nested_xsl_and_quotes_segments() -> None:
    value = "xslF345X06/doc 4.xml"
    assert validate_sec_relative_document_path(value) == value
    assert quote_sec_relative_document_path(value) == "xslF345X06/doc%204.xml"


@pytest.mark.parametrize(
    "value",
    [
        "../doc.xml", "/doc.xml", r"xsl\doc.xml", "xsl/../doc.xml",
        "xsl//doc.xml", "foo./doc.htm", "foo /doc.htm",
    ],
)
def test_relative_sec_path_rejects_traversal_and_noncanonical_paths(value: str) -> None:
    with pytest.raises(ValueError):
        validate_sec_relative_document_path(value)


def test_relative_path_resolution_is_contained(tmp_path: Path) -> None:
    accession = tmp_path / "accession"
    nested = accession / "xslF345X06"
    nested.mkdir(parents=True)
    document = nested / "doc4.xml"
    document.write_text("safe", encoding="utf-8")
    assert resolve_sec_relative_document_path(
        accession, "xslF345X06/doc4.xml", containment_root=tmp_path,
        require_file=True,
    ) == document.resolve()


def test_document_symlink_escape_is_rejected(tmp_path: Path) -> None:
    accession = tmp_path / "accession"
    accession.mkdir()
    outside = tmp_path / "outside.htm"
    outside.write_text("outside", encoding="utf-8")
    link = accession / "filing.htm"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with pytest.raises(ValueError, match="outside the accession"):
        resolve_sec_document_path(accession, "filing.htm", require_file=True)


def test_seal_relative_path_is_canonical_and_relocatable(tmp_path: Path) -> None:
    relocated = tmp_path / "relocated"
    expected = relocated.resolve() / "sealed" / "2026-08-12"
    assert resolve_sec_seal_root(
        relocated, "sealed/2026-08-12", expected_asof="2026-08-12"
    ) == expected
    with pytest.raises(ValueError):
        resolve_sec_seal_root(relocated, "../outside-seal")


def test_required_parser_document_symlink_escape_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    filing = FilingRef(
        ticker="TEST", cik="0000000001", accession_number="0000000001-26-000001",
        form_type="10-K", filing_date="2026-02-01", accepted_at="2026-02-01T12:00:00Z",
        report_date="2025-12-31", primary_document="filing.htm", source_id="sec_submissions",
    )
    accession = (
        tmp_path / "sec_archive_xbrl" / "CIK0000000001" / "000000000126000001"
    )
    accession.mkdir(parents=True)
    outside = tmp_path / "outside.htm"
    outside.write_text("outside", encoding="utf-8")
    try:
        (accession / "filing.htm").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    import sqlite3
    conn = sqlite3.connect(":memory:")
    try:
        with pytest.raises(ValueError, match="outside the accession"):
            build_document_refs(
                conn, cache_dir=tmp_path, filing=filing, keywords=(),
                required_documents=["filing.htm"],
            )
    finally:
        conn.close()


def test_generic_catalog_accepts_contained_nested_primary_document(
    tmp_path: Path,
) -> None:
    filing = FilingRef(
        ticker="TEST", cik="0000000001",
        accession_number="0000000001-26-000001", form_type="10-K",
        filing_date="2026-02-01", accepted_at="2026-02-01T12:00:00Z",
        report_date="2025-12-31",
        primary_document="xslF345X06/doc4.xml", source_id="sec_submissions",
    )
    accession = (
        tmp_path / "sec_archive_xbrl" / "CIK0000000001"
        / "000000000126000001"
    )
    nested = accession / "xslF345X06"
    nested.mkdir(parents=True)
    document = nested / "doc4.xml"
    document.write_text("<document/>", encoding="utf-8")
    import sqlite3
    conn = sqlite3.connect(":memory:")
    try:
        documents = build_document_refs(
            conn, cache_dir=tmp_path, filing=filing, keywords=(),
        )
    finally:
        conn.close()
    assert len(documents) == 1
    assert documents[0].name == "xslF345X06/doc4.xml"
    assert Path(documents[0].path) == document.resolve()
    assert documents[0].is_primary is True


def test_generic_catalog_accepts_paper_metadata_and_uses_full_submission(
    tmp_path: Path,
) -> None:
    filing = FilingRef(
        ticker="TEST", cik="0000000001",
        accession_number="0000000001-26-000001", form_type="10-K",
        filing_date="2026-02-01", accepted_at="2026-02-01T12:00:00Z",
        report_date="2025-12-31", primary_document="legacy.paper",
        source_id="sec_submissions",
    )
    accession = (
        tmp_path / "sec_archive_xbrl" / "CIK0000000001"
        / "000000000126000001"
    )
    accession.mkdir(parents=True)
    full_submission = accession / "0000000001-26-000001.txt"
    full_submission.write_text("<SEC-DOCUMENT>", encoding="utf-8")
    (accession / "index.json").write_text(
        '{"directory":{"item":['
        '{"name":"submission.paper","type":"10-K"},'
        '{"name":"0000000001-26-000001.txt","type":"10-K"}'
        ']}}', encoding="utf-8",
    )
    import sqlite3
    conn = sqlite3.connect(":memory:")
    try:
        documents = build_document_refs(
            conn, cache_dir=tmp_path, filing=filing, keywords=(),
        )
    finally:
        conn.close()
    assert [document.name for document in documents] == [full_submission.name]
    assert documents[0].is_primary is False
    assert documents[0].is_full_submission is True


def test_event_catalog_ignores_earnings_images_but_parses_release(
    tmp_path: Path,
) -> None:
    filing = FilingRef(
        ticker="TEST", cik="0000000001",
        accession_number="0000000001-26-000001", form_type="8-K",
        filing_date="2026-08-11", accepted_at="2026-08-11T20:00:00Z",
        report_date="2026-06-30", primary_document="filing.htm",
        source_id="sec_submissions",
    )
    accession = (
        tmp_path / "sec_archive_xbrl" / "CIK0000000001"
        / "000000000126000001"
    )
    accession.mkdir(parents=True)
    (accession / "filing.htm").write_text("<html>8-K</html>", encoding="utf-8")
    (accession / "earnings-release.htm").write_text(
        "<html>earnings release</html>", encoding="utf-8"
    )
    (accession / "earnings-release-005.jpg").write_bytes(b"not-a-document")
    (accession / "index.json").write_text(
        '{"directory":{"item":['
        '{"name":"filing.htm","type":"8-K","description":"Current report"},'
        '{"name":"earnings-release.htm","type":"EX-99.1","description":"Earnings release"},'
        '{"name":"earnings-release-005.jpg","type":"GRAPHIC","description":"Earnings release image"}'
        ']}}',
        encoding="utf-8",
    )
    import sqlite3

    conn = sqlite3.connect(":memory:")
    try:
        documents = build_document_refs(
            conn, cache_dir=tmp_path, filing=filing, keywords=(),
        )
    finally:
        conn.close()
    assert [document.name for document in documents] == [
        "filing.htm",
        "earnings-release.htm",
    ]



@pytest.mark.parametrize(
    "name", ["../doc.xml", r"xslF345X06\\doc4.xml", "CON/doc4.xml"],
)
def test_generic_catalog_rejects_unsafe_nested_primary_document(
    tmp_path: Path, name: str,
) -> None:
    filing = FilingRef(
        ticker="TEST", cik="0000000001",
        accession_number="0000000001-26-000001", form_type="10-K",
        filing_date="2026-02-01", accepted_at="2026-02-01T12:00:00Z",
        report_date="2025-12-31", primary_document=name,
        source_id="sec_submissions",
    )
    import sqlite3
    conn = sqlite3.connect(":memory:")
    try:
        with pytest.raises(ValueError):
            build_document_refs(
                conn, cache_dir=tmp_path, filing=filing, keywords=(),
            )
    finally:
        conn.close()


def test_json_archive_suffix_policy_rejects_other_suffix() -> None:
    assert validate_sec_document_basename(
        "submissions-001.json", allowed_suffixes=SEC_SUBMISSIONS_ARCHIVE_SUFFIXES
    ) == "submissions-001.json"
    with pytest.raises(ValueError):
        validate_sec_document_basename(
            "submissions-001.txt", allowed_suffixes=SEC_SUBMISSIONS_ARCHIVE_SUFFIXES
        )
