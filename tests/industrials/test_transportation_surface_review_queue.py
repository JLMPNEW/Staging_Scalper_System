from __future__ import annotations

import importlib

from industrials.transportation.dedicated_parser_adapter import _concept_patterns


queue = importlib.import_module(
    "industrials.transportation.scripts.36p_build_transportation_surface_semantic_review_queue"
)


def _row(period_end: str, confidence: float) -> dict[str, object]:
    return {
        "source_lane": "fact_store_ratio",
        "ticker": "ARCB",
        "metric_id": "purchased_transportation_ratio",
        "concept_name": "PurchasedTransportationCosts",
        "unit": "ratio",
        "extraction_method": "loaded_sec_fact_store_ratio",
        "status_reason": "derived_from_loaded_sec_fact_store_requires_definition_review",
        "formula": "purchased_transportation/revenue",
        "numerator_concept": "PurchasedTransportationCosts",
        "denominator_concept": "Revenues",
        "period_end": period_end,
        "filing_date": period_end,
        "confidence": confidence,
        "evidence_key": period_end,
    }


def test_review_queue_collapses_repeated_periods_by_semantic_definition() -> None:
    result = queue._representatives(
        [_row("2024-12-31", 0.9), _row("2025-12-31", 0.92)]
    )

    assert len(result) == 1
    assert result[0]["period_end"] == "2025-12-31"
    assert result[0]["represented_candidate_count"] == 2
    assert result[0]["represented_period_count"] == 2


def test_review_priority_keeps_broad_operands_below_exact_operands() -> None:
    assert queue._priority("fact_store_ratio", "loaded_sec_fact_store_ratio", "exact") == "HIGH"
    assert (
        queue._priority(
            "fact_store_ratio",
            "loaded_sec_fact_store_ratio",
            "broad_fact_store_operand_requires_note_confirmation",
        )
        == "MEDIUM"
    )


def test_registry_requests_local_and_namespace_qualified_xbrl_concepts() -> None:
    patterns = _concept_patterns("purchased_transportation_ratio")

    assert any(pattern.startswith("(?i)^PurchasedTransportation") for pattern in patterns)
    assert any("(?:^|[:}])PurchasedTransportation" in pattern for pattern in patterns)
