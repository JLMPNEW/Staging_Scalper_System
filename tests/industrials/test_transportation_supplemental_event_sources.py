from __future__ import annotations

import json

from industrials.transportation.supplemental_event_sources import (
    audit_cached_event_sources,
    audit_patterns,
    selected_documents,
)


def test_selected_documents_are_limited_to_primary_form_and_ex99(tmp_path) -> None:
    accession = tmp_path / "accession"
    accession.mkdir()
    (accession / "index.json").write_text(
        json.dumps({
            "directory": {
                "item": [
                    {"name": "primary.htm", "type": "8-K"},
                    {"name": "earnings-ex991.htm", "type": "EX-99.1"},
                    {"name": "graphic.jpg", "type": "GRAPHIC"},
                    {"name": "ownership.xml", "type": "XML"},
                ]
            }
        }),
        encoding="utf-8",
    )

    assert selected_documents(accession, form_type="8-K") == (
        "earnings-ex991.htm",
        "primary.htm",
    )


def test_one_cached_document_is_scanned_for_all_metric_families(tmp_path) -> None:
    cache = tmp_path / "cache"
    accession = (
        cache
        / "sec_archive_xbrl"
        / "CIK1000"
        / "000000100026000001"
    )
    accession.mkdir(parents=True)
    (accession / "index.json").write_text(
        json.dumps({
            "directory": {
                "item": [
                    {"name": "earnings-ex991.htm", "type": "EX-99.1"}
                ]
            }
        }),
        encoding="utf-8",
    )
    (accession / "earnings-ex991.htm").write_text(
        "<table><tr><td>Pounds per day</td><td>1,200,000</td></tr>"
        "<tr><td>Shipments per day</td><td>1,000</td></tr>"
        "<tr><td>Operating ratio</td><td>88.0%</td></tr></table>",
        encoding="utf-8",
    )
    decisions = [{
        "ticker": "ODFL",
        "cik": "1000",
        "accession_number": "0000001000-26-000001",
        "form_type": "8-K",
        "filing_date": "2026-02-01",
        "candidate_type": "supplemental_event",
    }]
    patterns = audit_patterns(
        {
            "freight_weight_per_shipment": ("weight per shipment",),
            "operating_ratio": ("operating ratio",),
        },
        ("freight_weight_per_shipment", "operating_ratio"),
    )

    rows, summary = audit_cached_event_sources(
        decision_rows=decisions,
        cache_dir=cache,
        patterns=patterns,
    )

    assert len(rows) == 1
    assert rows[0]["matched_metric_ids"] == "freight_weight_per_shipment|operating_ratio"
    assert summary["scanned_document_count"] == 1
    assert summary["positive_accession_count"] == 1

