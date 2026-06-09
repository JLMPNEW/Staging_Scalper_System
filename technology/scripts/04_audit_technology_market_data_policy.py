#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import logging
import sys
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


LOGGER = logging.getLogger("audit_technology_market_data_policy")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
RUN_TYPE = "audit_technology_market_data_policy"
FIELDNAMES = [
    "ticker",
    "company_name",
    "is_benchmark",
    "source_id",
    "bar_count",
    "adjusted_bar_count",
    "first_bar_date",
    "latest_bar_date",
    "latest_adj_close",
    "stale_days",
    "status",
    "review_reason",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit selected technology market-data coverage.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", default="", help="Audit as of this YYYY-MM-DD date. Defaults to today.")
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--allow-partial", action="store_true")
    return parser.parse_args()


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def load_jobs(conn: Any, benchmark_tickers: list[str]) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT ticker, company_name, 0 AS is_benchmark
        FROM dim_company
        WHERE is_active = 1
        ORDER BY ticker
        """
    ).fetchall()
    out = [
        {"ticker": str(row["ticker"]), "company_name": str(row["company_name"] or ""), "is_benchmark": 0}
        for row in rows
    ]
    seen = {normalize_ticker(row["ticker"]) for row in out}
    for raw in benchmark_tickers:
        ticker = normalize_ticker(raw)
        if ticker and ticker not in seen:
            out.append({"ticker": ticker, "company_name": ticker, "is_benchmark": 1})
            seen.add(ticker)
    return out


def audit_ticker(conn: Any, job: dict[str, Any], *, source_id: str, asof: date, max_staleness_days: int, min_days: int) -> dict[str, Any]:
    ticker = normalize_ticker(job["ticker"])
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS bar_count,
            SUM(CASE WHEN adj_close IS NOT NULL THEN 1 ELSE 0 END) AS adjusted_bar_count,
            MIN(bar_date) AS first_bar_date,
            MAX(bar_date) AS latest_bar_date
        FROM fact_price_ohlcv
        WHERE ticker = ? AND source_id = ?
        """,
        (ticker, source_id),
    ).fetchone()
    bar_count = int(row["bar_count"] or 0)
    adjusted_count = int(row["adjusted_bar_count"] or 0)
    first_bar = str(row["first_bar_date"] or "")
    latest_bar = str(row["latest_bar_date"] or "")
    latest_adj_close = ""
    stale_days = ""
    status = "success"
    reasons: list[str] = []
    if latest_bar:
        latest_adj_row = conn.execute(
            """
            SELECT adj_close
            FROM fact_price_ohlcv
            WHERE ticker = ? AND source_id = ? AND bar_date = ?
            """,
            (ticker, source_id, latest_bar),
        ).fetchone()
        latest_adj_close = latest_adj_row["adj_close"] if latest_adj_row is not None else ""
        latest_date = parse_date(latest_bar)
        if latest_date is not None:
            stale_days_int = (asof - latest_date).days
            stale_days = stale_days_int
            if stale_days_int > max_staleness_days:
                reasons.append(f"stale_latest_bar_{stale_days_int}d")
    else:
        reasons.append("no_price_bars")
    if adjusted_count == 0:
        reasons.append("no_adjusted_close")
    if bar_count < min_days:
        reasons.append(f"low_history_{bar_count}_bars")
    if reasons:
        status = "review" if bar_count > 0 else "failed"
    return {
        "ticker": ticker,
        "company_name": job["company_name"],
        "is_benchmark": job["is_benchmark"],
        "source_id": source_id,
        "bar_count": bar_count,
        "adjusted_bar_count": adjusted_count,
        "first_bar_date": first_bar,
        "latest_bar_date": latest_bar,
        "latest_adj_close": latest_adj_close,
        "stale_days": stale_days,
        "status": status,
        "review_reason": ";".join(reasons),
    }


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
        else resolve_path(cfg_get(config, "market_data_audit.output_csv"), base_dir=base_dir)
    )
    asof = parse_date(args.asof) or date.today()
    source_id = str(cfg_get(config, "market_data_policy.scoring_primary_source", "yahoo_finance_adjusted") or "yahoo_finance_adjusted")
    max_staleness_days = int(cfg_get(config, "market_data_policy.max_staleness_days", 7))
    min_days = int(cfg_get(config, "market_data_policy.min_trading_days_for_full_features", 252))
    benchmark_tickers = [str(x) for x in cfg_get(config, "technology_universe.benchmark_tickers", [])]

    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        run_id = start_run(conn, run_type=RUN_TYPE, input_path=config_path)
        try:
            jobs = load_jobs(conn, benchmark_tickers)
            rows = [audit_ticker(conn, job, source_id=source_id, asof=asof, max_staleness_days=max_staleness_days, min_days=min_days) for job in jobs]
            failed = sum(1 for row in rows if row["status"] == "failed")
            review = sum(1 for row in rows if row["status"] == "review")
            status = "success" if failed == 0 else ("partial" if args.allow_partial else "failed")
            write_report(output_csv, rows)
            finish_run(
                conn,
                run_id=run_id,
                status=status,
                row_count=len(rows),
                message=f"rows={len(rows)} failed={failed} review={review} output={output_csv}",
            )
            LOGGER.info("Wrote market-data audit report: %s", output_csv)
            LOGGER.info("Market-data audit complete: rows=%d failed=%d review=%d", len(rows), failed, review)
            if failed and not args.allow_partial:
                raise SystemExit(1)
        except BaseException as exc:
            if not isinstance(exc, SystemExit):
                finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()
