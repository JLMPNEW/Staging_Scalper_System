from __future__ import annotations

from pathlib import Path

from industrials.transportation.delta_parser_manifest import (
    build_delta_parser_rows,
)


def test_delta_parser_manifest_hashes_only_sealed_documents(
    tmp_path: Path,
) -> None:
    accession = "0000000001-24-000001"
    directory = (
        tmp_path
        / "CIK0000000001"
        / accession.replace("-", "")
    )
    directory.mkdir(parents=True)
    primary = directory / "primary.htm"
    primary.write_text("<p>operating ratio 92%</p>", encoding="utf-8")
    pdf = directory / "deck.pdf"
    pdf.write_bytes(b"%PDF-1.4\nsealed")
    rows, errors = build_delta_parser_rows(
        delta_rows=[
            {
                "ticker": "TST",
                "cik": "1",
                "accession_number": accession,
                "form_type": "8-K",
                "filing_date": "2024-02-01",
                "accepted_at": "2024-02-01T16:00:00Z",
                "report_date": "2023-12-31",
                "primary_document": "primary.htm",
                "selected_document_names": "primary.htm|deck.pdf",
                "target_metric_ids": "operating_ratio|load_factor",
                "delta_action": "PARSE_NEW_CACHED_DOCUMENT_HASHES",
            }
        ],
        archive_cache_dir=tmp_path,
        source_id="sec_submissions",
    )
    assert errors == []
    assert [row["document_name"] for row in rows] == [
        "deck.pdf",
        "primary.htm",
    ]
    assert all(row["cache_status"] == "CACHED_HASHED" for row in rows)
    assert rows[0]["applicable_metric_count"] == 2


def test_delta_parser_manifest_fails_closed_on_unhydrated_delta(
    tmp_path: Path,
) -> None:
    rows, errors = build_delta_parser_rows(
        delta_rows=[
            {
                "ticker": "TST",
                "accession_number": "0000000001-24-000001",
                "delta_action": "HYDRATE_SELECTED_DOCUMENTS",
            }
        ],
        archive_cache_dir=tmp_path,
        source_id="sec_submissions",
    )
    assert rows == []
    assert errors == [
        "TST|0000000001-24-000001: unresolved "
        "delta_action=HYDRATE_SELECTED_DOCUMENTS"
    ]
