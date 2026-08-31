"""Economic and statistical promotion governance for technology models.

The family optimizers remain independent. This module consumes their governed
Stage 8, walk-forward, and Stage 9 artifacts and applies one technology-owned
decision framework without mutating production weights.
"""
from __future__ import annotations

import bisect
import csv
import json
import math
import os
import random
import shutil
import sqlite3
import statistics
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import NormalDist
from typing import Any, Iterable, Mapping, Sequence

from technology.core.calibration_governance import canonical_sha256, sha256_file
from technology.core.config import cfg_get, load_yaml, resolve_path
from technology.core.optuna_artifact_governance import validate_stage8, validate_walk_forward


SCHEMA_VERSION = "technology_consolidated_promotion_v1"
MANIFEST_SCHEMA_VERSION = "technology_consolidated_promotion_manifest_v1"
ALLOWED_DECISIONS = {
    "full_promotion",
    "limited_promotion",
    "shadow_challenger",
    "retain_incumbent",
}
FAMILY_CONFIG_KEYS = {
    "semiconductors": "semiconductor_optuna_calibration",
    "software_infrastructure": "software_infrastructure_optuna_calibration",
    "technology_hardware": "technology_hardware_optuna_calibration",
}


@dataclass(frozen=True)
class FamilyPaths:
    family: str
    calibration_family: str
    optuna_output_dir: Path
    periods_csv: Path
    holdings_csv: Path
    candidate_model: str
    incumbent_model: str


@dataclass(frozen=True)
class ScoreBundle:
    economic_advantage: float
    risk_efficiency: float
    predictive_evidence: float
    deployability: float
    base_score: float
    confidence: float
    adjusted_score: float


def _as_float(value: Any, default: float | None = None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _parse_date(value: Any) -> date:
    return date.fromisoformat(str(value).strip()[:10])


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        raise RuntimeError(f"Missing or empty CSV: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists() or path.stat().st_size == 0:
        raise RuntimeError(f"Missing or empty JSON: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"JSON root must be an object: {path}")
    return value


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.replace(temp_name, path)
    finally:
        Path(temp_name).unlink(missing_ok=True)


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    _atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    if not rows:
        raise RuntimeError(f"Refusing to write empty promotion artifact: {path}")
    fieldnames: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                fieldnames.append(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temp_name, path)
    finally:
        Path(temp_name).unlink(missing_ok=True)


def _piecewise_score(value: float | None, anchors: Sequence[Any]) -> float:
    if value is None or len(anchors) != 3:
        return 50.0
    bad, neutral, good = (float(item) for item in anchors)
    if not bad < neutral < good:
        raise ValueError(f"Score anchors must be strictly increasing: {anchors}")
    if value <= bad:
        return 0.0
    if value >= good:
        return 100.0
    if value <= neutral:
        return 50.0 * (value - bad) / (neutral - bad)
    return 50.0 + 50.0 * (value - neutral) / (good - neutral)


def _quantile(values: Sequence[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = _clamp(probability, 0.0, 1.0) * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _compound(values: Iterable[float]) -> float:
    return math.prod(1.0 + value for value in values) - 1.0


def _max_drawdown(values: Sequence[float]) -> float:
    equity = 1.0
    peak = 1.0
    maximum_drawdown = 0.0
    for value in values:
        equity *= 1.0 + value
        peak = max(peak, equity)
        if peak > 0.0:
            maximum_drawdown = min(maximum_drawdown, equity / peak - 1.0)
    return maximum_drawdown


def _expected_shortfall(values: Sequence[float], tail_probability: float = 0.05) -> float:
    if not values:
        return 0.0
    count = max(1, math.ceil(len(values) * tail_probability))
    return statistics.fmean(sorted(values)[:count])


def _profit_factor(values: Sequence[float]) -> float:
    gains = sum(value for value in values if value > 0.0)
    losses = -sum(value for value in values if value < 0.0)
    if losses <= 0.0:
        return 10.0 if gains > 0.0 else 1.0
    return min(10.0, gains / losses)


def _return_statistics(values: Sequence[float], horizon_days: int) -> dict[str, float]:
    if not values:
        raise RuntimeError(f"No returns for {horizon_days}-session evaluation")
    periods_per_year = 252.0 / horizon_days
    wealth = math.prod(1.0 + value for value in values)
    cagr = wealth ** (periods_per_year / len(values)) - 1.0 if wealth > 0.0 else -1.0
    mean = statistics.fmean(values)
    standard_deviation = statistics.stdev(values) if len(values) > 1 else 0.0
    annualized_volatility = standard_deviation * math.sqrt(periods_per_year)
    maximum_drawdown = _max_drawdown(values)
    return {
        "periods": float(len(values)),
        "terminal_wealth": wealth,
        "cagr": cagr,
        "annualized_volatility": annualized_volatility,
        "sharpe": mean * periods_per_year / annualized_volatility if annualized_volatility > 0.0 else 0.0,
        "hit_rate": sum(value > 0.0 for value in values) / len(values),
        "maximum_drawdown": maximum_drawdown,
        "expected_shortfall_95": _expected_shortfall(values),
        "profit_factor": _profit_factor(values),
        "calmar": cagr / abs(maximum_drawdown) if maximum_drawdown < 0.0 else 0.0,
    }


def _paired_statistics(active_returns: Sequence[float], horizon_days: int) -> dict[str, float]:
    if not active_returns:
        raise RuntimeError("No paired active returns")
    periods_per_year = 252.0 / horizon_days
    mean = statistics.fmean(active_returns)
    standard_deviation = statistics.stdev(active_returns) if len(active_returns) > 1 else 0.0
    standard_error = standard_deviation / math.sqrt(len(active_returns)) if standard_deviation > 0.0 else 0.0
    return {
        "active_return_annualized": mean * periods_per_year,
        "active_t_stat": mean / standard_error if standard_error > 0.0 else 0.0,
        "information_ratio": mean * math.sqrt(periods_per_year) / standard_deviation if standard_deviation > 0.0 else 0.0,
        "active_win_rate": sum(value > 0.0 for value in active_returns) / len(active_returns),
        "active_profit_factor": _profit_factor(active_returns),
    }


def _circular_block_bootstrap(
    values: Sequence[float],
    *,
    repetitions: int,
    block_length: int,
    seed: int,
    lower_quantile: float,
    annualization: float,
) -> dict[str, float]:
    if not values:
        return {"positive_probability": 0.0, "annualized_mean_lcb": 0.0, "annualized_mean_median": 0.0}
    if repetitions < 100:
        raise ValueError("Bootstrap repetitions must be at least 100")
    rng = random.Random(seed)
    count = len(values)
    length = max(1, min(block_length, count))
    means: list[float] = []
    for _ in range(repetitions):
        sample: list[float] = []
        while len(sample) < count:
            start = rng.randrange(count)
            sample.extend(values[(start + offset) % count] for offset in range(length))
        means.append(statistics.fmean(sample[:count]) * annualization)
    lower = _quantile(means, lower_quantile)
    median = _quantile(means, 0.50)
    return {
        "positive_probability": sum(value > 0.0 for value in means) / repetitions,
        "annualized_mean_lcb": lower if lower is not None else 0.0,
        "annualized_mean_median": median if median is not None else 0.0,
    }


def _probabilistic_sharpe(values: Sequence[float], *, trials: int) -> dict[str, float]:
    """Return PSR and a conservative Bonferroni multiplicity adjustment.

    The adjustment is deliberately labelled an approximation because the full
    trial-level Sharpe distribution required by the exact deflated Sharpe ratio
    is not part of the existing Optuna artifacts.
    """
    if len(values) < 3:
        return {"probabilistic_sharpe_ratio": 0.5, "deflated_sharpe_probability_approx": 0.0}
    mean = statistics.fmean(values)
    deviation = statistics.stdev(values)
    if deviation <= 0.0:
        probability = 1.0 if mean > 0.0 else 0.0
        return {
            "probabilistic_sharpe_ratio": probability,
            "deflated_sharpe_probability_approx": probability,
        }
    sharpe = mean / deviation
    centered = [(value - mean) / deviation for value in values]
    skewness = statistics.fmean(value**3 for value in centered)
    kurtosis = statistics.fmean(value**4 for value in centered)
    variance_term = max(1e-12, 1.0 - skewness * sharpe + ((kurtosis - 1.0) / 4.0) * sharpe * sharpe)
    z_score = sharpe * math.sqrt(len(values) - 1.0) / math.sqrt(variance_term)
    probability = NormalDist().cdf(z_score)
    adjusted = max(0.0, 1.0 - min(1.0, (1.0 - probability) * max(1, trials)))
    return {
        "probabilistic_sharpe_ratio": probability,
        "deflated_sharpe_probability_approx": adjusted,
    }


def _resolve_family_paths(policy: Mapping[str, Any], policy_path: Path, family: str) -> FamilyPaths:
    raw = cfg_get(dict(policy), f"families.{family}")
    if not isinstance(raw, dict):
        raise KeyError(f"Promotion policy has no family definition: {family}")
    base_dir = policy_path.parent
    return FamilyPaths(
        family=family,
        calibration_family=str(raw.get("calibration_family") or family),
        optuna_output_dir=resolve_path(raw.get("optuna_output_dir"), base_dir=base_dir),
        periods_csv=resolve_path(raw.get("backtest_periods_csv"), base_dir=base_dir),
        holdings_csv=resolve_path(raw.get("backtest_holdings_csv"), base_dir=base_dir),
        candidate_model=str(raw.get("candidate_model") or ""),
        incumbent_model=str(raw.get("incumbent_model") or ""),
    )


def _selected_period_rows(
    path: Path,
    *,
    candidate_model: str,
    incumbent_model: str,
    portfolio_name: str,
    weight_method: str,
    exposure_mode: str,
    holdout_start: date,
    holdout_end: date,
) -> list[dict[str, Any]]:
    selected: dict[tuple[str, date], dict[str, Any]] = {}
    for row in _read_csv(path):
        model = str(row.get("model_name") or "")
        if model not in {candidate_model, incumbent_model}:
            continue
        if row.get("portfolio_name") != portfolio_name:
            continue
        if row.get("weight_method") != weight_method or row.get("exposure_mode") != exposure_mode:
            continue
        asof = _parse_date(row.get("asof_date"))
        if not holdout_start <= asof <= holdout_end:
            continue
        key = (model, asof)
        if key in selected:
            raise RuntimeError(f"Duplicate backtest period for {model} on {asof}: {path}")
        required = ("net_return", "benchmark_return", "equal_weight_benchmark_return", "turnover", "total_cost")
        missing = [name for name in required if _as_float(row.get(name)) is None]
        if missing:
            raise RuntimeError(f"Backtest period {model} {asof} is missing numeric fields: {missing}")
        selected[key] = {
            **row,
            "asof_date": asof,
            "net_return": float(row["net_return"]),
            "benchmark_return": float(row["benchmark_return"]),
            "equal_weight_benchmark_return": float(row["equal_weight_benchmark_return"]),
            "turnover": float(row["turnover"]),
            "total_cost": float(row["total_cost"]),
            "max_cohort_share": _as_float(row.get("max_cohort_share"), 0.0) or 0.0,
        }
    candidate_dates = {asof for model, asof in selected if model == candidate_model}
    incumbent_dates = {asof for model, asof in selected if model == incumbent_model}
    matched_dates = sorted(candidate_dates & incumbent_dates)
    if candidate_dates != incumbent_dates:
        missing_candidate = sorted(incumbent_dates - candidate_dates)
        missing_incumbent = sorted(candidate_dates - incumbent_dates)
        raise RuntimeError(
            "Candidate/incumbent holdout dates do not match: "
            f"missing_candidate={missing_candidate[:5]} missing_incumbent={missing_incumbent[:5]}"
        )
    return [selected[(model, asof)] for asof in matched_dates for model in (candidate_model, incumbent_model)]


def _matched_base_rows(rows: Sequence[Mapping[str, Any]], candidate_model: str, incumbent_model: str) -> list[dict[str, Any]]:
    by_key = {(str(row["model_name"]), row["asof_date"]): row for row in rows}
    dates = sorted({row["asof_date"] for row in rows})
    matched: list[dict[str, Any]] = []
    for asof in dates:
        candidate = by_key[(candidate_model, asof)]
        incumbent = by_key[(incumbent_model, asof)]
        if abs(float(candidate["benchmark_return"]) - float(incumbent["benchmark_return"])) > 1e-12:
            raise RuntimeError(f"Benchmark return mismatch between models on {asof}")
        if abs(float(candidate["equal_weight_benchmark_return"]) - float(incumbent["equal_weight_benchmark_return"])) > 1e-12:
            raise RuntimeError(f"Equal-weight benchmark mismatch between models on {asof}")
        matched.append(
            {
                "asof_date": asof,
                "candidate_return": float(candidate["net_return"]),
                "incumbent_return": float(incumbent["net_return"]),
                "benchmark_return": float(candidate["benchmark_return"]),
                "equal_weight_return": float(candidate["equal_weight_benchmark_return"]),
                "candidate_turnover": float(candidate["turnover"]),
                "incumbent_turnover": float(incumbent["turnover"]),
                "candidate_cost": float(candidate["total_cost"]),
                "incumbent_cost": float(incumbent["total_cost"]),
                "candidate_cohort_share": float(candidate["max_cohort_share"]),
                "incumbent_cohort_share": float(incumbent["max_cohort_share"]),
            }
        )
    return matched


def _horizon_blocks(base_rows: Sequence[Mapping[str, Any]], *, base_days: int, horizon_days: int) -> list[dict[str, Any]]:
    if horizon_days % base_days != 0:
        raise ValueError(f"Evaluation horizon {horizon_days} must be divisible by base period {base_days}")
    block_size = horizon_days // base_days
    blocks: list[dict[str, Any]] = []
    for start in range(0, len(base_rows) - block_size + 1, block_size):
        group = base_rows[start : start + block_size]
        candidate_return = _compound(float(row["candidate_return"]) for row in group)
        incumbent_return = _compound(float(row["incumbent_return"]) for row in group)
        blocks.append(
            {
                "block_start": group[0]["asof_date"].isoformat(),
                "block_end": group[-1]["asof_date"].isoformat(),
                "candidate_return": candidate_return,
                "incumbent_return": incumbent_return,
                "active_return": candidate_return - incumbent_return,
                "benchmark_return": _compound(float(row["benchmark_return"]) for row in group),
                "equal_weight_return": _compound(float(row["equal_weight_return"]) for row in group),
            }
        )
    return blocks


def _horizon_metrics(blocks: Sequence[Mapping[str, Any]], horizon_days: int) -> dict[str, Any]:
    candidate_returns = [float(row["candidate_return"]) for row in blocks]
    incumbent_returns = [float(row["incumbent_return"]) for row in blocks]
    benchmark_returns = [float(row["benchmark_return"]) for row in blocks]
    equal_weight_returns = [float(row["equal_weight_return"]) for row in blocks]
    active_returns = [float(row["active_return"]) for row in blocks]
    candidate = _return_statistics(candidate_returns, horizon_days)
    incumbent = _return_statistics(incumbent_returns, horizon_days)
    benchmark = _return_statistics(benchmark_returns, horizon_days)
    equal_weight = _return_statistics(equal_weight_returns, horizon_days)
    paired = _paired_statistics(active_returns, horizon_days)
    return {
        "horizon_days": horizon_days,
        "periods": len(blocks),
        **{f"candidate_{key}": value for key, value in candidate.items() if key != "periods"},
        **{f"incumbent_{key}": value for key, value in incumbent.items() if key != "periods"},
        **{f"benchmark_{key}": value for key, value in benchmark.items() if key != "periods"},
        **{f"equal_weight_{key}": value for key, value in equal_weight.items() if key != "periods"},
        **paired,
        "incremental_cagr": candidate["cagr"] - incumbent["cagr"],
        "relative_terminal_wealth": (
            candidate["terminal_wealth"] / incumbent["terminal_wealth"]
            if incumbent["terminal_wealth"] > 0.0
            else 0.0
        ),
        "candidate_cagr_vs_benchmark": candidate["cagr"] - benchmark["cagr"],
        "candidate_cagr_vs_equal_weight": candidate["cagr"] - equal_weight["cagr"],
        "max_drawdown_improvement": candidate["maximum_drawdown"] - incumbent["maximum_drawdown"],
        "expected_shortfall_improvement": candidate["expected_shortfall_95"] - incumbent["expected_shortfall_95"],
        "calmar_improvement": candidate["calmar"] - incumbent["calmar"],
        "active_returns": active_returns,
    }


def _find_model_row(rows: Sequence[Mapping[str, Any]], model: str) -> Mapping[str, Any]:
    matches = [row for row in rows if str(row.get("model") or "") == model]
    if len(matches) != 1:
        raise RuntimeError(f"Expected exactly one {model} row; found {len(matches)}")
    return matches[0]


def _numeric(source: Mapping[str, Any], key: str, default: float = 0.0) -> float:
    return _as_float(source.get(key), default) or 0.0


def _predictive_metrics(
    stage8_rows: Sequence[Mapping[str, Any]],
    walk_forward: Mapping[str, Any],
    horizons: Sequence[int],
) -> dict[str, float | int]:
    candidate = _find_model_row(stage8_rows, "stage8_best_candidate")
    baseline = _find_model_row(stage8_rows, "stage7_baseline")
    primary = horizons[0]
    secondary = horizons[1] if len(horizons) > 1 else primary
    ic_improvements = [
        _numeric(candidate, f"holdout_mean_ic_{horizon}") - _numeric(baseline, f"holdout_mean_ic_{horizon}")
        for horizon in (primary, secondary)
    ]
    spread_improvements = [
        _numeric(candidate, f"holdout_mean_spread_net_{horizon}")
        - _numeric(baseline, f"holdout_mean_spread_net_{horizon}")
        for horizon in (primary, secondary)
    ]
    newey_west_values = [
        _numeric(
            candidate,
            f"holdout_newey_west_t_stat_{horizon}",
            _numeric(candidate, f"holdout_t_stat_{horizon}"),
        )
        for horizon in (primary, secondary)
    ]
    return {
        "objective_improvement": _numeric(candidate, "holdout_objective") - _numeric(baseline, "holdout_objective"),
        "mean_ic_improvement": statistics.fmean(ic_improvements),
        "spread_improvement": statistics.fmean(spread_improvements),
        "minimum_newey_west_t_stat": min(newey_west_values),
        "mean_newey_west_t_stat": statistics.fmean(newey_west_values),
        "fold_win_fraction": _numeric(candidate, "fold_win_fraction"),
        "stage8_strict_gate_pass": _as_int(candidate.get("stage8_gate_pass")),
        "walk_forward_objective_improvement": _numeric(walk_forward, "mean_objective_improvement"),
        "walk_forward_paired_t": _numeric(walk_forward, "improvement_paired_t"),
        "walk_forward_win_rate": _numeric(walk_forward, "refit_win_rate"),
        "walk_forward_gate_pass_rate": _numeric(walk_forward, "promotion_gate_pass_rate"),
        "walk_forward_constraint_pass_rate": _numeric(walk_forward, "constraint_pass_rate"),
        "walk_forward_procedure_adds_value": _as_int(walk_forward.get("procedure_adds_value")),
        "legacy_final_promotion_eligible": _as_int(walk_forward.get("final_promotion_eligible")),
        "candidate_holdout_turnover": _numeric(candidate, "holdout_avg_top_turnover"),
        "candidate_holdout_cohort_share": _numeric(candidate, "holdout_avg_top_cohort_share"),
    }


def _holding_rows(
    path: Path,
    *,
    model: str,
    portfolio_name: str,
    weight_method: str,
    exposure_mode: str,
    holdout_start: date,
    holdout_end: date,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[date, str]] = set()
    for row in _read_csv(path):
        if row.get("model_name") != model:
            continue
        if row.get("portfolio_name") != portfolio_name:
            continue
        if row.get("weight_method") != weight_method or row.get("exposure_mode") != exposure_mode:
            continue
        asof = _parse_date(row.get("asof_date"))
        if not holdout_start <= asof <= holdout_end:
            continue
        ticker = str(row.get("ticker") or "").strip().upper()
        weight = _as_float(row.get("weight"))
        if not ticker or ticker.startswith("__") or weight is None or weight == 0.0:
            continue
        key = (asof, ticker)
        if key in seen:
            raise RuntimeError(f"Duplicate holding {ticker} on {asof}: {path}")
        seen.add(key)
        result.append({"asof_date": asof, "ticker": ticker, "weight": abs(weight)})
    return result


def _capacity_metrics(
    db_path: Path,
    holdings: Sequence[Mapping[str, Any]],
    capacity_policy: Mapping[str, Any],
) -> dict[str, Any]:
    if not holdings:
        return {
            "coverage": 0.0,
            "p05_capacity_ratio": 0.0,
            "median_capacity_ratio": 0.0,
            "observations": 0,
            "missing_tickers": [],
            "source_counts": {},
        }
    tickers = sorted({str(row["ticker"]) for row in holdings})
    first_date = min(row["asof_date"] for row in holdings)
    last_date = max(row["asof_date"] for row in holdings)
    trailing = int(capacity_policy.get("trailing_observations") or 60)
    minimum_observations = int(capacity_policy.get("minimum_trailing_observations") or 40)
    start_date = first_date - timedelta(days=max(120, trailing * 3))
    placeholders = ",".join("?" for _ in tickers)
    query = f"""
        SELECT ticker, bar_date, source_id, close, adj_close, volume
        FROM fact_price_ohlcv
        WHERE ticker IN ({placeholders}) AND bar_date BETWEEN ? AND ?
        ORDER BY ticker, source_id, bar_date
    """
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    grouped: dict[str, dict[str, list[tuple[date, float]]]] = defaultdict(lambda: defaultdict(list))
    with sqlite3.connect(uri, uri=True, timeout=120.0) as connection:
        for ticker, bar_date, source_id, close, adjusted_close, volume in connection.execute(
            query,
            [*tickers, start_date.isoformat(), last_date.isoformat()],
        ):
            price = _as_float(close)
            if price is None or price <= 0.0:
                price = _as_float(adjusted_close)
            parsed_volume = _as_float(volume)
            if price is None or price <= 0.0 or parsed_volume is None or parsed_volume <= 0.0:
                continue
            grouped[str(ticker)][str(source_id)].append((_parse_date(bar_date), price * parsed_volume))

    holding_dates: dict[str, list[date]] = defaultdict(list)
    for row in holdings:
        holding_dates[str(row["ticker"])].append(row["asof_date"])
    preference = [str(item) for item in capacity_policy.get("source_preference") or []]
    preference_rank = {source: len(preference) - index for index, source in enumerate(preference)}
    selected_source: dict[str, str] = {}
    adv_by_holding: dict[tuple[str, date], float] = {}
    for ticker in tickers:
        candidates: list[tuple[int, int, str, dict[date, float]]] = []
        for source, observations in grouped.get(ticker, {}).items():
            dates = [item[0] for item in observations]
            values = [item[1] for item in observations]
            coverage: dict[date, float] = {}
            for asof in holding_dates[ticker]:
                end = bisect.bisect_right(dates, asof)
                start = max(0, end - trailing)
                window = values[start:end]
                if len(window) >= minimum_observations:
                    coverage[asof] = statistics.fmean(window)
            candidates.append((len(coverage), preference_rank.get(source, 0), source, coverage))
        if not candidates:
            continue
        _coverage_count, _rank, source, coverage = max(candidates, key=lambda item: (item[0], item[1], item[2]))
        selected_source[ticker] = source
        for asof, adv in coverage.items():
            adv_by_holding[(ticker, asof)] = adv

    notional = float(capacity_policy.get("reference_notional_usd") or 1_000_000.0)
    participation = float(capacity_policy.get("max_adv_participation") or 0.05)
    liquidation_days = float(capacity_policy.get("liquidation_days") or 5.0)
    ratios: list[float] = []
    missing_tickers: set[str] = set()
    for row in holdings:
        ticker = str(row["ticker"])
        adv = adv_by_holding.get((ticker, row["asof_date"]))
        if adv is None:
            missing_tickers.add(ticker)
            continue
        required = notional * float(row["weight"])
        if required > 0.0:
            ratios.append(adv * participation * liquidation_days / required)
    source_counts = Counter(selected_source.values())
    return {
        "coverage": len(ratios) / len(holdings),
        "p05_capacity_ratio": _quantile(ratios, 0.05) or 0.0,
        "median_capacity_ratio": statistics.median(ratios) if ratios else 0.0,
        "observations": len(ratios),
        "total_holdings": len(holdings),
        "missing_tickers": sorted(missing_tickers),
        "source_counts": dict(sorted(source_counts.items())),
        "reference_notional_usd": notional,
        "max_adv_participation": participation,
        "liquidation_days": liquidation_days,
    }


def _anchor(policy: Mapping[str, Any], name: str) -> Sequence[Any]:
    value = cfg_get(dict(policy), f"score_anchors.{name}")
    if not isinstance(value, list) or len(value) != 3:
        raise KeyError(f"Missing score anchor triple: {name}")
    return value


def _weighted_horizon_score(
    horizon_rows: Sequence[Mapping[str, Any]],
    horizon_weights: Mapping[str, Any],
    policy: Mapping[str, Any],
    metrics: Sequence[tuple[str, str, float]],
) -> float:
    total = 0.0
    total_weight = 0.0
    for row in horizon_rows:
        horizon_weight = float(horizon_weights.get(str(row["horizon_days"])) or 0.0)
        if horizon_weight <= 0.0:
            continue
        metric_score = sum(
            metric_weight * _piecewise_score(_as_float(row.get(field)), _anchor(policy, anchor_name))
            for field, anchor_name, metric_weight in metrics
        )
        total += horizon_weight * metric_score
        total_weight += horizon_weight
    return total / total_weight if total_weight > 0.0 else 50.0


def _score_bundle(
    policy: Mapping[str, Any],
    horizon_rows: Sequence[Mapping[str, Any]],
    predictive: Mapping[str, Any],
    capacity: Mapping[str, Any],
    base_rows: Sequence[Mapping[str, Any]],
    *,
    max_turnover: float,
    max_cohort_share: float,
    deflated_sharpe_probability: float,
    positive_probability: float,
) -> ScoreBundle:
    horizon_weights = cfg_get(dict(policy), "portfolio_contract.horizon_weights", {}) or {}
    economic = _weighted_horizon_score(
        horizon_rows,
        horizon_weights,
        policy,
        (
            ("incremental_cagr", "incremental_cagr", 0.30),
            ("relative_terminal_wealth", "relative_terminal_wealth", 0.20),
            ("active_win_rate", "active_win_rate", 0.15),
            ("candidate_cagr_vs_equal_weight", "cagr_vs_equal_weight", 0.15),
            ("candidate_cagr_vs_benchmark", "cagr_vs_benchmark", 0.10),
            ("active_profit_factor", "active_profit_factor", 0.10),
        ),
    )
    risk = _weighted_horizon_score(
        horizon_rows,
        horizon_weights,
        policy,
        (
            ("max_drawdown_improvement", "max_drawdown_improvement", 0.35),
            ("expected_shortfall_improvement", "expected_shortfall_improvement", 0.30),
            ("calmar_improvement", "calmar_improvement", 0.20),
            ("candidate_maximum_drawdown", "candidate_max_drawdown", 0.15),
        ),
    )
    predictive_score = sum(
        weight * _piecewise_score(_as_float(predictive.get(field)), _anchor(policy, anchor_name))
        for field, anchor_name, weight in (
            ("objective_improvement", "objective_improvement", 0.20),
            ("mean_ic_improvement", "mean_ic_improvement", 0.20),
            ("spread_improvement", "spread_improvement", 0.10),
            ("minimum_newey_west_t_stat", "newey_west_t_stat", 0.15),
            ("fold_win_fraction", "fold_win_fraction", 0.10),
            ("walk_forward_objective_improvement", "walk_forward_objective_improvement", 0.10),
            ("walk_forward_paired_t", "walk_forward_paired_t", 0.10),
            ("walk_forward_win_rate", "walk_forward_win_rate", 0.05),
        )
    )
    candidate_cost = statistics.fmean(float(row["candidate_cost"]) for row in base_rows)
    incumbent_cost = statistics.fmean(float(row["incumbent_cost"]) for row in base_rows)
    turnover_headroom = max_turnover - float(predictive["candidate_holdout_turnover"])
    cohort_headroom = max_cohort_share - float(predictive["candidate_holdout_cohort_share"])
    deployability = sum(
        weight * _piecewise_score(value, _anchor(policy, anchor_name))
        for value, anchor_name, weight in (
            (turnover_headroom, "turnover_headroom", 0.25),
            (cohort_headroom, "cohort_headroom", 0.20),
            (incumbent_cost - candidate_cost, "cost_improvement", 0.20),
            (_as_float(capacity.get("p05_capacity_ratio")), "capacity_ratio", 0.20),
            (_as_float(capacity.get("coverage")), "capacity_coverage", 0.15),
        )
    )
    component_weights = cfg_get(dict(policy), "score_weights", {}) or {}
    base_score = (
        economic * float(component_weights.get("economic_advantage") or 0.0)
        + risk * float(component_weights.get("risk_efficiency") or 0.0)
        + predictive_score * float(component_weights.get("predictive_evidence") or 0.0)
        + deployability * float(component_weights.get("deployability") or 0.0)
    )
    pbo_proxy = 1.0 - _clamp(float(predictive.get("walk_forward_win_rate") or 0.0), 0.0, 1.0)
    confidence = _clamp(
        0.40
        + 0.20 * deflated_sharpe_probability
        + 0.20 * (1.0 - pbo_proxy)
        + 0.20 * positive_probability,
        0.0,
        1.0,
    )
    adjusted_score = 50.0 + confidence * (base_score - 50.0)
    return ScoreBundle(
        economic_advantage=_clamp(economic),
        risk_efficiency=_clamp(risk),
        predictive_evidence=_clamp(predictive_score),
        deployability=_clamp(deployability),
        base_score=_clamp(base_score),
        confidence=confidence,
        adjusted_score=_clamp(adjusted_score),
    )


def _decision(
    policy: Mapping[str, Any],
    score: ScoreBundle,
    primary: Mapping[str, Any],
    predictive: Mapping[str, Any],
    hard_failures: Sequence[str],
) -> tuple[str, bool, bool, list[str], float]:
    decision_policy = cfg_get(dict(policy), "decision_policy", {}) or {}
    material_economic_improvement = (
        float(primary["incremental_cagr"]) >= float(decision_policy["material_incremental_cagr"])
        or float(primary["relative_terminal_wealth"]) >= float(decision_policy["material_relative_wealth"])
    )
    risk_non_inferior = (
        float(primary["max_drawdown_improvement"]) >= -float(decision_policy["maximum_drawdown_deterioration"])
        and float(primary["expected_shortfall_improvement"])
        >= -float(decision_policy["maximum_expected_shortfall_deterioration"])
    )
    probability = float(primary["bootstrap_positive_probability"])
    economic_dominance = (
        material_economic_improvement
        and float(primary["active_win_rate"]) >= float(decision_policy["minimum_active_win_rate"])
        and risk_non_inferior
        and probability >= float(decision_policy["limited_promotion_min_positive_probability"])
    )
    clearly_inferior = (
        float(primary["incremental_cagr"]) <= float(decision_policy["clear_inferiority_cagr"])
        and float(primary["relative_terminal_wealth"]) <= float(decision_policy["clear_inferiority_relative_wealth"])
    )
    strong_statistical_support = (
        bool(int(predictive.get("stage8_strict_gate_pass") or 0))
        and bool(int(predictive.get("legacy_final_promotion_eligible") or 0))
    ) or score.predictive_evidence >= float(decision_policy["full_promotion_min_predictive_score"])
    reasons: list[str] = []
    if hard_failures:
        reasons.extend(f"hard_safety:{reason}" for reason in hard_failures)
        return "retain_incumbent", economic_dominance, strong_statistical_support, reasons, 0.0
    if clearly_inferior or score.adjusted_score <= float(decision_policy["retain_incumbent_max_adjusted_score"]):
        reasons.append("candidate_economically_inferior")
        return "retain_incumbent", economic_dominance, strong_statistical_support, reasons, 0.0
    if (
        economic_dominance
        and strong_statistical_support
        and score.adjusted_score >= float(decision_policy["full_promotion_min_adjusted_score"])
        and probability >= float(decision_policy["full_promotion_min_positive_probability"])
    ):
        reasons.append("economic_and_statistical_evidence_support_full_promotion")
        return "full_promotion", economic_dominance, strong_statistical_support, reasons, 1.0
    if (
        economic_dominance
        and score.adjusted_score >= float(decision_policy["limited_promotion_min_adjusted_score"])
        and probability >= float(decision_policy["limited_promotion_min_positive_probability"])
    ):
        reasons.append("economic_evidence_supports_limited_promotion_while_uncertainty_remains")
        return (
            "limited_promotion",
            economic_dominance,
            strong_statistical_support,
            reasons,
            float(decision_policy["limited_promotion_exposure_cap"]),
        )
    reasons.append("safe_but_evidence_is_not_decisive")
    return "shadow_challenger", economic_dominance, strong_statistical_support, reasons, 0.0


def evaluate_family(
    *,
    family: str,
    policy: Mapping[str, Any],
    policy_path: Path,
    technology_config: Mapping[str, Any],
    technology_config_path: Path,
    db_path: Path,
    bootstrap_repetitions: int | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    paths = _resolve_family_paths(policy, policy_path, family)
    artifact_errors: list[str] = []
    if bool(cfg_get(dict(policy), "safety.require_governed_calibration_artifacts", True)):
        artifact_errors.extend(validate_stage8(paths.calibration_family, config_path=technology_config_path))
        artifact_errors.extend(validate_walk_forward(paths.calibration_family, config_path=technology_config_path))

    weights_path = paths.optuna_output_dir / "stage8_best_weights.json"
    summary_path = paths.optuna_output_dir / "stage8_best_summary.csv"
    walk_forward_path = paths.optuna_output_dir / "walk_forward" / "walk_forward_summary.json"
    weights = _read_json(weights_path)
    stage8_rows = _read_csv(summary_path)
    walk_forward = _read_json(walk_forward_path)
    holdout_dates = weights.get("holdout_dates") or []
    if not isinstance(holdout_dates, list) or len(holdout_dates) != 2:
        raise RuntimeError(f"{family} Stage 8 weights have no two-date holdout range")
    holdout_start, holdout_end = (_parse_date(value) for value in holdout_dates)

    contract = cfg_get(dict(policy), "portfolio_contract", {}) or {}
    portfolio_name = str(contract["portfolio_name"])
    weight_method = str(contract["weight_method"])
    exposure_mode = str(contract["exposure_mode"])
    base_days = int(contract["base_period_days"])
    horizons = [int(value) for value in contract["evaluation_horizons"]]
    minimum_periods = {int(key): int(value) for key, value in dict(contract["minimum_periods"]).items()}
    period_rows = _selected_period_rows(
        paths.periods_csv,
        candidate_model=paths.candidate_model,
        incumbent_model=paths.incumbent_model,
        portfolio_name=portfolio_name,
        weight_method=weight_method,
        exposure_mode=exposure_mode,
        holdout_start=holdout_start,
        holdout_end=holdout_end,
    )
    base_rows = _matched_base_rows(period_rows, paths.candidate_model, paths.incumbent_model)
    horizon_rows: list[dict[str, Any]] = []
    block_rows: list[dict[str, Any]] = []
    bootstrap_policy = cfg_get(dict(policy), "bootstrap", {}) or {}
    repetitions = bootstrap_repetitions or int(bootstrap_policy["repetitions"])
    seed = int(bootstrap_policy["seed"])
    for horizon in horizons:
        blocks = _horizon_blocks(base_rows, base_days=base_days, horizon_days=horizon)
        metrics = _horizon_metrics(blocks, horizon)
        bootstrap = _circular_block_bootstrap(
            metrics["active_returns"],
            repetitions=repetitions,
            block_length=int(bootstrap_policy["circular_block_length"]),
            seed=seed + horizon,
            lower_quantile=float(bootstrap_policy["lower_confidence_quantile"]),
            annualization=252.0 / horizon,
        )
        metrics.pop("active_returns")
        metrics.update({f"bootstrap_{key}": value for key, value in bootstrap.items()})
        horizon_rows.append({"family": family, **metrics})
        block_rows.extend({"family": family, "horizon_days": horizon, **row} for row in blocks)

    predictive = _predictive_metrics(stage8_rows, walk_forward, horizons)
    holdings = _holding_rows(
        paths.holdings_csv,
        model=paths.candidate_model,
        portfolio_name=portfolio_name,
        weight_method=weight_method,
        exposure_mode=exposure_mode,
        holdout_start=holdout_start,
        holdout_end=holdout_end,
    )
    capacity_policy = cfg_get(dict(policy), "capacity", {}) or {}
    capacity = _capacity_metrics(db_path, holdings, capacity_policy)
    primary_horizon = int(cfg_get(dict(policy), "decision_policy.primary_economic_horizon", 63))
    primary = next(row for row in horizon_rows if int(row["horizon_days"]) == primary_horizon)
    sharpe_evidence = _probabilistic_sharpe(
        [float(row["active_return"]) for row in block_rows if int(row["horizon_days"]) == primary_horizon],
        trials=_as_int(weights.get("n_trials"), 1),
    )
    config_key = FAMILY_CONFIG_KEYS[family]
    max_turnover = float(cfg_get(dict(technology_config), f"{config_key}.max_turnover", 0.60))
    max_cohort_share = float(cfg_get(dict(technology_config), f"{config_key}.max_top_cohort_share", 0.55))
    score = _score_bundle(
        policy,
        horizon_rows,
        predictive,
        capacity,
        base_rows,
        max_turnover=max_turnover,
        max_cohort_share=max_cohort_share,
        deflated_sharpe_probability=sharpe_evidence["deflated_sharpe_probability_approx"],
        positive_probability=float(primary["bootstrap_positive_probability"]),
    )

    safety = cfg_get(dict(policy), "safety", {}) or {}
    hard_failures = list(dict.fromkeys(artifact_errors))
    for row in horizon_rows:
        horizon = int(row["horizon_days"])
        if int(row["periods"]) < minimum_periods[horizon]:
            hard_failures.append(f"insufficient_{horizon}d_periods")
    if bool(safety.get("reject_post_lock_research_override", True)) and (
        bool(weights.get("post_lock_data_included")) or bool(walk_forward.get("post_lock_data_included"))
    ):
        hard_failures.append("post_lock_research_override")
    if float(primary["candidate_maximum_drawdown"]) < float(safety["maximum_drawdown_floor"]):
        hard_failures.append("candidate_maximum_drawdown_below_floor")
    base_horizon = next(row for row in horizon_rows if int(row["horizon_days"]) == base_days)
    if float(base_horizon["candidate_expected_shortfall_95"]) < float(safety["expected_shortfall_95_floor"]):
        hard_failures.append("candidate_expected_shortfall_below_floor")
    average_cost = statistics.fmean(float(row["candidate_cost"]) for row in base_rows)
    if average_cost > float(safety["maximum_average_period_cost"]):
        hard_failures.append("candidate_average_cost_above_limit")
    if float(predictive["candidate_holdout_turnover"]) > max_turnover + 1e-12:
        hard_failures.append("candidate_turnover_above_family_cap")
    if float(predictive["candidate_holdout_cohort_share"]) > max_cohort_share + 1e-12:
        hard_failures.append("candidate_cohort_share_above_family_cap")
    if float(capacity["coverage"]) < float(capacity_policy["minimum_coverage"]):
        hard_failures.append("liquidity_capacity_coverage_below_minimum")
    if float(capacity["p05_capacity_ratio"]) < float(capacity_policy["minimum_p05_capacity_ratio"]):
        hard_failures.append("liquidity_capacity_ratio_below_minimum")
    if (
        float(predictive["walk_forward_paired_t"]) <= -2.0
        and float(predictive["walk_forward_objective_improvement"]) < 0.0
        and float(predictive["mean_ic_improvement"]) < 0.0
    ):
        hard_failures.append("strong_adverse_predictive_evidence")
    hard_failures = list(dict.fromkeys(hard_failures))

    decision, economic_dominance, strong_support, reasons, exposure_cap = _decision(
        policy,
        score,
        primary,
        predictive,
        hard_failures,
    )
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "engine_version": policy.get("engine_version"),
        "family": family,
        "calibration_family": paths.calibration_family,
        "candidate_model": paths.candidate_model,
        "incumbent_model": paths.incumbent_model,
        "decision": decision,
        "decision_reasons": reasons,
        "manual_approval_required": 1,
        "production_weights_modified": 0,
        "recommended_exposure_cap": exposure_cap,
        "economic_dominance_flag": int(economic_dominance),
        "strong_statistical_support_flag": int(strong_support),
        "hard_safety_pass": int(not hard_failures),
        "hard_safety_failures": hard_failures,
        "holdout_start": holdout_start.isoformat(),
        "holdout_end": holdout_end.isoformat(),
        "matched_base_periods": len(base_rows),
        "portfolio_contract": {
            "portfolio_name": portfolio_name,
            "weight_method": weight_method,
            "exposure_mode": exposure_mode,
            "base_period_days": base_days,
            "horizon_construction": "non_overlapping_compounded_base_periods",
        },
        "scores": {
            "economic_advantage": score.economic_advantage,
            "risk_efficiency": score.risk_efficiency,
            "predictive_evidence": score.predictive_evidence,
            "deployability": score.deployability,
            "base_score": score.base_score,
            "confidence": score.confidence,
            "adjusted_score": score.adjusted_score,
        },
        "primary_economic_evidence": {key: value for key, value in primary.items() if key != "family"},
        "predictive_evidence": dict(predictive),
        "multiple_testing_evidence": {
            **sharpe_evidence,
            "method": "probabilistic_sharpe_with_bonferroni_trial_adjustment",
            "trial_count": _as_int(weights.get("n_trials"), 1),
            "walk_forward_overfit_probability_proxy": 1.0 - float(predictive["walk_forward_win_rate"]),
            "proxy_note": "one_minus_walk_forward_refit_win_rate_not_exact_CSCV_PBO",
        },
        "capacity_evidence": capacity,
        "input_provenance": {
            "policy_path": str(policy_path),
            "policy_sha256": sha256_file(policy_path),
            "technology_config_path": str(technology_config_path),
            "technology_config_sha256": sha256_file(technology_config_path),
            "stage8_weights_path": str(weights_path),
            "stage8_weights_sha256": sha256_file(weights_path),
            "stage8_summary_path": str(summary_path),
            "stage8_summary_sha256": sha256_file(summary_path),
            "walk_forward_summary_path": str(walk_forward_path),
            "walk_forward_summary_sha256": sha256_file(walk_forward_path),
            "backtest_periods_path": str(paths.periods_csv),
            "backtest_periods_sha256": sha256_file(paths.periods_csv),
            "backtest_holdings_path": str(paths.holdings_csv),
            "backtest_holdings_sha256": sha256_file(paths.holdings_csv),
            "database_path": str(db_path),
        },
    }
    result["decision_content_sha256"] = canonical_sha256(result)
    return result, horizon_rows, block_rows


def _summary_row(result: Mapping[str, Any]) -> dict[str, Any]:
    scores = result["scores"]
    primary = result["primary_economic_evidence"]
    predictive = result["predictive_evidence"]
    capacity = result["capacity_evidence"]
    return {
        "family": result["family"],
        "candidate_model": result["candidate_model"],
        "incumbent_model": result["incumbent_model"],
        "decision": result["decision"],
        "recommended_exposure_cap": result["recommended_exposure_cap"],
        "hard_safety_pass": result["hard_safety_pass"],
        "hard_safety_failures": ";".join(result["hard_safety_failures"]),
        "economic_dominance_flag": result["economic_dominance_flag"],
        "strong_statistical_support_flag": result["strong_statistical_support_flag"],
        "holdout_start": result["holdout_start"],
        "holdout_end": result["holdout_end"],
        "matched_base_periods": result["matched_base_periods"],
        "economic_advantage_score": scores["economic_advantage"],
        "risk_efficiency_score": scores["risk_efficiency"],
        "predictive_evidence_score": scores["predictive_evidence"],
        "deployability_score": scores["deployability"],
        "base_score": scores["base_score"],
        "confidence": scores["confidence"],
        "adjusted_score": scores["adjusted_score"],
        "primary_incremental_cagr": primary["incremental_cagr"],
        "primary_relative_terminal_wealth": primary["relative_terminal_wealth"],
        "primary_active_win_rate": primary["active_win_rate"],
        "primary_active_t_stat": primary["active_t_stat"],
        "primary_bootstrap_positive_probability": primary["bootstrap_positive_probability"],
        "primary_bootstrap_annualized_mean_lcb": primary["bootstrap_annualized_mean_lcb"],
        "primary_max_drawdown_improvement": primary["max_drawdown_improvement"],
        "primary_expected_shortfall_improvement": primary["expected_shortfall_improvement"],
        "stage8_strict_gate_pass": predictive["stage8_strict_gate_pass"],
        "legacy_final_promotion_eligible": predictive["legacy_final_promotion_eligible"],
        "capacity_coverage": capacity["coverage"],
        "capacity_p05_ratio": capacity["p05_capacity_ratio"],
    }


def _seal_outputs(
    *,
    output_dir: Path,
    artifacts: Sequence[Path],
    policy_path: Path,
    technology_config_path: Path,
    families: Sequence[str],
) -> dict[str, Any]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    run_id = f"technology_consolidated_promotion_{stamp}_{sha256_file(policy_path)[:8]}"
    immutable_dir = output_dir / "runs" / run_id
    immutable_dir.mkdir(parents=True, exist_ok=False)
    records: list[dict[str, Any]] = []
    try:
        for path in artifacts:
            if not path.exists() or path.stat().st_size == 0:
                raise RuntimeError(f"Cannot seal missing or empty promotion artifact: {path}")
            digest = sha256_file(path)
            records.append({"name": path.name, "sha256": digest, "size_bytes": path.stat().st_size})
            shutil.copy2(path, immutable_dir / path.name)
        manifest: dict[str, Any] = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "run_id": run_id,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
            "families": list(families),
            "policy_path": str(policy_path),
            "policy_sha256": sha256_file(policy_path),
            "technology_config_path": str(technology_config_path),
            "technology_config_sha256": sha256_file(technology_config_path),
            "immutable_run_dir": str(immutable_dir),
            "artifacts": records,
        }
        manifest["manifest_content_sha256"] = canonical_sha256(manifest)
        _write_json(immutable_dir / "technology_consolidated_promotion_manifest.json", manifest)
        _write_json(output_dir / "technology_consolidated_promotion_manifest.json", manifest)
        return manifest
    except Exception:
        shutil.rmtree(immutable_dir, ignore_errors=True)
        raise


def run_consolidated_evaluation(
    *,
    policy_path: Path,
    technology_config_path: Path,
    families: Sequence[str] | None = None,
    output_dir: Path | None = None,
    bootstrap_repetitions: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    policy_path = policy_path.resolve()
    technology_config_path = technology_config_path.resolve()
    policy = load_yaml(policy_path)
    if policy.get("schema_version") != "technology_consolidated_promotion_policy_v1":
        raise RuntimeError(f"Unsupported promotion policy schema: {policy.get('schema_version')}")
    technology_config = load_yaml(technology_config_path)
    selected_families = list(families or (policy.get("families") or {}).keys())
    unknown = [family for family in selected_families if family not in FAMILY_CONFIG_KEYS]
    if unknown:
        raise ValueError(f"Unknown technology families: {unknown}")
    score_weights = cfg_get(policy, "score_weights", {}) or {}
    if abs(sum(float(value) for value in score_weights.values()) - 1.0) > 1e-9:
        raise RuntimeError("Promotion score weights must sum to 1.0")
    horizon_weights = cfg_get(policy, "portfolio_contract.horizon_weights", {}) or {}
    if abs(sum(float(value) for value in horizon_weights.values()) - 1.0) > 1e-9:
        raise RuntimeError("Promotion horizon weights must sum to 1.0")
    resolved_output = output_dir.resolve() if output_dir else resolve_path(policy["output_dir"], base_dir=policy_path.parent)
    resolved_output.mkdir(parents=True, exist_ok=True)
    db_path = resolve_path(cfg_get(technology_config, "paths.database_path"), base_dir=technology_config_path.parent)
    results: list[dict[str, Any]] = []
    all_horizon_rows: list[dict[str, Any]] = []
    all_block_rows: list[dict[str, Any]] = []
    artifacts: list[Path] = []
    for family in selected_families:
        result, horizon_rows, block_rows = evaluate_family(
            family=family,
            policy=policy,
            policy_path=policy_path,
            technology_config=technology_config,
            technology_config_path=technology_config_path,
            db_path=db_path,
            bootstrap_repetitions=bootstrap_repetitions,
        )
        results.append(result)
        all_horizon_rows.extend(horizon_rows)
        all_block_rows.extend(block_rows)
        decision_path = resolved_output / f"{family}_promotion_decision.json"
        _write_json(decision_path, result)
        artifacts.append(decision_path)
    summary_path = resolved_output / "technology_consolidated_promotion_summary.csv"
    horizons_path = resolved_output / "technology_consolidated_promotion_horizon_metrics.csv"
    blocks_path = resolved_output / "technology_consolidated_promotion_return_blocks.csv"
    _write_csv(summary_path, [_summary_row(result) for result in results])
    _write_csv(horizons_path, all_horizon_rows)
    _write_csv(blocks_path, all_block_rows)
    artifacts.extend((summary_path, horizons_path, blocks_path))
    manifest = _seal_outputs(
        output_dir=resolved_output,
        artifacts=artifacts,
        policy_path=policy_path,
        technology_config_path=technology_config_path,
        families=selected_families,
    )
    return results, manifest


def validate_consolidated_outputs(
    *,
    policy_path: Path,
    technology_config_path: Path,
    output_dir: Path | None = None,
) -> list[str]:
    policy_path = policy_path.resolve()
    technology_config_path = technology_config_path.resolve()
    policy = load_yaml(policy_path)
    resolved_output = output_dir.resolve() if output_dir else resolve_path(policy["output_dir"], base_dir=policy_path.parent)
    errors: list[str] = []
    manifest_path = resolved_output / "technology_consolidated_promotion_manifest.json"
    try:
        manifest = _read_json(manifest_path)
    except RuntimeError as exc:
        return [str(exc)]
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        errors.append("Unsupported consolidated promotion manifest schema")
    recorded_hash = str(manifest.get("manifest_content_sha256") or "")
    manifest_content = {key: value for key, value in manifest.items() if key != "manifest_content_sha256"}
    if recorded_hash != canonical_sha256(manifest_content):
        errors.append("Consolidated promotion manifest content hash mismatch")
    if manifest.get("policy_sha256") != sha256_file(policy_path):
        errors.append("Consolidated promotion policy hash is stale")
    if manifest.get("technology_config_sha256") != sha256_file(technology_config_path):
        errors.append("Consolidated promotion technology config hash is stale")
    immutable_dir = Path(str(manifest.get("immutable_run_dir") or ""))
    immutable_manifest = immutable_dir / manifest_path.name
    if not immutable_manifest.exists() or sha256_file(immutable_manifest) != sha256_file(manifest_path):
        errors.append("Immutable consolidated promotion manifest is missing or differs")
    for record in manifest.get("artifacts") or []:
        name = str(record.get("name") or "")
        expected_hash = str(record.get("sha256") or "")
        if not name or not expected_hash:
            errors.append("Malformed consolidated promotion artifact record")
            continue
        if sha256_file(resolved_output / name) != expected_hash:
            errors.append(f"Compatibility promotion artifact hash mismatch: {name}")
        if sha256_file(immutable_dir / name) != expected_hash:
            errors.append(f"Immutable promotion artifact hash mismatch: {name}")
    summary_path = resolved_output / "technology_consolidated_promotion_summary.csv"
    try:
        summary_rows = _read_csv(summary_path)
    except RuntimeError as exc:
        errors.append(str(exc))
        summary_rows = []
    expected_families = list(manifest.get("families") or [])
    if sorted(str(row.get("family") or "") for row in summary_rows) != sorted(expected_families):
        errors.append("Consolidated promotion summary family coverage mismatch")
    weights = cfg_get(policy, "score_weights", {}) or {}
    for family in expected_families:
        decision_path = resolved_output / f"{family}_promotion_decision.json"
        try:
            result = _read_json(decision_path)
        except RuntimeError as exc:
            errors.append(str(exc))
            continue
        recorded = str(result.get("decision_content_sha256") or "")
        content = {key: value for key, value in result.items() if key != "decision_content_sha256"}
        if recorded != canonical_sha256(content):
            errors.append(f"{family}: decision content hash mismatch")
        decision = str(result.get("decision") or "")
        if decision not in ALLOWED_DECISIONS:
            errors.append(f"{family}: unsupported decision {decision}")
        scores = result.get("scores") or {}
        for key in ("economic_advantage", "risk_efficiency", "predictive_evidence", "deployability", "base_score", "adjusted_score"):
            value = _as_float(scores.get(key))
            if value is None or not 0.0 <= value <= 100.0:
                errors.append(f"{family}: score {key} is outside [0, 100]")
        confidence = _as_float(scores.get("confidence"))
        if confidence is None or not 0.0 <= confidence <= 1.0:
            errors.append(f"{family}: confidence is outside [0, 1]")
        expected_base = sum(float(weights[key]) * float(scores[key]) for key in weights)
        if abs(expected_base - float(scores.get("base_score") or 0.0)) > 1e-8:
            errors.append(f"{family}: base score does not reconcile to component scores")
        expected_adjusted = 50.0 + float(scores.get("confidence") or 0.0) * (expected_base - 50.0)
        if abs(expected_adjusted - float(scores.get("adjusted_score") or 0.0)) > 1e-8:
            errors.append(f"{family}: confidence-adjusted score does not reconcile")
        hard_pass = int(result.get("hard_safety_pass") or 0)
        failures = list(result.get("hard_safety_failures") or [])
        if hard_pass != int(not failures):
            errors.append(f"{family}: hard safety flag/reasons mismatch")
        if not hard_pass and decision != "retain_incumbent":
            errors.append(f"{family}: unsafe candidate cannot be promoted or shadowed")
        if decision in {"full_promotion", "limited_promotion"} and int(result.get("economic_dominance_flag") or 0) != 1:
            errors.append(f"{family}: promotion lacks economic dominance")
        if int(result.get("production_weights_modified") or 0) != 0:
            errors.append(f"{family}: evaluation must not modify production weights")
    return errors
