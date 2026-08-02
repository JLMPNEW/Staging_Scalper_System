from __future__ import annotations

from datetime import date
from pathlib import Path

from industrials.core.share_sources import (
    ShareObservation,
    load_reviewed_share_observations,
    resolve_observations,
)


ASOF = date(2026, 7, 30)


def observation(
    source_id: str,
    *,
    family: str = "transportation",
    asof: date = ASOF,
    outstanding: float | None = None,
    float_shares: float | None = None,
    market_cap: float | None = None,
    price: float | None = None,
    outstanding_method: str = "",
    float_method: str = "",
    float_proxy: bool = False,
) -> ShareObservation:
    return ShareObservation(
        ticker="TEST",
        model_family=family,
        asof_date=asof,
        source_asof_date=asof,
        source_id=source_id,
        shares_outstanding=outstanding,
        float_shares=float_shares,
        market_cap=market_cap,
        price=price,
        outstanding_method=outstanding_method,
        float_method=float_method,
        float_proxy_flag=float_proxy,
    )


def test_resolver_keeps_outstanding_and_public_float_source_orders_separate() -> None:
    resolved = resolve_observations(
        [
            observation(
                "sec_companyfacts",
                outstanding=90.0,
                float_shares=65.0,
                outstanding_method="sec_point_in_time",
                float_method="sec_public_float_proxy",
                float_proxy=True,
            ),
            observation(
                "yahoo_finance_share_statistics",
                outstanding=100.0,
                float_shares=75.0,
                market_cap=1_000.0,
                price=10.0,
                outstanding_method="yahoo_shares_outstanding",
                float_method="yahoo_float_shares",
            ),
            observation(
                "interactive_brokers_fundamentals",
                outstanding=110.0,
                outstanding_method="ib_fundamental_ratios_shares_outstanding",
            ),
        ],
        ticker="TEST",
        model_family="transportation",
        asof=ASOF,
    )
    assert resolved.shares_outstanding == 110.0
    assert resolved.shares_outstanding_source_id == "interactive_brokers_fundamentals"
    assert resolved.float_shares == 75.0
    assert resolved.float_shares_source_id == "yahoo_finance_share_statistics"
    assert resolved.float_shares_proxy_flag is False
    assert resolved.market_cap == 1_000.0
    assert resolved.market_cap_source_id == "yahoo_finance_share_statistics"


def test_shortable_inventory_is_not_an_accepted_float_source() -> None:
    resolved = resolve_observations(
        [
            observation("interactive_brokers", float_shares=999.0),
            observation(
                "sec_companyfacts",
                outstanding=100.0,
                float_shares=70.0,
                float_method="sec_public_float_proxy",
                float_proxy=True,
            ),
        ],
        ticker="TEST",
        model_family="transportation",
        asof=ASOF,
    )
    assert resolved.float_shares == 70.0
    assert resolved.float_shares_source_id == "sec_companyfacts"
    assert resolved.float_shares_proxy_flag is True


def test_resolver_is_point_in_time_and_rejects_stale_live_sources() -> None:
    resolved = resolve_observations(
        [
            observation(
                "interactive_brokers_fundamentals",
                asof=date(2026, 7, 1),
                outstanding=120.0,
            ),
            observation(
                "yahoo_finance_share_statistics",
                outstanding=100.0,
                price=10.0,
            ),
            observation(
                "interactive_brokers_fundamentals",
                asof=date(2026, 7, 31),
                outstanding=130.0,
            ),
        ],
        ticker="TEST",
        model_family="transportation",
        asof=ASOF,
    )
    assert resolved.shares_outstanding == 100.0
    assert resolved.market_cap == 1_000.0
    assert resolved.market_cap_method == "price_times_shares_outstanding"


def test_resolver_is_model_family_scoped() -> None:
    resolved = resolve_observations(
        [
            observation(
                "interactive_brokers_fundamentals",
                family="defense",
                outstanding=999.0,
            ),
            observation(
                "sec_companyfacts",
                family="transportation",
                outstanding=80.0,
            ),
        ],
        ticker="TEST",
        model_family="transportation",
        asof=ASOF,
    )
    assert resolved.shares_outstanding == 80.0
    assert resolved.shares_outstanding_source_id == "sec_companyfacts"


def test_reviewed_filing_observations_are_validated_and_family_scoped(
    tmp_path: Path,
) -> None:
    path = tmp_path / "reviewed.csv"
    path.write_text(
        "ticker,available_date,measurement_date,shares_outstanding,method,proxy_flag,source_url,notes\n"
        "TEST,2026-02-20,2026-02-19,101,cover_page_multiclass_sum,0,https://example.test/filing,reviewed\n"
        "OTHER,2026-02-20,2026-02-19,202,cover_page,1,https://example.test/other,proxy\n",
        encoding="utf-8",
    )
    rows = load_reviewed_share_observations(
        path,
        model_family="transportation",
        history_start=date(2025, 1, 1),
        asof=ASOF,
        allowed_tickers={"TEST"},
    )
    assert len(rows) == 1
    assert rows[0].ticker == "TEST"
    assert rows[0].model_family == "transportation"
    assert rows[0].shares_outstanding == 101.0
    assert rows[0].outstanding_proxy_flag is False
    assert rows[0].source_id == "reviewed_filing_share_override"


def test_reviewed_filing_source_fills_sec_gap_but_does_not_override_yahoo() -> None:
    reviewed = observation(
        "reviewed_filing_share_override",
        asof=date(2026, 2, 20),
        outstanding=101.0,
    )
    sec = observation(
        "sec_companyfacts",
        asof=date(2026, 2, 20),
        outstanding=99.0,
    )
    resolved_without_live = resolve_observations(
        [sec, reviewed],
        ticker="TEST",
        model_family="transportation",
        asof=date(2026, 3, 1),
    )
    assert resolved_without_live.shares_outstanding == 101.0
    assert (
        resolved_without_live.shares_outstanding_source_id
        == "reviewed_filing_share_override"
    )
    yahoo = observation(
        "yahoo_finance_share_statistics",
        asof=date(2026, 3, 1),
        outstanding=103.0,
    )
    resolved_with_live = resolve_observations(
        [sec, reviewed, yahoo],
        ticker="TEST",
        model_family="transportation",
        asof=date(2026, 3, 1),
    )
    assert resolved_with_live.shares_outstanding == 103.0
    assert resolved_with_live.shares_outstanding_source_id == "yahoo_finance_share_statistics"
