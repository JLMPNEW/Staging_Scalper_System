from __future__ import annotations

import pytest

from dedicated_parser.contracts import FilingRef, MetricRequest, NormalizedFact, WorkItem
from dedicated_parser.semantic import parse_semantic_document
from industrials.transportation.dedicated_parser_adapter import (
    ADAPTER_VERSION,
    _concept_patterns,
    _surface_filing_profiles,
    _surface_source_map,
    applicable_parser_metrics,
    map_normalized_facts,
)
from industrials.transportation.surface_metric_parser import (
    derive_surface_table_evidence,
)


def _item(
    ticker: str,
    *metrics: str,
    form_type: str = "10-K",
) -> WorkItem:
    return WorkItem(
        model_family="transportation",
        adapter_path=(
            "industrials.transportation.dedicated_parser_adapter:"
            "extract_metric_evidence"
        ),
        adapter_version=ADAPTER_VERSION,
        filing=FilingRef(
            ticker=ticker,
            cik="0000000001",
            accession_number="0000000001-26-000001",
            form_type=form_type,
            filing_date="2026-02-15",
            accepted_at="2026-02-15T21:00:00Z",
            report_date="2025-12-31",
            primary_document="annual.htm",
            source_id="sec_archive_xbrl",
            company_currency="USD",
        ),
        documents=(),
        requested_metrics=tuple(MetricRequest(metric) for metric in metrics),
        enable_arelle=False,
        enable_edgartools=False,
    )


def _fact(
    concept: str,
    value: float,
    *,
    unit: str = "USD",
    period_start: str = "2025-01-01",
    period_end: str = "2025-12-31",
    scope: str = "consolidated",
    context_id: str | None = None,
) -> NormalizedFact:
    return NormalizedFact(
        taxonomy="us-gaap",
        concept_name=concept,
        value_text=str(value),
        numeric_value=value,
        unit=unit,
        period_start=period_start,
        period_end=period_end,
        context_id=context_id or "ctx-consolidated-period",
        dimensions_json="{}",
        scope=scope,
        source_document="annual.htm",
        provider="arelle",
    )


def _derive_table(html: str, ticker: str, *metrics: str, form_type: str = "10-K"):
    semantic = parse_semantic_document(html, source_document="annual.htm")
    return derive_surface_table_evidence(
        _item(ticker, *metrics, form_type=form_type),
        semantic.blocks,
        requested_metrics=set(metrics),
        source_document="annual.htm",
        document_sha256="a" * 64,
        source_kind="sec_archive_primary",
        source_contracts=_surface_source_map(),
        filing_profiles=_surface_filing_profiles(),
        document_extraction_method="html_text",
        document_extraction_cache_status="NOT_APPLICABLE",
    )


def test_surface_applicability_is_metric_specific_and_excludes_downstream_derivation() -> None:
    unp = applicable_parser_metrics("UNP")
    arcb = applicable_parser_metrics("ARCB")
    expd = applicable_parser_metrics("EXPD")

    assert "operating_ratio" in unp & arcb & expd
    assert "rail_network_velocity" in unp
    assert "rail_network_velocity" not in arcb
    assert "purchased_transportation_ratio" in arcb and "purchased_transportation_ratio" in expd
    assert "purchased_transportation_ratio" not in unp
    assert "average_length_of_haul" in arcb and "average_length_of_haul" not in expd
    assert "surface_volume_growth" not in unp


def test_registry_requests_xbrl_operands_without_treating_them_as_ratio_values() -> None:
    patterns = _concept_patterns("operating_ratio")
    assert any("OperatingIncomeLoss" in pattern for pattern in patterns)
    assert any("OperatingExpenses" in pattern for pattern in patterns)
    assert any("Revenues" in pattern for pattern in patterns)

    evidence = map_normalized_facts(
        _item("UNP", "operating_ratio"),
        (
            _fact("Revenues", 1_000.0),
            _fact("OperatingIncomeLoss", 200.0),
        ),
    )

    assert len(evidence) == 1
    assert evidence[0].value == pytest.approx(0.8)
    assert evidence[0].unit == "ratio"
    assert evidence[0].status == "REVIEW_REQUIRED"
    assert evidence[0].concept_name == "DerivedOperatingRatioFromXbrlOperands"
    assert evidence[0].provenance["formula"] == "1-operating_income/revenue"


def test_operating_expense_is_preferred_to_operating_income_fallback() -> None:
    evidence = map_normalized_facts(
        _item("CSX", "operating_ratio"),
        (
            _fact("Revenues", 1_000.0),
            _fact("OperatingExpenses", 690.0),
            _fact("OperatingIncomeLoss", 280.0),
        ),
    )

    assert len(evidence) == 1
    assert evidence[0].value == pytest.approx(0.69)
    assert evidence[0].provenance["formula"] == "operating_expense/revenue"


def test_purchased_transportation_ratio_is_same_period_and_consolidated_only() -> None:
    evidence = map_normalized_facts(
        _item("CHRW", "purchased_transportation_ratio"),
        (
            _fact("Revenues", 1_000.0),
            _fact("PurchasedTransportationExpense", 725.0),
            _fact(
                "PurchasedTransportationExpense",
                100.0,
                scope="segment",
                context_id="segment-context",
            ),
        ),
    )

    assert len(evidence) == 1
    assert evidence[0].value == pytest.approx(0.725)
    assert evidence[0].provenance["numerator_concept"] == "PurchasedTransportationExpense"


def test_contracted_services_fallback_remains_explicitly_review_only() -> None:
    evidence = map_normalized_facts(
        _item("HUBG", "purchased_transportation_ratio"),
        (
            _fact("Revenues", 1_000.0),
            _fact("ContractedServicesExpense", 600.0),
        ),
    )

    assert len(evidence) == 1
    assert evidence[0].value == pytest.approx(0.6)
    assert evidence[0].reason == "broad_contracted_services_operand_requires_note_confirmation"
    assert evidence[0].confidence < 0.9


def test_section_aware_operational_table_extracts_value_and_lineage() -> None:
    evidence = _derive_table(
        """
        <h2>Item 7. Management's Discussion and Analysis</h2>
        <p>Our LTL Operating Statistics</p>
        <table>
          <tr><th>Metric</th><th>2025</th></tr>
          <tr><td>Average Length of Haul</td><td>531 miles</td></tr>
          <tr><td>Weight per Shipment</td><td>1,247 pounds</td></tr>
        </table>
        """,
        "ARCB",
        "average_length_of_haul",
        "freight_weight_per_shipment",
    )
    by_metric = {row.metric_name: row for row in evidence}

    assert by_metric["average_length_of_haul"].value == 531.0
    assert by_metric["average_length_of_haul"].unit == "distance"
    assert by_metric["freight_weight_per_shipment"].value == 1247.0
    assert by_metric["average_length_of_haul"].provenance["preferred_section_match"] is True
    assert by_metric["average_length_of_haul"].provenance["document_extraction_method"] == "html_text"


def test_raw_levels_in_comparable_year_columns_derive_growth_instead_of_becoming_rates() -> None:
    evidence = _derive_table(
        """
        <h2>Item 7. Management's Discussion and Analysis</h2>
        <p>Our Revenue Statistics</p>
        <table>
          <tr><th>Metric</th><th>2025</th><th>2024</th></tr>
          <tr><td>Freight Carloads</td><td>1,100</td><td>1,000</td></tr>
        </table>
        """,
        "UNP",
        "rail_carload_growth",
    )

    assert len(evidence) == 1
    assert evidence[0].value == pytest.approx(0.1)
    assert evidence[0].concept_name == "DerivedSurfaceYearOverYearGrowth"
    assert evidence[0].provenance["formula"] == "latest/prior-1"


def test_40f_profile_and_event_kpis_preserve_source_context() -> None:
    annual = _derive_table(
        """
        <h2>Item 5. Operating and Financial Review and Prospects</h2>
        <p>Our Operating Statistics</p>
        <table><tr><th>Metric</th><th>2025</th></tr>
        <tr><td>Operating Ratio</td><td>61.4%</td></tr></table>
        """,
        "CNI",
        "operating_ratio",
        form_type="40-F",
    )
    event = _derive_table(
        """
        <h2>Item 8.01 Railroad Performance</h2>
        <p>Our Weekly Railroad Performance</p>
        <table><tr><th>Metric</th><th>Current Week</th></tr>
        <tr><td>Network Velocity</td><td>22.5 mph</td></tr>
        <tr><td>Terminal Dwell</td><td>19.7 hours</td></tr></table>
        """,
        "UNP",
        "rail_network_velocity",
        "terminal_dwell_time",
        form_type="8-K",
    )

    assert annual[0].value == pytest.approx(0.614)
    assert annual[0].provenance["form_profile_match"] is True
    assert annual[0].provenance["accounting_framework"] == "US_GAAP"
    event_by_metric = {row.metric_name: row for row in event}
    assert event_by_metric["rail_network_velocity"].value == 22.5
    assert event_by_metric["terminal_dwell_time"].value == 19.7
    assert event_by_metric["terminal_dwell_time"].provenance["document_source_kind"] == "sec_archive_primary"


def test_peer_table_is_rejected_even_when_metric_and_value_are_present() -> None:
    evidence = _derive_table(
        """
        <h2>Peer Comparison</h2>
        <table><tr><th>Competitor Metric</th><th>2025</th></tr>
        <tr><td>Average Length of Haul</td><td>500 miles</td></tr></table>
        """,
        "ARCB",
        "average_length_of_haul",
    )

    assert len(evidence) == 1
    assert evidence[0].status == "REJECTED_POLICY"
    assert evidence[0].reason == "nonissuer_or_proforma_scope"


def test_chrw_purchased_transportation_ratio_uses_paired_consolidated_table_rows() -> None:
    evidence = _derive_table(
        """
        <h2>Item 8. Financial Statements</h2>
        <p>Our consolidated statements of operations follow.</p>
        <table>
          <tr><th>Line item</th><th>2025</th></tr>
          <tr><td>Total consolidated revenues</td><td>4,000</td></tr>
          <tr><td>Purchased transportation and related services</td><td>3,000</td></tr>
        </table>
        """,
        "CHRW",
        "purchased_transportation_ratio",
    )

    strict = [
        row for row in evidence
        if row.concept_name == "DerivedPurchasedTransportationRatioFromReportedTable"
    ]
    assert len(strict) == 1
    assert strict[0].value == pytest.approx(0.75)
    assert strict[0].provenance["numerator_value"] == 3000.0
    assert strict[0].provenance["denominator_value"] == 4000.0


def test_ltl_growth_parser_binds_direction_and_value_to_exact_kpi_label() -> None:
    evidence = _derive_table(
        """
        <h2>Item 7. Management's Discussion and Analysis</h2>
        <p>Revenue declined 2.9 percent. LTL shipments per workday were down
        4.4 percent, while LTL revenue per shipment increased 1.5 percent.</p>
        """,
        "SAIA",
        "shipment_or_load_growth",
        "pricing_or_yield_growth",
    )
    strict = {
        row.metric_name: row
        for row in evidence
        if row.extraction_method.endswith("transportation_surface_strict_v2")
    }

    assert strict["shipment_or_load_growth"].value == pytest.approx(-0.044)
    assert strict["pricing_or_yield_growth"].value == pytest.approx(0.015)


def test_equipment_additions_and_monetary_property_rows_are_not_fleet_counts() -> None:
    evidence = _derive_table(
        """
        <h2>Item 2. Properties</h2>
        <p>We purchased over 1,200 tractors in 2025.</p>
        <table><tr><th>Property class</th><th>Cost in millions</th></tr>
        <tr><td>Vehicles, tractors and trailers</td><td>3,033</td></tr></table>
        """,
        "XPO",
        "fleet_or_equipment_count",
    )

    assert not any(row.concept_name == "ReportedLtlTractorCount" for row in evidence)


def test_owned_ltl_tractor_count_is_extracted_without_using_trailer_or_year_values() -> None:
    evidence = _derive_table(
        """
        <h2>Item 2. Properties</h2>
        <p>At December 31, 2025, we owned approximately 7,700 tractors and
        26,500 trailers, including equipment acquired with finance leases.</p>
        """,
        "SAIA",
        "fleet_or_equipment_count",
    )

    strict = [row for row in evidence if row.concept_name == "ReportedLtlTractorCount"]
    assert len(strict) == 1
    assert strict[0].value == 7700.0
