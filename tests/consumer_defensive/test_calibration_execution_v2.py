from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from consumer_defensive.core import calibration_execution_v2 as execution_v2
from consumer_defensive.core.calibration_preregistration_v2 import (
    build_candidate_registry,
    build_preregistration,
)
from consumer_defensive.core.calibration_execution_v2 import (
    _build_folds,
    _candidate_score,
    _holdings_for_date,
    _portfolio_for_date,
    _realized_daily_path,
    _turnover,
)
from consumer_defensive.core.calibration_v2 import WalkForwardFold
from consumer_defensive.core.config import load_config
from consumer_defensive.core.promotion_framework_v2 import (
    REQUIRED_COHORTS,
    REQUIRED_HORIZONS,
    canonical_sha256,
    load_framework,
)
from consumer_defensive.core.scoring_features import CORE_COMPONENT_SPECS
from consumer_defensive.core.shared_services import load_shared_service_contract


ROOT = Path(__file__).resolve().parents[2]


def _candidate() -> dict[str, object]:
    weight = 0.95 / len(CORE_COMPONENT_SPECS)
    return {
        "candidate_id": "candidate_001",
        "core_weights": {spec.name: weight for spec in CORE_COMPONENT_SPECS},
        "specialized_weights": {"validated_metric": 0.05},
    }


def _row(ticker: str, *, asof: str = "2022-01-31", score: float = 60.0):
    return {
        "asof_date": asof,
        "ticker": ticker,
        "membership_eligible_flag": 1,
        "investable_flag": 1,
        "_component_scores": {spec.name: score for spec in CORE_COMPONENT_SPECS},
        "_component_quality": {spec.name: 1.0 for spec in CORE_COMPONENT_SPECS},
        "_component_raw_values": {"avg_dollar_volume_63d": 10_000_000.0},
        "_specialized_scores": {},
    }


def test_turnover_includes_cash_and_initial_entry() -> None:
    assert _turnover({}, {"AAA": 0.5, "BBB": 0.5}) == pytest.approx(1.0)
    assert _turnover({"AAA": 0.5, "BBB": 0.5}, {"AAA": 0.5, "CCC": 0.5}) == pytest.approx(0.5)


def test_missing_specialized_score_is_neutral_without_redistribution() -> None:
    row = _row("AAA")
    score, eligible = _candidate_score(
        row,
        _candidate(),
        short_interest_birthdate="2021-07-01",
        minimum_quality=0.65,
        maximum_missing=0.35,
    )
    assert eligible is True
    assert score == pytest.approx(0.95 * 60.0 + 0.05 * 50.0)


def test_structural_and_optional_missingness_do_not_block_prebirth_rows() -> None:
    row = _row("AAA", asof="2020-12-31")
    row["_component_quality"] = {spec.name: 0.0 for spec in CORE_COMPONENT_SPECS}
    required = [
        spec.name
        for spec in CORE_COMPONENT_SPECS
        if spec.rank_requirement == "required"
    ]
    for name in [*required, "gross_margin", "operating_margin"]:
        row["_component_quality"][name] = 1.0
    candidate = {
        "candidate_id": "candidate_era_test",
        "core_weights": {
            spec.name: 1.0 / len(CORE_COMPONENT_SPECS)
            for spec in CORE_COMPONENT_SPECS
        },
        "specialized_weights": {},
    }
    _, prebirth = _candidate_score(
        row,
        candidate,
        short_interest_birthdate="2021-07-01",
        minimum_quality=0.65,
        maximum_missing=0.35,
    )
    row["asof_date"] = "2022-01-31"
    _, postbirth = _candidate_score(
        row,
        candidate,
        short_interest_birthdate="2021-07-01",
        minimum_quality=0.65,
        maximum_missing=0.35,
    )
    assert prebirth is True
    assert postbirth is False


def test_holdings_are_selected_without_any_label_argument() -> None:
    rows = [_row(f"T{index:02d}", score=50.0 + index) for index in range(8)]
    snapshot = _holdings_for_date(
        rows,
        candidate=_candidate(),
        short_interest_birthdate="2021-07-01",
        minimum_quality=0.65,
        maximum_missing=0.35,
    )
    assert set(snapshot["weights"]) == {"T03", "T04", "T05", "T06", "T07"}
    assert set(snapshot["weights"].values()) == {0.2}
    assert snapshot["liquidity_capacity_ratio"] >= 1.0


def test_missing_outcome_fails_after_holdings_are_frozen() -> None:
    rows = [_row(f"T{index:02d}", score=50.0 + index) for index in range(8)]
    with pytest.raises(ValueError, match="completed frozen label"):
        _portfolio_for_date(
            rows,
            candidate=_candidate(),
            horizon=21,
            labels={},
            short_interest_birthdate="2021-07-01",
            minimum_quality=0.65,
            maximum_missing=0.35,
        )


def test_fold_builder_uses_even_nonoverlapping_outer_census() -> None:
    origin = date(2018, 1, 31)
    signals = [(origin + timedelta(days=31 * index)).isoformat() for index in range(100)]
    folds = _build_folds(signals, completion_by_date={value: value for value in signals})
    assert len(folds) >= 8
    assert len(folds) % 2 == 0
    tests = [value for fold in folds for value in fold.test_dates]
    assert len(tests) == len(set(tests))
    assert len(tests) >= 30


def test_shortest_cohort_history_retains_thirty_oos_after_long_horizon_purge() -> None:
    origin = date(2019, 1, 1)
    signals = [(origin + timedelta(days=30 * index)).isoformat() for index in range(81)]
    completion = {
        signal: (date.fromisoformat(signal) + timedelta(days=180)).isoformat()
        for signal in signals
    }
    folds = _build_folds(signals, completion_by_date=completion)
    assert len(folds) % 2 == 0
    assert sum(len(fold.test_dates) for fold in folds) >= 30
    assert all(len(fold.train_dates) >= 30 for fold in folds)
    assert all(fold.validation_dates for fold in folds)


def _actual_style_fold_inputs(
    horizon: int,
) -> tuple[list[str], dict[str, str], list[str]]:
    cursor = date(2018, 1, 1)
    calendar: list[date] = []
    while cursor <= date(2026, 12, 31):
        if cursor.weekday() < 5:
            calendar.append(cursor)
        cursor += timedelta(days=1)
    month_ends: dict[tuple[int, int], date] = {}
    for session in calendar:
        month_ends[(session.year, session.month)] = session
    signals = [
        session
        for _, session in sorted(month_ends.items())
        if date(2019, 1, 1) <= session <= date(2026, 1, 31)
    ]
    calendar_index = {session: index for index, session in enumerate(calendar)}
    completion = {
        signal.isoformat(): calendar[
            calendar_index[signal] + 1 + horizon
        ].isoformat()
        for signal in signals
    }
    ready = [
        signal.isoformat()
        for signal in signals
        if signal >= date(2021, 7, 1)
        and (signal.year, signal.month) != (2022, 4)
    ]
    return [signal.isoformat() for signal in signals], completion, ready


@pytest.mark.parametrize(
    ("horizon", "expected_raw", "expected_rejected"),
    ((21, 13, 3), (63, 13, 3), (126, 12, 2)),
)
def test_actual_style_readiness_census_retains_ten_folds_and_thirty_oos(
    horizon: int,
    expected_raw: int,
    expected_rejected: int,
) -> None:
    signals, completion, ready = _actual_style_fold_inputs(horizon)
    diagnostics: dict[str, object] = {}
    folds = _build_folds(
        signals,
        completion_by_date=completion,
        portfolio_ready_dates=ready,
        diagnostics_out=diagnostics,
    )

    assert len(signals) == 85
    assert len(ready) == 54
    assert len(folds) == 10
    assert sum(len(fold.test_dates) for fold in folds) == 30
    assert diagnostics["raw_admissible_fold_count"] == expected_raw
    assert (
        diagnostics["portfolio_readiness_rejected_fold_count"]
        == expected_rejected
    )
    assert diagnostics["post_readiness_fold_count"] == 10
    assert diagnostics["odd_fold_rule_rejected_count"] == 0
    assert diagnostics["split_signal_date_census"] == signals
    assert diagnostics["portfolio_ready_signal_date_census"] == ready
    assert {
        value
        for row in diagnostics["rejected_folds"]
        for value in row["unready_validation_dates"]
    } == {"2022-04-29"}
    assert all(not row["unready_test_dates"] for row in diagnostics["rejected_folds"])
    assert all(
        row["unready_train_date_count"] > 0
        for row in diagnostics["retained_fold_train_readiness"]
    )
    assert diagnostics["train_partition_role"] == (
        "chronology_and_label_completion_purge_burn_in_no_candidate_fit"
    )
    assert diagnostics["candidate_fit_performed_on_train"] is False
    assert folds[0].test_dates == (
        date(2023, 7, 31),
        date(2023, 8, 31),
        date(2023, 9, 29),
    )
    assert folds[-1].test_dates == (
        date(2025, 10, 31),
        date(2025, 11, 28),
        date(2025, 12, 31),
    )
    for fold in folds:
        validation_start = fold.validation_dates[0]
        test_start = fold.test_dates[0]
        assert all(
            date.fromisoformat(completion[value.isoformat()]) < validation_start
            for value in fold.train_dates
        )
        assert all(
            date.fromisoformat(completion[value.isoformat()]) < test_start
            for value in fold.validation_dates
        )


@pytest.mark.parametrize(
    ("scope", "diagnostic_field"),
    (
        ("validation", "unready_validation_dates"),
        ("test", "unready_test_dates"),
    ),
)
def test_unready_validation_or_test_rejects_whole_fold_before_even_rule(
    scope: str,
    diagnostic_field: str,
) -> None:
    signals, completion, _ = _actual_style_fold_inputs(21)
    all_ready_diagnostics: dict[str, object] = {}
    all_ready_folds = _build_folds(
        signals,
        completion_by_date=completion,
        portfolio_ready_dates=signals,
        diagnostics_out=all_ready_diagnostics,
    )
    unready = (
        all_ready_folds[0].validation_dates[0].isoformat()
        if scope == "validation"
        else signals[-2]
    )
    ready = [value for value in signals if value != unready]
    diagnostics: dict[str, object] = {}
    folds = _build_folds(
        signals,
        completion_by_date=completion,
        portfolio_ready_dates=ready,
        diagnostics_out=diagnostics,
    )

    assert all_ready_diagnostics["raw_admissible_fold_count"] == 13
    assert len(folds) == 12
    assert diagnostics["post_readiness_fold_count"] == 12
    assert diagnostics["portfolio_readiness_rejected_fold_count"] == 1
    assert diagnostics["odd_fold_rule_rejected_count"] == 0
    rejected = diagnostics["rejected_folds"]
    assert len(rejected) == 1
    assert rejected[0]["reason"] == "validation_or_test_portfolio_unready"
    assert rejected[0][diagnostic_field] == [unready]
    assert all(
        date_value.isoformat() != unready
        for fold in folds
        for date_value in (*fold.validation_dates, *fold.test_dates)
    )


def _price_contract(
    ticker: str,
    *,
    observed_first: str,
    observed_last: str,
    internal_carried_dates: tuple[str, ...] = (),
    terminal_transition_carried_dates: tuple[str, ...] = (),
    terminal_value_start_date: str = "",
    terminal_event_sha256: str = "",
    terminal_last_trade_date: str = "",
    terminal_type: str = "",
) -> dict[str, object]:
    return {
        "ticker": ticker,
        "observed_first_bar_date": observed_first,
        "observed_last_bar_date": observed_last,
        "internal_carried_dates": list(internal_carried_dates),
        "terminal_transition_carried_dates": list(terminal_transition_carried_dates),
        "terminal_value_start_date": terminal_value_start_date,
        "terminal_event_sha256": terminal_event_sha256,
        "terminal_last_trade_date": terminal_last_trade_date,
        "terminal_type": terminal_type,
    }


def _assert_attestations_reconcile(
    rows: list[object],
    attestations: list[dict[str, object]],
) -> None:
    assert len(attestations) == len(rows)
    for row, attestation in zip(rows, attestations, strict=True):
        assert attestation["observation_id"] == row.observation_id
        assert (
            attestation["source_portfolio_observation_id"]
            == row.source_portfolio_observation_id
        )
        assert attestation["fold_id"] == row.fold_id
        assert attestation["cohort"] == row.cohort
        assert attestation["horizon_sessions"] == row.horizon_sessions
        assert attestation["return_date"] == row.return_date.isoformat()
        assert float(attestation["prior_nav"]) > 0.0
        positions = list(attestation["positions"])
        prior_reconciled = float(attestation["entry_cash_value"]) + sum(
            float(position["prior_value"]) for position in positions
        )
        current_reconciled = float(attestation["entry_cash_value"]) + sum(
            float(position["current_value"]) for position in positions
        )
        assert prior_reconciled == pytest.approx(float(attestation["prior_nav"]))
        assert current_reconciled == pytest.approx(float(attestation["current_nav"]))
        gross = (
            float(attestation["current_nav"])
            / float(attestation["prior_nav"])
            - 1.0
        )
        assert float(attestation["gross_return"]) == pytest.approx(gross)
        assert row.strategy_return == pytest.approx(gross)
        assert float(attestation["net_return"]) == pytest.approx(
            gross - float(attestation["transaction_cost"])
        )
        assert row.net_strategy_return == pytest.approx(
            float(attestation["net_return"])
        )
        assert 0.0 <= float(attestation["gross_exposure_ratio"]) <= 1.0 + 1e-12


def _cumulative_gross_return(rows: list[object]) -> float:
    nav = 1.0
    for row in rows:
        nav *= 1.0 + row.strategy_return
    return nav - 1.0


def test_realized_path_is_linked_to_each_exact_portfolio() -> None:
    calendar = [f"2022-01-{day:02d}" for day in range(3, 13)]
    prices = {
        ticker: {session: 100.0 + index for index, session in enumerate(calendar)}
        for ticker in ("AAA", "BBB")
    }
    contracts = {
        ticker: _price_contract(
            ticker,
            observed_first=calendar[0],
            observed_last=calendar[-1],
        )
        for ticker in prices
    }
    snapshots = [
        {
            "asof_date": calendar[0],
            "observation_id": "portfolio_001",
            "fold_id": "wf_001",
            "weights": {"AAA": 0.5, "BBB": 0.5},
            "transaction_cost": 0.002,
        },
        {
            "asof_date": calendar[4],
            "observation_id": "portfolio_002",
            "fold_id": "wf_002",
            "weights": {"AAA": 0.5, "BBB": 0.5},
            "transaction_cost": 0.0,
        },
    ]
    rows, attestations = _realized_daily_path(
        snapshots,
        cohort="beverages",
        horizon=21,
        calendar=calendar,
        prices=prices,
        price_special_states={},
        price_series_contracts=contracts,
    )
    assert {row.source_portfolio_observation_id for row in rows} == {
        "portfolio_001",
        "portfolio_002",
    }
    assert len({row.return_date for row in rows}) == len(rows)
    assert sum(row.transaction_cost > 0 for row in rows) == 1
    assert all(row.horizon_sessions == 21 for row in rows)
    _assert_attestations_reconcile(rows, attestations)


def test_realized_path_uses_buy_and_hold_sleeve_nav_not_daily_fixed_weights() -> None:
    calendar = ["2022-01-03", "2022-01-04", "2022-01-05", "2022-01-06"]
    prices = {
        "AAA": dict(zip(calendar, (100.0, 100.0, 200.0, 200.0), strict=True)),
        "BBB": dict(zip(calendar, (100.0, 100.0, 100.0, 200.0), strict=True)),
    }
    contracts = {
        ticker: _price_contract(
            ticker,
            observed_first=calendar[0],
            observed_last=calendar[-1],
        )
        for ticker in prices
    }
    snapshots = [
        {
            "asof_date": calendar[0],
            "observation_id": "portfolio_buy_hold",
            "fold_id": "wf_001",
            "weights": {"AAA": 0.5, "BBB": 0.5},
            "transaction_cost": 0.0,
        }
    ]
    rows, attestations = _realized_daily_path(
        snapshots,
        cohort="beverages",
        horizon=21,
        calendar=calendar,
        prices=prices,
        price_special_states={},
        price_series_contracts=contracts,
    )
    assert [row.strategy_return for row in rows] == pytest.approx([0.5, 1.0 / 3.0])
    assert _cumulative_gross_return(rows) == pytest.approx(1.0)
    final_positions = {
        position["ticker"]: position
        for position in attestations[-1]["positions"]
    }
    assert float(final_positions["AAA"]["units"]) == pytest.approx(0.005)
    assert float(final_positions["BBB"]["units"]) == pytest.approx(0.005)
    assert float(attestations[-1]["current_nav"]) == pytest.approx(2.0)
    _assert_attestations_reconcile(rows, attestations)


def test_verified_cash_terminal_pays_on_event_day_then_carries_cash() -> None:
    calendar = [
        "2024-09-30",
        "2024-10-01",
        "2024-10-02",
        "2024-10-03",
        "2024-10-04",
        "2024-10-07",
        "2024-10-08",
        "2024-10-09",
    ]
    prices = {
        "VGR": dict(
            zip(
                calendar,
                (14.92, 14.91, 14.93, 14.96, 14.99, 15.0, 15.0, 15.0),
                strict=True,
            )
        )
    }
    terminal_sha = "a" * 64
    contracts = {
        "VGR": _price_contract(
            "VGR",
            observed_first=calendar[0],
            observed_last="2024-10-04",
            terminal_value_start_date="2024-10-07",
            terminal_event_sha256=terminal_sha,
            terminal_last_trade_date="2024-10-04",
            terminal_type="cash",
        )
    }
    snapshots = [
        {
            "asof_date": "2024-09-30",
            "observation_id": "portfolio_vgr",
            "fold_id": "wf_vgr",
            "weights": {"VGR": 1.0},
            "transaction_cost": 0.0,
        }
    ]
    rows, attestations = _realized_daily_path(
        snapshots,
        cohort="household_personal_tobacco",
        horizon=21,
        calendar=calendar,
        prices=prices,
        price_special_states={
            "VGR": {
                session: {
                    "provenance": "terminal_value",
                    "calculation_status": "resolved_fixed_terminal_value",
                    "cash_component": 15.0,
                    "market_component": 0.0,
                    "terminal_event_sha256": terminal_sha,
                }
                for session in ("2024-10-07", "2024-10-08", "2024-10-09")
            }
        },
        price_series_contracts=contracts,
    )
    by_date = {row.return_date.isoformat(): row for row in rows}
    proof_by_date = {
        proof["return_date"]: proof
        for proof in attestations
    }
    assert by_date["2024-10-07"].strategy_return == pytest.approx(15.0 / 14.99 - 1.0)
    assert by_date["2024-10-08"].strategy_return == pytest.approx(0.0)
    assert by_date["2024-10-09"].strategy_return == pytest.approx(0.0)
    assert _cumulative_gross_return(rows) == pytest.approx(15.0 / 14.91 - 1.0)
    terminal_position = proof_by_date["2024-10-07"]["positions"][0]
    cash_carry_position = proof_by_date["2024-10-08"]["positions"][0]
    assert terminal_position["terminal_event_sha256"] == terminal_sha
    assert terminal_position["current_mark"] == pytest.approx(15.0)
    assert terminal_position["current_provenance"] != "observed"
    assert cash_carry_position["terminal_event_sha256"] == terminal_sha
    assert cash_carry_position["current_value"] == pytest.approx(
        terminal_position["current_value"]
    )
    assert proof_by_date["2024-10-07"]["gross_exposure_ratio"] == pytest.approx(0.0)
    assert proof_by_date["2024-10-08"]["gross_exposure_ratio"] == pytest.approx(0.0)
    _assert_attestations_reconcile(rows, attestations)


def test_realized_path_rejects_entry_after_terminal_value_start() -> None:
    calendar = ["2024-10-07", "2024-10-08", "2024-10-09"]
    prices = {"VGR": {session: 15.0 for session in calendar}}
    contracts = {
        "VGR": _price_contract(
            "VGR",
            observed_first="2017-11-28",
            observed_last="2024-10-04",
            terminal_value_start_date="2024-10-07",
            terminal_event_sha256="a" * 64,
            terminal_last_trade_date="2024-10-04",
            terminal_type="cash",
        )
    }
    snapshots = [
        {
            "asof_date": "2024-10-07",
            "observation_id": "portfolio_after_terminal",
            "fold_id": "wf_terminal",
            "weights": {"VGR": 1.0},
            "transaction_cost": 0.0,
        }
    ]
    with pytest.raises(ValueError, match="observed tradable entry|after terminal"):
        _realized_daily_path(
            snapshots,
            cohort="household_personal_tobacco",
            horizon=21,
            calendar=calendar,
            prices=prices,
            price_special_states={
                "VGR": {
                    session: {
                        "provenance": "terminal_value",
                        "calculation_status": "resolved_fixed_terminal_value",
                        "cash_component": 15.0,
                        "market_component": 0.0,
                        "terminal_event_sha256": "a" * 64,
                    }
                    for session in calendar
                }
            },
            price_series_contracts=contracts,
        )


def test_internal_missing_session_carries_then_realizes_cumulative_move() -> None:
    calendar = ["2022-01-03", "2022-01-04", "2022-01-05", "2022-01-06"]
    prices = {
        "AAA": {
            "2022-01-03": 100.0,
            "2022-01-04": 100.0,
            "2022-01-05": 100.0,
            "2022-01-06": 110.0,
        }
    }
    contracts = {
        "AAA": _price_contract(
            "AAA",
            observed_first=calendar[0],
            observed_last=calendar[-1],
            internal_carried_dates=("2022-01-05",),
        )
    }
    snapshots = [
        {
            "asof_date": calendar[0],
            "observation_id": "portfolio_internal_gap",
            "fold_id": "wf_gap",
            "weights": {"AAA": 1.0},
            "transaction_cost": 0.0,
        }
    ]
    rows, attestations = _realized_daily_path(
        snapshots,
        cohort="beverages",
        horizon=21,
        calendar=calendar,
        prices=prices,
        price_special_states={
            "AAA": {
                "2022-01-05": {
                    "provenance": "internal_carry",
                    "calculation_status": "carried_internal_missing_session",
                    "cash_component": 0.0,
                    "market_component": 100.0,
                    "terminal_event_sha256": "",
                }
            }
        },
        price_series_contracts=contracts,
    )
    assert [row.strategy_return for row in rows] == pytest.approx([0.0, 0.1])
    assert _cumulative_gross_return(rows) == pytest.approx(0.1)
    assert attestations[0]["positions"][0]["current_provenance"] == "internal_carry"
    assert attestations[1]["positions"][0]["current_provenance"] == "observed"
    _assert_attestations_reconcile(rows, attestations)

    incomplete = {"AAA": dict(prices["AAA"])}
    incomplete["AAA"].pop("2022-01-05")
    with pytest.raises(ValueError, match="price path is incomplete"):
        _realized_daily_path(
            snapshots,
            cohort="beverages",
            horizon=21,
            calendar=calendar,
            prices=incomplete,
            price_special_states={},
            price_series_contracts={
                "AAA": _price_contract(
                    "AAA",
                    observed_first=calendar[0],
                    observed_last=calendar[-1],
                )
            },
        )

def test_evaluate_horizon_hashes_path_attestation_lists_with_generic_hasher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cohort = sorted(REQUIRED_COHORTS)[0]
    signal = "2024-01-31"
    calendar = [
        (date.fromisoformat(signal) + timedelta(days=offset)).isoformat()
        for offset in range(30)
    ]
    completion = calendar[22]
    fold = WalkForwardFold(
        fold_id="wf_hash_regression",
        train_dates=(date(2023, 11, 30),),
        validation_dates=(date.fromisoformat(signal),),
        test_dates=(date.fromisoformat(signal),),
        purged_train_count=0,
        purged_validation_count=0,
    )
    candidate = {
        "candidate_id": "candidate_hash_regression",
        "candidate_kind": "stage7_seed",
    }

    monkeypatch.setattr(execution_v2, "_true_month_ends", lambda *_args, **_kwargs: {signal})
    monkeypatch.setattr(
        execution_v2,
        "_completion_date",
        lambda *_args, **_kwargs: completion,
    )
    monkeypatch.setattr(
        execution_v2,
        "_holdings_for_date",
        lambda *_args, **_kwargs: {"AAA": 1.0},
    )
    monkeypatch.setattr(
        execution_v2,
        "_build_folds",
        lambda *_args, **_kwargs: (fold,),
    )
    monkeypatch.setattr(
        execution_v2,
        "_candidate_path",
        lambda _candidate, *, dates, **_kwargs: [
            {"asof_date": value, "net_alpha": 0.01}
            for value in dates
        ],
    )

    def fake_portfolio(
        _rows: object,
        *,
        candidate: dict[str, object],
        **_kwargs: object,
    ) -> dict[str, object]:
        return {
            "asof_date": signal,
            "candidate_id": candidate["candidate_id"],
            "weights": {"AAA": 1.0},
            "gross_return": 0.02,
            "benchmark_return": 0.01,
            "liquidity_capacity_ratio": 2.0,
        }

    monkeypatch.setattr(execution_v2, "_portfolio_for_date", fake_portfolio)
    expected_candidate_matrix = {
        str(candidate["candidate_id"]): {fold.fold_id: 0.01}
    }
    expected_candidate_hash = canonical_sha256(
        {"value": expected_candidate_matrix}
    )

    def fake_evaluate_cohort(
        *_args: object,
        **kwargs: object,
    ) -> dict[str, object]:
        assert kwargs["candidate_performance_by_fold"] == expected_candidate_matrix
        return {
            "performance": {"sentinel": 1},
            "evidence": {
                "sentinel": 2,
                "candidate_matrix_sha256": expected_candidate_hash,
            },
        }

    monkeypatch.setattr(
        execution_v2,
        "evaluate_cohort",
        fake_evaluate_cohort,
    )
    prices = {"AAA": {session: 100.0 for session in calendar}}
    result, detail, attestations = execution_v2._evaluate_horizon(
        cohort=cohort,
        horizon=21,
        candidates=[candidate],
        panel_rows=[{"cohort_id": cohort, "asof_date": signal}],
        labels={},
        calendar=calendar,
        prices=prices,
        price_special_states={},
        price_series_contracts={
            "AAA": _price_contract(
                "AAA",
                observed_first=calendar[0],
                observed_last=calendar[-1],
            )
        },
        decision_asof=date(2024, 3, 31),
        framework={},
        short_interest_birthdate="2021-07-01",
        minimum_quality=0.65,
        maximum_missing=0.35,
    )

    assert result["performance"] == {"sentinel": 1}
    assert (
        result["evidence"]["candidate_matrix_sha256"]
        == expected_candidate_hash
    )
    assert detail["candidate_matrix_sha256"] == expected_candidate_hash
    assert isinstance(attestations, list)
    assert attestations
    assert detail["realized_path_attestation_sha256"] == execution_v2._sha(
        attestations
    )
    assert detail["realized_daily_return_count"] == len(attestations)


def _sequence1_contracts() -> tuple[object, dict[str, object], dict[str, object]]:
    bundle = load_config(ROOT / "consumer_defensive/config.yaml")
    framework = load_framework(
        ROOT
        / "consumer_defensive/data/consumer_defensive_promotion_framework_v2.yaml"
    )
    shared = load_shared_service_contract(
        ROOT
        / "consumer_defensive/data/consumer_defensive_shared_service_contract_v1.yaml"
    )
    return bundle, framework, shared


def _passing_sequence1_cell(
    cohort: str,
    horizon: int,
) -> tuple[dict[str, object], dict[str, object], list[dict[str, object]]]:
    performance: dict[str, object] = {
        "paired_net_alpha_lcb": 0.01,
        "net_alpha_mean": 0.02,
        "absolute_profit_factor": 1.20,
        "relative_profit_factor": 1.20,
        "robust_profit_factor": 1.10,
        "deflated_sharpe_ratio": 0.90,
        "probability_of_backtest_overfitting": 0.10,
        "maximum_drawdown": 0.10,
        "expected_shortfall_95": -0.05,
        "turnover": 0.50,
        "average_transaction_cost": 0.001,
        "liquidity_capacity_ratio": 2.0,
        "winner_concentration_hhi": 0.20,
        "maximum_single_name_weight": 0.20,
        "paired_observation_count": 30,
        "positive_return_count": 20,
        "negative_return_count": 10,
    }
    def evidence_hash(label: str) -> str:
        return execution_v2._sha(
            {"cohort": cohort, "horizon": horizon, "label": label}
        )
    evidence: dict[str, object] = {
        "evaluation_role": "outer_test",
        "horizon_sessions": horizon,
        "observation_count": 30,
        "observation_ids_sha256": evidence_hash("observations"),
        "fold_ids_sha256": evidence_hash("folds"),
        "signal_start_date": "2020-01-31",
        "signal_end_date": "2025-01-31",
        "latest_label_completion_date": "2025-08-14",
        "candidate_matrix_sha256": evidence_hash("candidate_matrix"),
        "selected_weights_sha256": evidence_hash("selected_weights"),
        "realized_return_stream_sha256": evidence_hash("realized_stream"),
        "realized_return_count": 100,
        "realized_return_start_date": "2020-02-03",
        "realized_return_end_date": "2026-08-14",
    }
    path: list[dict[str, object]] = [
        {
            "observation_id": f"realized_{cohort}_{horizon}",
            "source_portfolio_observation_id": f"portfolio_{cohort}_{horizon}",
            "fold_id": "wf_synthetic",
            "cohort": cohort,
            "horizon_sessions": horizon,
            "signal_date": "2025-01-31",
            "entry_date": "2025-02-03",
            "return_date": "2025-02-04",
            "prior_nav": 1.0,
            "current_nav": 1.001,
            "entry_cash_value": 0.0,
            "cash_value": 0.0,
            "market_exposure_value": 1.001,
            "gross_return": 0.001,
            "transaction_cost": 0.0,
            "net_return": 0.001,
            "gross_exposure_ratio": 1.0,
            "positions": [
                {
                    "ticker": "SYNTH",
                    "units": 0.01,
                    "prior_mark": 100.0,
                    "current_mark": 100.1,
                    "prior_value": 1.0,
                    "current_value": 1.001,
                    "prior_provenance": "observed",
                    "current_provenance": "observed",
                    "terminal_event_sha256": "",
                }
            ],
        }
    ]
    detail = {
        "cohort": cohort,
        "horizon_sessions": horizon,
        "realized_path_attestation_sha256": execution_v2._sha(path),
        "synthetic_fast_path": True,
    }
    return {"performance": performance, "evidence": evidence}, detail, path


def test_sequence1_fast_synthetic_traverses_all_cells_and_publishes_bound_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, framework, shared = _sequence1_contracts()
    stage6c = {
        "stage6c_run_id": 7,
        "asof_date": "2026-08-14",
        "history_start": "2019-01-02",
        "status": "complete",
        "panel_sha256": "a" * 64,
        "panel_row_count": 120,
        "evaluation_date_count": 80,
    }
    campaign = {
        "campaign_id": "synthetic_sequence1_campaign",
        "registry_sha256": "b" * 64,
    }
    registry = build_candidate_registry(
        bundle,
        framework=framework,
        shared_contract=shared,
        asof_date=str(stage6c["asof_date"]),
        stage6c_run=stage6c,
        campaign_summary=campaign,
        accepted_factor_cells=[],
    )
    preregistration = build_preregistration(
        bundle,
        repository_root=ROOT,
        framework=framework,
        shared_contract=shared,
        stage6c_run=stage6c,
        candidate_registry=registry,
    )

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    def assert_query_only(connection: sqlite3.Connection) -> None:
        assert int(connection.execute("PRAGMA query_only").fetchone()[0]) == 1

    monkeypatch.setattr(
        execution_v2,
        "validate_stage6c_panel",
        lambda connection, **_kwargs: (
            assert_query_only(connection)
            or {"status": "PASS", "panel_sha256": stage6c["panel_sha256"]}
        ),
    )
    monkeypatch.setattr(
        execution_v2,
        "verify_factor_campaign",
        lambda _root, **_kwargs: (campaign, []),
    )
    monkeypatch.setattr(
        execution_v2,
        "_membership_rows",
        lambda connection, **_kwargs: (
            assert_query_only(connection)
            or [
                {"ticker": ticker, "cohort_id": cohort}
                for cohort, tickers in bundle.payload["calibration_scope"][
                    "excluded_tickers_by_cohort"
                ].items()
                for ticker in tickers
            ]
            + [{
                "ticker": "SYNTH",
                "cohort_id": sorted(REQUIRED_COHORTS)[0],
            }]
        ),
    )

    feature_rows = [
        {
            "asof_date": "2024-01-31",
            "ticker": f"SYN{index}",
            "cohort_id": cohort,
            "membership_eligible_flag": 1,
            "investable_flag": 1,
            "component_scores_json": "{}",
            "component_quality_json": "{}",
            "component_raw_values_json": "{}",
            "specialized_scores_json": "{}",
            "row_sha256": execution_v2._sha(
                {"cohort": cohort, "ticker": f"SYN{index}"}
            ),
        }
        for index, cohort in enumerate(sorted(REQUIRED_COHORTS), start=1)
    ]

    def fake_feature_panel(
        connection: sqlite3.Connection,
        _bundle: object,
        **_kwargs: object,
    ) -> tuple[list[dict[str, object]], dict[str, object]]:
        assert_query_only(connection)
        return feature_rows, {
            "synthetic_fast_path": True,
            "row_count": len(feature_rows),
        }

    monkeypatch.setattr(
        execution_v2,
        "build_historical_core_panel_v2",
        fake_feature_panel,
    )
    monkeypatch.setattr(
        execution_v2,
        "_exact_stage6c_labels",
        lambda connection, **_kwargs: (
            assert_query_only(connection)
            or {
                (str(row["asof_date"]), str(row["ticker"])): {
                    "asof_date": row["asof_date"],
                    "ticker": row["ticker"],
                    "cohort_id": row["cohort_id"],
                    "forward_xlp_residual_return_21d": 0.01,
                }
                for row in feature_rows
            }
        ),
    )

    def fake_prices(
        connection: sqlite3.Connection,
        *,
        tickers: object,
        maximum_date: str,
    ) -> tuple[object, object, object, str, int, dict[str, object]]:
        assert_query_only(connection)
        assert maximum_date == preregistration["asof_date"]
        names = set(tickers) | {"XLP", "SPY"}
        sessions = ("2024-01-30", "2024-01-31", "2024-02-01")
        prices = {
            ticker: {session: 100.0 for session in sessions}
            for ticker in names
        }
        contracts = {
            ticker: {
                "ticker": ticker,
                "normalized_mark_count": len(sessions),
                "synthetic_fast_path": True,
            }
            for ticker in names
        }
        terminal_validation = {
            "status": "PASS",
            "counts": {"events": 0},
            "synthetic_fast_path": True,
        }
        return prices, {}, contracts, "d" * 64, len(names) * len(sessions), terminal_validation

    monkeypatch.setattr(execution_v2, "_load_price_history", fake_prices)

    evaluated_cells: list[tuple[str, int]] = []

    def fake_evaluate_horizon(**kwargs: object) -> tuple[object, object, object]:
        cohort = str(kwargs["cohort"])
        horizon = int(kwargs["horizon"])
        candidates = list(kwargs["candidates"])
        assert candidates
        assert all(
            row["cohort"] == cohort and row["horizon_sessions"] == horizon
            for row in candidates
        )
        evaluated_cells.append((cohort, horizon))
        return _passing_sequence1_cell(cohort, horizon)

    monkeypatch.setattr(execution_v2, "_evaluate_horizon", fake_evaluate_horizon)

    def fake_benchmark_paths(**_kwargs: object) -> tuple[dict, dict]:
        attestation = {
            "schema_version": "consumer_defensive_matched_benchmark_attestation_v3",
            "model_family": "consumer_defensive",
            "primary_benchmark": "point_in_time_equal_weight_cohort",
            "diagnostic_benchmarks": ["XLP", "SPY"],
            "membership_sha256": "e" * 64,
            "cohorts": {
                cohort: {str(horizon): [] for horizon in REQUIRED_HORIZONS}
                for cohort in REQUIRED_COHORTS
            },
        }
        attestation["payload_sha256"] = canonical_sha256(attestation)
        return {}, attestation

    monkeypatch.setattr(
        execution_v2, "build_matched_benchmark_paths", fake_benchmark_paths
    )
    output_dir = tmp_path / "sequence1"
    payload = execution_v2.run_sequence1_calibration(
        conn,
        bundle,
        repository_root=ROOT,
        framework=framework,
        preregistration=preregistration,
        candidate_registry=registry,
        factor_root=tmp_path / "factor_campaign",
        output_dir=output_dir,
    )

    expected_cells = {
        (cohort, horizon)
        for cohort in REQUIRED_COHORTS
        for horizon in REQUIRED_HORIZONS
    }
    assert len(evaluated_cells) == 12
    assert set(evaluated_cells) == expected_cells
    assert set(payload) == {
        "input_manifest",
        "fold_registry",
        "path_attestation",
        "benchmark_attestation",
        "results",
        "decision",
        "independent_validation",
    }
    for artifact in payload.values():
        assert artifact["payload_sha256"] == canonical_sha256(artifact)

    manifest = payload["input_manifest"]
    folds = payload["fold_registry"]
    path = payload["path_attestation"]
    benchmark = payload["benchmark_attestation"]
    results = payload["results"]
    decision = payload["decision"]
    validation = payload["independent_validation"]
    assert folds["realized_path_attestation_sha256"] == path["payload_sha256"]
    assert results["input_manifest_sha256"] == manifest["payload_sha256"]
    assert results["fold_registry_sha256"] == folds["payload_sha256"]
    assert results["realized_path_attestation_sha256"] == path["payload_sha256"]
    assert results["matched_benchmark_attestation_sha256"] == benchmark["payload_sha256"]
    assert results["decision_payload_sha256"] == decision["payload_sha256"]
    assert decision["input_panel_sha256"] == manifest["payload_sha256"]
    assert decision["fold_registry_sha256"] == folds["payload_sha256"]
    assert decision["candidate_registry_sha256"] == registry["payload_sha256"]
    assert decision["code_sha256"] == preregistration["code_sha256"]
    assert validation["decision_payload_sha256"] == decision["payload_sha256"]
    assert validation["input_manifest_sha256"] == manifest["payload_sha256"]
    assert validation["fold_registry_sha256"] == folds["payload_sha256"]
    assert validation["realized_path_attestation_sha256"] == path["payload_sha256"]
    assert validation["matched_benchmark_attestation_sha256"] == benchmark["payload_sha256"]
    assert validation["candidate_registry_sha256"] == registry["payload_sha256"]
    assert validation["code_sha256"] == preregistration["code_sha256"]
    assert all(
        item["state"] == "active_pilot"
        for item in decision["cohorts"].values()
    )
    assert results["production_promotion_enabled"] is False
    assert results["portfolio_write_enabled"] is False
    assert validation["production_write_performed"] is False
    assert validation["portfolio_write_performed"] is False

    published = {
        "input_manifest": "consumer_defensive_calibration_input_manifest_v2.json",
        "fold_registry": "consumer_defensive_calibration_fold_registry_v2.json",
        "path_attestation": (
            "consumer_defensive_calibration_realized_path_attestation_v2.json"
        ),
        "benchmark_attestation": (
            "consumer_defensive_matched_benchmark_attestation_v3.json"
        ),
        "results": "consumer_defensive_calibration_results_v2.json",
        "decision": "consumer_defensive_calibration_decision_v2.json",
        "independent_validation": (
            "consumer_defensive_calibration_independent_validation_v2.json"
        ),
    }
    for key, filename in published.items():
        with (output_dir / filename).open(encoding="utf-8") as handle:
            assert json.load(handle) == payload[key]

    assert int(conn.execute("PRAGMA query_only").fetchone()[0]) == 1
    assert (
        int(
            conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
            ).fetchone()[0]
        )
        == 0
    )
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("CREATE TABLE forbidden_write(value INTEGER)")
