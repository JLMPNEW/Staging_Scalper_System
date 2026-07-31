from __future__ import annotations

import hashlib
import json
from pathlib import Path

from dedicated_parser.contracts import file_sha256
from industrials.transportation.primary_document_hydration import (
    DomainThrottle,
    FetchPayload,
    build_document_results,
    hydration_request_ids_sha256,
    hydrate_one_request,
    hydrate_requests,
    summarize_document_results,
    validate_hydration_resume_progress,
)


def _request(
    request_id: str,
    *,
    url: str,
    source_domain: str = "ir.example.com",
) -> dict[str, str]:
    return {
        "hydration_request_id": request_id,
        "retrieval_identity_type": "CANONICAL_URL",
        "retrieval_identity_sha256": hashlib.sha256(
            url.encode("utf-8")
        ).hexdigest(),
        "retrieval_url": url,
        "canonical_url": url,
        "source_content_digest": "",
        "source_content_digest_algorithm": "",
        "fanout_document_count": "1",
        "fanout_ticker_count": "1",
        "fanout_tickers": "TST",
        "source_domains": source_domain,
        "document_types": "ANNUAL_REPORT",
        "applicable_parser_metric_count": "2",
        "applicable_parser_metric_ids": "unit_cost|passenger_yield",
        "applicable_supporting_metric_count": "1",
        "applicable_supporting_metric_ids": "airline_capacity_units",
        "parse_all_applicable_metrics": "1",
        "hydration_status": "PLANNED_NOT_AUTHORIZED",
        "retrieval_authorized": "0",
        "parser_execution_authorized": "0",
    }


def _fetch_payload(
    *,
    url: str,
    payload: bytes,
    status: int = 200,
    final_url: str = "",
    content_type: str = "application/pdf",
) -> FetchPayload:
    return FetchPayload(
        http_status=status,
        final_url=final_url or url,
        content_type=content_type,
        payload=payload,
        attempt_count=1,
        network_request_count=1,
        error_class="" if status == 200 else f"HTTP_{status}",
        error="" if status == 200 else f"HTTP {status}",
    )


def test_hydration_is_content_addressed_and_resume_safe(
    tmp_path: Path,
) -> None:
    request = _request(
        "trnhyd_test_resume",
        url="https://ir.example.com/annual-report.pdf",
    )
    calls: list[str] = []

    def fetch(url: str, **_: object) -> FetchPayload:
        calls.append(url)
        return _fetch_payload(
            url=url,
            payload=b"%PDF-1.7\nprimary issuer report",
        )

    first = hydrate_one_request(
        request,
        execute=True,
        cache_root=tmp_path / "cache",
        request_manifest_sha256="sealed-request-manifest",
        user_agent="test",
        timeout_sec=1.0,
        max_retries=1,
        max_bytes=1_000_000,
        throttle=DomainThrottle(0.0),
        retry_failures=False,
        fetch=fetch,
    )
    second = hydrate_one_request(
        request,
        execute=True,
        cache_root=tmp_path / "cache",
        request_manifest_sha256="sealed-request-manifest",
        user_agent="test",
        timeout_sec=1.0,
        max_retries=1,
        max_bytes=1_000_000,
        throttle=DomainThrottle(0.0),
        retry_failures=False,
        fetch=fetch,
    )
    assert calls == [request["retrieval_url"]]
    assert first.status == "HYDRATED"
    assert first.content_ready is True
    assert second.status == "CACHE_HIT_VALID"
    assert second.network_request_count == 0
    assert first.content_sha256 == second.content_sha256
    assert Path(first.content_cache_path).is_file()


def test_failed_request_is_sealed_until_explicit_retry(
    tmp_path: Path,
) -> None:
    request = _request(
        "trnhyd_test_retry",
        url="https://ir.example.com/missing.pdf",
    )
    states = [404, 200]
    calls = 0

    def fetch(url: str, **_: object) -> FetchPayload:
        nonlocal calls
        status = states[min(calls, len(states) - 1)]
        calls += 1
        return _fetch_payload(
            url=url,
            payload=(
                b"%PDF-1.7\nrecovered" if status == 200 else b""
            ),
            status=status,
        )

    first = hydrate_one_request(
        request,
        execute=True,
        cache_root=tmp_path / "cache",
        request_manifest_sha256="sealed",
        user_agent="test",
        timeout_sec=1.0,
        max_retries=1,
        max_bytes=1_000_000,
        throttle=DomainThrottle(0.0),
        retry_failures=False,
        fetch=fetch,
    )
    second = hydrate_one_request(
        request,
        execute=True,
        cache_root=tmp_path / "cache",
        request_manifest_sha256="sealed",
        user_agent="test",
        timeout_sec=1.0,
        max_retries=1,
        max_bytes=1_000_000,
        throttle=DomainThrottle(0.0),
        retry_failures=False,
        fetch=fetch,
    )
    recovered = hydrate_one_request(
        request,
        execute=True,
        cache_root=tmp_path / "cache",
        request_manifest_sha256="sealed",
        user_agent="test",
        timeout_sec=1.0,
        max_retries=1,
        max_bytes=1_000_000,
        throttle=DomainThrottle(0.0),
        retry_failures=True,
        fetch=fetch,
    )
    assert first.status == "FAILED"
    assert second.status == "FAILED"
    assert calls == 2
    assert recovered.status == "HYDRATED"
    assert recovered.content_ready is True


def test_unreviewed_redirect_is_quarantined_without_losing_bytes(
    tmp_path: Path,
) -> None:
    request = _request(
        "trnhyd_test_redirect",
        url="https://ir.example.com/annual-report.pdf",
    )

    def fetch(url: str, **_: object) -> FetchPayload:
        return _fetch_payload(
            url=url,
            final_url="https://unknown-cdn.example/report.pdf",
            payload=b"%PDF-1.7\nissuer report",
        )

    outcome = hydrate_one_request(
        request,
        execute=True,
        cache_root=tmp_path / "cache",
        request_manifest_sha256="sealed",
        user_agent="test",
        timeout_sec=1.0,
        max_retries=1,
        max_bytes=1_000_000,
        throttle=DomainThrottle(0.0),
        retry_failures=False,
        fetch=fetch,
    )
    assert outcome.status == (
        "QUARANTINED_REDIRECT_DOMAIN_REVIEW_REQUIRED"
    )
    assert outcome.content_ready is False
    assert outcome.error_class == "UNREVIEWED_REDIRECT_DOMAIN"
    assert Path(outcome.content_cache_path).is_file()


def test_reviewed_redirect_policy_reuses_quarantined_bytes_without_network(
    tmp_path: Path,
) -> None:
    calls = 0

    def fetch(url: str, **_: object) -> FetchPayload:
        nonlocal calls
        calls += 1
        return _fetch_payload(
            url=url,
            final_url="https://issuer-cdn.example/report.pdf",
            payload=b"%PDF-1.7\nissuer report",
        )

    approved = _request(
        "trnhyd_redirect_approved",
        url="https://ir.example.com/approved.pdf",
    )
    initial = hydrate_one_request(
        approved,
        execute=True,
        cache_root=tmp_path / "cache",
        request_manifest_sha256="sealed",
        user_agent="test",
        timeout_sec=1.0,
        max_retries=1,
        max_bytes=1_000_000,
        throttle=DomainThrottle(0.0),
        retry_failures=False,
        fetch=fetch,
    )
    approved["approved_redirect_domains"] = "issuer-cdn.example"
    reviewed = hydrate_one_request(
        approved,
        execute=True,
        cache_root=tmp_path / "cache",
        request_manifest_sha256="sealed",
        user_agent="test",
        timeout_sec=1.0,
        max_retries=1,
        max_bytes=1_000_000,
        throttle=DomainThrottle(0.0),
        retry_failures=False,
        fetch=fetch,
    )

    excluded = _request(
        "trnhyd_redirect_excluded",
        url="https://ir.example.com/excluded.pdf",
    )
    hydrate_one_request(
        excluded,
        execute=True,
        cache_root=tmp_path / "cache",
        request_manifest_sha256="sealed",
        user_agent="test",
        timeout_sec=1.0,
        max_retries=1,
        max_bytes=1_000_000,
        throttle=DomainThrottle(0.0),
        retry_failures=False,
        fetch=fetch,
    )
    excluded["excluded_redirect_domains"] = "issuer-cdn.example"
    rejected = hydrate_one_request(
        excluded,
        execute=True,
        cache_root=tmp_path / "cache",
        request_manifest_sha256="sealed",
        user_agent="test",
        timeout_sec=1.0,
        max_retries=1,
        max_bytes=1_000_000,
        throttle=DomainThrottle(0.0),
        retry_failures=False,
        fetch=fetch,
    )

    assert initial.status == (
        "QUARANTINED_REDIRECT_DOMAIN_REVIEW_REQUIRED"
    )
    assert reviewed.status == "CACHE_HIT_REDIRECT_POLICY_APPROVED"
    assert reviewed.content_ready is True
    assert reviewed.network_request_count == 0
    assert rejected.status == "EXCLUDED_AFTER_DP6R_REDIRECT_REVIEW"
    assert rejected.content_ready is False
    assert rejected.network_request_count == 0
    assert calls == 2


def _reviewed_document(
    *,
    document_id: str,
    request_id: str = "",
    cached_path: Path | None = None,
    cached_sha256: str = "",
) -> dict[str, str]:
    cached = cached_path is not None
    return {
        "document_id": document_id,
        "ticker": "TST",
        "endpoint_id": "endpoint-tst",
        "document_type": "ANNUAL_REPORT",
        "published_date_hint": "2025-12-31",
        "source_domain": "ir.example.com",
        "canonical_url": f"https://ir.example.com/{document_id}.pdf",
        "retrieval_url": f"https://ir.example.com/{document_id}.pdf",
        "include_in_hydration": "1",
        "hydration_request_id": request_id,
        "cache_reuse_available": "1" if cached else "0",
        "content_cache_path": str(cached_path or ""),
        "content_sha256": cached_sha256,
        "content_bytes": (
            str(cached_path.stat().st_size) if cached_path else "0"
        ),
        "content_type": "application/pdf" if cached else "",
        "source_authority_class": "ISSUER_CONTROLLED_DOMAIN",
        "review_evidence_label": "fact_source_reported",
        "applicable_parser_metric_count": "2",
        "applicable_parser_metric_ids": "unit_cost|passenger_yield",
        "applicable_supporting_metric_count": "1",
        "applicable_supporting_metric_ids": "airline_capacity_units",
        "parse_all_applicable_metrics": "1",
    }


def test_full_request_fanout_builds_deduplicated_content_catalog(
    tmp_path: Path,
) -> None:
    request_manifest = tmp_path / "requests.csv"
    request_manifest.write_text("sealed requests\n", encoding="utf-8")
    source_manifest = tmp_path / "review.json"
    source_manifest.write_text('{"sealed": true}\n', encoding="utf-8")
    requests = [
        _request(
            "trnhyd_a",
            url="https://ir.example.com/a.pdf",
        ),
        _request(
            "trnhyd_b",
            url="https://ir.example.com/b.pdf",
        ),
    ]

    def fetch(url: str, **_: object) -> FetchPayload:
        return _fetch_payload(
            url=url,
            payload=b"%PDF-1.7\nidentical issuer content",
        )

    request_results, request_summary = hydrate_requests(
        requests,
        execute=True,
        cache_root=tmp_path / "cache",
        request_manifest_path=request_manifest,
        source_manifest_paths=(source_manifest, request_manifest),
        progress_path=tmp_path / "progress.json",
        user_agent="test",
        timeout_sec=1.0,
        max_retries=1,
        request_spacing_sec=0.0,
        max_bytes=1_000_000,
        workers=2,
        progress_every=1,
        batch_size=1,
        batch_pause_sec=0.0,
        fetch=fetch,
    )
    cached_payload = b"%PDF-1.7\nprefetched body"
    cached_path = tmp_path / "discovery-cache.bin"
    cached_path.write_bytes(cached_payload)
    reviewed = [
        _reviewed_document(
            document_id="doc-a",
            request_id="trnhyd_a",
        ),
        _reviewed_document(
            document_id="doc-b",
            request_id="trnhyd_b",
        ),
        _reviewed_document(
            document_id="doc-cached",
            cached_path=cached_path,
            cached_sha256=hashlib.sha256(cached_payload).hexdigest(),
        ),
    ]
    document_rows, catalog, errors = build_document_results(
        reviewed_documents=reviewed,
        request_results=request_results,
        cache_root=tmp_path / "cache",
        require_complete_requests=True,
    )
    assert errors == []
    assert request_summary["source_artifacts_unchanged"] is True
    assert request_summary["content_ready_request_count"] == 2
    assert request_summary["batch_count"] == 2
    assert len(catalog) == 2
    summary = summarize_document_results(
        document_rows=document_rows,
        content_rows=catalog,
    )
    assert summary["content_ready_document_count"] == 3
    assert summary["unique_content_sha256_count"] == 2
    assert summary["content_level_document_deduplication_savings"] == 1
    assert all(
        row["parser_execution_authorized"] == 0
        for row in document_rows
    )
    progress = json.loads(
        (tmp_path / "progress.json").read_text(encoding="utf-8")
    )
    assert progress["selection_start_offset"] == 0
    assert progress["selection_total_count"] == 2
    assert progress["selection_completed_count"] == 2
    assert progress["selection_remaining_count"] == 0
    assert progress["selection_request_id_sha256"] == (
        hydration_request_ids_sha256(requests)
    )


def test_legacy_cooldown_checkpoint_resumes_at_exact_batch_boundary(
    tmp_path: Path,
) -> None:
    request_manifest = tmp_path / "requests.csv"
    request_manifest.write_text("sealed requests\n", encoding="utf-8")
    selection = [
        _request(
            f"trnhyd_resume_{index}",
            url=f"https://ir.example.com/{index}.pdf",
        )
        for index in range(6)
    ]
    progress_path = tmp_path / "legacy-progress.json"
    progress = {
        "hydration_version": (
            "transportation_dp6r_primary_document_hydration_v1"
        ),
        "execute": True,
        "request_manifest_path": str(request_manifest.resolve()),
        "request_manifest_sha256": file_sha256(request_manifest),
        "planned_request_count": 6,
        "completed_count": 4,
        "remaining_count": 2,
        "status_counts": {"FAILED": 2, "HYDRATED": 2},
        "parser_invocations": 0,
        "phase": "COOLDOWN",
        "batch_size": 2,
        "batch_number": 2,
        "batch_count": 3,
    }
    progress_path.write_text(
        json.dumps(progress),
        encoding="utf-8",
    )

    validated = validate_hydration_resume_progress(
        progress,
        progress_path=progress_path,
        request_manifest_path=request_manifest,
        request_manifest_sha256=file_sha256(request_manifest),
        full_selection=selection,
        expected_batch_size=2,
    )

    assert validated["validated"] is True
    assert validated["legacy_checkpoint"] is True
    assert validated["next_selection_offset"] == 4
    assert validated["remaining_selection_count"] == 2
    assert validated["selection_request_id_sha256"] == (
        hydration_request_ids_sha256(selection)
    )


def test_resume_checkpoint_rejects_non_boundary_or_changed_manifest(
    tmp_path: Path,
) -> None:
    request_manifest = tmp_path / "requests.csv"
    request_manifest.write_text("sealed requests\n", encoding="utf-8")
    selection = [
        _request(
            f"trnhyd_reject_{index}",
            url=f"https://ir.example.com/{index}.pdf",
        )
        for index in range(4)
    ]
    progress_path = tmp_path / "bad-progress.json"
    progress = {
        "hydration_version": (
            "transportation_dp6r_primary_document_hydration_v1"
        ),
        "execute": True,
        "request_manifest_path": str(request_manifest.resolve()),
        "request_manifest_sha256": "changed",
        "planned_request_count": 4,
        "completed_count": 2,
        "remaining_count": 2,
        "status_counts": {"HYDRATED": 2},
        "parser_invocations": 0,
        "phase": "RUNNING",
        "batch_size": 2,
        "batch_number": 1,
        "batch_count": 2,
    }
    progress_path.write_text(
        json.dumps(progress),
        encoding="utf-8",
    )

    try:
        validate_hydration_resume_progress(
            progress,
            progress_path=progress_path,
            request_manifest_path=request_manifest,
            request_manifest_sha256=file_sha256(request_manifest),
            full_selection=selection,
            expected_batch_size=2,
        )
    except ValueError as exc:
        message = str(exc)
    else:
        raise AssertionError("Invalid resume checkpoint was accepted")

    assert "checkpoint is not at a completed batch boundary" in message
    assert "request manifest hash does not match" in message
