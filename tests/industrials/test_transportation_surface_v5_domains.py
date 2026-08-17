from __future__ import annotations

import importlib.util
import sys
import csv
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (
    PROJECT_ROOT
    / "industrials"
    / "transportation"
    / "scripts"
    / "38c_audit_transportation_surface_v5_domain_coverage.py"
)


def _module():
    spec = importlib.util.spec_from_file_location("transportation_surface_v5_domains", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v5_domain_and_source_maps_are_exact_and_cover_24_names() -> None:
    module = _module()
    rules = module.validate_maps(module.DEFAULT_MAPPING, module.DEFAULT_SOURCE_MAP)
    assert len(module.SURFACE_TICKERS) == 24
    assert set(module.NEW_TICKERS) <= set(module.SURFACE_TICKERS)
    assert rules
    truckload_operating = next(
        rule
        for rule in rules
        if rule.metric_id == "operating_ratio"
        and rule.domain_id == "truckload_intermodal"
    )
    assert set(module.NEW_TICKERS) - {"FWRD"} <= set(truckload_operating.tickers)
    assert truckload_operating.minimum_breadth == 6


def test_fordward_air_is_scoped_to_asset_light_domains() -> None:
    module = _module()
    rules = module.validate_maps(module.DEFAULT_MAPPING, module.DEFAULT_SOURCE_MAP)
    fwrd_domains = {
        rule.domain_id for rule in rules if "FWRD" in rule.tickers
    }
    assert fwrd_domains == {"asset_light_logistics"}


def test_v5_candidate_policy_partitions_catalog_but_is_not_active() -> None:
    from industrials.transportation.investable_universe import (
        CANDIDATE_POLICY_VERSION,
        load_investable_universe_policy,
        validate_investable_universe_policy,
    )

    policy_path = (
        PROJECT_ROOT
        / "industrials"
        / "transportation"
        / "data"
        / "transportation_investable_universe_v5.yaml"
    )
    policy = load_investable_universe_policy(policy_path)
    errors, summary = validate_investable_universe_policy(policy)
    assert policy.policy_version == CANDIDATE_POLICY_VERSION
    assert [len(group.tickers) for group in policy.groups] == [24, 11]
    assert len(policy.selected_tickers) == 35
    assert errors == []
    assert summary["acceptance"] == "PASS"
    assert summary["selected_count"] == 35
    assert summary["excluded_count"] == 85
    config_text = (PROJECT_ROOT / "industrials" / "config.yaml").read_text(
        encoding="utf-8"
    )
    assert "transportation_investable_universe_v4.yaml" in config_text
    assert "transportation_investable_universe_v5.yaml" not in config_text

    with policy.positioning_universe_path.open(
        "r", encoding="utf-8-sig", newline=""
    ) as handle:
        positioned = {row["ticker"] for row in csv.DictReader(handle)}
    assert set(module.NEW_TICKERS if (module := _module()) else ()) <= positioned
