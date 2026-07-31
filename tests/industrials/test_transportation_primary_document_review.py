from __future__ import annotations

from industrials.transportation.primary_document_review import (
    APPROVED_EXTERNAL_DISPOSITION,
    EXCLUDED_EXTERNAL_DISPOSITION,
    build_endpoint_review_rows,
    build_external_domain_adjudications,
    build_reviewed_document_and_hydration_rows,
    summarize_review,
)


def _endpoint(
    *,
    ticker: str,
    status: str,
    candidates: int,
    failed: int,
    fallback: str = "",
) -> dict[str, object]:
    reviewed_access = status.startswith("REVIEWED_")
    return {
        "endpoint_id": f"endpoint-{ticker.lower()}",
        "ticker": ticker,
        "endpoint_type": "ISSUER_WEBSITE_DISCOVERY_ROOT",
        "universe_role": "active",
        "endpoint_status": status,
        "required_pair_count": "4",
        "discovery_page_count": str(max(1, failed + 1)),
        "ready_discovery_page_count": "1" if not reviewed_access else "0",
        "failed_discovery_page_count": str(failed),
        "candidate_document_count": str(candidates),
        "external_asset_document_count": "0",
        "archive_digest_document_count": "0",
        "root_repair_review_status": (
            "APPROVED" if reviewed_access else ""
        ),
        "root_repair_unresolved_disposition": (
            status if reviewed_access else ""
        ),
        "root_repair_fallback_source_lane": fallback,
    }


def test_endpoint_review_seals_every_exception_without_execution() -> None:
    endpoints = [
        _endpoint(
            ticker="AAA",
            status="ENUMERATED_AND_URL_DEDUPLICATED",
            candidates=3,
            failed=0,
        ),
        _endpoint(
            ticker="BBB",
            status="ENUMERATED_WITH_PARTIAL_DISCOVERY_FAILURES",
            candidates=2,
            failed=1,
        ),
        _endpoint(
            ticker="CCC",
            status="NO_PRIMARY_DOCUMENT_CANDIDATES_REVIEW_REQUIRED",
            candidates=0,
            failed=0,
        ),
        _endpoint(
            ticker="DDD",
            status="REVIEWED_PRIMARY_SITE_ACCESS_LIMITATION",
            candidates=0,
            failed=1,
            fallback="SEC_FILINGS_AND_ISSUER_IR",
        ),
        _endpoint(
            ticker="EEE",
            status="REVIEWED_PRIMARY_SITE_ACCESS_LIMITATION",
            candidates=1,
            failed=1,
            fallback="SEC_FILINGS_AND_ISSUER_IR",
        ),
    ]
    discovery = [
        {
            "endpoint_id": "endpoint-bbb",
            "page_status": "FAILED",
            "page_role": "SITEMAP",
            "http_status": "403",
            "error": "HTTP 403",
        },
        {
            "endpoint_id": "endpoint-ddd",
            "page_status": "FAILED",
            "page_role": "ROOT",
            "http_status": "0",
            "error": "ReadTimeout: request timed out",
        },
        {
            "endpoint_id": "endpoint-eee",
            "page_status": "FAILED",
            "page_role": "ROOT",
            "http_status": "0",
            "error": "ReadTimeout: request timed out",
        },
    ]
    rows, errors = build_endpoint_review_rows(
        endpoint_rows=endpoints,
        discovery_rows=discovery,
    )
    assert errors == []
    by_ticker = {str(row["ticker"]): row for row in rows}
    assert by_ticker["AAA"]["endpoint_disposition"] == (
        "ACCEPT_ENUMERATED_SET"
    )
    assert by_ticker["BBB"]["endpoint_disposition"] == (
        "ACCEPT_ENUMERATED_SET_WITH_BOUNDED_DISCOVERY_GAPS"
    )
    assert by_ticker["CCC"]["evidence_label"] == (
        "missing_required_source"
    )
    assert by_ticker["DDD"]["endpoint_disposition"] == (
        "RETAIN_ZERO_RESULT_AND_REQUIRE_DECLARED_FALLBACK_LANE"
    )
    assert by_ticker["EEE"]["endpoint_disposition"] == (
        "ACCEPT_ENUMERATED_SET_WITH_REVIEWED_ACCESS_LIMITATION"
    )
    assert all(row["review_status"] == "REVIEWED" for row in rows)
    assert all(row["retrieval_authorized"] == 0 for row in rows)
    assert all(row["parser_execution_authorized"] == 0 for row in rows)


def _external_policy(
    *,
    ticker: str,
    domain: str,
    disposition: str,
) -> dict[str, str]:
    return {
        "policy_version": "test",
        "ticker": ticker,
        "source_domain": domain,
        "review_status": "APPROVED",
        "review_disposition": disposition,
        "source_authority_class": "TEST_SOURCE_CLASS",
        "evidence_label": "analyst_interpretation",
        "decision_basis": "unit-test decision",
        "notes": "",
    }


def _external_row(
    *,
    ticker: str,
    domain: str,
    url: str,
) -> dict[str, object]:
    return {
        "ticker": ticker,
        "endpoint_id": f"endpoint-{ticker.lower()}",
        "source_domain": domain,
        "source_url": url,
        "discovered_from_url": f"https://ir.{ticker.lower()}.com/",
        "title": "Quarterly results",
        "document_type": "EARNINGS_OR_FINANCIAL_RESULTS",
        "domain_status": (
            "ISSUER_LINKED_EXTERNAL_ASSET_REVIEW_REQUIRED"
        ),
    }


def _document(
    *,
    document_id: str,
    ticker: str,
    domain: str,
    canonical_url: str,
    domain_status: str,
    content_sha256: str = "",
) -> dict[str, object]:
    return {
        "document_id": document_id,
        "ticker": ticker,
        "endpoint_id": f"endpoint-{ticker.lower()}",
        "endpoint_type": "ISSUER_WEBSITE_DISCOVERY_ROOT",
        "universe_role": "active",
        "source_id": "issuer_primary_site",
        "source_type": "primary_issuer_document",
        "source_rank": "3",
        "freshness_status": "acceptable_for_period",
        "document_type": "EARNINGS_OR_FINANCIAL_RESULTS",
        "title": "Quarterly results",
        "published_date_hint": "2025-05-01",
        "canonical_url": canonical_url,
        "retrieval_url": canonical_url,
        "source_domain": domain,
        "domain_status": domain_status,
        "candidate_source_lane_ids": "issuer_ir_earnings_release",
        "applicable_parser_metric_count": "2",
        "applicable_parser_metric_ids": "passenger_yield|unit_cost",
        "applicable_supporting_metric_count": "1",
        "applicable_supporting_metric_ids": "airline_capacity_units",
        "parse_all_applicable_metrics": "1",
        "url_identity_sha256": f"url-{document_id}",
        "source_content_digest": "",
        "source_content_digest_algorithm": "",
        "content_type": "application/pdf" if content_sha256 else "",
        "content_bytes": "100" if content_sha256 else "0",
        "content_cache_path": (
            f"cache/{document_id}.pdf" if content_sha256 else ""
        ),
        "content_sha256": content_sha256,
        "content_hash_status": (
            "CONTENT_SHA256_AVAILABLE"
            if content_sha256
            else "PENDING_ONE_TIME_DOCUMENT_HYDRATION"
        ),
    }


def test_external_review_freezes_deduplicated_hydration_plan() -> None:
    external_rows = [
        _external_row(
            ticker="AAA",
            domain="cdn.example.com",
            url="https://cdn.example.com/results.pdf",
        ),
        _external_row(
            ticker="BBB",
            domain="social.example",
            url="https://social.example/post",
        ),
    ]
    policy_rows = [
        _external_policy(
            ticker="AAA",
            domain="cdn.example.com",
            disposition=APPROVED_EXTERNAL_DISPOSITION,
        ),
        _external_policy(
            ticker="BBB",
            domain="social.example",
            disposition=EXCLUDED_EXTERNAL_DISPOSITION,
        ),
    ]
    adjudications, policy, errors = (
        build_external_domain_adjudications(
            external_rows=external_rows,
            policy_rows=policy_rows,
        )
    )
    assert errors == []
    assert [row["include_in_hydration"] for row in adjudications] == [
        1,
        0,
    ]

    shared_url = "https://issuer.example.com/shared-results.pdf"
    documents = [
        _document(
            document_id="doc-approved",
            ticker="AAA",
            domain="cdn.example.com",
            canonical_url="https://cdn.example.com/results.pdf",
            domain_status=(
                "ISSUER_LINKED_EXTERNAL_ASSET_REVIEW_REQUIRED"
            ),
        ),
        _document(
            document_id="doc-rejected",
            ticker="BBB",
            domain="social.example",
            canonical_url="https://social.example/post",
            domain_status=(
                "ISSUER_LINKED_EXTERNAL_ASSET_REVIEW_REQUIRED"
            ),
        ),
        _document(
            document_id="doc-shared-1",
            ticker="CCC",
            domain="issuer.example.com",
            canonical_url=shared_url,
            domain_status="ISSUER_CONTROLLED_DOMAIN",
        ),
        _document(
            document_id="doc-shared-2",
            ticker="DDD",
            domain="issuer.example.com",
            canonical_url=shared_url,
            domain_status="ISSUER_CONTROLLED_DOMAIN",
        ),
        _document(
            document_id="doc-cached",
            ticker="EEE",
            domain="issuer.example.com",
            canonical_url="https://issuer.example.com/cached.pdf",
            domain_status="ISSUER_CONTROLLED_DOMAIN",
            content_sha256="a" * 64,
        ),
    ]
    reviewed, requests, errors = (
        build_reviewed_document_and_hydration_rows(
            document_rows=documents,
            external_policy=policy,
        )
    )
    assert errors == []
    by_id = {str(row["document_id"]): row for row in reviewed}
    assert by_id["doc-rejected"]["include_in_hydration"] == 0
    assert by_id["doc-cached"]["cache_reuse_available"] == 1
    assert by_id["doc-cached"]["hydration_required"] == 0
    assert (
        by_id["doc-shared-1"]["hydration_request_id"]
        == by_id["doc-shared-2"]["hydration_request_id"]
    )
    assert len(requests) == 2
    assert sum(
        int(str(row["fanout_document_count"])) for row in requests
    ) == 3
    summary = summarize_review(
        endpoint_rows=[],
        external_rows=adjudications,
        document_rows=reviewed,
        hydration_rows=requests,
    )
    assert summary["hydration_included_document_count"] == 4
    assert summary["hydration_excluded_document_count"] == 1
    assert summary["discovery_cache_reuse_document_count"] == 1
    assert summary["hydration_required_document_count"] == 3
    assert summary["unique_hydration_request_count"] == 2
    assert summary["physical_request_deduplication_savings"] == 1
