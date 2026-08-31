from __future__ import annotations

import json

import pytest

from consumer_defensive.core.financial_pipeline import (
    build_financial_feature_bundle,
    select_canonical_financial_facts,
)


CONCEPT_INDEX = {
    "Revenues": ("revenue", "income", "total", 0),
    "CostOfGoodsAndServicesSold": ("cost_of_revenue", "income", "total", 0),
    "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization": (
        "cost_of_revenue",
        "income",
        "excluding_depreciation_depletion_amortization",
        0,
    ),
    "CostOfGoodsAndServicesSoldDepreciationAndAmortization": (
        "cost_of_revenue",
        "income",
        "depreciation_amortization",
        0,
    ),
}


def _raw_fact(
    raw_fact_id: int,
    concept: str,
    value: float,
    *,
    accession: str = "0000726958-26-000020",
    accepted_at: str = "2026-06-25T16:00:00Z",
    period_start: str = "2025-05-01",
    period_end: str = "2026-04-30",
    unit: str = "USD",
    dimensions_json: str = "{}",
) -> dict[str, object]:
    return {
        "raw_fact_id": raw_fact_id,
        "source_observation_id": f"casy-source-{raw_fact_id}",
        "ticker": "CASY",
        "accession_number": accession,
        "taxonomy": "us-gaap",
        "concept": concept,
        "numeric_value": value,
        "unit": unit,
        "period_start": period_start,
        "period_end": period_end,
        "accepted_at": accepted_at,
        "dimensions_json": dimensions_json,
    }


def _canonical_rows(result: object) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for decision in result.decisions:
        rows.append({
            "canonical_metric": decision.metric,
            "canonical_component": decision.component,
            "accession_number": decision.accession_number,
            "taxonomy": decision.taxonomy,
            "source_concept": decision.source_concept,
            "period_start": decision.period_start,
            "period_end": decision.period_end,
            "accepted_at": decision.accepted_at,
            "frequency": "annual",
            "value_usd": decision.normalized_value,
            "reported_currency": decision.reported_currency,
            "source_raw_fact_id": decision.raw_fact_id,
            "source_observation_id": decision.source_observation_id,
            "quality_flags_json": json.dumps(decision.quality_flags),
        })
    return rows


def _inventory_row(
    raw_id: int, value: float, *, end: str
) -> dict[str, object]:
    return {
        "canonical_metric": "inventory",
        "canonical_component": "total",
        "accession_number": f"inventory-{end}",
        "taxonomy": "us-gaap",
        "source_concept": "InventoryNet",
        "period_start": None,
        "period_end": end,
        "accepted_at": "2026-06-25T16:00:01Z",
        "frequency": "instant",
        "value_usd": value,
        "reported_currency": "USD",
        "source_raw_fact_id": raw_id,
        "source_observation_id": f"inventory-source-{raw_id}",
        "quality_flags_json": "[]",
    }


def _build(rows: list[dict[str, object]]):
    return build_financial_feature_bundle(
        rows,
        as_of="2026-06-30",
        listing_start_date="1983-10-20",
        listing_end_date=None,
        maximum_period_age_days=550,
    )


def test_casy_exact_components_restore_gross_margin_and_average_inventory_turnover() -> None:
    result = select_canonical_financial_facts(
        [
            _raw_fact(1, "Revenues", 17_561_101_000),
            _raw_fact(
                2,
                "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization",
                13_240_060_000,
                accepted_at="2026-06-25T16:00:00Z",
            ),
            _raw_fact(
                3,
                "CostOfGoodsAndServicesSoldDepreciationAndAmortization",
                449_958_000,
                accepted_at="2026-06-25T16:00:01Z",
            ),
        ],
        concept_index=CONCEPT_INDEX,
        supported_currencies={"USD"},
    )
    rows = _canonical_rows(result)
    rows.extend([
        _inventory_row(4, 900_000_000, end="2026-04-30"),
        _inventory_row(5, 800_000_000, end="2025-04-30"),
    ])

    feature = _build(rows)

    total_cogs = 13_240_060_000 + 449_958_000
    assert feature.values["gross_margin"] == pytest.approx(
        (17_561_101_000 - total_cogs) / 17_561_101_000
    )
    assert feature.values["inventory_turnover"] == pytest.approx(
        total_cogs / 850_000_000
    )
    assert set(feature.lineage["selected_flow_lineage"]["cost_of_revenue"]) == {
        "casy-source-2",
        "casy-source-3",
    }
    assert feature.lineage["selected_flow_basis"]["cost_of_revenue"] == (
        "composed_additive_components:direct_annual"
    )
    composition = [
        row
        for row in feature.lineage["flow_selection"]["cost_of_revenue"]
        if row.get("status") == "composed_exact_context"
    ]
    assert composition[0]["component_lineage"] == [
        "casy-source-2",
        "casy-source-3",
    ]


def test_direct_total_wins_same_filing_conflict_without_double_counting() -> None:
    result = select_canonical_financial_facts(
        [
            _raw_fact(10, "Revenues", 1_000),
            _raw_fact(11, "CostOfGoodsAndServicesSold", 600),
            _raw_fact(
                12,
                "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization",
                500,
            ),
            _raw_fact(
                13,
                "CostOfGoodsAndServicesSoldDepreciationAndAmortization",
                200,
            ),
        ],
        concept_index=CONCEPT_INDEX,
        supported_currencies={"USD"},
    )
    direct = next(
        row
        for row in result.decisions
        if row.metric == "cost_of_revenue" and row.component == "total"
    )
    assert "direct_total_conflicts_with_additive_components" in direct.quality_flags

    feature = _build(_canonical_rows(result))

    assert feature.values["gross_margin"] == pytest.approx(0.4)
    assert feature.lineage["selected_flow_lineage"]["cost_of_revenue"] == [
        "casy-source-11"
    ]
    reconciliations = [
        row
        for row in feature.lineage["flow_selection"]["cost_of_revenue"]
        if row.get("source_kind") == "direct_component_reconciliation"
    ]
    assert reconciliations[0]["status"] == "conflict"
    assert reconciliations[0]["selected_source_kind"] == "direct_total"


def test_dimensioned_component_is_rejected_and_never_composed() -> None:
    result = select_canonical_financial_facts(
        [
            _raw_fact(20, "Revenues", 1_000),
            _raw_fact(
                21,
                "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization",
                500,
            ),
            _raw_fact(
                22,
                "CostOfGoodsAndServicesSoldDepreciationAndAmortization",
                100,
                dimensions_json=json.dumps({
                    "StatementBusinessSegmentsAxis": "RetailSegmentMember"
                }),
            ),
        ],
        concept_index=CONCEPT_INDEX,
        supported_currencies={"USD"},
    )

    assert result.audit_counts["dimensioned_additive_fact_rejected"] == 1
    assert not any(
        row.component == "depreciation_amortization" for row in result.decisions
    )
    feature = _build(_canonical_rows(result))
    assert feature.values["gross_margin"] is None
    assert "cost_of_revenue" not in feature.lineage["selected_flow_lineage"]


def test_components_from_different_accessions_are_not_combined() -> None:
    result = select_canonical_financial_facts(
        [
            _raw_fact(30, "Revenues", 1_000),
            _raw_fact(
                31,
                "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization",
                500,
            ),
            _raw_fact(
                32,
                "CostOfGoodsAndServicesSoldDepreciationAndAmortization",
                100,
                accession="0000726958-26-000021",
            ),
        ],
        concept_index=CONCEPT_INDEX,
        supported_currencies={"USD"},
    )

    feature = _build(_canonical_rows(result))
    assert feature.values["gross_margin"] is None
    components = [
        row for row in result.decisions if row.metric == "cost_of_revenue"
    ]
    assert all(
        "incomplete_additive_component_set" in row.quality_flags
        for row in components
    )
    rejected = [
        row
        for row in feature.lineage["flow_selection"]["cost_of_revenue"]
        if row.get("status") == "rejected_uncertified_component_rows"
    ]
    assert len(rejected) == 1
    assert rejected[0]["component_lineage"] == [
        "casy-source-31",
        "casy-source-32",
    ]


def test_declared_third_component_missing_fails_closed() -> None:
    concept_index = {
        **CONCEPT_INDEX,
        "OtherRequiredCostComponent": (
            "cost_of_revenue",
            "income",
            "other_required_cost",
            0,
        ),
    }
    result = select_canonical_financial_facts(
        [
            _raw_fact(40, "Revenues", 1_000),
            _raw_fact(
                41,
                "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization",
                500,
            ),
            _raw_fact(
                42,
                "CostOfGoodsAndServicesSoldDepreciationAndAmortization",
                100,
            ),
        ],
        concept_index=concept_index,
        supported_currencies={"USD"},
    )

    components = [
        row for row in result.decisions if row.metric == "cost_of_revenue"
    ]
    assert len(components) == 2
    assert all(
        "incomplete_additive_component_set" in row.quality_flags
        for row in components
    )
    assert all(
        "exact_context_component_set_complete" not in row.quality_flags
        for row in components
    )

    feature = _build(_canonical_rows(result))
    assert feature.values["gross_margin"] is None
    assert "cost_of_revenue" not in feature.lineage["selected_flow_lineage"]
    rejected = [
        row
        for row in feature.lineage["flow_selection"]["cost_of_revenue"]
        if row.get("status") == "rejected_uncertified_component_rows"
    ]
    assert len(rejected) == 1
    assert rejected[0]["component_lineage"] == [
        "casy-source-41",
        "casy-source-42",
    ]
