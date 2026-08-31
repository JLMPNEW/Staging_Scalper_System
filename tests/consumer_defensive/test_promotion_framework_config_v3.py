from __future__ import annotations

import copy
import importlib
from pathlib import Path

import pytest

from consumer_defensive.core.config import cfg_get, load_config, resolve_path, validate_config


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "consumer_defensive/config.yaml"


def test_v3_framework_is_wired_to_pinned_standard_allocation() -> None:
    bundle = load_config(CONFIG)
    config = bundle.payload
    assert cfg_get(config, "promotion_framework_v3") == {
        "framework_path": "data/consumer_defensive_promotion_framework_v3.yaml",
        "engine_module": "consumer_defensive.core.promotion_engine_v3",
        "status": "active_standard_allocation_pinned_registry",
        "portfolio_activation_requires_pinned_registry": True,
    }
    framework_path = resolve_path(
        cfg_get(config, "promotion_framework_v3.framework_path"),
        base_dir=bundle.base_dir,
    )
    assert framework_path.is_file()
    engine = importlib.import_module(cfg_get(config, "promotion_framework_v3.engine_module"))
    assert callable(engine.build_promotion_decision)
    assert callable(engine.build_activation_registry)

    assert cfg_get(config, "portfolio_layer.promotion_state") == "active"
    assert cfg_get(config, "portfolio_layer.enabled") is True
    assert cfg_get(config, "portfolio_layer.required") is True
    assert cfg_get(config, "portfolio_layer.sector_weight_cap") == 0.125
    publisher = cfg_get(config, "production_score_publisher_v3")
    assert publisher["entry_lag_trading_sessions"] == 1
    assert set(publisher["selected_candidate_id_by_cohort"]) == {
        "beverages",
        "consumer_staples_distribution_retail",
        "household_personal_tobacco",
        "packaged_foods_agricultural_products",
    }


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("framework_path", "data/other.yaml"),
        ("engine_module", "another_sector.promotion_engine"),
        ("status", "production"),
        ("portfolio_activation_requires_pinned_registry", False),
    ],
)
def test_v3_framework_contract_rejects_drift(key: str, value: object) -> None:
    config = copy.deepcopy(load_config(CONFIG).payload)
    config["promotion_framework_v3"][key] = value
    with pytest.raises(ValueError, match=rf"promotion_framework_v3\.{key}"):
        validate_config(config)


def test_v3_framework_rejects_unknown_nested_keys() -> None:
    config = copy.deepcopy(load_config(CONFIG).payload)
    config["promotion_framework_v3"]["auto_activate"] = True
    with pytest.raises(
        ValueError,
        match="Unknown Consumer Defensive config keys under promotion_framework_v3",
    ):
        validate_config(config)
