from __future__ import annotations

import pytest

from dedicated_parser.contracts import FilingRef, MetricRequest, WorkItem
from dedicated_parser.semantic import parse_semantic_document
from industrials.transportation.dedicated_parser_adapter import ADAPTER_VERSION
from industrials.transportation.tanker_metric_derivations import (
    derive_tanker_table_evidence,
)


def _item(*metrics: str) -> WorkItem:
    return WorkItem(
        model_family="transportation",
        adapter_path=(
            "industrials.transportation.dedicated_parser_adapter:"
            "extract_metric_evidence"
        ),
        adapter_version=ADAPTER_VERSION,
        filing=FilingRef(
            ticker="INSW",
            cik="0000000001",
            accession_number="0000000001-26-000001",
            form_type="10-K",
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


def _derive(html: str, *metrics: str):
    semantic = parse_semantic_document(html, source_document="annual.htm")
    return derive_tanker_table_evidence(
        _item(*metrics),
        semantic.blocks,
        requested_metrics=set(metrics),
        source_document="annual.htm",
        document_sha256="a" * 64,
    )


def test_fleet_age_is_dwt_weighted_from_vessel_rows() -> None:
    evidence = _derive(
        """
        <h2>Fleet Overview</h2>
        <table>
          <tr><th>Vessel</th><th>Year Built</th><th>DWT</th></tr>
          <tr><td>Alpha</td><td>2020</td><td>50,000</td></tr>
          <tr><td>Beta</td><td>2015</td><td>100,000</td></tr>
        </table>
        """,
        "fleet_age",
    )

    assert len(evidence) == 1
    assert evidence[0].metric_name == "fleet_age"
    assert evidence[0].value == pytest.approx(8.3333333333)
    assert evidence[0].unit == "years"
    assert evidence[0].status == "REVIEW_REQUIRED"
    assert evidence[0].provenance["operand_count"] == 2


def test_revenue_days_and_offhire_ratio_share_reconciled_operands() -> None:
    evidence = _derive(
        """
        <h2>Operating Statistics</h2>
        <table>
          <tr><th>Metric</th><th>2025</th></tr>
          <tr><td>Available Days</td><td>360</td></tr>
          <tr><td>Off-hire Days</td><td>10</td></tr>
        </table>
        """,
        "revenue_days",
        "offhire_or_drydock_ratio",
    )
    by_metric = {row.metric_name: row for row in evidence}

    assert by_metric["revenue_days"].value == 350.0
    assert by_metric["revenue_days"].unit == "days"
    assert by_metric["offhire_or_drydock_ratio"].value == pytest.approx(10 / 360)
    assert by_metric["offhire_or_drydock_ratio"].unit == "ratio"


def test_charter_coverage_is_derived_only_when_fixed_days_fit_denominator() -> None:
    valid = _derive(
        """
        <table>
          <tr><th>Fleet Deployment</th><th>2026</th></tr>
          <tr><td>Contracted Days</td><td>180</td></tr>
          <tr><td>Total Available Days</td><td>360</td></tr>
        </table>
        """,
        "charter_coverage_next_12m",
    )
    invalid = _derive(
        """
        <table>
          <tr><th>Fleet Deployment</th><th>2026</th></tr>
          <tr><td>Contracted Days</td><td>400</td></tr>
          <tr><td>Total Available Days</td><td>360</td></tr>
        </table>
        """,
        "charter_coverage_next_12m",
    )

    assert len(valid) == 1
    assert valid[0].value == 0.5
    assert valid[0].status == "REVIEW_REQUIRED"
    assert invalid == ()


def test_exact_tanker_rows_emit_normalized_strict_candidates() -> None:
    evidence = _derive(
        """
        <h2>Item 5. Operating and Financial Review and Prospects</h2>
        <table>
          <tr><th>Operating KPI</th><th>2025</th></tr>
          <tr><td>Revenue days</td><td>3,650</td></tr>
          <tr><td>Average daily TCE rate</td><td>$31,250</td></tr>
          <tr><td>Commercial utilization</td><td>97.5%</td></tr>
        </table>
        """,
        "revenue_days",
        "tce_day_rate",
        "fleet_utilization",
    )
    by_metric = {row.metric_name: row for row in evidence}

    assert by_metric["revenue_days"].value == 3650.0
    assert by_metric["tce_day_rate"].value == 31250.0
    assert by_metric["fleet_utilization"].value == pytest.approx(0.975)
    assert all(
        row.provenance["derivation_version"] == "transportation_tanker_tables_v2"
        for row in by_metric.values()
    )


def test_percent_change_is_not_misread_as_tanker_ratio_level() -> None:
    evidence = _derive(
        """
        <table>
          <tr><th>Commentary</th><th>Change</th></tr>
          <tr><td>Commercial utilization increased</td><td>2.5%</td></tr>
        </table>
        """,
        "fleet_utilization",
    )

    assert evidence == ()

def test_single_vessel_schedule_derives_count_capacity_age_and_forward_coverage() -> None:
    evidence = _derive(
        """
        <h2>Fleet Overview</h2>
        <table>
          <tr><th>Vessel Name</th><th>Year Built</th><th>DWT</th><th>Employment</th><th>Charter End</th></tr>
          <tr><td>Alpha</td><td>2020</td><td>50,000</td><td>Time charter</td><td>December 31, 2026</td></tr>
          <tr><td>Beta</td><td>2015</td><td>100,000</td><td>Spot</td><td>-</td></tr>
        </table>
        """,
        "vessel_count",
        "fleet_capacity",
        "fleet_age",
        "charter_coverage_next_12m",
    )
    by_concept = {row.concept_name: row for row in evidence}

    assert by_concept["DerivedVesselCountFromSchedule"].value == 2.0
    assert by_concept["DerivedFleetCapacityFromVesselSchedule"].value == 150_000.0
    assert by_concept["DerivedDwtWeightedFleetAge"].value == pytest.approx(8.3333333333)
    assert by_concept["DerivedForwardCharterCoverageFromVesselSchedule"].value == pytest.approx(0.5)
    assert by_concept["DerivedForwardCharterCoverageFromVesselSchedule"].provenance["coverage_end_date"] == "2026-12-31"


def test_utilization_and_scaled_opex_use_explicit_day_denominators() -> None:
    evidence = _derive(
        """
        <h2>Operating Statistics (in thousands)</h2>
        <table>
          <tr><th>Metric</th><th>2025</th></tr>
          <tr><td>Revenue Days</td><td>3,500</td></tr>
          <tr><td>Available Days</td><td>3,650</td></tr>
          <tr><td>Operating Days</td><td>3,650</td></tr>
          <tr><td>Vessel Operating Expenses</td><td>36,500</td></tr>
        </table>
        """,
        "fleet_utilization",
        "vessel_opex_per_day",
    )
    by_concept = {row.concept_name: row for row in evidence}

    assert by_concept["DerivedFleetUtilizationFromDays"].value == pytest.approx(3500 / 3650)
    assert by_concept["DerivedVesselOpexPerOperatingDay"].value == pytest.approx(10_000.0)
    assert by_concept["DerivedVesselOpexPerOperatingDay"].provenance["denominator_basis"] == "operating_days"
