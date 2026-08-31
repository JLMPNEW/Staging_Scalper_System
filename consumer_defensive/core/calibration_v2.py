"""Leakage-safe Consumer Defensive calibration primitives.

Forecast skill is evaluated independently at 21/63/126 sessions on immutable
outer-test observations.  Absolute profitability and path risk use a separate,
non-overlapping daily realized-return stream so overlapping forward labels are
never compounded into fictitious wealth.
"""

from __future__ import annotations

import itertools
import math
import random
import statistics
from dataclasses import dataclass
from datetime import date
from statistics import NormalDist
from typing import Any, Mapping, Sequence

from consumer_defensive.core.promotion_framework_v2 import (
    DECISION_SCHEMA,
    DECISION_STATES,
    REQUIRED_COHORTS,
    REQUIRED_HORIZONS,
    REQUIRED_HORIZON_KEYS,
    _expected_transition,
    canonical_sha256,
    framework_sha256,
    performance_gate_failures,
    validate_calibration_decision,
    validate_framework,
    validate_performance,
)


PF_FINITE_CAP = 1_000_000.0
OUTER_TEST_ROLE = "outer_test"


def _identifier(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError(f"{label} must be a nonblank identifier of at most 256 characters")
    return value.strip()


def _exact_date(value: Any, *, label: str) -> date:
    if type(value) is not date:
        raise ValueError(f"{label} must be a datetime.date")
    return value


def _finite(value: Any, *, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be finite numeric data")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be finite numeric data") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{label} must be finite numeric data")
    return parsed


def _positive_integer(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


@dataclass(frozen=True)
class ReturnObservation:
    observation_id: str
    fold_id: str
    evaluation_role: str
    asof_date: date
    label_completion_date: date
    cohort: str
    horizon_sessions: int
    strategy_return: float
    benchmark_return: float
    transaction_cost: float = 0.0
    turnover: float = 0.0
    liquidity_capacity_ratio: float = PF_FINITE_CAP

    def __post_init__(self) -> None:
        object.__setattr__(self, "observation_id", _identifier(self.observation_id, label="observation_id"))
        object.__setattr__(self, "fold_id", _identifier(self.fold_id, label="fold_id"))
        if self.evaluation_role != OUTER_TEST_ROLE:
            raise ValueError("calibration observations must be selection-blind outer_test rows")
        _exact_date(self.asof_date, label="asof_date")
        _exact_date(self.label_completion_date, label="label_completion_date")
        if self.cohort not in REQUIRED_COHORTS:
            raise ValueError(f"unsupported Consumer cohort: {self.cohort}")
        if isinstance(self.horizon_sessions, bool) or self.horizon_sessions not in REQUIRED_HORIZONS:
            raise ValueError("horizon_sessions must be the integer 21, 63, or 126")
        if self.label_completion_date < self.asof_date:
            raise ValueError("label completion cannot predate the signal")
        for name in (
            "strategy_return",
            "benchmark_return",
            "transaction_cost",
            "turnover",
            "liquidity_capacity_ratio",
        ):
            object.__setattr__(self, name, _finite(getattr(self, name), label=name))
        if self.transaction_cost < 0.0 or self.transaction_cost >= 1.0:
            raise ValueError("transaction_cost must be in [0, 1)")
        if self.turnover < 0.0:
            raise ValueError("turnover cannot be negative")
        if not 0.0 < self.liquidity_capacity_ratio <= PF_FINITE_CAP:
            raise ValueError("liquidity_capacity_ratio is outside its supported range")
        if self.benchmark_return <= -1.0 or self.net_strategy_return <= -1.0:
            raise ValueError("strategy and benchmark returns must exceed -100%")

    @property
    def net_strategy_return(self) -> float:
        return self.strategy_return - self.transaction_cost

    @property
    def paired_net_alpha(self) -> float:
        return self.net_strategy_return - self.benchmark_return


@dataclass(frozen=True)
class RealizedReturnObservation:
    observation_id: str
    source_portfolio_observation_id: str
    fold_id: str
    evaluation_role: str
    return_date: date
    cohort: str
    horizon_sessions: int
    strategy_return: float
    transaction_cost: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "observation_id", _identifier(self.observation_id, label="observation_id"))
        object.__setattr__(
            self,
            "source_portfolio_observation_id",
            _identifier(
                self.source_portfolio_observation_id,
                label="source_portfolio_observation_id",
            ),
        )
        object.__setattr__(self, "fold_id", _identifier(self.fold_id, label="fold_id"))
        if self.evaluation_role != OUTER_TEST_ROLE:
            raise ValueError("realized returns must be selection-blind outer_test rows")
        _exact_date(self.return_date, label="return_date")
        if self.cohort not in REQUIRED_COHORTS:
            raise ValueError(f"unsupported Consumer cohort: {self.cohort}")
        if isinstance(self.horizon_sessions, bool) or self.horizon_sessions not in REQUIRED_HORIZONS:
            raise ValueError("horizon_sessions must be the integer 21, 63, or 126")
        object.__setattr__(self, "strategy_return", _finite(self.strategy_return, label="strategy_return"))
        object.__setattr__(self, "transaction_cost", _finite(self.transaction_cost, label="transaction_cost"))
        if not 0.0 <= self.transaction_cost < 1.0:
            raise ValueError("transaction_cost must be in [0, 1)")
        if self.net_strategy_return <= -1.0:
            raise ValueError("net realized return must exceed -100%")

    @property
    def net_strategy_return(self) -> float:
        return self.strategy_return - self.transaction_cost


@dataclass(frozen=True)
class SelectedPortfolioObservation:
    """Exact dated holdings for one selection-blind outer-test portfolio."""

    observation_id: str
    fold_id: str
    asof_date: date
    cohort: str
    horizon_sessions: int
    selected_candidate_id: str
    weights: tuple[tuple[str, float], ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "observation_id", _identifier(self.observation_id, label="observation_id"))
        object.__setattr__(self, "fold_id", _identifier(self.fold_id, label="fold_id"))
        _exact_date(self.asof_date, label="asof_date")
        if self.cohort not in REQUIRED_COHORTS:
            raise ValueError(f"unsupported Consumer cohort: {self.cohort}")
        if isinstance(self.horizon_sessions, bool) or self.horizon_sessions not in REQUIRED_HORIZONS:
            raise ValueError("horizon_sessions must be the integer 21, 63, or 126")
        object.__setattr__(
            self,
            "selected_candidate_id",
            _identifier(self.selected_candidate_id, label="selected_candidate_id"),
        )
        if isinstance(self.weights, (str, bytes)) or not self.weights:
            raise ValueError("selected portfolio requires dated ticker weights")
        normalized: list[tuple[str, float]] = []
        seen: set[str] = set()
        for position, item in enumerate(self.weights):
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                raise ValueError(f"weights[{position}] must be a (ticker, weight) pair")
            ticker = _identifier(item[0], label=f"weights[{position}].ticker").upper()
            weight = _finite(item[1], label=f"weights[{position}].weight")
            if ticker in seen:
                raise ValueError("selected portfolio tickers must be unique")
            if weight <= 0.0:
                raise ValueError("selected portfolio weights must be positive and long-only")
            seen.add(ticker)
            normalized.append((ticker, weight))
        normalized.sort()
        if sum(weight for _, weight in normalized) > 1.0 + 1e-12:
            raise ValueError("selected portfolio gross exposure cannot exceed 1.0")
        object.__setattr__(self, "weights", tuple(normalized))


@dataclass(frozen=True)
class WalkForwardFold:
    fold_id: str
    train_dates: tuple[date, ...]
    validation_dates: tuple[date, ...]
    test_dates: tuple[date, ...]
    purged_train_count: int
    purged_validation_count: int


def build_nested_purged_walk_forward(
    signal_dates: Sequence[date],
    *,
    label_completion_by_date: Mapping[date, date],
    initial_train_size: int,
    validation_size: int,
    test_size: int,
    step_size: int | None = None,
    embargo_observations: int = 0,
) -> tuple[WalkForwardFold, ...]:
    """Build expanding, non-overlapping outer folds with purge and embargo."""

    dates = tuple(signal_dates)
    if any(type(value) is not date for value in dates):
        raise ValueError("signal dates must be datetime.date values")
    if dates != tuple(sorted(set(dates))):
        raise ValueError("signal dates must be strictly increasing and unique")
    if set(label_completion_by_date) != set(dates):
        raise ValueError("label completion mapping must cover the exact signal-date census")
    for signal, completion in label_completion_by_date.items():
        if type(completion) is not date or completion < signal:
            raise ValueError("label completion dates must be dates on or after their signal")
    for name, value in {
        "initial_train_size": initial_train_size,
        "validation_size": validation_size,
        "test_size": test_size,
    }.items():
        _positive_integer(value, label=name)
    if isinstance(embargo_observations, bool) or not isinstance(embargo_observations, int) or embargo_observations < 0:
        raise ValueError("embargo_observations must be a nonnegative integer")
    step = test_size if step_size is None else _positive_integer(step_size, label="step_size")
    if step < test_size:
        raise ValueError("step_size cannot be smaller than test_size; outer tests cannot overlap")

    first_test = initial_train_size + validation_size
    folds: list[WalkForwardFold] = []
    occupied_test_dates: set[date] = set()
    for test_start in range(first_test, len(dates) - test_size + 1, step):
        validation_start = test_start - validation_size
        raw_train = dates[:validation_start]
        raw_validation = dates[validation_start:test_start]
        test = dates[test_start : test_start + test_size]
        if len(test) != test_size:
            continue
        train_stop = max(0, validation_start - embargo_observations)
        validation_stop = max(validation_start, test_start - embargo_observations)
        train = tuple(value for value in dates[:train_stop] if label_completion_by_date[value] < raw_validation[0])
        validation = tuple(
            value for value in dates[validation_start:validation_stop] if label_completion_by_date[value] < test[0]
        )
        if len(train) < initial_train_size or not validation:
            continue
        if occupied_test_dates.intersection(test):
            raise AssertionError("outer-test dates overlap")
        occupied_test_dates.update(test)
        folds.append(
            WalkForwardFold(
                fold_id=f"wf_{len(folds) + 1:03d}",
                train_dates=train,
                validation_dates=validation,
                test_dates=test,
                purged_train_count=len(raw_train) - len(train),
                purged_validation_count=len(raw_validation) - len(validation),
            )
        )
    if not folds:
        raise ValueError("walk-forward settings produced no admissible folds")
    return tuple(folds)


def _finite_series(values: Sequence[float], *, label: str) -> list[float]:
    return [_finite(value, label=label) for value in values]


def profit_factor(returns: Sequence[float]) -> float:
    values = _finite_series(returns, label="profit-factor return")
    gains = sum(value for value in values if value > 0.0)
    losses = -sum(value for value in values if value < 0.0)
    if losses == 0.0:
        return PF_FINITE_CAP if gains > 0.0 else 0.0
    return min(PF_FINITE_CAP, gains / losses)


def _winsorized(values: Sequence[float], *, fraction: float) -> list[float]:
    ordered = sorted(_finite_series(values, label="winsorized return"))
    if not 0.0 <= fraction < 0.5:
        raise ValueError("winsor fraction must be in [0, 0.5)")
    if not ordered:
        return []
    width = min(int(len(ordered) * fraction), max(0, (len(ordered) - 1) // 2))
    if width == 0:
        return ordered
    low, high = ordered[width], ordered[-width - 1]
    return [min(high, max(low, value)) for value in ordered]


def block_bootstrap_lcb(
    values: Sequence[float],
    *,
    confidence: float,
    block_size: int,
    samples: int,
    seed: int,
) -> float:
    series = _finite_series(values, label="bootstrap return")
    if not series:
        raise ValueError("LCB requires at least one paired observation")
    if not 0.5 < confidence < 1.0:
        raise ValueError("bootstrap confidence must be in (0.5, 1)")
    _positive_integer(block_size, label="block_size")
    if isinstance(samples, bool) or not isinstance(samples, int) or samples < 100:
        raise ValueError("bootstrap samples must be an integer >= 100")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("bootstrap seed must be an integer")
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(samples):
        draw: list[float] = []
        while len(draw) < len(series):
            start = rng.randrange(len(series))
            draw.extend(series[(start + offset) % len(series)] for offset in range(block_size))
        means.append(statistics.fmean(draw[: len(series)]))
    means.sort()
    index = max(0, min(len(means) - 1, math.floor((1.0 - confidence) * samples)))
    return means[index]


def maximum_drawdown(returns: Sequence[float]) -> float:
    values = _finite_series(returns, label="drawdown return")
    wealth = peak = 1.0
    worst = 0.0
    for value in values:
        if value <= -1.0:
            raise ValueError("wealth-path returns must exceed -100%")
        wealth *= 1.0 + value
        peak = max(peak, wealth)
        worst = max(worst, (peak - wealth) / peak)
    return worst


def expected_shortfall(returns: Sequence[float], *, tail_probability: float) -> float:
    ordered = sorted(_finite_series(returns, label="expected-shortfall return"))
    if not ordered:
        raise ValueError("expected shortfall requires observations")
    if not 0.0 < tail_probability <= 0.5:
        raise ValueError("tail_probability must be in (0, 0.5]")
    count = max(1, math.ceil(len(ordered) * tail_probability))
    return statistics.fmean(ordered[:count])


def deflated_sharpe_ratio(returns: Sequence[float], *, candidate_sharpe_ratios: Sequence[float]) -> float:
    """Deflated Sharpe ratio using the observed candidate-Sharpe dispersion."""

    values = _finite_series(returns, label="DSR return")
    candidate_sharpes = _finite_series(candidate_sharpe_ratios, label="candidate Sharpe ratio")
    if len(values) < 3 or not candidate_sharpes:
        return 0.0
    mean = statistics.fmean(values)
    standard_deviation = statistics.stdev(values)
    if standard_deviation <= 0.0:
        return 0.0
    sharpe = mean / standard_deviation
    centered = [(value - mean) / standard_deviation for value in values]
    skewness = statistics.fmean(value**3 for value in centered)
    kurtosis = statistics.fmean(value**4 for value in centered)
    variance_numerator = 1.0 - skewness * sharpe + ((kurtosis - 1.0) / 4.0) * sharpe**2
    if variance_numerator <= 0.0:
        return 0.0
    selected_sharpe_variance = variance_numerator / (len(values) - 1.0)
    if len(candidate_sharpes) == 1:
        expected_maximum = 0.0
    else:
        candidate_variance = statistics.variance(candidate_sharpes)
        gamma = 0.5772156649015329
        normal = NormalDist()
        trial_count = len(candidate_sharpes)
        standardized_maximum = (1.0 - gamma) * normal.inv_cdf(1.0 - 1.0 / trial_count) + gamma * normal.inv_cdf(
            1.0 - 1.0 / (trial_count * math.e)
        )
        expected_maximum = math.sqrt(max(0.0, candidate_variance)) * standardized_maximum
    statistic = (sharpe - expected_maximum) / math.sqrt(selected_sharpe_variance)
    return min(1.0, max(0.0, NormalDist().cdf(statistic)))


def _candidate_sharpe_ratios(rows: Sequence[Sequence[float]]) -> list[float]:
    ratios: list[float] = []
    for row in rows:
        values = _finite_series(row, label="candidate fold alpha")
        if len(values) < 2:
            ratios.append(0.0)
            continue
        deviation = statistics.stdev(values)
        ratios.append(0.0 if deviation <= 0.0 else statistics.fmean(values) / deviation)
    return ratios


def _validated_candidate_matrix(
    candidate_performance_by_fold: Mapping[str, Mapping[str, float]] | None,
) -> tuple[list[str], list[str], list[list[float]]]:
    if not candidate_performance_by_fold:
        return [], [], []
    if not isinstance(candidate_performance_by_fold, Mapping):
        raise ValueError("candidate performance must be a mapping")
    candidates = sorted(_identifier(name, label="candidate_id") for name in candidate_performance_by_fold)
    if len(candidates) < 2:
        return candidates, [], []
    first = candidate_performance_by_fold[candidates[0]]
    if not isinstance(first, Mapping):
        raise ValueError("each candidate must map fold identifiers to performance")
    folds = sorted(_identifier(name, label="candidate fold_id") for name in first)
    if len(folds) < 4 or len(folds) % 2:
        raise ValueError("PBO requires an even census of at least four folds")
    rows: list[list[float]] = []
    for candidate in candidates:
        raw = candidate_performance_by_fold[candidate]
        if not isinstance(raw, Mapping) or set(raw) != set(folds):
            raise ValueError("candidate fold matrices must share the exact fold census")
        rows.append([_finite(raw[fold], label=f"candidate {candidate}/{fold}") for fold in folds])
    return candidates, folds, rows


def probability_of_backtest_overfitting(
    candidate_performance_by_fold: Mapping[str, Mapping[str, float]] | None,
    *,
    maximum_combinations: int = 65_536,
) -> float:
    """Compute exhaustive balanced CSCV/PBO or fail if the census is too large."""

    candidates, folds, rows = _validated_candidate_matrix(candidate_performance_by_fold)
    if len(candidates) < 2:
        return 1.0
    _positive_integer(maximum_combinations, label="maximum_combinations")
    half = len(folds) // 2
    combination_count = math.comb(len(folds), half)
    if combination_count > maximum_combinations:
        raise ValueError(f"PBO requires {combination_count} combinations, above frozen maximum {maximum_combinations}")
    overfit = total = 0
    all_indices = set(range(len(folds)))
    for selected in itertools.combinations(range(len(folds)), half):
        selected_set = set(selected)
        out_indices = sorted(all_indices - selected_set)
        in_means = [statistics.fmean(row[index] for index in selected) for row in rows]
        winner = max(range(len(candidates)), key=lambda index: (in_means[index], -index))
        out_means = [statistics.fmean(row[index] for index in out_indices) for row in rows]
        ordered = sorted(range(len(candidates)), key=lambda index: (out_means[index], index))
        rank = ordered.index(winner)
        percentile = (rank + 1.0) / (len(candidates) + 1.0)
        overfit += int(percentile <= 0.5)
        total += 1
    return overfit / total


def concentration_metrics(weights: Sequence[float], *, maximum_gross_exposure: float = 1.0) -> tuple[float, float]:
    if isinstance(weights, (str, bytes)) or not weights:
        raise ValueError("selected portfolio weights must be a nonempty sequence")
    parsed = [_finite(value, label="portfolio weight") for value in weights]
    if any(value < 0.0 for value in parsed):
        raise ValueError("Consumer calibration portfolios must be long-only")
    gross = sum(parsed)
    if gross <= 0.0 or gross > maximum_gross_exposure + 1e-12:
        raise ValueError("selected portfolio gross exposure exceeds the frozen limit")
    normalized = [value / gross for value in parsed]
    return sum(value * value for value in normalized), max(normalized)


def _hash_payload(value: Any) -> str:
    return canonical_sha256({"value": value})


def _validated_outer_test_membership(
    folds: Sequence[WalkForwardFold],
) -> tuple[dict[date, str], list[dict[str, Any]]]:
    """Validate typed fold lineage and return its exact outer-test membership."""

    if not folds:
        raise ValueError("outer-test fold lineage is required")
    if len({fold.fold_id for fold in folds}) != len(folds):
        raise ValueError("outer-test fold identifiers must be unique")
    membership: dict[date, str] = {}
    payload: list[dict[str, Any]] = []
    for fold in folds:
        _identifier(fold.fold_id, label="outer fold_id")
        partitions = {
            "train": tuple(fold.train_dates),
            "validation": tuple(fold.validation_dates),
            "test": tuple(fold.test_dates),
        }
        for name, values in partitions.items():
            if not values or any(type(value) is not date for value in values):
                raise ValueError(f"{fold.fold_id}: {name} dates must be nonempty dates")
            if values != tuple(sorted(set(values))):
                raise ValueError(f"{fold.fold_id}: {name} dates must be increasing and unique")
        if not max(partitions["train"]) < min(partitions["validation"]) < min(partitions["test"]):
            raise ValueError(f"{fold.fold_id}: fold partitions are not chronological")
        if set(partitions["train"]).intersection(partitions["validation"], partitions["test"]):
            raise ValueError(f"{fold.fold_id}: fold partitions overlap")
        if set(partitions["validation"]).intersection(partitions["test"]):
            raise ValueError(f"{fold.fold_id}: validation and test partitions overlap")
        for signal_date in partitions["test"]:
            if signal_date in membership:
                raise ValueError("outer-test dates cannot appear in multiple folds")
            membership[signal_date] = fold.fold_id
        payload.append(
            {
                "fold_id": fold.fold_id,
                "train_dates": [value.isoformat() for value in partitions["train"]],
                "validation_dates": [value.isoformat() for value in partitions["validation"]],
                "test_dates": [value.isoformat() for value in partitions["test"]],
                "purged_train_count": fold.purged_train_count,
                "purged_validation_count": fold.purged_validation_count,
            }
        )
    return membership, payload


def _validated_realized_rows(
    rows: Sequence[RealizedReturnObservation],
    *,
    cohort: str,
    horizon: int,
    decision_asof: date,
    selected_portfolios: Sequence[SelectedPortfolioObservation],
) -> list[RealizedReturnObservation]:
    ordered = sorted(rows, key=lambda item: (item.return_date, item.observation_id))
    if not ordered:
        raise ValueError("a non-overlapping realized-return stream is required")
    if any(row.cohort != cohort for row in ordered):
        raise ValueError("realized returns cannot pool cohorts")
    if any(row.horizon_sessions != horizon for row in ordered):
        raise ValueError("realized returns cannot pool horizons")
    if len({row.observation_id for row in ordered}) != len(ordered):
        raise ValueError("realized-return observation identities must be unique")
    if len({row.return_date for row in ordered}) != len(ordered):
        raise ValueError("realized-return dates must be non-overlapping and unique")
    if ordered[-1].return_date > decision_asof:
        raise ValueError("realized-return evidence cannot postdate the decision")
    selected = {row.observation_id: row for row in selected_portfolios}
    if len(selected) != len(selected_portfolios):
        raise ValueError("selected-portfolio identities must be unique")
    source_order: list[str] = []
    observed_sources: set[str] = set()
    for row in ordered:
        source = selected.get(row.source_portfolio_observation_id)
        if source is None:
            raise ValueError("realized return references an unknown selected portfolio")
        if (
            row.fold_id != source.fold_id
            or row.cohort != source.cohort
            or row.horizon_sessions != source.horizon_sessions
        ):
            raise ValueError("realized-return lineage disagrees with its selected portfolio")
        if row.return_date <= source.asof_date:
            raise ValueError("realized return cannot predate its portfolio signal")
        if not source_order or source_order[-1] != source.observation_id:
            if source.observation_id in observed_sources:
                raise ValueError("realized-return source portfolios must form contiguous blocks")
            source_order.append(source.observation_id)
            observed_sources.add(source.observation_id)
    expected_order = [
        row.observation_id
        for row in sorted(selected_portfolios, key=lambda item: (item.asof_date, item.observation_id))
    ]
    if source_order != expected_order:
        raise ValueError("realized-return path must cover selected portfolios in chronological order")
    return ordered


def _validated_selected_portfolios(
    rows: Sequence[SelectedPortfolioObservation],
    *,
    observations: Sequence[ReturnObservation],
    maximum_gross_exposure: float,
) -> tuple[list[SelectedPortfolioObservation], float, float]:
    ordered = sorted(rows, key=lambda item: (item.asof_date, item.observation_id))
    if not ordered:
        raise ValueError("dated selected portfolios are required")
    if len({row.observation_id for row in ordered}) != len(ordered):
        raise ValueError("selected-portfolio observation identities must be unique")
    observed = {row.observation_id: row for row in observations}
    if set(observed) != {row.observation_id for row in ordered}:
        raise ValueError("selected portfolios must match the exact outer-test observation census")
    worst_hhi = 0.0
    worst_single = 0.0
    for portfolio in ordered:
        observation = observed[portfolio.observation_id]
        if (
            portfolio.fold_id != observation.fold_id
            or portfolio.asof_date != observation.asof_date
            or portfolio.cohort != observation.cohort
            or portfolio.horizon_sessions != observation.horizon_sessions
        ):
            raise ValueError("selected portfolio lineage does not match its return observation")
        hhi, maximum = concentration_metrics(
            [weight for _, weight in portfolio.weights],
            maximum_gross_exposure=maximum_gross_exposure,
        )
        worst_hhi = max(worst_hhi, hhi)
        worst_single = max(worst_single, maximum)
    return ordered, worst_hhi, worst_single


def evaluate_cohort(
    observations: Sequence[ReturnObservation],
    *,
    realized_returns: Sequence[RealizedReturnObservation],
    outer_test_folds: Sequence[WalkForwardFold],
    decision_asof: date,
    framework: Mapping[str, Any],
    candidate_performance_by_fold: Mapping[str, Mapping[str, float]] | None,
    selected_portfolios: Sequence[SelectedPortfolioObservation],
) -> dict[str, Any]:
    """Evaluate exactly one cohort and one horizon on completed outer-OOS labels."""

    _exact_date(decision_asof, label="decision_asof")
    validated_framework = validate_framework(framework)
    rows = sorted(observations, key=lambda item: (item.asof_date, item.observation_id))
    if not rows:
        raise ValueError("cohort evaluation requires observations")
    if len({row.cohort for row in rows}) != 1:
        raise ValueError("cohort observations cannot be pooled")
    if len({row.horizon_sessions for row in rows}) != 1:
        raise ValueError("horizons must be evaluated independently")
    if len({row.observation_id for row in rows}) != len(rows):
        raise ValueError("outer-test observation identities must be unique")
    if len({row.asof_date for row in rows}) != len(rows):
        raise ValueError("each horizon requires one portfolio observation per signal date")
    if any(row.label_completion_date > decision_asof for row in rows):
        raise ValueError("outer-test labels must complete on or before decision_asof")
    cohort = rows[0].cohort
    horizon = rows[0].horizon_sessions
    outer_membership, fold_payload = _validated_outer_test_membership(outer_test_folds)
    if set(outer_membership) != {row.asof_date for row in rows}:
        raise ValueError("observations must match the exact outer-test fold census")
    if any(outer_membership[row.asof_date] != row.fold_id for row in rows):
        raise ValueError("observation fold identities do not match the outer-test registry")
    candidates, candidate_folds, candidate_rows = _validated_candidate_matrix(candidate_performance_by_fold)
    expected_candidate_folds = {fold.fold_id for fold in outer_test_folds}
    if set(candidate_folds) != expected_candidate_folds:
        raise ValueError("candidate net-alpha matrix must match the exact outer-test fold census")
    candidate_sharpes = _candidate_sharpe_ratios(candidate_rows)
    settings = validated_framework["evaluation"]["estimator_settings"]
    portfolios, hhi, maximum_weight = _validated_selected_portfolios(
        selected_portfolios,
        observations=rows,
        maximum_gross_exposure=float(settings["maximum_portfolio_gross_exposure"]),
    )
    realized = _validated_realized_rows(
        realized_returns,
        cohort=cohort,
        horizon=horizon,
        decision_asof=decision_asof,
        selected_portfolios=portfolios,
    )
    alpha = [row.paired_net_alpha for row in rows]
    realized_net = [row.net_strategy_return for row in realized]
    performance: dict[str, float | int] = {
        "paired_net_alpha_lcb": block_bootstrap_lcb(
            alpha,
            confidence=float(settings["bootstrap_confidence"]),
            block_size=int(settings["block_size_by_horizon"][str(horizon)]),
            samples=int(settings["bootstrap_samples"]),
            seed=int(settings["bootstrap_seed"]),
        ),
        "net_alpha_mean": statistics.fmean(alpha),
        "absolute_profit_factor": profit_factor(realized_net),
        "relative_profit_factor": profit_factor(alpha),
        "robust_profit_factor": profit_factor(_winsorized(alpha, fraction=float(settings["winsor_fraction"]))),
        "deflated_sharpe_ratio": deflated_sharpe_ratio(alpha, candidate_sharpe_ratios=candidate_sharpes),
        "probability_of_backtest_overfitting": probability_of_backtest_overfitting(
            candidate_performance_by_fold,
            maximum_combinations=int(settings["maximum_pbo_combinations"]),
        ),
        "maximum_drawdown": maximum_drawdown(realized_net),
        "expected_shortfall_95": expected_shortfall(
            realized_net,
            tail_probability=float(settings["expected_shortfall_tail_probability"]),
        ),
        "turnover": statistics.fmean(row.turnover for row in rows),
        "average_transaction_cost": statistics.fmean(row.transaction_cost for row in rows),
        "liquidity_capacity_ratio": min(row.liquidity_capacity_ratio for row in rows),
        "winner_concentration_hhi": hhi,
        "maximum_single_name_weight": maximum_weight,
        "paired_observation_count": len(alpha),
        "positive_return_count": sum(value > 0.0 for value in alpha),
        "negative_return_count": sum(value < 0.0 for value in alpha),
    }
    validate_performance(performance, label=f"{cohort}.horizon_{horizon}")
    candidate_payload = (
        {name: {fold: candidate_performance_by_fold[name][fold] for fold in candidate_folds} for name in candidates}
        if candidate_performance_by_fold
        else {}
    )
    observation_payload = [
        {
            "observation_id": row.observation_id,
            "fold_id": row.fold_id,
            "asof_date": row.asof_date.isoformat(),
            "label_completion_date": row.label_completion_date.isoformat(),
        }
        for row in rows
    ]
    realized_payload = [
        {
            "observation_id": row.observation_id,
            "source_portfolio_observation_id": row.source_portfolio_observation_id,
            "fold_id": row.fold_id,
            "return_date": row.return_date.isoformat(),
            "net_strategy_return": row.net_strategy_return,
        }
        for row in realized
    ]
    evidence = {
        "evaluation_role": OUTER_TEST_ROLE,
        "horizon_sessions": horizon,
        "observation_count": len(rows),
        "observation_ids_sha256": _hash_payload(observation_payload),
        "fold_ids_sha256": _hash_payload(fold_payload),
        "signal_start_date": rows[0].asof_date.isoformat(),
        "signal_end_date": rows[-1].asof_date.isoformat(),
        "latest_label_completion_date": max(row.label_completion_date for row in rows).isoformat(),
        "candidate_matrix_sha256": _hash_payload(candidate_payload),
        "selected_weights_sha256": _hash_payload(
            [
                {
                    "observation_id": row.observation_id,
                    "fold_id": row.fold_id,
                    "asof_date": row.asof_date.isoformat(),
                    "selected_candidate_id": row.selected_candidate_id,
                    "weights": [[ticker, weight] for ticker, weight in row.weights],
                }
                for row in portfolios
            ]
        ),
        "realized_return_stream_sha256": _hash_payload(realized_payload),
        "realized_return_count": len(realized),
        "realized_return_start_date": realized[0].return_date.isoformat(),
        "realized_return_end_date": realized[-1].return_date.isoformat(),
    }
    return {"performance": performance, "evidence": evidence}


def evaluate_all_horizons(
    observations: Sequence[ReturnObservation],
    *,
    realized_returns_by_horizon: Mapping[str, Sequence[RealizedReturnObservation]],
    decision_asof: date,
    framework: Mapping[str, Any],
    outer_test_folds_by_horizon: Mapping[str, Sequence[WalkForwardFold]],
    candidate_performance_by_horizon: Mapping[str, Mapping[str, Mapping[str, float]]],
    selected_portfolios_by_horizon: Mapping[str, Sequence[SelectedPortfolioObservation]],
) -> dict[str, dict[str, Any]]:
    if set(outer_test_folds_by_horizon) != REQUIRED_HORIZON_KEYS:
        raise ValueError("outer-test folds must cover exact 21/63/126 horizons")
    if set(candidate_performance_by_horizon) != REQUIRED_HORIZON_KEYS:
        raise ValueError("candidate matrices must cover exact 21/63/126 horizons")
    if set(realized_returns_by_horizon) != REQUIRED_HORIZON_KEYS:
        raise ValueError("realized-return streams must cover exact 21/63/126 horizons")
    if set(selected_portfolios_by_horizon) != REQUIRED_HORIZON_KEYS:
        raise ValueError("selected portfolios must cover exact 21/63/126 horizons")
    rows_by_horizon = {
        str(horizon): [row for row in observations if row.horizon_sessions == horizon] for horizon in REQUIRED_HORIZONS
    }
    if any(not rows for rows in rows_by_horizon.values()):
        raise ValueError("outer-test observations must cover exact 21/63/126 horizons")
    return {
        key: evaluate_cohort(
            rows_by_horizon[key],
            realized_returns=realized_returns_by_horizon[key],
            outer_test_folds=outer_test_folds_by_horizon[key],
            decision_asof=decision_asof,
            framework=framework,
            candidate_performance_by_fold=candidate_performance_by_horizon[key],
            selected_portfolios=selected_portfolios_by_horizon[key],
        )
        for key in sorted(REQUIRED_HORIZON_KEYS, key=int)
    }


def active_gate_failures(performance: Mapping[str, Any], *, framework: Mapping[str, Any]) -> tuple[str, ...]:
    return performance_gate_failures(performance, framework=framework)


def recommend_next_state(
    performance: Mapping[str, Any],
    *,
    framework: Mapping[str, Any],
    current_state: str,
) -> tuple[str, tuple[str, ...]]:
    """Evidence-only helper; governed decisions additionally enforce chain and dwell."""

    if current_state not in DECISION_STATES:
        raise ValueError("current_state is unsupported")
    failures = performance_gate_failures(performance, framework=framework)
    if failures:
        return (
            "rollback" if current_state in {"active_pilot", "active_scaled", "active_full"} else current_state,
            failures,
        )
    state = {
        "rollback": "active_pilot",
        "benchmark_production": "active_pilot",
        "active_pilot": "active_scaled",
        "active_scaled": "active_full",
        "active_full": "active_full",
    }[current_state]
    return state, ()


def build_calibration_decision(
    *,
    asof_date: date,
    framework: Mapping[str, Any],
    horizon_results_by_cohort: Mapping[str, Mapping[str, Mapping[str, Any]]],
    input_panel_sha256: str,
    fold_registry_sha256: str,
    candidate_registry_sha256: str,
    code_sha256: str,
    previous_decision: Mapping[str, Any] | None = None,
    decision_history: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build and immediately validate a predecessor-bound four-cohort decision."""

    _exact_date(asof_date, label="asof_date")
    validated_framework = validate_framework(framework)
    if set(horizon_results_by_cohort) != REQUIRED_COHORTS:
        raise ValueError("results must cover the exact Consumer cohort census")
    sequence = 1 if previous_decision is None else int(previous_decision["decision_sequence"]) + 1
    previous_hash = None if previous_decision is None else previous_decision["payload_sha256"]
    material_change = previous_decision is not None and any(
        new != previous_decision[key]
        for key, new in {
            "candidate_registry_sha256": candidate_registry_sha256,
            "code_sha256": code_sha256,
        }.items()
    )
    cohorts: dict[str, Any] = {}
    policy = validated_framework["state_transition_policy"]
    for cohort in sorted(REQUIRED_COHORTS):
        results = horizon_results_by_cohort[cohort]
        if set(results) != REQUIRED_HORIZON_KEYS:
            raise ValueError(f"{cohort}: results must cover exact 21/63/126 horizons")
        performance = {key: dict(results[key]["performance"]) for key in sorted(results, key=int)}
        evidence = {key: dict(results[key]["evidence"]) for key in sorted(results, key=int)}
        failures = sorted(
            f"{key}:{failure}"
            for key in sorted(performance, key=int)
            for failure in performance_gate_failures(
                performance[key], framework=validated_framework, label=f"{cohort}.horizon_{key}"
            )
        )
        if previous_decision is None:
            prior_state = "benchmark_production"
            prior_entered = None
            state = "active_pilot" if not failures else "benchmark_production"
            blockers: list[str] = []
            state_entered = asof_date.isoformat()
        else:
            prior_item = previous_decision["cohorts"][cohort]
            prior_state = prior_item["state"]
            prior_entered = prior_item["state_entered_asof"]
            growth_blockers = []
            for key in sorted(REQUIRED_HORIZON_KEYS, key=int):
                growth = evidence[key]["observation_count"] - prior_item["horizon_evidence"][key]["observation_count"]
                if growth < policy["minimum_new_paired_observations_per_horizon_for_advancement"]:
                    growth_blockers.append(f"minimum_new_paired_observations_{key}")
            elapsed = (asof_date - date.fromisoformat(prior_entered)).days
            state, blockers = _expected_transition(
                prior_state=prior_state,
                failed_gates=failures,
                material_model_change=material_change,
                elapsed_days=elapsed,
                growth_blockers=growth_blockers,
                framework=validated_framework,
            )
            reset_same_tier = (
                material_change and prior_state in {"active_pilot", "active_scaled", "active_full"} and not failures
            )
            state_entered = asof_date.isoformat() if reset_same_tier or state != prior_state else prior_entered
        cohorts[cohort] = {
            "prior_state": prior_state,
            "prior_state_entered_asof": prior_entered,
            "state": state,
            "state_entered_asof": state_entered,
            "active_cap": float(validated_framework["capital_tiers"][state]["active_cap"]),
            "horizon_performance": performance,
            "horizon_evidence": evidence,
            "failed_gates": failures,
            "transition_blockers": blockers,
        }
    decision: dict[str, Any] = {
        "schema_version": DECISION_SCHEMA,
        "model_family": "consumer_defensive",
        "asof_date": asof_date.isoformat(),
        "framework_sha256": framework_sha256(validated_framework),
        "shared_service_contract_sha256": validated_framework["ownership"]["shared_service_contract_sha256"],
        "input_panel_sha256": input_panel_sha256,
        "fold_registry_sha256": fold_registry_sha256,
        "candidate_registry_sha256": candidate_registry_sha256,
        "code_sha256": code_sha256,
        "decision_sequence": sequence,
        "previous_decision_sha256": previous_hash,
        "calibration_completed": True,
        "cohorts": cohorts,
    }
    decision["payload_sha256"] = canonical_sha256(decision)
    return validate_calibration_decision(
        decision,
        framework=validated_framework,
        previous_decision=previous_decision,
        decision_history=decision_history,
    )


__all__ = [
    "OUTER_TEST_ROLE",
    "PF_FINITE_CAP",
    "RealizedReturnObservation",
    "ReturnObservation",
    "SelectedPortfolioObservation",
    "WalkForwardFold",
    "active_gate_failures",
    "block_bootstrap_lcb",
    "build_calibration_decision",
    "build_nested_purged_walk_forward",
    "concentration_metrics",
    "deflated_sharpe_ratio",
    "evaluate_all_horizons",
    "evaluate_cohort",
    "expected_shortfall",
    "maximum_drawdown",
    "probability_of_backtest_overfitting",
    "profit_factor",
    "recommend_next_state",
]




