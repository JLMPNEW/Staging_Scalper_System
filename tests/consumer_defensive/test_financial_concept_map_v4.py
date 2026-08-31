from __future__ import annotations

from pathlib import Path

import yaml
from consumer_defensive.core.financial_semantics import normalize_capex_payment

from consumer_defensive.core.stage4 import _concept_index


ROOT = Path(__file__).resolve().parents[2]
CONCEPT_MAP = (
    ROOT
    / "consumer_defensive"
    / "data"
    / "consumer_defensive_financial_concept_map.yaml"
)


def _load_concept_map() -> dict[str, object]:
    payload = yaml.safe_load(CONCEPT_MAP.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_v4_maps_current_us_gaap_and_ifrs_revenue_concepts() -> None:
    payload = _load_concept_map()

    assert payload["definition_version"] == "consumer_defensive_financial_v4"
    concept_index = _concept_index(payload)
    assert concept_index[
        "RevenueFromContractWithCustomerIncludingAssessedTax"
    ][:3] == ("revenue", "income", "total")
    assert concept_index["RevenueFromContractsWithCustomers"][:3] == (
        "revenue",
        "income",
        "total",
    )


def test_v4_keeps_specific_revenue_concepts_ahead_of_generic_fallbacks() -> None:
    concept_index = _concept_index(_load_concept_map())

    excluding_priority = concept_index[
        "RevenueFromContractWithCustomerExcludingAssessedTax"
    ][3]
    including_priority = concept_index[
        "RevenueFromContractWithCustomerIncludingAssessedTax"
    ][3]
    ifrs_contract_priority = concept_index["RevenueFromContractsWithCustomers"][3]
    generic_priorities = [
        concept_index[concept][3]
        for concept in ("Revenues", "SalesRevenueNet", "Revenue")
    ]

    assert excluding_priority < including_priority < min(generic_priorities)
    assert ifrs_contract_priority < min(generic_priorities)


def test_v4_maps_exact_ifrs_capex_before_generic_ifrs_fallback() -> None:
    concept_index = _concept_index(_load_concept_map())
    exact = concept_index[
        "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"
    ]
    generic = concept_index["PurchaseOfPropertyPlantAndEquipment"]

    assert exact[:3] == ("capital_expenditures", "cash_flow", "total")
    assert exact[3] < generic[3]


def test_v4_uses_final_operating_cash_flow_and_rejects_ifrs_intermediate() -> None:
    payload = _load_concept_map()
    concepts = payload["metrics"]["operating_cash_flow"]["concepts"]

    assert concepts == [
        "NetCashProvidedByUsedInOperatingActivities",
        "CashFlowsFromUsedInOperatingActivities",
    ]
    assert "CashFlowsFromUsedInOperations" not in concepts


def test_exact_ifrs_capex_is_normalized_as_one_positive_cash_use() -> None:
    concept = "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"

    positive = normalize_capex_payment(551_000_000, concept)
    negative = normalize_capex_payment(-551_000_000, concept)

    assert positive.normalized_value == 551_000_000
    assert positive.method == "reported_positive_payment_magnitude"
    assert positive.sign_changed is False
    assert negative.normalized_value == 551_000_000
    assert negative.method == "absolute_value_of_negative_payment"
    assert negative.sign_changed is True
