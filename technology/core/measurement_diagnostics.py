from __future__ import annotations

import csv
import json
import math
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from technology.core.scoring_features import (
    percentile_scores,
    safe_float,
    weighted_available_score,
)
from technology.core.text_norm import normalize_ticker


SubfeatureSpec = tuple[
    str,
    str,
    bool,
    Callable[[float], bool] | None,
]


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = ?
        """,
        (table_name,),
    ).fetchone()
    return row is not None


def load_pit_measurement_features(
    conn: sqlite3.Connection,
    *,
    model_family: str,
    metric_version: str,
    metric_names: set[str],
    start_date: date,
    end_date: date,
) -> tuple[
    dict[tuple[str, str], dict[str, float]],
    dict[str, date],
    list[dict[str, Any]],
]:
    """Load only features whose source was available by their PIT as-of date."""
    if (
        not metric_version
        or not metric_names
        or not _table_exists(conn, "feature_technology_specialized_metric")
    ):
        return {}, {}, []
    placeholders = ",".join("?" for _ in metric_names)
    params: tuple[Any, ...] = (
        model_family,
        metric_version,
        start_date.isoformat(),
        end_date.isoformat(),
        *sorted(metric_names),
    )
    rows = conn.execute(
        f"""
        SELECT ticker, asof_date, metric_name, value,
               source_availability_datetime
        FROM feature_technology_specialized_metric
        WHERE model_family = ?
          AND metric_version = ?
          AND asof_date BETWEEN ? AND ?
          AND metric_name IN ({placeholders})
          AND availability_status = 'AVAILABLE_PIT'
          AND review_required_flag = 0
          AND value IS NOT NULL
        ORDER BY asof_date, ticker, metric_name
        """,
        params,
    ).fetchall()
    by_key: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
    birthdates: dict[str, date] = {}
    for row in rows:
        asof_text = str(row["asof_date"])[:10]
        available_text = str(row["source_availability_datetime"] or "")[:10]
        if not available_text or available_text > asof_text:
            raise RuntimeError(
                "Specialized metric violates PIT availability: "
                f"{row['ticker']} {row['metric_name']} "
                f"asof={asof_text} available={available_text or '<missing>'}"
            )
        metric_name = str(row["metric_name"])
        value = safe_float(row["value"])
        if value is None:
            continue
        ticker = normalize_ticker(row["ticker"])
        by_key[(ticker, asof_text)][metric_name] = value
        available_date = date.fromisoformat(available_text)
        current_birthdate = birthdates.get(metric_name)
        if current_birthdate is None or available_date < current_birthdate:
            birthdates[metric_name] = available_date
    birthdate_rows = [
        {
            "signal": metric_name,
            "birthdate": birthdate.isoformat(),
            "source_scope": "software_specialized_metric",
            "gating_rule": (
                "signal is NULL before first acceptance-datetime-visible "
                "observation; facts are also PIT-gated per issuer"
            ),
        }
        for metric_name, birthdate in sorted(birthdates.items())
    ]
    return dict(by_key), birthdates, birthdate_rows


def _rankdata(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    cursor = 0
    while cursor < len(order):
        end = cursor
        while (
            end + 1 < len(order)
            and values[order[end + 1]] == values[order[cursor]]
        ):
            end += 1
        rank = (cursor + end) / 2.0 + 1.0
        for index in range(cursor, end + 1):
            ranks[order[index]] = rank
        cursor = end + 1
    return ranks


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    covariance = sum(
        (left - mean_x) * (right - mean_y)
        for left, right in zip(xs, ys)
    )
    variance_x = math.sqrt(sum((value - mean_x) ** 2 for value in xs))
    variance_y = math.sqrt(sum((value - mean_y) ** 2 for value in ys))
    if variance_x <= 0 or variance_y <= 0:
        return None
    return covariance / (variance_x * variance_y)


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    return _pearson(_rankdata(xs), _rankdata(ys))


def _partial_spearman(
    values: list[float],
    returns: list[float],
    controls: list[float],
) -> float | None:
    if not (len(values) == len(returns) == len(controls)) or len(values) < 5:
        return None
    ranked_values = _rankdata(values)
    ranked_returns = _rankdata(returns)
    ranked_controls = _rankdata(controls)
    value_return = _pearson(ranked_values, ranked_returns)
    value_control = _pearson(ranked_values, ranked_controls)
    return_control = _pearson(ranked_returns, ranked_controls)
    if (
        value_return is None
        or value_control is None
        or return_control is None
    ):
        return None
    denominator = math.sqrt(
        max(0.0, 1.0 - value_control**2)
        * max(0.0, 1.0 - return_control**2)
    )
    if denominator <= 1e-12:
        return None
    return (
        value_return - value_control * return_control
    ) / denominator


def _newey_west_t(values: list[float], lags: int) -> float | None:
    if len(values) < 3:
        return None
    mean_value = sum(values) / len(values)
    centered = [value - mean_value for value in values]
    gamma_zero = sum(value * value for value in centered) / len(values)
    long_run_variance = gamma_zero
    max_lag = min(max(0, lags), len(values) - 1)
    for lag in range(1, max_lag + 1):
        covariance = sum(
            centered[index] * centered[index - lag]
            for index in range(lag, len(values))
        ) / len(values)
        long_run_variance += (
            2.0 * (1.0 - lag / (max_lag + 1.0)) * covariance
        )
    if long_run_variance <= 0:
        return None
    error = math.sqrt(long_run_variance / len(values))
    return mean_value / error if error > 0 else None


def _nw_lags(horizon: int, step: int) -> int:
    return max(0, math.ceil(horizon / max(1, step)) - 1)


def _quantile_profile(
    values: list[float],
    returns: list[float],
) -> list[float] | None:
    if len(values) != len(returns) or len(values) < 10:
        return None
    order = sorted(range(len(values)), key=lambda index: values[index])
    buckets: list[list[float]] = [[] for _ in range(5)]
    for rank, index in enumerate(order):
        bucket = min(4, rank * 5 // len(order))
        buckets[bucket].append(returns[index])
    if any(not bucket for bucket in buckets):
        return None
    return [sum(bucket) / len(bucket) for bucket in buckets]


def _summary(
    *,
    signal: str,
    horizon: int,
    values: list[float],
    coverage: list[int],
    step: int,
    min_t_stat: float,
) -> dict[str, Any]:
    mean_value = sum(values) / len(values) if values else None
    t_stat = _newey_west_t(values, _nw_lags(horizon, step))
    return {
        "signal": signal,
        "horizon_days": horizon,
        "n_dates": len(values),
        "avg_coverage": (
            round(sum(coverage) / len(coverage), 1) if coverage else 0
        ),
        "mean_ic": round(mean_value, 4) if mean_value is not None else "",
        "newey_west_t_stat": round(t_stat, 2) if t_stat is not None else "",
        "newey_west_lags": _nw_lags(horizon, step),
        "positive_hit_rate": (
            round(sum(value > 0 for value in values) / len(values), 3)
            if values
            else ""
        ),
        "keep_candidate": int(
            mean_value is not None
            and mean_value > 0
            and t_stat is not None
            and t_stat >= min_t_stat
        ),
        "measurement_only_flag": 1,
        "production_weight": 0.0,
    }


def _valid(
    value: float | None,
    predicate: Callable[[float], bool] | None,
) -> bool:
    return value is not None and (predicate is None or predicate(value))


def _baseline_scores(
    rows: list[dict[str, Any]],
    *,
    all_specs: Sequence[SubfeatureSpec],
    component_specs: dict[str, list[tuple[str, float]]],
    component_weights: dict[str, float],
) -> dict[str, float]:
    working = [dict(row) for row in rows]
    for raw_key, score_key, higher_is_better, predicate in all_specs:
        scores = percentile_scores(
            working,
            raw_key,
            higher_is_better=higher_is_better,
            valid=predicate,
        )
        for row in working:
            row[score_key] = scores.get(str(row["ticker"]))
    output: dict[str, float] = {}
    for row in working:
        weighted_sum = 0.0
        available_weight = 0.0
        for component, specs in component_specs.items():
            score, quality, _available, _missing, _detail = (
                weighted_available_score(
                    row,
                    specs,
                    neutral_score=50.0,
                )
            )
            weight = max(0.0, component_weights.get(component, 0.0))
            if quality > 0 and weight > 0:
                weighted_sum += score * weight
                available_weight += weight
        if available_weight > 0:
            output[str(row["ticker"])] = weighted_sum / available_weight
    return output


def _write_csv(
    path: Path,
    rows: list[dict[str, Any]],
    fieldnames: list[str],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fieldnames,
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def write_measurement_diagnostics(
    *,
    output_dir: Path,
    panel_rows: list[dict[str, Any]],
    measurement_specs: Sequence[SubfeatureSpec],
    all_specs: Sequence[SubfeatureSpec],
    component_specs: dict[str, list[tuple[str, float]]],
    component_weights: dict[str, float],
    horizons: list[int],
    step: int,
    min_cross_section: int,
    min_t_stat: float,
    metric_version: str,
) -> dict[str, Any]:
    measurement_names = {spec[0] for spec in measurement_specs}
    specs_by_name = {spec[0]: spec for spec in measurement_specs}
    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in panel_rows:
        by_date[str(row["asof_date"])].append(row)

    regime_values: dict[tuple[str, int, str], list[float]] = defaultdict(
        list
    )
    regime_coverage: dict[tuple[str, int, str], list[int]] = defaultdict(
        list
    )
    incremental_values: dict[tuple[str, int], list[float]] = defaultdict(
        list
    )
    incremental_coverage: dict[tuple[str, int], list[int]] = defaultdict(
        list
    )
    profiles: dict[tuple[str, int], list[list[float]]] = defaultdict(list)
    profile_coverage: dict[tuple[str, int], list[int]] = defaultdict(list)
    signal_date_coverage: dict[str, list[tuple[str, int]]] = defaultdict(list)

    for asof, rows in sorted(by_date.items()):
        baseline = _baseline_scores(
            rows,
            all_specs=all_specs,
            component_specs=component_specs,
            component_weights=component_weights,
        )
        benchmark_trend = safe_float(rows[0].get("benchmark_trailing_252d"))
        regime = (
            "benchmark_up"
            if benchmark_trend is not None and benchmark_trend >= 0
            else "benchmark_down"
            if benchmark_trend is not None
            else "unknown"
        )
        for signal in sorted(measurement_names):
            _raw, _score, higher_is_better, predicate = specs_by_name[signal]
            date_coverage = sum(
                _valid(safe_float(row.get(signal)), predicate)
                for row in rows
            )
            signal_date_coverage[signal].append((asof, date_coverage))
            for horizon in horizons:
                values: list[float] = []
                returns: list[float] = []
                controls: list[float] = []
                for row in rows:
                    value = safe_float(row.get(signal))
                    forward = safe_float(row.get(f"fwd_resid_{horizon}d"))
                    control = baseline.get(str(row["ticker"]))
                    if value is None or forward is None or control is None:
                        continue
                    if predicate is not None and not predicate(value):
                        continue
                    values.append(value if higher_is_better else -value)
                    returns.append(forward)
                    controls.append(control)
                if len(values) < min_cross_section:
                    continue
                direct_ic = _spearman(values, returns)
                if direct_ic is not None:
                    key = (signal, horizon, regime)
                    regime_values[key].append(direct_ic)
                    regime_coverage[key].append(len(values))
                incremental_ic = _partial_spearman(
                    values,
                    returns,
                    controls,
                )
                if incremental_ic is not None:
                    key2 = (signal, horizon)
                    incremental_values[key2].append(incremental_ic)
                    incremental_coverage[key2].append(len(values))
                profile = _quantile_profile(values, returns)
                if profile is not None:
                    profiles[(signal, horizon)].append(profile)
                    profile_coverage[(signal, horizon)].append(len(values))

    regime_rows: list[dict[str, Any]] = []
    for (signal, horizon, regime), values in sorted(regime_values.items()):
        row = _summary(
            signal=signal,
            horizon=horizon,
            values=values,
            coverage=regime_coverage[(signal, horizon, regime)],
            step=step,
            min_t_stat=min_t_stat,
        )
        row["regime"] = regime
        regime_rows.append(row)

    incremental_rows = [
        {
            **_summary(
                signal=signal,
                horizon=horizon,
                values=values,
                coverage=incremental_coverage[(signal, horizon)],
                step=step,
                min_t_stat=min_t_stat,
            ),
            "control": "existing_production_component_composite",
            "method": "partial_spearman_rank_ic",
        }
        for (signal, horizon), values in sorted(incremental_values.items())
    ]

    quantile_rows: list[dict[str, Any]] = []
    for (signal, horizon), signal_profiles in sorted(profiles.items()):
        averages = [
            sum(profile[index] for profile in signal_profiles)
            / len(signal_profiles)
            for index in range(5)
        ]
        monotonicities = [
            value
            for profile in signal_profiles
            if (
                value := _spearman(
                    [1.0, 2.0, 3.0, 4.0, 5.0],
                    profile,
                )
            )
            is not None
        ]
        quantile_rows.append(
            {
                "signal": signal,
                "horizon_days": horizon,
                "n_dates": len(signal_profiles),
                "avg_coverage": round(
                    sum(profile_coverage[(signal, horizon)])
                    / len(profile_coverage[(signal, horizon)]),
                    1,
                ),
                **{
                    f"q{index + 1}_mean_fwd_resid": round(value, 6)
                    for index, value in enumerate(averages)
                },
                "q5_minus_q1": round(averages[4] - averages[0], 6),
                "mean_quantile_monotonicity": (
                    round(sum(monotonicities) / len(monotonicities), 4)
                    if monotonicities
                    else ""
                ),
                "positive_monotonicity_hit_rate": (
                    round(
                        sum(value > 0 for value in monotonicities)
                        / len(monotonicities),
                        3,
                    )
                    if monotonicities
                    else ""
                ),
                "measurement_only_flag": 1,
                "production_weight": 0.0,
            }
        )

    decay_values: dict[str, list[float]] = defaultdict(list)
    decay_coverage: dict[str, list[int]] = defaultdict(list)
    ordered_dates = sorted(by_date)
    for prior_date, current_date in zip(ordered_dates, ordered_dates[1:]):
        prior_by_ticker = {
            str(row["ticker"]): row for row in by_date[prior_date]
        }
        current_by_ticker = {
            str(row["ticker"]): row for row in by_date[current_date]
        }
        common = sorted(set(prior_by_ticker) & set(current_by_ticker))
        for signal in sorted(measurement_names):
            _raw, _score, higher_is_better, predicate = specs_by_name[signal]
            prior_values: list[float] = []
            current_values: list[float] = []
            for ticker in common:
                prior = safe_float(prior_by_ticker[ticker].get(signal))
                current = safe_float(current_by_ticker[ticker].get(signal))
                if prior is None or current is None:
                    continue
                if predicate is not None and (
                    not predicate(prior) or not predicate(current)
                ):
                    continue
                direction = 1.0 if higher_is_better else -1.0
                prior_values.append(prior * direction)
                current_values.append(current * direction)
            if len(prior_values) < max(3, min_cross_section):
                continue
            correlation = _spearman(prior_values, current_values)
            if correlation is not None:
                decay_values[signal].append(correlation)
                decay_coverage[signal].append(len(prior_values))

    decay_rows: list[dict[str, Any]] = []
    for signal in sorted(measurement_names):
        values = decay_values.get(signal, [])
        mean_correlation = sum(values) / len(values) if values else None
        half_life = ""
        if (
            mean_correlation is not None
            and 0 < mean_correlation < 1
        ):
            half_life = round(
                math.log(0.5) / math.log(mean_correlation),
                2,
            )
        decay_rows.append(
            {
                "signal": signal,
                "n_transitions": len(values),
                "avg_common_tickers": (
                    round(
                        sum(decay_coverage[signal])
                        / len(decay_coverage[signal]),
                        1,
                    )
                    if values
                    else 0
                ),
                "mean_rank_autocorrelation": (
                    round(mean_correlation, 4)
                    if mean_correlation is not None
                    else ""
                ),
                "estimated_half_life_panel_periods": half_life,
                "panel_step_trading_days": step,
                "measurement_only_flag": 1,
                "production_weight": 0.0,
            }
        )

    coverage_rows = []
    for signal in sorted(measurement_names):
        observations = signal_date_coverage.get(signal, [])
        nonzero = [(asof, count) for asof, count in observations if count]
        coverage_rows.append(
            {
                "signal": signal,
                "panel_date_count": len(observations),
                "populated_panel_date_count": len(nonzero),
                "max_cross_section": max(
                    (count for _asof, count in nonzero),
                    default=0,
                ),
                "first_populated_asof_date": (
                    nonzero[0][0] if nonzero else ""
                ),
                "last_populated_asof_date": (
                    nonzero[-1][0] if nonzero else ""
                ),
                "min_cross_section_required": min_cross_section,
                "ic_testable_flag": int(
                    any(count >= min_cross_section for _asof, count in nonzero)
                ),
                "measurement_only_flag": 1,
                "production_weight": 0.0,
            }
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(
        output_dir / "measurement_signal_coverage.csv",
        coverage_rows,
        [
            "signal",
            "panel_date_count",
            "populated_panel_date_count",
            "max_cross_section",
            "first_populated_asof_date",
            "last_populated_asof_date",
            "min_cross_section_required",
            "ic_testable_flag",
            "measurement_only_flag",
            "production_weight",
        ],
    )
    _write_csv(
        output_dir / "measurement_regime_ic.csv",
        regime_rows,
        [
            "signal",
            "horizon_days",
            "regime",
            "n_dates",
            "avg_coverage",
            "mean_ic",
            "newey_west_t_stat",
            "newey_west_lags",
            "positive_hit_rate",
            "keep_candidate",
            "measurement_only_flag",
            "production_weight",
        ],
    )
    _write_csv(
        output_dir / "measurement_incremental_ic.csv",
        incremental_rows,
        [
            "signal",
            "horizon_days",
            "control",
            "method",
            "n_dates",
            "avg_coverage",
            "mean_ic",
            "newey_west_t_stat",
            "newey_west_lags",
            "positive_hit_rate",
            "keep_candidate",
            "measurement_only_flag",
            "production_weight",
        ],
    )
    _write_csv(
        output_dir / "measurement_quantile_monotonicity.csv",
        quantile_rows,
        [
            "signal",
            "horizon_days",
            "n_dates",
            "avg_coverage",
            "q1_mean_fwd_resid",
            "q2_mean_fwd_resid",
            "q3_mean_fwd_resid",
            "q4_mean_fwd_resid",
            "q5_mean_fwd_resid",
            "q5_minus_q1",
            "mean_quantile_monotonicity",
            "positive_monotonicity_hit_rate",
            "measurement_only_flag",
            "production_weight",
        ],
    )
    _write_csv(
        output_dir / "measurement_rank_decay.csv",
        decay_rows,
        [
            "signal",
            "n_transitions",
            "avg_common_tickers",
            "mean_rank_autocorrelation",
            "estimated_half_life_panel_periods",
            "panel_step_trading_days",
            "measurement_only_flag",
            "production_weight",
        ],
    )
    summary = {
        "manifest_version": "technology_measurement_diagnostics_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        ),
        "metric_version": metric_version,
        "measurement_signals": sorted(measurement_names),
        "production_weight": 0.0,
        "production_scores_modified_flag": 0,
        "predictive_claim_authorized_flag": 0,
        "regime_ic_row_count": len(regime_rows),
        "incremental_ic_row_count": len(incremental_rows),
        "quantile_monotonicity_row_count": len(quantile_rows),
        "rank_decay_row_count": len(decay_rows),
        "ic_testable_signal_count": sum(
            int(row["ic_testable_flag"]) for row in coverage_rows
        ),
        "coverage_status": (
            "TESTABLE"
            if any(int(row["ic_testable_flag"]) for row in coverage_rows)
            else "INSUFFICIENT_CROSS_SECTION"
        ),
    }
    (
        output_dir / "measurement_diagnostics_summary.json"
    ).write_text(
        json.dumps(summary, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return summary


def validate_measurement_diagnostics(
    output_dir: Path,
    *,
    expected_metric_version: str,
) -> list[str]:
    filenames = (
        "measurement_signal_coverage.csv",
        "measurement_regime_ic.csv",
        "measurement_incremental_ic.csv",
        "measurement_quantile_monotonicity.csv",
        "measurement_rank_decay.csv",
        "measurement_diagnostics_summary.json",
    )
    errors = [
        f"Missing measurement diagnostics output: {filename}"
        for filename in filenames
        if not (output_dir / filename).is_file()
    ]
    summary_path = output_dir / "measurement_diagnostics_summary.json"
    if not summary_path.is_file():
        return errors
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [*errors, f"Invalid measurement diagnostics summary: {exc}"]
    if summary.get("metric_version") != expected_metric_version:
        errors.append("Measurement metric version does not match configuration")
    if float(summary.get("production_weight") or 0.0) != 0.0:
        errors.append("Measurement diagnostics must retain zero production weight")
    if int(summary.get("production_scores_modified_flag") or 0) != 0:
        errors.append("Measurement diagnostics claims production score mutation")
    if int(summary.get("predictive_claim_authorized_flag") or 0) != 0:
        errors.append("Measurement diagnostics prematurely authorizes a claim")
    return errors
