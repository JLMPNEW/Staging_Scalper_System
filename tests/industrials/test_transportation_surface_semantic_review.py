from __future__ import annotations

import json

from industrials.transportation.surface_semantic_review import (
    candidate_key,
    definition_id,
    review_candidate,
)


def _parser(metric: str, value: float, unit: str, evidence: str, raw: str, currency: str = "") -> dict[str, object]:
    return {
        "source_lane": "parser_run_evidence",
        "ticker": "TEST",
        "metric_id": metric,
        "candidate_value": value,
        "unit": unit,
        "concept_name": "ReportedSurfaceOperatingKpi",
        "extraction_method": "dedicated_parser:transportation_surface_table_v1",
        "status_reason": "reported_surface_kpi_requires_semantic_fixture_review",
        "evidence_text": evidence,
        "evidence_key": "evidence-1",
        "provenance_json": json.dumps({"raw_value_text": raw, "raw_currency": currency}),
    }


def _fact(numerator: str, formula: str, value: float, numerator_value: float) -> dict[str, object]:
    return {
        "source_lane": "fact_store_ratio",
        "ticker": "TEST",
        "metric_id": "operating_ratio",
        "candidate_value": value,
        "unit": "ratio",
        "concept_name": numerator,
        "extraction_method": "loaded_sec_fact_store_ratio",
        "status_reason": "derived_from_loaded_sec_fact_store_requires_definition_review",
        "formula": formula,
        "numerator_concept": numerator,
        "denominator_concept": "Revenues",
        "provenance_json": json.dumps({"numerator_value": numerator_value, "denominator_value": 100.0}),
    }


def test_exact_fact_operating_ratio_recalculates_and_generic_costs_fail() -> None:
    exact = review_candidate(_fact("OperatingExpenses", "operating_expense/revenue", 0.82, 82.0))
    broad = review_candidate(_fact("CostsAndExpenses", "operating_expense/revenue", 0.82, 82.0))

    assert exact.approved
    assert not broad.approved
    assert broad.reason == "operating_operand_not_definition_exact"


def test_reported_operating_ratio_passes_but_bonus_threshold_does_not() -> None:
    valid = _parser(
        "operating_ratio", 0.762, "ratio",
        "Operating ratio | 76.2 | % | 75.4 | %", "76.2",
    )
    threshold = _parser(
        "operating_ratio", 0.93, "ratio",
        "Profit-sharing bonus upon achievement of an annual operating ratio of 93.0% or below",
        "93.0%",
    )

    assert review_candidate(valid).approved
    assert not review_candidate(threshold).approved


def test_equipment_capex_dollars_and_growth_level_are_rejected() -> None:
    capex = _parser(
        "fleet_or_equipment_count", 25474.0, "count",
        "The table below sets forth net capital expenditures (in thousands) | Tractors | 25,474",
        "25,474",
    )
    yield_level = _parser(
        "pricing_or_yield_growth", 0.2609, "ratio",
        "Gross revenue per hundredweight, excluding fuel surcharges | $ | 26.09 | $ | 24.99 | 4.4 | %",
        "$ 26.09", "$",
    )

    assert not review_candidate(capex).approved
    assert not review_candidate(yield_level).approved


def test_true_operating_kpis_and_candidate_identity_are_stable() -> None:
    length = _parser(
        "average_length_of_haul", 913.0, "distance",
        "Average length of haul (miles) | 913 | 916 | (0.3) | %", "913",
    )
    velocity = _parser(
        "rail_network_velocity", 18.9, "distance_per_time",
        "Train Velocity (Miles Per Hour) | 18.9 | 18.6 | 2 | %", "18.9",
    )

    assert review_candidate(length).approved
    assert review_candidate(velocity).approved
    assert definition_id(length) == definition_id(dict(length))
    assert candidate_key(length) == "evidence-1"


def test_strict_chrw_table_ratio_recalculates_before_approval() -> None:
    row = _parser(
        "purchased_transportation_ratio",
        0.75,
        "ratio",
        "Purchased transportation and related services=3,000; total consolidated revenues=4,000",
        "0.75",
    )
    row["concept_name"] = "DerivedPurchasedTransportationRatioFromReportedTable"
    row["extraction_method"] = "dedicated_parser:transportation_surface_strict_v2"
    row["provenance_json"] = json.dumps({
        "formula": "purchased_transportation/revenue",
        "numerator_concept": "PurchasedTransportationAndRelatedServices",
        "numerator_value": 3000.0,
        "denominator_concept": "TotalConsolidatedRevenues",
        "denominator_value": 4000.0,
        "raw_value_text": "0.75",
    })

    assert review_candidate(row).approved
    row["candidate_value"] = 0.70
    assert not review_candidate(row).approved


def test_terminal_dwell_without_the_word_time_and_leading_growth_values_are_supported() -> None:
    dwell = _parser(
        "terminal_dwell_time",
        9.5,
        "hours",
        "Summary of Rail Data | Average terminal dwell (hours) | 9.5 | 10.3",
        "9.5",
    )
    shipments = _parser(
        "shipment_or_load_growth",
        0.044,
        "ratio",
        "A 4.4 percent increase in LTL shipments per workday was recorded.",
        "4.4 percent",
    )

    assert review_candidate(dwell).approved
    assert review_candidate(shipments).approved


def test_fleet_count_requires_comparable_primary_power_units() -> None:
    tractors = _parser(
        "fleet_or_equipment_count", 6188.0, "count",
        "Operating Data by Segment | Tractors (end of period) | 6,188", "6,188",
    )
    trailers = _parser(
        "fleet_or_equipment_count", 51227.0, "count",
        "Revenue equipment statistics | Trailers | 51,227", "51,227",
    )
    footnote = _parser(
        "fleet_or_equipment_count", 2.0, "count",
        "Average tractors (Trucking segment only) 2 | 18,393 | Average trailers | 57,716", "2",
    )
    later_trailers = _parser(
        "fleet_or_equipment_count", 57716.0, "count",
        "Average tractors (Trucking segment only) 2 | 18,393 | Average trailers | 57,716", "57,716",
    )
    finance_lease_context = _parser(
        "fleet_or_equipment_count", 7700.0, "count",
        "We owned approximately 7,700 tractors and 26,500 trailers, including equipment acquired with finance leases.",
        "7,700",
    )

    assert review_candidate(tractors).approved
    assert not review_candidate(trailers).approved
    assert not review_candidate(footnote).approved
    assert not review_candidate(later_trailers).approved
    assert review_candidate(finance_lease_context).approved


def test_revenue_per_power_unit_is_annualized_or_rejected_when_period_is_unknown() -> None:
    weekly = _parser(
        "revenue_per_tractor_or_power_unit", 5190.0, "currency_per_asset_period",
        "Operating Data by Segment | Revenue per truck per week | $ | 5,190", "$5,190", "$",
    )
    quarterly = _parser(
        "revenue_per_tractor_or_power_unit", 93627.0, "currency_per_asset_period",
        "Three Months Ended March 31 | Average revenue per tractor | $ | 93,627", "$93,627", "$",
    )
    unknown = _parser(
        "revenue_per_tractor_or_power_unit", 93627.0, "currency_per_asset_period",
        "Average revenue per tractor | $ | 93,627", "$93,627", "$",
    )

    assert review_candidate(weekly).reviewed_value == 5190.0 * 52.0
    assert review_candidate(quarterly).reviewed_value == 93627.0 * 4.0
    assert not review_candidate(unknown).approved
