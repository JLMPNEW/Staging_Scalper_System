from __future__ import annotations

import math
import random
from dataclasses import dataclass
from statistics import median
from typing import Iterable, Mapping


@dataclass(frozen=True)
class MetricSettings:
    lcb_z: float = 1.0
    cvar_q: float = 0.05
    profit_factor_cap: float = 10.0
    min_profit_factor_wins: int = 3
    min_profit_factor_losses: int = 3
    bootstrap_iterations: int = 500
    bootstrap_seed: int = 1729
    bootstrap_block_dates: int = 4


def finite_float(raw: object) -> float | None:
    try:
        value = float(str(raw))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def stdev(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    avg = sum(values) / len(values)
    return math.sqrt(sum((value - avg) ** 2 for value in values) / (len(values) - 1))


def quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = max(0.0, min(1.0, q)) * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def lower_confidence_bound(values: list[float], *, z: float) -> float | None:
    avg = mean(values)
    if avg is None:
        return None
    sigma = stdev(values)
    if sigma is None:
        return avg
    return avg - max(0.0, z) * sigma / math.sqrt(len(values))


def upper_confidence_bound(values: list[float], *, z: float) -> float | None:
    avg = mean(values)
    if avg is None:
        return None
    sigma = stdev(values)
    if sigma is None:
        return avg
    return avg + max(0.0, z) * sigma / math.sqrt(len(values))


def sortino_like(values: list[float]) -> float | None:
    avg = mean(values)
    if avg is None:
        return None
    downside = [min(0.0, value) for value in values]
    denominator = math.sqrt(sum(value * value for value in downside) / len(downside))
    return None if denominator <= 1e-12 else avg / denominator


def omega_ratio(values: list[float], *, hurdle: float = 0.0) -> float | None:
    gains = sum(max(0.0, value - hurdle) for value in values)
    losses = sum(max(0.0, hurdle - value) for value in values)
    return None if losses <= 1e-12 else gains / losses


def profit_factor(
    values: list[float],
    *,
    cap: float,
    min_wins: int,
    min_losses: int,
) -> float | None:
    gains = [value for value in values if value > 0.0]
    losses = [-value for value in values if value < 0.0]
    if len(gains) < min_wins or len(losses) < min_losses:
        return None
    denominator = sum(losses)
    if denominator <= 1e-12:
        return None
    return min(max(1.0, cap), sum(gains) / denominator)


def winsorize(values: list[float], lower_q: float = 0.05, upper_q: float = 0.95) -> list[float]:
    lower = quantile(values, lower_q)
    upper = quantile(values, upper_q)
    if lower is None or upper is None:
        return []
    return [min(max(value, lower), upper) for value in values]


def max_drawdown(values: list[float]) -> float | None:
    if not values:
        return None
    wealth = 1.0
    peak = 1.0
    worst = 0.0
    for value in values:
        wealth *= max(0.0, 1.0 + value)
        peak = max(peak, wealth)
        if peak > 0.0:
            worst = min(worst, wealth / peak - 1.0)
    return worst


def top_gain_contribution(values: list[float], top_n: int) -> float | None:
    gains = sorted((value for value in values if value > 0.0), reverse=True)
    if not gains:
        return None
    total = sum(gains)
    if total <= 1e-12:
        return None
    return sum(gains[: max(1, top_n)]) / total


def _pct(value: float | None) -> float | str:
    return "" if value is None else round(100.0 * value, 6)


def _ratio(value: float | None) -> float | str:
    return "" if value is None else round(value, 6)


def summarize_returns(values: Iterable[object], settings: MetricSettings) -> dict[str, object]:
    clean = [value for raw in values if (value := finite_float(raw)) is not None]
    if not clean:
        return {
            "n": 0,
            "win_count": 0,
            "loss_count": 0,
            "mean_return_pct": "",
            "median_return_pct": "",
            "lcb_return_pct": "",
            "ucb_return_pct": "",
            "sortino_like": "",
            "omega_ratio": "",
            "profit_factor": "",
            "winsorized_profit_factor": "",
            "profit_factor_ex_largest_winner": "",
            "profit_factor_ex_top3_winners": "",
            "hit_rate_pct": "",
            "loss20_rate_pct": "",
            "loss40_rate_pct": "",
            "cvar_return_pct": "",
            "worst_decile_return_pct": "",
            "max_drawdown_pct": "",
            "top1_gain_contribution_pct": "",
            "top3_gain_contribution_pct": "",
        }
    wins = sum(1 for value in clean if value > 0.0)
    losses = sum(1 for value in clean if value < 0.0)
    ordered_winners = sorted((value for value in clean if value > 0.0), reverse=True)
    without_largest = list(clean)
    without_top3 = list(clean)
    if ordered_winners:
        without_largest.remove(ordered_winners[0])
        for winner in ordered_winners[:3]:
            without_top3.remove(winner)
    cvar_cutoff = quantile(clean, settings.cvar_q)
    cvar_values = [value for value in clean if cvar_cutoff is not None and value <= cvar_cutoff]
    worst_decile_cutoff = quantile(clean, 0.10)
    worst_decile_values = [
        value for value in clean if worst_decile_cutoff is not None and value <= worst_decile_cutoff
    ]
    return {
        "n": len(clean),
        "win_count": wins,
        "loss_count": losses,
        "mean_return_pct": _pct(mean(clean)),
        "median_return_pct": _pct(float(median(clean))),
        "lcb_return_pct": _pct(lower_confidence_bound(clean, z=settings.lcb_z)),
        "ucb_return_pct": _pct(upper_confidence_bound(clean, z=settings.lcb_z)),
        "sortino_like": _ratio(sortino_like(clean)),
        "omega_ratio": _ratio(omega_ratio(clean)),
        "profit_factor": _ratio(
            profit_factor(
                clean,
                cap=settings.profit_factor_cap,
                min_wins=settings.min_profit_factor_wins,
                min_losses=settings.min_profit_factor_losses,
            )
        ),
        "winsorized_profit_factor": _ratio(
            profit_factor(
                winsorize(clean),
                cap=settings.profit_factor_cap,
                min_wins=settings.min_profit_factor_wins,
                min_losses=settings.min_profit_factor_losses,
            )
        ),
        "profit_factor_ex_largest_winner": _ratio(
            profit_factor(
                without_largest,
                cap=settings.profit_factor_cap,
                min_wins=settings.min_profit_factor_wins,
                min_losses=settings.min_profit_factor_losses,
            )
        ),
        "profit_factor_ex_top3_winners": _ratio(
            profit_factor(
                without_top3,
                cap=settings.profit_factor_cap,
                min_wins=settings.min_profit_factor_wins,
                min_losses=settings.min_profit_factor_losses,
            )
        ),
        "hit_rate_pct": round(100.0 * wins / len(clean), 6),
        "loss20_rate_pct": round(100.0 * sum(1 for value in clean if value <= -0.20) / len(clean), 6),
        "loss40_rate_pct": round(100.0 * sum(1 for value in clean if value <= -0.40) / len(clean), 6),
        "cvar_return_pct": _pct(mean(cvar_values)),
        "worst_decile_return_pct": _pct(mean(worst_decile_values)),
        "max_drawdown_pct": _pct(max_drawdown(clean)),
        "top1_gain_contribution_pct": _pct(top_gain_contribution(clean, 1)),
        "top3_gain_contribution_pct": _pct(top_gain_contribution(clean, 3)),
    }


def equal_weight_returns_by_date(
    rows: Iterable[Mapping[str, object]],
    *,
    return_key: str,
    date_key: str = "asof_date",
) -> dict[str, float]:
    grouped: dict[str, list[float]] = {}
    for row in rows:
        asof_date = str(row.get(date_key) or "").strip()
        value = finite_float(row.get(return_key))
        if not asof_date or value is None:
            continue
        grouped.setdefault(asof_date, []).append(value)
    return {
        asof_date: sum(values) / len(values)
        for asof_date, values in sorted(grouped.items())
        if values
    }


def _circular_block_sample(values: list[float], rng: random.Random, block_size: int) -> list[float]:
    if not values:
        return []
    block = max(1, min(int(block_size), len(values)))
    sampled: list[float] = []
    while len(sampled) < len(values):
        start = rng.randrange(len(values))
        sampled.extend(values[(start + offset) % len(values)] for offset in range(block))
    return sampled[: len(values)]


def paired_policy_comparison(
    candidate_returns: Mapping[str, float],
    incumbent_returns: Mapping[str, float],
    settings: MetricSettings,
) -> dict[str, object]:
    common_dates = sorted(set(candidate_returns).intersection(incumbent_returns))
    candidate = [candidate_returns[asof_date] for asof_date in common_dates]
    incumbent = [incumbent_returns[asof_date] for asof_date in common_dates]
    deltas = [left - right for left, right in zip(candidate, incumbent)]
    bootstrap_lcb: float | None = None
    if deltas and settings.bootstrap_iterations > 0:
        rng = random.Random(settings.bootstrap_seed)
        means = [
            mean(_circular_block_sample(deltas, rng, settings.bootstrap_block_dates))
            for _ in range(settings.bootstrap_iterations)
        ]
        clean_means = [value for value in means if value is not None]
        bootstrap_lcb = quantile(clean_means, 0.05)
    candidate_summary = summarize_returns(candidate, settings)
    incumbent_summary = summarize_returns(incumbent, settings)
    delta_summary = summarize_returns(deltas, settings)
    return {
        "paired_date_count": len(common_dates),
        "paired_start_date": common_dates[0] if common_dates else "",
        "paired_end_date": common_dates[-1] if common_dates else "",
        "paired_delta_bootstrap_lcb_pct": _pct(bootstrap_lcb),
        **{f"candidate_{key}": value for key, value in candidate_summary.items()},
        **{f"incumbent_{key}": value for key, value in incumbent_summary.items()},
        **{f"delta_{key}": value for key, value in delta_summary.items()},
    }

