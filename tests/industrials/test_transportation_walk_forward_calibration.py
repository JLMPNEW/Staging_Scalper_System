from __future__ import annotations

import math

from industrials.transportation.walk_forward_calibration import (
    aggregate_period_rows,
    equal_weights,
    overlay_score,
    percentile_scores,
    ranked_sleeves,
    spearman,
    turnover,
)


def test_percentile_scores_winsor_and_preserve_order() -> None:
    result = percentile_scores(
        {"A": 1.0, "B": 2.0, "C": 3.0, "D": 100.0}
    )
    assert list(sorted(result, key=result.get)) == ["A", "B", "C", "D"]
    assert result["A"] == 0.0
    assert result["D"] == 100.0


def test_overlay_score_keeps_frozen_generic_scale() -> None:
    assert overlay_score(60.0, 100.0, 0.0) == 60.0
    assert overlay_score(60.0, 100.0, 0.10) == 64.0


def test_spearman_handles_ties_and_direction() -> None:
    positive = spearman([1.0, 2.0, 3.0], [10.0, 20.0, 30.0])
    negative = spearman([1.0, 2.0, 3.0], [30.0, 20.0, 10.0])
    assert positive is not None and math.isclose(positive, 1.0)
    assert negative is not None and math.isclose(negative, -1.0)
    assert spearman([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]) is None


def test_ranked_sleeves_are_disjoint_and_deterministic() -> None:
    rows = [
        ({"ticker": ticker}, score)
        for ticker, score in (
            ("C", 3.0),
            ("A", 1.0),
            ("D", 4.0),
            ("B", 2.0),
        )
    ]
    sleeve = ranked_sleeves(rows, fraction=0.20)
    assert sleeve is not None
    assert [row["ticker"] for row, _ in sleeve.top] == ["D"]
    assert [row["ticker"] for row, _ in sleeve.bottom] == ["A"]


def test_turnover_uses_one_way_convention() -> None:
    current = {"A": 0.5, "B": 0.5}
    assert turnover(current, None) == (1.0, 1.0)
    assert turnover(current, {"A": 1.0}) == (0.5, 1.0)
    assert equal_weights(
        [({"ticker": "A"}, 1.0), ({"ticker": "B"}, 0.0)]
    ) == {"A": 0.5, "B": 0.5}


def test_aggregate_period_rows_keeps_cost_layers_separate() -> None:
    rows = [
        {
            "cross_section_count": "3",
            "rank_ic": "0.5",
            "top_mean_excess_return": "0.1",
            "bottom_mean_excess_return": "-0.1",
            "gross_top_bottom_spread": "0.2",
            "top_one_way_turnover": "1",
            "bottom_one_way_turnover": "1",
            "base_transaction_cost": "0.004",
            "stress_transaction_cost": "0.008",
            "net_top_bottom_spread_base": "0.196",
            "net_top_bottom_spread_stress": "0.192",
        }
    ]
    result = aggregate_period_rows(rows)
    assert result["period_count"] == 1
    assert result["row_count"] == 3
    assert result["mean_rank_ic"] == 0.5
    assert result["mean_net_top_bottom_spread_stress"] == 0.192
