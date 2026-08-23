from __future__ import annotations

import json

from industrials.transportation.specialized_metric_freeze import (
    AcceptedDomain,
    accepted_summary_rows,
    compare_replay_with_panel,
)


def _panel(asof: str, ticker: str, value: float | None) -> dict[str, object]:
    values = {} if value is None else {"operating_ratio": value}
    statuses = {
        "operating_ratio": "NOT_DISCLOSED" if value is None else "REPORTED"
    }
    return {
        "asof_date": asof,
        "ticker": ticker,
        "horizon_sessions": "63",
        "metric_values_json": json.dumps(values),
        "metric_status_json": json.dumps(statuses),
    }


def _replay(ticker: str, value: float) -> dict[str, object]:
    return {
        "ticker": ticker,
        "metric_id": "operating_ratio",
        "value": value,
        "unit": "ratio",
        "period_end": "2019-12-31",
        "filing_date": "2020-02-01",
        "accepted_at": "2020-02-01T20:00:00Z",
        "comparability_class": "rail_operating_ratio",
        "definition_basis": "operating_expense_over_revenue",
        "replay_status": "ACCEPTED",
    }


def test_accepted_summary_rows_freezes_only_passed_domains() -> None:
    result = accepted_summary_rows(
        [
            {
                "cohort": "surface",
                "metric_id": "operating_ratio",
                "comparison_domain_id": "rail",
                "calibration_gate": "PASS",
            },
            {
                "cohort": "tanker",
                "metric_id": "fleet_age",
                "comparison_domain_id": "tankers",
                "calibration_gate": "FAIL",
            },
        ]
    )
    assert [(row["metric_id"], row["comparison_domain_id"]) for row in result] == [
        ("operating_ratio", "rail")
    ]


def test_input_delta_detects_new_fills_and_changed_values_without_outcomes() -> None:
    domain = AcceptedDomain(
        cohort="surface",
        metric_id="operating_ratio",
        domain_id="rail",
        tickers=("A", "B"),
        minimum_breadth=2,
        max_staleness_days=500,
    )
    panel = [
        _panel("2020-03-01", "A", 0.8),
        _panel("2020-03-01", "B", None),
        _panel("2021-03-01", "A", 0.8),
        _panel("2021-03-01", "B", 0.9),
    ]

    detail, summary = compare_replay_with_panel(
        panel_rows=panel,
        replay_rows=[_replay("A", 0.8), _replay("B", 0.85)],
        domains=[domain],
    )

    assert len(detail) == 4
    assert summary[0]["prior_panel_passing_date_count"] == 1
    assert summary[0]["new_replay_passing_date_count"] == 2
    assert summary[0]["new_fill_cell_count"] == 1
    assert summary[0]["changed_value_cell_count"] == 1
    assert summary[0]["new_information_cell_count"] == 2
    assert summary[0]["input_delta_gate"] == "PASS"
