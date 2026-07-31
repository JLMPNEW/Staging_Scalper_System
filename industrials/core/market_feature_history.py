from __future__ import annotations

import bisect
import importlib.util
import sqlite3
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any

from industrials.core.reports import write_csv_atomic


@dataclass(frozen=True)
class MembershipSpell:
    start_date: date
    end_date: date | None


class PrefixRows(Sequence[Any]):
    """Zero-copy list prefix accepted by the shared market feature formulas."""

    def __init__(self, rows: list[Any], stop: int) -> None:
        self._rows = rows
        self._stop = max(0, min(int(stop), len(rows)))

    def __len__(self) -> int:
        return self._stop

    def __getitem__(self, key: int | slice) -> Any:
        if isinstance(key, slice):
            start, stop, step = key.indices(self._stop)
            return self._rows[start:stop:step]
        index = key + self._stop if key < 0 else key
        if index < 0 or index >= self._stop:
            raise IndexError(index)
        return self._rows[index]


def load_shared_market_module(project_root: Path) -> ModuleType:
    path = project_root / "industrials" / "scripts" / "05_build_industrials_market_features.py"
    name = "industrials_shared_market_feature_builder"
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def membership_spells(
    connection: sqlite3.Connection, *, model_family: str
) -> dict[str, list[MembershipSpell]]:
    result: dict[str, set[MembershipSpell]] = {}
    for row in connection.execute(
        """
        SELECT ticker, start_date, end_date
        FROM dim_universe_membership
        WHERE model_family=?
        ORDER BY ticker, start_date, end_date
        """,
        (model_family,),
    ).fetchall():
        ticker = str(row["ticker"] or "").strip().upper()
        if not ticker:
            continue
        result.setdefault(ticker, set()).add(
            MembershipSpell(
                start_date=date.fromisoformat(str(row["start_date"])[:10]),
                end_date=(
                    date.fromisoformat(str(row["end_date"])[:10])
                    if str(row["end_date"] or "")
                    else None
                ),
            )
        )
    return {ticker: sorted(spells, key=lambda item: item.start_date) for ticker, spells in result.items()}


def effective_members(
    spells_by_ticker: dict[str, list[MembershipSpell]], asof: date
) -> dict[str, MembershipSpell]:
    output: dict[str, MembershipSpell] = {}
    for ticker, spells in spells_by_ticker.items():
        effective = [
            item
            for item in spells
            if item.start_date <= asof
            and (item.end_date is None or item.end_date >= asof)
        ]
        if not effective:
            continue
        start = min(item.start_date for item in effective)
        end = None if any(item.end_date is None for item in effective) else max(
            item.end_date for item in effective if item.end_date is not None
        )
        output[ticker] = MembershipSpell(start_date=start, end_date=end)
    return output


def exact_market_tickers(
    connection: sqlite3.Connection, *, model_family: str, asof: str
) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            """
            SELECT DISTINCT ticker FROM feature_market_technical
            WHERE model_family=? AND asof_date=?
            """,
            (model_family, asof),
        ).fetchall()
    }


def prefix_length(rows: Sequence[Any], asof: date) -> int:
    return bisect.bisect_right([row.bar_date for row in rows], asof)


def select_source_prefix(
    series_by_source: dict[str, list[Any]],
    *,
    source_ids: Sequence[str],
    asof: date,
    minimum_bars: int,
) -> tuple[PrefixRows, str]:
    best_rows: list[Any] = []
    best_stop = 0
    best_source = str(source_ids[0]) if source_ids else ""
    best_key: tuple[int, date, int, int] | None = None
    for priority, source_id in enumerate(source_ids):
        rows = series_by_source.get(str(source_id), [])
        stop = prefix_length(rows, asof)
        if stop <= 0:
            continue
        key = (
            int(stop >= minimum_bars),
            rows[stop - 1].bar_date,
            stop,
            -priority,
        )
        if best_key is None or key > best_key:
            best_key = key
            best_rows = rows
            best_stop = stop
            best_source = str(source_id)
    return PrefixRows(best_rows, best_stop), best_source


def materialize_bulk_market_history(
    connection: sqlite3.Connection,
    *,
    shared: ModuleType,
    model_family: str,
    dates: Sequence[str],
    source_ids: Sequence[str],
    benchmark_source_ids: Sequence[str],
    benchmark_tickers: Sequence[str],
    primary_benchmark: str,
    secondary_benchmarks: Sequence[str],
    maximum_staleness_days: int,
    minimum_days: int,
    minimum_dollar_volume: float,
    minimum_source_bars: int,
    windows: dict[str, int],
    output_root: Path,
    report_path: Path,
    rebuild_existing: bool = False,
) -> dict[str, Any]:
    selected_dates = sorted(set(str(value)[:10] for value in dates if str(value)))
    if not selected_dates:
        raise ValueError("bulk market history requires at least one date")
    parsed_dates = [date.fromisoformat(value) for value in selected_dates]
    first_date, last_date = parsed_dates[0], parsed_dates[-1]
    spells = membership_spells(connection, model_family=model_family)
    all_tickers = sorted(spells)
    earliest_start = {
        ticker: min(item.start_date for item in ticker_spells)
        for ticker, ticker_spells in spells.items()
    }
    price_series: dict[str, dict[str, list[Any]]] = {}
    for ticker in all_tickers:
        price_series[ticker] = {
            str(source_id): shared.load_price_rows(
                connection,
                ticker,
                str(source_id),
                last_date,
                earliest_start[ticker],
            )
            for source_id in source_ids
        }
    benchmark_rows = shared.load_benchmark_rows(
        connection,
        list(benchmark_source_ids),
        list(benchmark_tickers),
        last_date,
        min_bars=minimum_source_bars,
    )
    if primary_benchmark and not benchmark_rows.get(primary_benchmark):
        raise ValueError(f"missing primary benchmark history={primary_benchmark}")

    report_rows: list[dict[str, Any]] = []
    failures: list[str] = []
    for asof in parsed_dates:
        started = time.monotonic()
        asof_text = asof.isoformat()
        members = effective_members(spells, asof)
        expected = set(members)
        output_path = output_root / asof_text / "market_features.csv"
        if (
            not rebuild_existing
            and exact_market_tickers(
                connection, model_family=model_family, asof=asof_text
            )
            == expected
            and output_path.is_file()
            and output_path.stat().st_size > 0
        ):
            report_rows.append(
                {
                    "asof_date": asof_text,
                    "expected_ticker_count": len(expected),
                    "market_feature_count": len(expected),
                    "status": "PASS",
                    "elapsed_seconds": 0.0,
                    "message": "valid_existing",
                }
            )
            continue
        feature_report: list[dict[str, Any]] = []
        try:
            source_placeholders = ",".join("?" for _ in source_ids)
            connection.execute(
                f"""
                DELETE FROM feature_market_technical
                WHERE model_family=? AND asof_date=?
                  AND source_id IN ({source_placeholders})
                """,
                (model_family, asof_text, *source_ids),
            )
            for ticker in sorted(members):
                member = members[ticker]
                rows, feature_source = select_source_prefix(
                    price_series.get(ticker, {}),
                    source_ids=source_ids,
                    asof=asof,
                    minimum_bars=minimum_source_bars,
                )
                feature, review_reason = shared.build_feature(
                    ticker,
                    rows,
                    source_id=feature_source,
                    model_family=model_family,
                    asof=asof,
                    membership_end=member.end_date,
                    max_staleness_days=maximum_staleness_days,
                    min_days=minimum_days,
                    min_avg_dollar_volume_60d=minimum_dollar_volume,
                    windows=windows,
                    bench_rows=benchmark_rows,
                    primary_benchmark=primary_benchmark,
                    secondary_benchmarks=list(secondary_benchmarks),
                )
                shared.upsert_feature(connection, feature)
                feature_report.append(
                    {
                        "ticker": ticker,
                        "asof_date": asof_text,
                        "source_id": feature_source,
                        "model_family": model_family,
                        "status": "review" if review_reason else "success",
                        "trading_days_available": feature.get("trading_days_available", 0),
                        "latest_bar_date": feature.get("latest_bar_date", ""),
                        "latest_adj_close": feature.get("latest_adj_close", ""),
                        "ret_3m": feature.get("ret_3m", ""),
                        "ret_12m_ex_1m": feature.get("ret_12m_ex_1m", ""),
                        "rel_strength_bench_3m": feature.get("rel_strength_bench_3m", ""),
                        "avg_dollar_volume_60d": feature.get("avg_dollar_volume_60d", ""),
                        "low_liquidity_flag": feature.get("low_liquidity_flag", 0),
                        "realized_vol_60d": feature.get("realized_vol_60d", ""),
                        "max_drawdown_12m": feature.get("max_drawdown_12m", ""),
                        "distance_from_52w_high": feature.get("distance_from_52w_high", ""),
                        "review_reason": review_reason,
                    }
                )
            connection.commit()
            shared.write_report(output_path, feature_report)
            actual = exact_market_tickers(
                connection, model_family=model_family, asof=asof_text
            )
            if actual != expected:
                raise ValueError(
                    f"market membership mismatch missing={sorted(expected-actual)[:20]} "
                    f"extra={sorted(actual-expected)[:20]}"
                )
            status = "PASS"
            message = ""
        except (OSError, ValueError, sqlite3.Error) as exc:
            connection.rollback()
            status = "FAIL"
            message = f"{type(exc).__name__}: {exc}"
            failures.append(f"{asof_text}:{message}")
        report_rows.append(
            {
                "asof_date": asof_text,
                "expected_ticker_count": len(expected),
                "market_feature_count": len(feature_report),
                "status": status,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "message": message,
            }
        )
        if failures:
            break
    write_csv_atomic(
        report_path,
        (
            "asof_date",
            "expected_ticker_count",
            "market_feature_count",
            "status",
            "elapsed_seconds",
            "message",
        ),
        report_rows,
    )
    return {
        "acceptance": "PASS" if not failures else "FAIL",
        "model_family": model_family,
        "start_date": first_date.isoformat(),
        "end_date": last_date.isoformat(),
        "selected_date_count": len(selected_dates),
        "completed_date_count": sum(row["status"] == "PASS" for row in report_rows),
        "price_series_loaded_once": True,
        "formula_source": str(Path(shared.__file__).resolve()),
        "errors": failures,
    }
