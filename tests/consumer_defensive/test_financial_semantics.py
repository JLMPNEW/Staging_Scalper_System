from __future__ import annotations

from datetime import date, timedelta

import pytest

from consumer_defensive.core.financial_semantics import (
    FinancialFact,
    FinancialValue,
    FxRateObservation,
    RedenominationExemption,
    classify_fx_daily_rates,
    construct_financial_ratios,
    normalize_capex_payment,
    select_revenue_candidate,
    select_safe_flow_value,
)


ACCESSION = "0000000000-25-000001"
START = "2024-01-01"
END = "2024-12-31"
TAXONOMY = "us-gaap-2024"


def _fact(
    metric: str,
    value: float,
    *,
    concept: str | None = None,
    currency: str = "USD",
    accession: str = ACCESSION,
    start: str = START,
    end: str = END,
    taxonomy: str = TAXONOMY,
    raw_fact_id: str | None = None,
) -> FinancialFact:
    return FinancialFact(
        metric=metric,
        value=value,
        period_start=start,
        period_end=end,
        taxonomy=taxonomy,
        currency=currency,
        accession_number=accession,
        concept=concept,
        raw_fact_id=raw_fact_id,
    )


def _value(
    metric: str,
    value: float,
    *,
    start: str | None = START,
    end: str = END,
    taxonomy: str = TAXONOMY,
    currency: str = "USD",
) -> FinancialValue:
    return FinancialValue(
        metric=metric,
        value=value,
        period_start=start,
        period_end=end,
        taxonomy=taxonomy,
        currency=currency,
        basis="direct_annual",
    )


def _complete_ratio_inputs() -> dict[str, FinancialValue]:
    return {
        "revenue": _value("revenue", 1_000.0),
        "gross_profit": _value("gross_profit", 400.0),
        "cost_of_revenue": _value("cost_of_revenue", 600.0),
        "operating_income": _value("operating_income", 150.0),
        "operating_cash_flow": _value("operating_cash_flow", 180.0),
        "capex": _value("capex", 50.0),
        "pretax_income": _value("pretax_income", 140.0),
        "income_tax_expense": _value("income_tax_expense", 28.0),
        "depreciation_and_amortization": _value("depreciation_and_amortization", 30.0),
        "cash": _value("cash", 100.0, start=None),
        "equity": _value("equity", 500.0, start=None),
        "debt_current": _value("debt_current", 50.0, start=None),
        "debt_noncurrent": _value("debt_noncurrent", 250.0, start=None),
        "inventory": _value("inventory", 120.0, start=None),
        "inventory_average": _value("inventory_average", 120.0, start=None),
        "cash_average": _value("cash_average", 100.0, start=None),
        "equity_average": _value("equity_average", 500.0, start=None),
        "debt_current_average": _value("debt_current_average", 50.0, start=None),
        "debt_noncurrent_average": _value("debt_noncurrent_average", 250.0, start=None),
    }


def test_ande_revenue_is_selected_by_same_context_gross_profit_identity() -> None:
    # ANDE-style filings expose multiple plausible top-line concepts.  Magnitude
    # alone favors neither safely; the exact identity identifies Revenues.
    candidates = [
        _fact(
            "revenue",
            9_700_000_000.0,
            concept="SalesRevenueNet",
            raw_fact_id="ande-sales-revenue-net",
        ),
        _fact(
            "revenue",
            13_000_000_000.0,
            concept="Revenues",
            raw_fact_id="ande-revenues",
        ),
    ]
    result = select_revenue_candidate(
        candidates,
        cost_of_revenue=_fact(
            "cost_of_revenue",
            -11_800_000_000.0,
            concept="CostOfRevenue",
            raw_fact_id="ande-cogs",
        ),
        gross_profit=_fact(
            "gross_profit",
            1_200_000_000.0,
            concept="GrossProfit",
            raw_fact_id="ande-gross-profit",
        ),
    )

    assert result.status == "selected"
    assert result.selected is candidates[1]
    assert result.scores[0].absolute_residual == 0.0
    assert result.normalized_cost_of_revenue == 11_800_000_000.0
    assert "cost_of_revenue_sign_normalized" in result.quality_flags
    assert result.lineage == ("ande-revenues", "ande-sales-revenue-net")


def test_revenue_identity_tie_remains_ambiguous_with_full_lineage() -> None:
    candidates = [
        _fact("revenue", 100.0, concept="RevenueFromContractWithCustomer", raw_fact_id="candidate-a"),
        _fact("revenue", 100.0, concept="Revenues", raw_fact_id="candidate-b"),
    ]
    result = select_revenue_candidate(
        candidates,
        cost_of_revenue=_fact("cost_of_revenue", 70.0),
        gross_profit=_fact("gross_profit", 30.0),
    )

    assert result.status == "ambiguous_identity_tie"
    assert result.selected is None
    assert set(result.lineage) == {"candidate-a", "candidate-b"}
    assert "accounting_identity_tie" in result.quality_flags


def test_pep_style_multi_currency_revenue_candidates_require_explicit_currency() -> None:
    candidates = [
        _fact("revenue", 100.0, currency="USD", raw_fact_id="pep-usd"),
        _fact("revenue", 137.0, currency="CAD", raw_fact_id="pep-cad"),
    ]
    references = {
        "cost_of_revenue": _fact("cost_of_revenue", 60.0, currency="USD"),
        "gross_profit": _fact("gross_profit", 40.0, currency="USD"),
    }

    rejected = select_revenue_candidate(candidates, **references)
    accepted = select_revenue_candidate(candidates, reporting_currency="USD", **references)

    assert rejected.status == "ambiguous_currency_context"
    assert rejected.selected is None
    assert accepted.status == "selected"
    assert accepted.selected is candidates[0]
    assert "candidate_context_rejected" in accepted.quality_flags


def test_revenue_reference_facts_cannot_mix_accessions_or_periods() -> None:
    result = select_revenue_candidate(
        [_fact("revenue", 100.0)],
        cost_of_revenue=_fact("cost_of_revenue", 60.0),
        gross_profit=_fact("gross_profit", 40.0, accession="0000000000-25-000002"),
    )

    assert result.status == "reference_context_mismatch"
    assert result.selected is None


def test_negative_capex_payment_is_normalized_without_losing_reported_value() -> None:
    result = normalize_capex_payment(-125.5, "PaymentsToAcquirePropertyPlantAndEquipment")

    assert result.reported_value == -125.5
    assert result.normalized_value == 125.5
    assert result.sign_changed is True
    assert result.method == "absolute_value_of_negative_payment"


def test_capex_normalization_fails_closed_for_nonpayment_concept() -> None:
    with pytest.raises(ValueError, match="unsupported capex payment concept"):
        normalize_capex_payment(25.0, "ProceedsFromSaleOfPropertyPlantAndEquipment")


def test_clp_fx_spike_is_quarantined_by_trailing_median_and_mad() -> None:
    first_day = date(2025, 1, 1)
    observations = [
        FxRateObservation("CLP", first_day + timedelta(days=index), 0.00105 + index * 0.0000002)
        for index in range(10)
    ]
    observations.append(FxRateObservation("CLP", first_day + timedelta(days=10), 0.20))

    decisions = classify_fx_daily_rates(observations, window=10, minimum_history=5)
    spike = decisions[-1]

    assert spike.status == "quarantined_outlier"
    assert spike.is_usable is False
    assert spike.robust_z is not None and spike.robust_z > 8.0
    assert spike.relative_deviation is not None and spike.relative_deviation > 0.35


def test_explicit_redenomination_exemption_overrides_fx_quarantine() -> None:
    first_day = date(2025, 1, 1)
    observations = [
        FxRateObservation("CLP", first_day + timedelta(days=index), 0.00105 + index * 0.0000002)
        for index in range(10)
    ]
    event_date = first_day + timedelta(days=10)
    observations.append(FxRateObservation("CLP", event_date, 0.20))

    decisions = classify_fx_daily_rates(
        observations,
        window=10,
        minimum_history=5,
        exemptions=(
            RedenominationExemption(
                "CLP",
                event_date,
                event_date,
                "documented test redenomination",
            ),
        ),
    )

    assert decisions[-1].status == "redenomination_exempt"
    assert decisions[-1].is_usable is True
    assert "documented test redenomination" in decisions[-1].reason


def test_all_aligned_financial_inputs_construct_ratios() -> None:
    result = construct_financial_ratios(_complete_ratio_inputs())

    assert result.ratios["gross_margin"].value == pytest.approx(0.4)
    assert result.ratios["operating_margin"].value == pytest.approx(0.15)
    assert result.ratios["free_cash_flow_margin"].value == pytest.approx(0.13)
    assert result.ratios["inventory_turnover"].value == pytest.approx(5.0)


def test_aci_style_stale_capex_nulls_free_cash_flow_margin() -> None:
    values = _complete_ratio_inputs()
    values["capex"] = _value("capex", 50.0, start="2020-01-01", end="2020-12-31")

    outcome = construct_financial_ratios(values).ratios["free_cash_flow_margin"]

    assert outcome.value is None
    assert "period_end_mismatch:capex" in outcome.quality_flags
    assert "period_start_mismatch:capex" in outcome.quality_flags


def test_wmt_style_stale_depreciation_nulls_net_debt_to_ebitda() -> None:
    values = _complete_ratio_inputs()
    values["depreciation_and_amortization"] = _value(
        "depreciation_and_amortization",
        30.0,
        start="2018-02-01",
        end="2019-01-31",
    )

    outcome = construct_financial_ratios(values).ratios["net_debt_to_ebitda"]

    assert outcome.value is None
    assert "period_end_mismatch:depreciation_and_amortization" in outcome.quality_flags


def test_syy_style_stale_inventory_nulls_inventory_turnover() -> None:
    values = _complete_ratio_inputs()
    values["inventory_average"] = _value("inventory_average", 120.0, start=None, end="2011-06-30")

    outcome = construct_financial_ratios(values).ratios["inventory_turnover"]

    assert outcome.value is None
    assert "period_end_mismatch:inventory_average" in outcome.quality_flags


def test_sam_style_stale_debt_nulls_net_debt_to_ebitda() -> None:
    values = _complete_ratio_inputs()
    values["debt_current"] = _value("debt_current", 50.0, start=None, end="2014-12-31")
    values["debt_noncurrent"] = _value("debt_noncurrent", 250.0, start=None, end="2014-12-31")

    outcome = construct_financial_ratios(values).ratios["net_debt_to_ebitda"]

    assert outcome.value is None
    assert "period_end_mismatch:debt_current" in outcome.quality_flags
    assert "period_end_mismatch:debt_noncurrent" in outcome.quality_flags


def test_tpb_style_stale_revenue_cannot_combine_with_current_profit_flows() -> None:
    values = _complete_ratio_inputs()
    values["revenue"] = _value("revenue", 1_000.0, start="2016-01-01", end="2016-12-31")

    result = construct_financial_ratios(values)

    assert result.ratios["gross_margin"].value is None
    assert "period_end_mismatch:gross_profit" in result.ratios["gross_margin"].quality_flags
    assert result.ratios["operating_margin"].value is None
    assert "period_end_mismatch:operating_income" in result.ratios["operating_margin"].quality_flags


def test_ratios_reject_taxonomy_and_currency_mismatches() -> None:
    values = _complete_ratio_inputs()
    values["gross_profit"] = _value("gross_profit", 400.0, currency="CAD")
    values["operating_income"] = _value("operating_income", 150.0, taxonomy="ifrs-full-2024")

    result = construct_financial_ratios(values)

    assert result.ratios["gross_margin"].value is None
    assert "currency_mismatch:gross_profit" in result.ratios["gross_margin"].quality_flags
    assert result.ratios["operating_margin"].value is None
    assert "taxonomy_mismatch:operating_income" in result.ratios["operating_margin"].quality_flags


def test_four_nonoverlapping_quarters_form_safe_ttm() -> None:
    facts = [
        _fact("revenue", 20.0, start="2024-01-01", end="2024-03-31", raw_fact_id="q1"),
        _fact("revenue", 22.0, start="2024-04-01", end="2024-06-30", raw_fact_id="q2"),
        _fact("revenue", 24.0, start="2024-07-01", end="2024-09-30", raw_fact_id="q3"),
        _fact("revenue", 26.0, start="2024-10-01", end="2024-12-31", raw_fact_id="q4"),
    ]

    result = select_safe_flow_value(facts)

    assert result.status == "selected_ttm"
    assert result.selected is not None
    assert result.selected.value == 92.0
    assert result.selected.basis == "ttm_four_quarters"
    assert result.selected.lineage == ("q1", "q2", "q3", "q4")


def test_stale_annual_is_not_used_when_newer_interim_cannot_form_ttm() -> None:
    facts = [
        _fact("revenue", 100.0, start="2023-01-01", end="2023-12-31", raw_fact_id="annual"),
        _fact("revenue", 30.0, start="2024-01-01", end="2024-03-31", raw_fact_id="new-quarter"),
    ]

    result = select_safe_flow_value(facts)

    assert result.status == "unreconciled_newer_interim"
    assert result.selected is None
    assert "direct_annual_is_stale_relative_to_newer_interim" in result.quality_flags


def test_annual_plus_current_minus_comparable_prior_builds_safe_ttm_bridge() -> None:
    facts = [
        _fact("revenue", 100.0, start="2023-01-01", end="2023-12-31", raw_fact_id="annual"),
        _fact("revenue", 28.0, start="2024-01-01", end="2024-03-31", raw_fact_id="current-q1"),
        _fact("revenue", 25.0, start="2023-01-01", end="2023-03-31", raw_fact_id="prior-q1"),
    ]

    result = select_safe_flow_value(facts)

    assert result.status == "selected_ttm_bridge"
    assert result.selected is not None
    assert result.selected.value == 103.0
    assert result.selected.period_end == "2024-03-31"
    assert result.selected.basis == "ttm_annual_plus_current_minus_prior"
    assert result.selected.lineage == ("annual", "current-q1", "prior-q1")
