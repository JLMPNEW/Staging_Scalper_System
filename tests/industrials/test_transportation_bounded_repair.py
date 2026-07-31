from __future__ import annotations

from industrials.transportation.bounded_repair import (
    apply_cached_financial_source_overrides,
    apply_financial_overrides,
    audit_no_value_pairs,
    build_bounded_repair_scope,
    execute_financial_repairs,
    summarize_scope,
)


def _financial_pair(
    *,
    ticker: str,
    metric_id: str,
    classification: str,
) -> dict[str, str]:
    return {
        "repair_id": f"repair_{ticker}_{metric_id}",
        "pair_key": f"{ticker}|{metric_id}",
        "ticker": ticker,
        "metric_id": metric_id,
        "repair_classification": classification,
        "unit_contract": "ratio",
        "formula": "test_formula",
        "current_feature_period_end": "2025-12-31",
        "latest_dependency_periods_json": "{}",
    }


def test_bounded_scope_collapses_empty_documents_by_hash() -> None:
    financial = [
        _financial_pair(
            ticker="AAA",
            metric_id="capital_raise_dependence",
            classification="ALIGNMENT_OR_FORMULA_PIPELINE_GAP",
        ),
        _financial_pair(
            ticker="BBB",
            metric_id="cash_runway_years",
            classification="FORMULA_DEFINED_NOT_APPLICABLE",
        ),
        _financial_pair(
            ticker="CCC",
            metric_id="stock_compensation_to_revenue",
            classification="SOURCE_OR_PERIOD_GAP",
        ),
    ]
    coverage = [
        {
            "run_id": "65",
            "ticker": "AAA",
            "metric_id": "empty_mile_ratio",
            "coverage_status": "TEXT_HIT_NO_VALUE",
            "text_hit_count": "2",
        }
    ]
    empty = [
        {
            "content_sha256": "a" * 64,
            "ticker": ticker,
            "requested_metric_ids": "fleet_age|utilization_rate",
            "document_name": "scan.pdf",
            "document_ids": f"doc-{ticker}",
        }
        for ticker in ("AAA", "BBB")
    ]
    adjudication = [
        {
            "pair_key": "AAA|fleet_age",
            "ticker": "AAA",
            "metric_id": "fleet_age",
            "review_decision": "DEFER",
            "fixture_priority": "1",
            "representative_evidence_keys": "evidence-1",
            "required_next_action": "KEEP_DEFERRED",
        }
    ]

    rows = build_bounded_repair_scope(
        financial_rows=financial,
        coverage_rows=coverage,
        empty_context_rows=empty,
        adjudication_rows=adjudication,
    )
    summary = summarize_scope(rows)

    assert summary["repair_item_count"] == 6
    assert summary["repair_lane_counts"]["EMPTY_PDF_OCR"] == 1
    empty_row = next(
        row for row in rows if row["repair_lane"] == "EMPTY_PDF_OCR"
    )
    assert empty_row["ticker"] == "AAA|BBB"
    assert empty_row["content_sha256"] == "a" * 64


def test_financial_execution_accepts_only_aligned_operands() -> None:
    pair = _financial_pair(
        ticker="CRGO",
        metric_id="capital_raise_dependence",
        classification="ALIGNMENT_OR_FORMULA_PIPELINE_GAP",
    )
    dependencies = [
        {
            "pair_key": pair["pair_key"],
            "dependency_id": dependency_id,
            "requirement_status": status,
        }
        for dependency_id, status in (
            ("cash_burn_ttm", "PRESENT_IN_ALIGNED_FEATURE"),
            (
                "equity_issuance_ttm",
                "PRESENT_IN_ALIGNED_FEATURE",
            ),
            ("debt_issuance_ttm", "PERIOD_ALIGNMENT_REQUIRED"),
        )
    ]
    rows = execute_financial_repairs(
        pair_rows=[pair],
        dependency_rows=dependencies,
        feature_rows={
            "CRGO": {
                "cash_burn_ttm_usd": 100.0,
                "equity_issuance_proceeds_ttm_usd": 25.0,
                "debt_issuance_proceeds_ttm_usd": 50.0,
            }
        },
    )

    assert rows[0]["candidate_value"] == 0.25
    assert (
        rows[0]["coverage_override"] == "COVERED_FINANCIAL_DERIVED"
    )
    assert (
        rows[0]["quality_flags"]
        == "PARTIAL_CAPITAL_RAISE_COMPONENT_LOWER_BOUND"
    )


def test_reviewed_cached_source_override_requires_exact_contract() -> None:
    source_result = {
        **_financial_pair(
            ticker="HMR",
            metric_id="stock_compensation_to_revenue",
            classification="SOURCE_OR_PERIOD_GAP",
        ),
        "execution_status": "REQUIRES_CACHED_SOURCE_SEARCH",
        "candidate_value": "",
        "aligned_dependency_ids": "",
        "unresolved_dependency_ids": "revenue|stock_compensation",
        "coverage_override": "",
        "quality_flags": "",
        "provenance": "",
    }
    override = {
        "pair_key": "HMR|stock_compensation_to_revenue",
        "ticker": "HMR",
        "metric_id": "stock_compensation_to_revenue",
        "period_end": "2026-03-31",
        "numerator_label": "stock_based_compensation_usd",
        "numerator_value": "600000",
        "denominator_label": "total_revenue_usd",
        "denominator_value": "18400000",
        "unit": "ratio",
        "content_sha256": "b" * 64,
        "reviewed_by": "reviewer",
        "decision": "ACCEPT_EXACT_SAME_PERIOD_UNIT",
    }

    rows, count = apply_cached_financial_source_overrides(
        financial_rows=[source_result],
        override_rows=[override],
    )

    assert count == 1
    assert rows[0]["candidate_value"] == 600000 / 18400000
    assert (
        rows[0]["execution_status"]
        == "RESOLVED_CACHED_PRIMARY_SOURCE_FORMULA"
    )
    assert (
        rows[0]["coverage_override"] == "COVERED_FINANCIAL_DERIVED"
    )


def test_no_value_audit_never_promotes_ambiguous_numbers() -> None:
    coverage = [
        {
            "ticker": "AAA",
            "metric_id": "service_reliability_rate",
            "coverage_status": "TEXT_HIT_NO_VALUE",
        }
    ]
    rows = audit_no_value_pairs(
        coverage_rows=coverage,
        evidence_by_pair={
            ("AAA", "service_reliability_rate"): [
                {"evidence_text": "On-time performance improved in 2025."}
            ]
        },
    )

    assert rows[0]["coverage_override"] == ""
    assert (
        rows[0]["execution_status"]
        == "TERMINAL_STORED_EVIDENCE_AMBIGUOUS"
    )


def test_financial_overlay_changes_only_sealed_pairs() -> None:
    coverage = [
        {
            "ticker": "AAA",
            "metric_id": "capital_raise_dependence",
            "applicability_status": "APPLICABLE",
            "coverage_status": "FINANCIAL_INPUTS_MISSING",
        },
        {
            "ticker": "BBB",
            "metric_id": "cash_runway_years",
            "applicability_status": "APPLICABLE",
            "coverage_status": "FINANCIAL_INPUTS_MISSING",
        },
    ]
    financial = [
        {
            "pair_key": "AAA|capital_raise_dependence",
            "repair_id": "repair-aaa",
            "coverage_override": "COVERED_FINANCIAL_DERIVED",
            "current_feature_period_end": "2025-12-31",
        },
        {
            "pair_key": "BBB|cash_runway_years",
            "repair_id": "repair-bbb",
            "coverage_override": "NOT_APPLICABLE",
            "current_feature_period_end": "",
        },
    ]

    rows, counts = apply_financial_overrides(
        coverage_rows=coverage,
        financial_rows=financial,
    )

    assert counts == {
        "COVERED_FINANCIAL_DERIVED": 1,
        "NOT_APPLICABLE": 1,
    }
    assert rows[0]["accepted_value_count"] == 1
    assert rows[1]["applicability_status"] == "NOT_APPLICABLE"
