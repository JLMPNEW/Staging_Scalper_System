from __future__ import annotations

import json
from datetime import date

import pytest

from industrials.transportation.fact_conflict_resolution import (
    resolve_accepted_fact_conflicts,
)
from industrials.transportation.subgroup_scoring import (
    build_fact_history,
    derive_feature,
    resolver_selection_conflict_counts,
)


METRIC_IDS = {
    "operating_ratio",
    "pricing_or_yield_growth",
    "purchased_transportation_ratio",
    "tce_day_rate",
}


def row(
    key: str,
    *,
    metric: str = "operating_ratio",
    value: float,
    concept: str = "ReportedSurfaceOperatingKpi",
    definition: str = "ReportedSurfaceOperatingKpi",
    period_start: str = "2025-01-01",
    unit: str = "ratio",
    segment: str = "consolidated",
    adjustment: str = "",
    source_lane: str = "parser_run_evidence",
    document: str = "filing.htm",
) -> dict[str, object]:
    return {
        "ticker": "AAA",
        "metric_id": metric,
        "value": value,
        "unit": unit,
        "period_start": period_start,
        "period_end": "2025-03-31",
        "filing_date": "2025-05-01",
        "accepted_at": "2025-05-01",
        "definition_basis": definition,
        "comparability_class": "issuer_stable",
        "concept_name": concept,
        "segment_id": segment,
        "denominator_basis": "reported_revenue",
        "weighting_basis": "not_applicable",
        "capacity_basis": "not_applicable",
        "adjustment_basis": adjustment,
        "source_lane": source_lane,
        "candidate_key": key,
        "evidence_key": key if source_lane == "parser_run_evidence" else "",
        "accession_number": "0000000000-25-000001",
        "source_document": document,
        "replay_status": "ACCEPTED",
    }


def evidence(
    key: str,
    *,
    text: str = "",
    scope: str = "consolidated",
    raw_value: str = "",
) -> dict[str, object]:
    return {
        "evidence_key": key,
        "scope": scope,
        "evidence_text": text,
        "extraction_method": "dedicated_parser:test",
        "provenance_json": json.dumps({"raw_value_text": raw_value}),
    }


def resolve(
    rows: list[dict[str, object]],
    evidence_rows: dict[str, dict[str, object]] | None = None,
):
    evidence_map = dict(evidence_rows or {})
    for item in rows:
        key = str(item.get("evidence_key") or "")
        if key and key not in evidence_map:
            evidence_map[key] = evidence(key)
    return resolve_accepted_fact_conflicts(
        rows=rows,
        evidence_by_key=evidence_map,
        metric_ids=METRIC_IDS,
    )


def test_score_resolver_preserves_period_start_and_fails_quarter_ytd_tie() -> None:
    rows = [
        row(
            "quarter",
            value=0.80,
            period_start="2025-01-01",
            source_lane="fact_store_ratio",
        ),
        row(
            "ytd",
            value=0.85,
            period_start="2024-10-01",
            source_lane="fact_store_ratio",
        ),
    ]
    history = build_fact_history(rows)
    value, sources = derive_feature(
        ticker="AAA",
        asof=date(2025, 6, 1),
        spec={"source_metric": "operating_ratio", "transform": "identity"},
        history=history,
        staleness_days={"operating_ratio": 800},
    )
    assert value is None
    assert sources == ()
    assert resolver_selection_conflict_counts(history) == {
        "operating_ratio": 1
    }


def test_score_duration_preference_cannot_cross_definition_boundary() -> None:
    rows = [
        row(
            "quarter",
            value=0.80,
            period_start="2025-01-01",
            definition="operating_expense/revenue",
            source_lane="fact_store_ratio",
        ),
        row(
            "ytd-other-definition",
            value=0.85,
            period_start="2024-10-01",
            definition="1-operating_income/revenue",
            source_lane="fact_store_ratio",
        ),
    ]
    history = build_fact_history(rows)
    assert derive_feature(
        ticker="AAA",
        asof=date(2025, 6, 1),
        spec={"source_metric": "operating_ratio", "transform": "identity"},
        history=history,
        staleness_days={"operating_ratio": 800},
    ) == (None, ())
    assert resolver_selection_conflict_counts(history) == {"operating_ratio": 1}


def test_score_resolver_cannot_override_fail_closed_audit_with_duration_rule() -> None:
    rows = [
        row(
            "quarter",
            value=0.80,
            period_start="2025-01-01",
            source_lane="fact_store_ratio",
        ),
        row(
            "ytd",
            value=0.85,
            period_start="2024-10-01",
            source_lane="fact_store_ratio",
        ),
    ]
    for item in rows:
        item["conflict_resolution_status"] = "FAIL_CLOSED_REVIEW_REQUIRED"
    history = build_fact_history(rows)
    assert derive_feature(
        ticker="AAA",
        asof=date(2025, 6, 1),
        spec={"source_metric": "operating_ratio", "transform": "identity"},
        history=history,
        staleness_days={"operating_ratio": 800},
    ) == (None, ())
    assert resolver_selection_conflict_counts(history) == {"operating_ratio": 1}


def test_conflict_audit_does_not_choose_between_quarter_and_ytd() -> None:
    result = resolve(
        [
            row(
                "quarter",
                value=0.80,
                period_start="2025-01-01",
                source_lane="fact_store_ratio",
            ),
            row(
                "ytd",
                value=0.85,
                period_start="2024-10-01",
                source_lane="fact_store_ratio",
            ),
        ]
    )
    assert result.manifest["resolver_conflict_count_before"] == 1
    assert result.manifest["resolver_conflict_count_after"] == 1
    assert result.groups[0].status == "FAIL_CLOSED_REVIEW_REQUIRED"
    assert {float(item["value"]) for item in result.normalized_rows} == {
        0.80,
        0.85,
    }


@pytest.mark.parametrize(
    ("field", "left", "right"),
    [
        ("period_start", "2025-01-01", "2025-02-01"),
        ("unit", "ratio", "percentage_points"),
        ("segment_id", "segment_a", "segment_b"),
        ("definition_basis", "definition_a", "definition_b"),
    ],
)
def test_adjusted_rule_cannot_cross_identity_boundaries(
    field: str,
    left: str,
    right: str,
) -> None:
    reported = row(
        "reported",
        value=0.80,
        adjustment="reported",
    )
    adjusted = row(
        "adjusted",
        value=0.75,
        adjustment="adjusted",
    )
    reported[field] = left
    adjusted[field] = right
    result = resolve([reported, adjusted])
    assert result.manifest["resolver_conflict_count_after"] == 1
    assert result.groups[0].status == "FAIL_CLOSED_REVIEW_REQUIRED"


def test_reported_gaap_operating_ratio_beats_adjusted_same_identity() -> None:
    result = resolve(
        [
            row("reported", value=0.80, adjustment="reported"),
            row("adjusted", value=0.75, adjustment="adjusted"),
        ]
    )
    assert result.groups[0].resolution_rule == (
        "reported_gaap_over_adjusted_operating_ratio"
    )
    assert {float(item["value"]) for item in result.normalized_rows} == {0.80}


@pytest.mark.parametrize(
    "field",
    [
        "period_start",
        "segment_id",
        "denominator_basis",
        "weighting_basis",
        "capacity_basis",
    ],
)
def test_known_and_missing_boundary_values_never_compare_as_same_measurement(
    field: str,
) -> None:
    reported = row("reported", value=0.80, adjustment="reported")
    adjusted = row("adjusted", value=0.75, adjustment="adjusted")
    adjusted[field] = ""
    result = resolve([reported, adjusted])
    assert result.groups[0].status == "FAIL_CLOSED_REVIEW_REQUIRED"
    assert result.manifest["resolver_conflict_count_after"] == 1


def test_known_and_missing_evidence_scope_never_compare_as_same_measurement() -> None:
    rows = [
        row("reported", value=0.80, adjustment="reported"),
        row("adjusted", value=0.75, adjustment="adjusted"),
    ]
    evidence_rows = {
        "reported": evidence("reported", scope="consolidated"),
        "adjusted": evidence("adjusted", scope="unknown"),
    }
    result = resolve(rows, evidence_rows)
    assert result.groups[0].status == "FAIL_CLOSED_REVIEW_REQUIRED"


def test_unknown_adjustment_candidate_is_not_retained_as_reported() -> None:
    rows = [
        row("reported", value=0.80, adjustment="reported"),
        row("adjusted", value=0.75, adjustment="adjusted"),
        row("unknown", value=0.70, adjustment=""),
    ]
    result = resolve(rows)
    assert result.groups[0].status == "FAIL_CLOSED_REVIEW_REQUIRED"
    assert {float(item["value"]) for item in result.normalized_rows} == {
        0.70,
        0.75,
        0.80,
    }


def test_exact_metric_parser_beats_only_its_registered_broad_alias() -> None:
    exact = row(
        "exact",
        metric="pricing_or_yield_growth",
        value=0.02,
        concept="ReportedLtlYieldGrowth",
        definition="ReportedLtlYieldGrowth",
    )
    broad = row(
        "broad",
        metric="pricing_or_yield_growth",
        value=0.90,
        concept="ReportedSurfaceOperatingKpi",
        definition="ReportedSurfaceOperatingKpi",
    )
    result = resolve([exact, broad])
    assert result.groups[0].resolution_rule == (
        "exact_metric_parser_over_broad_discovery"
    )
    assert {float(item["value"]) for item in result.normalized_rows} == {0.02}


@pytest.mark.parametrize(
    ("field", "left", "right"),
    [
        ("period_start", "2025-01-01", "2025-02-01"),
        ("unit", "ratio", "currency_per_day"),
        ("segment_id", "segment_a", "segment_b"),
        (
            "definition_basis",
            "ReportedLtlYieldGrowth",
            "incompatible_definition",
        ),
    ],
)
def test_exact_parser_rule_cannot_cross_identity_boundaries(
    field: str,
    left: str,
    right: str,
) -> None:
    exact = row(
        "exact",
        metric="pricing_or_yield_growth",
        value=0.02,
        concept="ReportedLtlYieldGrowth",
        definition="ReportedLtlYieldGrowth",
    )
    broad = row(
        "broad",
        metric="pricing_or_yield_growth",
        value=0.90,
        concept="ReportedSurfaceOperatingKpi",
        definition="ReportedSurfaceOperatingKpi",
    )
    exact[field] = left
    broad[field] = right
    result = resolve([exact, broad])
    assert result.manifest["resolver_conflict_count_after"] == 1


def test_growth_sign_normalization_requires_explicit_direction_context() -> None:
    rows = [
        row(
            "positive-parser",
            metric="pricing_or_yield_growth",
            value=0.305,
            concept="ReportedLtlYieldGrowth",
            definition="ReportedLtlYieldGrowth",
        ),
        row(
            "negative-parser",
            metric="pricing_or_yield_growth",
            value=-0.305,
            concept="ReportedLtlYieldGrowth",
            definition="ReportedLtlYieldGrowth",
        ),
    ]
    evidence_rows = {
        key: evidence(
            key,
            text="Revenue per shipment declined 30.5% versus the prior year.",
            raw_value="30.5%",
        )
        for key in ("positive-parser", "negative-parser")
    }
    result = resolve(rows, evidence_rows)
    assert result.groups[0].resolution_rule == (
        "explicit_growth_direction_sign_normalization"
    )
    assert {float(item["value"]) for item in result.normalized_rows} == {-0.305}


def test_rounding_tie_uses_direct_reported_value_not_average() -> None:
    rows = [
        row("reported", value=0.637),
        row(
            "derived",
            value=0.6369958275382476,
            concept="OperatingExpenses",
            definition="operating_expense/revenue",
            source_lane="parser_run_evidence",
        ),
    ]
    result = resolve(rows)
    assert result.groups[0].resolution_rule == (
        "reported_or_modal_value_within_disclosed_rounding"
    )
    assert {float(item["value"]) for item in result.normalized_rows} == {0.637}


@pytest.mark.parametrize(
    ("field", "left", "right"),
    [
        ("period_start", "2025-01-01", "2025-02-01"),
        ("unit", "ratio", "percentage_points"),
        ("segment_id", "segment_a", "segment_b"),
        ("definition_basis", "ReportedSurfaceOperatingKpi", "other_ratio"),
    ],
)
def test_rounding_rule_cannot_cross_identity_boundaries(
    field: str,
    left: str,
    right: str,
) -> None:
    reported = row("reported", value=0.637)
    derived = row(
        "derived",
        value=0.6369,
        concept="OperatingExpenses",
        definition="operating_expense/revenue",
        source_lane="fact_store_ratio",
    )
    reported[field] = left
    derived[field] = right
    result = resolve([reported, derived])
    assert result.manifest["resolver_conflict_count_after"] == 1


def test_fully_identified_cross_document_contradiction_remains_fail_closed() -> None:
    rows = [
        row(
            "source-a",
            value=0.80,
            period_start="2025-01-01",
            segment="consolidated",
            adjustment="reported",
            document="filing-a.htm",
        ),
        row(
            "source-b",
            value=0.90,
            period_start="2025-01-01",
            segment="consolidated",
            adjustment="reported",
            document="filing-b.htm",
        ),
    ]
    result = resolve(rows)
    group = result.groups[0]
    assert group.status == "FAIL_CLOSED_REVIEW_REQUIRED"
    assert group.confirmed_true_contradiction is True
    assert result.manifest["confirmed_true_contradiction_count"] == 1
    assert {float(item["value"]) for item in result.normalized_rows} == {0.80, 0.90}
