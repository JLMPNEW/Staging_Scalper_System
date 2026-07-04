#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import sys
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.db import connect, finish_run, init_db, start_run  # noqa: E402
from industrials.core.logging_utils import configure_utc_logging  # noqa: E402
from industrials.core.reports import write_csv_atomic  # noqa: E402
from industrials.core.text_norm import normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("audit_industrials_market_data_policy")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
RUN_TYPE = "audit_industrials_market_data_policy"
FIELDNAMES = [
    "ticker",
    "company_name",
    "model_family",
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
# Default severity per review-reason key; policy violations fail the audit unless a
# config override under market_data_audit.severity_overrides explicitly downgrades them.
DEFAULT_REASON_SEVERITY = {
    "no_price_bars": "failed",
    "stale_latest_bar": "failed",
    "future_bar": "failed",
    "no_adjusted_close": "failed",
    "low_history": "review",
}
ALLOWED_SEVERITIES = {"failed", "review"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit selected industrials market-data coverage.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--model-family", default="", help="Industrials model family to audit, e.g. defense.")
    parser.add_argument("--benchmark-tickers", default="", help="Optional comma-separated benchmark ticker override.")
    parser.add_argument("--asof", default="", help="Audit as of this YYYY-MM-DD date. Defaults to the last expected trading day from today.")
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


def parse_asof_arg(raw: str) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"Unparseable --asof value: {raw!r} (expected YYYY-MM-DD)") from exc


def last_expected_trading_day(day: date) -> date:
    """Weekend-aware expected latest bar date.

    This intentionally avoids a full exchange-calendar dependency. Exchange
    holidays may still re-fetch once, which is acceptable; weekends should not.
    """
    expected = day
    while expected.weekday() >= 5:
        expected -= timedelta(days=1)
    return expected


def require_cfg(config: dict[str, Any], key: str) -> Any:
    value = cfg_get(config, key, None)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise KeyError(f"Required config key missing or empty: {key}")
    return value


def coerce_bool(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "y"}


def resolve_severity_map(config: dict[str, Any], *, require_adjusted: bool) -> dict[str, str]:
    severity_map = dict(DEFAULT_REASON_SEVERITY)
    if not require_adjusted:
        severity_map["no_adjusted_close"] = "review"
    overrides = cfg_get(config, "market_data_audit.severity_overrides", {}) or {}
    if not isinstance(overrides, dict):
        raise ValueError("market_data_audit.severity_overrides must be a mapping of reason -> severity")
    for raw_key, raw_value in overrides.items():
        key = str(raw_key).strip()
        severity = str(raw_value or "").strip().lower()
        if key not in severity_map:
            raise ValueError(f"Unknown market_data_audit.severity_overrides reason {key!r}; known reasons: {sorted(severity_map)}")
        if severity not in ALLOWED_SEVERITIES:
            raise ValueError(f"Invalid severity {raw_value!r} for market_data_audit.severity_overrides.{key}; allowed: {sorted(ALLOWED_SEVERITIES)}")
        severity_map[key] = severity
    return severity_map


def parse_ticker_list(raw: object) -> list[str]:
    values = raw if isinstance(raw, list) else str(raw or "").split(",")
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        ticker = normalize_ticker(value)
        if ticker and ticker not in seen:
            out.append(ticker)
            seen.add(ticker)
    return out


def load_jobs(conn: Any, *, model_family: str, benchmark_tickers: list[str]) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT DISTINCT c.ticker, c.company_name
        FROM dim_company c
        JOIN dim_industrials_taxonomy t
          ON t.ticker = c.ticker
         AND t.model_family = ?
        WHERE c.is_active = 1
        ORDER BY c.ticker
        """,
        (model_family,),
    ).fetchall()
    out = [
        {
            "ticker": str(row["ticker"]),
            "company_name": str(row["company_name"] or ""),
            "model_family": model_family,
            "is_benchmark": 0,
        }
        for row in rows
    ]
    seen = {normalize_ticker(row["ticker"]) for row in out}
    for raw in benchmark_tickers:
        ticker = normalize_ticker(raw)
        if ticker and ticker not in seen:
            out.append({"ticker": ticker, "company_name": ticker, "model_family": "benchmark", "is_benchmark": 1})
            seen.add(ticker)
    return out


def panel_max_bar_date(conn: Any, jobs: list[dict[str, Any]], *, source_id: str) -> date | None:
    tickers = [normalize_ticker(job["ticker"]) for job in jobs if normalize_ticker(job["ticker"])]
    if not tickers:
        return None
    placeholders = ",".join("?" for _ in tickers)
    row = conn.execute(
        f"""
        SELECT MAX(bar_date) AS max_bar_date
        FROM fact_price_ohlcv
        WHERE source_id = ?
          AND ticker IN ({placeholders})
        """,
        (source_id, *tickers),
    ).fetchone()
    return parse_date(row["max_bar_date"] if row is not None else "")


def audit_ticker(
    conn: Any,
    job: dict[str, Any],
    *,
    source_id: str,
    asof: date,
    max_staleness_days: int,
    min_days: int,
    severity_map: dict[str, str],
    check_future_bars: bool,
) -> dict[str, Any]:
    ticker = normalize_ticker(job["ticker"])
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS bar_count,
            SUM(CASE WHEN adj_close IS NOT NULL THEN 1 ELSE 0 END) AS adjusted_bar_count,
            MIN(bar_date) AS first_bar_date,
            MAX(bar_date) AS latest_bar_date
        FROM fact_price_ohlcv
        WHERE ticker = ? AND source_id = ? AND bar_date <= ?
        """,
        (ticker, source_id, asof.isoformat()),
    ).fetchone()
    bar_count = int(row["bar_count"] or 0)
    adjusted_count = int(row["adjusted_bar_count"] or 0)
    first_bar = str(row["first_bar_date"] or "")
    latest_bar = str(row["latest_bar_date"] or "")
    latest_adj_close = ""
    stale_days: int | str = ""
    status = "success"
    reasons: list[tuple[str, str]] = []
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
            stale_days = (asof - latest_date).days
            if int(stale_days) > max_staleness_days:
                reasons.append(("stale_latest_bar", f"stale_latest_bar_{stale_days}d"))
    else:
        reasons.append(("no_price_bars", "no_price_bars"))
    if check_future_bars:
        future_row = conn.execute(
            """
            SELECT MAX(bar_date) AS max_bar_date
            FROM fact_price_ohlcv
            WHERE ticker = ? AND source_id = ?
            """,
            (ticker, source_id),
        ).fetchone()
        future_date = parse_date(future_row["max_bar_date"] if future_row is not None else "")
        if future_date is not None and future_date > asof:
            reasons.append(("future_bar", f"future_bar_{future_date.isoformat()}"))
    if adjusted_count == 0:
        reasons.append(("no_adjusted_close", "no_adjusted_close"))
    if bar_count < min_days:
        reasons.append(("low_history", f"low_history_{bar_count}_bars"))
    if reasons:
        severities = {severity_map[key] for key, _ in reasons}
        status = "failed" if "failed" in severities else "review"
    return {
        "ticker": ticker,
        "company_name": job["company_name"],
        "model_family": job["model_family"],
        "is_benchmark": job["is_benchmark"],
        "source_id": source_id,
        "bar_count": bar_count,
        "adjusted_bar_count": adjusted_count,
        "first_bar_date": first_bar,
        "latest_bar_date": latest_bar,
        "latest_adj_close": latest_adj_close,
        "stale_days": stale_days,
        "status": status,
        "review_reason": ";".join(text for _, text in reasons),
    }


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_csv = args.output_csv.expanduser().resolve() if args.output_csv else resolve_path(cfg_get(config, "market_data_audit.output_csv"), base_dir=base_dir)
    requested_asof = parse_asof_arg(args.asof)
    model_family = str(args.model_family or cfg_get(config, "industrials_universe.initial_subsector", "defense") or "defense").strip()
    source_id = str(cfg_get(config, "market_data_policy.scoring_primary_source", "yahoo_finance_adjusted") or "yahoo_finance_adjusted")
    max_staleness_days = int(require_cfg(config, "market_data_policy.max_staleness_days"))
    min_days = int(require_cfg(config, "market_data_policy.min_trading_days_for_full_features"))
    require_adjusted = coerce_bool(require_cfg(config, "market_data_policy.require_adjusted_for_scoring"))
    severity_map = resolve_severity_map(config, require_adjusted=require_adjusted)
    benchmark_tickers = parse_ticker_list(args.benchmark_tickers) or parse_ticker_list(cfg_get(config, "industrials_universe.benchmark_tickers", []))

    with closing(connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0)))) as conn:
        init_db(conn)
        run_id = start_run(conn, run_type=RUN_TYPE, input_path=config_path)
        try:
            jobs = load_jobs(conn, model_family=model_family, benchmark_tickers=benchmark_tickers)
            panel_error = ""
            if requested_asof is not None:
                asof = requested_asof
            else:
                asof = last_expected_trading_day(date.today())
                LOGGER.info("No --asof supplied; anchoring staleness to last expected trading day: %s", asof.isoformat())
                panel_max = panel_max_bar_date(conn, jobs, source_id=source_id)
                if panel_max is None:
                    panel_error = f"No price bars found for source_id={source_id} across the audited panel"
                elif (asof - panel_max).days > max_staleness_days:
                    panel_error = (
                        f"Panel max bar date {panel_max.isoformat()} is {(asof - panel_max).days}d behind expected trading day "
                        f"{asof.isoformat()} (max_staleness_days={max_staleness_days})"
                    )
            rows = [
                audit_ticker(
                    conn,
                    job,
                    source_id=source_id,
                    asof=asof,
                    max_staleness_days=max_staleness_days,
                    min_days=min_days,
                    severity_map=severity_map,
                    check_future_bars=requested_asof is None,
                )
                for job in jobs
            ]
            failed = sum(1 for row in rows if row["status"] == "failed")
            review = sum(1 for row in rows if row["status"] == "review")
            status = "success" if failed == 0 else ("partial" if args.allow_partial else "failed")
            if panel_error:
                status = "failed"
            write_csv_atomic(output_csv, FIELDNAMES, rows)
            finish_run(
                conn,
                run_id=run_id,
                status=status,
                row_count=len(rows),
                message=f"rows={len(rows)} failed={failed} review={review} panel_error={panel_error or 'none'} output={output_csv}",
            )
            LOGGER.info("Wrote market-data audit report: %s", output_csv)
            LOGGER.info("Market-data audit complete: rows=%d failed=%d review=%d", len(rows), failed, review)
            if panel_error:
                LOGGER.error("Market-data panel staleness gate failed: %s", panel_error)
                raise SystemExit(1)
            if failed and not args.allow_partial:
                raise SystemExit(1)
        except BaseException as exc:
            if not isinstance(exc, SystemExit):
                finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()
