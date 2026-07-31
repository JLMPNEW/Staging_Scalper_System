from __future__ import annotations

import json

from industrials.transportation.financial_repair_contract import (
    build_financial_repair_contracts,
)
from industrials.transportation.one_pass_preflight import (
    build_one_pass_preflight,
    summarize_one_pass_preflight,
)
from industrials.transportation.primary_document_enumeration import (
    build_archive_document_candidates,
    build_archive_queries,
    build_archive_year_queries,
    build_endpoint_enumeration_rows,
    build_live_document_candidates,
    canonicalize_url,
    classify_document,
    deduplicate_document_rows,
    is_navigation_url,
    parse_html_metadata,
)
from industrials.transportation.semantic_fixture_freeze import (
    build_semantic_metric_contracts,
    build_semantic_pair_contracts,
)


def test_semantic_freeze_preserves_review_required_evidence() -> None:
    metric_rows, errors = build_semantic_metric_contracts(
        final_metric_rows=[
            {
                "metric_id": "passenger_yield",
                "metric_pack": "air",
                "source_lane": "DP",
                "component": "operating_efficiency",
                "applicability_tags": "passenger_airline",
                "unit_contract": "currency_per_passenger_distance",
                "period_type": "fiscal_period",
                "max_staleness_days": "550",
                "bounds_policy": "nonnegative",
                "formula": "",
            }
        ],
        supporting_metric_rows=[],
        search_aliases={
            "passenger_yield": (
                "passenger yield",
                "yield per passenger mile",
            )
        },
        deferred_metric_ids={"passenger_yield"},
    )
    assert errors == []
    assert metric_rows[0]["has_deferred_fixture_pairs"] == 1
    assert metric_rows[0]["semantic_contract_sha256"]

    pair_rows, evidence_rows, errors = (
        build_semantic_pair_contracts(
            adjudication_rows=[
                {
                    "queue_rank": "1",
                    "pair_key": "AAL|passenger_yield",
                    "fixture_priority": "1",
                    "ticker": "AAL",
                    "universe_role": "active",
                    "calibration_cohort": "air",
                    "primary_archetype": "passenger_airline",
                    "metric_id": "passenger_yield",
                    "metric_pack": "air",
                    "source_lane": "DP",
                    "source_metric_ids": "passenger_yield",
                    "representative_evidence_keys": "e1",
                    "review_decision": "DEFER",
                }
            ],
            fixture_evidence_rows=[
                {
                    "pair_key": "AAL|passenger_yield",
                    "fixture_priority": "1",
                    "ticker": "AAL",
                    "metric_id": "passenger_yield",
                    "source_metric_id": "passenger_yield",
                    "source_stage": "SEC_DELTA_RUN",
                    "evidence_key": "e1",
                    "candidate_status": "REVIEW_REQUIRED",
                    "candidate_value": "0.18",
                    "unit": "USD_per_passenger_distance",
                    "period_end": "2025-12-31",
                    "scope": "consolidated",
                    "confidence": "0.65",
                }
            ],
            metric_contract_rows=metric_rows,
        )
    )
    assert errors == []
    assert pair_rows[0]["fixture_status"] == "FROZEN_REVIEW_REQUIRED"
    assert pair_rows[0]["acceptance_authorized"] == 0
    assert pair_rows[0]["retrieval_eligible_after_freeze"] == 1
    assert evidence_rows[0]["evidence_row_sha256"]


def test_financial_repair_separates_not_applicable_from_source_gap() -> None:
    residual = [
        {
            "pair_key": f"{ticker}|cash_runway_years",
            "ticker": ticker,
            "universe_role": "active",
            "calibration_cohort": "development",
            "primary_archetype": "precommercial_transport",
            "metric_id": "cash_runway_years",
            "source_lane": "FIN-D",
        }
        for ticker in ("CASH", "MISS")
    ]
    endpoints = {
        ticker: {
            "endpoint_id": f"endpoint-{ticker}",
            "endpoint_type": "ISSUER_WEBSITE_DISCOVERY_ROOT",
            "discovery_url": f"https://{ticker.lower()}.example/",
        }
        for ticker in ("CASH", "MISS")
    }
    pairs, dependencies, errors = build_financial_repair_contracts(
        residual_rows=residual,
        feature_rows={
            "CASH": {
                "source_id": "SRC-CASH",
                "fiscal_period_end": "2025-12-31",
                "cash_burn_ttm_usd": 0.0,
                "cash_and_equivalents_usd": 10.0,
            },
            "MISS": {
                "source_id": "SRC-MISS",
                "fiscal_period_end": "2025-12-31",
            },
        },
        availability_rows={},
        canonical_rows=[],
        endpoint_rows=endpoints,
        asof_date="2026-07-22",
    )
    assert errors == []
    by_ticker = {row["ticker"]: row for row in pairs}
    assert (
        by_ticker["CASH"]["repair_classification"]
        == "FORMULA_DEFINED_NOT_APPLICABLE"
    )
    assert by_ticker["CASH"]["retrieval_included_in_one_pass"] == 0
    assert (
        by_ticker["MISS"]["repair_classification"]
        == "SOURCE_OR_PERIOD_GAP"
    )
    assert by_ticker["MISS"]["retrieval_included_in_one_pass"] == 1
    assert {row["ticker"] for row in dependencies} == {"CASH", "MISS"}


def test_one_pass_preflight_reconciles_all_three_lanes() -> None:
    residual = [
        {
            "pair_key": "AAL|passenger_yield",
            "ticker": "AAL",
            "metric_id": "passenger_yield",
            "source_lane": "DP",
            "coverage_status": "SEARCHED_NOT_FOUND",
            "required_action": "SEAL_NON_SEC_ENDPOINTS",
            "candidate_source_lane_ids": "issuer_ir_annual_report_pdf",
            "search_aliases": "passenger yield",
        },
        {
            "pair_key": "AAL|unit_cost",
            "ticker": "AAL",
            "metric_id": "unit_cost",
            "source_lane": "DP",
            "coverage_status": "COVERED_REVIEW_REQUIRED",
            "required_action": "ADJUDICATE",
            "candidate_source_lane_ids": "issuer_ir_annual_report_pdf",
            "search_aliases": "unit cost",
        },
        {
            "pair_key": "AAL|cash_runway_years",
            "ticker": "AAL",
            "metric_id": "cash_runway_years",
            "source_lane": "FIN-D",
            "coverage_status": "FINANCIAL_INPUTS_MISSING",
            "required_action": "REPAIR_FINANCIAL_INPUT_PIPELINE",
            "candidate_source_lane_ids": "financial_statement_input_pipeline",
            "search_aliases": "",
        },
    ]
    endpoint = {
        "ticker": "AAL",
        "universe_role": "active",
        "endpoint_id": "endpoint-aal",
        "endpoint_type": "ISSUER_WEBSITE_DISCOVERY_ROOT",
        "discovery_url": "https://aal.example/",
        "approved_domain": "aal.example",
        "target_domain": "aal.example",
    }
    requirements, ticker_scope, errors = build_one_pass_preflight(
        residual_rows=residual,
        endpoint_rows=[endpoint],
        base_pair_endpoint_rows=[
            {
                **endpoint,
                "pair_key": "AAL|passenger_yield",
            }
        ],
        semantic_pair_rows=[
            {
                "pair_key": "AAL|unit_cost",
                "fixture_id": "fixture-aal-unit-cost",
            }
        ],
        financial_pair_rows=[
            {
                "pair_key": "AAL|cash_runway_years",
                "ticker": "AAL",
                "metric_id": "cash_runway_years",
                "repair_id": "repair-aal-runway",
                "required_action": "RECLASSIFY_NOT_APPLICABLE",
                "candidate_source_lane_ids": "issuer_ir_annual_report_pdf",
                "search_terms": "cash and cash equivalents",
                "retrieval_included_in_one_pass": "0",
            }
        ],
        full_scope_rows=[
            {
                "ticker": "AAL",
                "metric_id": "passenger_yield",
                "source_lane": "DP",
                "applicability_status": "APPLICABLE",
            },
            {
                "ticker": "AAL",
                "metric_id": "unit_cost",
                "source_lane": "DP",
                "applicability_status": "APPLICABLE",
            },
        ],
        supporting_scope_rows=[
            {
                "ticker": "AAL",
                "support_metric_id": "airline_capacity_units",
                "applicability_status": "APPLICABLE",
            }
        ],
    )
    assert errors == []
    assert len(requirements) == 3
    summary = summarize_one_pass_preflight(requirements)
    assert summary["document_discovery_required_pair_count"] == 2
    assert ticker_scope[0]["applicable_parser_metric_count"] == 2
    assert ticker_scope[0]["parse_all_applicable_metrics"] == 1


def _enumeration_endpoint(
    *,
    ticker: str = "AAL",
    endpoint_type: str = "ISSUER_WEBSITE_DISCOVERY_ROOT",
) -> dict[str, str]:
    return {
        "endpoint_id": f"endpoint-{ticker.lower()}",
        "ticker": ticker,
        "endpoint_type": endpoint_type,
        "universe_role": "active",
        "discovery_url": "https://ir.example.com/",
        "approved_domain": "ir.example.com",
        "target_domain": "example.com",
    }


def _enumeration_scope(ticker: str = "AAL") -> dict[str, str]:
    return {
        "ticker": ticker,
        "candidate_source_lane_ids": "issuer_ir_earnings_release",
        "applicable_parser_metric_count": "2",
        "applicable_parser_metric_ids": "passenger_yield|unit_cost",
        "applicable_supporting_metric_count": "1",
        "applicable_supporting_metric_ids": "airline_capacity_units",
    }


def test_live_primary_document_enumeration_keeps_all_metric_scope() -> None:
    endpoint = _enumeration_endpoint()
    documents, external = build_live_document_candidates(
        endpoint=endpoint,
        scope=_enumeration_scope(),
        pages=[
            {
                "request_url": "https://ir.example.com/results/",
                "final_url": "https://ir.example.com/results/",
                "page_role": "NAVIGATION",
                "content_type": "text/html",
                "payload": (
                    b'<a href="/files/2025-annual-report.pdf">'
                    b"2025 Annual Report</a>"
                    b'<a href="https://s2.q4cdn.com/123/results.xlsx">'
                    b"Quarterly results supplement</a>"
                    b'<a href="/news/financial-results-on-august-6-2026">'
                    b"Financial results on August 6 2026</a>"
                ),
            }
        ],
        metric_search_terms=("passenger yield", "unit cost"),
    )
    rows = deduplicate_document_rows([*documents, *documents])
    assert len(rows) == 3
    assert all(row["parse_all_applicable_metrics"] == 1 for row in rows)
    assert all(
        row["applicable_parser_metric_ids"]
        == "passenger_yield|unit_cost"
        for row in rows
    )
    assert rows[0]["content_hash_status"] == (
        "PENDING_ONE_TIME_DOCUMENT_HYDRATION"
    )
    assert {
        row["published_date_hint"] for row in rows
    } >= {"2026-08-06"}
    assert external == []


def test_archive_enumeration_deduplicates_cdx_content_digest() -> None:
    endpoint = {
        **_enumeration_endpoint(
            ticker="AAI",
            endpoint_type="ARCHIVED_ISSUER_WEBSITE_CDX_ROOT",
        ),
        "universe_role": "delisted_usable",
        "discovery_url": (
            "https://web.archive.org/cdx/search/cdx?"
            "url=airtran.com%2F%2A&output=json"
            "&filter=statuscode:200&filter=mimetype:text/html"
            "&collapse=digest&from=1994&to=2012"
        ),
        "approved_domain": "web.archive.org",
        "target_domain": "airtran.com",
    }
    html_query, pdf_query, data_query = build_archive_queries(
        endpoint["discovery_url"]
    )
    assert "mimetype%3Atext%2Fhtml" in html_query
    assert "mimetype%3Aapplication%2Fpdf" in pdf_query
    assert "ms-excel" in data_query
    yearly_queries = build_archive_year_queries(
        endpoint["discovery_url"]
    )
    assert len(yearly_queries) == 57
    assert yearly_queries[0][0:2] == (
        "ARCHIVE_CDX_HTML",
        1994,
    )
    assert "from=1994" in yearly_queries[0][2]
    assert "to=1994" in yearly_queries[0][2]
    payload = json.dumps(
        [
            [
                "timestamp",
                "original",
                "digest",
                "statuscode",
                "mimetype",
            ],
            [
                "20040101000000",
                "http://airtran.com/investor/2003-annual-report.pdf",
                "CDXDIGEST",
                "200",
                "application/pdf",
            ],
            [
                "20040201000000",
                "http://www.airtran.com/investor/2003-annual-report.pdf",
                "CDXDIGEST",
                "200",
                "application/pdf",
            ],
        ]
    ).encode()
    documents = build_archive_document_candidates(
        endpoint=endpoint,
        scope=_enumeration_scope("AAI"),
        cdx_pages=[
            {
                "request_url": pdf_query,
                "page_role": "ARCHIVE_CDX_PDF",
                "payload": payload,
            }
        ],
        metric_search_terms=("passenger yield",),
    )
    assert len(documents) == 1
    assert documents[0]["archive_capture_count"] == 2
    assert documents[0]["source_content_digest"] == "CDXDIGEST"
    assert documents[0]["content_hash_status"] == (
        "SOURCE_ARCHIVE_DIGEST_AVAILABLE"
    )


def test_endpoint_enumeration_fails_closed_when_root_is_missing() -> None:
    endpoint = _enumeration_endpoint()
    endpoint_rows, errors = build_endpoint_enumeration_rows(
        endpoint_rows=[endpoint],
        discovery_rows=[
            {
                "endpoint_id": endpoint["endpoint_id"],
                "page_role": "ROOT",
                "page_status": "FAILED",
            }
        ],
        document_rows=[],
        required_pair_counts={"AAL": 2},
    )
    assert endpoint_rows[0]["endpoint_status"] == "ROOT_DISCOVERY_FAILED"
    assert errors == ["AAL: sealed root was not enumerated"]
    reviewed_endpoint = {
        **endpoint,
        "root_repair_unresolved_disposition": (
            "REVIEWED_PRIMARY_SITE_ACCESS_LIMITATION"
        ),
    }
    endpoint_rows, errors = build_endpoint_enumeration_rows(
        endpoint_rows=[reviewed_endpoint],
        discovery_rows=[
            {
                "endpoint_id": endpoint["endpoint_id"],
                "page_role": "ROOT",
                "page_status": "FAILED",
            }
        ],
        document_rows=[],
        required_pair_counts={"AAL": 2},
    )
    assert endpoint_rows[0]["endpoint_status"] == (
        "REVIEWED_PRIMARY_SITE_ACCESS_LIMITATION"
    )
    assert errors == []
    assert canonicalize_url(
        "https://IR.Example.com/report.pdf?utm_source=x#page=2"
    ) == "https://ir.example.com/report.pdf"
    assert is_navigation_url(
        "https://ri.example.com/informacoes-e-relatorios/"
        "resultados-trimestrais/"
    )
    assert not is_navigation_url(
        "https://ir.example.com/investor-contacts/"
    )
    assert not is_navigation_url(
        "https://ir.example.com/search-results/?q=quarterly+results"
    )
    assert not is_navigation_url(
        "https://ir.example.com/customer-support/results/"
    )
    assert not is_navigation_url(
        "https://ir.example.com/results/annual-report.pdf"
    )
    assert not is_navigation_url(
        "https://ir.example.com/results-wheel.png"
    )
    relevant, _, _ = classify_document(
        url="https://ir.example.com/results-wheel.png",
        title="Quarterly results wheel",
        metric_search_terms=("unit cost",),
        linked_from_relevant_page=True,
    )
    assert not relevant
    title, published = parse_html_metadata(
        b"<html><head><title>Q2 Results</title>"
        b'<meta property="article:published_time" '
        b'content="2026-07-23T06:00:00-05:00"></head></html>'
    )
    assert title == "Q2 Results"
    assert published == "2026-07-23"
