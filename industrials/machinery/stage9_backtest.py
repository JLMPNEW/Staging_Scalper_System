from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from industrials.core.config import cfg_get
from industrials.core.reports import write_csv_atomic
from industrials.machinery.production_universe import (
    ALL_RANK_READY_UNIVERSE_POLICY,
    configured_universe_policy,
    production_universe_eligible,
)
from industrials.machinery.scoring import file_sha256, write_json_atomic
from industrials.machinery.stage8_calibration import (
    COMPONENT_FIELDS,
    as_float,
    mean,
    parse_date,
    read_csv_rows,
    stage8_paths,
    stdev,
    utc_now,
    validate_stage8,
)


MODEL_FAMILY = "machinery"
CONFIG_KEY = "machinery_stage9"
PRODUCTION_SELECTION_POLICY_VERSION = "machinery_stage9_selection_v2"
PERIOD_FIELDS = (
    "model",
    "variant",
    "portfolio_type",
    "weighting",
    "quantile",
    "horizon_days",
    "split_name",
    "asof_date",
    "forward_date",
    "universe_count",
    "position_count",
    "long_count",
    "short_count",
    "gross_exposure",
    "net_exposure",
    "one_way_turnover",
    "traded_notional_fraction",
    "transaction_cost",
    "borrow_cost",
    "gross_return",
    "net_return",
    "benchmark_return",
    "net_excess_return",
    "max_position_weight",
    "max_cohort_share",
    "adv_weight_coverage",
    "trade_adv_coverage",
    "capacity_usd",
    "selected_tickers",
)
HOLDING_FIELDS = (
    "model",
    "variant",
    "horizon_days",
    "split_name",
    "asof_date",
    "forward_date",
    "ticker",
    "side",
    "calibration_cohort",
    "score",
    "weight",
    "previous_weight",
    "trade_weight",
    "forward_return",
    "gross_return_contribution",
    "latest_borrow_fee_rate",
    "borrow_rate_source",
    "borrow_cost_contribution",
    "avg_dollar_volume_60d",
    "trade_capacity_usd",
)
SUMMARY_FIELDS = (
    "model",
    "variant",
    "portfolio_type",
    "weighting",
    "quantile",
    "horizon_days",
    "split_name",
    "period_count",
    "mean_net_return",
    "mean_benchmark_return",
    "mean_net_excess_return",
    "annualized_net_return",
    "annualized_benchmark_return",
    "annualized_excess_return",
    "annualized_volatility",
    "information_ratio",
    "hit_rate_vs_benchmark",
    "max_drawdown",
    "average_one_way_turnover",
    "average_transaction_cost",
    "average_borrow_cost",
    "average_max_position_weight",
    "worst_max_position_weight",
    "average_max_cohort_share",
    "worst_max_cohort_share",
    "average_adv_weight_coverage",
    "average_trade_adv_coverage",
    "capacity_p10_usd",
    "capacity_median_usd",
    "cohort_count",
)
PARITY_FIELDS = (
    "selection_policy_version",
    "model",
    "variant",
    "horizon_days",
    "split_name",
    "asof_date",
    "expected_position_count",
    "actual_position_count",
    "membership_match",
    "expected_weight_sum",
    "actual_weight_sum",
    "maximum_absolute_weight_difference",
    "weight_tolerance",
    "expected_tickers",
    "actual_tickers",
    "parity_status",
)


@dataclass(frozen=True)
class StrategySpec:
    name: str
    portfolio_type: str
    weighting: str
    quantile: float


@dataclass(frozen=True)
class Stage9Paths:
    root: Path
    periods_csv: Path
    holdings_csv: Path
    summary_csv: Path
    parity_csv: Path
    acceptance_json: Path
    run_manifest_json: Path
    validation_csv: Path
    validation_json: Path


def stage9_paths(root: Path) -> Stage9Paths:
    return Stage9Paths(
        root=root,
        periods_csv=root / "machinery_stage9_periods.csv",
        holdings_csv=root / "machinery_stage9_holdings.csv",
        summary_csv=root / "machinery_stage9_summary.csv",
        parity_csv=root / "machinery_stage9_production_policy_parity.csv",
        acceptance_json=root / "machinery_stage9_acceptance.json",
        run_manifest_json=root / "machinery_stage9_run_manifest.json",
        validation_csv=root / "machinery_stage9_validation.csv",
        validation_json=root / "machinery_stage9_validation.json",
    )


def stage9_config_sha256(config: dict[str, Any]) -> str:
    encoded = json.dumps(
        cfg_get(config, CONFIG_KEY, {}),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def strategy_specs(config: dict[str, Any]) -> tuple[StrategySpec, ...]:
    quantiles = [
        float(value)
        for value in cfg_get(config, f"{CONFIG_KEY}.quantiles", [0.10, 0.20])
    ]
    if any(value <= 0 or value >= 0.50 for value in quantiles):
        raise ValueError("Stage 9 quantiles must be greater than 0 and below 0.50")
    output: list[StrategySpec] = []
    for quantile in quantiles:
        label = f"q{int(round(quantile * 100)):02d}"
        for portfolio_type in ("long_only", "long_short"):
            for weighting in ("equal", "score"):
                output.append(
                    StrategySpec(
                        name=f"{portfolio_type}_{label}_{weighting}",
                        portfolio_type=portfolio_type,
                        weighting=weighting,
                        quantile=quantile,
                    )
                )
    return tuple(output)


def strategy_spec_by_name(
    config: dict[str, Any],
    name: str,
) -> StrategySpec:
    matches = [spec for spec in strategy_specs(config) if spec.name == name]
    if len(matches) != 1:
        raise ValueError(
            f"Expected exactly one Stage 9 strategy named {name!r}; "
            f"found={len(matches)}"
        )
    return matches[0]


def component_score(
    row: Mapping[str, str],
    weights: Mapping[str, float],
) -> float | None:
    total = 0.0
    for field in COMPONENT_FIELDS:
        value = as_float(row.get(field))
        if value is None:
            return None
        total += value * float(weights[field])
    return total


def production_universe_policy(config: Mapping[str, Any]) -> str:
    return configured_universe_policy(
        config,
        config_key=CONFIG_KEY,
    )


def _normalize(raw: Mapping[str, float], target: float) -> dict[str, float]:
    total = sum(max(0.0, value) for value in raw.values())
    if total <= 0:
        return {key: target / len(raw) for key in raw} if raw else {}
    return {
        key: target * max(0.0, value) / total
        for key, value in raw.items()
    }


def portfolio_weights(
    scored: Sequence[tuple[Mapping[str, str], float]],
    *,
    spec: StrategySpec,
    minimum_positions: int,
) -> dict[str, float]:
    ordered = sorted(
        scored,
        key=lambda item: (
            -item[1],
            str(item[0].get("ticker") or ""),
        ),
    )
    if not ordered:
        return {}
    side_count = max(
        minimum_positions,
        math.ceil(len(ordered) * spec.quantile),
    )
    if spec.portfolio_type == "long_short":
        side_count = min(side_count, len(ordered) // 2)
    else:
        side_count = min(side_count, len(ordered))
    if side_count <= 0:
        return {}
    long_rows = ordered[:side_count]
    if spec.weighting == "equal":
        long_raw = {
            str(row.get("ticker")): 1.0 for row, _ in long_rows
        }
    else:
        long_raw = {
            str(row.get("ticker")): max(score, 1e-6)
            for row, score in long_rows
        }
    long_target = 0.5 if spec.portfolio_type == "long_short" else 1.0
    weights = _normalize(long_raw, long_target)
    if spec.portfolio_type == "long_short":
        short_rows = ordered[-side_count:]
        maximum_score = max(score for _, score in ordered)
        short_raw = {
            str(row.get("ticker")): max(maximum_score - score, 1e-6)
            for row, score in short_rows
        }
        weights.update(
            {
                ticker: -value
                for ticker, value in _normalize(short_raw, 0.5).items()
            }
        )
    return weights


def non_overlapping_dates(
    rows: Sequence[Mapping[str, str]],
    *,
    horizon: int,
    split_names: set[str],
    minimum_cross_section: int,
    universe_policy: str = ALL_RANK_READY_UNIVERSE_POLICY,
) -> list[str]:
    grouped: dict[str, list[Mapping[str, str]]] = defaultdict(list)
    for row in rows:
        if (
            str(row.get("split_name") or "") in split_names
            and str(row.get("base_panel_eligible_flag") or "") == "1"
            and str(
                row.get("execution_universe_eligible_flag") or ""
            )
            == "1"
            and production_universe_eligible(row, policy=universe_policy)
        ):
            grouped[str(row["asof_date"])].append(row)
    selected: list[str] = []
    next_available = None
    for asof in sorted(grouped):
        members = grouped[asof]
        if len(members) < minimum_cross_section:
            continue
        current = parse_date(asof)
        if next_available is not None and current < next_available:
            continue
        forward_dates = [
            parse_date(value, field="forward_date")
            for row in members
            if (
                value := str(
                    row.get(
                        f"benchmark_execution_exit_date_{horizon}d"
                    )
                    or ""
                )
            )
        ]
        if not forward_dates:
            continue
        selected.append(asof)
        next_available = max(forward_dates)
    return selected


def _product_return(values: Sequence[float]) -> float:
    wealth = 1.0
    for value in values:
        wealth *= max(1e-8, 1.0 + value)
    return wealth - 1.0


def _max_drawdown(values: Sequence[float]) -> float:
    wealth = 1.0
    peak = 1.0
    drawdown = 0.0
    for value in values:
        wealth *= max(1e-8, 1.0 + value)
        peak = max(peak, wealth)
        drawdown = min(drawdown, wealth / peak - 1.0)
    return drawdown


def _quantile(values: Sequence[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    location = (len(ordered) - 1) * probability
    lower = math.floor(location)
    upper = math.ceil(location)
    if lower == upper:
        return ordered[lower]
    fraction = location - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _annualize(total_return: float, periods: int, horizon: int) -> float:
    if periods <= 0 or total_return <= -1.0:
        return -1.0 if total_return <= -1.0 else 0.0
    return (1.0 + total_return) ** (252.0 / (periods * horizon)) - 1.0


def _fmt(value: object, digits: int = 12) -> str:
    parsed = as_float(value)
    if parsed is None:
        return ""
    return f"{parsed:.{digits}f}".rstrip("0").rstrip(".")


def run_variant(
    config: dict[str, Any],
    *,
    rows: Sequence[Mapping[str, str]],
    model: str,
    model_weights: Mapping[str, float],
    spec: StrategySpec,
    horizon: int,
    split_name: str,
    split_names: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    minimum_cross_section = int(
        cfg_get(config, "machinery_stage8.minimum_cross_section", 30)
    )
    minimum_positions = int(
        cfg_get(config, f"{CONFIG_KEY}.minimum_positions", 10)
    )
    transaction_cost_rate = float(
        cfg_get(config, f"{CONFIG_KEY}.transaction_cost_bps", 20.0)
    ) / 10000.0
    default_borrow_rate = float(
        cfg_get(config, f"{CONFIG_KEY}.default_borrow_fee_rate", 0.05)
    )
    adv_participation = float(
        cfg_get(config, f"{CONFIG_KEY}.max_adv_participation", 0.05)
    )
    universe_policy = production_universe_policy(config)
    selected_dates = non_overlapping_dates(
        rows,
        horizon=horizon,
        split_names=split_names,
        minimum_cross_section=minimum_cross_section,
        universe_policy=universe_policy,
    )
    grouped: dict[str, list[Mapping[str, str]]] = defaultdict(list)
    for row in rows:
        if str(row.get("asof_date") or "") in selected_dates:
            grouped[str(row["asof_date"])].append(row)
    previous: dict[str, float] | None = None
    previous_rows: dict[str, Mapping[str, str]] = {}
    period_rows: list[dict[str, Any]] = []
    holding_rows: list[dict[str, Any]] = []
    for asof in selected_dates:
        members = [
            row
            for row in grouped[asof]
            if str(row.get("base_panel_eligible_flag") or "") == "1"
            and str(
                row.get("execution_universe_eligible_flag") or ""
            )
            == "1"
            and production_universe_eligible(row, policy=universe_policy)
        ]
        scored = [
            (row, score)
            for row in members
            if (score := component_score(row, model_weights)) is not None
        ]
        weights = portfolio_weights(
            scored,
            spec=spec,
            minimum_positions=minimum_positions,
        )
        if not weights:
            continue
        row_by_ticker = {
            str(row.get("ticker") or ""): row for row, _ in scored
        }
        score_by_ticker = {
            str(row.get("ticker") or ""): score for row, score in scored
        }
        adv_by_ticker = {
            ticker: as_float(row.get("avg_dollar_volume_60d"))
            for ticker, row in row_by_ticker.items()
        }
        previous_weights = previous or {}
        trades = {
            ticker: weights.get(ticker, 0.0) - previous_weights.get(ticker, 0.0)
            for ticker in set(weights) | set(previous_weights)
        }
        traded_notional = sum(abs(value) for value in trades.values())
        one_way_turnover = (
            traded_notional
            if previous is None
            else traded_notional / 2.0
        )
        transaction_cost = traded_notional * transaction_cost_rate
        gross_return = 0.0
        borrow_cost = 0.0
        gross_exposure = sum(abs(value) for value in weights.values())
        net_exposure = sum(weights.values())
        cohort_weights: dict[str, float] = defaultdict(float)
        adv_weight = 0.0
        trade_with_adv = 0.0
        trade_capacity: list[float] = []
        capacity_by_ticker: dict[str, float] = {}
        forward_dates: list[str] = []
        benchmark_returns: list[float] = []
        period_holdings: list[dict[str, Any]] = []
        for ticker, trade in trades.items():
            if abs(trade) <= 1e-12:
                continue
            adv = adv_by_ticker.get(ticker)
            if adv is None:
                adv = as_float(
                    previous_rows.get(ticker, {}).get(
                        "avg_dollar_volume_60d"
                    )
                )
            if adv is not None and adv > 0:
                capacity = adv * adv_participation / abs(trade)
                capacity_by_ticker[ticker] = capacity
                trade_capacity.append(capacity)
                trade_with_adv += abs(trade)
        for ticker, weight in sorted(weights.items()):
            row = row_by_ticker[ticker]
            outcome = as_float(row.get(f"execution_return_{horizon}d"))
            if outcome is None:
                raise ValueError(
                    f"Eligible Stage 9 row lacks {horizon}d return: "
                    f"{asof} {ticker}"
                )
            gross_return += weight * outcome
            cohort = str(row.get("calibration_cohort") or "unclassified")
            cohort_weights[cohort] += abs(weight)
            adv = as_float(row.get("avg_dollar_volume_60d"))
            if adv is not None and adv > 0:
                adv_weight += abs(weight)
            trade = trades.get(ticker, weight)
            ticker_capacity = capacity_by_ticker.get(ticker)
            borrow_raw = as_float(row.get("latest_borrow_fee_rate"))
            borrow_rate = (
                borrow_raw
                if borrow_raw is not None and borrow_raw >= 0
                else default_borrow_rate
            )
            borrow_source = (
                "reported"
                if borrow_raw is not None and borrow_raw >= 0
                else "configured_fallback"
            )
            holding_borrow_cost = (
                abs(weight) * borrow_rate * horizon / 252.0
                if weight < 0
                else 0.0
            )
            borrow_cost += holding_borrow_cost
            forward_date = str(
                row.get(f"execution_exit_date_{horizon}d") or ""
            )
            if forward_date:
                forward_dates.append(forward_date)
            benchmark_return = as_float(
                row.get(f"benchmark_execution_return_{horizon}d")
            )
            if benchmark_return is not None:
                benchmark_returns.append(benchmark_return)
            period_holdings.append(
                {
                    "model": model,
                    "variant": spec.name,
                    "horizon_days": horizon,
                    "split_name": split_name,
                    "asof_date": asof,
                    "forward_date": forward_date,
                    "ticker": ticker,
                    "side": "long" if weight > 0 else "short",
                    "calibration_cohort": cohort,
                    "score": _fmt(score_by_ticker[ticker]),
                    "weight": _fmt(weight),
                    "previous_weight": _fmt(previous_weights.get(ticker, 0.0)),
                    "trade_weight": _fmt(trade),
                    "forward_return": _fmt(outcome),
                    "gross_return_contribution": _fmt(weight * outcome),
                    "latest_borrow_fee_rate": _fmt(borrow_rate),
                    "borrow_rate_source": borrow_source,
                    "borrow_cost_contribution": _fmt(holding_borrow_cost),
                    "avg_dollar_volume_60d": _fmt(adv),
                    "trade_capacity_usd": _fmt(ticker_capacity, 2),
                }
            )
        for ticker in sorted(set(previous_weights) - set(weights)):
            source_row = row_by_ticker.get(ticker) or previous_rows.get(ticker)
            if source_row is None:
                continue
            trade = trades[ticker]
            period_holdings.append(
                {
                    "model": model,
                    "variant": spec.name,
                    "horizon_days": horizon,
                    "split_name": split_name,
                    "asof_date": asof,
                    "forward_date": "",
                    "ticker": ticker,
                    "side": "exit",
                    "calibration_cohort": str(
                        source_row.get("calibration_cohort") or "unclassified"
                    ),
                    "score": _fmt(score_by_ticker.get(ticker)),
                    "weight": "0",
                    "previous_weight": _fmt(previous_weights[ticker]),
                    "trade_weight": _fmt(trade),
                    "forward_return": "",
                    "gross_return_contribution": "0",
                    "latest_borrow_fee_rate": "",
                    "borrow_rate_source": "not_applicable_exit",
                    "borrow_cost_contribution": "0",
                    "avg_dollar_volume_60d": _fmt(
                        adv_by_ticker.get(ticker)
                        or as_float(
                            previous_rows.get(ticker, {}).get(
                                "avg_dollar_volume_60d"
                            )
                        )
                    ),
                    "trade_capacity_usd": _fmt(
                        capacity_by_ticker.get(ticker),
                        2,
                    ),
                }
            )
        benchmark = mean(benchmark_returns) or 0.0
        if spec.portfolio_type == "long_short":
            benchmark = 0.0
        net_return = gross_return - transaction_cost - borrow_cost
        excess = net_return - benchmark
        max_cohort_share = (
            max(cohort_weights.values()) / gross_exposure
            if cohort_weights and gross_exposure > 0
            else 0.0
        )
        period_rows.append(
            {
                "model": model,
                "variant": spec.name,
                "portfolio_type": spec.portfolio_type,
                "weighting": spec.weighting,
                "quantile": _fmt(spec.quantile),
                "horizon_days": horizon,
                "split_name": split_name,
                "asof_date": asof,
                "forward_date": max(forward_dates) if forward_dates else "",
                "universe_count": len(scored),
                "position_count": len(weights),
                "long_count": sum(value > 0 for value in weights.values()),
                "short_count": sum(value < 0 for value in weights.values()),
                "gross_exposure": _fmt(gross_exposure),
                "net_exposure": _fmt(net_exposure),
                "one_way_turnover": _fmt(one_way_turnover),
                "traded_notional_fraction": _fmt(traded_notional),
                "transaction_cost": _fmt(transaction_cost),
                "borrow_cost": _fmt(borrow_cost),
                "gross_return": _fmt(gross_return),
                "net_return": _fmt(net_return),
                "benchmark_return": _fmt(benchmark),
                "net_excess_return": _fmt(excess),
                "max_position_weight": _fmt(
                    max(abs(value) for value in weights.values())
                ),
                "max_cohort_share": _fmt(max_cohort_share),
                "adv_weight_coverage": _fmt(
                    adv_weight / gross_exposure
                    if gross_exposure > 0
                    else 0.0
                ),
                "trade_adv_coverage": _fmt(
                    trade_with_adv / traded_notional
                    if traded_notional > 0
                    else 1.0
                ),
                "capacity_usd": _fmt(
                    min(trade_capacity) if trade_capacity else None,
                    2,
                ),
                "selected_tickers": ";".join(sorted(weights)),
            }
        )
        holding_rows.extend(period_holdings)
        previous = weights
        previous_rows = {
            ticker: row_by_ticker[ticker]
            for ticker in weights
        }
    return period_rows, holding_rows


def build_production_policy_parity(
    config: dict[str, Any],
    *,
    panel_rows: Sequence[Mapping[str, str]],
    period_rows: Sequence[Mapping[str, Any]],
    holding_rows: Sequence[Mapping[str, Any]],
    model_weights: Mapping[str, float],
    spec: StrategySpec,
    horizon: int,
) -> list[dict[str, Any]]:
    tolerance = float(
        cfg_get(
            config,
            f"{CONFIG_KEY}.gates.maximum_production_weight_parity_error",
            1e-10,
        )
    )
    if tolerance < 0:
        raise ValueError(
            "maximum_production_weight_parity_error must be non-negative"
        )
    minimum_positions = int(
        cfg_get(config, f"{CONFIG_KEY}.minimum_positions", 10)
    )
    universe_policy = production_universe_policy(config)
    source_by_date: dict[str, list[Mapping[str, str]]] = defaultdict(list)
    for row in panel_rows:
        source_by_date[str(row.get("asof_date") or "")].append(row)
    holdings_by_period: dict[
        tuple[str, str],
        list[Mapping[str, Any]],
    ] = defaultdict(list)
    for row in holding_rows:
        if (
            str(row.get("model") or "") == "stage8_candidate"
            and str(row.get("variant") or "") == spec.name
            and int(str(row.get("horizon_days") or "0")) == horizon
            and str(row.get("split_name") or "")
            in {"validation", "holdout"}
        ):
            holdings_by_period[
                (
                    str(row.get("split_name") or ""),
                    str(row.get("asof_date") or ""),
                )
            ].append(row)
    selected_periods = sorted(
        (
            row
            for row in period_rows
            if str(row.get("model") or "") == "stage8_candidate"
            and str(row.get("variant") or "") == spec.name
            and int(str(row.get("horizon_days") or "0")) == horizon
            and str(row.get("split_name") or "")
            in {"validation", "holdout"}
        ),
        key=lambda row: (
            str(row.get("split_name") or ""),
            str(row.get("asof_date") or ""),
        ),
    )
    output: list[dict[str, Any]] = []
    for period in selected_periods:
        split_name = str(period.get("split_name") or "")
        asof = str(period.get("asof_date") or "")
        members = [
            row
            for row in source_by_date.get(asof, [])
            if str(row.get("split_name") or "") == split_name
            and str(row.get(f"execution_available_flag_{horizon}d") or "")
            == "1"
            and str(row.get("base_panel_eligible_flag") or "") == "1"
            and production_universe_eligible(row, policy=universe_policy)
        ]
        scored = [
            (row, score)
            for row in members
            if (score := component_score(row, model_weights)) is not None
        ]
        expected = portfolio_weights(
            scored,
            spec=spec,
            minimum_positions=minimum_positions,
        )
        actual: dict[str, float] = {}
        for row in holdings_by_period.get((split_name, asof), []):
            weight = as_float(row.get("weight"))
            ticker = str(row.get("ticker") or "")
            if ticker and weight is not None and abs(weight) > tolerance:
                actual[ticker] = weight
        tickers = set(expected) | set(actual)
        maximum_difference = max(
            (
                abs(expected.get(ticker, 0.0) - actual.get(ticker, 0.0))
                for ticker in tickers
            ),
            default=0.0,
        )
        membership_match = set(expected) == set(actual)
        parity_pass = membership_match and maximum_difference <= tolerance
        output.append(
            {
                "selection_policy_version": (
                    PRODUCTION_SELECTION_POLICY_VERSION
                ),
                "model": "stage8_candidate",
                "variant": spec.name,
                "horizon_days": horizon,
                "split_name": split_name,
                "asof_date": asof,
                "expected_position_count": len(expected),
                "actual_position_count": len(actual),
                "membership_match": "1" if membership_match else "0",
                "expected_weight_sum": _fmt(sum(expected.values())),
                "actual_weight_sum": _fmt(sum(actual.values())),
                "maximum_absolute_weight_difference": _fmt(
                    maximum_difference
                ),
                "weight_tolerance": _fmt(tolerance),
                "expected_tickers": ";".join(sorted(expected)),
                "actual_tickers": ";".join(sorted(actual)),
                "parity_status": "PASS" if parity_pass else "FAIL",
            }
        )
    return output


def summarize_variant(
    period_rows: Sequence[Mapping[str, Any]],
    holding_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if not period_rows:
        return {}
    first = period_rows[0]
    horizon = int(first["horizon_days"])
    net_returns = [
        value
        for row in period_rows
        if (value := as_float(row.get("net_return"))) is not None
    ]
    benchmark_returns = [
        value
        for row in period_rows
        if (value := as_float(row.get("benchmark_return"))) is not None
    ]
    excess_returns = [
        value
        for row in period_rows
        if (value := as_float(row.get("net_excess_return"))) is not None
    ]
    total = _product_return(net_returns)
    benchmark_total = _product_return(benchmark_returns)
    net_stdev = stdev(net_returns) or 0.0
    excess_stdev = stdev(excess_returns) or 0.0
    capacities = [
        value
        for row in period_rows
        if (value := as_float(row.get("capacity_usd"))) is not None
    ]
    cohorts = {
        str(row.get("calibration_cohort") or "")
        for row in holding_rows
        if str(row.get("calibration_cohort") or "")
    }
    result = {
        "model": first["model"],
        "variant": first["variant"],
        "portfolio_type": first["portfolio_type"],
        "weighting": first["weighting"],
        "quantile": first["quantile"],
        "horizon_days": horizon,
        "split_name": first["split_name"],
        "period_count": len(period_rows),
        "mean_net_return": mean(net_returns) or 0.0,
        "mean_benchmark_return": mean(benchmark_returns) or 0.0,
        "mean_net_excess_return": mean(excess_returns) or 0.0,
        "annualized_net_return": _annualize(total, len(net_returns), horizon),
        "annualized_benchmark_return": _annualize(
            benchmark_total,
            len(benchmark_returns),
            horizon,
        ),
        "annualized_volatility": net_stdev * math.sqrt(252.0 / horizon),
        "information_ratio": (
            (mean(excess_returns) or 0.0)
            / excess_stdev
            * math.sqrt(252.0 / horizon)
            if excess_stdev > 0
            else 0.0
        ),
        "hit_rate_vs_benchmark": (
            sum(value > 0 for value in excess_returns) / len(excess_returns)
            if excess_returns
            else 0.0
        ),
        "max_drawdown": _max_drawdown(net_returns),
        "average_one_way_turnover": mean(
            value
            for row in period_rows
            if (value := as_float(row.get("one_way_turnover"))) is not None
        )
        or 0.0,
        "average_transaction_cost": mean(
            value
            for row in period_rows
            if (value := as_float(row.get("transaction_cost"))) is not None
        )
        or 0.0,
        "average_borrow_cost": mean(
            value
            for row in period_rows
            if (value := as_float(row.get("borrow_cost"))) is not None
        )
        or 0.0,
        "average_max_position_weight": mean(
            value
            for row in period_rows
            if (value := as_float(row.get("max_position_weight"))) is not None
        )
        or 0.0,
        "worst_max_position_weight": max(
            (
                value
                for row in period_rows
                if (
                    value := as_float(row.get("max_position_weight"))
                )
                is not None
            ),
            default=0.0,
        ),
        "average_max_cohort_share": mean(
            value
            for row in period_rows
            if (value := as_float(row.get("max_cohort_share"))) is not None
        )
        or 0.0,
        "worst_max_cohort_share": max(
            (
                value
                for row in period_rows
                if (value := as_float(row.get("max_cohort_share"))) is not None
            ),
            default=0.0,
        ),
        "average_adv_weight_coverage": mean(
            value
            for row in period_rows
            if (
                value := as_float(row.get("adv_weight_coverage"))
            )
            is not None
        )
        or 0.0,
        "average_trade_adv_coverage": mean(
            value
            for row in period_rows
            if (
                value := as_float(row.get("trade_adv_coverage"))
            )
            is not None
        )
        or 0.0,
        "capacity_p10_usd": _quantile(capacities, 0.10) or 0.0,
        "capacity_median_usd": _quantile(capacities, 0.50) or 0.0,
        "cohort_count": len(cohorts),
    }
    result["annualized_excess_return"] = (
        float(result["annualized_net_return"])
        - float(result["annualized_benchmark_return"])
    )
    return result


def _summary_gate(
    config: dict[str, Any],
    summary: Mapping[str, Any],
    *,
    split_name: str,
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    target_aum = float(
        cfg_get(config, f"{CONFIG_KEY}.target_aum_usd", 300_000)
    )
    minimum_capacity = target_aum * float(
        cfg_get(
            config,
            f"{CONFIG_KEY}.gates.minimum_capacity_multiple",
            5.0,
        )
    )
    minimum_periods = int(
        cfg_get(
            config,
            (
                f"{CONFIG_KEY}.gates.minimum_validation_periods"
                if split_name == "validation"
                else f"{CONFIG_KEY}.gates.minimum_holdout_periods"
            ),
            12,
        )
    )
    if int(summary.get("period_count") or 0) < minimum_periods:
        reasons.append("insufficient_non_overlapping_periods")
    checks = (
        (
            "mean_net_excess_return",
            float(
                cfg_get(
                    config,
                    f"{CONFIG_KEY}.gates.minimum_mean_net_excess_return",
                    0.0,
                )
            ),
            "mean_net_excess_below_gate",
            "minimum",
        ),
        (
            "hit_rate_vs_benchmark",
            float(
                cfg_get(
                    config,
                    f"{CONFIG_KEY}.gates.minimum_hit_rate",
                    0.50,
                )
            ),
            "hit_rate_below_gate",
            "minimum",
        ),
        (
            "max_drawdown",
            float(
                cfg_get(
                    config,
                    f"{CONFIG_KEY}.gates.maximum_drawdown",
                    -0.35,
                )
            ),
            "drawdown_below_gate",
            "minimum",
        ),
        (
            "average_one_way_turnover",
            float(
                cfg_get(
                    config,
                    f"{CONFIG_KEY}.gates.maximum_average_turnover",
                    0.75,
                )
            ),
            "turnover_above_gate",
            "maximum",
        ),
        (
            "worst_max_position_weight",
            float(
                cfg_get(
                    config,
                    f"{CONFIG_KEY}.gates.maximum_position_weight",
                    0.15,
                )
            ),
            "position_concentration_above_gate",
            "maximum",
        ),
        (
            "worst_max_cohort_share",
            float(
                cfg_get(
                    config,
                    f"{CONFIG_KEY}.gates.maximum_cohort_share",
                    0.50,
                )
            ),
            "cohort_concentration_above_gate",
            "maximum",
        ),
        (
            "average_adv_weight_coverage",
            float(
                cfg_get(
                    config,
                    f"{CONFIG_KEY}.gates.minimum_adv_coverage",
                    0.95,
                )
            ),
            "adv_coverage_below_gate",
            "minimum",
        ),
        (
            "capacity_p10_usd",
            minimum_capacity,
            "capacity_below_gate",
            "minimum",
        ),
        (
            "cohort_count",
            float(
                cfg_get(
                    config,
                    f"{CONFIG_KEY}.gates.minimum_selected_cohorts",
                    3,
                )
            ),
            "cohort_breadth_below_gate",
            "minimum",
        ),
    )
    for field, threshold, reason, direction in checks:
        value = float(summary.get(field) or 0.0)
        if direction == "minimum" and value < threshold:
            reasons.append(reason)
        if direction == "maximum" and value > threshold:
            reasons.append(reason)
    return not reasons, reasons


def _summary_key(
    row: Mapping[str, Any],
) -> tuple[str, str, int, str]:
    return (
        str(row["model"]),
        str(row["variant"]),
        int(row["horizon_days"]),
        str(row["split_name"]),
    )


def run_stage9(
    config: dict[str, Any],
    *,
    stage8_root: Path,
    output_root: Path,
) -> dict[str, Any]:
    paths = stage9_paths(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    stage8_validation = validate_stage8(
        config,
        output_root=stage8_root,
        require_stage9_ready=True,
    )
    if stage8_validation["acceptance"] != "PASS":
        raise ValueError(
            "Stage 8 artifacts did not clear the Stage 9 prerequisite: "
            + ";".join(stage8_validation["issues"])
        )
    source_paths = stage8_paths(stage8_root)
    panel_manifest = json.loads(
        source_paths.panel_manifest_json.read_text(encoding="utf-8")
    )
    stage9_universe_policy = production_universe_policy(config)
    if (
        panel_manifest.get("production_universe_policy")
        != stage9_universe_policy
    ):
        raise ValueError(
            "Stage 8 and Stage 9 production universe policies differ"
        )
    stage8_acceptance = json.loads(
        source_paths.acceptance_json.read_text(encoding="utf-8")
    )
    static = json.loads(
        source_paths.static_summary_json.read_text(encoding="utf-8")
    )
    panel_rows = read_csv_rows(source_paths.panel_csv)
    candidate_weights = {
        str(key): float(value)
        for key, value in stage8_acceptance["recommended_weights"].items()
    }
    baseline_weights = {
        str(key): float(value)
        for key, value in static["baseline_weights"].items()
    }
    models = {
        "configured_baseline": baseline_weights,
        "stage8_candidate": candidate_weights,
    }
    horizons = [
        int(value)
        for value in cfg_get(config, f"{CONFIG_KEY}.horizons_trading_days", [21, 63])
    ]
    split_sets = {
        "validation": {"validation"},
        "holdout": {"holdout"},
        "all_development": {"train", "validation", "holdout"},
    }
    period_rows: list[dict[str, Any]] = []
    holding_rows: list[dict[str, Any]] = []
    summary_rows: list[dict[str, Any]] = []
    for model, weights in models.items():
        for spec in strategy_specs(config):
            for horizon in horizons:
                for split_name, split_names in split_sets.items():
                    periods, holdings = run_variant(
                        config,
                        rows=panel_rows,
                        model=model,
                        model_weights=weights,
                        spec=spec,
                        horizon=horizon,
                        split_name=split_name,
                        split_names=split_names,
                    )
                    period_rows.extend(periods)
                    holding_rows.extend(holdings)
                    summary = summarize_variant(periods, holdings)
                    if summary:
                        summary_rows.append(summary)
    write_csv_atomic(paths.periods_csv, PERIOD_FIELDS, period_rows)
    write_csv_atomic(paths.holdings_csv, HOLDING_FIELDS, holding_rows)
    write_csv_atomic(paths.summary_csv, SUMMARY_FIELDS, summary_rows)
    summary_by_key = {
        _summary_key(row): row for row in summary_rows
    }
    primary_horizon = int(
        cfg_get(config, f"{CONFIG_KEY}.gates.primary_horizon_days", 21)
    )
    validation_candidates = [
        row
        for row in summary_rows
        if row["model"] == "stage8_candidate"
        and row["portfolio_type"] == "long_only"
        and int(row["horizon_days"]) == primary_horizon
        and row["split_name"] == "validation"
    ]
    passing_validation = [
        row
        for row in validation_candidates
        if _summary_gate(
            config,
            row,
            split_name="validation",
        )[0]
    ]
    selection_pool = passing_validation or validation_candidates
    preferred = max(
        selection_pool,
        key=lambda row: (
            float(row["annualized_excess_return"]),
            -float(row["average_one_way_turnover"]),
            str(row["variant"]),
        ),
        default=None,
    )
    blockers: list[str] = []
    validation_gate = False
    holdout_gate = False
    validation_reasons: list[str] = []
    holdout_reasons: list[str] = []
    candidate_improvement = 0.0
    preferred_variant = ""
    parity_rows: list[dict[str, Any]] = []
    if preferred is None:
        blockers.append("no_primary_horizon_candidate_summary")
    else:
        preferred_variant = str(preferred["variant"])
        validation_gate, validation_reasons = _summary_gate(
            config,
            preferred,
            split_name="validation",
        )
        holdout = summary_by_key.get(
            (
                "stage8_candidate",
                preferred_variant,
                primary_horizon,
                "holdout",
            )
        )
        baseline_holdout = summary_by_key.get(
            (
                "configured_baseline",
                preferred_variant,
                primary_horizon,
                "holdout",
            )
        )
        if holdout is None or baseline_holdout is None:
            blockers.append("preferred_variant_holdout_summary_missing")
        else:
            holdout_gate, holdout_reasons = _summary_gate(
                config,
                holdout,
                split_name="holdout",
            )
            candidate_improvement = float(
                holdout["mean_net_excess_return"]
            ) - float(baseline_holdout["mean_net_excess_return"])
            minimum_improvement = float(
                cfg_get(
                    config,
                    (
                        f"{CONFIG_KEY}.gates."
                        "minimum_candidate_excess_improvement"
                    ),
                    0.0,
                )
            )
            if candidate_improvement < minimum_improvement:
                blockers.append("candidate_did_not_beat_baseline_on_holdout")
        preferred_spec = strategy_spec_by_name(config, preferred_variant)
        parity_rows = build_production_policy_parity(
            config,
            panel_rows=panel_rows,
            period_rows=period_rows,
            holding_rows=holding_rows,
            model_weights=candidate_weights,
            spec=preferred_spec,
            horizon=primary_horizon,
        )
        if not parity_rows:
            blockers.append("production_policy_parity_has_no_periods")
        elif any(
            str(row.get("parity_status") or "") != "PASS"
            for row in parity_rows
        ):
            blockers.append("production_policy_parity_failed")
    if not validation_gate:
        blockers.extend(
            f"validation:{reason}" for reason in validation_reasons
        )
    if not holdout_gate:
        blockers.extend(f"holdout:{reason}" for reason in holdout_reasons)
    stage12_ready = (
        preferred is not None
        and validation_gate
        and holdout_gate
        and not blockers
    )
    write_csv_atomic(paths.parity_csv, PARITY_FIELDS, parity_rows)
    production_policy = (
        {
            "version": PRODUCTION_SELECTION_POLICY_VERSION,
            "variant": preferred_variant,
            "portfolio_type": preferred_spec.portfolio_type,
            "weighting": preferred_spec.weighting,
            "quantile": preferred_spec.quantile,
            "minimum_positions": int(
                cfg_get(config, f"{CONFIG_KEY}.minimum_positions", 10)
            ),
            "universe_policy": production_universe_policy(config),
            "parity_period_count": len(parity_rows),
            "parity_status": (
                "PASS"
                if parity_rows
                and all(
                    str(row.get("parity_status") or "") == "PASS"
                    for row in parity_rows
                )
                else "FAIL"
            ),
        }
        if preferred
        else {}
    )
    acceptance = {
        "acceptance": "PASS",
        "stage9_implementation_status": "COMPLETE",
        "stage12_readiness": "READY" if stage12_ready else "BLOCKED",
        "recommended_model_for_stage12": (
            "stage8_candidate" if stage12_ready else "none"
        ),
        "recommended_variant_for_stage12": (
            preferred_variant if stage12_ready else "none"
        ),
        "recommended_weights": candidate_weights if stage12_ready else {},
        "production_selection_policy": (
            production_policy if stage12_ready else {}
        ),
        "primary_horizon_days": primary_horizon,
        "validation_variant_selected_without_holdout": preferred_variant,
        "validation_gate": validation_gate,
        "validation_gate_reasons": validation_reasons,
        "holdout_gate": holdout_gate,
        "holdout_gate_reasons": holdout_reasons,
        "candidate_holdout_excess_improvement_vs_baseline": (
            candidate_improvement
        ),
        "target_aum_usd": float(
            cfg_get(config, f"{CONFIG_KEY}.target_aum_usd", 300_000)
        ),
        "minimum_capacity_multiple": float(
            cfg_get(
                config,
                f"{CONFIG_KEY}.gates.minimum_capacity_multiple",
                5.0,
            )
        ),
        "period_row_count": len(period_rows),
        "holding_row_count": len(holding_rows),
        "summary_row_count": len(summary_rows),
        "production_promotion_performed": False,
        "live_dashboard_modified": False,
        "lockbox_outcomes_accessed": False,
        "blockers": list(dict.fromkeys(blockers)),
    }
    write_json_atomic(paths.acceptance_json, acceptance)
    artifacts = (
        paths.periods_csv,
        paths.holdings_csv,
        paths.summary_csv,
        paths.parity_csv,
        paths.acceptance_json,
    )
    manifest = {
        "artifact_family": "machinery_stage9_backtest",
        "model_family": MODEL_FAMILY,
        "created_at_utc": utc_now(),
        "report_only": True,
        "effective_stage9_config_sha256": stage9_config_sha256(config),
        "source_stage8_root": str(stage8_root),
        "source_stage8_run_manifest_sha256": file_sha256(
            source_paths.run_manifest_json
        ),
        "source_stage8_panel_sha256": file_sha256(source_paths.panel_csv),
        "source_stage8_acceptance_sha256": file_sha256(
            source_paths.acceptance_json
        ),
        "production_promotion_performed": False,
        "files": {
            path.name: {
                "path": str(path),
                "sha256": file_sha256(path),
            }
            for path in artifacts
        },
    }
    write_json_atomic(paths.run_manifest_json, manifest)
    return acceptance


def validate_stage9(
    config: dict[str, Any],
    *,
    stage8_root: Path,
    output_root: Path,
    require_stage12_ready: bool,
) -> dict[str, Any]:
    paths = stage9_paths(output_root)
    source_paths = stage8_paths(stage8_root)
    required = (
        paths.periods_csv,
        paths.holdings_csv,
        paths.summary_csv,
        paths.parity_csv,
        paths.acceptance_json,
        paths.run_manifest_json,
    )
    issues = [
        f"missing Stage 9 artifact {path}"
        for path in required
        if not path.exists() or path.stat().st_size == 0
    ]
    stage8_validation = validate_stage8(
        config,
        output_root=stage8_root,
        require_stage9_ready=True,
    )
    if stage8_validation["acceptance"] != "PASS":
        issues.extend(
            f"Stage 8 prerequisite: {issue}"
            for issue in stage8_validation["issues"]
        )
    if issues:
        result = {
            "acceptance": "FAIL",
            "stage12_readiness": "UNKNOWN",
            "issues": issues,
        }
        write_json_atomic(paths.validation_json, result)
        return result
    manifest = json.loads(paths.run_manifest_json.read_text(encoding="utf-8"))
    if manifest.get("effective_stage9_config_sha256") != stage9_config_sha256(
        config
    ):
        issues.append("effective Stage 9 configuration changed after run")
    files = manifest.get("files")
    if not isinstance(files, Mapping):
        issues.append("Stage 9 run manifest missing files mapping")
    else:
        for metadata in files.values():
            if not isinstance(metadata, Mapping):
                issues.append("Stage 9 run manifest has invalid file metadata")
                continue
            path = Path(str(metadata.get("path") or ""))
            if not path.exists() or file_sha256(path) != str(
                metadata.get("sha256") or ""
            ):
                issues.append(f"Stage 9 artifact hash mismatch {path}")
    if file_sha256(source_paths.run_manifest_json) != str(
        manifest.get("source_stage8_run_manifest_sha256") or ""
    ):
        issues.append("Stage 8 run manifest changed after Stage 9")
    if file_sha256(source_paths.panel_csv) != str(
        manifest.get("source_stage8_panel_sha256") or ""
    ):
        issues.append("Stage 8 panel changed after Stage 9")
    panel_manifest = json.loads(
        source_paths.panel_manifest_json.read_text(encoding="utf-8")
    )
    if (
        panel_manifest.get("production_universe_policy")
        != production_universe_policy(config)
    ):
        issues.append("Stage 8/9 production universe policy mismatch")
    acceptance = json.loads(paths.acceptance_json.read_text(encoding="utf-8"))
    period_rows = read_csv_rows(paths.periods_csv)
    holding_rows = read_csv_rows(paths.holdings_csv)
    summary_rows = read_csv_rows(paths.summary_csv)
    parity_rows = read_csv_rows(paths.parity_csv)
    groups: dict[
        tuple[str, str, str, str],
        list[dict[str, str]],
    ] = defaultdict(list)
    for row in period_rows:
        groups[
            (
                row["model"],
                row["variant"],
                row["horizon_days"],
                row["split_name"],
            )
        ].append(row)
        gross = as_float(row.get("gross_exposure"))
        net = as_float(row.get("net_exposure"))
        if gross is None or abs(gross - 1.0) > 1e-8:
            issues.append("Stage 9 period gross exposure is not 1")
            break
        portfolio_type = row.get("portfolio_type")
        target_net = 1.0 if portfolio_type == "long_only" else 0.0
        if net is None or abs(net - target_net) > 1e-8:
            issues.append("Stage 9 period net exposure mismatch")
            break
    for key, members in groups.items():
        prior_forward = None
        for row in sorted(members, key=lambda item: item["asof_date"]):
            asof = parse_date(row["asof_date"])
            if prior_forward is not None and asof < prior_forward:
                issues.append(f"overlapping Stage 9 periods detected for {key}")
                break
            prior_forward = parse_date(
                row["forward_date"],
                field="forward_date",
            )
        if issues:
            break
    if acceptance.get("production_promotion_performed") is not False:
        issues.append("Stage 9 unexpectedly performed production promotion")
    if acceptance.get("live_dashboard_modified") is not False:
        issues.append("Stage 9 unexpectedly modified the live dashboard")
    production_policy = acceptance.get("production_selection_policy")
    if not isinstance(production_policy, Mapping):
        issues.append("Stage 9 production selection policy is missing")
    else:
        if (
            production_policy.get("version")
            != PRODUCTION_SELECTION_POLICY_VERSION
        ):
            issues.append("Stage 9 production selection policy is stale")
        if production_policy.get("parity_status") != "PASS":
            issues.append("Stage 9 production policy parity did not pass")
        if int(production_policy.get("parity_period_count") or 0) != len(
            parity_rows
        ):
            issues.append("Stage 9 production policy parity count mismatch")
    if not parity_rows:
        issues.append("Stage 9 production policy parity is empty")
    elif any(row.get("parity_status") != "PASS" for row in parity_rows):
        issues.append("Stage 9 production policy parity contains failures")
    readiness = str(acceptance.get("stage12_readiness") or "UNKNOWN")
    if require_stage12_ready and readiness != "READY":
        issues.append("Stage 9 did not clear the Stage 12 readiness gate")
    result = {
        "acceptance": "PASS" if not issues else "FAIL",
        "stage12_readiness": readiness,
        "recommended_model_for_stage12": acceptance.get(
            "recommended_model_for_stage12",
            "none",
        ),
        "recommended_variant_for_stage12": acceptance.get(
            "recommended_variant_for_stage12",
            "none",
        ),
        "period_rows": len(period_rows),
        "holding_rows": len(holding_rows),
        "summary_rows": len(summary_rows),
        "production_policy_parity_rows": len(parity_rows),
        "issues": issues,
    }
    write_csv_atomic(
        paths.validation_csv,
        (
            "acceptance",
            "stage12_readiness",
            "recommended_model_for_stage12",
            "recommended_variant_for_stage12",
            "period_rows",
            "holding_rows",
            "summary_rows",
            "production_policy_parity_rows",
            "issues",
        ),
        [{**result, "issues": ";".join(issues)}],
    )
    write_json_atomic(paths.validation_json, result)
    return result
