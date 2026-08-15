from __future__ import annotations

import json

from industrials.transportation.tanker_semantic_review import review_candidate


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
