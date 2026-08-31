from __future__ import annotations

import pytest

from portfolio_layer.earnings_dates.earnings_common import (
    assess_pipeline_coverage,
    pipeline_coverage_summary,
)


def _row(
    ticker: str,
    pipeline: str,
    *,
    investable: bool = True,
    earnings_date: str = "",
) -> dict[str, str]:
    return {
        "ticker": ticker,
        "source_pipeline": pipeline,
        "investable_eligible": "1" if investable else "0",
        "next_earnings_date": earnings_date,
    }


def test_pipeline_coverage_summary_preserves_consumer_defensive_lineage() -> None:
    rows = [
        _row("ADM", "consumer_defensive"),
        _row(
            "CALM",
            "consumer_defensive",
            earnings_date="2026-10-01",
        ),
        _row(
            "NVDA",
            "semiconductors",
            earnings_date="2026-11-18",
        ),
        _row("OLD", "consumer_defensive", investable=False),
    ]

    assert pipeline_coverage_summary(rows) == [
        {
            "source_pipeline": "consumer_defensive",
            "universe_count": 3,
            "investable_count": 2,
            "investable_with_date": 1,
            "investable_coverage_fraction": 0.5,
        },
        {
            "source_pipeline": "semiconductors",
            "universe_count": 1,
            "investable_count": 1,
            "investable_with_date": 1,
            "investable_coverage_fraction": 1.0,
        },
    ]


def test_live_refresh_fails_when_material_pipeline_has_zero_coverage() -> None:
    rows = [
        _row("ADM", "consumer_defensive"),
        _row("CALM", "consumer_defensive"),
        _row("MZTI", "consumer_defensive"),
    ]

    result = assess_pipeline_coverage(
        rows,
        minimum_investable_count=3,
        minimum_coverage_fraction=0.60,
        provider_network_calls_allowed=True,
    )

    assert result["status"] == "FAIL"
    assert result["deferred"] is False
    assert result["zero_coverage_pipelines"] == ["consumer_defensive"]


def test_historical_refresh_defers_instead_of_backdating_zero_coverage() -> None:
    rows = [
        _row("ADM", "consumer_defensive"),
        _row("CALM", "consumer_defensive"),
        _row("MZTI", "consumer_defensive"),
    ]

    result = assess_pipeline_coverage(
        rows,
        minimum_investable_count=3,
        minimum_coverage_fraction=0.60,
        provider_network_calls_allowed=False,
    )

    assert result["status"] == "WARN"
    assert result["deferred"] is True
    assert result["zero_coverage_pipelines"] == ["consumer_defensive"]


def test_nonzero_below_floor_coverage_warns_without_deferral() -> None:
    rows = [
        _row("A", "consumer_defensive", earnings_date="2026-10-01"),
        _row("B", "consumer_defensive"),
        _row("C", "consumer_defensive"),
    ]

    result = assess_pipeline_coverage(
        rows,
        minimum_investable_count=3,
        minimum_coverage_fraction=0.60,
        provider_network_calls_allowed=True,
    )

    assert result["status"] == "WARN"
    assert result["deferred"] is False
    assert result["below_floor_pipelines"] == ["consumer_defensive"]


@pytest.mark.parametrize(
    ("minimum_count", "floor"),
    [(0, 0.60), (3, -0.01), (3, 1.01)],
)
def test_pipeline_coverage_policy_rejects_invalid_thresholds(
    minimum_count: int,
    floor: float,
) -> None:
    with pytest.raises(ValueError):
        assess_pipeline_coverage(
            [],
            minimum_investable_count=minimum_count,
            minimum_coverage_fraction=floor,
            provider_network_calls_allowed=True,
        )
