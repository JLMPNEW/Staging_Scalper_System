from __future__ import annotations

import importlib.util
import json
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load_script(name: str):
    path = PROJECT_ROOT / "industrials" / "transportation" / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.replace(".", "_"), path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_v5_score_snapshot_requires_exact_hash_bound_contract(tmp_path: Path) -> None:
    builder = load_script("38l_build_transportation_v5_pit_score_history.py")
    snapshot = tmp_path / "transportation" / "2020-01-31"
    snapshot.mkdir(parents=True)
    score = snapshot / "scoring_features.csv"
    sidecar = snapshot / "calibration_eligibility.csv"
    score.write_text("ticker\nAAA\n", encoding="utf-8")
    sidecar.write_text("ticker\nAAA\n", encoding="utf-8")
    policies = {"surface_v1": "abc", "tanker_v1": "def"}
    manifest = {
        "acceptance": "PASS",
        "asof_date": "2020-01-31",
        "ticker_scope_sha256": builder.scope_hash(["AAA"]),
        "rebuild_validation_sha256": "validation-hash",
        "policy_sha256": policies,
        "score_row_count": 1,
        "score_sha256": builder.file_sha256(score),
        "calibration_sidecar_sha256": builder.file_sha256(sidecar),
    }
    (snapshot / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    assert builder.snapshot_valid(
        snapshot,
        asof="2020-01-31",
        expected_tickers=["AAA"],
        validation_sha256="validation-hash",
        policy_hashes=policies,
    )
    score.write_text("ticker\nBBB\n", encoding="utf-8")
    assert not builder.snapshot_valid(
        snapshot,
        asof="2020-01-31",
        expected_tickers=["AAA"],
        validation_sha256="validation-hash",
        policy_hashes=policies,
    )


def test_v5_score_history_stages_remain_pre_production() -> None:
    builder = load_script("38l_build_transportation_v5_pit_score_history.py")
    validator = load_script("38m_validate_transportation_v5_pit_score_history.py")
    assert "pit_score_history" in str(builder.DEFAULT_OUTPUT_ROOT)
    assert "pit_score_validation" in str(validator.DEFAULT_OUTPUT_DIR)
    assert builder.SIDECAR_FIELDS[-1] == "current_portfolio_eligibility_authorized"
    validator_source = (
        PROJECT_ROOT
        / "industrials"
        / "transportation"
        / "scripts"
        / "38m_validate_transportation_v5_pit_score_history.py"
    ).read_text(encoding="utf-8")
    assert "zero_current_score_ready_contributors" in validator_source
    assert "noncontributing_historical_only_tickers" in validator_source


def test_v5_score_history_maps_database_pool_to_governed_cohort_id() -> None:
    builder = load_script("38l_build_transportation_v5_pit_score_history.py")
    policies = (
        {
            "calibration_pool": "surface_freight_and_logistics",
            "cohort_id": "north_american_surface_freight_and_logistics_v5",
        },
        {
            "calibration_pool": "marine_shipping_and_maritime",
            "cohort_id": "oil_tanker_operators_v5",
        },
    )
    by_pool = builder.policy_by_calibration_pool(policies)
    assert builder.governed_cohort_id(
        {"calibration_cohort": "surface_freight_and_logistics"}, by_pool
    ) == "north_american_surface_freight_and_logistics_v5"
    assert builder.governed_cohort_id(
        {"calibration_cohort": "marine_shipping_and_maritime"}, by_pool
    ) == "oil_tanker_operators_v5"

    validator = load_script("38m_validate_transportation_v5_pit_score_history.py")
    assert validator.governed_cohort_id(
        {"calibration_cohort": "surface_freight_and_logistics"}, by_pool
    ) == "north_american_surface_freight_and_logistics_v5"


def test_v5_pinned_raw_price_normalization_matches_adjusted_open_math() -> None:
    builder = load_script("38o_build_transportation_v5_outcome_panel.py")
    prices = builder.normalized_price_points(
        [
            {
                "ticker": "AAA",
                "source_id": "test",
                "bar_date": "2020-01-02",
                "open": "100",
                "close": "110",
                "adj_close": "55",
                "price_adjustment": "adjusted_close",
            }
        ]
    )
    point = prices["AAA"]["test"][0]
    assert point.adjusted_close == 55.0
    assert point.adjusted_open == 50.0


def test_v5_outcome_panel_emits_governed_sidecar_cohort() -> None:
    source = (
        PROJECT_ROOT
        / "industrials"
        / "transportation"
        / "scripts"
        / "38o_build_transportation_v5_outcome_panel.py"
    ).read_text(encoding="utf-8")
    assert 'row["calibration_cohort"] = str(gate.get("cohort_id") or "")' in source
    validator_source = (
        PROJECT_ROOT
        / "industrials"
        / "transportation"
        / "scripts"
        / "38p_validate_transportation_v5_outcome_panel.py"
    ).read_text(encoding="utf-8")
    assert "zero_current_outcome_contributors" in validator_source
    assert "noncontributing_historical_only_tickers" in validator_source


def test_v5_candidate_sort_preserves_exact_zero() -> None:
    calibration = load_script(
        "38q_run_transportation_v5_diagnostic_calibration.py"
    )
    assert calibration.finite_sort(0.0) == 0.0
    assert calibration.finite_sort("0") == 0.0
    assert calibration.finite_sort("") == float("-inf")


def test_v5_diagnostic_blocks_are_fixed_calendar_ranges() -> None:
    calibration = load_script(
        "38q_run_transportation_v5_diagnostic_calibration.py"
    )
    rows = [
        {
            "asof_date": asof,
            "horizon_sessions": "63",
            "calibration_eligible_flag": "1",
            "outcome_available_flag": "1",
        }
        for asof in ("2021-12-31", "2022-01-01", "2024-01-01")
    ]
    blocks = calibration.assign_blocks(
        rows,
        horizon=63,
        calendar_blocks=[
            {
                "block_id": "diagnostic_block_1",
                "start_date": "2019-01-01",
                "end_date": "2021-12-31",
            },
            {
                "block_id": "diagnostic_block_2",
                "start_date": "2022-01-01",
                "end_date": "2023-12-31",
            },
            {
                "block_id": "diagnostic_block_3",
                "start_date": "2024-01-01",
                "end_date": "2026-07-30",
            },
        ],
    )
    assert blocks == {
        "2021-12-31": "diagnostic_block_1",
        "2022-01-01": "diagnostic_block_2",
        "2024-01-01": "diagnostic_block_3",
    }


def test_v5_aggregate_history_is_never_a_passing_gate() -> None:
    calibration = load_script(
        "38q_run_transportation_v5_diagnostic_calibration.py"
    )
    row = calibration.metric_row(
        cohort="surface",
        candidate="candidate",
        horizon=63,
        block="diagnostic_all",
        metrics={
            "eligible_row_count": 100,
            "available_outcome_row_count": 100,
            "outcome_coverage": 1.0,
            "snapshot_count": 30,
            "mean_ic": 0.1,
            "mean_top_excess_net": 0.02,
            "top_excess_hit_rate": 0.6,
            "mean_top_minus_cohort_net": 0.01,
            "mean_top_minus_bottom_gross": 0.02,
            "non_overlapping_snapshot_count": 10,
        },
        minimum_non_overlapping_snapshots=6,
    )
    assert row["ranking_gate"] == "PASS"
    assert row["investability_gate"] == "PASS"
    assert row["diagnostic_gate"] == "DESCRIPTIVE_ONLY"


def test_v5_positive_iyt_excess_does_not_mask_failed_ranking() -> None:
    calibration = load_script(
        "38q_run_transportation_v5_diagnostic_calibration.py"
    )
    row = calibration.metric_row(
        cohort="surface",
        candidate="candidate",
        horizon=63,
        block="diagnostic_block_1",
        metrics={
            "eligible_row_count": 100,
            "available_outcome_row_count": 100,
            "outcome_coverage": 1.0,
            "snapshot_count": 24,
            "mean_ic": 0.05,
            "mean_top_excess_net": 0.02,
            "top_excess_hit_rate": 0.6,
            "mean_top_minus_cohort_net": -0.01,
            "mean_top_minus_bottom_gross": -0.02,
            "non_overlapping_snapshot_count": 8,
        },
        minimum_non_overlapping_snapshots=6,
    )
    assert row["investability_gate"] == "PASS"
    assert row["ranking_gate"] == "FAIL"
    assert row["diagnostic_gate"] == "FAIL"


def test_v5_block_summary_preserves_boundary_turnover_and_sample_gate() -> None:
    from industrials.core.oos_research import summarize_candidate_period_rows

    periods = [
        {
            "asof_date": "2024-01-31",
            "exit_date": "2024-04-30",
            "ic": 0.1,
            "turnover": 0.75,
            "net_excess": 0.01,
            "cohort_excess": 0.0,
            "top_minus_cohort_net": 0.01,
            "top_minus_bottom_gross": 0.02,
        },
        {
            "asof_date": "2024-05-31",
            "exit_date": "2024-08-31",
            "ic": 0.1,
            "turnover": 0.25,
            "net_excess": 0.01,
            "cohort_excess": 0.0,
            "top_minus_cohort_net": 0.01,
            "top_minus_bottom_gross": 0.02,
        },
    ]
    metrics = summarize_candidate_period_rows(
        periods, eligible_row_count=20, available_outcome_row_count=20
    )
    assert metrics["average_turnover"] == 0.5

    calibration = load_script(
        "38q_run_transportation_v5_diagnostic_calibration.py"
    )
    row = calibration.metric_row(
        cohort="surface",
        candidate="candidate",
        horizon=63,
        block="diagnostic_block_3",
        metrics=metrics,
        minimum_non_overlapping_snapshots=6,
    )
    assert row["effective_sample_gate"] == "FAIL"
    assert row["diagnostic_gate"] == "FAIL"
