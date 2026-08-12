#!/usr/bin/env python3
from __future__ import annotations

# ruff: noqa: E402

import argparse
import json
import sys
from datetime import date
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from consumer_defensive.core.config import cfg_get, load_config
from consumer_defensive.core.db import connect, finish_run, start_run
from consumer_defensive.core.market_data import write_json
from consumer_defensive.core.script_runtime import (
    cache_only_environment,
    iso_date,
    parse_ticker_csv,
    require_date_window,
    require_known_tickers,
)
from consumer_defensive.core.stage3_runtime import (
    DEFAULT_MARKET_POLICY,
    assert_stage2_ready,
    bootstrap_stage3,
    database_path,
    load_stage3_policy,
    stage3_output_dir,
)
from consumer_defensive.core.yahoo_prices import load_yahoo_prices


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load Consumer Defensive active and benchmark Yahoo adjusted prices.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--market-policy", type=Path, default=DEFAULT_MARKET_POLICY)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--start", type=iso_date, default=None)
    parser.add_argument("--as-of", type=iso_date, default=date.today().isoformat())
    parser.add_argument("--tickers", default="")
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument("--cache-only", action="store_true", help="Forbid network access and fail on missing cache entries.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.cache_only:
        if args.force_refresh:
            raise ValueError("--cache-only and --force-refresh are mutually exclusive.")
    bundle = load_config(args.config)
    policy = load_stage3_policy(args.market_policy)
    start = args.start or str(policy.payload["history_start"])
    require_date_window(start, args.as_of)
    requested_tickers = parse_ticker_csv(args.tickers)
    db_path = database_path(bundle, args.db)
    output_dir = stage3_output_dir(bundle, policy, as_of=args.as_of, override=args.output_dir)
    timeout = float(cfg_get(bundle.payload, "runtime.sqlite_timeout_sec", 30.0))
    with connect(db_path, timeout_sec=timeout) as conn:
        bootstrap_stage3(conn, bundle)
        readiness = assert_stage2_ready(conn, bundle)
        run_id = start_run(conn, run_type="consumer_defensive_stage3_yahoo_prices", input_path=args.market_policy)
        try:
            tickers = require_known_tickers(conn, requested_tickers)
            with cache_only_environment(args.cache_only):
                summary = load_yahoo_prices(
                    conn,
                    policy,
                    start=start,
                    end=args.as_of,
                    tickers=tickers,
                    force_refresh=args.force_refresh,
                )
            status = "success" if not summary["failures"] else "partial"
            finish_run(conn, run_id=run_id, status=status, row_count=int(summary["bars_written"]), message=json.dumps(summary, sort_keys=True))
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", message=f"{type(exc).__name__}: {exc}")
            raise
    payload = {"database": str(db_path), "stage2": readiness, "requested_tickers": requested_tickers or "ALL", **summary}
    report_path = output_dir / "yahoo_price_sync_summary.json"
    write_json(report_path, payload)
    print(json.dumps({**payload, "report": str(report_path)}, indent=2, sort_keys=True))
    return 0 if not summary["failures"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
