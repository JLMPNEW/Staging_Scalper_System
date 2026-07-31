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
) -> float | None:
    numerator = 0.0
    denominator = 0.0
    for field, weight in weights.items():
        value = finite_float(row.get(field))
        if value is None or weight <= 0:
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


def evaluate_candidate(
    rows: Sequence[Mapping[str, object]],
    *,
    weights: Mapping[str, float],
    split: str,
    horizon_sessions: int = 63,
    top_fraction: float = 0.20,
    minimum_cross_section: int = 5,
    transaction_cost_bps: float = 20.0,
) -> dict[str, object]:
    by_date: dict[str, list[tuple[str, float, float, str]]] = defaultdict(list)
    eligible_rows = 0
    available_rows = 0
    for row in rows:
        if str(row.get("split") or "") != split:
            continue
        if (
            int(float(str(row.get("horizon_sessions") or 0)))
            != horizon_sessions
        ):
            continue
        if str(row.get("calibration_eligible_flag") or "") != "1":
            continue
        eligible_rows += 1
        outcome = finite_float(row.get("forward_excess_return"))
        score = weighted_score(row, weights)
        if outcome is None or score is None:
            continue
        available_rows += 1
        by_date[str(row.get("asof_date") or "")].append(
            (
                str(row.get("ticker") or ""),
                score,
                outcome,
                str(row.get("benchmark_exit_date") or ""),
            )
        )
    period_rows: list[dict[str, object]] = []
    previous: set[str] = set()
    for asof in sorted(by_date):
        values = by_date[asof]
        if len(values) < minimum_cross_section:
            continue
        count = max(1, math.ceil(len(values) * top_fraction))
        selected = sorted(
            values,
            key=lambda item: (-item[1], item[0]),
        )[:count]
        selected_tickers = {item[0] for item in selected}
        turnover = (
            0.0
            if not previous
            else 1.0
            - len(previous & selected_tickers)
            / max(len(previous), len(selected_tickers))
        )
        gross = sum(item[2] for item in selected) / len(selected)
        net = gross - turnover * transaction_cost_bps / 10000.0
        ic = spearman(
            [item[1] for item in values],
            [item[2] for item in values],
        )
        period_rows.append(
            {
                "asof_date": asof,
                "exit_date": selected[0][3],
                "cross_section": len(values),
                "selected": len(selected),
                "ic": ic,
                "turnover": turnover,
                "gross_excess": gross,
                "net_excess": net,
            }
        )
        previous = selected_tickers
    net_returns = [
        float(item["net_excess"])
        for item in period_rows
    ]
    non_overlapping_returns: list[float] = []
    last_exit: date | None = None
    for item in period_rows:
        asof_date = parse_date(item["asof_date"])
        exit_date = parse_date(item["exit_date"])
        if last_exit is not None and asof_date <= last_exit:
            continue
        non_overlapping_returns.append(float(item["net_excess"]))
        last_exit = exit_date
    ics = [
        float(item["ic"])
        for item in period_rows
        if item["ic"] is not None
    ]
    turnovers = [
        float(item["turnover"])
        for item in period_rows
    ]
    return {
        "split": split,
        "horizon_sessions": horizon_sessions,
        "eligible_row_count": eligible_rows,
        "available_outcome_row_count": available_rows,
        "outcome_coverage": (
            available_rows / eligible_rows
            if eligible_rows
            else 0.0
        ),
        "snapshot_count": len(period_rows),
        "mean_ic": sum(ics) / len(ics) if ics else None,
        "mean_top_excess_net": (
            sum(net_returns) / len(net_returns)
            if net_returns
            else None
        ),
        "top_excess_hit_rate": (
            sum(value > 0 for value in net_returns) / len(net_returns)
            if net_returns
            else None
        ),
        "non_overlapping_snapshot_count": len(non_overlapping_returns),
        "mean_non_overlapping_top_excess_net": (
            sum(non_overlapping_returns) / len(non_overlapping_returns)
            if non_overlapping_returns else None
        ),
        "non_overlapping_top_excess_hit_rate": (
            sum(value > 0 for value in non_overlapping_returns)
            / len(non_overlapping_returns)
            if non_overlapping_returns else None
        ),
        "max_drawdown": max_drawdown(non_overlapping_returns),
        "average_turnover": (
            sum(turnovers) / len(turnovers)
            if turnovers
            else None
        ),
        "period_rows": period_rows,
    }


def artifact_sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
