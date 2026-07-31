from __future__ import annotations

from dedicated_parser.contracts import (
    FilingRef,
    MetricRequest,
    NormalizedFact,
    WorkItem,
)
from industrials.transportation.required_metric_parser_adapter import (
    TARGET_METRICS,
    get_registry,
    map_normalized_facts,
)


def _item() -> WorkItem:
    return WorkItem(
        model_family="transportation",
        adapter_path=(
            "industrials.transportation.required_metric_parser_adapter:"
            "extract_metric_evidence"
        ),
        adapter_version=get_registry().adapter_version,
        filing=FilingRef(
            ticker="TEST",
            cik="0000000001",
            accession_number="0000000001-26-000001",
            form_type="20-F",
            filing_date="2026-03-01",
            accepted_at="2026-03-01",
            report_date="2025-12-31",
            primary_document="test.htm",
            source_id="sec_companyfacts",
        ),
        documents=(),
        requested_metrics=tuple(
            MetricRequest(metric) for metric in sorted(TARGET_METRICS)
        ),
    )


def _fact(
    *,
    taxonomy: str,
    concept: str,
    value: float,
    metadata: str = "{}",
) -> NormalizedFact:
    return NormalizedFact(
        taxonomy=taxonomy,
        concept_name=concept,
        value_text=str(value),
        numeric_value=value,
        unit="USD",
        period_start="2025-01-01",
        period_end="2025-12-31",
        context_id="C1",
        dimensions_json="{}",
        scope="consolidated",
        source_document="test.htm",
        provider="arelle",
        concept_metadata_json=metadata,
    )


def test_registry_is_bounded_to_required_financial_dependencies() -> None:
    registry = get_registry()

    assert registry.model_family == "transportation"
    assert {row.metric_name for row in registry.source_metrics} == TARGET_METRICS
    assert set(registry.production_mappings) == TARGET_METRICS


def test_standard_capex_fact_is_accepted() -> None:
    rows = map_normalized_facts(
        _item(),
        (
            _fact(
                taxonomy="us-gaap",
                concept="PaymentsToAcquirePropertyPlantAndEquipment",
                value=125.0,
            ),
        ),
    )

    capex = [row for row in rows if row.metric_name == "capex"]
    assert len(capex) == 1
    assert capex[0].status == "ACCEPTED"
    assert capex[0].value == 125.0


def test_issuer_extension_capex_requires_review() -> None:
    rows = map_normalized_facts(
        _item(),
        (
            _fact(
                taxonomy="issuer-extension",
                concept="PaymentsForAcquisitionOfVessels",
                value=250.0,
                metadata=(
                    '{"label":"Payments for acquisition of vessels",'
                    '"namespace_uri":"https://issuer.example/2025"}'
                ),
            ),
        ),
    )

    capex = [row for row in rows if row.metric_name == "capex"]
    assert len(capex) == 1
    assert capex[0].status == "REVIEW_REQUIRED"
    assert capex[0].provenance["automatic_promotion_allowed"] is False
