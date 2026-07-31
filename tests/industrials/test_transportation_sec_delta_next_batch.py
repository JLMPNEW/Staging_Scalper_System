from __future__ import annotations

from industrials.transportation.non_sec_residual import (
    build_non_sec_residual_rows,
    candidate_source_lanes,
    residual_disposition,
)
from industrials.transportation.non_sec_endpoints import (
    archived_discovery_url,
    build_endpoint_rows,
    extract_domain_counts,
    select_issuer_domain,
)
from industrials.transportation.adjudication import policy_row
from industrials.transportation.parser_repair import (
    PARSER_REPAIR_VERSION,
    build_parser_repair_rows,
    failure_document_keys,
    summarize_parser_repair,
)
from industrials.transportation.repair_coverage import (
    repaired_document_keys,
    suppress_repaired_failure_counts,
)
from industrials.transportation.sec_delta_execution import (
    validate_execution_payload,
    validate_execution_preflight,
)
from industrials.transportation.sec_union_coverage import (
    coverage_rates_from_counts,
    merge_evidence_stats,
    merge_work_stats,
)
from industrials.transportation.union_adjudication import (
    build_union_adjudication,
    source_metrics_for_pair,
    summarize_union_adjudication,
)


def _source() -> dict[str, object]:
    return {
        "acceptance": "PASS",
        "parser_metric_count": 84,
        "selected_accession_count": 2,
        "selected_document_row_count": 3,
        "artifact": {"sha256": "source-hash"},
    }


def _plan() -> dict[str, object]:
    return {
        "mode": "plan_only",
        "summary": {
            "scheduled_accessions": 2,
            "scheduled_documents": 3,
            "missing_cache_accessions": 0,
            "execution_scope": {
                "all_metrics": True,
                "max_filings_per_ticker": 0,
                "max_documents_per_filing": 0,
                "enable_pdf_ocr": False,
                "source_manifest": {"sha256": "source-hash"},
            },
        },
    }


def test_sec_delta_preflight_and_execution_accept_complete_resume() -> None:
    errors = validate_execution_preflight(
        source_manifest=_source(),
        source_csv_sha256="source-hash",
        plan_gate={
            "acceptance": "PASS",
            "mode": "plan_only",
            "source_manifest_sha256": "source-hash",
            "adapter_version": "adapter-v1",
            "parser_metric_count": 84,
            "missing_cache_accessions": 0,
            "all_parser_metrics": True,
        },
        plan_payload=_plan(),
        adapter_version="adapter-v1",
        parser_metric_count=84,
    )
    assert errors == []

    execution = {
        "mode": "shadow",
        "run_id": 61,
        "completed_work_count": 1,
        "failed_work_count": 0,
        "adjudication_skeleton_written": False,
        "summary": {
            "scheduled_accessions": 1,
            "skipped_completed_accessions": 1,
            "linked_completed_work_count": 1,
            "missing_cache_accessions": 0,
            "execution_scope": {
                "all_metrics": True,
                "enable_pdf_ocr": False,
                "source_manifest": {"sha256": "source-hash"},
            },
        },
    }
    assert (
        validate_execution_payload(
            payload=execution,
            source_manifest=_source(),
            source_csv_sha256="source-hash",
            parser_return_code=0,
        )
        == []
    )


def test_sec_delta_execution_rejects_partial_or_unsealed_result() -> None:
    errors = validate_execution_payload(
        payload={
            "mode": "shadow",
            "run_id": 61,
            "completed_work_count": 1,
            "failed_work_count": 1,
            "adjudication_skeleton_written": True,
            "summary": {
                "scheduled_accessions": 1,
                "skipped_completed_accessions": 0,
                "linked_completed_work_count": 0,
                "missing_cache_accessions": 1,
                "execution_scope": {
                    "all_metrics": False,
                    "enable_pdf_ocr": True,
                    "source_manifest": {"sha256": "wrong"},
                },
            },
        },
        source_manifest=_source(),
        source_csv_sha256="source-hash",
        parser_return_code=1,
    )
    assert errors
    assert any("nonzero" in error for error in errors)
    assert any("failed work" in error for error in errors)
    assert any("source-manifest hash" in error for error in errors)


def test_union_coverage_merges_counts_periods_and_work() -> None:
    base = {
        ("AAL", "passenger_load_factor"): {
            "text_hit_count": 1,
            "value_candidate_count": 1,
            "accepted_value_count": 1,
            "review_value_count": 0,
            "rejected_value_count": 0,
            "parser_failure_count": 0,
            "periods": {"2024-12-31"},
            "accepted_periods": {"2024-12-31"},
            "usable_periods": {"2024-12-31"},
        }
    }
    delta = {
        ("AAL", "passenger_load_factor"): {
            **base[("AAL", "passenger_load_factor")],
            "accepted_value_count": 0,
            "review_value_count": 1,
            "periods": {"2025-12-31"},
            "accepted_periods": set(),
            "usable_periods": {"2025-12-31"},
        }
    }
    merged = merge_evidence_stats(base, delta)[
        ("AAL", "passenger_load_factor")
    ]
    assert merged["text_hit_count"] == 2
    assert merged["accepted_value_count"] == 1
    assert merged["review_value_count"] == 1
    assert merged["periods"] == {"2024-12-31", "2025-12-31"}
    assert merge_work_stats(
        {"AAL": {"searched": 2, "completed": 2, "failed": 0}},
        {"AAL": {"searched": 3, "completed": 3, "failed": 0}},
    )["AAL"] == {"searched": 5, "completed": 5, "failed": 0}
    rates = coverage_rates_from_counts(
        {
            "COVERED_ACCEPTED": 1,
            "COVERED_REVIEW_REQUIRED": 1,
            "SEARCHED_NOT_FOUND": 2,
        }
    )
    assert rates == {
        "accepted": 0.25,
        "usable": 0.5,
        "discovery": 0.5,
    }


def test_non_sec_residual_routes_retrieval_and_review_separately() -> None:
    lanes = candidate_source_lanes(
        calibration_cohort="air_transport_and_aviation_services",
        metric_pack="air",
        universe_role="delisted_usable",
        foreign_private_issuer=True,
        development_overlay=False,
    )
    assert "issuer_ir_results_presentation" in lanes
    assert "primary_local_exchange_regulatory_filing" in lanes
    assert "archived_issuer_ir_site" in lanes
    assert residual_disposition(
        coverage_status="COVERED_REVIEW_REQUIRED",
        source_lane="DP",
    ) == (3, "ADJUDICATE_EXISTING_EVIDENCE_FIRST", 0)

    rows = build_non_sec_residual_rows(
        coverage_rows=[
            {
                "ticker": "HAFN",
                "universe_role": "active",
                "calibration_cohort": (
                    "marine_transportation_and_infrastructure"
                ),
                "industry": "Marine Shipping",
                "primary_archetype": "shipping_operator",
                "metric_id": "fleet_capacity",
                "metric_pack": "marine",
                "source_lane": "DP",
                "applicability_status": "APPLICABLE",
                "coverage_status": "SEARCHED_NOT_FOUND",
            },
            {
                "ticker": "HAFN",
                "metric_id": "pre_revenue_flag",
                "source_lane": "FIN-D",
                "applicability_status": "APPLICABLE",
                "coverage_status": "FINANCIAL_INPUTS_MISSING",
            },
            {
                "ticker": "HAFN",
                "metric_id": "vessel_utilization",
                "source_lane": "DP",
                "applicability_status": "APPLICABLE",
                "coverage_status": "COVERED_ACCEPTED",
            },
        ],
        metric_aliases={"fleet_capacity": ("fleet capacity",)},
        foreign_tickers={"HAFN"},
    )
    assert len(rows) == 2
    assert rows[0]["retrieval_eligible"] == 1
    assert (
        "primary_local_exchange_regulatory_filing"
        in str(rows[0]["candidate_source_lane_ids"])
    )
    financial = next(
        row for row in rows if row["source_lane"] == "FIN-D"
    )
    assert financial["retrieval_eligible"] == 0


def test_pdf_repair_manifest_selects_only_repairable_failure_docs() -> None:
    failures = [
        {
            "ticker": "AAL",
            "accession_number": "0001",
            "metric_name": "passenger_yield",
            "source_document": "annual.pdf",
            "candidate_status": "PARSER_FAILURE",
            "extraction_method": "dedicated_parser:pdf_size_limit",
        },
        {
            "ticker": "AAL",
            "accession_number": "0001",
            "metric_name": "unit_cost",
            "source_document": "annual.pdf",
            "candidate_status": "PARSER_FAILURE",
            "extraction_method": "dedicated_parser:pdf_size_limit",
        },
        {
            "ticker": "AAL",
            "accession_number": "0002",
            "metric_name": "unit_cost",
            "source_document": "broken.htm",
            "candidate_status": "PARSER_FAILURE",
            "extraction_method": "dedicated_parser:document_read",
        },
    ]
    assert failure_document_keys(failures) == {
        ("AAL", "0001", "annual.pdf")
    }
    source = {
        "ticker": "AAL",
        "accession_number": "0001",
        "document_name": "annual.pdf",
        "content_sha256": "a" * 64,
        "cache_status": "CACHED_HASHED",
        "file_size": "51000000",
        "applicable_metric_ids": "passenger_yield|unit_cost",
    }
    rows, errors = build_parser_repair_rows(
        source_rows=[source],
        failure_rows=failures,
    )
    assert errors == []
    assert len(rows) == 1
    assert rows[0]["manifest_version"] == PARSER_REPAIR_VERSION
    assert rows[0]["document_name"] == "annual.pdf"
    assert (
        rows[0]["selection_rule"]
        == "dp6h_targeted_existing_pdf_failure_repair"
    )


def test_pdf_repair_summary_keeps_evidence_pairs_separate_from_docs() -> None:
    failures = [
        {
            "ticker": "AAL",
            "accession_number": "0001",
            "metric_name": metric,
            "source_document": "annual.pdf",
            "candidate_status": "PARSER_FAILURE",
            "extraction_method": "dedicated_parser:pdf_size_limit",
        }
        for metric in ("passenger_yield", "unit_cost")
    ]
    repair_rows, errors = build_parser_repair_rows(
        source_rows=[
            {
                "ticker": "AAL",
                "accession_number": "0001",
                "document_name": "annual.pdf",
                "content_sha256": "a" * 64,
                "cache_status": "CACHED_HASHED",
                "file_size": "51000000",
            }
        ],
        failure_rows=failures,
    )
    assert errors == []
    summary = summarize_parser_repair(
        repair_rows=repair_rows,
        failure_rows=failures,
        residual_failure_pairs={
            ("AAL", "passenger_yield"),
            ("AAL", "unit_cost"),
        },
    )
    assert summary["failure_evidence_count"] == 2
    assert summary["repair_document_count"] == 1
    assert summary["residual_parser_failure_pair_count"] == 2
    assert (
        summary["residual_pairs_covered_by_failure_evidence_count"]
        == 2
    )


def test_repaired_coverage_suppresses_only_matching_old_failures() -> None:
    evidence = {
        ("AAL", "unit_cost"): {
            "text_hit_count": 0,
            "value_candidate_count": 0,
            "accepted_value_count": 0,
            "review_value_count": 0,
            "rejected_value_count": 0,
            "parser_failure_count": 2,
            "periods": set(),
            "accepted_periods": set(),
            "usable_periods": set(),
        }
    }
    repair_keys = repaired_document_keys(
        [
            {
                "ticker": "AAL",
                "accession_number": "0001",
                "document_name": "annual.pdf",
            }
        ]
    )
    cleaned, count, errors = suppress_repaired_failure_counts(
        evidence=evidence,
        failure_rows=[
            {
                "ticker": "AAL",
                "accession_number": "0001",
                "metric_name": "unit_cost",
                "source_document": "annual.pdf",
                "candidate_status": "PARSER_FAILURE",
            },
            {
                "ticker": "AAL",
                "accession_number": "0002",
                "metric_name": "unit_cost",
                "source_document": "other.pdf",
                "candidate_status": "PARSER_FAILURE",
            },
        ],
        repaired_keys=repair_keys,
    )
    assert errors == []
    assert count == 1
    assert cleaned[("AAL", "unit_cost")]["parser_failure_count"] == 1
    assert evidence[("AAL", "unit_cost")]["parser_failure_count"] == 2


def test_union_adjudication_defers_unconfirmed_broad_discovery() -> None:
    assert source_metrics_for_pair(
        metric_id="fleet_capacity_growth",
        source_lane="DP-D",
    )
    decisions, fixtures = build_union_adjudication(
        coverage_rows=[
            {
                "ticker": "AAL",
                "universe_role": "active",
                "calibration_cohort": (
                    "air_transport_and_aviation_services"
                ),
                "primary_archetype": "passenger_airline",
                "metric_id": "passenger_yield",
                "metric_pack": "air",
                "source_lane": "DP",
                "applicability_status": "APPLICABLE",
                "coverage_status": "COVERED_REVIEW_REQUIRED",
            }
        ],
        evidence_rows=[
            {
                "source_stage": "SEC_DELTA_RUN",
                "evidence_key": "e1",
                "ticker": "AAL",
                "metric_name": "passenger_yield",
                "candidate_status": "REVIEW_REQUIRED",
                "candidate_value": 0.18,
                "unit": "USD_per_passenger_distance",
                "period_end": "2025-12-31",
                "scope": "consolidated",
                "confidence": 0.65,
                "accession_number": "0001",
                "source_document": "release.htm",
            }
        ],
        legacy_index={},
        reviewed_at="2026-07-27",
        reviewed_by="test",
    )
    assert len(decisions) == 1
    assert decisions[0]["review_decision"] == "DEFER"
    assert (
        decisions[0]["required_next_action"]
        == "BUILD_METRIC_SPECIFIC_SEMANTIC_FIXTURE"
    )
    assert decisions[0]["exact_confirmation_count"] == 0
    assert len(fixtures) == 1
    assert summarize_union_adjudication(decisions)[
        "metric_fixture_pair_count"
    ] == 1


def test_policy_row_can_target_a_later_immutable_run() -> None:
    row = policy_row(
        {
            "evidence_key": "e1",
            "ticker": "AAL",
            "accession_number": "0001",
            "source_document": "release.htm",
            "metric_name": "passenger_yield",
            "concept_name": "PassengerYield",
            "candidate_value": 0.18,
            "unit": "USD_per_passenger_distance",
            "period_start": "",
            "period_end": "2025-12-31",
        },
        decision="ACCEPTED",
        status_reason="exact_confirmation",
        reviewed_at="2026-07-27",
        run_id=59,
        policy_version="test-v1",
    )
    assert row["policy_id"].startswith("trprev_r59_")
    assert row["policy_version"] == "test-v1"


def test_endpoint_domain_selection_prefers_issuer_name_and_ir_signal() -> None:
    counts = extract_domain_counts(
        """
        https://ir.suncountry.com/report
        https://ir.suncountry.com/report2
        https://ir.allegiantair.com/annual
        https://ir.allegiantair.com/quarter
        https://ir.allegiantair.com/deck
        https://www.sec.gov/Archives/example
        """
    )
    domain, count, name_match, confidence = select_issuer_domain(
        ticker="ALGT",
        company_name="Allegiant Travel Company",
        counts=counts,
    )
    assert domain == "ir.allegiantair.com"
    assert count == 3
    assert name_match
    assert confidence >= 0.8


def test_endpoint_seal_maps_each_pair_once_and_archives_delisted() -> None:
    residual = [
        {
            "pair_key": "AAI|passenger_yield",
            "ticker": "AAI",
            "metric_id": "passenger_yield",
            "universe_role": "delisted_usable",
            "calibration_cohort": "air",
            "coverage_status": "SEARCHED_NOT_FOUND",
            "required_action": (
                "SEAL_NON_SEC_ENDPOINTS_FOR_ONE_PASS_RETRIEVAL"
            ),
            "retrieval_eligible": "1",
            "candidate_source_lane_ids": "archived_issuer_ir_site",
            "search_aliases": "passenger yield",
        }
    ]
    endpoints, pairs, errors = build_endpoint_rows(
        residual_rows=residual,
        issuers={
            "AAI": {
                "company_name": "AirTran Holdings",
                "start_date": "2000-01-01",
                "end_date": "2011-05-02",
            }
        },
        profile_websites={},
        inferred_domains={
            "AAI": ("airtran.com", 20, True, 0.85)
        },
    )
    assert errors == []
    assert len(endpoints) == len(pairs) == 1
    assert str(endpoints[0]["endpoint_type"]).startswith("ARCHIVED_")
    assert "web.archive.org/cdx/" in str(
        endpoints[0]["discovery_url"]
    )
    assert pairs[0]["retrieval_authorized"] == 0
    assert "url=airtran.com%2F%2A" in archived_discovery_url(
        domain="airtran.com",
        start_year=2000,
        end_year=2012,
    )
