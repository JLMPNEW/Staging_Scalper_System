#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.db import connect  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402
from med_devices.core.market_policy import (  # noqa: E402
    is_adjusted_price_row,
    price_adjustment_label,
    scoring_market_sources,
    select_latest_rows_by_source_priority,
)
from med_devices.core.text_norm import normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("audit_med_device_market_data_policy")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_INPUT = PROJECT_ROOT / "ticker_mapping" / "med_dev_tickers_clean_keep.csv"
FIELDNAMES = [
    "asof_date",
    "ticker",
    "selected_source",
    "selected_source_rank",
    "selected_bar_date",
    "selected_close",
    "selected_adj_close",
    "selected_price_adjustment",
    "selected_is_adjusted",
    "available_sources",
    "available_adjustments",
    "requires_adjusted_for_scoring",
    "policy_status",
    "review_reason",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit medical-device market-data source policy.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--input", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--asof", type=str, default="", help="Audit date in YYYY-MM-DD. Defaults to today.")
    parser.add_argument("--tickers", type=str, default="", help="Optional comma-separated ticker subset.")
    parser.add_argument("--max-tickers", type=int, default=0, help="Smoke-test limit; 0 means all tickers.")
    parser.add_argument("--allow-fail", action="store_true", help="Exit 0 even if some rows fail policy.")
    return parser.parse_args()


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def as_bool(raw: object, default: bool = False) -> bool:
    if raw is None:
        return default
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def read_csv_flexible(path: Path) -> list[dict[str, str]]:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                reader = csv.DictReader(handle)
                if reader.fieldnames is None:
                    raise ValueError(f"CSV has no header: {path}")
                return [{str(key): str(value or "") for key, value in row.items()} for row in reader]
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
    raise ValueError(f"Could not decode CSV {path}: {last_error}")


def selected_tickers(rows: list[dict[str, str]], args: argparse.Namespace) -> list[str]:
    ticker_filter = {normalize_ticker(item) for item in str(args.tickers or "").split(",") if normalize_ticker(item)}
    out: list[str] = []
    seen: set[str] = set()
    for row in rows:
        ticker = normalize_ticker(row.get("Name") or row.get("Ticker") or row.get("ticker"))
        if not ticker or ticker in seen:
            continue
        if ticker_filter and ticker not in ticker_filter:
            continue
        out.append(ticker)
        seen.add(ticker)
        if int(args.max_tickers) > 0 and len(out) >= int(args.max_tickers):
            break
    return out


def load_price_rows(conn: Any, tickers: list[str], asof_date: str, sources: list[str]) -> list[Any]:
    if not tickers:
        return []
    ticker_placeholders = ",".join("?" for _ in tickers)
    source_clause = ""
    params: list[Any] = [*tickers, asof_date]
    if sources:
        source_clause = " AND source_id IN (" + ",".join("?" for _ in sources) + ")"
        params.extend(sources)
    return conn.execute(
        f"""
        SELECT f.*
        FROM fact_price_ohlcv f
        JOIN (
            SELECT ticker, source_id, MAX(bar_date) AS max_bar_date
            FROM fact_price_ohlcv
            WHERE ticker IN ({ticker_placeholders})
              AND bar_date <= ?{source_clause}
            GROUP BY ticker, source_id
        ) latest
          ON latest.ticker = f.ticker
         AND latest.source_id = f.source_id
         AND latest.max_bar_date = f.bar_date
        """,
        tuple(params),
    ).fetchall()


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
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
    input_csv = (
        args.input.expanduser().resolve()
        if args.input
        else resolve_path(cfg_get(config, "market_data_policy.audit_input_csv", DEFAULT_INPUT), base_dir=base_dir)
    )
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(
            cfg_get(config, "market_data_policy.audit_output_csv", "../output/med_devices_reports/market_data_source_audit.csv"),
            base_dir=base_dir,
        )
    )
    asof = str(args.asof or datetime.now(timezone.utc).date().isoformat())
    asof_obj = parse_date(asof)
    if asof_obj is None:
        raise ValueError(f"Invalid --asof date, expected YYYY-MM-DD: {asof}")

    sources = scoring_market_sources(config)
    if not sources:
        raise ValueError("No market-data scoring sources configured")
    require_adjusted = as_bool(cfg_get(config, "market_data_policy.require_adjusted_for_scoring", True), True)
    max_staleness_days = int(cfg_get(config, "market_data_policy.max_staleness_days", 7))
    tickers = selected_tickers(read_csv_flexible(input_csv), args)
    if not tickers:
        raise ValueError(f"No tickers selected from {input_csv}")

    LOGGER.info("Auditing market policy db=%s input=%s tickers=%d sources=%s", db_path, input_csv, len(tickers), ",".join(sources))
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        conn.execute("PRAGMA temp_store = MEMORY")
        rows = load_price_rows(conn, tickers, asof, sources)
    selected = select_latest_rows_by_source_priority(
        rows,
        asof_date=asof_obj,
        source_priority=sources,
        max_staleness_days=max_staleness_days,
    )

    source_rank = {source: idx for idx, source in enumerate(sources)}
    available_by_ticker: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        row_dict = dict(row)
        available_by_ticker.setdefault(normalize_ticker(row_dict.get("ticker")), []).append(row_dict)

    audit_rows: list[dict[str, Any]] = []
    for ticker in tickers:
        selected_row = selected.get(ticker, {})
        available = available_by_ticker.get(ticker, [])
        reasons: list[str] = []
        selected_source = str(selected_row.get("source_id") or "").lower()
        selected_bar_date = parse_date(selected_row.get("bar_date"))
        adjusted_available = is_adjusted_price_row(selected_row) if selected_row else False
        if not selected_row:
            reasons.append("missing_market_row")
        if selected_row and require_adjusted and not adjusted_available:
            reasons.append("selected_row_has_no_adjusted_price")
        if selected_row and selected_source != sources[0]:
            reasons.append("fallback_source_used")
        if selected_bar_date is not None and (asof_obj - selected_bar_date).days > max_staleness_days:
            reasons.append("selected_row_stale")

        available_sources = sorted({str(row.get("source_id") or "").lower() for row in available if row.get("source_id")})
        available_adjustments = sorted(
            {
                f"{str(row.get('source_id') or '').lower()}:{price_adjustment_label(row)}:{int(row.get('is_adjusted') or 0)}"
                for row in available
                if row.get("source_id")
            }
        )
        audit_rows.append(
            {
                "asof_date": asof,
                "ticker": ticker,
                "selected_source": selected_source,
                "selected_source_rank": source_rank.get(selected_source, ""),
                "selected_bar_date": selected_row.get("bar_date", ""),
                "selected_close": selected_row.get("close", ""),
                "selected_adj_close": selected_row.get("adj_close", ""),
                "selected_price_adjustment": price_adjustment_label(selected_row) if selected_row else "",
                "selected_is_adjusted": int(adjusted_available) if selected_row else "",
                "available_sources": ";".join(available_sources),
                "available_adjustments": ";".join(available_adjustments),
                "requires_adjusted_for_scoring": int(require_adjusted),
                "policy_status": "pass" if not reasons else "fail",
                "review_reason": ";".join(reasons),
            }
        )

    write_csv(output_csv, audit_rows)
    counts: dict[str, int] = {}
    for row in audit_rows:
        key = f"{row['policy_status']}:{row['selected_source'] or '<missing>'}"
        counts[key] = counts.get(key, 0) + 1
    LOGGER.info("Market policy audit written: %s rows=%d counts=%s", output_csv, len(audit_rows), counts)
    if not args.allow_fail and any(row["policy_status"] != "pass" for row in audit_rows):
        raise SystemExit(2)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException as exc:
        configure_utc_logging()
        LOGGER.exception("Fatal med-device market policy audit error: %s", exc)
        raise SystemExit(1) from exc
