from __future__ import annotations

import copy
import sqlite3
from pathlib import Path

import pytest

from consumer_defensive.core.calibration_preregistration_v2 import (
    PORTFOLIO_POLICY,
    build_candidate_registry,
    build_preregistration,
    publish_immutable_json,
    read_stage6c_run_metadata,
    validate_candidate_registry,
    validate_preregistration,
)
from consumer_defensive.core.config import load_config
from consumer_defensive.core.promotion_framework_v2 import (
    REQUIRED_COHORTS,
    REQUIRED_HORIZONS,
    canonical_sha256,
    load_framework,
)
from consumer_defensive.core.scoring_features import CORE_COMPONENT_SPECS
from consumer_defensive.core.shared_services import load_shared_service_contract


ROOT = Path(__file__).resolve().parents[2]


def _contracts():
    bundle = load_config(ROOT / "consumer_defensive/config.yaml")
    framework = load_framework(
        ROOT / "consumer_defensive/data/consumer_defensive_promotion_framework_v2.yaml"
    )
    shared = load_shared_service_contract(
        ROOT / "consumer_defensive/data/consumer_defensive_shared_service_contract_v1.yaml"
    )
    return bundle, framework, shared


def _stage6c() -> dict[str, object]:
    return {
        "stage6c_run_id": 3,
        "asof_date": "2026-08-14",
        "history_start": "2019-01-02",
        "status": "complete",
        "panel_sha256": "a" * 64,
        "panel_row_count": 81_221,
        "evaluation_date_count": 86,
    }


def _campaign() -> dict[str, object]:
    return {
        "campaign_id": "campaign_001",
        "registry_sha256": "b" * 64,
    }


def _registry(*, accepted=()):
    bundle, framework, shared = _contracts()
    return build_candidate_registry(
        bundle,
        framework=framework,
        shared_contract=shared,
        asof_date="2026-08-14",
        stage6c_run=_stage6c(),
        campaign_summary=_campaign(),
        accepted_factor_cells=accepted,
    )


def test_zero_accepted_cells_generate_complete_core_only_registry() -> None:
    registry = _registry()
    groups = {spec.group for spec in CORE_COMPONENT_SPECS}
    expected_per_scope = 1 + len(CORE_COMPONENT_SPECS) + len(groups)
    assert registry["candidate_count"] == (
        len(REQUIRED_COHORTS) * len(REQUIRED_HORIZONS) * expected_per_scope
    )
    assert all(not row["specialized_weights"] for row in registry["candidates"])
    census = {
        (cohort, horizon): sum(
            row["cohort"] == cohort and row["horizon_sessions"] == horizon
            for row in registry["candidates"]
        )
        for cohort in REQUIRED_COHORTS
        for horizon in REQUIRED_HORIZONS
    }
    assert set(census.values()) == {expected_per_scope}


def test_specialized_acceptance_is_exactly_scope_and_horizon_routed() -> None:
    cohort = sorted(REQUIRED_COHORTS)[0]
    cells = [
        {
            "cell_id": "cell_primary",
            "factor_id": "organic_revenue_growth_pct",
            "scope_id": cohort,
            "target_name": "forward_xlp_residual_return",
            "horizon_trading_days": 21,
            "factor_direction": "higher_is_better",
        },
        {
            "cell_id": "cell_robust",
            "factor_id": "organic_revenue_growth_pct",
            "scope_id": cohort,
            "target_name": "forward_spy_beta_residual_return",
            "horizon_trading_days": 21,
            "factor_direction": "higher_is_better",
        },
    ]
    registry = _registry(accepted=list(reversed(cells)))
    overlay = [
        row for row in registry["candidates"]
        if row["candidate_kind"].startswith("validated_specialized_overlay:")
    ]
    assert len(overlay) == 1
    assert overlay[0]["cohort"] == cohort
    assert overlay[0]["horizon_sessions"] == 21
    assert overlay[0]["accepted_factor_cell_ids"] == ["cell_primary", "cell_robust"]
    reordered = _registry(accepted=cells)
    assert reordered == registry


def test_candidate_identity_and_registry_self_hash_fail_on_mutation() -> None:
    registry = _registry()
    mutated = copy.deepcopy(registry)
    mutated["candidates"][0]["core_weights"][next(iter(mutated["candidates"][0]["core_weights"]))] += 0.01
    mutated["payload_sha256"] = canonical_sha256(mutated)
    with pytest.raises(ValueError, match="identity|sum"):
        validate_candidate_registry(mutated)


def test_preregistration_is_methodology_bound_and_immutable(tmp_path: Path) -> None:
    bundle, framework, shared = _contracts()
    registry = _registry()
    prereg = build_preregistration(
        bundle,
        repository_root=ROOT,
        framework=framework,
        shared_contract=shared,
        stage6c_run=_stage6c(),
        candidate_registry=registry,
    )
    assert prereg["forward_label_accessed"] is False
    assert prereg["production_promotion_enabled"] is False
    assert prereg["portfolio_write_enabled"] is False
    assert "final_holding_sessions" not in prereg["portfolio_policy"]
    assert prereg["portfolio_policy"] == PORTFOLIO_POLICY
    assert prereg["portfolio_policy"]["live_path_rebalance_policy"] == (
        "buy_and_hold_sleeves_between_next_signal_rebalances"
    )
    assert prereg["portfolio_policy"]["final_path_sessions_after_last_signal"] == 21
    assert prereg["portfolio_policy"]["candidate_selection_metric_policy"] == (
        "horizon_specific_forward_labels_and_relative_metrics"
    )
    assert prereg["portfolio_policy"]["realized_path_metric_policy"] == (
        "daily_realized_monthly_rebalanced_absolute_profitability"
    )
    expected_terminal_methodology = {
        "consumer_defensive/core/terminal_events.py",
        "consumer_defensive/core/stage3_runtime.py",
        "consumer_defensive/data/consumer_defensive_terminal_event_policy.yaml",
        "consumer_defensive/system_csvs/consumer_defensive_terminal_events.csv",
    }
    assert expected_terminal_methodology <= set(prereg["code_file_sha256s"])
    validate_preregistration(prereg, candidate_registry=registry)
    path = tmp_path / "prereg.json"
    publish_immutable_json(path, prereg)
    publish_immutable_json(path, prereg)
    divergent = dict(prereg)
    divergent["asof_date"] = "2026-08-13"
    with pytest.raises(FileExistsError, match="divergent"):
        publish_immutable_json(path, divergent)


def test_stage6c_metadata_preregistration_query_never_reads_forward_labels() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE stage6c_panel_run(
               stage6c_run_id INTEGER PRIMARY KEY, asof_date TEXT,
               history_start TEXT, evaluation_frequency TEXT,
               entry_lag_trading_days INTEGER, horizons_json TEXT,
               freshness_days INTEGER, config_sha256 TEXT,
               metric_policy_sha256 TEXT, source_stage6b_run_id INTEGER,
               status TEXT, evaluation_date_count INTEGER,
               panel_row_count INTEGER, numeric_row_count INTEGER,
               panel_sha256 TEXT)"""
    )
    conn.execute(
        "INSERT INTO stage6c_panel_run VALUES(3,'2026-08-14','2019-01-02',"
        "'monthly',1,'[21,63,126]',1,?,?,37,'complete',86,81221,28487,?)",
        ("c" * 64, "d" * 64, "a" * 64),
    )
    traced: list[str] = []
    conn.set_trace_callback(traced.append)
    result = read_stage6c_run_metadata(conn, stage6c_run_id=3)
    assert result["panel_sha256"] == "a" * 64
    assert not any("forward_" in query.lower() for query in traced)



