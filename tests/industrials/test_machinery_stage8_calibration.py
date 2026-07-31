from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from industrials.core.config import load_yaml
from industrials.machinery.stage8_calibration import (
    COMPONENT_FIELDS,
    PANEL_BASE_FIELDS,
    PricePoint,
    _baseline_weights,
    _bootstrap_mean_lower_bound,
    _candidate_registry,
    _component_bounds,
    _execution_window,
    _fold_protocol_summary,
    _metric_gate,
    _normalized_weights,
    _product_aligned_objective,
    _ranked_evaluation_population,
    _return_window,
    _split_map,
    _turnover_and_cost,
    _write_quantile_diagnostics,
    _write_sleeve_membership,
    evaluate_weights,
    quintile_spread,
    read_csv_rows,
    stage8_paths,
)


def _point(day: date, value: float, source: str) -> PricePoint:
    return PricePoint(
        bar_date=day,
        value=value,
        source_id=source,
        price_basis="adj_close",
        open_value=value,
    )


def test_configured_baseline_is_not_rewritten() -> None:
    config = load_yaml(Path("industrials/machinery/config.yaml"))
    bounds = _component_bounds(config)

    weights = _baseline_weights(config, bounds)

    assert sum(weights.values()) == pytest.approx(1.0)
    assert weights == pytest.approx(
        {
            field: float(
                config["machinery_scoring"]["component_weights"][field]
            )
            for field in COMPONENT_FIELDS
        }
    )


def test_bounded_simplex_projection_handles_extreme_trial() -> None:
    config = load_yaml(Path("industrials/machinery/config.yaml"))
    bounds = _component_bounds(config)

    weights = _normalized_weights(
        {
            field: 1000.0 if index == 0 else -1000.0
            for index, field in enumerate(COMPONENT_FIELDS)
        },
        bounds,
    )

    assert sum(weights.values()) == pytest.approx(1.0)
    for field, value in weights.items():
        lower, upper = bounds[field]
        assert lower <= value <= upper


def test_return_window_never_mixes_price_sources() -> None:
    start = date(2025, 1, 2)
    incomplete = [
        _point(start + timedelta(days=index), 10.0 + index, "primary")
        for index in range(3)
    ]
    complete = [
        _point(start + timedelta(days=index), 20.0 + index, "fallback")
        for index in range(8)
    ]

    anchor, forward, reason = _return_window(
        {"primary": incomplete, "fallback": complete},
        asof=start.isoformat(),
        horizon=5,
        source_order=("primary", "fallback"),
    )

    assert reason == ""
    assert anchor is not None
    assert forward is not None
    assert anchor.source_id == forward.source_id == "fallback"
    assert forward.bar_date == start + timedelta(days=5)


def test_return_window_marks_unavailable_development_label() -> None:
    start = date(2025, 12, 29)
    series = [
        _point(start + timedelta(days=index), 10.0 + index, "primary")
        for index in range(3)
    ]

    anchor, forward, reason = _return_window(
        {"primary": series},
        asof=start.isoformat(),
        horizon=5,
        source_order=("primary",),
    )

    assert anchor is not None
    assert forward is None
    assert reason == "label_crosses_development_end"


def test_execution_window_enters_at_next_session_open() -> None:
    start = date(2025, 1, 2)
    series = [
        _point(start + timedelta(days=index), 10.0 + index, "primary")
        for index in range(8)
    ]

    entry, exit_point, reason, outcome_type = _execution_window(
        {"primary": series},
        asof=start.isoformat(),
        horizon=5,
        source_order=("primary",),
    )

    assert reason == ""
    assert outcome_type == "scheduled_horizon"
    assert entry is not None
    assert exit_point is not None
    assert entry.bar_date == start + timedelta(days=1)
    assert exit_point.bar_date == start + timedelta(days=6)


def test_execution_window_uses_reviewed_terminal_adjusted_close() -> None:
    start = date(2025, 1, 2)
    series = [
        _point(start + timedelta(days=index), 10.0 + index, "primary")
        for index in range(8)
    ]

    entry, exit_point, reason, outcome_type = _execution_window(
        {"primary": series},
        asof=start.isoformat(),
        horizon=5,
        source_order=("primary",),
        terminal_date=start + timedelta(days=3),
        horizon_end=start + timedelta(days=6),
    )

    assert reason == ""
    assert outcome_type == "terminal_membership_exit"
    assert entry is not None
    assert exit_point is not None
    assert entry.bar_date == start + timedelta(days=1)
    assert exit_point.bar_date == start + timedelta(days=3)


def _evaluation_row(
    ticker: str,
    score: float,
    execution_outcome: float | None,
    close_outcome: float,
) -> dict[str, str]:
    row = {
        "ticker": ticker,
        "asof_date": "2025-01-03",
        "calibration_cohort": f"cohort_{ticker}",
        "core_model_eligible_flag": "1",
        "execution_universe_eligible_flag": "1",
        "benchmark_execution_return_21d": "0",
        "execution_excess_return_21d": (
            "" if execution_outcome is None else str(execution_outcome)
        ),
        "forward_excess_return_21d": str(close_outcome),
    }
    row.update({field: str(score) for field in COMPONENT_FIELDS})
    return row


def _evaluation_config() -> dict[str, object]:
    return {
        "machinery_stage8": {
            "minimum_cross_section": 4,
            "top_quantile": 0.25,
            "minimum_positions": 1,
            "turnover_cost_bps": 20.0,
            "minimum_outcome_coverage": 0.75,
            "stability_penalty": 0.0,
            "maximum_turnover": 1.0,
            "maximum_cohort_share": 1.0,
        }
    }


def test_evaluation_uses_execution_not_close_outcomes() -> None:
    rows = [
        _evaluation_row("A", 90.0, 0.10, -0.10),
        _evaluation_row("B", 70.0, 0.03, -0.03),
        _evaluation_row("C", 30.0, -0.03, 0.03),
        _evaluation_row("D", 10.0, -0.10, 0.10),
    ]
    weights = {field: 1.0 / len(COMPONENT_FIELDS) for field in COMPONENT_FIELDS}

    metrics = evaluate_weights(
        _evaluation_config(),
        rows=rows,
        dates=["2025-01-03"],
        horizons=[21],
        weights=weights,
    )

    assert metrics["mean_ic_21d"] > 0
    assert metrics["mean_spread_21d"] == pytest.approx(0.20)
    assert metrics["mean_spread_net_21d"] == pytest.approx(0.196)


def test_missing_future_outcome_does_not_change_ranked_sleeve() -> None:
    rows = [
        _evaluation_row("A", 90.0, None, -0.10),
        _evaluation_row("B", 70.0, 0.03, -0.03),
        _evaluation_row("C", 30.0, -0.03, 0.03),
        _evaluation_row("D", 10.0, -0.10, 0.10),
    ]
    weights = {field: 1.0 / len(COMPONENT_FIELDS) for field in COMPONENT_FIELDS}

    metrics = evaluate_weights(
        _evaluation_config(),
        rows=rows,
        dates=["2025-01-03"],
        horizons=[21],
        weights=weights,
    )

    assert metrics["top_missing_outcome_dates_21d"] == 1
    assert metrics["n_top_dates_21d"] == 0
    assert metrics["date_rows"][0]["ranked_cross_section"] == 4


def test_turnover_uses_complete_equal_weight_vectors() -> None:
    first = {"A": 0.5, "B": 0.5}
    second = {"B": 1.0}

    initial = _turnover_and_cost(
        first,
        None,
        transaction_cost_rate=0.002,
    )
    replacement = _turnover_and_cost(
        second,
        first,
        transaction_cost_rate=0.002,
    )

    assert initial == pytest.approx((1.0, 1.0, 0.002))
    assert replacement == pytest.approx((0.5, 1.0, 0.002))


def test_split_map_purges_rows_near_later_boundaries() -> None:
    start = date(2020, 1, 3)
    dates = [
        (start + timedelta(days=7 * index)).isoformat()
        for index in range(20)
    ]

    splits = _split_map(
        dates,
        train_fraction=0.50,
        validation_fraction=0.25,
        purge_calendar_days=14,
    )

    assert splits[dates[0]] == "train"
    assert splits[dates[7]] == "train"
    assert splits[dates[8]] == "embargo"
    assert splits[dates[9]] == "embargo"
    assert splits[dates[10]] == "validation"
    assert splits[dates[12]] == "validation"
    assert splits[dates[13]] == "embargo"
    assert splits[dates[14]] == "embargo"
    assert splits[dates[15]] == "holdout"


def test_quintile_spread_refuses_constant_signal() -> None:
    assert quintile_spread([50.0] * 10, list(range(10))) is None


def test_quintile_spread_does_not_split_boundary_ties() -> None:
    scores = [0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 2.0, 2.0, 2.0, 2.0]
    returns = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.8, 0.9, 1.0, 1.1]

    spread = quintile_spread(scores, returns)

    assert spread == pytest.approx(0.85)


def test_stage8_panel_contract_has_unique_columns() -> None:
    assert len(PANEL_BASE_FIELDS) == len(set(PANEL_BASE_FIELDS))


def test_stage8_csv_reader_rejects_duplicate_columns(
    tmp_path: Path,
) -> None:
    path = tmp_path / "duplicate.csv"
    path.write_text("ticker,ticker\nAAA,BBB\n", encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate columns"):
        read_csv_rows(path)


def test_spread_is_diagnostic_not_a_product_gate() -> None:
    config = _evaluation_config()
    stage8 = config["machinery_stage8"]
    assert isinstance(stage8, dict)
    stage8["gates"] = {
        "minimum_evaluation_dates": 1,
        "minimum_mean_ic": 0.0,
        "minimum_ic_hit_rate": 0.50,
        "spread_gate_mode": "diagnostic_only",
        "minimum_mean_top_excess_net": 0.0,
        "minimum_top_excess_net_lcb": 0.0,
        "minimum_non_overlapping_top_dates": 1,
        "minimum_non_overlapping_top_excess_net": 0.0,
    }
    metrics = {
        "n_dates_21d": 10,
        "n_top_dates_21d": 10,
        "mean_ic_21d": 0.02,
        "ic_hit_rate_21d": 0.60,
        "mean_spread_net_21d": -0.50,
        "mean_top_excess_net_21d": 0.01,
        "top_excess_net_lower_confidence_bound_21d": 0.001,
        "n_non_overlapping_top_dates_21d": 3,
        "mean_non_overlapping_top_excess_net_21d": 0.01,
        "mean_outcome_coverage_21d": 1.0,
        "avg_top_turnover": 0.10,
        "avg_top_cohort_share": 0.25,
    }

    passed, reasons = _metric_gate(config, metrics, [21])

    assert passed
    assert reasons == []


def test_product_objective_rewards_top_sleeve_with_other_terms_fixed() -> None:
    config = load_yaml(Path("industrials/machinery/config.yaml"))
    common = {
        "mean_ic_21d": 0.01,
        "mean_spread_net_21d": -0.04,
        "top_excess_net_newey_west_se_21d": 0.002,
    }
    positive = {
        **common,
        "mean_top_excess_net_21d": 0.01,
        "mean_non_overlapping_top_excess_net_21d": 0.01,
    }
    negative = {
        **common,
        "mean_top_excess_net_21d": -0.01,
        "mean_non_overlapping_top_excess_net_21d": -0.01,
    }

    assert _product_aligned_objective(config, positive, [21]) > (
        _product_aligned_objective(config, negative, [21])
    )


def test_candidate_registry_is_capped_and_deterministic() -> None:
    config = load_yaml(Path("industrials/machinery/config.yaml"))
    bounds = _component_bounds(config)

    first, payload, first_hash = _candidate_registry(config, bounds)
    second, _, second_hash = _candidate_registry(config, bounds)

    assert first == second
    assert first_hash == second_hash
    assert len(first) == 6
    assert payload["search_policy"] == "preregistered_only"
    assert payload["prior_same_panel_static_trials"] == 192
    assert payload["prior_same_panel_walk_forward_trials"] == 336


def test_fold_protocol_requires_positive_median_and_block_rate() -> None:
    config = _evaluation_config()
    stage8 = config["machinery_stage8"]
    assert isinstance(stage8, dict)
    stage8["walk_forward"] = {
        "minimum_blocks": 4,
        "minimum_fold_product_pass_rate": 0.70,
        "minimum_positive_top_block_rate": 0.70,
    }
    passing = [
        {
            "objective": 0.1,
            "mean_top_excess_net_21d": 0.01,
            "mean_outcome_coverage_21d": 1.0,
            "avg_top_turnover": 0.1,
            "avg_top_cohort_share": 0.25,
        }
        for _ in range(4)
    ]
    failing = [*passing[:2], *[
        {
            **passing[0],
            "mean_top_excess_net_21d": -0.01,
        }
        for _ in range(2)
    ]]

    assert _fold_protocol_summary(config, passing, [21])["protocol_pass"]
    assert not _fold_protocol_summary(config, failing, [21])[
        "protocol_pass"
    ]


def test_sleeve_and_quantile_diagnostics_share_ranked_population(
    tmp_path: Path,
) -> None:
    config = _evaluation_config()
    stage8 = config["machinery_stage8"]
    assert isinstance(stage8, dict)
    stage8["production_universe_policy"] = "operating_only"
    stage8["diagnostics"] = {"quantile_buckets": 4}
    rows = [
        {**_evaluation_row("A", 90.0, 0.10, 0.10), "split_name": "train"},
        {**_evaluation_row("B", 70.0, 0.03, 0.03), "split_name": "train"},
        {**_evaluation_row("C", 30.0, -0.03, -0.03), "split_name": "train"},
        {**_evaluation_row("D", 10.0, -0.10, -0.10), "split_name": "train"},
    ]
    weights = {
        field: 1.0 / len(COMPONENT_FIELDS) for field in COMPONENT_FIELDS
    }
    paths = stage8_paths(tmp_path)

    population = _ranked_evaluation_population(
        config,
        date_rows=rows,
        weights=weights,
    )
    membership = _write_sleeve_membership(
        config,
        rows=rows,
        models={"candidate": weights},
        horizons=[21],
        paths=paths,
    )
    quantiles = _write_quantile_diagnostics(
        config,
        membership_rows=membership,
        paths=paths,
    )

    assert population is not None
    assert population.top[0][0]["ticker"] == "A"
    train_rows = [row for row in membership if row["split_name"] == "train"]
    assert next(row for row in train_rows if row["ticker"] == "A")[
        "configured_sleeve"
    ] == "top"
    assert next(row for row in train_rows if row["ticker"] == "D")[
        "configured_sleeve"
    ] == "bottom"
    assert all(row["rank_direction"] == "1_is_highest_score" for row in quantiles)


def test_bootstrap_lower_bound_is_deterministic() -> None:
    values = [0.01, 0.02, -0.01, 0.03, 0.02]

    first = _bootstrap_mean_lower_bound(
        values,
        confidence=0.90,
        simulations=500,
        seed=357,
    )
    second = _bootstrap_mean_lower_bound(
        values,
        confidence=0.90,
        simulations=500,
        seed=357,
    )

    assert first == second
