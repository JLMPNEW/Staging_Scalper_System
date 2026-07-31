from __future__ import annotations

from datetime import date, timedelta

from industrials.transportation.oos_outcomes import (
    ACTIVE_PRICE_SOURCE,
    DELISTED_PRICE_SOURCE,
    AliasPolicy,
    ContinuityPolicy,
    MembershipEvent,
    PricePoint,
    outcome_window,
    price_source_order,
    rank_usable_period_count,
    resolve_price_ticker,
)


def points(
    source: str,
    *,
    count: int,
    start: date = date(2020, 1, 1),
) -> list[PricePoint]:
    return [
        PricePoint(
            bar_date=start + timedelta(days=index),
            value=100.0 + index,
            source_id=source,
            price_basis="adj_close",
        )
        for index in range(count)
    ]


def test_outcome_window_requires_both_legs_from_one_source() -> None:
    primary = points(ACTIVE_PRICE_SOURCE, count=3)
    fallback = points(DELISTED_PRICE_SOURCE, count=8)
    result = outcome_window(
        {
            ACTIVE_PRICE_SOURCE: primary,
            DELISTED_PRICE_SOURCE: fallback,
        },
        asof="2020-01-02",
        forward_trading_days=4,
        source_order=(ACTIVE_PRICE_SOURCE, DELISTED_PRICE_SOURCE),
    )
    assert result.anchor is not None
    assert result.forward is not None
    assert result.anchor.source_id == DELISTED_PRICE_SOURCE
    assert result.forward.source_id == DELISTED_PRICE_SOURCE
    assert result.session_count == 4


def test_delisted_source_is_preferred_only_for_delisted_role() -> None:
    assert price_source_order("active") == (
        ACTIVE_PRICE_SOURCE,
        DELISTED_PRICE_SOURCE,
    )
    assert price_source_order("delisted_usable") == (
        DELISTED_PRICE_SOURCE,
        ACTIVE_PRICE_SOURCE,
    )


def test_terminal_acquisition_uses_last_verified_adjusted_close() -> None:
    series = points(DELISTED_PRICE_SOURCE, count=6)
    membership = MembershipEvent(
        ticker="OLD",
        start_date=date(2020, 1, 1),
        end_date=date(2020, 1, 6),
        membership_status="delisted",
        terminal_type="acquisition",
        exit_type="strategic",
    )
    result = outcome_window(
        {DELISTED_PRICE_SOURCE: series},
        asof="2020-01-02",
        forward_trading_days=10,
        source_order=(DELISTED_PRICE_SOURCE,),
        membership=membership,
        horizon_end=date(2020, 1, 20),
    )
    assert result.outcome_method == "terminal_membership_exit"
    assert result.forward is not None
    assert result.forward.bar_date == date(2020, 1, 6)
    assert result.forward.value == 105.0


def test_reviewed_wipeout_overrides_terminal_value_to_zero() -> None:
    series = points(DELISTED_PRICE_SOURCE, count=6)
    membership = MembershipEvent(
        ticker="FAIL",
        start_date=date(2020, 1, 1),
        end_date=date(2020, 1, 6),
        membership_status="delisted",
        terminal_type="wipeout",
        exit_type="bankruptcy",
    )
    result = outcome_window(
        {DELISTED_PRICE_SOURCE: series},
        asof="2020-01-02",
        forward_trading_days=10,
        source_order=(DELISTED_PRICE_SOURCE,),
        membership=membership,
        horizon_end=date(2020, 1, 20),
    )
    assert result.forward is not None
    assert result.forward.value == 0.0
    assert result.forward.price_basis == "reviewed_terminal_zero"
    assert result.forward_return == -1.0


def test_structural_break_window_fails_closed() -> None:
    policy = ContinuityPolicy(
        ticker="BREAK",
        current_security_start_date=date(2020, 1, 1),
        continuity_policy="STRUCTURAL_BREAK_NO_STITCH",
        structural_break_date=date(2020, 1, 5),
        history_treatment="separate_regime_no_return_stitch",
    )
    result = outcome_window(
        {ACTIVE_PRICE_SOURCE: points(ACTIVE_PRICE_SOURCE, count=10)},
        asof="2020-01-02",
        forward_trading_days=5,
        source_order=(ACTIVE_PRICE_SOURCE,),
        continuity=policy,
    )
    assert result.forward is None
    assert (
        result.unavailable_reason
        == "security_continuity_boundary_violation"
    )


def test_verified_alias_resolution_is_effective_dated() -> None:
    policies = {
        "NEW": [
            AliasPolicy(
                contract_ticker="NEW",
                active_ticker="NEW",
                predecessor_ticker="OLD",
                effective_date=date(2022, 1, 1),
            )
        ]
    }
    assert resolve_price_ticker(
        "NEW",
        date(2021, 12, 31),
        policies,
    ) == ("OLD", "verified_predecessor")
    assert resolve_price_ticker(
        "NEW",
        date(2022, 1, 1),
        policies,
    ) == ("NEW", "verified_active_alias")


def test_rank_usable_period_requires_breadth_and_variation() -> None:
    rows = [
        {
            "asof_date": "2020-01-31",
            "ticker": ticker,
            "direction_adjusted_metric_value": value,
            "panel_row_eligible_flag": "1",
        }
        for ticker, value in (("A", "1"), ("B", "2"), ("C", "3"))
    ]
    rows.extend(
        {
            "asof_date": "2020-02-29",
            "ticker": ticker,
            "direction_adjusted_metric_value": "1",
            "panel_row_eligible_flag": "1",
        }
        for ticker in ("A", "B", "C")
    )
    assert rank_usable_period_count(rows, minimum_tickers=3) == 1
