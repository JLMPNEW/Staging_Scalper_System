#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sys
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.db import connect, finish_run, init_db, start_run, utc_now  # noqa: E402
from industrials.core.reports import write_csv_atomic  # noqa: E402
from industrials.core.source_registry import load_source_registry, upsert_source_registry  # noqa: E402
from industrials.machinery.contracts import read_csv_rows  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
MODEL_FAMILY = "machinery"
REPORT_FIELDS = [
    "internal_ticker",
    "actual_ticker",
    "norgate_symbol",
    "company_name",
    "start_date",
    "end_date",
    "mapping_status",
    "status",
    "loaded_rows",
    "first_bar_date",
    "last_bar_date",
    "error",
]


@dataclass(frozen=True)
class ImportMember:
    internal_ticker: str
    actual_ticker: str
    norgate_symbol: str
    company_name: str
    start_date: str
    end_date: str
    mapping_status: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import verified machinery Norgate PIT price history.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--max-tickers", type=int, default=0)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument("--no-purge-existing-range", action="store_true")
    return parser.parse_args()


def parse_date(value: object) -> str:
    text = str(value or "").strip()[:10]
    if not text:
        return ""
    return datetime.strptime(text, "%Y-%m-%d").date().isoformat()


def safe_float(value: object) -> float | None:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def load_members(mapping_path: Path, membership_path: Path) -> list[ImportMember]:
    mappings = {
        str(row.get("internal_ticker") or "").strip().upper(): row
        for row in read_csv_rows(mapping_path)
        if str(row.get("internal_ticker") or "").strip()
    }
    members: list[ImportMember] = []
    for row in read_csv_rows(membership_path):
        if str(row.get("membership_status") or "").strip() != "historical_delisted":
            continue
        ticker = str(row.get("internal_ticker") or "").strip().upper()
        mapping = mappings.get(ticker)
        if mapping is None:
            raise ValueError(f"Historical membership ticker has no Norgate mapping row: {ticker}")
        if str(mapping.get("calibration_usable_flag") or "") != "1":
            raise ValueError(f"Historical membership ticker is not calibration-usable in symbol map: {ticker}")
        symbol = str(mapping.get("norgate_symbol") or "").strip()
        if not symbol:
            raise ValueError(f"Historical membership ticker has blank Norgate symbol: {ticker}")
        members.append(
            ImportMember(
                internal_ticker=ticker,
                actual_ticker=str(mapping.get("actual_ticker") or ticker).strip().upper(),
                norgate_symbol=symbol,
                company_name=str(mapping.get("company_name") or row.get("company_name") or ticker).strip(),
                start_date=parse_date(row.get("start_date")),
                end_date=parse_date(row.get("end_date")),
                mapping_status=str(mapping.get("mapping_status") or "").strip(),
            )
        )
    if not members:
        raise ValueError(f"No historical-delisted rows are importable from {membership_path}")
    if len({member.internal_ticker for member in members}) != len(members):
        raise ValueError("Duplicate internal_ticker in Norgate import membership")
    return sorted(members, key=lambda member: member.internal_ticker)


def resolve_adjustment_type(provider: Any, mode: str) -> Any:
    clean = str(mode or "CAPITAL").strip().upper()
    try:
        return getattr(provider.StockPriceAdjustmentType, clean)
    except AttributeError as exc:
        valid = sorted(name for name in dir(provider.StockPriceAdjustmentType) if name.isupper())
        raise ValueError(f"Unsupported Norgate price adjustment mode={clean!r}; valid={valid}") from exc


def fetch_prices(
    provider: Any,
    *,
    symbol: str,
    start_date: str,
    end_date: str,
    adjustment_mode: str,
) -> pd.DataFrame:
    raw = provider.price_timeseries(
        symbol,
        stock_price_adjustment_setting=provider.StockPriceAdjustmentType.NONE,
        start_date=start_date,
        end_date=end_date,
        timeseriesformat="pandas-dataframe",
    )
    if raw is None or len(raw) == 0:
        return pd.DataFrame()
    result = raw.copy().sort_index()
    result.index = pd.to_datetime(result.index).date
    if adjustment_mode.strip().upper() == "NONE":
        result["AdjClose"] = result["Close"]
        return result
    adjusted = provider.price_timeseries(
        symbol,
        stock_price_adjustment_setting=resolve_adjustment_type(provider, adjustment_mode),
        start_date=start_date,
        end_date=end_date,
        timeseriesformat="pandas-dataframe",
    )
    if adjusted is None or len(adjusted) == 0:
        raise ValueError(f"Adjusted Norgate price history is empty for {symbol}")
    adjusted = adjusted.copy().sort_index()
    adjusted.index = pd.to_datetime(adjusted.index).date
    result["AdjClose"] = adjusted["Close"].reindex(result.index)
    return result


def upsert_prices(
    conn: Any,
    *,
    member: ImportMember,
    prices: pd.DataFrame,
    source_id: str,
    adjustment_mode: str,
) -> int:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    adjustment = (
        f"norgate_{adjustment_mode.lower()}_adj_close;source_symbol={member.norgate_symbol};"
        f"actual_ticker={member.actual_ticker};verified_mapping={member.mapping_status}"
    )
    inserted = 0
    for index, row in prices.iterrows():
        close = safe_float(row.get("Close"))
        adj_close = safe_float(row.get("AdjClose"))
        if close is None or adj_close is None:
            continue
        conn.execute(
            """
            INSERT INTO fact_price_ohlcv(
                ticker, bar_date, source_id, open, high, low, close, adj_close, volume,
                dividend_amount, split_factor, price_adjustment, is_adjusted, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, 1, ?, ?)
            ON CONFLICT(ticker, bar_date, source_id) DO UPDATE SET
                open = excluded.open,
                high = excluded.high,
                low = excluded.low,
                close = excluded.close,
                adj_close = excluded.adj_close,
                volume = excluded.volume,
                dividend_amount = excluded.dividend_amount,
                price_adjustment = excluded.price_adjustment,
                is_adjusted = excluded.is_adjusted,
                updated_at = excluded.updated_at
            """,
            (
                member.internal_ticker,
                str(index)[:10],
                source_id,
                safe_float(row.get("Open")),
                safe_float(row.get("High")),
                safe_float(row.get("Low")),
                close,
                adj_close,
                safe_float(row.get("Volume")),
                safe_float(row.get("Dividend")),
                adjustment,
                now,
                now,
            ),
        )
        inserted += 1
    return inserted


def purge_existing_range(
    conn: Any,
    *,
    ticker: str,
    source_id: str,
    first_bar: str,
    last_bar: str,
    full_refresh: bool,
) -> None:
    # The upsert alone can never delete stale rows. On a full-interval run the
    # freshly fetched history is authoritative, so purge everything for the
    # ticker/source — this also removes wrong-symbol bars INSIDE the certified
    # interval on dates where the corrected symbol has no bar (different
    # calendar/halts), which an outside-interval purge would miss. When the
    # operator narrowed the fetch window (--start-date/--end-date), only purge
    # outside the certified interval so untouched valid history survives.
    if full_refresh:
        conn.execute(
            "DELETE FROM fact_price_ohlcv WHERE ticker = ? AND source_id = ?",
            (ticker, source_id),
        )
        return
    conn.execute(
        """
        DELETE FROM fact_price_ohlcv
        WHERE ticker = ?
          AND source_id = ?
          AND (bar_date < ? OR bar_date > ?)
        """,
        (ticker, source_id, first_bar, last_bar),
    )


def add_issue(conn: Any, *, ticker: str, source_id: str, detail: str) -> None:
    now = utc_now()
    company = conn.execute("SELECT company_id FROM dim_company WHERE ticker = ?", (ticker,)).fetchone()
    company_id = int(company["company_id"]) if company is not None else None
    conn.execute(
        """
        INSERT INTO data_quality_issues(
            detected_at, severity, stage, model_family, ticker, company_id, source_id,
            issue_type, issue_detail, resolution_status, created_at, updated_at
        )
        VALUES (?, 'error', 'import_machinery_norgate_prices', ?, ?, ?, ?,
                'norgate_price_import_failed', ?, 'open', ?, ?)
        """,
        (now, MODEL_FAMILY, ticker, company_id, source_id, detail[:1000], now, now),
    )


def load_provider() -> Any:
    try:
        import norgatedata  # pyright: ignore[reportMissingImports]
    except ImportError as exc:
        raise SystemExit(
            "norgatedata is not installed in this Python environment. Run Stage 15 with the base Miniconda Python."
        ) from exc
    return norgatedata


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    mapping_path = resolve_path(cfg_get(config, "industrials_universe.norgate_symbol_map_csv"), base_dir=base_dir)
    membership_path = resolve_path(cfg_get(config, "industrials_universe.historical_membership_csv"), base_dir=base_dir)
    source_id = str(cfg_get(config, "norgate_delisted_import.source_id", "norgate_us_equities_total_return"))
    adjustment_mode = str(cfg_get(config, "norgate_delisted_import.price_adjustment_mode", "CAPITAL")).strip().upper()
    purge_config = str(cfg_get(config, "norgate_delisted_import.purge_existing_range", True)).strip().lower()
    purge_ranges = not args.no_purge_existing_range and purge_config not in {"0", "false", "no", "off"}
    output_path = args.output_csv.expanduser().resolve() if args.output_csv else resolve_path(
        cfg_get(config, "norgate_delisted_import.output_csv"),
        base_dir=base_dir,
    )
    registry_path = resolve_path(cfg_get(config, "source_registry.path"), base_dir=base_dir)
    requested_start = parse_date(args.start_date)
    requested_end = parse_date(args.end_date)
    members = load_members(mapping_path, membership_path)
    if args.max_tickers > 0:
        members = members[: args.max_tickers]
    provider = load_provider()
    report: list[dict[str, Any]] = []
    failures = 0
    timeout = float(cfg_get(config, "runtime.sqlite_timeout_sec", 120.0))
    connection_context = nullcontext(None) if args.dry_run else connect(db_path, timeout_sec=timeout)
    with connection_context as conn:
        run_id: int | None = None
        if conn is not None:
            init_db(conn)
            upsert_source_registry(conn, load_source_registry(registry_path))
            run_id = start_run(conn, run_type="import_machinery_norgate_prices", input_path=mapping_path)
        try:
            for member in members:
                start_date = max(member.start_date, requested_start) if requested_start else member.start_date
                end_date = min(member.end_date, requested_end) if requested_end else member.end_date
                row: dict[str, Any] = {
                    "internal_ticker": member.internal_ticker,
                    "actual_ticker": member.actual_ticker,
                    "norgate_symbol": member.norgate_symbol,
                    "company_name": member.company_name,
                    "start_date": start_date,
                    "end_date": end_date,
                    "mapping_status": member.mapping_status,
                    "status": "",
                    "loaded_rows": 0,
                    "first_bar_date": "",
                    "last_bar_date": "",
                    "error": "",
                }
                try:
                    if end_date < start_date:
                        raise ValueError(f"empty requested date range {start_date}>{end_date}")
                    prices = fetch_prices(
                        provider,
                        symbol=member.norgate_symbol,
                        start_date=start_date,
                        end_date=end_date,
                        adjustment_mode=adjustment_mode,
                    )
                    if prices.empty:
                        raise ValueError("no Norgate price rows")
                    row["first_bar_date"] = str(prices.index[0])[:10]
                    row["last_bar_date"] = str(prices.index[-1])[:10]
                    if row["first_bar_date"] < start_date or row["last_bar_date"] > end_date:
                        raise ValueError("Norgate returned bars outside the certified membership interval")
                    if args.dry_run:
                        row["status"] = "DRY_RUN"
                        row["loaded_rows"] = len(prices)
                    elif conn is not None:
                        with conn:
                            if purge_ranges:
                                purge_existing_range(
                                    conn,
                                    ticker=member.internal_ticker,
                                    source_id=source_id,
                                    first_bar=member.start_date,
                                    last_bar=member.end_date,
                                    full_refresh=not requested_start and not requested_end,
                                )
                            row["loaded_rows"] = upsert_prices(
                                conn,
                                member=member,
                                prices=prices,
                                source_id=source_id,
                                adjustment_mode=adjustment_mode,
                            )
                        row["status"] = "PASS"
                except Exception as exc:
                    failures += 1
                    row["status"] = "FAIL"
                    row["error"] = f"{type(exc).__name__}: {exc}"
                    if conn is not None:
                        with conn:
                            add_issue(conn, ticker=member.internal_ticker, source_id=source_id, detail=row["error"])
                report.append(row)
            if conn is not None and run_id is not None:
                finish_run(
                    conn,
                    run_id=run_id,
                    status="success" if failures == 0 or args.allow_partial else "failed",
                    row_count=sum(int(row["loaded_rows"] or 0) for row in report),
                    message=f"tickers={len(report)} failures={failures} dry_run={args.dry_run}",
                )
        except BaseException as exc:
            if conn is not None and run_id is not None:
                finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise
    write_csv_atomic(output_path, REPORT_FIELDS, report)
    summary = {
        "acceptance": "PASS" if failures == 0 or args.allow_partial else "FAIL",
        "dry_run": bool(args.dry_run),
        "ticker_count": len(report),
        "failed_ticker_count": failures,
        "loaded_rows": sum(int(row["loaded_rows"] or 0) for row in report),
        "output_csv": str(output_path),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["acceptance"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
