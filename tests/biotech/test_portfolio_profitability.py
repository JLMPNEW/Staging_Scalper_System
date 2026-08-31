from __future__ import annotations

from datetime import date, timedelta

import pytest

from biotech_index.core.portfolio_profitability import (
    ReplayCostModel,
    ReplayTarget,
    TerminalRecovery,
    compare_daily_replays,
    deflated_sharpe_probability,
    normalized_target_weights,
    run_daily_portfolio_replay,
    targets_from_selection_rows,
)


def trading_prices(*, aaa: list[float], xbi: list[float]) -> dict[str, dict[date, float]]:
    start = date(2026, 1, 5)
    days = [start + timedelta(days=index) for index in range(len(xbi))]
    return {
        "AAA": dict(zip(days, aaa)),
        "XBI": dict(zip(days, xbi)),
    }


def test_normalized_target_weights_fills_residual_with_benchmark() -> None:
    assert normalized_target_weights({"AAA": 0.55}, benchmark_ticker="XBI") == {
        "AAA": pytest.approx(0.55),
        "XBI": pytest.approx(0.45),
    }


def test_selection_targets_keep_no_selection_dates_in_benchmark() -> None:
    targets = targets_from_selection_rows(
        [{"asof_date": "2026-01-05", "ticker": "AAA"}],
        ["2026-01-05", "2026-01-06"],
        active_weight_by_date={"2026-01-05": 0.5, "2026-01-06": 0.5},
        benchmark_ticker="XBI",
    )
    assert targets[0].weights == {"AAA": pytest.approx(0.5), "XBI": pytest.approx(0.5)}
    assert targets[1].weights == {"XBI": pytest.approx(1.0)}


def test_replay_executes_after_signal_and_charges_costs() -> None:
    prices = trading_prices(aaa=[100.0, 110.0, 121.0, 121.0], xbi=[100.0] * 4)
    target = ReplayTarget(date(2026, 1, 5), {"AAA": 1.0}, {"AAA": 1_000_000_000.0})
    result = run_daily_portfolio_replay(
        prices,
        [target],
        benchmark_ticker="XBI",
        model=ReplayCostModel(
            initial_capital=100_000.0,
            base_one_way_cost_bps=10.0,
            market_impact_coefficient_bps=0.0,
            max_adv_participation_pct=100.0,
        ),
    )

    first_trade = next(row for row in result.trade_rows if row["side"] == "buy")
    assert first_trade["trade_date"] == "2026-01-06"
    assert first_trade["price"] == pytest.approx(110.0)
    assert float(str(result.summary["terminal_wealth"])) < 110_000.0
    assert float(str(result.summary["total_transaction_cost"])) > 0.0


def test_replay_caps_stock_trade_by_adv() -> None:
    prices = trading_prices(aaa=[100.0] * 4, xbi=[100.0] * 4)
    target = ReplayTarget(date(2026, 1, 5), {"AAA": 1.0}, {"AAA": 10_000.0})
    result = run_daily_portfolio_replay(
        prices,
        [target],
        benchmark_ticker="XBI",
        model=ReplayCostModel(
            initial_capital=100_000.0,
            base_one_way_cost_bps=0.0,
            market_impact_coefficient_bps=0.0,
            max_adv_participation_pct=2.0,
        ),
    )
    buy = next(row for row in result.trade_rows if row["side"] == "buy")
    assert buy["notional"] == pytest.approx(200.0)
    assert buy["partial_fill_flag"] == 1
    assert result.summary["partial_fill_count"] == 1


def test_terminal_wipeout_is_booked_as_arithmetic_loss() -> None:
    prices = trading_prices(aaa=[10.0, 10.0, 10.0, 10.0], xbi=[100.0] * 4)
    target = ReplayTarget(date(2026, 1, 5), {"AAA": 1.0}, {"AAA": 1_000_000_000.0})
    result = run_daily_portfolio_replay(
        prices,
        [target],
        benchmark_ticker="XBI",
        model=ReplayCostModel(
            initial_capital=100_000.0,
            base_one_way_cost_bps=0.0,
            market_impact_coefficient_bps=0.0,
            max_adv_participation_pct=100.0,
        ),
        terminal_events={
            "AAA": TerminalRecovery(date(2026, 1, 7), 0.0, "wipeout"),
        },
    )
    terminal = next(row for row in result.trade_rows if row["side"] == "terminal_resolution")
    assert terminal["price"] == 0.0
    assert result.summary["terminal_wealth"] == pytest.approx(0.0)
    assert all(row["daily_net_return"] != float("-inf") for row in result.daily_rows)


def test_more_trials_reduce_deflated_sharpe_probability() -> None:
    returns = [0.002, 0.001, -0.0005, 0.0015] * 30
    one_trial = deflated_sharpe_probability(returns, effective_trials=1)
    many_trials = deflated_sharpe_probability(returns, effective_trials=500)
    assert one_trial is not None and many_trials is not None
    assert many_trials < one_trial


def test_paired_replay_comparison_reports_terminal_wealth_delta() -> None:
    prices = trading_prices(aaa=[100.0, 100.0, 110.0, 121.0, 121.0], xbi=[100.0] * 5)
    model = ReplayCostModel(
        initial_capital=100_000.0,
        base_one_way_cost_bps=0.0,
        benchmark_one_way_cost_bps=0.0,
        market_impact_coefficient_bps=0.0,
        max_adv_participation_pct=100.0,
    )
    candidate = run_daily_portfolio_replay(
        prices,
        [ReplayTarget(date(2026, 1, 5), {"AAA": 1.0}, {"AAA": 1_000_000_000.0})],
        benchmark_ticker="XBI",
        model=model,
    )
    incumbent = run_daily_portfolio_replay(
        prices,
        [ReplayTarget(date(2026, 1, 5), {"XBI": 1.0}, {})],
        benchmark_ticker="XBI",
        model=model,
    )
    comparison = compare_daily_replays(
        candidate,
        incumbent,
        effective_trials=10,
        bootstrap_iterations=20,
        bootstrap_block_days=2,
    )
    assert float(str(comparison["candidate_terminal_wealth"])) > float(str(comparison["incumbent_terminal_wealth"]))
    assert float(str(comparison["delta_net_profit"])) > 0.0
