from __future__ import annotations

import math
from pathlib import Path

from portfolio_layer.levels.levels_common import (
    build_valuation_contract_row,
    valuation_range,
)


AS_OF = "2026-07-31"
SOURCE_PATH = Path("unused_when_source_date_is_present.csv")
POLICY = {
    "allow_ttm_fcf_per_share_reconstruction": True,
    "ttm_fcf_reconstruction_pipelines": [
        "semiconductors",
        "software_infrastructure",
        "technology_hardware",
    ],
    "maximum_source_fcf_yield": 1.0,
    "sector_specialist_method_allowlist": {
        "biotech": ["probability_weighted_pipeline_value"],
    },
}
MULTIPLES = {
    "default": {"pe": 18.0, "fcf_yield": 0.06, "ev_ebitda": 10.0},
    "stable_profitable": {
        "pe": 20.0,
        "fcf_yield": 0.05,
        "ev_ebitda": 12.0,
    },
    "unprofitable_growth": {"pe": 0.0, "fcf_yield": 0.0, "ev_ebitda": 0.0},
}


def _contract(
    *,
    ticker: str = "TEST",
    pipeline: str = "semiconductors",
    raw: dict[str, object],
) -> dict[str, object]:
    return build_valuation_contract_row(
        as_of=AS_OF,
        ticker=ticker,
        source_pipeline=pipeline,
        raw=raw,
        source_path=SOURCE_PATH,
        source_sha="a" * 64,
        valuation_policy=POLICY,
    )


def test_ttm_fcf_yield_reconstructs_numerator_not_price_anchor() -> None:
    contract = _contract(
        raw={
            "asof_date": AS_OF,
            "latest_price": 100.0,
            "fcf_yield": 0.05,
            "fcf_margin": 0.20,
        }
    )

    assert contract["contract_status"] == "valid"
    assert contract["fcf_per_share_ttm"] == 5.0
    assert "latest_price" not in contract
    values, low, base, high, disagreement, confidence = valuation_range(
        contract, MULTIPLES
    )
    assert values == {"fcf_yield_ttm": 100.0}
    assert (low, base, high, disagreement, confidence) == (
        100.0,
        100.0,
        100.0,
        0.0,
        0.6,
    )


def test_ttm_fcf_reconstruction_is_pipeline_and_unit_bounded() -> None:
    defense = _contract(
        pipeline="defense",
        raw={"asof_date": AS_OF, "latest_price": 100.0, "fcf_yield": 0.05},
    )
    percent_scaled = _contract(
        raw={"asof_date": AS_OF, "latest_price": 100.0, "fcf_yield": 5.0}
    )

    assert defense["contract_status"] == "invalid"
    assert percent_scaled["contract_status"] == "invalid"
    assert defense["contract_reason"] == "no_supported_absolute_valuation_method"
    assert percent_scaled["fcf_per_share_ttm"] is None


def test_ttm_fcf_reconstruction_rejects_future_price_component() -> None:
    contract = _contract(
        raw={
            "asof_date": AS_OF,
            "price_data_asof_date": "2026-08-01",
            "latest_price": 100.0,
            "fcf_yield": 0.05,
        }
    )

    assert contract["contract_status"] == "invalid"
    assert contract["fcf_per_share_ttm"] is None


def test_specialist_range_is_allowlisted_pit_and_preserved() -> None:
    contract = _contract(
        pipeline="biotech",
        raw={
            "asof_date": "2026-07-29",
            "sector_valuation_low": 20.0,
            "sector_valuation_base": 30.0,
            "sector_valuation_high": 50.0,
            "sector_valuation_method": "probability_weighted_pipeline_value",
            "sector_valuation_confidence": 0.8,
            "sector_valuation_available_at_utc": "2026-07-30T20:00:00+00:00",
        },
    )

    assert contract["contract_status"] == "valid"
    assert contract["available_at_utc"] == "2026-07-30T20:00:00+00:00"
    values, low, base, high, disagreement, confidence = valuation_range(
        contract, MULTIPLES
    )
    assert values == {
        "sector_specialist:probability_weighted_pipeline_value": 30.0
    }
    assert (low, base, high) == (20.0, 30.0, 50.0)
    assert math.isclose(disagreement, 1.0)
    assert math.isclose(confidence, 0.3)


def test_specialist_range_fails_closed_when_future_or_incomplete() -> None:
    future = _contract(
        pipeline="biotech",
        raw={
            "asof_date": AS_OF,
            "sector_valuation_low": 20.0,
            "sector_valuation_base": 30.0,
            "sector_valuation_high": 40.0,
            "sector_valuation_method": "probability_weighted_pipeline_value",
            "sector_valuation_confidence": 0.8,
            "sector_valuation_available_at_utc": "2026-08-01T00:00:00+00:00",
        },
    )
    incomplete = _contract(
        pipeline="biotech",
        raw={
            "asof_date": AS_OF,
            "sector_valuation_base": 30.0,
            "sector_valuation_method": "probability_weighted_pipeline_value",
        },
    )

    assert future["contract_status"] == "invalid"
    assert future["contract_reason"] == "sector_specialist_available_after_as_of"
    assert valuation_range(future, MULTIPLES)[1:] == (
        None,
        None,
        None,
        0.0,
        0.0,
    )
    assert incomplete["contract_status"] == "invalid"
    assert incomplete["contract_reason"] == "incomplete_sector_specialist_valuation"


def test_direct_market_price_cannot_create_a_valuation() -> None:
    contract = _contract(raw={"asof_date": AS_OF, "latest_price": 100.0})

    assert contract["contract_status"] == "invalid"
    assert contract["method_allowlist"] == "[]"
    assert contract["contract_reason"] == "no_supported_absolute_valuation_method"
