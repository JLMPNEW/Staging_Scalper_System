from __future__ import annotations

import copy

import pytest

from portfolio_layer.core.capital_reservation import (
    consumer_defensive_reserved_cash_fraction,
)


COHORTS = (
    "beverages",
    "consumer_staples_distribution_retail",
    "household_personal_tobacco",
    "packaged_foods_agricultural_products",
)


def _config() -> dict:
    caps = {cohort: 0.03125 for cohort in COHORTS}
    return {
        "score_contract": {
            "sectors": [
                {
                    "model_family": "consumer_defensive",
                    "enabled": True,
                    "optimizer_sector_cap": 0.125,
                    "optimizer_cap_by_scope": dict(caps),
                }
            ]
        },
        "optimizer": {
            "sector_weight_caps": {"consumer_defensive": 0.125},
            "scope_weight_caps": {"consumer_defensive": dict(caps)},
        },
    }


def test_no_cash_reservation_when_all_four_cohorts_are_active() -> None:
    assert consumer_defensive_reserved_cash_fraction(_config()) == pytest.approx(0.0)


def test_failed_cohort_equal_slot_is_reserved_as_cash() -> None:
    config = _config()
    failed = "packaged_foods_agricultural_products"
    config["score_contract"]["sectors"][0]["optimizer_cap_by_scope"][failed] = 0.0
    config["optimizer"]["scope_weight_caps"]["consumer_defensive"][failed] = 0.0

    assert consumer_defensive_reserved_cash_fraction(config) == pytest.approx(0.03125)


def test_disabled_consumer_does_not_reserve_cash() -> None:
    config = _config()
    config["score_contract"]["sectors"][0]["enabled"] = False

    assert consumer_defensive_reserved_cash_fraction(config) == pytest.approx(0.0)


def test_divergent_scope_cap_surfaces_fail_closed() -> None:
    config = _config()
    config["optimizer"]["scope_weight_caps"]["consumer_defensive"]["beverages"] = 0.02

    with pytest.raises(ValueError, match="scope cap diverges"):
        consumer_defensive_reserved_cash_fraction(config)


def test_active_authority_cannot_exceed_sector_cap() -> None:
    config = copy.deepcopy(_config())
    config["score_contract"]["sectors"][0]["optimizer_cap_by_scope"]["beverages"] = 0.04
    config["optimizer"]["scope_weight_caps"]["consumer_defensive"]["beverages"] = 0.04

    with pytest.raises(ValueError, match="exceeds its sector cap"):
        consumer_defensive_reserved_cash_fraction(config)
