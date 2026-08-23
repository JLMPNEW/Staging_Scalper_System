from __future__ import annotations

import importlib
import json

from industrials.transportation.tanker_semantic_review import (
    definition_signature,
    review_candidate,
)


def _row(**updates: object) -> dict[str, object]:
    row: dict[str, object] = {
        "source_lane": "parser_run_evidence",
        "ticker": "INSW",
        "metric_id": "tce_day_rate",
        "candidate_value": 31_250.0,
        "unit": "currency_per_day",
        "concept_name": "ReportedTceDayRate",
        "extraction_method": "dedicated_parser:transportation_table_derivation",
        "status_reason": "strict_reported_tanker_table_row_requires_semantic_replay",
        "evidence_text": "Average daily TCE rate | $31,250",
        "provenance_json": json.dumps({
            "unit_contract": "currency_per_day",
            "raw_value_text": "$31,250",
            "strict_rule_id": "tce_day_rate_exact_table_row_v2",
        }),
    }
    row.update(updates)
    return row


def test_exact_tce_row_passes_tanker_semantic_guard() -> None:
    result = review_candidate(_row())
    assert result.approved
    assert result.reviewed_value == 31_250.0


def test_utilization_change_is_not_accepted_as_utilization_level() -> None:
    result = review_candidate(_row(
        metric_id="fleet_utilization",
        candidate_value=0.025,
        unit="ratio",
        concept_name="ReportedFleetUtilization",
        evidence_text="Commercial utilization increased 2.5 percent.",
        provenance_json=json.dumps({"unit_contract": "ratio", "raw_value_text": "2.5 percent"}),
    ))
    assert not result.approved


def test_revenue_days_derivation_recalculates_operands() -> None:
    result = review_candidate(_row(
        metric_id="revenue_days",
        candidate_value=350.0,
        unit="days",
        concept_name="DerivedRevenueDaysFromAvailableLessOffhire",
        evidence_text="Available days 360 less off-hire days 10.",
        provenance_json=json.dumps({
            "unit_contract": "days",
            "available_days": 360,
            "offhire_days": 10,
        }),
    ))
    assert result.approved


def test_derived_offhire_ratio_rejects_period_mismatch_extremes() -> None:
    result = review_candidate(_row(
        metric_id="offhire_or_drydock_ratio",
        candidate_value=1.0,
        unit="ratio",
        concept_name="DerivedOffhireDaysToAvailableDays",
        evidence_text="Off-hire days 10 divided by available days 10.",
        provenance_json=json.dumps({
            "unit_contract": "ratio",
            "available_days": 10,
            "offhire_days": 10,
        }),
    ))
    assert not result.approved


def test_vessel_count_rejects_year_near_generic_fleet_word() -> None:
    result = review_candidate(_row(
        metric_id="vessel_count",
        candidate_value=2025.0,
        unit="count",
        concept_name="ReportedOperatingVesselCount",
        evidence_text="Fleet overview | 2025 | vessel name",
        provenance_json=json.dumps({"unit_contract": "count", "raw_value_text": "2025"}),
    ))
    assert not result.approved


def test_currency_metric_rejects_filing_year_and_capacity_requires_aggregate_label() -> None:
    year_rate = review_candidate(_row(
        metric_id="vessel_opex_per_day",
        candidate_value=2024.0,
        unit="currency_per_day",
        concept_name="TransportationDiscoveryVesselOpexPerDay",
        evidence_text="Daily vessel operating expenses | $ | 2024",
        provenance_json=json.dumps({"unit_contract": "currency_per_day", "raw_value_text": "$2024"}),
    ))
    individual_dwt = review_candidate(_row(
        metric_id="fleet_capacity",
        candidate_value=115_000.0,
        unit="segment_native_capacity",
        concept_name="TransportationDiscoveryFleetCapacity",
        evidence_text="Vessel Alpha | DWT | 115,000",
        provenance_json=json.dumps({"unit_contract": "segment_native_capacity", "raw_value_text": "115,000"}),
    ))
    aggregate_dwt = review_candidate(_row(
        metric_id="fleet_capacity",
        candidate_value=1_150_000.0,
        unit="segment_native_capacity",
        concept_name="ReportedAggregateFleetCapacity",
        evidence_text="Total fleet carrying capacity | DWT | 1,150,000",
        provenance_json=json.dumps({"unit_contract": "segment_native_capacity", "raw_value_text": "1,150,000"}),
    ))

    assert not year_rate.approved
    assert not individual_dwt.approved
    assert aggregate_dwt.approved

def test_schedule_and_day_denominator_derivations_recalculate_before_approval() -> None:
    vessel_count = review_candidate(_row(
        metric_id="vessel_count",
        candidate_value=2.0,
        unit="count",
        concept_name="DerivedVesselCountFromSchedule",
        evidence_text="Unique issuer vessel rows=2",
        provenance_json=json.dumps({
            "operand_count": 2,
            "identity_basis": "normalized_unique_vessel_name",
        }),
    ))
    utilization = review_candidate(_row(
        metric_id="fleet_utilization",
        candidate_value=3500 / 3650,
        unit="ratio",
        concept_name="DerivedFleetUtilizationFromDays",
        evidence_text="Revenue days 3,500 divided by available days 3,650",
        provenance_json=json.dumps({
            "revenue_days": 3500,
            "available_days": 3650,
            "denominator_basis": "available_days",
        }),
    ))
    opex = review_candidate(_row(
        metric_id="vessel_opex_per_day",
        candidate_value=10_000.0,
        unit="currency_per_day",
        concept_name="DerivedVesselOpexPerOperatingDay",
        evidence_text="Vessel operating expense 36,500,000 divided by operating days 3,650",
        provenance_json=json.dumps({
            "vessel_operating_expense": 36_500_000,
            "operating_days": 3650,
            "denominator_basis": "operating_days",
        }),
    ))

    assert vessel_count.approved
    assert utilization.approved
    assert opex.approved


def test_forward_charter_coverage_requires_365_day_denominator_and_date_lineage() -> None:
    valid = review_candidate(_row(
        metric_id="charter_coverage_next_12m",
        candidate_value=0.5,
        unit="ratio",
        concept_name="DerivedForwardCharterCoverageFromVesselSchedule",
        evidence_text="Contracted vessel-days=365; available vessel-days=730",
        provenance_json=json.dumps({
            "contracted_days": 365,
            "available_days": 730,
            "vessel_count": 2,
            "fixed_vessel_count": 1,
            "coverage_start_date": "2025-12-31",
            "coverage_end_date": "2026-12-31",
            "denominator_basis": "all_schedule_vessels_x_365",
        }),
    ))
    missing_dates = _row(
        metric_id="charter_coverage_next_12m",
        candidate_value=0.5,
        unit="ratio",
        concept_name="DerivedForwardCharterCoverageFromVesselSchedule",
        evidence_text="Contracted vessel-days=365; available vessel-days=730",
        provenance_json=json.dumps({
            "contracted_days": 365,
            "available_days": 730,
            "vessel_count": 2,
            "fixed_vessel_count": 1,
            "denominator_basis": "all_schedule_vessels_x_365",
        }),
    )

    assert valid.approved
    assert not review_candidate(missing_dates).approved

def test_tanker_queue_and_reviewer_share_the_canonical_definition_signature() -> None:
    queue_module = importlib.import_module(
        "industrials.transportation.scripts.36t_build_transportation_tanker_semantic_review_queue"
    )
    row = _row(
        metric_id="fleet_age",
        candidate_value=11.2,
        unit="years",
        evidence_text="Average fleet age was 11.2 years.",
    )
    row["provenance_json"] = json.dumps({
        "definition_basis": "dwt_weighted_average_age",
        "weighting_basis": "dwt",
        "denominator_basis": "operating_fleet",
        "coverage_start_date": "2025-01-01",
        "coverage_end_date": "2025-12-31",
    })

    assert queue_module._signature(row) == definition_signature(row)
