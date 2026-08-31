from __future__ import annotations

import bisect
import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable, Mapping, Sequence


@dataclass(frozen=True)
class ExecutionPricePoint:
    bar_date: date
    adjusted_close: float
    adjusted_open: float | None
    source_id: str
    price_basis: str
    price_adjustment: str = ""


@dataclass(frozen=True)
class ExecutionWindow:
    entry: ExecutionPricePoint | None
    exit: ExecutionPricePoint | None
    method: str
    unavailable_reason: str
    terminal_exit: bool = False

    @property
    def return_value(self) -> float | None:
        if self.entry is None or self.exit is None:
            return None
        entry = self.entry.adjusted_open
        exit_value = (
            self.exit.adjusted_close
            if self.terminal_exit
            else self.exit.adjusted_open
        )
        if entry is None or entry <= 0 or exit_value is None or exit_value < 0:
            return None
        value = exit_value / entry - 1.0
        return value if math.isfinite(value) else None


def finite_float(value: object) -> float | None:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def finite_or_default(value: object, *, default: float) -> float:
    """Return a finite numeric value without treating an exact zero as missing."""
    parsed = finite_float(value)
    return default if parsed is None else parsed


def fmt(value: object, digits: int = 12) -> str:
    parsed = finite_float(value)
    if parsed is None:
        return ""
    return f"{parsed:.{digits}f}".rstrip("0").rstrip(".")


def parse_date(value: object, *, field: str = "date") -> date:
    raw = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"Invalid {field}: {value!r}") from exc


def select_weekly_dates(
    available_dates: Iterable[str],
    *,
    anchor: str,
    selection: str = "last",
) -> list[str]:
    if selection not in {"first", "last"}:
        raise ValueError("weekly selection must be first or last")
    anchor_date = parse_date(anchor, field="weekly anchor")
    buckets: dict[int, list[str]] = defaultdict(list)
    for raw in sorted(set(available_dates)):
        parsed = parse_date(raw)
        buckets[(parsed - anchor_date).days // 7].append(parsed.isoformat())
    return [
        (
            sorted(buckets[key])[0]
            if selection == "first"
            else sorted(buckets[key])[-1]
        )
        for key in sorted(buckets)
    ]


def load_adjusted_open_prices(
    connection: sqlite3.Connection,
    *,
    tickers: Sequence[str],
    sources: Sequence[str],
    end_date: str | None = None,
) -> dict[str, dict[str, list[ExecutionPricePoint]]]:
    clean_tickers = sorted(
        {str(item).upper() for item in tickers if str(item).strip()}
    )
    clean_sources = list(
        dict.fromkeys(str(item) for item in sources if str(item).strip())
    )
    if not clean_tickers or not clean_sources:
        return {}
    ticker_slots = ",".join("?" for _ in clean_tickers)
    source_slots = ",".join("?" for _ in clean_sources)
    end_clause = "AND bar_date <= ?" if end_date else ""
    params: list[object] = [*clean_tickers, *clean_sources]
    if end_date:
        params.append(end_date)
    rows = connection.execute(
        f"""
        SELECT ticker, source_id, bar_date, open, close, adj_close,
               COALESCE(price_adjustment, '') AS price_adjustment
        FROM fact_price_ohlcv
        WHERE UPPER(ticker) IN ({ticker_slots})
          AND source_id IN ({source_slots})
          {end_clause}
          AND (adj_close IS NOT NULL OR close IS NOT NULL)
        ORDER BY ticker, source_id, bar_date
        """,
        params,
    )
    output: dict[str, dict[str, list[ExecutionPricePoint]]] = {}
    for row in rows:
        adjusted_close = finite_float(row["adj_close"])
        close = finite_float(row["close"])
        open_value = finite_float(row["open"])
        value = adjusted_close if adjusted_close is not None else close
        if value is None or value < 0:
            continue
        adjusted_open = open_value
        if (
            adjusted_close is not None
            and close is not None
            and close > 0
            and open_value is not None
            and open_value > 0
        ):
            adjusted_open = open_value * adjusted_close / close
        ticker = str(row["ticker"]).upper()
        source = str(row["source_id"])
        output.setdefault(ticker, {}).setdefault(source, []).append(
            ExecutionPricePoint(
                bar_date=parse_date(row["bar_date"], field="bar_date"),
                adjusted_close=value,
                adjusted_open=adjusted_open,
                source_id=source,
                price_basis=(
                    "split_dividend_adjusted_open"
                    if adjusted_close is not None
                    else "raw_open"
                ),
                price_adjustment=str(row["price_adjustment"] or ""),
            )
        )
    return output


def execution_window(
    series_by_source: Mapping[str, Sequence[ExecutionPricePoint]],
    *,
    asof: str,
    horizon_sessions: int,
    source_order: Sequence[str],
    terminal_date: date | None = None,
    terminal_type: str = "",
    horizon_end: date | None = None,
    current_security_start_date: date | None = None,
    structural_break_date: date | None = None,
    max_signal_staleness_days: int = 10,
    max_terminal_staleness_days: int = 10,
) -> ExecutionWindow:
    if horizon_sessions <= 0:
        raise ValueError("horizon_sessions must be positive")
    signal_date = parse_date(asof, field="asof")
    terminal_expected = bool(
        terminal_date is not None
        and horizon_end is not None
        and signal_date < terminal_date <= horizon_end
    )
    saw_signal = False
    saw_entry = False
    saw_boundary = False
    saw_terminal_gap = False
    for source in source_order:
        series = list(series_by_source.get(source, ()))
        if not series:
            continue
        dates = [point.bar_date for point in series]
        signal_index = bisect.bisect_right(dates, signal_date) - 1
        if signal_index < 0:
            continue
        signal = series[signal_index]
        if (signal_date - signal.bar_date).days > max_signal_staleness_days:
            continue
        saw_signal = True
        entry_index = signal_index + 1
        if entry_index >= len(series):
            continue
        entry = series[entry_index]
        if entry.adjusted_open is None or entry.adjusted_open <= 0:
            continue
        saw_entry = True
        if (
            current_security_start_date
            and entry.bar_date < current_security_start_date
        ):
            saw_boundary = True
            continue
        if terminal_expected and terminal_date is not None:
            if terminal_date <= entry.bar_date:
                saw_terminal_gap = True
                continue
            terminal_points = [
                point
                for point in series[entry_index:]
                if point.bar_date <= terminal_date
            ]
            if not terminal_points:
                saw_terminal_gap = True
                continue
            terminal = terminal_points[-1]
            if (
                terminal_date - terminal.bar_date
            ).days > max_terminal_staleness_days:
                saw_terminal_gap = True
                continue
            if (
                structural_break_date
                and entry.bar_date < structural_break_date <= terminal.bar_date
            ):
                saw_boundary = True
                continue
            if terminal_type == "wipeout":
                terminal = ExecutionPricePoint(
                    bar_date=terminal_date,
                    adjusted_close=0.0,
                    adjusted_open=0.0,
                    source_id=source,
                    price_basis="reviewed_terminal_zero",
                    price_adjustment="terminal_type=wipeout",
                )
            elif terminal_type not in {
                "acquisition",
                "distressed_nonzero",
                "wipeout",
            }:
                saw_terminal_gap = True
                continue
            return ExecutionWindow(
                entry,
                terminal,
                "terminal_membership_exit",
                "",
                True,
            )
        exit_index = entry_index + horizon_sessions
        if exit_index >= len(series):
            continue
        exit_point = series[exit_index]
        if exit_point.adjusted_open is None or exit_point.adjusted_open <= 0:
            continue
        if (
            structural_break_date
            and entry.bar_date < structural_break_date <= exit_point.bar_date
        ):
            saw_boundary = True
            continue
        return ExecutionWindow(
            entry,
            exit_point,
            "scheduled_d1_open_to_open",
            "",
            False,
        )
    if saw_boundary:
        reason = "security_continuity_boundary_violation"
    elif terminal_expected and saw_terminal_gap:
        reason = "missing_verified_terminal_outcome"
    elif saw_entry:
        reason = "execution_window_crosses_data_end"
    elif saw_signal:
        reason = "missing_d1_open_execution_price"
    else:
        reason = "missing_or_stale_signal_price"
    return ExecutionWindow(None, None, "", reason, False)


def purged_split_map(
    snapshot_dates: Sequence[str],
    *,
    train_fraction: float = 0.60,
    validation_fraction: float = 0.20,
    purge_calendar_days: int = 91,
) -> dict[str, str]:
    dates = sorted(set(snapshot_dates))
    if len(dates) < 10:
        return {item: "insufficient_history" for item in dates}
    train_cut = max(1, int(len(dates) * train_fraction))
    validation_cut = min(
        len(dates) - 1,
        max(
            train_cut + 1,
            int(len(dates) * (train_fraction + validation_fraction)),
        ),
    )
    output = {
        asof: (
            "train"
            if index < train_cut
            else "validation"
            if index < validation_cut
            else "holdout"
        )
        for index, asof in enumerate(dates)
    }
    validation_start = parse_date(dates[train_cut])
    holdout_start = parse_date(dates[validation_cut])
    for asof, role in list(output.items()):
        parsed = parse_date(asof)
        boundary = (
            validation_start
            if role == "train"
            else holdout_start
            if role == "validation"
            else None
        )
        if boundary and (boundary - parsed).days <= purge_calendar_days:
            output[asof] = "embargo"
    return output


def rank_values(values: Sequence[float]) -> list[float]:
    indexed = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    cursor = 0
    while cursor < len(indexed):
        end = cursor + 1
        while (
            end < len(indexed)
            and indexed[end][1] == indexed[cursor][1]
        ):
            end += 1
        rank = (cursor + 1 + end) / 2.0
        for original, _ in indexed[cursor:end]:
            ranks[original] = rank
        cursor = end
    return ranks


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    xr = rank_values(xs)
    yr = rank_values(ys)
    xm = sum(xr) / len(xr)
    ym = sum(yr) / len(yr)
    xd = [value - xm for value in xr]
    yd = [value - ym for value in yr]
    scale = math.sqrt(
        sum(value * value for value in xd)
        * sum(value * value for value in yd)
    )
    return (
        sum(x * y for x, y in zip(xd, yd)) / scale
        if scale > 0
        else None
    )


def normalized_weights(
    fields: Sequence[str],
    weights: Mapping[str, float],
) -> dict[str, float]:
    clean = {
        field: max(0.0, float(weights.get(field, 0.0)))
        for field in fields
    }
    total = sum(clean.values())
    if total <= 0:
        raise ValueError("candidate weights must contain a positive weight")
    return {field: value / total for field, value in clean.items()}


def weighted_score(
    row: Mapping[str, object],
    weights: Mapping[str, float],
    *,
    require_complete: bool = False,
) -> float | None:
    numerator = 0.0
    denominator = 0.0
    for field, weight in weights.items():
        value = finite_float(row.get(field))
        if weight <= 0:
            continue
        if value is None:
            if require_complete:
                return None
            continue
        numerator += value * weight
        denominator += weight
    return numerator / denominator if denominator > 0 else None


def max_drawdown(returns: Sequence[float]) -> float | None:
    if not returns:
        return None
    equity = 1.0
    peak = 1.0
    worst = 0.0
    for value in returns:
        equity *= 1.0 + value
        peak = max(peak, equity)
        if peak > 0:
            worst = min(worst, equity / peak - 1.0)
    return worst


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _hit_rate(values: Sequence[float]) -> float | None:
    return (
        sum(value > 0 for value in values) / len(values)
        if values
        else None
    )


def _one_way_turnover(
    current: set[str],
    previous: set[str] | None,
) -> float:
    if previous is None:
        return 1.0
    return 1.0 - len(previous & current) / max(len(previous), len(current))


def _non_overlapping_period_rows(
    rows: Sequence[Mapping[str, object]],
) -> list[Mapping[str, object]]:
    selected: list[Mapping[str, object]] = []
    last_exit: date | None = None
    for row in sorted(
        rows,
        key=lambda item: (
            str(item.get("entry_date") or item.get("asof_date") or ""),
            str(item.get("asof_date") or ""),
        ),
    ):
        entry = parse_date(
            row.get("entry_date") or row.get("asof_date"),
            field="independent entry_date",
        )
        exit_date = parse_date(row.get("exit_date"), field="independent exit_date")
        if last_exit is not None and entry <= last_exit:
            continue
        selected.append(row)
        last_exit = exit_date
    return selected


def summarize_candidate_period_rows(
    period_rows: Sequence[Mapping[str, object]],
    *,
    eligible_row_count: int,
    available_outcome_row_count: int,
    independent_schedule_rows: Sequence[Mapping[str, object]] | None = None,
    independent_eligible_row_count: int | None = None,
    independent_available_outcome_row_count: int | None = None,
    invalid_execution_interval_cross_sections: Sequence[
        Mapping[str, object]
    ] = (),
    early_terminal_observation_count: int = 0,
    late_security_entry_observation_count: int = 0,
) -> dict[str, object]:
    """Summarize overlapping diagnostics and a separately costed schedule."""
    ordered = sorted(period_rows, key=lambda row: str(row.get("asof_date") or ""))
    if independent_schedule_rows is None:
        independent_rows = _non_overlapping_period_rows(ordered)
    else:
        independent_rows = [
            row
            for row in independent_schedule_rows
            if int(float(str(row.get("evaluation_available_flag") or 0))) == 1
        ]

    def values(field: str, source: Sequence[Mapping[str, object]]) -> list[float]:
        return [
            value
            for item in source
            if (value := finite_float(item.get(field))) is not None
        ]

    net_returns = values("net_excess", ordered)
    rank_vs_cohort = values("top_minus_cohort_net", ordered)
    rank_top_bottom = values("top_minus_bottom_gross", ordered)
    cohort_returns = values("cohort_excess", ordered)
    non_overlapping_returns = values("net_excess", independent_rows)
    non_overlapping_rank_vs_cohort = values(
        "top_minus_cohort_net", independent_rows
    )
    non_overlapping_top_bottom = values(
        "top_minus_bottom_gross", independent_rows
    )
    non_overlapping_ics = values("ic", independent_rows)
    ics = values("ic", ordered)
    turnovers = values("turnover", ordered)
    independent_turnovers = values("turnover", independent_rows)
    independent_eligible = (
        eligible_row_count
        if independent_eligible_row_count is None
        else independent_eligible_row_count
    )
    independent_available = (
        available_outcome_row_count
        if independent_available_outcome_row_count is None
        else independent_available_outcome_row_count
    )

    return {
        "eligible_row_count": eligible_row_count,
        "available_outcome_row_count": available_outcome_row_count,
        "outcome_coverage": (
            available_outcome_row_count / eligible_row_count
            if eligible_row_count
            else 0.0
        ),
        "snapshot_count": len(ordered),
        "mean_ic": _mean(ics),
        "mean_top_excess_net": _mean(net_returns),
        "top_excess_hit_rate": _hit_rate(net_returns),
        "mean_cohort_excess": _mean(cohort_returns),
        "mean_top_minus_cohort_net": _mean(rank_vs_cohort),
        "top_minus_cohort_hit_rate": _hit_rate(rank_vs_cohort),
        "mean_top_minus_bottom_gross": _mean(rank_top_bottom),
        "top_minus_bottom_hit_rate": _hit_rate(rank_top_bottom),
        "non_overlapping_snapshot_count": len(independent_rows),
        "independent_snapshot_count": len(independent_rows),
        "independent_eligible_row_count": independent_eligible,
        "independent_available_outcome_row_count": independent_available,
        "independent_outcome_coverage": (
            independent_available / independent_eligible
            if independent_eligible
            else 0.0
        ),
        "mean_non_overlapping_ic": _mean(non_overlapping_ics),
        "mean_independent_ic": _mean(non_overlapping_ics),
        "mean_non_overlapping_top_excess_net": _mean(non_overlapping_returns),
        "mean_independent_top_excess_net": _mean(non_overlapping_returns),
        "non_overlapping_top_excess_hit_rate": _hit_rate(
            non_overlapping_returns
        ),
        "independent_top_excess_hit_rate": _hit_rate(non_overlapping_returns),
        "mean_non_overlapping_top_minus_cohort_net": _mean(
            non_overlapping_rank_vs_cohort
        ),
        "mean_independent_top_minus_cohort_net": _mean(
            non_overlapping_rank_vs_cohort
        ),
        "non_overlapping_top_minus_cohort_hit_rate": _hit_rate(
            non_overlapping_rank_vs_cohort
        ),
        "independent_top_minus_cohort_hit_rate": _hit_rate(
            non_overlapping_rank_vs_cohort
        ),
        "mean_non_overlapping_top_minus_bottom_gross": _mean(
            non_overlapping_top_bottom
        ),
        "mean_independent_top_minus_bottom_gross": _mean(
            non_overlapping_top_bottom
        ),
        "independent_top_minus_bottom_hit_rate": _hit_rate(
            non_overlapping_top_bottom
        ),
        "max_drawdown": max_drawdown(non_overlapping_returns),
        "average_turnover": _mean(turnovers),
        "average_independent_turnover": _mean(independent_turnovers),
        "invalid_execution_interval_cross_section_count": len(
            invalid_execution_interval_cross_sections
        ),
        "invalid_execution_interval_cross_sections": [
            dict(row) for row in invalid_execution_interval_cross_sections
        ],
        "early_terminal_observation_count": early_terminal_observation_count,
        "late_security_entry_observation_count": (
            late_security_entry_observation_count
        ),
        "terminal_proceeds_policy": (
            "terminal_proceeds_cash_carry_to_benchmark_exit_zero_return"
        ),
        "late_security_entry_policy": (
            "cash_carry_from_benchmark_entry_to_security_entry_zero_return"
        ),
        "independent_intervals": [
            {
                "asof_date": str(row.get("asof_date") or ""),
                "entry_date": str(row.get("entry_date") or ""),
                "exit_date": str(row.get("exit_date") or ""),
                "evaluation_available_flag": int(
                    float(str(row.get("evaluation_available_flag") or 0))
                ),
            }
            for row in (
                independent_schedule_rows
                if independent_schedule_rows is not None
                else independent_rows
            )
        ],
    }


def _benchmark_interval_contract(
    rows: Sequence[Mapping[str, object]],
    *,
    asof: str,
    strict: bool,
) -> tuple[str, str, int, int, list[str]]:
    entries = {
        str(
            row.get("benchmark_entry_date")
            or ("" if strict else row.get("entry_date") or asof)
        ).strip()[:10]
        for row in rows
    }
    exits = {
        str(
            row.get("benchmark_exit_date")
            or ("" if strict else row.get("exit_date"))
        ).strip()[:10]
        for row in rows
    }
    errors: list[str] = []
    if "" in entries or len(entries) != 1:
        errors.append("benchmark_entry_date_missing_or_nonunique")
    if "" in exits or len(exits) != 1:
        errors.append("benchmark_exit_date_missing_or_nonunique")
    if errors:
        return "", "", 0, 0, errors
    entry = next(iter(entries))
    exit_date = next(iter(exits))
    try:
        parsed_asof = parse_date(asof, field="asof_date")
        parsed_entry = parse_date(entry, field="benchmark_entry_date")
        parsed_exit = parse_date(exit_date, field="benchmark_exit_date")
    except ValueError:
        return "", "", 0, 0, ["benchmark_interval_invalid_date"]
    if not (parsed_asof < parsed_entry <= parsed_exit):
        errors.append("benchmark_interval_order_invalid")
    if not strict:
        return entry, exit_date, 0, 0, errors

    early_terminal_count = 0
    late_security_entry_count = 0
    benchmark_returns: list[float] = []
    allowed_terminal_types = {
        "acquisition",
        "distressed_nonzero",
        "wipeout",
    }
    for row in rows:
        forward = finite_float(row.get("forward_excess_return"))
        available_flag = str(row.get("outcome_available_flag") or "")
        if available_flag == "1" and forward is None:
            errors.append("available_outcome_missing_forward_excess_return")
            continue
        if forward is None:
            continue
        if available_flag and available_flag != "1":
            errors.append("forward_return_present_but_outcome_not_available")
        security_return = finite_float(row.get("security_forward_return"))
        benchmark_return = finite_float(row.get("benchmark_forward_return"))
        if security_return is None or benchmark_return is None:
            errors.append("forward_return_components_missing")
            continue
        if not math.isclose(
            security_return - benchmark_return,
            forward,
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            errors.append("forward_excess_return_arithmetic_mismatch")
        benchmark_returns.append(benchmark_return)
        security_entry_raw = str(row.get("entry_date") or "").strip()[:10]
        security_exit_raw = str(row.get("exit_date") or "").strip()[:10]
        try:
            security_entry = parse_date(
                security_entry_raw,
                field="security entry_date",
            )
            security_exit = parse_date(
                security_exit_raw,
                field="security exit_date",
            )
        except ValueError:
            errors.append("security_execution_interval_missing_or_invalid")
            continue
        if security_entry < parsed_entry:
            errors.append("security_entry_before_benchmark_entry")
        elif security_entry > parsed_entry:
            late_security_entry_count += 1
        if security_entry > security_exit:
            errors.append("security_entry_after_security_exit")
        if security_exit > parsed_exit:
            errors.append("security_exit_after_benchmark_horizon")
        if security_exit < parsed_exit:
            if (
                str(row.get("outcome_method") or "")
                != "terminal_membership_exit"
                or str(row.get("terminal_type") or "")
                not in allowed_terminal_types
            ):
                errors.append("early_security_exit_without_terminal_contract")
            else:
                early_terminal_count += 1
    if benchmark_returns and max(benchmark_returns) - min(benchmark_returns) > 1e-9:
        errors.append("benchmark_return_nonunique_within_cross_section")
    return (
        entry,
        exit_date,
        early_terminal_count,
        late_security_entry_count,
        sorted(set(errors)),
    )


def evaluate_candidate(
    rows: Sequence[Mapping[str, object]],
    *,
    weights: Mapping[str, float],
    split: str,
    horizon_sessions: int = 63,
    top_fraction: float = 0.20,
    minimum_cross_section: int = 5,
    transaction_cost_bps: float = 20.0,
    require_complete_components: bool = False,
    require_unique_benchmark_interval: bool = False,
) -> dict[str, object]:
    by_date: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    eligible_rows = 0
    available_rows = 0
    for row in rows:
        if str(row.get("split") or "") != split:
            continue
        if int(float(str(row.get("horizon_sessions") or 0))) != horizon_sessions:
            continue
        if str(row.get("calibration_eligible_flag") or "") != "1":
            continue
        eligible_rows += 1
        score = weighted_score(
            row,
            weights,
            require_complete=require_complete_components,
        )
        outcome = finite_float(row.get("forward_excess_return"))
        if score is not None and outcome is not None:
            available_rows += 1
        by_date[str(row.get("asof_date") or "")].append(row)

    interval_failures: list[dict[str, object]] = []
    ranked_records: list[dict[str, object]] = []
    total_early_terminal = 0
    total_late_security_entry = 0
    for asof in sorted(by_date):
        date_rows = by_date[asof]
        (
            entry,
            exit_date,
            terminal_count,
            late_entry_count,
            errors,
        ) = _benchmark_interval_contract(
            date_rows,
            asof=asof,
            strict=require_unique_benchmark_interval,
        )
        tickers = [str(row.get("ticker") or "") for row in date_rows]
        if not tickers or len(tickers) != len(set(tickers)):
            errors.append("ticker_identity_missing_or_duplicated")
        if errors:
            interval_failures.append(
                {
                    "asof_date": asof,
                    "horizon_sessions": horizon_sessions,
                    "eligible_row_count": len(date_rows),
                    "reasons": sorted(set(errors)),
                    "tickers": sorted(
                        str(row.get("ticker") or "") for row in date_rows
                    ),
                    "outcome_unavailable_reasons": sorted(
                        {
                            str(
                                row.get("outcome_unavailable_reason") or ""
                            )
                            for row in date_rows
                            if str(
                                row.get("outcome_unavailable_reason") or ""
                            )
                        }
                    ),
                    "right_censored_at_panel_end_flag": int(
                        bool(date_rows)
                        and all(
                            str(
                                row.get("outcome_unavailable_reason") or ""
                            )
                            in {
                                "execution_window_crosses_data_end",
                                "missing_d1_open_execution_price",
                            }
                            for row in date_rows
                        )
                    ),
                }
            )
            continue
        total_early_terminal += terminal_count
        total_late_security_entry += late_entry_count
        scored: list[tuple[Mapping[str, object], str, float]] = []
        for row in date_rows:
            score = weighted_score(
                row,
                weights,
                require_complete=require_complete_components,
            )
            if score is not None:
                scored.append((row, str(row.get("ticker") or ""), score))
        if len(scored) < minimum_cross_section:
            continue
        ranked = sorted(scored, key=lambda item: (-item[2], item[1]))
        sleeve_count = max(1, math.ceil(len(ranked) * top_fraction))
        selected = ranked[:sleeve_count]
        bottom = ranked[-sleeve_count:]
        selected_tickers = {item[1] for item in selected}
        bottom_tickers = {item[1] for item in bottom}
        outcomes = {
            item[1]: finite_float(item[0].get("forward_excess_return"))
            for item in ranked
        }
        complete_outcomes = all(value is not None for value in outcomes.values())
        record: dict[str, object] = {
            "asof_date": asof,
            "entry_date": entry,
            "exit_date": exit_date,
            "eligible_row_count": len(date_rows),
            "available_outcome_row_count": sum(
                value is not None for value in outcomes.values()
            ),
            "cross_section": len(ranked),
            "selected": len(selected),
            "selected_tickers": selected_tickers,
            "bottom_tickers": bottom_tickers,
            "early_terminal_observation_count": terminal_count,
            "late_security_entry_observation_count": late_entry_count,
            "evaluation_available_flag": int(complete_outcomes),
        }
        if complete_outcomes:
            numeric = {
                ticker: float(value)
                for ticker, value in outcomes.items()
                if value is not None
            }
            gross = _mean([numeric[item[1]] for item in selected])
            cohort_excess = _mean(list(numeric.values()))
            bottom_excess = _mean([numeric[item[1]] for item in bottom])
            assert gross is not None
            assert cohort_excess is not None
            assert bottom_excess is not None
            record.update(
                ic=spearman(
                    [item[2] for item in ranked],
                    [numeric[item[1]] for item in ranked],
                ),
                gross_excess=gross,
                cohort_excess=cohort_excess,
                bottom_excess=bottom_excess,
                top_minus_cohort_gross=gross - cohort_excess,
                top_minus_bottom_gross=gross - bottom_excess,
            )
        ranked_records.append(record)

    period_rows: list[dict[str, object]] = []
    previous: set[str] | None = None
    cost_rate = transaction_cost_bps / 10000.0
    for record in sorted(ranked_records, key=lambda item: str(item["asof_date"])):
        selected_tickers = set(record["selected_tickers"])
        turnover = _one_way_turnover(selected_tickers, previous)
        previous = selected_tickers
        if int(record["evaluation_available_flag"]) != 1:
            continue
        gross = float(record["gross_excess"])
        cohort_excess = float(record["cohort_excess"])
        period_rows.append(
            {
                **record,
                "selected_tickers": sorted(selected_tickers),
                "bottom_tickers": sorted(set(record["bottom_tickers"])),
                "turnover": turnover,
                "net_excess": gross - turnover * cost_rate,
                "top_minus_cohort_net": (
                    gross - cohort_excess - turnover * cost_rate
                ),
            }
        )

    independent_records: list[dict[str, object]] = []
    last_exit: date | None = None
    for record in sorted(
        ranked_records,
        key=lambda item: (
            str(item["entry_date"]),
            str(item["asof_date"]),
        ),
    ):
        entry = parse_date(record["entry_date"], field="benchmark_entry_date")
        exit_date = parse_date(record["exit_date"], field="benchmark_exit_date")
        if last_exit is not None and entry <= last_exit:
            continue
        independent_records.append(record)
        last_exit = exit_date

    independent_schedule: list[dict[str, object]] = []
    independent_previous: set[str] | None = None
    for sequence, record in enumerate(independent_records, start=1):
        selected_tickers = set(record["selected_tickers"])
        turnover = _one_way_turnover(selected_tickers, independent_previous)
        independent_previous = selected_tickers
        schedule_row = {
            **record,
            "independent_sequence": sequence,
            "selected_tickers": sorted(selected_tickers),
            "bottom_tickers": sorted(set(record["bottom_tickers"])),
            "turnover": turnover,
        }
        if int(record["evaluation_available_flag"]) == 1:
            gross = float(record["gross_excess"])
            cohort_excess = float(record["cohort_excess"])
            schedule_row.update(
                net_excess=gross - turnover * cost_rate,
                top_minus_cohort_net=(
                    gross - cohort_excess - turnover * cost_rate
                ),
            )
        independent_schedule.append(schedule_row)

    independent_eligible = sum(
        int(record["eligible_row_count"]) for record in independent_records
    )
    independent_available = sum(
        int(record["available_outcome_row_count"])
        for record in independent_records
    )
    summary = summarize_candidate_period_rows(
        period_rows,
        eligible_row_count=eligible_rows,
        available_outcome_row_count=available_rows,
        independent_schedule_rows=independent_schedule,
        independent_eligible_row_count=independent_eligible,
        independent_available_outcome_row_count=independent_available,
        invalid_execution_interval_cross_sections=interval_failures,
        early_terminal_observation_count=total_early_terminal,
        late_security_entry_observation_count=total_late_security_entry,
    )
    return {
        "split": split,
        "horizon_sessions": horizon_sessions,
        **summary,
        "period_rows": period_rows,
        "independent_schedule_rows": independent_schedule,
    }


def artifact_sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
