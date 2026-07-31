from __future__ import annotations

import pytest

from industrials.machinery.stage8_calibration import COMPONENT_FIELDS
from industrials.machinery.stage9_backtest import (
    StrategySpec,
    build_production_policy_parity,
    non_overlapping_dates,
    portfolio_weights,
    run_variant,
    summarize_variant,
)


def _source_row(
    *,
    ticker: str,
    asof: str,
    forward: str,
    score: float,
    outcome: float,
) -> dict[str, str]:
    row = {
        "ticker": ticker,
        "asof_date": asof,
        "split_name": "holdout",
        "calibration_cohort": f"cohort_{ticker}",
        "base_panel_eligible_flag": "1",
        "execution_universe_eligible_flag": "1",
        "panel_row_eligible_flag_21d": "1",
        "price_forward_date_21d": forward,
        "forward_return_21d": str(outcome),
        "benchmark_return_21d": "0.01",
        "execution_available_flag_21d": "1",
        "execution_exit_date_21d": forward,
        "benchmark_execution_exit_date_21d": forward,
        "execution_return_21d": str(outcome),
        "benchmark_execution_return_21d": "0.01",
        "avg_dollar_volume_60d": "100000000",
        "latest_borrow_fee_rate": "",
    }
    row.update({field: str(score) for field in COMPONENT_FIELDS})
    return row


def test_portfolio_weights_enforce_long_short_exposures() -> None:
    scored = [
        ({"ticker": ticker}, score)
        for ticker, score in (
            ("A", 90.0),
            ("B", 80.0),
            ("C", 20.0),
            ("D", 10.0),
        )
    ]

    weights = portfolio_weights(
        scored,
        spec=StrategySpec(
            name="long_short_q25_equal",
            portfolio_type="long_short",
            weighting="equal",
            quantile=0.25,
        ),
        minimum_positions=1,
    )

    assert sum(abs(value) for value in weights.values()) == 1.0
    assert abs(sum(weights.values())) < 1e-12
    assert weights == {"A": 0.5, "D": -0.5}


def test_non_overlapping_dates_allow_boundary_rebalance() -> None:
    rows = [
        _source_row(
            ticker=ticker,
            asof=asof,
            forward=forward,
            score=50.0,
            outcome=0.02,
        )
        for asof, forward in (
            ("2020-01-02", "2020-02-03"),
            ("2020-01-10", "2020-02-11"),
            ("2020-02-03", "2020-03-04"),
        )
        for ticker in ("A", "B", "C", "D")
    ]

    selected = non_overlapping_dates(
        rows,
        horizon=21,
        split_names={"holdout"},
        minimum_cross_section=4,
    )

    assert selected == ["2020-01-02", "2020-02-03"]


def test_variant_costs_capacity_and_exit_ledger() -> None:
    first = [
        _source_row(
            ticker=ticker,
            asof="2020-01-02",
            forward="2020-02-03",
            score=score,
            outcome=0.10 if ticker == "A" else 0.0,
        )
        for ticker, score in (
            ("A", 90.0),
            ("B", 80.0),
            ("C", 70.0),
            ("D", 60.0),
        )
    ]
    second = [
        _source_row(
            ticker=ticker,
            asof="2020-02-03",
            forward="2020-03-04",
            score=score,
            outcome=0.20 if ticker == "B" else 0.0,
        )
        for ticker, score in (
            ("A", 60.0),
            ("B", 90.0),
            ("C", 80.0),
            ("D", 70.0),
        )
    ]
    config = {
        "machinery_stage8": {"minimum_cross_section": 4},
        "machinery_stage9": {
            "minimum_positions": 1,
            "transaction_cost_bps": 20.0,
            "default_borrow_fee_rate": 0.05,
            "max_adv_participation": 0.05,
        },
    }
    model_weights = {
        field: 1.0 / len(COMPONENT_FIELDS) for field in COMPONENT_FIELDS
    }

    periods, holdings = run_variant(
        config,
        rows=[*first, *second],
        model="candidate",
        model_weights=model_weights,
        spec=StrategySpec(
            name="long_only_q25_equal",
            portfolio_type="long_only",
            weighting="equal",
            quantile=0.25,
        ),
        horizon=21,
        split_name="holdout",
        split_names={"holdout"},
    )
    summary = summarize_variant(periods, holdings)

    assert len(periods) == 2
    assert float(periods[0]["one_way_turnover"]) == 1.0
    assert float(periods[0]["transaction_cost"]) == 0.002
    assert float(periods[1]["one_way_turnover"]) == 1.0
    assert float(periods[1]["transaction_cost"]) == 0.004
    assert float(periods[1]["capacity_usd"]) == 5_000_000.0
    assert any(
        row["ticker"] == "A"
        and row["side"] == "exit"
        and float(row["trade_weight"]) == -1.0
        for row in holdings
    )
    assert summary["period_count"] == 2
    assert float(summary["mean_net_excess_return"]) > 0


def test_production_policy_parity_reconstructs_stage9_holdings() -> None:
    rows = [
        _source_row(
            ticker=ticker,
            asof="2020-01-02",
            forward="2020-02-03",
            score=score,
            outcome=0.02,
        )
        for ticker, score in (
            ("A", 90.0),
            ("B", 80.0),
            ("C", 70.0),
            ("D", 60.0),
        )
    ]
    config = {
        "machinery_stage8": {"minimum_cross_section": 4},
        "machinery_stage9": {
            "minimum_positions": 1,
            "transaction_cost_bps": 20.0,
            "default_borrow_fee_rate": 0.05,
            "max_adv_participation": 0.05,
            "gates": {
                "maximum_production_weight_parity_error": 1e-10,
            },
        },
    }
    model_weights = {
        field: 1.0 / len(COMPONENT_FIELDS) for field in COMPONENT_FIELDS
    }
    spec = StrategySpec(
        name="long_only_q25_equal",
        portfolio_type="long_only",
        weighting="equal",
        quantile=0.25,
    )
    periods, holdings = run_variant(
        config,
        rows=rows,
        model="stage8_candidate",
        model_weights=model_weights,
        spec=spec,
        horizon=21,
        split_name="holdout",
        split_names={"holdout"},
    )

    parity = build_production_policy_parity(
        config,
        panel_rows=rows,
        period_rows=periods,
        holding_rows=holdings,
        model_weights=model_weights,
        spec=spec,
        horizon=21,
    )

    assert len(parity) == 1
    assert parity[0]["parity_status"] == "PASS"
    assert parity[0]["expected_tickers"] == "A"
    assert parity[0]["actual_tickers"] == "A"


def test_stage9_operating_only_policy_excludes_development_names() -> None:
    rows = [
        _source_row(
            ticker=ticker,
            asof="2020-01-02",
            forward="2020-02-03",
            score=score,
            outcome=0.02,
        )
        for ticker, score in (
            ("DEV", 99.0),
            ("OPERATING", 70.0),
        )
    ]
    rows[0]["development_stage"] = "development_stage"
    rows[1]["development_stage"] = "operating"
    config = {
        "machinery_stage8": {"minimum_cross_section": 1},
        "machinery_stage9": {
            "production_universe_policy": "operating_only",
            "minimum_positions": 1,
            "transaction_cost_bps": 20.0,
            "default_borrow_fee_rate": 0.05,
            "max_adv_participation": 0.05,
        },
    }
    model_weights = {
        field: 1.0 / len(COMPONENT_FIELDS) for field in COMPONENT_FIELDS
    }

    periods, holdings = run_variant(
        config,
        rows=rows,
        model="stage8_candidate",
        model_weights=model_weights,
        spec=StrategySpec(
            name="long_only_q20_equal",
            portfolio_type="long_only",
            weighting="equal",
            quantile=0.20,
        ),
        horizon=21,
        split_name="holdout",
        split_names={"holdout"},
    )

    assert periods[0]["selected_tickers"] == "OPERATING"
    assert {row["ticker"] for row in holdings if row["side"] != "exit"} == {
        "OPERATING"
    }


def test_stage9_fails_if_selected_name_lacks_execution_outcome() -> None:
    rows = [
        _source_row(
            ticker=ticker,
            asof="2020-01-02",
            forward="2020-02-03",
            score=score,
            outcome=0.02,
        )
        for ticker, score in (
            ("A", 90.0),
            ("B", 80.0),
            ("C", 70.0),
            ("D", 60.0),
        )
    ]
    rows[0]["execution_available_flag_21d"] = "0"
    rows[0]["execution_return_21d"] = ""
    config = {
        "machinery_stage8": {"minimum_cross_section": 4},
        "machinery_stage9": {
            "minimum_positions": 1,
            "transaction_cost_bps": 20.0,
            "default_borrow_fee_rate": 0.05,
            "max_adv_participation": 0.05,
        },
    }
    model_weights = {
        field: 1.0 / len(COMPONENT_FIELDS) for field in COMPONENT_FIELDS
    }

    with pytest.raises(ValueError, match="Eligible Stage 9 row lacks"):
        run_variant(
            config,
            rows=rows,
            model="stage8_candidate",
            model_weights=model_weights,
            spec=StrategySpec(
                name="long_only_q25_equal",
                portfolio_type="long_only",
                weighting="equal",
                quantile=0.25,
            ),
            horizon=21,
            split_name="holdout",
            split_names={"holdout"},
        )
