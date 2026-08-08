"""Pure statistical kernel for the ``factor_validation_v1`` evidence contract.

This module consolidates the strongest existing repository behavior while remaining intentionally
disconnected from production scoring. It accepts already point-in-time-safe factor observations and
returns deterministic evidence. It never reads files, writes artifacts, changes scores, or makes a
promotion decision.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
from statistics import median
from typing import Any, Literal

import numpy as np
from scipy import stats as scipy_stats


CONTRACT_VERSION = "factor_validation_v1"
CALENDAR_DAYS_PER_TRADING_DAY = 365.25 / 252.0


def _as_date(value: date | datetime | str, *, field_name: str = "as_of_date") -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a date, datetime, or ISO string, got {type(value).__name__}")
    text = value
    if len(text) != 10:
        raise ValueError(f"{field_name} must be exact ISO YYYY-MM-DD, got {value!r}")
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be ISO YYYY-MM-DD, got {value!r}") from exc
    if parsed.isoformat() != text:
        raise ValueError(f"{field_name} must be canonical ISO YYYY-MM-DD, got {value!r}")
    return parsed


def _optional_float(value: Any, *, field_name: str) -> float | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be numeric or missing, got {value!r}") from exc


def _json_safe(value: Any) -> Any:
    if isinstance(value, np.generic):
        return _json_safe(value.item())
    if isinstance(value, np.ndarray):
        if value.ndim == 0:
            return _json_safe(value.item())
        return [_json_safe(item) for item in value.tolist()]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("evidence contains a non-finite floating-point value")
        return value
    if value is None or isinstance(value, (bool, int, str)):
        return value
    raise TypeError(f"evidence contains unsupported JSON value {type(value).__name__}")


@dataclass(frozen=True, init=False)
class FactorObservation:
    """One point-in-time cross-sectional factor/forward-return observation.

    ``forward_return`` should already be excess, sector-residual, or beta-residual according to the
    sector adapter's declared target contract. Missing values are allowed and explicitly counted.
    Duplicate ``(as_of_date, entity_id)`` observations are rejected by :func:`validate_factor`.
    """

    as_of_date: date
    entity_id: str
    factor_value: float | None
    forward_return: float | None
    regime: str | None

    def __init__(
        self,
        as_of_date: date | datetime | str,
        entity_id: str,
        factor_value: float | str | None,
        forward_return: float | str | None,
        regime: str | None = None,
    ) -> None:
        normalized_entity_id = str(entity_id or "").strip().upper()
        if not normalized_entity_id:
            raise ValueError("entity_id must not be blank")
        normalized_regime = str(regime or "").strip() or None
        object.__setattr__(self, "as_of_date", _as_date(as_of_date))
        object.__setattr__(self, "entity_id", normalized_entity_id)
        object.__setattr__(self, "factor_value", _optional_float(factor_value, field_name="factor_value"))
        object.__setattr__(self, "forward_return", _optional_float(forward_return, field_name="forward_return"))
        object.__setattr__(self, "regime", normalized_regime)


@dataclass(frozen=True)
class FactorValidationConfig:
    """Frozen statistical settings for one factor/target/horizon validation cell."""

    horizon_trading_days: int
    entry_lag_trading_days: int = 1
    min_cross_section: int = 8
    min_dates: int = 12
    min_independent_windows: int = 3
    min_regime_dates: int = 3
    quantile_count: int = 5
    min_extreme_bucket_size: int = 2
    round_trip_cost: float = 0.0
    hac_max_lag: int | None = None
    primary_inference: Literal["independent_window"] = "independent_window"
    target_name: str = "excess_or_residual_forward_return"
    holiday_dates: tuple[date, ...] = ()

    def __post_init__(self) -> None:
        if self.horizon_trading_days <= 0:
            raise ValueError("horizon_trading_days must be positive")
        if self.entry_lag_trading_days < 0:
            raise ValueError("entry_lag_trading_days must be non-negative")
        if self.min_cross_section < 3:
            raise ValueError("min_cross_section must be at least 3")
        if self.min_dates < 3:
            raise ValueError("min_dates must be at least 3")
        if self.min_independent_windows < 2:
            raise ValueError("min_independent_windows must be at least 2")
        if self.min_regime_dates < 1:
            raise ValueError("min_regime_dates must be positive")
        if self.quantile_count < 2:
            raise ValueError("quantile_count must be at least 2")
        if self.min_extreme_bucket_size < 1:
            raise ValueError("min_extreme_bucket_size must be at least 1")
        object.__setattr__(
            self,
            "holiday_dates",
            tuple(sorted({_as_date(value, field_name="holiday_dates") for value in self.holiday_dates})),
        )
        if not math.isfinite(self.round_trip_cost) or self.round_trip_cost < 0.0:
            raise ValueError("round_trip_cost must be finite and non-negative")
        if self.hac_max_lag is not None and self.hac_max_lag < 0:
            raise ValueError("hac_max_lag must be non-negative when supplied")
        if self.primary_inference != "independent_window":
            raise ValueError("HAC is diagnostic-only; primary_inference must be 'independent_window'")
        if not str(self.target_name).strip():
            raise ValueError("target_name must not be blank")


@dataclass(frozen=True)
class QuantileDiagnostics:
    eligible: bool
    failure_reason: str | None
    gross_top_minus_bottom: float | None
    net_top_minus_bottom: float | None
    two_leg_turnover: float | None
    monotonicity: float | None
    bucket_means: tuple[float | None, ...]
    bucket_counts: tuple[int, ...]


@dataclass(frozen=True)
class PerDateDiagnostic:
    as_of_date: date
    regime: str | None
    observation_count: int
    spearman_ic: float | None
    gross_top_minus_bottom: float | None
    net_top_minus_bottom: float | None
    quantile_eligible: bool
    quantile_failure_reason: str | None
    quantile_monotonicity: float | None
    quantile_bucket_counts: tuple[int, ...]
    top_bucket_turnover: float | None
    two_leg_turnover: float | None


@dataclass(frozen=True)
class EvaluationCadence:
    gap_count: int
    minimum_step_trading_days: int
    median_step_trading_days: float
    maximum_step_trading_days: int
    gap_distribution: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class HACInference:
    observation_count: int
    evaluation_step_trading_days: int
    requested_max_lag: int
    max_lag: int
    lag_truncated: bool
    minimum_recommended_observations: int
    small_sample_adequate: bool
    mean: float | None
    standard_error: float | None
    t_stat: float | None
    two_sided_p_value: float | None


@dataclass(frozen=True)
class IndependentWindowInference:
    observation_count: int
    independent_window_count: int
    mean: float | None
    standard_error: float | None
    t_stat: float | None
    two_sided_p_value: float | None


@dataclass(frozen=True)
class RegimeDiagnostic:
    regime: str
    date_count: int
    mean_ic: float


@dataclass(frozen=True)
class FactorValidationResult:
    contract_version: str
    factor_id: str
    target_name: str
    horizon_trading_days: int
    entry_lag_trading_days: int
    total_observation_count: int
    valid_pair_count: int
    eligible_cross_section_count: int
    ic_date_count: int
    dropped_cross_section_count: int
    exclusion_counts: tuple[tuple[str, int], ...]
    mean_ic: float | None
    hit_rate: float | None
    half1_mean_ic: float | None
    half2_mean_ic: float | None
    chronological_half_sign_stable: bool | None
    regime_diagnostics: tuple[RegimeDiagnostic, ...]
    regime_sign_stable: bool | None
    mean_gross_top_minus_bottom: float | None
    mean_gross_top_minus_bottom_matched: float | None
    mean_net_top_minus_bottom: float | None
    mean_quantile_monotonicity: float | None
    mean_rank_persistence: float | None
    mean_top_bucket_turnover: float | None
    mean_two_leg_turnover: float | None
    evaluation_cadence: EvaluationCadence
    hac: HACInference
    independent_window: IndependentWindowInference
    primary_inference: str
    primary_p_value: float | None
    evidence_eligible: bool
    insufficiency_reasons: tuple[str, ...]
    per_date: tuple[PerDateDiagnostic, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a deterministic JSON-safe representation for future evidence writers."""

        value = _json_safe(asdict(self))
        if not isinstance(value, dict):  # pragma: no cover - dataclass invariant
            raise TypeError("FactorValidationResult serialization did not produce a mapping")
        return value


@dataclass(frozen=True)
class _CrossSectionState:
    diagnostic: PerDateDiagnostic
    factor_by_entity: tuple[tuple[str, float], ...]
    top_bucket_entities: frozenset[str]
    bottom_bucket_entities: frozenset[str]


def _clean_finite_pairs(xs: Sequence[float], ys: Sequence[float]) -> tuple[np.ndarray, np.ndarray]:
    if len(xs) != len(ys):
        raise ValueError("correlation inputs must have equal length")
    x = np.asarray(xs, dtype=float)
    y = np.asarray(ys, dtype=float)
    mask = np.isfinite(x) & np.isfinite(y)
    return x[mask], y[mask]


def average_ranks(values: Sequence[float]) -> tuple[float, ...]:
    """Return deterministic one-based average ranks, preserving ties exactly."""

    data = np.asarray(values, dtype=float)
    if data.ndim != 1:
        raise ValueError("rank input must be one-dimensional")
    if not np.isfinite(data).all():
        raise ValueError("rank input must contain only finite values")
    if len(data) == 0:
        return ()
    order = np.argsort(data, kind="mergesort")
    ranks = np.empty(len(data), dtype=float)
    start = 0
    while start < len(order):
        end = start + 1
        while end < len(order) and data[order[end]] == data[order[start]]:
            end += 1
        average_rank = (start + 1 + end) / 2.0
        ranks[order[start:end]] = average_rank
        start = end
    return tuple(float(value) for value in ranks)


def spearman_rank_correlation(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Tie-aware Spearman correlation using average ranks and pairwise finite observations."""

    x, y = _clean_finite_pairs(xs, ys)
    if len(x) < 3:
        return None
    x_rank = np.asarray(average_ranks(x.tolist()), dtype=float)
    y_rank = np.asarray(average_ranks(y.tolist()), dtype=float)
    x_centered = x_rank - float(x_rank.mean())
    y_centered = y_rank - float(y_rank.mean())
    denominator = math.sqrt(float(x_centered @ x_centered) * float(y_centered @ y_centered))
    if denominator <= 0.0:
        return None
    correlation = float(x_centered @ y_centered) / denominator
    return max(-1.0, min(1.0, correlation))


def _bucket_assignments(values: Sequence[float], quantile_count: int) -> tuple[int, ...]:
    if quantile_count < 2:
        raise ValueError("quantile_count must be at least 2")
    data = tuple(float(value) for value in values)
    if not data:
        return ()
    unique = sorted(set(data))
    if len(unique) == 1:
        return tuple(0 for _value in data)
    group_index = {value: index for index, value in enumerate(unique)}
    maximum_group = len(unique) - 1
    maximum_bucket = quantile_count - 1

    def _nearest_bucket(index: int) -> int:
        # Exact-rational nearest bucket with midpoint ties broken toward the
        # center bucket, so a factor and its negation produce mirrored bucket
        # assignments (the sole exception is a tie group sitting exactly at the
        # scale midpoint, which has no mirror-symmetric bucket and is placed
        # deterministically at the upper-middle bucket).
        numerator = 2 * index * maximum_bucket
        denominator = 2 * maximum_group
        quotient, remainder = divmod(numerator, denominator)
        if 2 * remainder < denominator:
            return quotient
        if 2 * remainder > denominator:
            return quotient + 1
        return quotient if 2 * quotient >= maximum_bucket else quotient + 1

    return tuple(_nearest_bucket(group_index[value]) for value in data)


def _finite_mean(values: Sequence[float], *, context: str) -> float:
    if not values:
        raise ValueError(f"{context} requires at least one value")
    if not all(math.isfinite(value) for value in values):
        raise ValueError(f"{context} received a non-finite value")
    scale = max(abs(value) for value in values)
    if scale == 0.0:
        return 0.0
    result = scale * math.fsum((value / scale) / len(values) for value in values)
    if not math.isfinite(result):
        raise ValueError(f"{context} produced a non-finite mean")
    return result


def quantile_diagnostics(
    factor_values: Sequence[float],
    forward_returns: Sequence[float],
    *,
    quantile_count: int = 5,
    round_trip_cost: float = 0.0,
    two_leg_turnover: float | None = None,
    min_extreme_bucket_size: int = 2,
) -> QuantileDiagnostics:
    """Compute tie-safe quantile diagnostics, charging cost only when turnover is measured."""

    if not math.isfinite(round_trip_cost) or round_trip_cost < 0.0:
        raise ValueError("round_trip_cost must be finite and non-negative")
    if min_extreme_bucket_size < 1:
        raise ValueError("min_extreme_bucket_size must be at least 1")
    if two_leg_turnover is not None and (
        not math.isfinite(two_leg_turnover) or not 0.0 <= two_leg_turnover <= 2.0
    ):
        raise ValueError("two_leg_turnover must be finite and in [0, 2]")
    factor, returns = _clean_finite_pairs(factor_values, forward_returns)
    insufficient = QuantileDiagnostics(
        eligible=False,
        failure_reason="insufficient_quantile_observations",
        gross_top_minus_bottom=None,
        net_top_minus_bottom=None,
        two_leg_turnover=two_leg_turnover,
        monotonicity=None,
        bucket_means=tuple(None for _ in range(quantile_count)),
        bucket_counts=tuple(0 for _ in range(quantile_count)),
    )
    if len(factor) < max(quantile_count * 2, 6):
        return insufficient
    if len(set(factor.tolist())) < 2:
        return replace(insufficient, failure_reason="constant_factor")
    assignments = _bucket_assignments(factor.tolist(), quantile_count)
    means: list[float | None] = []
    counts: list[int] = []
    for bucket in range(quantile_count):
        values = returns[np.asarray(assignments) == bucket]
        counts.append(int(len(values)))
        means.append(_finite_mean(values.tolist(), context=f"quantile_{bucket}_mean") if len(values) else None)
    if means[0] is None or means[-1] is None:
        return QuantileDiagnostics(
            eligible=False,
            failure_reason="empty_extreme_bucket",
            gross_top_minus_bottom=None,
            net_top_minus_bottom=None,
            two_leg_turnover=two_leg_turnover,
            monotonicity=None,
            bucket_means=tuple(means),
            bucket_counts=tuple(counts),
        )
    if counts[0] < min_extreme_bucket_size or counts[-1] < min_extreme_bucket_size:
        return QuantileDiagnostics(
            eligible=False,
            failure_reason="sparse_extreme_bucket",
            gross_top_minus_bottom=None,
            net_top_minus_bottom=None,
            two_leg_turnover=two_leg_turnover,
            monotonicity=None,
            bucket_means=tuple(means),
            bucket_counts=tuple(counts),
        )
    gross = means[-1] - means[0]
    if not math.isfinite(gross):
        raise ValueError("quantile top-minus-bottom spread is non-finite")
    nonempty = [(index, value) for index, value in enumerate(means) if value is not None]
    monotonicity = None
    if len(nonempty) == 2:
        difference = float(nonempty[1][1]) - float(nonempty[0][1])
        if not math.isfinite(difference):
            raise ValueError("quantile monotonicity difference is non-finite")
        monotonicity = 0.0 if difference == 0.0 else math.copysign(1.0, difference)
    elif len(nonempty) >= 3:
        monotonicity = spearman_rank_correlation(
            [float(index) for index, _value in nonempty],
            [float(value) for _index, value in nonempty],
        )
    net = None if two_leg_turnover is None else gross - round_trip_cost * two_leg_turnover
    if net is not None and not math.isfinite(net):
        raise ValueError("turnover-adjusted quantile spread is non-finite")
    return QuantileDiagnostics(
        eligible=True,
        failure_reason=None,
        gross_top_minus_bottom=gross,
        net_top_minus_bottom=net,
        two_leg_turnover=two_leg_turnover,
        monotonicity=monotonicity,
        bucket_means=tuple(means),
        bucket_counts=tuple(counts),
    )


def _business_day_gap(left: date, right: date, *, holidays: tuple[date, ...] = ()) -> int:
    if right <= left:
        raise ValueError("evaluation dates must be strictly increasing")
    holiday_list = [item.isoformat() for item in holidays]
    return max(1, int(np.busday_count(left.isoformat(), right.isoformat(), holidays=holiday_list)))


def evaluation_cadence(
    dates: Sequence[date | datetime | str],
    *,
    holidays: tuple[date, ...] = (),
) -> EvaluationCadence:
    """Describe actual evaluation gaps and select the minimum gap for overlap protection.

    ``holidays`` (exchange closures) refine the default Mon-Fri calendar; without them a
    holiday-spanning pair of consecutive trading days is counted as a 2-day gap, which is
    conservative for the HAC lag but can exclude that pair from transition diagnostics.
    """

    unique = sorted({_as_date(value) for value in dates})
    gaps = [
        _business_day_gap(left, right, holidays=holidays)
        for left, right in zip(unique, unique[1:], strict=False)
    ]
    if not gaps:
        return EvaluationCadence(0, 1, 1.0, 1, ())
    distribution = tuple(sorted(Counter(gaps).items()))
    return EvaluationCadence(
        gap_count=len(gaps),
        minimum_step_trading_days=min(gaps),
        median_step_trading_days=float(median(gaps)),
        maximum_step_trading_days=max(gaps),
        gap_distribution=distribution,
    )


def infer_evaluation_step_trading_days(
    dates: Sequence[date | datetime | str],
    *,
    holidays: tuple[date, ...] = (),
) -> int:
    """Return the minimum observed trading-day gap, safe for mixed-cadence overlap."""

    return evaluation_cadence(dates, holidays=holidays).minimum_step_trading_days


def hac_lag_for_overlapping_labels(
    horizon_trading_days: int,
    evaluation_step_trading_days: int,
    *,
    entry_lag_trading_days: int = 1,
) -> int:
    """Return the overlap HAC lag, including signal-to-entry lag in the label window."""

    if horizon_trading_days <= 0:
        raise ValueError("horizon_trading_days must be positive")
    if evaluation_step_trading_days <= 0:
        raise ValueError("evaluation_step_trading_days must be positive")
    if entry_lag_trading_days < 0:
        raise ValueError("entry_lag_trading_days must be non-negative")
    occupied_steps = math.ceil(
        (horizon_trading_days + entry_lag_trading_days) / evaluation_step_trading_days
    )
    return max(0, occupied_steps - 1)


def newey_west_mean_inference(
    values: Sequence[float],
    *,
    max_lag: int,
    evaluation_step_trading_days: int = 1,
) -> HACInference:
    """Diagnostic Newey-West inference with explicit lag and small-sample adequacy."""

    if max_lag < 0:
        raise ValueError("max_lag must be non-negative")
    data = np.asarray(values, dtype=float)
    if data.ndim != 1 or not np.isfinite(data).all():
        raise ValueError("HAC inputs must be a one-dimensional finite series")
    n = len(data)
    requested_lag = int(max_lag)
    actual_lag = min(requested_lag, max(0, n - 2))
    lag_truncated = actual_lag != requested_lag
    minimum_recommended = 5 * (requested_lag + 1)
    adequate = n >= minimum_recommended and not lag_truncated
    mean_value = _finite_mean(data.tolist(), context="HAC mean") if n else None
    if n < 3:
        return HACInference(
            n,
            evaluation_step_trading_days,
            requested_lag,
            actual_lag,
            lag_truncated,
            minimum_recommended,
            adequate,
            mean_value,
            None,
            None,
            None,
        )
    assert mean_value is not None
    centered = data - float(mean_value)
    gamma0 = float(centered @ centered) / n
    long_run_variance = gamma0
    for offset in range(1, actual_lag + 1):
        covariance = float(centered[offset:] @ centered[:-offset]) / n
        long_run_variance += 2.0 * (1.0 - offset / (actual_lag + 1.0)) * covariance
    if not math.isfinite(long_run_variance):
        raise ValueError("HAC long-run variance is non-finite")
    if long_run_variance <= 0.0:
        return HACInference(
            n, evaluation_step_trading_days, requested_lag, actual_lag, lag_truncated,
            minimum_recommended, adequate, mean_value, 0.0, None, None,
        )
    standard_error = math.sqrt(long_run_variance / n)
    if standard_error <= 0.0 or not math.isfinite(standard_error):
        return HACInference(
            n, evaluation_step_trading_days, requested_lag, actual_lag, lag_truncated,
            minimum_recommended, adequate, mean_value, standard_error, None, None,
        )
    t_stat = float(mean_value) / standard_error
    if not math.isfinite(t_stat):
        raise ValueError("HAC t-statistic is non-finite")
    p_value = float(2.0 * scipy_stats.t.sf(abs(t_stat), df=n - 1)) if adequate else None
    return HACInference(
        n, evaluation_step_trading_days, requested_lag, actual_lag, lag_truncated,
        minimum_recommended, adequate, mean_value, standard_error, t_stat, p_value,
    )


def _label_window_calendar_days(horizon_trading_days: int, entry_lag_trading_days: int) -> int:
    occupied_trading_days = horizon_trading_days + entry_lag_trading_days
    return int(math.ceil(occupied_trading_days * CALENDAR_DAYS_PER_TRADING_DAY))


def _independent_window_count(
    dates: Sequence[date],
    *,
    horizon_trading_days: int,
    entry_lag_trading_days: int,
) -> int:
    window_days = _label_window_calendar_days(horizon_trading_days, entry_lag_trading_days)
    selected: list[date] = []
    for candidate in sorted(set(dates)):
        if not selected or (candidate - selected[-1]).days >= window_days:
            selected.append(candidate)
    return len(selected)


def independent_window_mean_inference(
    values: Sequence[float],
    dates: Sequence[date],
    *,
    horizon_trading_days: int,
    entry_lag_trading_days: int,
) -> IndependentWindowInference:
    data = np.asarray(values, dtype=float)
    if data.ndim != 1 or not np.isfinite(data).all():
        raise ValueError("independent-window inputs must be a one-dimensional finite series")
    if len(data) != len(dates):
        raise ValueError("independent-window values and dates must have equal length")
    n = len(data)
    mean_value = _finite_mean(data.tolist(), context="independent-window mean") if n else None
    independent_count = _independent_window_count(
        dates,
        horizon_trading_days=horizon_trading_days,
        entry_lag_trading_days=entry_lag_trading_days,
    )
    if n < 2 or independent_count < 2:
        return IndependentWindowInference(n, independent_count, mean_value, None, None, None)
    assert mean_value is not None
    standard_deviation = float(data.std(ddof=1))
    if not math.isfinite(standard_deviation):
        raise ValueError("independent-window standard deviation is non-finite")
    if standard_deviation <= 0.0:
        return IndependentWindowInference(n, independent_count, mean_value, 0.0, None, None)
    standard_error = standard_deviation / math.sqrt(independent_count)
    t_stat = float(mean_value) / standard_error
    if not math.isfinite(t_stat):
        raise ValueError("independent-window t-statistic is non-finite")
    p_value = float(2.0 * scipy_stats.t.sf(abs(t_stat), df=independent_count - 1))
    return IndependentWindowInference(n, independent_count, mean_value, standard_error, t_stat, p_value)


def _mean_or_none(values: Iterable[float | None]) -> float | None:
    clean = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    return _finite_mean(clean, context="diagnostic mean") if clean else None


def _orientation(value: float | None, *, tolerance: float = 1e-12) -> int:
    if value is None or abs(value) <= tolerance:
        return 0
    return 1 if value > 0.0 else -1


def _sign_stable(reference: float | None, values: Sequence[float | None], *, minimum_values: int) -> bool | None:
    expected = _orientation(reference)
    clean = [value for value in values if value is not None]
    if expected == 0 or len(clean) < minimum_values:
        return None
    return all(_orientation(value) == expected for value in clean)


def _date_regime(rows: Sequence[FactorObservation]) -> str | None:
    regimes = {row.regime for row in rows if row.regime is not None}
    if len(regimes) > 1:
        day = rows[0].as_of_date if rows else "unknown"
        raise ValueError(f"multiple regimes supplied for {day}: {sorted(regimes)}")
    return next(iter(regimes), None)


def _cross_section_state(
    as_of_date: date,
    rows: Sequence[FactorObservation],
    *,
    config: FactorValidationConfig,
) -> _CrossSectionState | None:
    regime = _date_regime(rows)
    valid = [
        row
        for row in rows
        if row.factor_value is not None
        and row.forward_return is not None
        and math.isfinite(row.factor_value)
        and math.isfinite(row.forward_return)
    ]
    if len(valid) < config.min_cross_section:
        return None
    valid.sort(key=lambda row: row.entity_id)
    factor = [float(row.factor_value) for row in valid if row.factor_value is not None]
    returns = [float(row.forward_return) for row in valid if row.forward_return is not None]
    ic = spearman_rank_correlation(factor, returns)
    quantiles = quantile_diagnostics(
        factor,
        returns,
        quantile_count=config.quantile_count,
        round_trip_cost=config.round_trip_cost,
        min_extreme_bucket_size=config.min_extreme_bucket_size,
    )
    assignments = _bucket_assignments(factor, config.quantile_count) if quantiles.eligible else ()
    top = frozenset(
        row.entity_id
        for row, bucket in zip(valid, assignments, strict=True)
        if bucket == config.quantile_count - 1
    ) if quantiles.eligible else frozenset()
    bottom = frozenset(
        row.entity_id
        for row, bucket in zip(valid, assignments, strict=True)
        if bucket == 0
    ) if quantiles.eligible else frozenset()
    return _CrossSectionState(
        diagnostic=PerDateDiagnostic(
            as_of_date=as_of_date,
            regime=regime,
            observation_count=len(valid),
            spearman_ic=ic,
            gross_top_minus_bottom=quantiles.gross_top_minus_bottom,
            net_top_minus_bottom=quantiles.net_top_minus_bottom,
            quantile_eligible=quantiles.eligible,
            quantile_failure_reason=quantiles.failure_reason,
            quantile_monotonicity=quantiles.monotonicity,
            quantile_bucket_counts=quantiles.bucket_counts,
            top_bucket_turnover=None,
            two_leg_turnover=None,
        ),
        factor_by_entity=tuple((row.entity_id, float(row.factor_value)) for row in valid if row.factor_value is not None),
        top_bucket_entities=top,
        bottom_bucket_entities=bottom,
    )


def _jaccard_turnover(previous: frozenset[str], current: frozenset[str]) -> float | None:
    union = previous | current
    return None if not union else 1.0 - len(previous & current) / len(union)


def _transition_diagnostics(
    states: Sequence[_CrossSectionState],
    *,
    typical_step_trading_days: int,
    round_trip_cost: float,
    holidays: tuple[date, ...] = (),
) -> tuple[tuple[_CrossSectionState, ...], float | None, float | None, float | None]:
    persistence: list[float] = []
    top_turnovers: list[float] = []
    two_leg_turnovers: list[float] = []
    if not states:
        return (), None, None, None
    updated = [states[0]]
    # Adjacency tolerance is keyed to the TYPICAL (floored median) cadence,
    # not the minimum: a dense daily cluster inside a monthly panel must not
    # disqualify the monthly transitions. Flooring the median keeps a panel
    # whose gaps are half dropped-date 2s at typical 1, so single dropped
    # daily dates are never spanned; floor(1.5x) then tolerates
    # holiday-shifted weekly/monthly gaps, and declared holidays remove the
    # residual ambiguity.
    maximum_consecutive_gap = max(1, (typical_step_trading_days * 3) // 2)
    for previous, current in zip(states, states[1:], strict=False):
        gap = _business_day_gap(
            previous.diagnostic.as_of_date,
            current.diagnostic.as_of_date,
            holidays=holidays,
        )
        if gap > maximum_consecutive_gap:
            updated.append(current)
            continue
        previous_values = dict(previous.factor_by_entity)
        current_values = dict(current.factor_by_entity)
        common = sorted(set(previous_values) & set(current_values))
        if len(common) >= 3:
            value = spearman_rank_correlation(
                [previous_values[entity] for entity in common],
                [current_values[entity] for entity in common],
            )
            if value is not None:
                persistence.append(value)
        top_turnover = bottom_turnover = None
        if previous.diagnostic.quantile_eligible and current.diagnostic.quantile_eligible:
            top_turnover = _jaccard_turnover(previous.top_bucket_entities, current.top_bucket_entities)
            bottom_turnover = _jaccard_turnover(previous.bottom_bucket_entities, current.bottom_bucket_entities)
        if top_turnover is not None and bottom_turnover is not None:
            two_leg_turnover = top_turnover + bottom_turnover
            gross = current.diagnostic.gross_top_minus_bottom
            net = None if gross is None else gross - round_trip_cost * two_leg_turnover
            if net is not None and not math.isfinite(net):
                raise ValueError("turnover-adjusted quantile spread is non-finite")
            current = replace(
                current,
                diagnostic=replace(
                    current.diagnostic,
                    net_top_minus_bottom=net,
                    top_bucket_turnover=top_turnover,
                    two_leg_turnover=two_leg_turnover,
                ),
            )
            top_turnovers.append(top_turnover)
            two_leg_turnovers.append(two_leg_turnover)
        updated.append(current)
    return (
        tuple(updated),
        _mean_or_none(persistence),
        _mean_or_none(top_turnovers),
        _mean_or_none(two_leg_turnovers),
    )


def validate_factor(
    observations: Iterable[FactorObservation],
    *,
    factor_id: str,
    config: FactorValidationConfig,
) -> FactorValidationResult:
    """Validate one factor/target/horizon cell without making a promotion decision."""

    normalized_factor_id = str(factor_id or "").strip()
    if not normalized_factor_id:
        raise ValueError("factor_id must not be blank")
    supplied_rows = tuple(observations)
    if not supplied_rows:
        raise ValueError("at least one observation is required")
    if not all(isinstance(row, FactorObservation) for row in supplied_rows):
        raise TypeError("observations must contain only FactorObservation instances")
    rows = sorted(supplied_rows, key=lambda row: (row.as_of_date, row.entity_id))

    seen: set[tuple[date, str]] = set()
    duplicate_keys: list[str] = []
    grouped: dict[date, list[FactorObservation]] = defaultdict(list)
    exclusions: Counter[str] = Counter()
    valid_pair_count = 0
    for row in rows:
        key = (row.as_of_date, row.entity_id)
        if key in seen:
            duplicate_keys.append(f"{row.as_of_date.isoformat()}:{row.entity_id}")
        seen.add(key)
        grouped[row.as_of_date].append(row)
        if row.factor_value is None:
            exclusions["missing_factor"] += 1
        elif not math.isfinite(row.factor_value):
            exclusions["non_finite_factor"] += 1
        if row.forward_return is None:
            exclusions["missing_forward_return"] += 1
        elif not math.isfinite(row.forward_return):
            exclusions["non_finite_forward_return"] += 1
        if (
            row.factor_value is not None
            and row.forward_return is not None
            and math.isfinite(row.factor_value)
            and math.isfinite(row.forward_return)
        ):
            valid_pair_count += 1
    if duplicate_keys:
        preview = ", ".join(duplicate_keys[:5])
        raise ValueError(f"duplicate (as_of_date, entity_id) observations: {preview}")

    states: list[_CrossSectionState] = []
    for as_of_date, cross_section in sorted(grouped.items()):
        state = _cross_section_state(as_of_date, cross_section, config=config)
        if state is None:
            exclusions["cross_section_below_minimum"] += 1
            continue
        states.append(state)
        if state.diagnostic.spearman_ic is None:
            exclusions["undefined_cross_section_ic"] += 1

    ic_diagnostics = [state.diagnostic for state in states if state.diagnostic.spearman_ic is not None]
    ic_values = [float(item.spearman_ic) for item in ic_diagnostics if item.spearman_ic is not None]
    ic_dates = [item.as_of_date for item in ic_diagnostics]
    cadence = evaluation_cadence(ic_dates, holidays=config.holiday_dates)
    step = cadence.minimum_step_trading_days
    overlap_lag = hac_lag_for_overlapping_labels(
        config.horizon_trading_days,
        step,
        entry_lag_trading_days=config.entry_lag_trading_days,
    )
    # A user-supplied hac_max_lag may extend but never undercut the
    # overlap-implied lag: a shorter lag would emit a diagnostic HAC p-value
    # with no overlap correction at all.
    configured_lag = (
        max(config.hac_max_lag, overlap_lag) if config.hac_max_lag is not None else overlap_lag
    )
    hac = newey_west_mean_inference(ic_values, max_lag=configured_lag, evaluation_step_trading_days=step)
    independent = independent_window_mean_inference(
        ic_values,
        ic_dates,
        horizon_trading_days=config.horizon_trading_days,
        entry_lag_trading_days=config.entry_lag_trading_days,
    )

    midpoint = len(ic_values) // 2
    half1 = _mean_or_none(ic_values[:midpoint]) if midpoint else None
    half2 = _mean_or_none(ic_values[midpoint:]) if midpoint else None
    mean_ic = _mean_or_none(ic_values)
    half_stable = _sign_stable(mean_ic, [half1, half2], minimum_values=2)

    by_regime: dict[str, list[float]] = defaultdict(list)
    for item in ic_diagnostics:
        if item.regime and item.spearman_ic is not None:
            by_regime[item.regime].append(float(item.spearman_ic))
    regime_diagnostics = tuple(
        RegimeDiagnostic(
            regime=regime,
            date_count=len(values),
            mean_ic=_finite_mean(values, context=f"regime_{regime}_mean_ic"),
        )
        for regime, values in sorted(by_regime.items())
        if len(values) >= config.min_regime_dates
    )
    regime_stable = _sign_stable(
        mean_ic,
        [item.mean_ic for item in regime_diagnostics],
        minimum_values=2,
    )
    states_tuple, persistence, turnover, two_leg_turnover = _transition_diagnostics(
        states,
        typical_step_trading_days=max(1, int(cadence.median_step_trading_days)),
        round_trip_cost=config.round_trip_cost,
        holidays=config.holiday_dates,
    )
    states = list(states_tuple)

    reasons: list[str] = []
    if len(ic_values) < config.min_dates:
        reasons.append("insufficient_ic_dates")
    if independent.independent_window_count < config.min_independent_windows:
        reasons.append("insufficient_independent_windows")
    primary_p = independent.two_sided_p_value
    if primary_p is None:
        reasons.append("independent_window_inference_unavailable")

    return FactorValidationResult(
        contract_version=CONTRACT_VERSION,
        factor_id=normalized_factor_id,
        target_name=config.target_name,
        horizon_trading_days=config.horizon_trading_days,
        entry_lag_trading_days=config.entry_lag_trading_days,
        total_observation_count=len(rows),
        valid_pair_count=valid_pair_count,
        eligible_cross_section_count=len(states),
        ic_date_count=len(ic_values),
        dropped_cross_section_count=len(grouped) - len(states),
        exclusion_counts=tuple(sorted(exclusions.items())),
        mean_ic=mean_ic,
        hit_rate=_mean_or_none([1.0 if value > 0.0 else 0.0 for value in ic_values]),
        half1_mean_ic=half1,
        half2_mean_ic=half2,
        chronological_half_sign_stable=half_stable,
        regime_diagnostics=regime_diagnostics,
        regime_sign_stable=regime_stable,
        mean_gross_top_minus_bottom=_mean_or_none(item.diagnostic.gross_top_minus_bottom for item in states),
        mean_gross_top_minus_bottom_matched=_mean_or_none(
            item.diagnostic.gross_top_minus_bottom
            for item in states
            if item.diagnostic.net_top_minus_bottom is not None
        ),
        mean_net_top_minus_bottom=_mean_or_none(item.diagnostic.net_top_minus_bottom for item in states),
        mean_quantile_monotonicity=_mean_or_none(item.diagnostic.quantile_monotonicity for item in states),
        mean_rank_persistence=persistence,
        mean_top_bucket_turnover=turnover,
        mean_two_leg_turnover=two_leg_turnover,
        evaluation_cadence=cadence,
        hac=hac,
        independent_window=independent,
        primary_inference=config.primary_inference,
        primary_p_value=primary_p,
        evidence_eligible=not reasons,
        insufficiency_reasons=tuple(reasons),
        per_date=tuple(state.diagnostic for state in states),
    )
