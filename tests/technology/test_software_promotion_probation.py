from __future__ import annotations

from technology.software_infrastructure.promotion_probation import evaluate_holdings, select_holdings


def score_rows(order: list[str]) -> list[dict[str, object]]:
    return [
        {"ticker": ticker, "final_score": 100.0 - index, "final_rank": index + 1}
        for index, ticker in enumerate(order)
    ]


def test_probation_selects_each_model_from_the_same_eligible_universe() -> None:
    production = score_rows(["A", "B", "C", "D", "E"])
    rollback = score_rows(["E", "D", "C", "B", "X"])
    holdings = select_holdings(production, rollback, quantile=0.50, minimum=2)
    promoted = [row["ticker"] for row in holdings if row["model_role"] == "promoted"]
    reverted = [row["ticker"] for row in holdings if row["model_role"] == "rollback"]
    assert promoted == ["B", "C"]
    assert reverted == ["E", "D"]


def test_probation_evaluation_compares_frozen_equal_weight_baskets() -> None:
    holdings = [
        {"model_role": "promoted", "ticker": "A"},
        {"model_role": "promoted", "ticker": "B"},
        {"model_role": "rollback", "ticker": "C"},
        {"model_role": "rollback", "ticker": "D"},
    ]
    sessions = ["2026-09-01", "2026-09-02", "2026-09-03"]
    prices = {
        "A": {sessions[0]: 10.0, sessions[1]: 11.0, sessions[2]: 12.0},
        "B": {sessions[0]: 20.0, sessions[1]: 21.0, sessions[2]: 22.0},
        "C": {sessions[0]: 10.0, sessions[1]: 10.0, sessions[2]: 10.0},
        "D": {sessions[0]: 20.0, sessions[1]: 20.0, sessions[2]: 20.0},
    }
    rows, summary = evaluate_holdings(holdings, prices, sessions, cost_per_side=0.002)
    assert len(rows) == 6
    assert summary["promoted"]["net_return"] > summary["rollback"]["net_return"]
    assert summary["promoted"]["price_coverage"] == 1.0
    assert summary["rollback"]["price_coverage"] == 1.0
