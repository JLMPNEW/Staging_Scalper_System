#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import logging
import math
import statistics
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from technology.core.db import connect, finish_run, init_db, start_run, utc_now  # noqa: E402
from technology.core.logging_utils import configure_utc_logging  # noqa: E402
from technology.core.text_norm import normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("build_technology_market_features")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
RUN_TYPE = "build_technology_market_features"
FIELDNAMES = [
    "ticker",
    "asof_date",
    "source_id",
    "status",
    "trading_days_available",
    "latest_bar_date",
    "latest_adj_close",
    "ret_3m",
    "ret_12m_ex_1m",
    "avg_dollar_volume_60d",
    "low_liquidity_flag",
    "realized_vol_60d",
    "max_drawdown_12m",
    "distance_from_52w_high",
    "review_reason",
]


@dataclass(frozen=True)
class PriceRow:
    bar_date: date
    close: float
    adj_close: float
    volume: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build market and technical features for the technology universe.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--model-family", default="", help="Technology model family to build, e.g. semiconductors.")
    parser.add_argument("--benchmark-tickers", default="", help="Optional comma-separated benchmark ticker override.")
    parser.add_argument("--asof", default="", help="Feature as-of date. Defaults to latest available per ticker.")
    parser.add_argument("--output-csv", type=Path, default=None)
    return parser.parse_args()


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def safe_div(num: float | None, den: float | None) -> float | None:
    if num is None or den is None or den == 0:
        return None
    value = num / den
    return value if math.isfinite(value) else None


def pct_return(rows: list[PriceRow], end_idx: int, lookback: int) -> float | None:
    start_idx = end_idx - lookback
    if start_idx < 0:
        return None
    start = rows[start_idx].adj_close
    end = rows[end_idx].adj_close
    ratio = safe_div(end, start)
    return ratio - 1.0 if ratio is not None else None


def pct_return_between(rows: list[PriceRow], start_idx: int, end_idx: int) -> float | None:
    if start_idx < 0 or end_idx < 0 or start_idx >= len(rows) or end_idx >= len(rows) or end_idx <= start_idx:
        return None
    ratio = safe_div(rows[end_idx].adj_close, rows[start_idx].adj_close)
    return ratio - 1.0 if ratio is not None else None


def mean(values: list[float]) -> float | None:
    values = [value for value in values if math.isfinite(value)]
    return sum(values) / len(values) if values else None


def realized_vol(rows: list[PriceRow], end_idx: int, lookback: int) -> float | None:
    start_idx = max(1, end_idx - lookback + 1)
    if end_idx - start_idx + 1 < max(20, lookback // 2):
        return None
    returns: list[float] = []
    for idx in range(start_idx, end_idx + 1):
        prev = rows[idx - 1].adj_close
        cur = rows[idx].adj_close
        if prev > 0 and cur > 0:
            returns.append(math.log(cur / prev))
    if len(returns) < 20:
        return None
    return statistics.stdev(returns) * math.sqrt(252.0) if len(returns) > 1 else 0.0


def max_drawdown(rows: list[PriceRow], end_idx: int, lookback: int) -> float | None:
    start_idx = max(0, end_idx - lookback + 1)
    window = rows[start_idx : end_idx + 1]
    if len(window) < 20:
        return None
    peak = window[0].adj_close
    worst = 0.0
    for row in window:
        peak = max(peak, row.adj_close)
        if peak > 0:
            worst = min(worst, row.adj_close / peak - 1.0)
    return worst


def moving_average(rows: list[PriceRow], end_idx: int, lookback: int) -> float | None:
    start_idx = end_idx - lookback + 1
    if start_idx < 0:
        return None
    return mean([row.adj_close for row in rows[start_idx : end_idx + 1]])


def window_average(rows: list[PriceRow], end_idx: int, lookback: int, *, dollar: bool) -> float | None:
    start_idx = end_idx - lookback + 1
    if start_idx < 0:
        return None
    if dollar:
        return mean([row.adj_close * row.volume for row in rows[start_idx : end_idx + 1]])
    return mean([row.volume for row in rows[start_idx : end_idx + 1]])


def rel_strength(ticker_ret: float | None, bench_rows: list[PriceRow] | None, asof_date: date, lookback: int) -> float | None:
    if ticker_ret is None or not bench_rows:
        return None
    bench_ret = benchmark_return_asof(bench_rows, asof_date, lookback)
    return ticker_ret - bench_ret if bench_ret is not None else None


def distance_from_high(rows: list[PriceRow], end_idx: int, lookback: int) -> float | None:
    start_idx = max(0, end_idx - lookback + 1)
    window = rows[start_idx : end_idx + 1]
    if len(window) < 20:
        return None
    high = max(row.adj_close for row in window)
    return rows[end_idx].adj_close / high - 1.0 if high > 0 else None


def load_price_rows(conn: Any, ticker: str, source_id: str, asof: date | None) -> list[PriceRow]:
    params: list[Any] = [ticker, source_id]
    asof_clause = ""
    if asof is not None:
        asof_clause = "AND bar_date <= ?"
        params.append(asof.isoformat())
    db_rows = conn.execute(
        f"""
        SELECT bar_date, close, adj_close, volume
        FROM fact_price_ohlcv
        WHERE ticker = ? AND source_id = ? AND adj_close IS NOT NULL {asof_clause}
        ORDER BY bar_date
        """,
        tuple(params),
    ).fetchall()
    out: list[PriceRow] = []
    for row in db_rows:
        bar_date = parse_date(row["bar_date"])
        close = row["close"]
        adj_close = row["adj_close"]
        volume = row["volume"]
        if bar_date is None or close is None or adj_close is None:
            continue
        out.append(PriceRow(bar_date=bar_date, close=float(close), adj_close=float(adj_close), volume=float(volume or 0.0)))
    return out


def load_benchmark_rows(conn: Any, source_id: str, tickers: list[str], asof: date | None) -> dict[str, list[PriceRow]]:
    return {normalize_ticker(ticker): load_price_rows(conn, normalize_ticker(ticker), source_id, asof) for ticker in tickers}


def benchmark_return_asof(rows: list[PriceRow], asof_date: date, lookback: int) -> float | None:
    """Benchmark return over the same window as the ticker (ending at the ticker's latest bar)."""
    if not rows:
        return None
    idx = len(rows) - 1
    while idx >= 0 and rows[idx].bar_date > asof_date:
        idx -= 1
    if idx < 0:
        return None
    return pct_return(rows, idx, lookback)


def parse_ticker_list(raw: object) -> list[str]:
    if isinstance(raw, list):
        values = raw
    else:
        values = str(raw or "").split(",")
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        ticker = normalize_ticker(value)
        if ticker and ticker not in seen:
            out.append(ticker)
            seen.add(ticker)
    return out


def placeholders(values: list[str]) -> str:
    if not values:
        raise ValueError("values cannot be empty")
    return ",".join("?" for _ in values)


def load_universe(conn: Any, model_family: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT c.ticker
        FROM dim_company c
        JOIN dim_technology_taxonomy t
          ON t.ticker = c.ticker
         AND t.model_family = ?
        WHERE c.is_active = 1
        ORDER BY c.ticker
        """,
        (model_family,),
    ).fetchall()
    return [normalize_ticker(row["ticker"]) for row in rows if normalize_ticker(row["ticker"])]


def build_feature(
    ticker: str,
    rows: list[PriceRow],
    *,
    source_id: str,
    model_family: str,
    asof: date,
    max_staleness_days: int,
    min_days: int,
    min_avg_dollar_volume_60d: float,
    windows: dict[str, int],
    bench_rows: dict[str, list[PriceRow]],
) -> tuple[dict[str, Any], str]:
    if not rows:
        return {
            "ticker": ticker,
            "asof_date": asof.isoformat(),
            "source_id": source_id,
            "model_family": model_family,
            "trading_days_available": 0,
            "low_liquidity_flag": 1,
            "market_data_quality": "missing",
        }, "no_price_bars"
    end_idx = len(rows) - 1
    latest = rows[end_idx]
    stale_days = (asof - latest.bar_date).days
    ret_1m = pct_return(rows, end_idx, windows["one_month_days"])
    ret_3m = pct_return(rows, end_idx, windows["three_month_days"])
    ret_6m = pct_return(rows, end_idx, windows["six_month_days"])
    ret_12m_ex_1m = pct_return_between(
        rows,
        end_idx - windows["one_year_days"],
        end_idx - windows["skip_latest_month_days"],
    )
    ma_50d = moving_average(rows, end_idx, windows["ma_short_days"])
    ma_200d = moving_average(rows, end_idx, windows["ma_long_days"])
    avg_volume_20d = window_average(rows, end_idx, 20, dollar=False)
    avg_volume_60d = window_average(rows, end_idx, 60, dollar=False)
    avg_dollar_volume_20d = window_average(rows, end_idx, 20, dollar=True)
    avg_dollar_volume_60d = window_average(rows, end_idx, 60, dollar=True)
    low_liquidity_flag = int(
        min_avg_dollar_volume_60d > 0
        and (
            avg_dollar_volume_60d is None
            or avg_dollar_volume_60d < min_avg_dollar_volume_60d
        )
    )
    reasons: list[str] = []
    if stale_days > max_staleness_days:
        reasons.append(f"stale_{stale_days}d")
    if len(rows) < min_days:
        reasons.append(f"low_history_{len(rows)}")
    if low_liquidity_flag:
        if avg_dollar_volume_60d is None:
            reasons.append("low_liquidity_60d_missing")
        else:
            reasons.append(f"low_liquidity_60d_{int(avg_dollar_volume_60d)}")
    if latest.adj_close <= 0:
        reasons.append("bad_latest_adj_close")
    quality = "complete" if not reasons else "review"
    feature = {
        "ticker": ticker,
        "asof_date": asof.isoformat(),
        "source_id": source_id,
        "model_family": model_family,
        "latest_close": latest.close,
        "latest_adj_close": latest.adj_close,
        "latest_volume": latest.volume,
        "trading_days_available": len(rows),
        "latest_bar_date": latest.bar_date.isoformat(),
        "stale_days": stale_days,
        "stale_flag": int(stale_days > max_staleness_days),
        "low_history_flag": int(len(rows) < min_days),
        "low_liquidity_flag": low_liquidity_flag,
        "ret_1m": ret_1m,
        "ret_3m": ret_3m,
        "ret_6m": ret_6m,
        "ret_12m_ex_1m": ret_12m_ex_1m,
        "rel_strength_smh_3m": rel_strength(ret_3m, bench_rows.get("SMH"), latest.bar_date, windows["three_month_days"]),
        "rel_strength_soxx_3m": rel_strength(ret_3m, bench_rows.get("SOXX"), latest.bar_date, windows["three_month_days"]),
        "rel_strength_qqq_3m": rel_strength(ret_3m, bench_rows.get("QQQ"), latest.bar_date, windows["three_month_days"]),
        "rel_strength_spy_3m": rel_strength(ret_3m, bench_rows.get("SPY"), latest.bar_date, windows["three_month_days"]),
        "avg_volume_20d": avg_volume_20d,
        "avg_volume_60d": avg_volume_60d,
        "avg_dollar_volume_20d": avg_dollar_volume_20d,
        "avg_dollar_volume_60d": avg_dollar_volume_60d,
        "realized_vol_60d": realized_vol(rows, end_idx, windows["volatility_days"]),
        "max_drawdown_6m": max_drawdown(rows, end_idx, windows["six_month_days"]),
        "max_drawdown_12m": max_drawdown(rows, end_idx, windows["one_year_days"]),
        "distance_from_52w_high": distance_from_high(rows, end_idx, windows["one_year_days"]),
        "ma_50d": ma_50d,
        "ma_200d": ma_200d,
        "above_ma_50d": int(ma_50d is not None and latest.adj_close > ma_50d),
        "above_ma_200d": int(ma_200d is not None and latest.adj_close > ma_200d),
        "market_data_quality": quality,
    }
    return feature, ";".join(reasons)


def upsert_feature(conn: Any, feature: dict[str, Any]) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO feature_market_technical(
            ticker, asof_date, source_id, model_family, latest_close, latest_adj_close,
            latest_volume, trading_days_available, latest_bar_date, stale_days, stale_flag,
            low_history_flag, low_liquidity_flag, ret_1m, ret_3m, ret_6m, ret_12m_ex_1m,
            rel_strength_smh_3m, rel_strength_soxx_3m, rel_strength_qqq_3m,
            rel_strength_spy_3m, avg_volume_20d, avg_volume_60d,
            avg_dollar_volume_20d, avg_dollar_volume_60d, realized_vol_60d,
            max_drawdown_6m, max_drawdown_12m, distance_from_52w_high,
            ma_50d, ma_200d, above_ma_50d, above_ma_200d, market_data_quality,
            created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker, asof_date, source_id, model_family) DO UPDATE SET
            latest_close = excluded.latest_close,
            latest_adj_close = excluded.latest_adj_close,
            latest_volume = excluded.latest_volume,
            trading_days_available = excluded.trading_days_available,
            latest_bar_date = excluded.latest_bar_date,
            stale_days = excluded.stale_days,
            stale_flag = excluded.stale_flag,
            low_history_flag = excluded.low_history_flag,
            low_liquidity_flag = excluded.low_liquidity_flag,
            ret_1m = excluded.ret_1m,
            ret_3m = excluded.ret_3m,
            ret_6m = excluded.ret_6m,
            ret_12m_ex_1m = excluded.ret_12m_ex_1m,
            rel_strength_smh_3m = excluded.rel_strength_smh_3m,
            rel_strength_soxx_3m = excluded.rel_strength_soxx_3m,
            rel_strength_qqq_3m = excluded.rel_strength_qqq_3m,
            rel_strength_spy_3m = excluded.rel_strength_spy_3m,
            avg_volume_20d = excluded.avg_volume_20d,
            avg_volume_60d = excluded.avg_volume_60d,
            avg_dollar_volume_20d = excluded.avg_dollar_volume_20d,
            avg_dollar_volume_60d = excluded.avg_dollar_volume_60d,
            realized_vol_60d = excluded.realized_vol_60d,
            max_drawdown_6m = excluded.max_drawdown_6m,
            max_drawdown_12m = excluded.max_drawdown_12m,
            distance_from_52w_high = excluded.distance_from_52w_high,
            ma_50d = excluded.ma_50d,
            ma_200d = excluded.ma_200d,
            above_ma_50d = excluded.above_ma_50d,
            above_ma_200d = excluded.above_ma_200d,
            market_data_quality = excluded.market_data_quality,
            updated_at = excluded.updated_at
        """,
        (
            feature.get("ticker"),
            feature.get("asof_date"),
            feature.get("source_id"),
            feature.get("model_family"),
            feature.get("latest_close"),
            feature.get("latest_adj_close"),
            feature.get("latest_volume"),
            feature.get("trading_days_available"),
            feature.get("latest_bar_date"),
            feature.get("stale_days"),
            feature.get("stale_flag", 0),
            feature.get("low_history_flag", 0),
            feature.get("low_liquidity_flag", 0),
            feature.get("ret_1m"),
            feature.get("ret_3m"),
            feature.get("ret_6m"),
            feature.get("ret_12m_ex_1m"),
            feature.get("rel_strength_smh_3m"),
            feature.get("rel_strength_soxx_3m"),
            feature.get("rel_strength_qqq_3m"),
            feature.get("rel_strength_spy_3m"),
            feature.get("avg_volume_20d"),
            feature.get("avg_volume_60d"),
            feature.get("avg_dollar_volume_20d"),
            feature.get("avg_dollar_volume_60d"),
            feature.get("realized_vol_60d"),
            feature.get("max_drawdown_6m"),
            feature.get("max_drawdown_12m"),
            feature.get("distance_from_52w_high"),
            feature.get("ma_50d"),
            feature.get("ma_200d"),
            feature.get("above_ma_50d"),
            feature.get("above_ma_200d"),
            feature.get("market_data_quality"),
            now,
            now,
        ),
    )


def add_issue(conn: Any, ticker: str, detail: str) -> None:
    now = utc_now()
    row = conn.execute("SELECT company_id FROM dim_company WHERE ticker = ?", (ticker,)).fetchone()
    company_id = int(row["company_id"]) if row is not None else None
    conn.execute(
        """
        INSERT INTO data_quality_issues(
            detected_at, severity, stage, ticker, company_id, source_id, issue_type,
            issue_detail, resolution_status, created_at, updated_at
        )
        VALUES (?, 'warning', ?, ?, ?, ?, 'market_feature_review', ?, 'open', ?, ?)
        """,
        (now, RUN_TYPE, ticker, company_id, "yahoo_finance_adjusted", detail, now, now),
    )


def write_report(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(cfg_get(config, "market_feature_build.output_csv"), base_dir=base_dir)
    )
    source_id = str(cfg_get(config, "market_feature_build.source_id", "yahoo_finance_adjusted") or "yahoo_finance_adjusted")
    model_family = str(
        args.model_family
        or cfg_get(config, "technology_universe.initial_subsector", "semiconductors")
        or "semiconductors"
    ).strip()
    if not model_family:
        raise ValueError("model_family cannot be empty")
    max_staleness_days = int(cfg_get(config, "market_data_policy.max_staleness_days", 7))
    min_days = int(cfg_get(config, "market_feature_build.min_trading_days_for_full_features", 252))
    min_avg_dollar_volume_60d = float(
        cfg_get(
            config,
            "market_feature_build.min_avg_dollar_volume_60d_for_full_features",
            cfg_get(config, "market_data_policy.min_avg_dollar_volume_60d_for_full_features", 0),
        )
        or 0
    )
    benchmark_tickers = parse_ticker_list(args.benchmark_tickers) or parse_ticker_list(
        cfg_get(config, "technology_universe.benchmark_tickers", [])
    )
    windows = {
        "one_month_days": int(cfg_get(config, "market_feature_build.windows.one_month_days", 21)),
        "three_month_days": int(cfg_get(config, "market_feature_build.windows.three_month_days", 63)),
        "six_month_days": int(cfg_get(config, "market_feature_build.windows.six_month_days", 126)),
        "one_year_days": int(cfg_get(config, "market_feature_build.windows.one_year_days", 252)),
        "skip_latest_month_days": int(cfg_get(config, "market_feature_build.windows.skip_latest_month_days", 21)),
        "volatility_days": int(cfg_get(config, "market_feature_build.windows.volatility_days", 60)),
        "ma_short_days": int(cfg_get(config, "market_feature_build.windows.ma_short_days", 50)),
        "ma_long_days": int(cfg_get(config, "market_feature_build.windows.ma_long_days", 200)),
    }
    asof = parse_date(args.asof)

    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        run_id = start_run(conn, run_type=RUN_TYPE, input_path=config_path)
        try:
            tickers = load_universe(conn, model_family)
            if not tickers:
                raise ValueError(f"No active technology universe tickers found for model_family={model_family}.")
            effective_asof = asof
            if effective_asof is None:
                all_symbols = sorted(set(tickers + benchmark_tickers))
                ph = placeholders(all_symbols)
                row = conn.execute(
                    f"SELECT MAX(bar_date) AS max_date FROM fact_price_ohlcv WHERE source_id = ? AND ticker IN ({ph})",
                    (source_id, *all_symbols),
                ).fetchone()
                effective_asof = parse_date(row["max_date"] if row is not None else "")
            if effective_asof is None:
                raise ValueError(f"No price bars found for source_id={source_id}")
            bench_rows = load_benchmark_rows(conn, source_id, benchmark_tickers, effective_asof)
            report_rows: list[dict[str, Any]] = []
            review_count = 0
            with conn:
                ph_tickers = placeholders(tickers)
                conn.execute(
                    f"DELETE FROM data_quality_issues WHERE stage = ? AND ticker IN ({ph_tickers})",
                    (RUN_TYPE, *tickers),
                )
                for ticker in tickers:
                    rows = load_price_rows(conn, ticker, source_id, effective_asof)
                    feature, review_reason = build_feature(
                        ticker,
                        rows,
                        source_id=source_id,
                        model_family=model_family,
                        asof=effective_asof,
                        max_staleness_days=max_staleness_days,
                        min_days=min_days,
                        min_avg_dollar_volume_60d=min_avg_dollar_volume_60d,
                        windows=windows,
                        bench_rows=bench_rows,
                    )
                    upsert_feature(conn, feature)
                    if review_reason:
                        review_count += 1
                        add_issue(conn, ticker, review_reason)
                    report_rows.append(
                        {
                            "ticker": ticker,
                            "asof_date": effective_asof.isoformat(),
                            "source_id": source_id,
                            "status": "review" if review_reason else "success",
                            "trading_days_available": feature.get("trading_days_available", 0),
                            "latest_bar_date": feature.get("latest_bar_date", ""),
                            "latest_adj_close": feature.get("latest_adj_close", ""),
                            "ret_3m": feature.get("ret_3m", ""),
                            "ret_12m_ex_1m": feature.get("ret_12m_ex_1m", ""),
                            "avg_dollar_volume_60d": feature.get("avg_dollar_volume_60d", ""),
                            "low_liquidity_flag": feature.get("low_liquidity_flag", 0),
                            "realized_vol_60d": feature.get("realized_vol_60d", ""),
                            "max_drawdown_12m": feature.get("max_drawdown_12m", ""),
                            "distance_from_52w_high": feature.get("distance_from_52w_high", ""),
                            "review_reason": review_reason,
                        }
                    )
            write_report(output_csv, report_rows)
            finish_run(
                conn,
                run_id=run_id,
                status="success",
                row_count=len(report_rows),
                message=f"asof={effective_asof.isoformat()} rows={len(report_rows)} review={review_count} output={output_csv}",
            )
            LOGGER.info("Wrote market feature coverage report: %s", output_csv)
            LOGGER.info("Built market features: asof=%s rows=%d review=%d", effective_asof, len(report_rows), review_count)
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()
