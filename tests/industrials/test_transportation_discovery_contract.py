from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from industrials.core.config import family_config, load_yaml, resolve_path
from industrials.transportation.discovery_contract import (
    EXPECTED_IDENTITY_COUNT,
    EXPECTED_LANE_COUNTS,
    EXPECTED_METRIC_COUNT,
    EXPECTED_PACK_COUNTS,
    EXPECTED_SCOPE_COUNT,
    EXPECTED_SUPPORTING_METRIC_COUNT,
    EXPECTED_SUPPORTING_SCOPE_COUNT,
    assign_archetypes,
    build_scope_rows,
    build_supporting_scope_rows,
    input_contract_hash,
    load_archetype_policy,
    load_discovery_metrics,
    load_supporting_metrics,
    load_universe,
    validate_scope,
    validate_supporting_scope,
    validate_written_contract,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
INDUSTRIALS_ROOT = PROJECT_ROOT / "industrials"
TRANSPORTATION_ROOT = INDUSTRIALS_ROOT / "transportation"
CONFIG_PATH = INDUSTRIALS_ROOT / "config.yaml"


def contract_paths() -> dict[str, Path | str]:
    config = load_yaml(CONFIG_PATH)
    family = family_config(config, "transportation")
    parser_cfg = family["dedicated_parser"]
    universe = family["universe"]
    return {
        "active": resolve_path(universe["seed_csv"], base_dir=INDUSTRIALS_ROOT),
        "delisted": resolve_path(universe["delisted_seed_csv"], base_dir=INDUSTRIALS_ROOT),
        "metrics": resolve_path(parser_cfg["discovery_registry_csv"], base_dir=INDUSTRIALS_ROOT),
        "supporting_metrics": resolve_path(parser_cfg["supporting_registry_csv"], base_dir=INDUSTRIALS_ROOT),
        "policy": resolve_path(parser_cfg["archetype_policy_yaml"], base_dir=INDUSTRIALS_ROOT),
        "archetypes": resolve_path(parser_cfg["archetype_map_csv"], base_dir=INDUSTRIALS_ROOT),
        "scope": resolve_path(parser_cfg["scope_manifest_csv"], base_dir=INDUSTRIALS_ROOT),
        "supporting_scope": resolve_path(
            parser_cfg["supporting_scope_manifest_csv"],
            base_dir=INDUSTRIALS_ROOT,
        ),
        "manifest": resolve_path(parser_cfg["dp0_manifest_json"], base_dir=INDUSTRIALS_ROOT),
        "registry_version": str(parser_cfg["discovery_registry_version"]),
        "scope_version": str(parser_cfg["scope_version"]),
        "supporting_registry_version": str(parser_cfg["supporting_registry_version"]),
        "supporting_scope_version": str(parser_cfg["supporting_scope_version"]),
    }


def load_inputs():
    paths = contract_paths()
    policy, policy_errors = load_archetype_policy(Path(paths["policy"]))
    assert policy_errors == []
    metrics, metric_errors = load_discovery_metrics(
        Path(paths["metrics"]),
        allowed_tags=set(policy["allowed_tags"]),
    )
    assert metric_errors == []
    universe, universe_errors = load_universe(
        Path(paths["active"]),
        Path(paths["delisted"]),
    )
    assert universe_errors == []
    assignments, assignment_errors = assign_archetypes(universe, policy)
    assert assignment_errors == []
    supporting_metrics, supporting_metric_errors = load_supporting_metrics(
        Path(paths["supporting_metrics"]),
        allowed_tags=set(policy["allowed_tags"]),
        discovery_metrics=metrics,
    )
    assert supporting_metric_errors == []
    return paths, policy, metrics, supporting_metrics, assignments


def test_discovery_registry_matches_approved_metric_universe() -> None:
    _, _, metrics, _, _ = load_inputs()
    assert len(metrics) == EXPECTED_METRIC_COUNT == 90
    assert Counter(row["metric_pack"] for row in metrics) == EXPECTED_PACK_COUNTS
    assert Counter(row["source_lane"] for row in metrics) == EXPECTED_LANE_COUNTS
    metric_ids = {row["metric_id"] for row in metrics}
    assert len(metric_ids) == 90

    catalog = (TRANSPORTATION_ROOT / "TRANSPORTATION_SPECIALIZED_METRIC_UNIVERSE.md").read_text(encoding="utf-8")
    documented_ids = {
        match.group(1) for match in re.finditer(r"^\| \d+ \| `([a-z][a-z0-9_]*)` \|", catalog, re.MULTILINE)
    }
    assert documented_ids == metric_ids


def test_archetype_policy_covers_all_active_and_delisted_identities() -> None:
    _, _, _, _, assignments = load_inputs()
    assert len(assignments) == EXPECTED_IDENTITY_COUNT == 160
    assert Counter(row["universe_role"] for row in assignments) == {
        "active": 112,
        "delisted_usable": 48,
    }
    assert sum(row["development_overlay"] == "1" for row in assignments) == 29
    by_ticker = {row["ticker"]: row for row in assignments}
    assert by_ticker["UNP"]["primary_archetype"] == "surface_rail_operator"
    assert by_ticker["GBX"]["primary_archetype"] == "surface_rail_equipment"
    assert by_ticker["AAWW"]["primary_archetype"] == "cargo_airline"
    assert by_ticker["ASLE"]["primary_archetype"] == "aviation_services"
    assert by_ticker["JOBY"]["primary_archetype"] == "precommercial_transport"
    assert "regulated_precommercial" in by_ticker["JOBY"]["applicability_tags"]
    assert by_ticker["UFG"]["primary_archetype"] == "marine_services"
    assert "surface_logistics" in by_ticker["MATX"]["applicability_tags"]


def test_scope_is_complete_fail_closed_and_includes_inactive_tickers() -> None:
    paths, policy, metrics, _, assignments = load_inputs()
    contract_hash = input_contract_hash(
        [
            Path(paths["active"]),
            Path(paths["delisted"]),
            Path(paths["metrics"]),
            Path(paths["supporting_metrics"]),
            Path(paths["policy"]),
        ]
    )
    scope = build_scope_rows(
        assignments=assignments,
        metrics=metrics,
        scope_version=str(paths["scope_version"]),
        registry_version=str(paths["registry_version"]),
        policy_version=str(policy["policy_version"]),
        contract_hash=contract_hash,
    )
    assert validate_scope(rows=scope, assignments=assignments, metrics=metrics) == []
    assert len(scope) == EXPECTED_SCOPE_COUNT == 14_400
    assert sum(row["applicability_status"] == "APPLICABLE" for row in scope) == 2_535
    assert sum(row["applicability_status"] == "NOT_APPLICABLE" for row in scope) == 11_865
    assert {row["metric_id"] for row in scope if row["applicability_status"] == "APPLICABLE"} == {
        row["metric_id"] for row in metrics
    }
    aa_w_rows = [row for row in scope if row["ticker"] == "AAWW"]
    assert len(aa_w_rows) == 90
    assert all(row["universe_role"] == "delisted_usable" for row in aa_w_rows)
    passenger_load = next(row for row in aa_w_rows if row["metric_id"] == "passenger_load_factor")
    assert passenger_load["applicability_status"] == "NOT_APPLICABLE"
    cargo_traffic = next(row for row in aa_w_rows if row["metric_id"] == "traffic_growth")
    assert cargo_traffic["applicability_status"] == "APPLICABLE"


def test_supporting_operands_are_frozen_for_one_pass_derivations() -> None:
    paths, policy, _, supporting_metrics, assignments = load_inputs()
    assert len(supporting_metrics) == EXPECTED_SUPPORTING_METRIC_COUNT == 7
    assert {row["support_metric_id"] for row in supporting_metrics} == {
        "airline_fuel_consumed",
        "airline_capacity_units",
        "airline_fuel_expense",
        "airport_aeronautical_revenue",
        "airport_non_aeronautical_revenue",
        "airport_passenger_throughput",
        "milestone_target_date",
    }
    contract_hash = input_contract_hash(
        [
            Path(paths["active"]),
            Path(paths["delisted"]),
            Path(paths["metrics"]),
            Path(paths["supporting_metrics"]),
            Path(paths["policy"]),
        ]
    )
    scope = build_supporting_scope_rows(
        assignments=assignments,
        metrics=supporting_metrics,
        scope_version=str(paths["supporting_scope_version"]),
        registry_version=str(paths["supporting_registry_version"]),
        policy_version=str(policy["policy_version"]),
        contract_hash=contract_hash,
    )
    assert (
        validate_supporting_scope(
            rows=scope,
            assignments=assignments,
            metrics=supporting_metrics,
        )
        == []
    )
    assert len(scope) == EXPECTED_SUPPORTING_SCOPE_COUNT == 1_120
    assert any(
        row["ticker"] == "JOBY"
        and row["support_metric_id"] == "milestone_target_date"
        and row["applicability_status"] == "APPLICABLE"
        for row in scope
    )
    assert any(
        row["ticker"] == "AAL"
        and row["support_metric_id"] == "airline_fuel_consumed"
        and row["applicability_status"] == "APPLICABLE"
        for row in scope
    )
    assert any(
        row["ticker"] == "AAWW"
        and row["support_metric_id"] == "airline_fuel_consumed"
        and row["applicability_status"] == "NOT_APPLICABLE"
        for row in scope
    )


def test_committed_dp0_contract_and_baseline_hashes_validate() -> None:
    paths = contract_paths()
    result = validate_written_contract(
        project_root=PROJECT_ROOT,
        active_path=Path(paths["active"]),
        delisted_path=Path(paths["delisted"]),
        metric_registry_path=Path(paths["metrics"]),
        supporting_registry_path=Path(paths["supporting_metrics"]),
        archetype_policy_path=Path(paths["policy"]),
        archetype_output_path=Path(paths["archetypes"]),
        scope_output_path=Path(paths["scope"]),
        supporting_scope_output_path=Path(paths["supporting_scope"]),
        manifest_output_path=Path(paths["manifest"]),
        registry_version=str(paths["registry_version"]),
        scope_version=str(paths["scope_version"]),
        supporting_registry_version=str(paths["supporting_registry_version"]),
        supporting_scope_version=str(paths["supporting_scope_version"]),
        validate_baseline=False,
    )
    assert result["acceptance"] == "PASS"
    assert result["errors"] == []
    assert result["identity_count"] == 160
    assert result["metric_count"] == 90
    assert result["scope_row_count"] == 14_400
    assert result["supporting_metric_count"] == 7
    assert result["supporting_scope_row_count"] == 1_120
