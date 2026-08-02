from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Mapping, Sequence

from industrials.core.oos_research import (
    ExecutionPricePoint,
    finite_float,
    parse_date,
)


PRICE_SLICE_FIELDS = (
    "ticker",
    "source_id",
    "bar_date",
    "adjusted_open",
    "adjusted_close",
    "price_basis",
    "price_adjustment",
)


def _exact_float(value: float | None) -> str:
    return "" if value is None else format(float(value), ".17g")


def price_slice_rows(
    prices: Mapping[
        str,
        Mapping[str, Sequence[ExecutionPricePoint]],
    ],
    *,
    start_date: str,
    end_date: str,
) -> list[dict[str, str]]:
    """Serialize the exact bounded price observations available to OOS logic."""
    start = parse_date(start_date, field="price slice start")
    end = parse_date(end_date, field="price slice end")
    if end < start:
        raise ValueError("price slice end precedes start")
    rows: list[dict[str, str]] = []
    for ticker in sorted(prices):
        for source_id in sorted(prices[ticker]):
            for point in prices[ticker][source_id]:
                if not start <= point.bar_date <= end:
                    continue
                rows.append(
                    {
                        "ticker": str(ticker).upper(),
                        "source_id": str(source_id),
                        "bar_date": point.bar_date.isoformat(),
                        "adjusted_open": _exact_float(point.adjusted_open),
                        "adjusted_close": _exact_float(point.adjusted_close),
                        "price_basis": str(point.price_basis),
                        "price_adjustment": str(point.price_adjustment),
                    }
                )
    return rows


def prices_from_slice(
    rows: Sequence[Mapping[str, object]],
) -> dict[str, dict[str, list[ExecutionPricePoint]]]:
    """Rehydrate a frozen price slice and reject duplicate or invalid keys."""
    output: dict[str, dict[str, list[ExecutionPricePoint]]] = {}
    keys: set[tuple[str, str, date]] = set()
    for row in rows:
        ticker = str(row.get("ticker") or "").strip().upper()
        source_id = str(row.get("source_id") or "").strip()
        bar_date = parse_date(row.get("bar_date"), field="price bar date")
        key = (ticker, source_id, bar_date)
        if not ticker or not source_id:
            raise ValueError(f"invalid frozen price key={key}")
        if key in keys:
            raise ValueError(f"duplicate frozen price key={key}")
        keys.add(key)
        adjusted_open = finite_float(row.get("adjusted_open"))
        adjusted_close = finite_float(row.get("adjusted_close"))
        if adjusted_close is None or adjusted_close < 0:
            raise ValueError(f"invalid frozen adjusted close key={key}")
        if adjusted_open is not None and adjusted_open < 0:
            raise ValueError(f"invalid frozen adjusted open key={key}")
        output.setdefault(ticker, {}).setdefault(source_id, []).append(
            ExecutionPricePoint(
                bar_date=bar_date,
                adjusted_close=adjusted_close,
                adjusted_open=adjusted_open,
                source_id=source_id,
                price_basis=str(row.get("price_basis") or ""),
                price_adjustment=str(row.get("price_adjustment") or ""),
            )
        )
    for by_source in output.values():
        for points in by_source.values():
            points.sort(key=lambda point: point.bar_date)
    return output


def audit_panel_return_lineage(
    panel_rows: Sequence[Mapping[str, object]],
    price_rows: Sequence[Mapping[str, object]],
    *,
    tolerance: float = 1e-9,
) -> dict[str, object]:
    """Independently rebuild available panel returns from the frozen price slice."""
    prices = prices_from_slice(price_rows)
    lookup: dict[tuple[str, str, str], ExecutionPricePoint] = {}
    session_indices: dict[tuple[str, str], dict[str, int]] = {}
    for ticker, by_source in prices.items():
        for source_id, points in by_source.items():
            session_indices[(ticker, source_id)] = {
                point.bar_date.isoformat(): index
                for index, point in enumerate(points)
            }
            for point in points:
                lookup[(ticker, source_id, point.bar_date.isoformat())] = point

    issues: list[str] = []
    counters: defaultdict[str, int] = defaultdict(int)
    max_error = 0.0

    def compare(
        *,
        key: tuple[str, str, str],
        field: str,
        observed: object,
        expected: float,
    ) -> None:
        nonlocal max_error
        parsed = finite_float(observed)
        if parsed is None:
            issues.append(f"{key}: {field} is missing")
            return
        error = abs(parsed - expected)
        max_error = max(max_error, error)
        if error > tolerance:
            issues.append(
                f"{key}: {field} mismatch observed={parsed} "
                f"expected={expected} error={error}"
            )

    for row in panel_rows:
        if str(row.get("outcome_available_flag") or "") != "1":
            continue
        key = (
            str(row.get("asof_date") or ""),
            str(row.get("ticker") or ""),
            str(row.get("horizon_sessions") or ""),
        )
        counters["available_rows"] += 1
        ticker = str(row.get("physical_price_ticker") or "").upper()
        source = str(row.get("security_price_source_id") or "")
        entry_date = str(row.get("entry_date") or "")
        exit_date = str(row.get("exit_date") or "")
        security_entry = lookup.get((ticker, source, entry_date))
        if security_entry is None or security_entry.adjusted_open is None:
            issues.append(f"{key}: frozen security entry observation missing")
            counters["missing_price_rows"] += 1
            continue
        compare(
            key=key,
            field="entry_adjusted_open",
            observed=row.get("entry_adjusted_open"),
            expected=security_entry.adjusted_open,
        )

        method = str(row.get("outcome_method") or "")
        if method == "scheduled_d1_open_to_open":
            counters["scheduled_rows"] += 1
            security_exit = lookup.get((ticker, source, exit_date))
            if security_exit is None or security_exit.adjusted_open is None:
                issues.append(f"{key}: frozen security exit observation missing")
                counters["missing_price_rows"] += 1
                continue
            security_exit_value = security_exit.adjusted_open
            indices = session_indices.get((ticker, source), {})
            entry_index = indices.get(entry_date)
            exit_index = indices.get(exit_date)
            horizon = int(float(str(row.get("horizon_sessions") or 0)))
            if (
                entry_index is None
                or exit_index is None
                or exit_index - entry_index != horizon
            ):
                issues.append(f"{key}: security session horizon mismatch")
        elif method == "terminal_membership_exit":
            counters["terminal_rows"] += 1
            terminal_type = str(row.get("terminal_type") or "")
            if terminal_type == "wipeout":
                security_exit_value = 0.0
            else:
                security_exit = lookup.get((ticker, source, exit_date))
                if security_exit is None:
                    issues.append(
                        f"{key}: frozen terminal security observation missing"
                    )
                    counters["missing_price_rows"] += 1
                    continue
                security_exit_value = security_exit.adjusted_close
        else:
            issues.append(f"{key}: unsupported outcome_method={method!r}")
            continue
        compare(
            key=key,
            field="exit_execution_value",
            observed=row.get("exit_execution_value"),
            expected=security_exit_value,
        )

        benchmark = str(row.get("benchmark_ticker") or "").upper()
        benchmark_source = str(row.get("benchmark_price_source_id") or "")
        benchmark_entry_date = str(row.get("benchmark_entry_date") or "")
        benchmark_exit_date = str(row.get("benchmark_exit_date") or "")
        benchmark_entry = lookup.get(
            (benchmark, benchmark_source, benchmark_entry_date)
        )
        benchmark_exit = lookup.get(
            (benchmark, benchmark_source, benchmark_exit_date)
        )
        if (
            benchmark_entry is None
            or benchmark_entry.adjusted_open is None
            or benchmark_exit is None
            or benchmark_exit.adjusted_open is None
        ):
            issues.append(f"{key}: frozen benchmark observations missing")
            counters["missing_price_rows"] += 1
            continue
        benchmark_indices = session_indices.get(
            (benchmark, benchmark_source), {}
        )
        horizon = int(float(str(row.get("horizon_sessions") or 0)))
        benchmark_entry_index = benchmark_indices.get(benchmark_entry_date)
        benchmark_exit_index = benchmark_indices.get(benchmark_exit_date)
        if (
            benchmark_entry_index is None
            or benchmark_exit_index is None
            or benchmark_exit_index - benchmark_entry_index != horizon
        ):
            issues.append(f"{key}: benchmark session horizon mismatch")

        security_return = (
            security_exit_value / security_entry.adjusted_open - 1.0
        )
        benchmark_return = (
            benchmark_exit.adjusted_open
            / benchmark_entry.adjusted_open
            - 1.0
        )
        excess_return = security_return - benchmark_return
        compare(
            key=key,
            field="security_forward_return",
            observed=row.get("security_forward_return"),
            expected=security_return,
        )
        compare(
            key=key,
            field="benchmark_forward_return",
            observed=row.get("benchmark_forward_return"),
            expected=benchmark_return,
        )
        compare(
            key=key,
            field="forward_excess_return",
            observed=row.get("forward_excess_return"),
            expected=excess_return,
        )
        counters["recomputed_rows"] += 1

    return {
        "acceptance": "PASS" if not issues else "FAIL",
        "available_row_count": counters["available_rows"],
        "recomputed_row_count": counters["recomputed_rows"],
        "scheduled_row_count": counters["scheduled_rows"],
        "terminal_row_count": counters["terminal_rows"],
        "missing_price_row_count": counters["missing_price_rows"],
        "maximum_absolute_error": max_error,
        "tolerance": tolerance,
        "issues": issues,
    }
