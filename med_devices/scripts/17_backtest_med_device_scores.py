#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import math
import sys
from datetime import date, datetime
from pathlib import Path
from statistics import mean, median
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.db import connect, init_db  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402
from med_devices.core.market_policy import calibration_market_sources  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
BASE_FIELDS = [
    "asof_date",
    "scoring_model_version",
    "ticker",
    "company_name",
    "subsector",
    "rank",
    "classification",
    "decision_bucket",
    "entry_status",
    "composite_score",
    "raw_composite_score",
    "composite_percentile",
    "rank_bucket",
    "entry_price_date",
    "entry_price",
    "price_source_id",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest med-device daily score buckets against forward returns.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", type=str, default="")
    parser.add_argument("--horizons", type=str, default="30,60,120", help="Comma-separated trading-day forward horizons.")
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


def to_float(raw: object) -> float | None:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def latest_score_asof(conn: Any) -> str:
    row = conn.execute("SELECT MAX(asof_date) AS asof_date FROM med_device_daily_scores").fetchone()
    asof = str(row["asof_date"] or "") if row is not None else ""
    if not asof:
        raise RuntimeError("No med_device_daily_scores rows found; run script 13 first.")
    return asof


def load_scores(conn: Any, *, asof: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT s.*, c.ticker, c.company_name, c.subsector
        FROM med_device_daily_scores s
        JOIN dim_company c ON c.company_id = s.company_id
        WHERE s.asof_date = ?
        ORDER BY s.rank
        """,
        (asof,),
    ).fetchall()
    return [dict(row) for row in rows]


def rank_bucket(percentile: float) -> str:
    if percentile >= 90.0:
        return "top_decile"
    if percentile >= 80.0:
        return "top_quintile_ex_decile"
    if percentile <= 20.0:
        return "bottom_quintile"
    return "middle"


def load_price_series(
    conn: Any,
    *,
    tickers: list[str],
    source_priority: list[str],
) -> dict[str, tuple[str, list[tuple[date, float]]]]:
    if not tickers:
        return {}
    ticker_placeholders = ",".join("?" for _ in tickers)
    source_placeholders = ",".join("?" for _ in source_priority)
    rows = conn.execute(
        f"""
        SELECT ticker, bar_date, source_id, COALESCE(adj_close, close) AS price
        FROM fact_price_ohlcv
        WHERE ticker IN ({ticker_placeholders})
          AND source_id IN ({source_placeholders})
          AND COALESCE(adj_close, close) > 0
        ORDER BY ticker, source_id, bar_date
        """,
        [*tickers, *source_priority],
    ).fetchall()
    by_ticker_source: dict[tuple[str, str], list[tuple[date, float]]] = {}
    for row in rows:
        item_date = parse_date(row["bar_date"])
        price = to_float(row["price"])
        if item_date is None or price is None or price <= 0:
            continue
        ticker = str(row["ticker"] or "").upper()
        source_id = str(row["source_id"] or "").lower()
        by_ticker_source.setdefault((ticker, source_id), []).append((item_date, price))

    selected: dict[str, tuple[str, list[tuple[date, float]]]] = {}
    for ticker in tickers:
        for source_id in source_priority:
            series = by_ticker_source.get((ticker, source_id))
            if series:
                selected[ticker] = (source_id, series)
                break
    return selected


def entry_index(series: list[tuple[date, float]], asof_date: date) -> int | None:
    idx: int | None = None
    for pos, (bar_date, _) in enumerate(series):
        if bar_date <= asof_date:
            idx = pos
        else:
            break
    return idx


def build_backtest_rows(
    score_rows: list[dict[str, Any]],
    price_series: dict[str, tuple[str, list[tuple[date, float]]]],
    *,
    asof: str,
    horizons: list[int],
) -> list[dict[str, Any]]:
    asof_date = parse_date(asof)
    if asof_date is None:
        raise ValueError(f"Invalid asof date: {asof}")
    out: list[dict[str, Any]] = []
    for row in score_rows:
        ticker = str(row["ticker"] or "").upper()
        source_id, series = price_series.get(ticker, ("", []))
        idx = entry_index(series, asof_date) if series else None
        composite_score = float(row["composite_score"] or 0.0)
        raw_composite_score = float(row.get("raw_composite_score") or composite_score)
        composite_percentile = float(row.get("composite_percentile") or composite_score)
        item = {
            "asof_date": asof,
            "scoring_model_version": row.get("scoring_model_version") or "",
            "ticker": ticker,
            "company_name": row.get("company_name") or "",
            "subsector": row.get("subsector") or "",
            "rank": row.get("rank") or "",
            "classification": row.get("classification") or "",
            "decision_bucket": row.get("decision_bucket") or "",
            "entry_status": row.get("entry_status") or "",
            "composite_score": composite_score,
            "raw_composite_score": raw_composite_score,
            "composite_percentile": composite_percentile,
            "rank_bucket": rank_bucket(composite_percentile),
            "entry_price_date": "",
            "entry_price": "",
            "price_source_id": source_id,
        }
        if idx is not None:
            entry_date, entry_price = series[idx]
            item["entry_price_date"] = entry_date.isoformat()
            item["entry_price"] = round(entry_price, 6)
            for horizon in horizons:
                target_idx = idx + horizon
                if target_idx < len(series):
                    target_date, target_price = series[target_idx]
                    item[f"forward_date_{horizon}d"] = target_date.isoformat()
                    item[f"forward_return_{horizon}d"] = round((target_price - entry_price) / entry_price, 6)
                else:
                    item[f"forward_date_{horizon}d"] = ""
                    item[f"forward_return_{horizon}d"] = ""
        else:
            for horizon in horizons:
                item[f"forward_date_{horizon}d"] = ""
                item[f"forward_return_{horizon}d"] = ""
        out.append(item)
    return out


def summarize(rows: list[dict[str, Any]], *, horizons: list[int]) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    group_specs = [
        ("classification", sorted({str(row["classification"]) for row in rows})),
        ("entry_status", sorted({str(row["entry_status"]) for row in rows})),
        ("rank_bucket", sorted({str(row["rank_bucket"]) for row in rows})),
    ]
    for group_name, group_values in group_specs:
        for group_value in group_values:
            group_rows = [row for row in rows if str(row.get(group_name)) == group_value]
            for horizon in horizons:
                values = [
                    float(row[f"forward_return_{horizon}d"])
                    for row in group_rows
                    if str(row.get(f"forward_return_{horizon}d") or "").strip()
                ]
                summary.append(
                    {
                        "group_type": group_name,
                        "group_value": group_value,
                        "horizon_days": horizon,
                        "count": len(values),
                        "mean_forward_return": round(mean(values), 6) if values else "",
                        "median_forward_return": round(median(values), 6) if values else "",
                        "hit_rate": round(sum(1 for value in values if value > 0) / len(values), 4) if values else "",
                    }
                )
    return summary


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def dated_output_dir(base_output_dir: Path, asof: str) -> Path:
    return base_output_dir if base_output_dir.name == asof else base_output_dir / asof


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    horizons = [int(item.strip()) for item in str(args.horizons or "30,60,120").split(",") if item.strip()]
    if not horizons or any(horizon <= 0 for horizon in horizons):
        raise ValueError("--horizons must contain positive integers")

    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        asof = args.asof.strip() or latest_score_asof(conn)
        output_csv = (
            args.output_csv.expanduser().resolve()
            if args.output_csv
            else dated_output_dir(
                resolve_path(
                    cfg_get(config, "scoring.review_pack_dir", "../output/med_devices_reports/score_review_pack"),
                    base_dir=base_dir,
                ),
                asof,
            )
            / "med_device_score_backtest.csv"
        )
        score_rows = load_scores(conn, asof=asof)
        if not score_rows:
            raise RuntimeError(f"No score rows found for {asof}")
        source_priority = calibration_market_sources(config)
        tickers = [str(row["ticker"] or "").upper() for row in score_rows]
        series = load_price_series(conn, tickers=tickers, source_priority=source_priority)
        rows = build_backtest_rows(score_rows, series, asof=asof, horizons=horizons)
        fieldnames = [
            *BASE_FIELDS,
            *[field for horizon in horizons for field in (f"forward_date_{horizon}d", f"forward_return_{horizon}d")],
        ]
        write_csv(output_csv, rows, fieldnames)
        summary_rows = summarize(rows, horizons=horizons)
        write_csv(
            output_csv.with_name(output_csv.stem + "_summary" + output_csv.suffix),
            summary_rows,
            ["group_type", "group_value", "horizon_days", "count", "mean_forward_return", "median_forward_return", "hit_rate"],
        )
        available = {
            horizon: sum(1 for row in rows if str(row.get(f"forward_return_{horizon}d") or "").strip())
            for horizon in horizons
        }
        print(f"backtest_csv={output_csv} asof={asof} rows={len(rows)} forward_counts={available}")


if __name__ == "__main__":
    main()
