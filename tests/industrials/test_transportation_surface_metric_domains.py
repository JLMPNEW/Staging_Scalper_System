from __future__ import annotations

import csv
import importlib
import math
from collections import Counter, defaultdict
from pathlib import Path

from industrials.transportation.investable_universe import (
    SURFACE_DOMAIN_POLICY_VERSION,
    load_investable_universe_policy,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = PROJECT_ROOT / "industrials" / "transportation" / "data"
POLICY_PATH = DATA_ROOT / "transportation_investable_universe_v3.yaml"


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _tickers(value: str) -> set[str]:
    return {ticker for ticker in value.split("|") if ticker}


def test_surface_domains_cover_the_19_names_without_changing_portfolio_membership() -> None:
    policy = load_investable_universe_policy(POLICY_PATH)
    surface = next(group for group in policy.groups if group.group_id == "surface_freight_core")
    memberships = Counter(
        ticker
        for domain in policy.surface_comparison_domains
        for ticker in domain.tickers
    )

    assert policy.surface_domain_policy_version == SURFACE_DOMAIN_POLICY_VERSION
    assert tuple(domain.domain_id for domain in policy.surface_comparison_domains) == (
        "rail_networks",
        "ltl_carriers",
        "truckload_intermodal",
        "asset_light_logistics",
        "integrated_parcel",
    )
    assert set(memberships) == set(surface.tickers)
    assert {ticker for ticker, count in memberships.items() if count > 1} == {"HUBG"}
    assert len(policy.selected_tickers) == len(set(policy.selected_tickers)) == 40


def test_metric_domain_rules_exactly_partition_source_applicability_by_union() -> None:
    policy = load_investable_universe_policy(POLICY_PATH)
    source_rows = _rows(DATA_ROOT / "transportation_surface_metric_source_map_v1.csv")
    mapped: defaultdict[str, set[str]] = defaultdict(set)
    for rule in policy.surface_metric_domain_rules:
        mapped[rule.metric_id].update(rule.applicable_tickers)
        assert rule.minimum_accepted_breadth == max(
            policy.surface_minimum_absolute_breadth,
            math.ceil(
                policy.surface_minimum_accepted_fraction
                * len(rule.applicable_tickers)
            ),
        )
        if len(rule.applicable_tickers) < policy.surface_minimum_calibratable_domain_size:
            assert not rule.is_calibration_candidate

    assert set(mapped) == {row["metric_id"] for row in source_rows}
    for row in source_rows:
        assert mapped[row["metric_id"]] == _tickers(row["applicable_tickers"])


def test_integrated_parcel_is_diagnostic_and_domains_use_within_domain_normalization() -> None:
    policy = load_investable_universe_policy(POLICY_PATH)
    parcel_rules = [
        rule
        for rule in policy.surface_metric_domain_rules
        if rule.comparison_domain_id == "integrated_parcel"
    ]

    assert parcel_rules
    assert all(not rule.is_calibration_candidate for rule in parcel_rules)
    assert all(
        rule.normalization_scope == "within_metric_domain"
        for rule in policy.surface_metric_domain_rules
    )


def test_semantic_queue_exposes_all_domains_for_an_overlapping_ticker() -> None:
    policy = load_investable_universe_policy(POLICY_PATH)
    queue = importlib.import_module(
        "industrials.transportation.scripts.36p_build_transportation_surface_semantic_review_queue"
    )
    hubg_rules = tuple(
        rule
        for rule in policy.surface_metric_rules("operating_ratio")
        if "HUBG" in rule.applicable_tickers
    )
    fields = queue._domain_fields(hubg_rules)

    assert set(str(fields["comparison_domain_ids"]).split("|")) == {
        "asset_light_logistics",
        "truckload_intermodal",
    }
    assert fields["candidate_domain_flag"] == 1
