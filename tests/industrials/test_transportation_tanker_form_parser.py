from __future__ import annotations

from dedicated_parser.contracts import FilingRef, MetricRequest, NormalizedFact, WorkItem
from industrials.transportation.dedicated_parser_adapter import (
    ADAPTER_VERSION,
    _tanker_filing_profiles,
    applicable_parser_metrics,
    get_registry,
    map_normalized_facts,
)


ADAPTER = "industrials.transportation.dedicated_parser_adapter:extract_metric_evidence"
DIRECT_TANKER_METRICS = {
    "fleet_capacity",
    "revenue_days",
    "offhire_or_drydock_ratio",
    "tce_day_rate",
    "fleet_age",
    "vessel_count",
    "newbuild_capacity_commitments",
    "capex_commitments",
    "vessel_opex_per_day",
    "spot_or_charter_day_rate",
    "fleet_utilization",
    "charter_coverage_next_12m",
    "contracted_revenue_backlog",
    "weighted_average_charter_term",
    "cash_breakeven_per_day",
    "spot_exposure_ratio",
}


def _item(ticker: str, metric_id: str, *, form_type: str = "20-F") -> WorkItem:
    return WorkItem(
        model_family="transportation",
        adapter_path=ADAPTER,
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
        requested_metrics=(MetricRequest(metric_id),),
        enable_arelle=False,
        enable_edgartools=False,
    )


def _fact(
    concept_name: str,
    value: float,
    unit: str,
    *,
    scope: str = "consolidated",
) -> NormalizedFact:
    return NormalizedFact(
        taxonomy="issuer",
        concept_name=concept_name,
        value_text=str(value),
        numeric_value=value,
        unit=unit,
        period_start="2025-01-01",
        period_end="2025-12-31",
        context_id="D2025",
        dimensions_json="{}" if scope == "consolidated" else '{"axis":"member"}',
        scope=scope,
        source_document="annual.htm",
        provider="arelle",
    )


def test_v3_delta_scope_is_visible_to_adapter_for_all_new_tankers() -> None:
    for ticker in ("DHT", "FRO", "NAT", "STNG", "TRMD", "INSW", "TNK", "TEN"):
        assert DIRECT_TANKER_METRICS <= applicable_parser_metrics(ticker)
    assert "passenger_load_factor" not in applicable_parser_metrics("DHT")


def test_filing_profiles_use_actual_sec_annual_forms() -> None:
    profiles = _tanker_filing_profiles()
    assert profiles["ASC"]["annual_form"] == "20-F"
    assert profiles["INSW"]["annual_form"] == "10-K"
    assert profiles["FRO"]["accounting_framework"] == "IFRS"
    assert profiles["TNK"]["accounting_framework"] == "US_GAAP"


def test_exact_audited_vessel_count_is_accepted_and_unit_normalized() -> None:
    evidence = map_normalized_facts(
        _item("DHT", "vessel_count"),
        (_fact("NumberOfVessels", 22.0, "Vessel"),),
    )
    assert len(evidence) == 1
    assert evidence[0].status == "ACCEPTED"
    assert evidence[0].reason == "audited_ixbrl_exact_tanker_concept"
    assert evidence[0].unit == "count"
    assert evidence[0].provenance["accounting_framework"] == "IFRS"


def test_exact_audited_capacity_is_accepted_with_native_unit_preserved() -> None:
    evidence = map_normalized_facts(
        _item("DHT", "fleet_capacity"),
        (_fact("TotalCarryingCapacity", 6_840_114.0, "t"),),
    )
    assert len(evidence) == 1
    assert evidence[0].status == "ACCEPTED"
    assert evidence[0].unit == "segment_native_capacity"
    assert evidence[0].provenance["raw_xbrl_unit"] == "t"


def test_forward_coverage_ratio_and_backlog_exact_concepts_are_accepted() -> None:
    coverage = map_normalized_facts(
        _item("TRMD", "charter_coverage_next_12m"),
        (
            _fact(
                "PercentageOfEarningsDaysInNextFiscalYearCoveredByCurrentContracts",
                0.085,
                "pure",
            ),
        ),
    )
    backlog = map_normalized_facts(
        _item("DHT", "contracted_revenue_backlog"),
        (_fact("FutureCharterPaymentsTotal", 150_955_000.0, "USD"),),
    )
    assert coverage[0].status == "ACCEPTED"
    assert coverage[0].unit == "ratio"
    assert backlog[0].status == "ACCEPTED"
    assert backlog[0].unit == "USD"


def test_transactional_vessel_subcount_stays_in_review() -> None:
    evidence = map_normalized_facts(
        _item("DHT", "vessel_count"),
        (_fact("NumberOfVesselsSold", 2.0, "Vessel"),),
    )
    assert len(evidence) == 1
    assert evidence[0].status == "REVIEW_REQUIRED"
    assert evidence[0].unit == "count"


def test_exact_concept_is_not_autoaccepted_from_unaudited_form() -> None:
    evidence = map_normalized_facts(
        _item("DHT", "vessel_count", form_type="6-K"),
        (_fact("NumberOfVessels", 22.0, "Vessel"),),
    )
    assert len(evidence) == 1
    assert evidence[0].status == "REVIEW_REQUIRED"


def test_registry_contains_form_table_search_anchors() -> None:
    registry = get_registry()
    assert {
        "deadweight",
        "dry-docking",
        "earning days",
        "non-GAAP",
        "off-hire",
        "operating days",
        "TCE",
    } <= set(registry.document_keywords)
    vessel = registry.request("vessel_count")
    capacity = registry.request("fleet_capacity")
    assert vessel is not None and any("NumberOfVessels" in value for value in vessel.concept_patterns)
    assert capacity is not None and any("AggregateVesselCapacity" in value for value in capacity.concept_patterns)
