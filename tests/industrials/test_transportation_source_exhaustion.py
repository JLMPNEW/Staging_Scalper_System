from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

from industrials.transportation.source_exhaustion import (
    SOURCE_EXHAUSTION_VERSION,
    _delta_action,
    _index_metadata,
    build_source_exhaustion,
    classify_source,
    load_submission_inventory,
    validate_written_source_exhaustion,
    write_source_exhaustion,
)
from industrials.transportation.source_exhaustion_hydration import (
    build_hydration_requests,
    hydrate_metadata,
    read_csv,
    validate_sealed_csv_artifact,
)
from dedicated_parser.contracts import file_sha256


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, sort_keys=True),
        encoding="utf-8",
    )


def _recent_block(
    *,
    accession: str,
    filing_date: str,
    form: str,
    items: str = "",
    primary_document: str = "primary.htm",
    description: str = "",
) -> dict[str, list[object]]:
    return {
        "accessionNumber": [accession],
        "filingDate": [filing_date],
        "reportDate": [filing_date],
        "acceptanceDateTime": [f"{filing_date}T16:00:00.000Z"],
        "form": [form],
        "items": [items],
        "primaryDocument": [primary_document],
        "primaryDocDescription": [description],
    }


def test_submission_inventory_reports_only_overlapping_missing_shards(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "sec_submissions"
    _write_json(
        cache / "CIK0000000001.json",
        {
            "filings": {
                "recent": _recent_block(
                    accession="0000000001-20-000001",
                    filing_date="2020-01-02",
                    form="8-K",
                    items="2.02,9.01",
                ),
                "files": [
                    {
                        "name": "CIK0000000001-submissions-001.json",
                        "filingFrom": "2018-01-01",
                        "filingTo": "2019-12-31",
                    },
                    {
                        "name": "CIK0000000001-submissions-002.json",
                        "filingFrom": "2000-01-01",
                        "filingTo": "2005-12-31",
                    },
                ],
            }
        },
    )
    rows, gaps, errors, summary = load_submission_inventory(
        cache,
        members={
            "TST": {
                "cik": "1",
                "universe_role": "active",
                "membership_end_date": "",
            }
        },
        active_start_date="2017-11-28",
        inactive_start_date="2000-01-01",
        asof_date="2026-07-22",
    )
    assert errors == []
    assert [row["accession_number"] for row in rows] == [
        "0000000001-20-000001"
    ]
    assert [row["source_file"] for row in gaps] == [
        "CIK0000000001-submissions-001.json"
    ]
    assert summary["referenced_overlapping_history_file_count"] == 1
    assert summary["missing_overlapping_history_file_count"] == 1


def test_source_classification_prioritizes_results_and_foreign_reports() -> None:
    assert classify_source(
        form_type="8-K",
        items="2.02, 9.01",
        primary_description="Form 8-K",
        development_overlay=False,
        registration_window=False,
    ) == (
        "DOMESTIC_RESULTS_EVENT",
        1,
        "RESULTS_ITEM_2_02_OR_7_01",
    )
    assert classify_source(
        form_type="6-K",
        items="",
        primary_description="Report of foreign private issuer",
        development_overlay=False,
        registration_window=False,
    ) == (
        "FOREIGN_RESULTS_EVENT",
        2,
        "FOREIGN_REPORT_REQUIRES_INDEX_METADATA",
    )
    assert classify_source(
        form_type="20-F",
        items="",
        primary_description="Annual report",
        development_overlay=False,
        registration_window=False,
    ) == (
        "PERIODIC_ANNUAL_REPORT",
        1,
        "PRIMARY_ANNUAL_STATEMENT",
    )
    assert classify_source(
        form_type="10-Q",
        items="",
        primary_description="Quarterly report",
        development_overlay=False,
        registration_window=False,
    ) == (
        "PERIODIC_INTERIM_REPORT",
        1,
        "PRIMARY_INTERIM_STATEMENT",
    )
    assert classify_source(
        form_type="424B5",
        items="",
        primary_description="Prospectus supplement",
        development_overlay=True,
        registration_window=False,
    ) == (
        "DEVELOPMENT_REGISTRATION",
        3,
        "DEVELOPMENT_STAGE_PROSPECTUS",
    )


def test_generic_foreign_report_keeps_primary_and_pdf_documents(
    tmp_path: Path,
) -> None:
    accession = "0000000001-24-000001"
    accession_dir = (
        tmp_path
        / "sec_archive_xbrl"
        / "CIK0000000001"
        / accession.replace("-", "")
    )
    _write_json(
        accession_dir / "index.json",
        {
            "directory": {
                "item": [
                    {"name": "opaque-primary.htm"},
                    {"name": "opaque-deck.pdf"},
                    {"name": "unrelated.htm"},
                    {"name": f"{accession}.txt"},
                ]
            }
        },
    )
    metadata = _index_metadata(
        cache_dir=tmp_path,
        cik="0000000001",
        accession_number=accession,
        primary_document="opaque-primary.htm",
        target_metrics=(),
        aliases={},
        source_category="FOREIGN_RESULTS_EVENT",
    )
    assert metadata["selected_document_names"] == (
        "opaque-deck.pdf",
        "opaque-primary.htm",
    )
    fallback = _index_metadata(
        cache_dir=tmp_path,
        cik="0000000001",
        accession_number=accession,
        primary_document="",
        target_metrics=(),
        aliases={},
        source_category="PERIODIC_INTERIM_REPORT",
    )
    assert fallback["selected_document_names"] == (
        f"{accession}.txt",
        "opaque-deck.pdf",
    )
    numeric_text_fallback = _index_metadata(
        cache_dir=tmp_path,
        cik="0000000001",
        accession_number=accession,
        primary_document="0001.txt",
        target_metrics=(),
        aliases={},
        source_category="PERIODIC_ANNUAL_REPORT",
    )
    assert numeric_text_fallback["selected_document_names"] == (
        f"{accession}.txt",
        "opaque-deck.pdf",
    )
    numeric_html_fallback = _index_metadata(
        cache_dir=tmp_path,
        cik="0000000001",
        accession_number=accession,
        primary_document="0001.htm",
        target_metrics=(),
        aliases={},
        source_category="PERIODIC_INTERIM_REPORT",
    )
    assert numeric_html_fallback["selected_document_names"] == (
        f"{accession}.txt",
        "opaque-deck.pdf",
    )
    assert _delta_action(
        priority=2,
        dp3_decision="",
        index_status="CACHED",
        selected_document_count=0,
        cached_selected_document_count=0,
        source_category="ANNUAL_REPORT_EXHIBIT",
        primary_document=f"{accession}.paper",
    )[0] == "NO_DELTA_NON_ELECTRONIC_PAPER_FILING"


def test_source_exhaustion_detects_registry_gap_and_remains_read_only(
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "cache"
    submissions = cache_dir / "sec_submissions"
    _write_json(
        submissions / "CIK0000000001.json",
        {
            "filings": {
                "recent": _recent_block(
                    accession="0000000001-24-000001",
                    filing_date="2024-02-01",
                    form="8-K",
                    items="2.02,7.01,9.01",
                    description="Results of Operations",
                ),
                "files": [],
            }
        },
    )
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE fact_sec_filing(
            ticker TEXT,
            accession_number TEXT,
            form_type TEXT,
            source_id TEXT
        )
        """
    )
    acceptance = {
        "metric_id": "operating_ratio",
        "metric_pack": "surface",
        "source_lane": "DP",
        "metric_disposition": "CALIBRATION_CANDIDATE",
        "post_active_accepted_count": "0",
        "post_active_usable_count": "1",
        "broad_required_count": "1",
        "broad_accepted_shortfall": "1",
        "best_accepted_niche_shortfall": "1",
        "accepted_breadth_gate_pass": "0",
        "historical_depth_gate_pass": "0",
    }
    (
        filing_rows,
        delta_rows,
        gaps,
        form_rows,
        metric_rows,
        errors,
        summary,
    ) = build_source_exhaustion(
        connection,
        members={
            "TST": {
                "ticker": "TST",
                "company_name": "Test Transport",
                "cik": "1",
                "industry": "Transportation",
                "calibration_cohort": "surface",
                "universe_role": "active",
                "membership_start_date": "2019-01-02",
                "membership_end_date": "",
            }
        },
        submissions_cache_dir=submissions,
        cache_dir=cache_dir,
        scope_rows=[
            {
                "ticker": "TST",
                "metric_id": "operating_ratio",
                "metric_pack": "surface",
                "applicability_status": "APPLICABLE",
                "development_overlay": "0",
            }
        ],
        metric_acceptance_rows=[acceptance],
        dp3_decisions=[],
        metric_aliases={"operating_ratio": ("operating ratio",)},
        registration_anchors={"TST": date(2024, 1, 1)},
        source_id="sec_submissions",
        active_start_date="2017-11-28",
        inactive_start_date="2000-01-01",
        asof_date="2026-07-22",
        expected_identity_count=1,
    )
    assert errors == []
    assert gaps == []
    assert len(filing_rows) == 1
    assert len(delta_rows) == 1
    assert delta_rows[0]["database_registry_gap"] == 1
    assert delta_rows[0]["delta_action"] == "HYDRATE_INDEX_ONLY"
    assert delta_rows[0]["target_metric_ids"] == "operating_ratio"
    assert form_rows[0]["database_registry_gap_count"] == 1
    assert metric_rows[0]["candidate_filing_count"] == 1
    assert summary["database_registry_gap_count"] == 1

    output_dir = tmp_path / "artifacts"
    manifest = write_source_exhaustion(
        filing_rows=filing_rows,
        delta_rows=delta_rows,
        gap_rows=gaps,
        form_rows=form_rows,
        metric_rows=metric_rows,
        summary={**summary, "errors": errors},
        input_artifacts={},
        output_dir=output_dir,
    )
    assert manifest["acceptance"] == "PASS_WITH_REQUIRED_DELTA"
    assert manifest["network_requests"] == 0
    assert manifest["parser_execution_authorized"] is False
    assert manifest["historical_materialization_authorized"] is False
    assert (
        validate_written_source_exhaustion(
            output_dir=output_dir,
            expected_identity_count=1,
            expected_metric_count=1,
        )
        == []
    )
    assert (
        manifest["manifest_version"]
        == SOURCE_EXHAUSTION_VERSION
    )


def test_metadata_hydration_is_resumable_and_cache_only(
    tmp_path: Path,
) -> None:
    source_manifest = tmp_path / "source_manifest.json"
    _write_json(source_manifest, {"acceptance": "PASS_WITH_REQUIRED_DELTA"})
    cache = tmp_path / "cache"
    requests = build_hydration_requests(
        gap_rows=[
            {
                "ticker": "TST",
                "cik": "0000000001",
                "source_file": "CIK0000000001-submissions-001.json",
                "required_action": "HYDRATE_SUBMISSIONS_HISTORY",
            }
        ],
        delta_rows=[],
        submissions_cache_dir=cache / "sec_submissions",
        archive_cache_dir=cache / "sec_archive_xbrl",
        phase="submissions",
    )
    calls: list[str] = []

    def fetch(
        url: str,
        *,
        user_agent: str,
        timeout_sec: float,
    ) -> tuple[int, str]:
        assert user_agent == "test@example.com"
        assert timeout_sec == 1.0
        calls.append(url)
        return 200, json.dumps(
            _recent_block(
                accession="0000000001-18-000001",
                filing_date="2018-01-01",
                form="8-K",
            )
        )

    results, summary = hydrate_metadata(
        requests,
        execute=True,
        user_agent="test@example.com",
        timeout_sec=1.0,
        max_retries=1,
        request_spacing_sec=0.0,
        progress_path=tmp_path / "progress.json",
        source_manifest_path=source_manifest,
        fetch=fetch,
    )
    assert len(calls) == 1
    assert results[0]["status"] == "HYDRATED"
    assert summary["database_writes"] == 0
    assert summary["parser_invocations"] == 0

    second_results, second_summary = hydrate_metadata(
        requests,
        execute=True,
        user_agent="test@example.com",
        timeout_sec=1.0,
        max_retries=1,
        request_spacing_sec=0.0,
        progress_path=tmp_path / "progress_second.json",
        source_manifest_path=source_manifest,
        fetch=fetch,
    )
    assert len(calls) == 1
    assert second_results[0]["status"] == "CACHE_HIT_VALID"
    assert second_summary["network_requests"] == 0
    assert second_summary["cache_hits"] == 1


def test_document_hydration_uses_only_sealed_missing_documents(
    tmp_path: Path,
) -> None:
    source_manifest = tmp_path / "source_manifest.json"
    _write_json(
        source_manifest,
        {"delta_document_manifest_ready": True},
    )
    archive = tmp_path / "sec_archive_xbrl"
    existing = (
        archive
        / "CIK0000000001"
        / "000000000124000001"
        / "release.htm"
    )
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text("<html>cached</html>", encoding="utf-8")
    requests = build_hydration_requests(
        gap_rows=[],
        delta_rows=[
            {
                "ticker": "TST",
                "cik": "0000000001",
                "accession_number": "0000000001-24-000001",
                "form_type": "8-K",
                "candidate_priority": "1",
                "delta_action": "HYDRATE_SELECTED_DOCUMENTS",
                "selected_document_names": "release.htm|deck.pdf",
            }
        ],
        submissions_cache_dir=tmp_path / "sec_submissions",
        archive_cache_dir=archive,
        phase="documents",
    )
    assert [request.cache_path.name for request in requests] == [
        "deck.pdf"
    ]

    def fetch(
        url: str,
        *,
        user_agent: str,
        timeout_sec: float,
    ) -> tuple[int, bytes]:
        assert url.endswith("/deck.pdf")
        return 200, b"%PDF-1.4\ntransportation"

    results, summary = hydrate_metadata(
        requests,
        execute=True,
        user_agent="test@example.com",
        timeout_sec=1.0,
        max_retries=1,
        request_spacing_sec=0.0,
        progress_path=tmp_path / "document_progress.json",
        source_manifest_path=source_manifest,
        workers=2,
        fetch=fetch,
    )
    assert results[0]["status"] == "HYDRATED"
    assert Path(str(results[0]["cache_path"])).read_bytes().startswith(
        b"%PDF-"
    )
    assert summary["database_writes"] == 0
    assert summary["parser_invocations"] == 0


def test_hydration_rejects_a_changed_source_artifact(tmp_path: Path) -> None:
    delta_path = tmp_path / "delta.csv"
    delta_path.write_text("ticker,delta_action\nTST,HYDRATE_INDEX_ONLY\n")
    rows = read_csv(delta_path)
    source_manifest = {
        "artifacts": {
            "delta_candidates": {
                "path": str(delta_path.resolve()),
                "row_count": 1,
                "sha256": file_sha256(delta_path),
            }
        }
    }
    assert (
        validate_sealed_csv_artifact(
            source_manifest=source_manifest,
            artifact_name="delta_candidates",
            path=delta_path,
            rows=rows,
        )
        == []
    )
    delta_path.write_text(
        "ticker,delta_action\nTST,HYDRATE_SELECTED_DOCUMENTS\n"
    )
    assert validate_sealed_csv_artifact(
        source_manifest=source_manifest,
        artifact_name="delta_candidates",
        path=delta_path,
        rows=read_csv(delta_path),
    ) == ["delta_candidates sha256 does not match the sealed manifest"]

    empty_path = tmp_path / "empty.csv"
    empty_path.write_text("ticker,required_action\n", encoding="utf-8")
    empty_manifest = {
        "artifacts": {
            "source_gaps": {
                "path": str(empty_path.resolve()),
                "row_count": 0,
                "sha256": file_sha256(empty_path),
            }
        }
    }
    assert validate_sealed_csv_artifact(
        source_manifest=empty_manifest,
        artifact_name="source_gaps",
        path=empty_path,
        rows=read_csv(empty_path),
    ) == []


def test_hydration_fails_if_manifest_changes_during_run(
    tmp_path: Path,
) -> None:
    source_manifest = tmp_path / "source_manifest.json"
    _write_json(source_manifest, {"sealed": True})
    request = build_hydration_requests(
        gap_rows=[
            {
                "ticker": "TST",
                "cik": "0000000001",
                "source_file": "CIK0000000001-submissions-001.json",
                "required_action": "HYDRATE_SUBMISSIONS_HISTORY",
            }
        ],
        delta_rows=[],
        submissions_cache_dir=tmp_path / "sec_submissions",
        archive_cache_dir=tmp_path / "sec_archive_xbrl",
        phase="submissions",
    )

    def fetch(
        url: str,
        *,
        user_agent: str,
        timeout_sec: float,
    ) -> tuple[int, str]:
        _write_json(source_manifest, {"sealed": False})
        return 200, "{}"

    _, summary = hydrate_metadata(
        request,
        execute=True,
        user_agent="test@example.com",
        timeout_sec=1.0,
        max_retries=1,
        request_spacing_sec=0.0,
        progress_path=tmp_path / "progress.json",
        source_manifest_path=source_manifest,
        fetch=fetch,
    )
    assert summary["acceptance"] == "FAIL"
    assert summary["source_manifest_unchanged"] is False
    assert summary["seal_error_count"] == 1
