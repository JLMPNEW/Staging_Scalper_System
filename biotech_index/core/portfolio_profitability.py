from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import dataclass
from datetime import date
from statistics import NormalDist, mean
from typing import Iterable, Mapping, Sequence

from biotech_index.core.calibration_metrics import finite_float, profit_factor, quantile


@dataclass(frozen=True)
class ReplayCostModel:
    initial_capital: float = 1_000_000.0
    base_one_way_cost_bps: float = 20.0
    benchmark_one_way_cost_bps: float = 2.0
    market_impact_coefficient_bps: float = 25.0
    max_market_impact_bps: float = 75.0
    max_adv_participation_pct: float = 2.0
    min_trade_notional: float = 25.0
    execution_lag_bars: int = 1
    periods_per_year: int = 252
    liquidate_at_end: bool = True
    # Zero preserves legacy replay manifests. Production calibration config
    # sets an explicit limit so truncated label windows cannot leave stale
    # stock targets in place until the end of an annual fold.
    max_target_staleness_bars: int = 0

    def __post_init__(self) -> None:
        if self.initial_capital <= 0.0:
            raise ValueError("initial_capital must be positive")
        for field in (
            "base_one_way_cost_bps",
            "benchmark_one_way_cost_bps",
            "market_impact_coefficient_bps",
            "max_market_impact_bps",
            "max_adv_participation_pct",
            "min_trade_notional",
        ):
            if float(getattr(self, field)) < 0.0:
                raise ValueError(f"{field} cannot be negative")
        if self.execution_lag_bars < 1:
            raise ValueError("execution_lag_bars must be at least one")
        if self.periods_per_year <= 0:
            raise ValueError("periods_per_year must be positive")
        if self.max_target_staleness_bars < 0:
            raise ValueError("max_target_staleness_bars cannot be negative")


@dataclass(frozen=True)
class ReplayTarget:
    signal_date: date
    weights: Mapping[str, float]
    adv_by_ticker: Mapping[str, float]


@dataclass(frozen=True)
class TerminalRecovery:
    terminal_date: date
    equity_recovery: float
    recovery_type: str
    drop_otc_tape: bool = True


@dataclass(frozen=True)
class ReplayResult:
    daily_rows: tuple[dict[str, object], ...]
    trade_rows: tuple[dict[str, object], ...]
    summary: Mapping[str, object]


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _clean_ticker(raw: object) -> str:
    return str(raw or "").strip().upper()


def normalized_target_weights(
    raw_weights: Mapping[str, object],
    *,
    benchmark_ticker: str,
) -> dict[str, float]:
    benchmark = _clean_ticker(benchmark_ticker)
    if not benchmark:
        raise ValueError("benchmark_ticker cannot be blank")
    clean: dict[str, float] = {}
    for raw_ticker, raw_weight in raw_weights.items():
        ticker = _clean_ticker(raw_ticker)
        weight = finite_float(raw_weight)
        if not ticker or weight is None or weight <= 0.0:
            continue
        clean[ticker] = clean.get(ticker, 0.0) + weight
    stock_weight = sum(weight for ticker, weight in clean.items() if ticker != benchmark)
    if stock_weight > 1.0 + 1e-9:
        scale = 1.0 / stock_weight
        clean = {
            ticker: (weight * scale if ticker != benchmark else weight)
            for ticker, weight in clean.items()
        }
        stock_weight = 1.0
    clean[benchmark] = max(0.0, 1.0 - stock_weight)
    total = sum(clean.values())
    if total <= 0.0:
        return {benchmark: 1.0}
    return {ticker: weight / total for ticker, weight in sorted(clean.items()) if weight > 1e-12}


def targets_from_selection_rows(
    selection_rows: Iterable[Mapping[str, object]],
    evaluation_dates: Iterable[object],
    *,
    active_weight_by_date: Mapping[str, object],
    benchmark_ticker: str,
    adv_lookup: Mapping[tuple[str, str], object] | None = None,
    target_weight_field: str | None = None,
) -> list[ReplayTarget]:
    grouped: dict[str, list[tuple[str, float | None]]] = {}
    for row in selection_rows:
        asof_date = str(row.get("asof_date") or "").strip()
        ticker = _clean_ticker(row.get("ticker"))
        if asof_date and ticker:
            explicit_weight = None
            if target_weight_field is not None:
                explicit_weight = finite_float(row.get(target_weight_field))
                if explicit_weight is None or explicit_weight < 0.0:
                    raise ValueError(
                        f"Invalid {target_weight_field} for {ticker} on {asof_date}: "
                        f"{row.get(target_weight_field)!r}"
                    )
            grouped.setdefault(asof_date, []).append((ticker, explicit_weight))
    lookup = adv_lookup or {}
    targets: list[ReplayTarget] = []
    for raw_date in sorted({str(value or "").strip() for value in evaluation_dates if str(value or "").strip()}):
        try:
            signal_date = date.fromisoformat(raw_date)
        except ValueError as exc:
            raise ValueError(f"Invalid replay signal date: {raw_date!r}") from exc
        date_rows = grouped.get(raw_date, [])
        tickers = sorted({ticker for ticker, _weight in date_rows})
        if target_weight_field is None:
            raw_active_weight = finite_float(active_weight_by_date.get(raw_date))
            active_weight = _clamp(
                0.0 if raw_active_weight is None else raw_active_weight,
                0.0,
                1.0,
            )
            weights = (
                {ticker: active_weight / len(tickers) for ticker in tickers}
                if tickers and active_weight > 0.0
                else {}
            )
        else:
            weights: dict[str, float] = {}
            for ticker, explicit_weight in date_rows:
                if ticker in weights:
                    raise ValueError(
                        f"Duplicate explicit target weight for {ticker} on {raw_date}"
                    )
                if explicit_weight is not None and explicit_weight > 0.0:
                    weights[ticker] = explicit_weight
            stock_weight = sum(weights.values())
            if stock_weight > 1.0 + 1e-9:
                raise ValueError(
                    f"Explicit stock target weights sum to {stock_weight:.12f} on {raw_date}; expected <= 1.0"
                )
        adv = {
            ticker: value
            for ticker in tickers
            if (value := finite_float(lookup.get((raw_date, ticker)))) is not None and value > 0.0
        }
        targets.append(
            ReplayTarget(
                signal_date=signal_date,
                weights=normalized_target_weights(weights, benchmark_ticker=benchmark_ticker),
                adv_by_ticker=adv,
            )
        )
    return targets


def target_allocations_equal(
    left: Sequence[ReplayTarget],
    right: Sequence[ReplayTarget],
    *,
    tolerance: float = 1e-12,
) -> bool:
    """Return whether two frozen target schedules express the same allocations."""
    if len(left) != len(right):
        return False
    for left_target, right_target in zip(
        sorted(left, key=lambda value: value.signal_date),
        sorted(right, key=lambda value: value.signal_date),
        strict=True,
    ):
        if left_target.signal_date != right_target.signal_date:
            return False
        tickers = set(left_target.weights).union(right_target.weights)
        if any(
            abs(
                float(left_target.weights.get(ticker, 0.0))
                - float(right_target.weights.get(ticker, 0.0))
            )
            > tolerance
            for ticker in tickers
        ):
            return False
    return True


def _execution_schedule(
    targets: Sequence[ReplayTarget],
    trading_days: Sequence[date],
    lag_bars: int,
) -> dict[date, ReplayTarget]:
    schedule: dict[date, ReplayTarget] = {}
    ordered_days = list(trading_days)
    for target in sorted(targets, key=lambda value: value.signal_date):
        later = [index for index, day in enumerate(ordered_days) if day > target.signal_date]
        if not later:
            continue
        execution_index = later[0] + max(0, lag_bars - 1)
        if execution_index < len(ordered_days):
            schedule[ordered_days[execution_index]] = target
    return schedule


def _trade_cost_bps(
    ticker: str,
    notional: float,
    adv: float | None,
    *,
    benchmark_ticker: str,
    model: ReplayCostModel,
) -> tuple[float, float, bool]:
    if ticker == benchmark_ticker:
        return model.benchmark_one_way_cost_bps, 0.0, False
    if adv is None or adv <= 0.0:
        return model.base_one_way_cost_bps, 0.0, True
    participation = max(0.0, notional / adv)
    impact = min(
        model.max_market_impact_bps,
        model.market_impact_coefficient_bps * math.sqrt(participation),
    )
    return model.base_one_way_cost_bps + impact, participation, False


def _max_trade_notional(ticker: str, adv: float | None, model: ReplayCostModel, benchmark: str) -> float:
    if ticker == benchmark or adv is None or adv <= 0.0 or model.max_adv_participation_pct <= 0.0:
        return math.inf
    return adv * model.max_adv_participation_pct / 100.0


def _series_hash(rows: Iterable[Mapping[str, object]]) -> str:
    payload = json.dumps(list(rows), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def run_daily_portfolio_replay(
    prices: Mapping[str, Mapping[date, float]],
    targets: Sequence[ReplayTarget],
    *,
    benchmark_ticker: str,
    model: ReplayCostModel,
    terminal_events: Mapping[str, TerminalRecovery] | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
) -> ReplayResult:
    benchmark = _clean_ticker(benchmark_ticker)
    benchmark_prices = prices.get(benchmark, {})
    trading_days = sorted(
        day
        for day, close in benchmark_prices.items()
        if close > 0.0
        and (start_date is None or day >= start_date)
        and (end_date is None or day <= end_date)
    )
    if len(trading_days) < 2:
        raise ValueError("Portfolio replay requires at least two benchmark trading days")
    schedule = _execution_schedule(targets, trading_days, model.execution_lag_bars)
    if not schedule:
        raise ValueError("Portfolio replay has no executable target dates")
    events = {_clean_ticker(key): value for key, value in (terminal_events or {}).items()}
    holdings: dict[str, float] = {}
    marks: dict[str, float] = {}
    cash = model.initial_capital
    previous_equity = model.initial_capital
    total_cost = 0.0
    total_notional = 0.0
    missing_adv_trade_count = 0
    missing_target_price_count = 0
    partial_fill_count = 0
    processed_terminal: set[str] = set()
    last_explicit_target_index: int | None = None
    last_explicit_target_adv: dict[str, float] = {}
    target_expired = False
    target_expiry_rebalance_count = 0
    daily_rows: list[dict[str, object]] = []
    trade_rows: list[dict[str, object]] = []

    for day_index, day in enumerate(trading_days):
        for ticker in set(holdings).union(prices):
            event = events.get(ticker)
            if event is not None and event.drop_otc_tape and day > event.terminal_date:
                continue
            close = finite_float(prices.get(ticker, {}).get(day))
            if close is not None and close > 0.0:
                marks[ticker] = close
        for ticker in sorted(set(holdings).intersection(events)):
            event = events[ticker]
            if ticker in processed_terminal or day < event.terminal_date:
                continue
            shares = holdings.pop(ticker, 0.0)
            proceeds = max(0.0, shares * event.equity_recovery)
            cash += proceeds
            processed_terminal.add(ticker)
            trade_rows.append(
                {
                    "trade_date": day.isoformat(),
                    "signal_date": "",
                    "ticker": ticker,
                    "side": "terminal_resolution",
                    "shares": -shares,
                    "price": event.equity_recovery,
                    "notional": proceeds,
                    "cost": 0.0,
                    "cost_bps": 0.0,
                    "adv": "",
                    "participation_pct": "",
                    "partial_fill_flag": 0,
                    "missing_adv_flag": 0,
                    "recovery_type": event.recovery_type,
                }
            )

        pretrade_equity = cash + sum(shares * marks.get(ticker, 0.0) for ticker, shares in holdings.items())
        target = schedule.get(day)
        target_expiry_rebalance = False
        if target is not None:
            last_explicit_target_index = day_index
            last_explicit_target_adv = dict(target.adv_by_ticker)
            target_expired = False
        elif (
            model.max_target_staleness_bars > 0
            and last_explicit_target_index is not None
            and day_index - last_explicit_target_index >= model.max_target_staleness_bars
        ):
            target = ReplayTarget(day, {benchmark: 1.0}, last_explicit_target_adv)
            if not target_expired:
                target_expired = True
                target_expiry_rebalance = True
                target_expiry_rebalance_count += 1
        day_cost = 0.0
        day_notional = 0.0
        if target is not None and pretrade_equity > 0.0:
            target_weights = dict(target.weights)
            missing_price_weight = sum(
                weight
                for ticker, weight in target_weights.items()
                if finite_float(prices.get(ticker, {}).get(day)) is None
                or float(prices.get(ticker, {}).get(day, 0.0)) <= 0.0
            )
            if missing_price_weight > 0.0:
                missing_target_price_count += sum(
                    1
                    for ticker, weight in target_weights.items()
                    if weight > 0.0
                    and (
                        finite_float(prices.get(ticker, {}).get(day)) is None
                        or float(prices.get(ticker, {}).get(day, 0.0)) <= 0.0
                    )
                )
                target_weights = {
                    ticker: weight
                    for ticker, weight in target_weights.items()
                    if finite_float(prices.get(ticker, {}).get(day)) is not None
                    and float(prices.get(ticker, {}).get(day, 0.0)) > 0.0
                }
                target_weights[benchmark] = target_weights.get(benchmark, 0.0) + missing_price_weight
                target_weights = normalized_target_weights(target_weights, benchmark_ticker=benchmark)

            desired_values = {ticker: pretrade_equity * weight for ticker, weight in target_weights.items()}
            all_tickers = sorted(set(holdings).union(desired_values))
            requested: list[tuple[str, float, float, float | None]] = []
            for ticker in all_tickers:
                price = finite_float(prices.get(ticker, {}).get(day))
                if price is None or price <= 0.0:
                    missing_target_price_count += 1
                    continue
                current_value = holdings.get(ticker, 0.0) * price
                adv = finite_float(target.adv_by_ticker.get(ticker))
                requested.append((ticker, desired_values.get(ticker, 0.0) - current_value, price, adv))

            for ticker, requested_notional, price, adv in sorted(requested, key=lambda item: item[1]):
                if requested_notional >= -model.min_trade_notional:
                    continue
                max_notional = _max_trade_notional(ticker, adv, model, benchmark)
                executed_notional = -min(abs(requested_notional), max_notional)
                partial = abs(executed_notional) + 1e-9 < abs(requested_notional)
                shares = executed_notional / price
                cost_bps, participation, missing_adv = _trade_cost_bps(
                    ticker,
                    abs(executed_notional),
                    adv,
                    benchmark_ticker=benchmark,
                    model=model,
                )
                cost = abs(executed_notional) * cost_bps / 10_000.0
                holdings[ticker] = holdings.get(ticker, 0.0) + shares
                if abs(holdings[ticker]) <= 1e-12:
                    holdings.pop(ticker, None)
                cash -= executed_notional + cost
                day_cost += cost
                day_notional += abs(executed_notional)
                missing_adv_trade_count += int(missing_adv)
                partial_fill_count += int(partial)
                trade_rows.append(
                    {
                        "trade_date": day.isoformat(),
                        "signal_date": target.signal_date.isoformat(),
                        "ticker": ticker,
                        "side": "sell",
                        "shares": shares,
                        "price": price,
                        "notional": abs(executed_notional),
                        "cost": cost,
                        "cost_bps": cost_bps,
                        "adv": "" if adv is None else adv,
                        "participation_pct": "" if adv is None else 100.0 * participation,
                        "partial_fill_flag": int(partial),
                        "missing_adv_flag": int(missing_adv),
                        "recovery_type": "",
                    }
                )

            for ticker, requested_notional, price, adv in sorted(requested, key=lambda item: item[1], reverse=True):
                if requested_notional <= model.min_trade_notional or cash <= model.min_trade_notional:
                    continue
                max_notional = _max_trade_notional(ticker, adv, model, benchmark)
                desired_notional = min(requested_notional, max_notional)
                cost_bps, participation, missing_adv = _trade_cost_bps(
                    ticker,
                    desired_notional,
                    adv,
                    benchmark_ticker=benchmark,
                    model=model,
                )
                affordable = cash / (1.0 + cost_bps / 10_000.0)
                executed_notional = min(desired_notional, affordable)
                if executed_notional < model.min_trade_notional:
                    continue
                partial = executed_notional + 1e-9 < requested_notional
                shares = executed_notional / price
                cost = executed_notional * cost_bps / 10_000.0
                holdings[ticker] = holdings.get(ticker, 0.0) + shares
                cash -= executed_notional + cost
                day_cost += cost
                day_notional += executed_notional
                missing_adv_trade_count += int(missing_adv)
                partial_fill_count += int(partial)
                trade_rows.append(
                    {
                        "trade_date": day.isoformat(),
                        "signal_date": target.signal_date.isoformat(),
                        "ticker": ticker,
                        "side": "buy",
                        "shares": shares,
                        "price": price,
                        "notional": executed_notional,
                        "cost": cost,
                        "cost_bps": cost_bps,
                        "adv": "" if adv is None else adv,
                        "participation_pct": "" if adv is None else 100.0 * participation,
                        "partial_fill_flag": int(partial),
                        "missing_adv_flag": int(missing_adv),
                        "recovery_type": "",
                    }
                )

        is_last = day_index == len(trading_days) - 1
        if is_last and model.liquidate_at_end and holdings:
            for ticker in sorted(list(holdings)):
                price = marks.get(ticker)
                if price is None or price <= 0.0:
                    continue
                shares_held = holdings.pop(ticker)
                notional = max(0.0, shares_held * price)
                cost_bps, participation, missing_adv = _trade_cost_bps(
                    ticker,
                    notional,
                    None,
                    benchmark_ticker=benchmark,
                    model=model,
                )
                cost = notional * cost_bps / 10_000.0
                cash += notional - cost
                day_cost += cost
                day_notional += notional
                missing_adv_trade_count += int(missing_adv)
                trade_rows.append(
                    {
                        "trade_date": day.isoformat(),
                        "signal_date": "",
                        "ticker": ticker,
                        "side": "fold_end_liquidation",
                        "shares": -shares_held,
                        "price": price,
                        "notional": notional,
                        "cost": cost,
                        "cost_bps": cost_bps,
                        "adv": "",
                        "participation_pct": "" if missing_adv else 100.0 * participation,
                        "partial_fill_flag": 0,
                        "missing_adv_flag": int(missing_adv),
                        "recovery_type": "",
                    }
                )

        equity = cash + sum(shares * marks.get(ticker, 0.0) for ticker, shares in holdings.items())
        daily_return = equity / previous_equity - 1.0 if previous_equity > 0.0 else -1.0
        total_cost += day_cost
        total_notional += day_notional
        gross_exposure = sum(abs(shares * marks.get(ticker, 0.0)) for ticker, shares in holdings.items())
        daily_rows.append(
            {
                "date": day.isoformat(),
                "equity": equity,
                "cash": cash,
                "daily_net_return": daily_return,
                "daily_cost": day_cost,
                "daily_traded_notional": day_notional,
                "gross_exposure_pct": 100.0 * gross_exposure / equity if equity > 0.0 else 0.0,
                "holding_count": len(holdings),
                "rebalance_flag": int(target is not None),
                "target_expiry_rebalance_flag": int(target_expiry_rebalance),
            }
        )
        previous_equity = equity

    summary = summarize_daily_replay(
        daily_rows,
        initial_capital=model.initial_capital,
        periods_per_year=model.periods_per_year,
    )
    summary.update(
        {
            "total_transaction_cost": round(total_cost, 6),
            "total_transaction_cost_pct_initial": round(100.0 * total_cost / model.initial_capital, 6),
            "gross_traded_notional": round(total_notional, 6),
            "gross_turnover_multiple": round(total_notional / model.initial_capital, 6),
            "trade_count": len(trade_rows),
            "partial_fill_count": partial_fill_count,
            "missing_adv_trade_count": missing_adv_trade_count,
            "missing_target_price_count": missing_target_price_count,
            "target_expiry_rebalance_count": target_expiry_rebalance_count,
            "daily_series_sha256": _series_hash(daily_rows),
            "trade_ledger_sha256": _series_hash(trade_rows),
        }
    )
    return ReplayResult(tuple(daily_rows), tuple(trade_rows), summary)


def _stdev(values: Sequence[float]) -> float | None:
    if len(values) < 2:
        return None
    avg = mean(values)
    return math.sqrt(sum((value - avg) ** 2 for value in values) / (len(values) - 1))


def _max_drawdown_from_equity(equity: Sequence[float]) -> float | None:
    if not equity:
        return None
    peak = equity[0]
    worst = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0.0:
            worst = min(worst, value / peak - 1.0)
    return worst


def summarize_daily_replay(
    daily_rows: Sequence[Mapping[str, object]],
    *,
    initial_capital: float,
    periods_per_year: int = 252,
) -> dict[str, object]:
    returns = [value for row in daily_rows if (value := finite_float(row.get("daily_net_return"))) is not None]
    equity = [value for row in daily_rows if (value := finite_float(row.get("equity"))) is not None]
    if not returns or not equity:
        raise ValueError("Cannot summarize an empty daily replay")
    terminal_wealth = equity[-1]
    total_return = terminal_wealth / initial_capital - 1.0
    elapsed_periods = max(1, len(returns) - 1)
    cagr = (terminal_wealth / initial_capital) ** (periods_per_year / elapsed_periods) - 1.0
    sigma = _stdev(returns)
    annual_vol = None if sigma is None else sigma * math.sqrt(periods_per_year)
    annual_return = mean(returns) * periods_per_year
    sharpe = None if annual_vol is None or annual_vol <= 1e-12 else annual_return / annual_vol
    downside = math.sqrt(mean([min(0.0, value) ** 2 for value in returns])) * math.sqrt(periods_per_year)
    sortino = None if downside <= 1e-12 else annual_return / downside
    max_drawdown = _max_drawdown_from_equity(equity)
    calmar = None if max_drawdown is None or max_drawdown >= -1e-12 else cagr / abs(max_drawdown)
    cutoff = quantile(returns, 0.05)
    tail = [value for value in returns if cutoff is not None and value <= cutoff]
    pf = profit_factor(returns, cap=10.0, min_wins=3, min_losses=3)
    gross_positive_return = sum(value for value in returns if value > 0.0)
    gross_negative_return = -sum(value for value in returns if value < 0.0)
    previous_equity = initial_capital
    dollar_changes: list[float] = []
    for value in equity:
        dollar_changes.append(value - previous_equity)
        previous_equity = value
    gross_profit_dollars = sum(value for value in dollar_changes if value > 0.0)
    gross_loss_dollars = -sum(value for value in dollar_changes if value < 0.0)
    dollar_profit_factor = (
        None if gross_loss_dollars <= 1e-12 else gross_profit_dollars / gross_loss_dollars
    )
    wealth_reconciliation_error = terminal_wealth - initial_capital - sum(dollar_changes)
    return {
        "daily_count": len(returns),
        "start_date": str(daily_rows[0].get("date") or ""),
        "end_date": str(daily_rows[-1].get("date") or ""),
        "initial_capital": round(initial_capital, 6),
        "terminal_wealth": round(terminal_wealth, 6),
        "net_profit": round(terminal_wealth - initial_capital, 6),
        "total_return_pct": round(100.0 * total_return, 6),
        "cagr_pct": round(100.0 * cagr, 6),
        "annualized_mean_return_pct": round(100.0 * annual_return, 6),
        "annualized_volatility_pct": "" if annual_vol is None else round(100.0 * annual_vol, 6),
        "sharpe_ratio": "" if sharpe is None else round(sharpe, 6),
        "sortino_ratio": "" if sortino is None else round(sortino, 6),
        "calmar_ratio": "" if calmar is None else round(calmar, 6),
        "profit_factor": "" if pf is None else round(pf, 6),
        "profit_factor_basis": "daily_net_return_gain_loss_ratio",
        "gross_positive_daily_return_sum": round(gross_positive_return, 10),
        "gross_negative_daily_return_sum_abs": round(gross_negative_return, 10),
        "dollar_pnl_profit_factor": (
            "" if dollar_profit_factor is None else round(dollar_profit_factor, 6)
        ),
        "gross_profit_dollars": round(gross_profit_dollars, 6),
        "gross_loss_dollars": round(gross_loss_dollars, 6),
        "wealth_reconciliation_error": round(wealth_reconciliation_error, 10),
        "max_drawdown_pct": "" if max_drawdown is None else round(100.0 * max_drawdown, 6),
        "daily_cvar_5pct": "" if not tail else round(100.0 * mean(tail), 6),
        "positive_day_rate_pct": round(100.0 * sum(value > 0.0 for value in returns) / len(returns), 6),
    }


def deflated_sharpe_probability(
    returns: Sequence[float],
    *,
    effective_trials: int,
) -> float | None:
    clean = [value for value in returns if math.isfinite(value)]
    if len(clean) < 3:
        return None
    sigma = _stdev(clean)
    if sigma is None or sigma <= 1e-12:
        return None
    avg = mean(clean)
    sharpe = avg / sigma
    centered = [(value - avg) / sigma for value in clean]
    skew = mean([value**3 for value in centered])
    kurtosis = mean([value**4 for value in centered])
    trials = max(1, int(effective_trials))
    normal = NormalDist()
    gamma = 0.5772156649015329
    sigma_sr = 1.0 / math.sqrt(max(1, len(clean) - 1))
    if trials == 1:
        hurdle = 0.0
    else:
        hurdle = sigma_sr * (
            (1.0 - gamma) * normal.inv_cdf(1.0 - 1.0 / trials)
            + gamma * normal.inv_cdf(1.0 - 1.0 / (trials * math.e))
        )
    denominator_sq = 1.0 - skew * sharpe + ((kurtosis - 1.0) / 4.0) * sharpe * sharpe
    if denominator_sq <= 1e-12:
        return None
    statistic = (sharpe - hurdle) * math.sqrt(len(clean) - 1) / math.sqrt(denominator_sq)
    return _clamp(normal.cdf(statistic), 0.0, 1.0)


def paired_daily_bootstrap_lcb(
    candidate_returns: Sequence[float],
    incumbent_returns: Sequence[float],
    *,
    iterations: int,
    block_days: int,
    seed: int,
    periods_per_year: int = 252,
) -> float | None:
    if len(candidate_returns) != len(incumbent_returns) or not candidate_returns:
        return None
    deltas = [left - right for left, right in zip(candidate_returns, incumbent_returns)]
    block = max(1, min(int(block_days), len(deltas)))
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(max(0, int(iterations))):
        sampled: list[float] = []
        while len(sampled) < len(deltas):
            start = rng.randrange(len(deltas))
            sampled.extend(deltas[(start + offset) % len(deltas)] for offset in range(block))
        means.append(mean(sampled[: len(deltas)]) * periods_per_year)
    return quantile(means, 0.05)


def compare_daily_replays(
    candidate: ReplayResult,
    incumbent: ReplayResult,
    *,
    effective_trials: int,
    bootstrap_iterations: int = 500,
    bootstrap_block_days: int = 20,
    bootstrap_seed: int = 1729,
    periods_per_year: int = 252,
) -> dict[str, object]:
    candidate_by_date = {
        str(row.get("date") or ""): value
        for row in candidate.daily_rows
        if (value := finite_float(row.get("daily_net_return"))) is not None
    }
    incumbent_by_date = {
        str(row.get("date") or ""): value
        for row in incumbent.daily_rows
        if (value := finite_float(row.get("daily_net_return"))) is not None
    }
    common = sorted(set(candidate_by_date).intersection(incumbent_by_date))
    candidate_returns = [candidate_by_date[value] for value in common]
    incumbent_returns = [incumbent_by_date[value] for value in common]
    delta_lcb = paired_daily_bootstrap_lcb(
        candidate_returns,
        incumbent_returns,
        iterations=bootstrap_iterations,
        block_days=bootstrap_block_days,
        seed=bootstrap_seed,
        periods_per_year=periods_per_year,
    )
    candidate_dsr = deflated_sharpe_probability(candidate_returns, effective_trials=effective_trials)
    incumbent_dsr = deflated_sharpe_probability(incumbent_returns, effective_trials=1)
    output: dict[str, object] = {
        "paired_daily_count": len(common),
        "paired_daily_start_date": common[0] if common else "",
        "paired_daily_end_date": common[-1] if common else "",
        "effective_trial_count": max(1, int(effective_trials)),
        "paired_annualized_delta_bootstrap_lcb_pct": (
            "" if delta_lcb is None else round(100.0 * delta_lcb, 6)
        ),
        "candidate_deflated_sharpe_probability": (
            "" if candidate_dsr is None else round(candidate_dsr, 6)
        ),
        "incumbent_deflated_sharpe_probability": (
            "" if incumbent_dsr is None else round(incumbent_dsr, 6)
        ),
    }
    for prefix, summary in (("candidate", candidate.summary), ("incumbent", incumbent.summary)):
        output.update({f"{prefix}_{key}": value for key, value in summary.items()})
    for key in (
        "terminal_wealth",
        "net_profit",
        "total_return_pct",
        "cagr_pct",
        "annualized_mean_return_pct",
        "sharpe_ratio",
        "sortino_ratio",
        "calmar_ratio",
        "profit_factor",
        "dollar_pnl_profit_factor",
        "gross_profit_dollars",
        "gross_loss_dollars",
        "wealth_reconciliation_error",
        "max_drawdown_pct",
        "daily_cvar_5pct",
        "total_transaction_cost",
        "gross_turnover_multiple",
        "target_expiry_rebalance_count",
    ):
        left = finite_float(candidate.summary.get(key))
        right = finite_float(incumbent.summary.get(key))
        output[f"delta_{key}"] = "" if left is None or right is None else round(left - right, 6)
    return output
