"""Pure mechanics for the Stage 11 rank-reconstitution long replay."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np
import pandas as pd


REGIME_MODES = ("unconditional", "v1_gate", "h1_gate")


@dataclass
class Position:
    ticker: str
    pipeline: str
    signal_date: str
    entry_date: str
    entry_price: float
    previous_price: float
    previous_benchmark_price: float
    entry_score_z: float
    entry_rank_pct: float
    initial_weight: float
    current_exposure: float
    max_holding_days: int
    holding_days: int = 0
    gross_pnl: float = 0.0
    benchmark_pnl: float = 0.0
    transaction_cost: float = 0.0
    stress_cost: float = 0.0
    pending_exit_reason: str | None = None


def economic_name_limit(
    *,
    sector_weight: float,
    aum_usd: float,
    commission_usd: float,
    max_commission_fraction: float,
    absolute_cap: int,
) -> int:
    """Maximum names whose one-way commission remains economically bounded."""
    if (
        sector_weight <= 0
        or aum_usd <= 0
        or commission_usd < 0
        or not 0 < max_commission_fraction <= 1
        or absolute_cap < 1
    ):
        raise ValueError("Invalid economic-name-limit inputs")
    minimum_notional = commission_usd / max_commission_fraction
    if minimum_notional <= 0:
        return absolute_cap
    return max(1, min(absolute_cap, int(sector_weight * aum_usd // minimum_notional)))


def desired_rank_set(
    frame: pd.DataFrame,
    *,
    active_tickers: set[str],
    entry_fraction: float,
    exit_fraction: float,
    minimum_names: int,
    maximum_names: int,
) -> tuple[pd.DataFrame, dict[str, float]]:
    """Return an exact-size target with an incumbent retention buffer."""
    if not 0 < entry_fraction < exit_fraction <= 1:
        raise ValueError("Rank entry/exit fractions must satisfy 0 < entry < exit <= 1")
    clean = frame.dropna(subset=["score_z_pipeline_date"]).copy()
    clean["ticker"] = clean["ticker"].astype(str).str.upper().str.strip()
    clean = clean.sort_values(
        ["score_z_pipeline_date", "ticker"], ascending=[False, True]
    ).drop_duplicates("ticker", keep="first")
    if clean.empty:
        return clean, {}
    clean["rank_number"] = np.arange(1, len(clean) + 1)
    clean["rank_pct"] = clean["rank_number"] / len(clean)
    entry_count = min(
        len(clean),
        maximum_names,
        max(minimum_names, int(math.ceil(len(clean) * entry_fraction))),
    )
    exit_count = min(len(clean), int(math.ceil(len(clean) * exit_fraction)))
    rank_pct = dict(zip(clean["ticker"], clean["rank_pct"], strict=True))
    incumbent_rows = clean.loc[
        clean["ticker"].isin(sorted(active_tickers))
        & (clean["rank_number"] <= exit_count)
    ].head(entry_count)
    retained = set(incumbent_rows["ticker"])
    entrants = clean.loc[
        (clean["rank_number"] <= entry_count)
        & ~clean["ticker"].isin(sorted(retained))
    ].head(max(0, entry_count - len(incumbent_rows)))
    desired = pd.concat([incumbent_rows, entrants], ignore_index=True)
    return desired, rank_pct


def circular_block_mean_ci(
    values: list[float],
    *,
    block_length: int,
    confidence: float,
    replications: int,
    seed: int,
) -> tuple[float | None, float | None]:
    """Seeded circular-block confidence interval for a dependent sample mean."""
    data = np.asarray(values, dtype=float)
    data = data[np.isfinite(data)]
    if (
        len(data) < 2
        or block_length < 1
        or not 0 < confidence < 1
        or replications < 100
    ):
        return None, None
    block = min(int(block_length), len(data))
    blocks_needed = int(math.ceil(len(data) / block))
    rng = np.random.default_rng(seed)
    means = np.empty(replications, dtype=float)
    offsets = np.arange(block)
    for idx in range(replications):
        starts = rng.integers(0, len(data), size=blocks_needed)
        sample_index = ((starts[:, None] + offsets) % len(data)).reshape(-1)[: len(data)]
        means[idx] = float(data[sample_index].mean())
    tail = (1.0 - confidence) / 2.0
    return float(np.quantile(means, tail)), float(np.quantile(means, 1.0 - tail))


def effective_sample_size(values: list[float], *, max_lag: int) -> float:
    """Autocorrelation-adjusted effective count, reported rather than hard-gated."""
    data = np.asarray(values, dtype=float)
    data = data[np.isfinite(data)]
    if len(data) < 3:
        return float(len(data))
    centered = data - data.mean()
    variance = float(np.dot(centered, centered))
    if variance <= 0:
        return float(len(data))
    total = 0.0
    for lag in range(1, min(max_lag, len(data) - 1) + 1):
        rho = float(np.dot(centered[:-lag], centered[lag:]) / variance)
        if rho <= 0:
            break
        total += (1.0 - lag / len(data)) * rho
    return float(np.clip(len(data) / (1.0 + 2.0 * total), 1.0, len(data)))


def run_rank_reconstitution(
    *,
    panel: pd.DataFrame,
    prices: pd.DataFrame,
    opens: pd.DataFrame,
    calendar: list[str],
    signal_dates: list[str],
    pipelines: list[str],
    sector_etfs: dict[str, str],
    v1_labels: dict[str, str],
    h1_labels: dict[str, str],
    terminal_dates: dict[str, str],
    parameters: dict[str, Any],
    spread_resolver: Callable[[str, str], tuple[float, str]],
    stressed_spread: Callable[[float, str], float],
    commission_fraction: float,
    aum_usd: float,
    commission_usd: float,
    min_commission_fraction: float,
    absolute_name_cap: int,
) -> dict[str, Any]:
    """Replay one rank-policy arm using D-close decisions and D+1-open execution."""
    entry_fraction = float(parameters["entry_fraction"])
    exit_fraction = float(parameters["exit_fraction"])
    max_holding_days = int(parameters["max_holding_days"])
    max_holding_days_by_signal = {
        str(day): int(value)
        for day, value in (
            parameters.get("max_holding_days_by_signal", {}) or {}
        ).items()
    }
    regime_mode = str(parameters["regime_mode"])
    target_gross = float(parameters["target_long_gross"])
    max_weight = float(parameters["max_position_weight"])
    minimum_names = int(parameters["minimum_names_per_sector"])
    supportive = {
        str(value).strip().upper()
        for value in parameters.get("supportive_regimes", ["HEATING_UP"])
    }
    if regime_mode not in REGIME_MODES:
        raise ValueError(f"Unsupported regime mode: {regime_mode}")
    if not 0 < target_gross <= 1 or not 0 < max_weight <= target_gross:
        raise ValueError("Invalid gross or position cap")

    calendar_pos = {day: idx for idx, day in enumerate(calendar)}
    signal_set = set(signal_dates)
    panel_by_day = {
        day: frame
        for day, frame in panel.loc[
            panel["as_of_date"].isin(sorted(signal_set))
        ].groupby(
            "as_of_date", sort=False
        )
    }
    sector_weight = target_gross / max(1, len(pipelines))
    maximum_names = economic_name_limit(
        sector_weight=sector_weight,
        aum_usd=aum_usd,
        commission_usd=commission_usd,
        max_commission_fraction=min_commission_fraction,
        absolute_cap=absolute_name_cap,
    )
    active: dict[str, Position] = {}
    scheduled_entries: dict[str, list[dict[str, Any]]] = {}
    trades: list[dict[str, Any]] = []
    daily: list[dict[str, Any]] = []
    sector_alpha = {pipeline: 0.0 for pipeline in pipelines}
    sector_equal_weight_alpha = {pipeline: 0.0 for pipeline in pipelines}
    coverage = {
        pipeline: {
            "candidates": 0,
            "with_open": 0,
            "post_terminal_rows_excluded": 0,
            "signal_close_missing_rows_excluded": 0,
        }
        for pipeline in pipelines
    }
    coverage_detail: dict[tuple[str, str], dict[str, Any]] = {}
    fallback_dates = 0
    regime_signal_summary = {
        "signal_dates": 0,
        "v1_supportive_dates": 0,
        "h1_supportive_dates": 0,
        "h1_v1_label_disagreement_dates": 0,
        "h1_v1_gate_disagreement_dates": 0,
    }

    def price(frame: pd.DataFrame, day: str, ticker: str) -> float | None:
        if day not in frame.index or ticker not in frame.columns:
            return None
        try:
            value = float(frame.at[day, ticker])
        except (TypeError, ValueError):
            return None
        return value if np.isfinite(value) and value > 0 else None

    def transaction_cost(weight: float, half_spread_bps: float) -> float:
        return abs(weight) * half_spread_bps / 1e4 + commission_fraction

    first_signal = min(signal_dates)
    last_signal = max(signal_dates)
    start_idx = calendar_pos[first_signal] + 1
    end_idx = min(len(calendar), calendar_pos[last_signal] + max_holding_days + 2)
    for day in calendar[start_idx:end_idx]:
        gross_day = 0.0
        net_day = 0.0
        stress_day = 0.0
        selection_day = 0.0

        for ticker, position in list(active.items()):
            if position.pending_exit_reason is None:
                continue
            exit_open = price(opens, day, ticker)
            etf_open = price(opens, day, sector_etfs.get(position.pipeline, ""))
            if exit_open is None or etf_open is None:
                continue
            gross = position.current_exposure * (
                exit_open / position.previous_price - 1.0
            )
            benchmark = position.current_exposure * (
                etf_open / position.previous_benchmark_price - 1.0
            )
            position.gross_pnl += gross
            position.benchmark_pnl += benchmark
            position.current_exposure *= exit_open / position.previous_price
            spread, source = spread_resolver(day, ticker)
            exit_cost = transaction_cost(position.current_exposure, spread)
            stress_cost = transaction_cost(
                position.current_exposure, stressed_spread(spread, source)
            )
            position.transaction_cost += exit_cost
            position.stress_cost += stress_cost
            selection = (
                position.gross_pnl
                - position.benchmark_pnl
                - position.transaction_cost
            )
            signal_members = panel_by_day.get(
                position.signal_date, panel.iloc[0:0]
            )
            signal_members = signal_members.loc[
                signal_members["source_pipeline"] == position.pipeline, "ticker"
            ].astype(str)
            peer_returns = []
            for peer in signal_members:
                peer_entry = price(opens, position.entry_date, peer)
                peer_exit = price(opens, day, peer)
                if peer_entry is not None and peer_exit is not None:
                    peer_returns.append(peer_exit / peer_entry - 1.0)
            equal_weight_benchmark = (
                position.initial_weight * float(np.mean(peer_returns))
                if peer_returns
                else position.benchmark_pnl
            )
            equal_weight_selection = (
                position.gross_pnl
                - equal_weight_benchmark
                - position.transaction_cost
            )
            sector_alpha[position.pipeline] += selection
            sector_equal_weight_alpha[position.pipeline] += equal_weight_selection
            gross_day += gross
            net_day += gross - exit_cost
            stress_day += gross - stress_cost
            selection_day += gross - benchmark - exit_cost
            trades.append(
                {
                    "ticker": ticker,
                    "source_pipeline": position.pipeline,
                    "signal_date": position.signal_date,
                    "entry_date": position.entry_date,
                    "exit_date": day,
                    "entry_score_z": position.entry_score_z,
                    "entry_rank_pct": position.entry_rank_pct,
                    "entry_weight": position.initial_weight,
                    "holding_days": position.holding_days,
                    "exit_reason": position.pending_exit_reason,
                    "net_return": position.gross_pnl - position.transaction_cost,
                    "selection_alpha_net": selection,
                    "selection_alpha_equal_weight_net": equal_weight_selection,
                }
            )
            del active[ticker]

        entries = scheduled_entries.pop(day, [])
        grouped_entries: dict[str, list[dict[str, Any]]] = {}
        for row in entries:
            ticker = str(row["ticker"])
            pipeline = str(row["source_pipeline"])
            if ticker in active:
                continue
            coverage[pipeline]["candidates"] += 1
            detail = coverage_detail.setdefault(
                (pipeline, ticker),
                {
                    "source_pipeline": pipeline,
                    "ticker": ticker,
                    "candidate_entries": 0,
                    "candidate_entries_with_open": 0,
                    "missing_open_with_sealed_close": 0,
                    "missing_open_without_sealed_close": 0,
                    "execution_panel_has_ticker": int(ticker in opens.columns),
                    "first_missing_date": "",
                    "last_missing_date": "",
                },
            )
            detail["candidate_entries"] += 1
            if price(opens, day, ticker) is None:
                reason = (
                    "missing_open_with_sealed_close"
                    if price(prices, day, ticker) is not None
                    else "missing_open_without_sealed_close"
                )
                detail[reason] += 1
                detail["first_missing_date"] = detail["first_missing_date"] or day
                detail["last_missing_date"] = day
                continue
            coverage[pipeline]["with_open"] += 1
            detail["candidate_entries_with_open"] += 1
            grouped_entries.setdefault(pipeline, []).append(row)
        for pipeline, rows in sorted(grouped_entries.items()):
            current_sector = sum(
                position.current_exposure
                for position in active.values()
                if position.pipeline == pipeline
            )
            remaining = max(0.0, sector_weight - current_sector)
            weight = min(max_weight, remaining / max(1, len(rows)))
            etf_open = price(opens, day, sector_etfs.get(pipeline, ""))
            if weight <= 0 or etf_open is None:
                continue
            for row in rows:
                ticker = str(row["ticker"])
                entry_open = price(opens, day, ticker)
                if entry_open is None:
                    continue
                spread, source = spread_resolver(str(row["as_of_date"]), ticker)
                entry_cost = transaction_cost(weight, spread)
                stress_cost = transaction_cost(
                    weight, stressed_spread(spread, source)
                )
                active[ticker] = Position(
                    ticker=ticker,
                    pipeline=pipeline,
                    signal_date=str(row["as_of_date"]),
                    entry_date=day,
                    entry_price=entry_open,
                    previous_price=entry_open,
                    previous_benchmark_price=etf_open,
                    entry_score_z=float(row["score_z_pipeline_date"]),
                    entry_rank_pct=float(row["rank_pct"]),
                    initial_weight=weight,
                    current_exposure=weight,
                    max_holding_days=max_holding_days_by_signal.get(
                        str(row["as_of_date"]), max_holding_days
                    ),
                    transaction_cost=entry_cost,
                    stress_cost=stress_cost,
                )
                net_day -= entry_cost
                stress_day -= stress_cost
                selection_day -= entry_cost

        for ticker, position in list(active.items()):
            close = price(prices, day, ticker)
            etf_close = price(prices, day, sector_etfs.get(position.pipeline, ""))
            if close is None or etf_close is None:
                position.pending_exit_reason = (
                    position.pending_exit_reason or "execution_data_missing"
                )
                continue
            gross = position.current_exposure * (
                close / position.previous_price - 1.0
            )
            benchmark = position.current_exposure * (
                etf_close / position.previous_benchmark_price - 1.0
            )
            position.gross_pnl += gross
            position.benchmark_pnl += benchmark
            position.current_exposure *= close / position.previous_price
            position.previous_price = close
            position.previous_benchmark_price = etf_close
            position.holding_days += 1
            gross_day += gross
            net_day += gross
            stress_day += gross
            selection_day += gross - benchmark
            if (
                position.pending_exit_reason is None
                and position.holding_days >= position.max_holding_days
            ):
                position.pending_exit_reason = "time_stop"
            if terminal_dates.get(ticker) == day:
                signal_members = panel_by_day.get(
                    position.signal_date, panel.iloc[0:0]
                )
                signal_members = signal_members.loc[
                    signal_members["source_pipeline"] == position.pipeline, "ticker"
                ].astype(str)
                peer_returns = []
                for peer in signal_members:
                    peer_entry = price(opens, position.entry_date, peer)
                    peer_exit = price(prices, day, peer)
                    if peer_entry is not None and peer_exit is not None:
                        peer_returns.append(peer_exit / peer_entry - 1.0)
                equal_weight_benchmark = (
                    position.initial_weight * float(np.mean(peer_returns))
                    if peer_returns
                    else position.benchmark_pnl
                )
                selection = (
                    position.gross_pnl
                    - position.benchmark_pnl
                    - position.transaction_cost
                )
                equal_weight_selection = (
                    position.gross_pnl
                    - equal_weight_benchmark
                    - position.transaction_cost
                )
                sector_alpha[position.pipeline] += selection
                sector_equal_weight_alpha[position.pipeline] += (
                    equal_weight_selection
                )
                trades.append(
                    {
                        "ticker": ticker,
                        "source_pipeline": position.pipeline,
                        "signal_date": position.signal_date,
                        "entry_date": position.entry_date,
                        "exit_date": day,
                        "entry_score_z": position.entry_score_z,
                        "entry_rank_pct": position.entry_rank_pct,
                        "entry_weight": position.initial_weight,
                        "holding_days": position.holding_days,
                        "exit_reason": "terminal_event",
                        "net_return": (
                            position.gross_pnl - position.transaction_cost
                        ),
                        "selection_alpha_net": selection,
                        "selection_alpha_equal_weight_net": (
                            equal_weight_selection
                        ),
                    }
                )
                del active[ticker]

        if day in signal_set:
            day_frame = panel_by_day.get(day, panel.iloc[0:0])
            v1_label = v1_labels.get(day, "")
            h1_raw = h1_labels.get(day, "")
            h1_label = h1_raw or v1_label
            if regime_mode == "h1_gate" and not h1_raw:
                fallback_dates += 1
            v1_enabled = v1_label in supportive
            h1_enabled = h1_label in supportive
            regime_signal_summary["signal_dates"] += 1
            regime_signal_summary["v1_supportive_dates"] += int(v1_enabled)
            regime_signal_summary["h1_supportive_dates"] += int(h1_enabled)
            regime_signal_summary["h1_v1_label_disagreement_dates"] += int(
                bool(v1_label and h1_label and v1_label != h1_label)
            )
            regime_signal_summary["h1_v1_gate_disagreement_dates"] += int(
                v1_enabled != h1_enabled
            )
            label = (
                v1_label
                if regime_mode == "v1_gate"
                else h1_label
                if regime_mode == "h1_gate"
                else "UNCONDITIONAL"
            )
            enabled = regime_mode == "unconditional" or label in supportive
            next_pos = calendar_pos[day] + 1
            if next_pos < len(calendar):
                entry_day = calendar[next_pos]
                for pipeline in pipelines:
                    sector_frame = day_frame.loc[
                        day_frame["source_pipeline"] == pipeline
                    ].copy()
                    terminal_ok = sector_frame["ticker"].map(
                        lambda ticker: terminal_dates.get(
                            str(ticker), "9999-12-31"
                        )
                        > day
                    )
                    coverage[pipeline]["post_terminal_rows_excluded"] += int(
                        (~terminal_ok).sum()
                    )
                    sector_frame = sector_frame.loc[terminal_ok]
                    signal_close_available = sector_frame["ticker"].map(
                        lambda ticker: price(prices, day, str(ticker)) is not None
                    )
                    coverage[pipeline][
                        "signal_close_missing_rows_excluded"
                    ] += int((~signal_close_available).sum())
                    sector_frame = sector_frame.loc[signal_close_available]
                    sector_active = {
                        ticker
                        for ticker, position in active.items()
                        if position.pipeline == pipeline
                        and position.pending_exit_reason is None
                    }
                    if enabled:
                        desired, rank_pct = desired_rank_set(
                            sector_frame,
                            active_tickers=sector_active,
                            entry_fraction=entry_fraction,
                            exit_fraction=exit_fraction,
                            minimum_names=minimum_names,
                            maximum_names=maximum_names,
                        )
                    else:
                        desired, rank_pct = sector_frame.iloc[0:0], {}
                    desired_tickers = set(desired["ticker"])
                    for ticker in sector_active - desired_tickers:
                        active[ticker].pending_exit_reason = (
                            "regime_gate"
                            if not enabled
                            else "rank_buffer_breach"
                        )
                    for row in desired.to_dict("records"):
                        ticker = str(row["ticker"])
                        if ticker in active:
                            continue
                        row["rank_pct"] = rank_pct[ticker]
                        scheduled_entries.setdefault(entry_day, []).append(row)

        daily.append(
            {
                "date": day,
                "gross_return": gross_day,
                "net_return": net_day,
                "stress_net_return": stress_day,
                "selection_alpha_net": selection_day,
                "open_positions": len(active),
            }
        )

    if active:
        raise RuntimeError("Rank replay ended with open positions")
    coverage_rows = []
    for pipeline in pipelines:
        total = coverage[pipeline]["candidates"]
        present = coverage[pipeline]["with_open"]
        pipeline_detail = [
            row
            for (row_pipeline, _ticker), row in coverage_detail.items()
            if row_pipeline == pipeline
        ]
        unique_present = sum(
            int(row["candidate_entries_with_open"]) > 0 for row in pipeline_detail
        )
        unique_total = len(pipeline_detail)
        coverage_rows.append(
            {
                "source_pipeline": pipeline,
                "candidate_entries": total,
                "candidate_entries_with_open": present,
                "coverage_fraction": present / total if total else 1.0,
                "unique_candidate_tickers": unique_total,
                "unique_candidate_tickers_with_open": unique_present,
                "unique_coverage_fraction": (
                    unique_present / unique_total if unique_total else 1.0
                ),
                "missing_open_with_sealed_close": sum(
                    int(row["missing_open_with_sealed_close"])
                    for row in pipeline_detail
                ),
                "missing_open_without_sealed_close": sum(
                    int(row["missing_open_without_sealed_close"])
                    for row in pipeline_detail
                ),
                "missing_execution_history_tickers": sum(
                    int(row["execution_panel_has_ticker"]) == 0
                    for row in pipeline_detail
                ),
                "post_terminal_rows_excluded": coverage[pipeline][
                    "post_terminal_rows_excluded"
                ],
                "signal_close_missing_rows_excluded": coverage[pipeline][
                    "signal_close_missing_rows_excluded"
                ],
            }
        )
    return {
        "trades": trades,
        "daily": daily,
        "sector_selection_alpha": sector_alpha,
        "sector_equal_weight_selection_alpha": sector_equal_weight_alpha,
        "coverage_by_sector": coverage_rows,
        "coverage_detail": sorted(
            coverage_detail.values(),
            key=lambda row: (
                str(row["source_pipeline"]),
                str(row["ticker"]),
            ),
        ),
        "h1_fallback_signal_dates": fallback_dates,
        "regime_signal_summary": regime_signal_summary,
        "economic_max_names_per_sector": maximum_names,
    }


def selftest() -> None:
    frame = pd.DataFrame(
        {
            "ticker": list("ABCDE"),
            "score_z_pipeline_date": [5, 4, 3, 2, 1],
        }
    )
    desired, ranks = desired_rank_set(
        frame,
        active_tickers={"B"},
        entry_fraction=0.20,
        exit_fraction=0.40,
        minimum_names=1,
        maximum_names=10,
    )
    assert list(desired["ticker"]) == ["B"]
    assert ranks["A"] == 0.2 and ranks["B"] == 0.4
    desired2, _ = desired_rank_set(
        frame,
        active_tickers={"C"},
        entry_fraction=0.20,
        exit_fraction=0.40,
        minimum_names=1,
        maximum_names=10,
    )
    assert list(desired2["ticker"]) == ["A"]
    assert economic_name_limit(
        sector_weight=0.20,
        aum_usd=100_000,
        commission_usd=1.25,
        max_commission_fraction=0.005,
        absolute_cap=200,
    ) == 80
    low, high = circular_block_mean_ci(
        [0.01] * 20,
        block_length=5,
        confidence=0.90,
        replications=200,
        seed=7,
    )
    assert low is not None and high is not None and low > 0
    assert effective_sample_size([1.0, -1.0] * 20, max_lag=10) == 40.0
