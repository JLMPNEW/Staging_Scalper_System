from __future__ import annotations

from industrials.transportation.fixture_priority import (
    build_fixture_priority_batches,
    review_phase,
)


def test_review_phase_order_is_non_overlapping() -> None:
    assert review_phase(
        {
            "metric_id": "fleet_capacity",
            "review_numeric_count": "1",
            "review_no_value_count": "0",
        }
    )[1] == "A_STRICT_SINGLE_NUMERIC"
    assert review_phase(
        {
            "metric_id": "fleet_capacity",
            "review_numeric_count": "1",
            "review_no_value_count": "3",
        }
    )[1] == "B_SINGLE_NUMERIC_WITH_TEXT_NOISE"
    assert review_phase(
        {
            "metric_id": "fleet_capacity",
            "review_numeric_count": "2",
            "review_no_value_count": "0",
        }
    )[1] == "C_TOP_SIX_REMAINING"
    assert review_phase(
        {
            "metric_id": "fleet_age",
            "review_numeric_count": "2",
            "review_no_value_count": "0",
        }
    )[1] == "D_REMAINING_FROZEN_QUEUE"


def test_priority_builder_routes_derived_pairs() -> None:
    adjudication = [
        {
            "pair_key": "AAA|fleet_capacity_growth",
            "fixture_priority": "1",
            "ticker": "AAA",
            "universe_role": "active",
            "calibration_cohort": "marine_shipping_and_maritime",
            "primary_archetype": "marine_operator",
            "metric_id": "fleet_capacity_growth",
            "metric_pack": "marine",
            "source_lane": "DP-D",
            "source_metric_ids": "fleet_capacity",
            "review_numeric_count": "1",
            "review_no_value_count": "0",
        }
    ]
    contracts = [
        {
            "pair_key": "AAA|fleet_capacity_growth",
            "fixture_id": "fixture",
            "representative_evidence_count": "1",
            "representative_evidence_keys": "key",
            "semantic_contract_sha256": "contract",
            "evidence_bundle_sha256": "bundle",
        }
    ]
    evidence = [
        {
            "pair_key": "AAA|fleet_capacity_growth",
            "ticker": "AAA",
            "metric_id": "fleet_capacity_growth",
            "source_metric_id": "fleet_capacity",
            "evidence_key": "key",
            "candidate_status": "REVIEW_REQUIRED",
            "candidate_value": "100",
            "unit": "segment_native_capacity",
            "period_end": "2025-12-31",
            "scope": "consolidated",
            "confidence": "0.65",
            "source_stage": "BASE_REVIEW_EVALUATION",
            "accession_number": "accession",
            "form_type": "20-F",
            "filing_date": "2026-03-01",
            "source_document": "annual.htm",
            "extraction_method": "semantic",
            "status_reason": "review",
            "evidence_row_sha256": "row",
            "evidence_text": "fleet capacity was 100 dwt",
        }
    ]

    pairs, evidence_rows, summary, errors = (
        build_fixture_priority_batches(
            adjudication_rows=adjudication,
            pair_contract_rows=contracts,
            evidence_rows=evidence,
        )
    )

    assert pairs[0]["review_route"] == "VALIDATE_DERIVATION_DEPENDENCIES"
    assert evidence_rows[0]["review_phase"] == "A_STRICT_SINGLE_NUMERIC"
    assert summary["selected_pair_count"] == 1
    assert errors  # Synthetic fixture intentionally does not match frozen totals.
