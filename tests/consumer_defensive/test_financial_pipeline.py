from __future__ import annotations

import json

import pytest

from consumer_defensive.core.financial_pipeline import (
    build_financial_feature_bundle,
    select_canonical_financial_facts,
)


CONCEPT_INDEX = {
    "RevenueFromContractWithCustomerExcludingAssessedTax": ("revenue", "income", "total", 0),
    "Revenues": ("revenue", "income", "total", 1),
    "CostOfRevenue": ("cost_of_revenue", "income", "total", 0),
    "GrossProfit": ("gross_profit", "income", "total", 0),
    "LongTermDebtCurrent": ("debt_current", "balance_sheet", "current_maturities", 0),
    "PaymentsToAcquirePropertyPlantAndEquipment": ("capital_expenditures", "cash_flow", "total", 0),
}


def raw_fact(
    raw_fact_id: int,
    concept: str,
    value: float,
    *,
    unit: str = "USD",
    start: str | None = "2026-01-01",
    end: str = "2026-06-30",
    accession: str = "0000821026-26-000122",
) -> dict[str, object]:
    return {
        "raw_fact_id": raw_fact_id,
        "ticker": "ANDE",
        "accession_number": accession,
        "taxonomy": "us-gaap",
        "concept": concept,
        "numeric_value": value,
        "unit": unit,
        "period_start": start,
        "period_end": end,
        "accepted_at": "2026-08-04T20:15:09Z",
    }


def test_pipeline_selects_ande_consolidated_revenue_by_identity() -> None:
    result = select_canonical_financial_facts(
        [
            raw_fact(1, "RevenueFromContractWithCustomerExcludingAssessedTax", 934_356_000),
            raw_fact(2, "Revenues", 5_724_926_000),
            raw_fact(3, "CostOfRevenue", 5_340_612_000),
            raw_fact(4, "GrossProfit", 384_314_000),
        ],
        concept_index=CONCEPT_INDEX,
        supported_currencies={"USD"},
    )

    revenue = next(row for row in result.decisions if row.metric == "revenue")
    assert revenue.source_concept == "Revenues"
    assert revenue.normalized_value == 5_724_926_000
    assert revenue.selection_method == "gross_profit_identity"
    assert result.audit_counts["revenue_identity_selected"] == 1


def test_filing_currency_plurality_rejects_pep_note_currencies() -> None:
    rows = [
        raw_fact(1, "Revenues", 10_000, unit="USD"),
        raw_fact(2, "CostOfRevenue", 6_000, unit="USD"),
        raw_fact(3, "GrossProfit", 4_000, unit="USD"),
        raw_fact(4, "LongTermDebtCurrent", 1_300, unit="USD", start=None),
        raw_fact(5, "LongTermDebtCurrent", 1_000, unit="EUR", start=None),
        raw_fact(6, "LongTermDebtCurrent", 800, unit="CAD", start=None),
    ]
    result = select_canonical_financial_facts(
        rows,
        concept_index=CONCEPT_INDEX,
        supported_currencies={"USD", "EUR", "CAD"},
    )
    debt = next(row for row in result.decisions if row.metric == "debt_current")
    assert debt.reported_currency == "USD"
    assert debt.reported_value == 1_300
    assert result.audit_counts["non_dominant_context_rejected"] == 2

    tied = select_canonical_financial_facts(
        rows[3:],
        concept_index=CONCEPT_INDEX,
        supported_currencies={"USD", "EUR", "CAD"},
    )
    assert tied.decisions == ()
    assert tied.audit_counts["ambiguous_reporting_currency"] == 3


def test_pipeline_preserves_and_normalizes_negative_capex() -> None:
    result = select_canonical_financial_facts(
        [raw_fact(7, "PaymentsToAcquirePropertyPlantAndEquipment", -125.5)],
        concept_index=CONCEPT_INDEX,
        supported_currencies={"USD"},
    )
    capex = result.decisions[0]
    assert capex.reported_value == -125.5
    assert capex.normalized_value == 125.5
    assert capex.sign_normalization_method == "absolute_value_of_negative_payment"
    assert "capex_sign_normalized" in capex.quality_flags


def canonical_row(
    raw_id: int,
    metric: str,
    value: float,
    *,
    start: str | None,
    end: str,
    component: str = "total",
) -> dict[str, object]:
    return {
        "canonical_metric": metric,
        "canonical_component": component,
        "accession_number": f"accession-{end}",
        "taxonomy": "us-gaap",
        "source_concept": metric,
        "period_start": start,
        "period_end": end,
        "accepted_at": "2025-02-20T16:30:00Z",
        "frequency": "instant" if start is None else "annual",
        "value_usd": value,
        "reported_currency": "USD",
        "source_raw_fact_id": raw_id,
        "quality_flags_json": "[]",
    }


def test_stale_capex_cannot_mix_with_current_revenue_and_ocf() -> None:
    rows = [
        canonical_row(1, "revenue", 1_000, start="2024-01-01", end="2024-12-31"),
        canonical_row(2, "operating_cash_flow", 200, start="2024-01-01", end="2024-12-31"),
        canonical_row(3, "capital_expenditures", 40, start="2020-01-01", end="2020-12-31"),
    ]
    feature = build_financial_feature_bundle(
        rows,
        as_of="2025-03-01",
        listing_start_date="2010-01-01",
        listing_end_date=None,
        maximum_period_age_days=550,
    )
    assert feature.values["revenue_ttm_usd"] == 1_000
    assert feature.values["free_cash_flow_margin"] is None
    assert "period_end_mismatch:capex" in feature.quality_reasons
    assert "available_but_context_mismatched:capex" in feature.quality_reasons
    assert "missing_input:capex" in feature.quality_reasons
    assert "capex" not in feature.lineage["selected_flow_lineage"]
    rejected = feature.lineage["rejected_flow_lineage"]["capex"]
    assert rejected[0]["period_end"] == "2020-12-31"
    assert rejected[0]["quality_flags"] == [
        "period_end_mismatch:capex",
        "period_start_mismatch:capex",
    ]
    assert feature.quality_status == "partial"


def test_truly_missing_capex_has_no_rejected_source_lineage() -> None:
    rows = [
        canonical_row(1, "revenue", 1_000, start="2024-01-01", end="2024-12-31"),
        canonical_row(2, "operating_cash_flow", 200, start="2024-01-01", end="2024-12-31"),
    ]
    feature = build_financial_feature_bundle(
        rows,
        as_of="2025-03-01",
        listing_start_date="2010-01-01",
        listing_end_date=None,
        maximum_period_age_days=550,
    )

    assert feature.values["free_cash_flow_margin"] is None
    assert "missing_input:capex" in feature.quality_reasons
    assert "period_end_mismatch:capex" not in feature.quality_reasons
    assert "available_but_context_mismatched:capex" not in feature.quality_reasons
    assert "capex" not in feature.lineage["rejected_flow_lineage"]


def test_pipeline_uses_average_balances_and_persists_basis_lineage() -> None:
    rows = [
        canonical_row(1, "revenue", 1_000, start="2024-01-01", end="2024-12-31"),
        canonical_row(2, "gross_profit", 400, start="2024-01-01", end="2024-12-31"),
        canonical_row(3, "cost_of_revenue", 600, start="2024-01-01", end="2024-12-31"),
        canonical_row(4, "operating_income", 150, start="2024-01-01", end="2024-12-31"),
        canonical_row(5, "operating_cash_flow", 180, start="2024-01-01", end="2024-12-31"),
        canonical_row(6, "capital_expenditures", 50, start="2024-01-01", end="2024-12-31"),
        canonical_row(7, "pretax_income", 140, start="2024-01-01", end="2024-12-31"),
        canonical_row(8, "income_tax_expense", 28, start="2024-01-01", end="2024-12-31"),
        canonical_row(9, "depreciation_amortization", 30, start="2024-01-01", end="2024-12-31"),
    ]
    balances = {"cash": 100, "equity": 500, "debt_current": 50, "debt_noncurrent": 250, "inventory": 120}
    raw_id = 10
    for end in ("2023-12-31", "2024-12-31"):
        for metric, value in balances.items():
            rows.append(canonical_row(raw_id, metric, value, start=None, end=end))
            raw_id += 1

    feature = build_financial_feature_bundle(
        rows,
        as_of="2025-03-01",
        listing_start_date="2010-01-01",
        listing_end_date=None,
        maximum_period_age_days=550,
    )
    assert feature.values["return_on_invested_capital"] == pytest.approx(120 / 700)
    assert feature.values["inventory_turnover"] == pytest.approx(5.0)
    assert feature.values["net_debt_to_ebitda"] == pytest.approx(200 / 180)
    assert feature.quality_status == "complete"
    assert feature.basis_period_end == "2024-12-31"
    assert len(feature.lineage["instant_lineage"]["inventory_average"]) == 2
    json.dumps(feature.lineage, sort_keys=True)


def test_feature_snapshot_is_ineligible_after_listing_end() -> None:
    feature = build_financial_feature_bundle(
        [canonical_row(1, "revenue", 1_000, start="2019-01-01", end="2019-12-31")],
        as_of="2025-03-01",
        listing_start_date="2010-01-01",
        listing_end_date="2020-09-30",
        maximum_period_age_days=550,
    )
    assert feature.quality_status == "ineligible"
    assert set(feature.values.values()) == {None}
    assert feature.quality_reasons == ("after_listing_end",)
