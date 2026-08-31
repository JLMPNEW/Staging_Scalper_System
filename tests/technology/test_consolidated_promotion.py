from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pytest

from technology.core.consolidated_promotion import (
    ScoreBundle,
    _capacity_metrics,
    _circular_block_bootstrap,
    _decision,
    _horizon_blocks,
    _piecewise_score,
    _probabilistic_sharpe,
)


def test_piecewise_score_uses_neutral_as_incumbent_equivalence() -> None:
    anchors = [-0.10, 0.0, 0.20]
    assert _piecewise_score(-0.10, anchors) == 0.0
    assert _piecewise_score(0.0, anchors) == 50.0
    assert _piecewise_score(0.20, anchors) == 100.0
    assert _piecewise_score(None, anchors) == 50.0


def test_horizon_blocks_are_non_overlapping_and_compounded() -> None:
    start = date(2024, 1, 2)
    rows = [
        {
            "asof_date": start + timedelta(days=21 * index),
            "candidate_return": 0.02,
            "incumbent_return": 0.01,
            "benchmark_return": 0.005,
            "equal_weight_return": 0.007,
        }
        for index in range(7)
    ]
    blocks = _horizon_blocks(rows, base_days=21, horizon_days=63)
    assert len(blocks) == 2
    assert blocks[0]["candidate_return"] == pytest.approx((1.02**3) - 1.0)
    assert blocks[1]["block_start"] == rows[3]["asof_date"].isoformat()


def test_bootstrap_and_sharpe_adjustment_are_deterministic_and_conservative() -> None:
    returns = [0.01, 0.02, -0.005, 0.015, 0.004, 0.011]
    first = _circular_block_bootstrap(
        returns,
        repetitions=500,
        block_length=2,
        seed=357,
        lower_quantile=0.10,
        annualization=12.0,
    )
    second = _circular_block_bootstrap(
        returns,
        repetitions=500,
        block_length=2,
        seed=357,
        lower_quantile=0.10,
        annualization=12.0,
    )
    assert first == second
    evidence = _probabilistic_sharpe(returns, trials=180)
    assert evidence["deflated_sharpe_probability_approx"] <= evidence["probabilistic_sharpe_ratio"]


def _decision_policy() -> dict[str, object]:
    return {
        "decision_policy": {
            "full_promotion_min_adjusted_score": 65.0,
            "limited_promotion_min_adjusted_score": 57.5,
            "retain_incumbent_max_adjusted_score": 45.0,
            "full_promotion_min_positive_probability": 0.80,
            "limited_promotion_min_positive_probability": 0.65,
            "full_promotion_min_predictive_score": 60.0,
            "material_incremental_cagr": 0.015,
            "material_relative_wealth": 1.04,
            "minimum_active_win_rate": 0.52,
            "maximum_drawdown_deterioration": 0.04,
            "maximum_expected_shortfall_deterioration": 0.03,
            "clear_inferiority_cagr": -0.015,
            "clear_inferiority_relative_wealth": 0.97,
            "limited_promotion_exposure_cap": 0.50,
        }
    }


def test_economic_dominance_can_support_limited_promotion_without_legacy_t_gate() -> None:
    score = ScoreBundle(72.0, 65.0, 45.0, 70.0, 63.0, 0.75, 59.75)
    primary = {
        "incremental_cagr": 0.03,
        "relative_terminal_wealth": 1.08,
        "active_win_rate": 0.58,
        "max_drawdown_improvement": 0.01,
        "expected_shortfall_improvement": 0.005,
        "bootstrap_positive_probability": 0.72,
    }
    decision, economic, strong_support, _reasons, exposure = _decision(
        _decision_policy(),
        score,
        primary,
        {"stage8_strict_gate_pass": 0, "legacy_final_promotion_eligible": 0},
        [],
    )
    assert decision == "limited_promotion"
    assert economic is True
    assert strong_support is False
    assert exposure == 0.50


def test_hard_safety_failure_always_retains_incumbent() -> None:
    score = ScoreBundle(90.0, 90.0, 90.0, 90.0, 90.0, 1.0, 90.0)
    primary = {
        "incremental_cagr": 0.10,
        "relative_terminal_wealth": 1.30,
        "active_win_rate": 0.70,
        "max_drawdown_improvement": 0.10,
        "expected_shortfall_improvement": 0.10,
        "bootstrap_positive_probability": 0.95,
    }
    decision, _economic, _strong, reasons, exposure = _decision(
        _decision_policy(),
        score,
        primary,
        {"stage8_strict_gate_pass": 1, "legacy_final_promotion_eligible": 1},
        ["tampered_artifact"],
    )
    assert decision == "retain_incumbent"
    assert reasons == ["hard_safety:tampered_artifact"]
    assert exposure == 0.0


def test_capacity_uses_one_source_per_ticker(tmp_path) -> None:
    db_path = tmp_path / "prices.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE fact_price_ohlcv (
                ticker TEXT, bar_date TEXT, source_id TEXT, close REAL,
                adj_close REAL, volume REAL
            )
            """
        )
        start = date(2023, 9, 1)
        for index in range(90):
            bar_date = start + timedelta(days=index)
            connection.execute(
                "INSERT INTO fact_price_ohlcv VALUES (?, ?, ?, ?, ?, ?)",
                ("AAA", bar_date.isoformat(), "preferred", 10.0, 10.0, 1_000_000.0),
            )
            if index < 20:
                connection.execute(
                    "INSERT INTO fact_price_ohlcv VALUES (?, ?, ?, ?, ?, ?)",
                    ("AAA", bar_date.isoformat(), "secondary", 10.0, 10.0, 2_000_000.0),
                )
    holdings = [{"asof_date": date(2023, 11, 29), "ticker": "AAA", "weight": 0.10}]
    metrics = _capacity_metrics(
        db_path,
        holdings,
        {
            "trailing_observations": 60,
            "minimum_trailing_observations": 40,
            "reference_notional_usd": 1_000_000,
            "max_adv_participation": 0.05,
            "liquidation_days": 5,
            "source_preference": ["preferred", "secondary"],
        },
    )
    assert metrics["coverage"] == 1.0
    assert metrics["source_counts"] == {"preferred": 1}
    assert metrics["p05_capacity_ratio"] == pytest.approx(25.0)
