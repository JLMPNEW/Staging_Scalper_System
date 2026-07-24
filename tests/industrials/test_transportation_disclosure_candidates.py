from __future__ import annotations

import json
from pathlib import Path

import pytest

from industrials.core.db import connect, init_db, utc_now
from industrials.core.source_registry import load_source_registry, upsert_source_registry
from industrials.transportation.disclosure_candidates import (
    extract_transportation_disclosure_candidates,
    upsert_transportation_disclosure_candidates,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
INDUSTRIALS_ROOT = PROJECT_ROOT / "industrials"
FILING = {
    "accession_number": "0000000000-26-000001",
    "form_type": "10-K",
    "filing_date": "2026-02-15",
    "accepted_at": "2026-02-15T16:00:00Z",
    "report_date": "2025-12-31",
}


def extracted(text: str, *, cohort: str, industry: str):
    return extract_transportation_disclosure_candidates(
        text,
        filing=FILING,
        cohort=cohort,
        industry=industry,
    )


def test_surface_rules_enforce_industry_applicability_and_signed_growth() -> None:
    text = """
    <p>Revenue ton-miles decreased by 4.2% compared with the prior year.</p>
    <p>Our operating ratio was 62.4%.</p>
    <p>Purchased transportation costs represented 31.5% of revenues.</p>
    """
    railroad = extracted(
        text,
        cohort="surface_freight_and_logistics",
        industry="Railroads",
    )
    metrics = {candidate.metric_name: candidate for candidate in railroad}
    assert metrics["transport_volume_growth"].value == -0.042
    assert metrics["operating_ratio"].value == 0.624
    assert "purchased_transportation_ratio" not in metrics
    logistics = extracted(
        text,
        cohort="surface_freight_and_logistics",
        industry="Integrated Freight & Logistics",
    )
    logistics_metrics = {candidate.metric_name: candidate for candidate in logistics}
    assert logistics_metrics["purchased_transportation_ratio"].value == 0.315
    assert "operating_ratio" not in logistics_metrics


def test_air_marine_and_development_rules_preserve_units_and_review_state() -> None:
    air = extracted(
        """
        <p>Revenue passenger miles increased by 7.0% and capacity grew by 5.5%.</p>
        <p>Passenger load factor was 84.1% and passenger yield was 18.2 cents.</p>
        """,
        cohort="air_transport_and_aviation_services",
        industry="Airlines",
    )
    air_metrics = {candidate.metric_name: candidate for candidate in air}
    assert air_metrics["traffic_growth"].value == 0.07
    assert air_metrics["capacity_growth"].value == 0.055
    assert air_metrics["load_factor_or_utilization"].value == 0.841
    assert air_metrics["passenger_or_lease_yield"].unit == "cents_per_passenger_unit"

    marine = extracted(
        """
        <p>Our fleet consisted of 42 vessels and fleet utilization was 96.2%.</p>
        <p>The average fleet age was 8.4 years and TCE rate was $24,500 per day.</p>
        """,
        cohort="marine_shipping_and_maritime",
        industry="Marine Shipping",
    )
    marine_metrics = {candidate.metric_name: candidate for candidate in marine}
    assert marine_metrics["fleet_capacity"].unit == "vessels"
    assert marine_metrics["tce_or_day_rate"].value == 24_500.0
    assert marine_metrics["fleet_age"].value == 8.4

    development = extracted(
        """
        <p>There is substantial doubt about our ability to continue as a going concern.</p>
        <p>We continued FAA certification flight-testing during the quarter.</p>
        """,
        cohort="development_stage_and_speculative_transport",
        industry="Airports & Air Services",
    )
    development_metrics = {candidate.metric_name: candidate for candidate in development}
    assert development_metrics["going_concern_flag"].candidate_status == "ACCEPTED"
    assert development_metrics["going_concern_flag"].value == 1.0
    assert development_metrics["commercialization_progress"].candidate_status == "REVIEW_REQUIRED"
    assert development_metrics["commercialization_progress"].value is None


def test_air_growth_requires_explicit_period_and_issuer_scope() -> None:
    air = extracted(
        """
        <p>First quarter 2026 system available seat miles decreased by 1.7%
        year-over-year compared with first quarter 2025.</p>
        <p>Regional capacity, as measured by ASMs, increased 10.3% year over year.</p>
        <p>Overall global air passenger traffic, measured in RPKs, grew by 5.3%
        in 2025 compared to 2024 according to IATA.</p>
        <p>Regional capacity purchase costs increased by 9.8% compared with the
        year-ago period.</p>
        """,
        cohort="air_transport_and_aviation_services",
        industry="Airlines",
    )
    capacity = [item for item in air if item.metric_name == "capacity_growth"]
    traffic = [item for item in air if item.metric_name == "traffic_growth"]
    assert [item.value for item in capacity] == pytest.approx([-0.017, 0.103, 0.098])
    assert [item.candidate_status for item in capacity] == [
        "ACCEPTED",
        "REVIEW_REQUIRED",
        "REVIEW_REQUIRED",
    ]
    assert [item.value for item in traffic] == pytest.approx([0.053])
    assert [item.candidate_status for item in traffic] == ["REVIEW_REQUIRED"]
    assert capacity[0].status_reason == "issuer_comparative_period_explicit"
    assert "first quarter 2025" in capacity[0].evidence_text


def test_air_operating_statistics_table_resolves_period_and_scope() -> None:
    air = extracted(
        """
        <p>Certain consolidated statistical information for the Company's
        operations for the three months ended June 30 is as follows:</p>
        <table>
          <tr><td>Revenue passenger miles ("RPMs" or "traffic") (millions)</td></tr>
          <tr><td>72,765</td><td>70,088</td><td>2,677</td><td>3.8</td></tr>
          <tr><td>Available seat miles ("ASMs" or "capacity") (millions)</td></tr>
          <tr><td>87,279</td><td>84,347</td><td>2,932</td><td>3.5</td></tr>
          <tr><td>Passenger load factor</td></tr>
          <tr><td>83.4%</td><td>83.1%</td><td>0.3 pts.</td><td>N/A</td></tr>
          <tr><td>Passenger yield (cents)</td></tr>
          <tr><td>22.13</td><td>19.74</td><td>2.39</td><td>12.1</td></tr>
        </table>
        """,
        cohort="air_transport_and_aviation_services",
        industry="Airlines",
    )
    metrics = {item.metric_name: item for item in air}
    assert metrics["traffic_growth"].value == 0.038
    assert metrics["capacity_growth"].value == 0.035
    assert metrics["load_factor_or_utilization"].value == pytest.approx(0.834)
    assert metrics["passenger_or_lease_yield"].value == 22.13
    assert {
        metrics[name].candidate_status
        for name in (
            "traffic_growth",
            "capacity_growth",
            "load_factor_or_utilization",
            "passenger_or_lease_yield",
        )
    } == {"ACCEPTED"}
    assert metrics["capacity_growth"].status_reason == (
        "operating_statistics_current_vs_prior_period_percent_change"
    )


def test_candidate_persistence_carries_sec_url_hash_and_evidence(tmp_path: Path) -> None:
    candidates = extracted(
        "<p>Passenger load factor was 84.1%.</p>",
        cohort="air_transport_and_aviation_services",
        industry="Airlines",
    )
    db_path = tmp_path / "transportation.sqlite"
    with connect(db_path) as conn:
        init_db(conn)
        registry = load_source_registry(INDUSTRIALS_ROOT / "data" / "free_source_registry.yaml")
        upsert_source_registry(conn, registry)
        count = upsert_transportation_disclosure_candidates(
            conn,
            ticker="TEST",
            cik="0000000000",
            source_id="sec_companyfacts",
            filing=FILING,
            document_name="test.htm",
            source_url=(
                "https://www.sec.gov/Archives/edgar/data/0/"
                "000000000026000001/test.htm"
            ),
            content_sha256="a" * 64,
            candidates=candidates,
            now=utc_now(),
        )
        assert count == 1
        row = conn.execute(
            """
            SELECT candidate_status, candidate_value, evidence_text, provenance_json
            FROM fact_sec_metric_disclosure_candidate
            """
        ).fetchone()
    provenance = json.loads(row["provenance_json"])
    assert row["candidate_status"] == "ACCEPTED"
    assert row["candidate_value"] == 0.841
    assert "Passenger load factor" in row["evidence_text"]
    assert provenance["source_url"].startswith("https://www.sec.gov/Archives/")
    assert provenance["content_sha256"] == "a" * 64
