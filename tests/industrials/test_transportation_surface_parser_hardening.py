from __future__ import annotations

from dedicated_parser.contracts import FilingRef, MetricRequest, NormalizedFact, WorkItem
from dedicated_parser.semantic import parse_semantic_document
from industrials.transportation.dedicated_parser_adapter import (
    ADAPTER_VERSION,
    _surface_filing_profiles,
    _surface_source_map,
    _surface_xbrl_rules,
)
from industrials.transportation.surface_metric_parser import (
    derive_surface_table_evidence,
    derive_surface_xbrl_evidence,
)


def _item(*metrics: str) -> WorkItem:
    return WorkItem(
        model_family="transportation",
        adapter_path="industrials.transportation.dedicated_parser_adapter:extract_metric_evidence",
        adapter_version=ADAPTER_VERSION,
        filing=FilingRef(
            ticker="CP",
            cik="0000000001",
            accession_number="0000000001-26-000001",
            form_type="10-K",
            filing_date="2026-02-15",
            accepted_at="2026-02-15T21:00:00Z",
            report_date="2025-12-31",
            primary_document="annual.htm",
            source_id="sec_archive_xbrl",
        ),
        documents=(),
        requested_metrics=tuple(MetricRequest(metric) for metric in metrics),
    )


def _fact(concept: str, value: float, context: str) -> NormalizedFact:
    return NormalizedFact(
        taxonomy="us-gaap",
        concept_name=concept,
        value_text=str(value),
        numeric_value=value,
        unit="USD",
        period_start="2025-01-01",
        period_end="2025-12-31",
        context_id=context,
        dimensions_json="{}",
        scope="consolidated",
        source_document="annual.htm",
        provider="arelle",
    )


def test_xbrl_ratio_operands_are_paired_by_exact_context() -> None:
    evidence = derive_surface_xbrl_evidence(
        _item("operating_ratio"),
        (
            _fact("Revenues", 1_000.0, "context-a"),
            _fact("OperatingIncomeLoss", 250.0, "context-a"),
            _fact("Revenues", 2_000.0, "context-b"),
            _fact("OperatingIncomeLoss", 400.0, "context-b"),
        ),
        requested_metrics={"operating_ratio"},
        rules_by_metric=_surface_xbrl_rules(),
    )

    assert {round(float(row.value or 0.0), 6) for row in evidence} == {0.75, 0.8}
    assert {row.provenance["paired_context_id"] for row in evidence} == {
        "context-a",
        "context-b",
    }


def test_long_narrative_table_cell_is_not_misread_as_equipment_count() -> None:
    semantic = parse_semantic_document(
        """
        <table><tr><td>
        Our locomotives and freight cars moved additional gross ton miles during
        the quarter. Gross ton miles were 64,411 and increased 6 percent as the
        network carried more intermodal traffic; this narrative is deliberately
        longer than a plausible table row label and is not an equipment table.
        </td><td>64,411</td></tr></table>
        """,
        source_document="quarter.htm",
    )
    evidence = derive_surface_table_evidence(
        _item("fleet_or_equipment_count"),
        semantic.blocks,
        requested_metrics={"fleet_or_equipment_count"},
        source_document="quarter.htm",
        document_sha256="a" * 64,
        source_kind="sec_archive_primary",
        source_contracts=_surface_source_map(),
        filing_profiles=_surface_filing_profiles(),
    )

    assert evidence == ()


def test_broader_purchased_transportation_concepts_are_review_only_operands() -> None:
    evidence = derive_surface_xbrl_evidence(
        _item("purchased_transportation_ratio"),
        (
            _fact("Revenues", 1_000.0, "context-a"),
            _fact("PurchasedTransportationAndWarehousing", 600.0, "context-a"),
        ),
        requested_metrics={"purchased_transportation_ratio"},
        rules_by_metric=_surface_xbrl_rules(),
    )

    assert len(evidence) == 1
    assert evidence[0].value == 0.6
    assert evidence[0].status == "REVIEW_REQUIRED"
    assert evidence[0].reason == "broad_contracted_services_operand_requires_note_confirmation"
