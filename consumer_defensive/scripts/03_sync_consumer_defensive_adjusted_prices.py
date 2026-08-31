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
from consumer_defensive.core.market_data import write_csv, write_json
from consumer_defensive.core.norgate_prices import load_norgate_prices
from consumer_defensive.core.script_runtime import (
    cache_only_environment,
    iso_date,
    require_date_window,
)
from consumer_defensive.core.stage3_runtime import (
    DEFAULT_MARKET_POLICY,
    DEFAULT_TERMINAL_POLICY,
    assert_stage2_ready,
    bootstrap_stage3,
    database_path,
    load_stage3_policy,
    stage3_output_dir,
)
from consumer_defensive.core.terminal_events import (
    load_norgate_successor_prices,
    load_terminal_event_ledger,
    load_terminal_event_policy,
    reconcile_terminal_events,
    validate_terminal_events,
)
from consumer_defensive.core.yahoo_prices import load_yahoo_prices


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load both Consumer Defensive Stage 3 price providers.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--market-policy", type=Path, default=DEFAULT_MARKET_POLICY)
    parser.add_argument("--terminal-policy", type=Path, default=DEFAULT_TERMINAL_POLICY)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--start", type=iso_date, default=None)
    parser.add_argument("--as-of", type=iso_date, default=date.today().isoformat())
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument(
        "--cache-only",
        action="store_true",
        help="Forbid Yahoo network access and fail on missing Yahoo cache entries.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.cache_only and args.force_refresh:
        raise ValueError("--cache-only and --force-refresh are mutually exclusive.")
    try:
        import norgatedata  # type: ignore
    except ImportError as exc:
        raise SystemExit("norgatedata is unavailable; run with the base Miniconda interpreter.") from exc
    bundle = load_config(args.config)
    policy = load_stage3_policy(args.market_policy)
    terminal_policy = load_terminal_event_policy(args.terminal_policy)
    terminal_events = load_terminal_event_ledger(terminal_policy)
    start = args.start or str(policy.payload["history_start"])
    require_date_window(start, args.as_of)
    db_path = database_path(bundle, args.db)
    output_dir = stage3_output_dir(bundle, policy, as_of=args.as_of, override=args.output_dir)
    timeout = float(cfg_get(bundle.payload, "runtime.sqlite_timeout_sec", 30.0))
    with connect(db_path, timeout_sec=timeout) as conn:
        bootstrap_stage3(conn, bundle)
        readiness = assert_stage2_ready(conn, bundle, as_of=args.as_of)
        run_id = start_run(conn, run_type="consumer_defensive_stage3_prices", input_path=args.market_policy)
        try:
            with cache_only_environment(args.cache_only):
                yahoo = load_yahoo_prices(
                    conn,
                    policy,
                    start=start,
                    end=args.as_of,
                    force_refresh=args.force_refresh,
                )
                norgate = load_norgate_prices(
                    conn, policy, provider=norgatedata, end=args.as_of
                )
                terminal_loaded = reconcile_terminal_events(conn, terminal_policy)
                terminal_norgate = load_norgate_successor_prices(
                    conn, terminal_events, provider=norgatedata, end=args.as_of
                )
            terminal_validation = validate_terminal_events(conn, terminal_policy, as_of=args.as_of)
            hard_fail = bool(
                yahoo["failures"]
                or norgate["failures"]
                or terminal_norgate["failures"]
                or terminal_validation["status"] != "PASS"
            )
            finish_run(
                conn,
                run_id=run_id,
                status="failed" if hard_fail else "success",
                row_count=(
                    int(yahoo["bars_written"])
                    + int(norgate["bars_written"])
                    + int(terminal_norgate["bars_written"])
                ),
                message=json.dumps(
                    {
                        "yahoo": yahoo,
                        "norgate": {key: value for key, value in norgate.items() if key != "rows"},
                        "terminal_loaded": terminal_loaded,
                        "terminal_successor_norgate": terminal_norgate,
                        "terminal_validation": {key: value for key, value in terminal_validation.items() if key != "checks"},
                    },
                    sort_keys=True,
                ),
            )
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", message=f"{type(exc).__name__}: {exc}")
            raise
    output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(output_dir / "norgate_price_import.csv", norgate["rows"])
    write_csv(output_dir / "terminal_event_reconciliation_checks.csv", terminal_validation["checks"])
    summary = {
        "database": str(db_path),
        "output_dir": str(output_dir),
        "stage2": readiness,
        "yahoo": yahoo,
        "norgate": {key: value for key, value in norgate.items() if key != "rows"},
        "terminal_loaded": terminal_loaded,
        "terminal_successor_norgate": terminal_norgate,
        "terminal_validation": {key: value for key, value in terminal_validation.items() if key != "checks"},
    }
    write_json(output_dir / "price_sync_summary.json", summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 1 if hard_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
