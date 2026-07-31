from __future__ import annotations

from industrials.transportation.calibration_contract import (
    FlagHistory,
    flag_exception_decision,
    purged_split_calendar,
    summarize_flag_history,
)


def test_flag_exception_requires_breadth_depth_and_binary_variation() -> None:
    disposition = {
        "accepted_breadth_gate_pass": "1",
        "evidence_precision_gate_pass": "1",
    }
    history = FlagHistory(
        metric_id="going_concern_flag",
        value_row_count=8,
        ticker_count=4,
        median_period_count=2.0,
        observed_values=(0.0, 1.0),
    )

    assert flag_exception_decision(disposition, history) == (
        True,
        "flag_specific_two_period_depth_exception_pass",
    )

    failed, reason = flag_exception_decision(
        {**disposition, "accepted_breadth_gate_pass": "0"},
        history,
    )
    assert failed is False
    assert "accepted_issuer_breadth_gate_failed" in reason


def test_empty_flag_history_fails_closed() -> None:
    history = FlagHistory(
        metric_id="pre_revenue_flag",
        value_row_count=0,
        ticker_count=0,
        median_period_count=0.0,
        observed_values=(),
    )
    authorized, reason = flag_exception_decision(
        {
            "accepted_breadth_gate_pass": "1",
            "evidence_precision_gate_pass": "1",
        },
        history,
    )
    assert authorized is False
    assert "no_frozen_pit_flag_values" in reason
    assert "binary_outcome_variation_not_observed" in reason


def test_summarize_flag_history_counts_distinct_periods() -> None:
    rows = [
        {
            "metric_id": "going_concern_flag",
            "ticker": "AAA",
            "period_end": "2023-12-31",
            "metric_value": "1",
        },
        {
            "metric_id": "going_concern_flag",
            "ticker": "AAA",
            "period_end": "2023-12-31",
            "metric_value": "1",
        },
        {
            "metric_id": "going_concern_flag",
            "ticker": "AAA",
            "period_end": "2024-12-31",
            "metric_value": "0",
        },
    ]
    result = summarize_flag_history(rows)["going_concern_flag"]
    assert result.value_row_count == 3
    assert result.ticker_count == 1
    assert result.median_period_count == 2.0
    assert result.observed_values == (0.0, 1.0)


def test_purged_split_calendar_preserves_holdout() -> None:
    dates = [
        "2020-01-31",
        "2020-02-28",
        "2020-03-31",
        "2020-04-30",
        "2020-05-29",
        "2020-06-30",
        "2020-07-31",
        "2020-08-31",
        "2020-09-30",
        "2020-10-30",
        "2020-11-30",
        "2020-12-31",
    ]
    result = purged_split_calendar(
        dates,
        forward_trading_days=63,
        embargo_days=21,
    )
    assert "embargo" in set(result.values())
    assert "holdout" in set(result.values())
