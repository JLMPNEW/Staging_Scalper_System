from __future__ import annotations

import copy
from datetime import date, timedelta
from pathlib import Path

import pytest

from consumer_defensive.core.config import load_config
from consumer_defensive.core.stage9_backtest import (
    PortfolioSpec,
    _file_sha256,
    _immutable_csv_gzip,
    _read_csv,
    _return_metrics,
    _source_tieout_rows,
    build_nonoverlap_schedule,
    build_portfolio_weights,
    stage9_config_payload,
    validate_stage9_policy,
)


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / 'consumer_defensive' / 'config.yaml'


def _calendar(start: str, count: int) -> list[str]:
    first = date.fromisoformat(start)
    return [(first + timedelta(days=index)).isoformat() for index in range(count)]


def test_nonoverlap_schedule_is_earliest_start_deterministic() -> None:
    calendar = _calendar('2024-01-01', 12)
    dates = ['2024-01-01', '2024-01-02', '2024-01-04']
    split = {
        'calibration_date_census': [
            {'asof_date': value, 'included_flag': 1} for value in dates
        ],
        'train_dates': dates,
        'first_embargo_dates': [],
        'validation_dates': [],
        'second_embargo_dates': [],
        'holdout_dates': [],
    }
    rows = build_nonoverlap_schedule(
        split, calendar, entry_lag=1, horizon_days=2
    )
    assert [row['selected_nonoverlap_flag'] for row in rows] == [1, 0, 1]
    assert rows[1]['exclusion_reason'] == 'overlaps_previous_selected_21d_window'
    assert len({row['schedule_row_sha256'] for row in rows}) == 3


def test_portfolio_weights_are_deterministic_disjoint_and_gross_normalized() -> None:
    rows = [
        {'ticker': f'T{index}', '_candidate_score': float(100 - index * 10)}
        for index in range(6)
    ]
    long_only = build_portfolio_weights(
        rows,
        PortfolioSpec('long', 'equal_weight', 'long_only'),
        top_quantile=0.33,
        minimum_positions=2,
    )
    assert long_only == {'T0': 0.5, 'T1': 0.5}

    long_short = build_portfolio_weights(
        rows,
        PortfolioSpec('long_short', 'equal_weight', 'dollar_neutral'),
        top_quantile=0.33,
        minimum_positions=2,
    )
    assert long_short == {
        'T0': 0.25,
        'T1': 0.25,
        'T5': -0.25,
        'T4': -0.25,
    }
    assert sum(abs(value) for value in long_short.values()) == pytest.approx(1.0)
    assert sum(long_short.values()) == pytest.approx(0.0)
    score_weight = build_portfolio_weights(
        rows,
        PortfolioSpec('long_short', 'score_weight', 'dollar_neutral'),
        top_quantile=0.33,
        minimum_positions=2,
    )
    assert sum(abs(value) for value in score_weight.values()) == pytest.approx(1.0)
    assert sum(score_weight.values()) == pytest.approx(0.0)
    assert set(score_weight) == set(long_short)


def _period(asof_date: str, value: float) -> dict[str, object]:
    return {
        'asof_date': asof_date,
        'split_role': 'train',
        'net_total_return_observed_cost': value,
        'net_total_return_stress_cost': value - 0.001,
        'net_xlp_relative_return_observed_cost': value - 0.01,
        'net_xlp_relative_return_stress_cost': value - 0.011,
        'trade_notional_turnover': 1.0,
        'transaction_cost': 0.002,
        'observed_borrow_cost': 0.0,
        'missing_borrow_stress_cost': 0.001,
        'borrow_fee_coverage_fraction': 0.5,
        'portfolio_capacity_usd': 200_000_000.0,
        'stress_portfolio_capacity_usd': 100_000_000.0,
        'reference_nav_capacity_pass_flag': 1,
        'stress_reference_nav_capacity_pass_flag': 1,
        'maximum_days_to_liquidate_reference_nav': 2.0,
        'max_cohort_gross_share': 0.5,
        'maximum_single_name_gross_share': 0.25,
        'terminal_return_position_count': 0,
    }


def test_summary_treats_unselected_slots_as_cash() -> None:
    schedule = [
        {'asof_date': '2024-01-31'},
        {'asof_date': '2024-02-29'},
        {'asof_date': '2024-03-31'},
    ]
    metrics = _return_metrics(
        [
            _period('2024-01-31', 0.10),
            _period('2024-03-31', -0.10),
        ],
        schedule,
        return_basis='total_return',
    )
    assert metrics['calendar_slot_count'] == 3
    assert metrics['invested_period_count'] == 2
    assert metrics['cash_slot_count'] == 1
    assert metrics['observed_total_return'] == pytest.approx(-0.01)
    assert metrics['observed_maximum_drawdown'] == pytest.approx(-0.10)


def test_stage9_config_fails_closed_on_mutation() -> None:
    bundle = load_config(CONFIG)
    payload = stage9_config_payload(bundle)
    for key, value in (
        ('portfolio_write_enabled', True),
        ('production_promotion_enabled', True),
        ('oos_score_valid_flag', 1),
    ):
        mutated = copy.deepcopy(payload)
        mutated[key] = value
        with pytest.raises(ValueError, match=f'stage9_backtest.{key}'):
            validate_stage9_policy(mutated)


def test_deterministic_gzip_writer_has_stable_bytes(tmp_path: Path) -> None:
    rows = [{'ticker': 'KO', 'weight': 0.5}, {'ticker': 'PEP', 'weight': 0.5}]
    first = tmp_path / 'first.csv.gz'
    second = tmp_path / 'second.csv.gz'
    _immutable_csv_gzip(first, rows)
    _immutable_csv_gzip(second, rows)
    assert _file_sha256(first) == _file_sha256(second)
    assert _read_csv(first) == [
        {'ticker': 'KO', 'weight': '0.5'},
        {'ticker': 'PEP', 'weight': '0.5'},
    ]
    _immutable_csv_gzip(first, rows)
    with pytest.raises(FileExistsError, match='content changed'):
        _immutable_csv_gzip(first, [{'ticker': 'KO', 'weight': 1.0}])


def test_source_tieout_binds_the_stage8_panel_artifact_hash() -> None:
    kwargs = {
        'stage8_contract': {
            'stage6c_asof_date': '2026-08-14',
            'contract_sha256': 'a' * 64,
        },
        'stage8_manifest': {
            'artifacts': {
                'stage8_historical_core_panel.csv': {'sha256': 'b' * 64}
            }
        },
        'registry': {
            'registry_sha256': 'c' * 64,
            'candidate_count': 1,
        },
        'split': {
            'split_sha256': 'd' * 64,
            'train_dates': ['2026-01-30'],
        },
        'stage6c_run': {
            'panel_sha256': 'e' * 64,
            'metric_policy_sha256': 'f' * 64,
            'asof_date': '2026-08-14',
        },
        'panel_row_count': 10,
        'terminal_row_count': 1,
    }
    rows = _source_tieout_rows(**kwargs)
    assert rows[0]['source_sha256'] == 'b' * 64
    assert all(len(row['source_sha256']) == 64 for row in rows)

    missing = copy.deepcopy(kwargs)
    missing['stage8_manifest'] = {'artifacts': {}}
    with pytest.raises(RuntimeError, match='requires a SHA-256'):
        _source_tieout_rows(**missing)
