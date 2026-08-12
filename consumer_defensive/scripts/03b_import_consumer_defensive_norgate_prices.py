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
from consumer_defensive.core.norgate_prices import load_norgate_prices
from consumer_defensive.core.script_runtime import iso_date, parse_ticker_csv, require_known_tickers
from consumer_defensive.core.stage3_runtime import (
    DEFAULT_MARKET_POLICY,
    assert_stage2_ready,
    bootstrap_stage3,
    database_path,
    load_stage3_policy,
    stage3_output_dir,
)
from consumer_defensive.core.market_data import write_csv, write_json


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load Consumer Defensive Norgate total-return histories.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--market-policy", type=Path, default=DEFAULT_MARKET_POLICY)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--as-of", type=iso_date, default=date.today().isoformat())
    parser.add_argument("--tickers", default="")
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        import norgatedata  # type: ignore
    except ImportError as exc:
        raise SystemExit("norgatedata is unavailable; run with the base Miniconda interpreter.") from exc
    bundle = load_config(args.config)
    policy = load_stage3_policy(args.market_policy)
    requested_tickers = parse_ticker_csv(args.tickers)
    db_path = database_path(bundle, args.db)
    output_dir = stage3_output_dir(bundle, policy, as_of=args.as_of, override=args.output_dir)
    timeout = float(cfg_get(bundle.payload, "runtime.sqlite_timeout_sec", 30.0))
    with connect(db_path, timeout_sec=timeout) as conn:
        bootstrap_stage3(conn, bundle)
        readiness = assert_stage2_ready(conn, bundle)
        run_id = start_run(conn, run_type="consumer_defensive_stage3_norgate_prices", input_path=args.market_policy)
        try:
            tickers = require_known_tickers(conn, requested_tickers)
            summary = load_norgate_prices(conn, policy, provider=norgatedata, end=args.as_of, tickers=tickers)
            finish_run(
                conn,
                run_id=run_id,
                status="failed" if summary["status"] == "FAIL" else ("partial" if summary["status"] == "WARN" else "success"),
                row_count=int(summary["bars_written"]),
                message=json.dumps({key: value for key, value in summary.items() if key != "rows"}, sort_keys=True),
            )
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", message=f"{type(exc).__name__}: {exc}")
            raise
    output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(output_dir / "norgate_price_import.csv", summary["rows"])
    write_json(output_dir / "norgate_price_import_summary.json", {key: value for key, value in summary.items() if key != "rows"})
    print(json.dumps({"database": str(db_path), "output_dir": str(output_dir), "stage2": readiness, "requested_tickers": requested_tickers or "ALL", **{key: value for key, value in summary.items() if key != "rows"}}, indent=2, sort_keys=True))
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
