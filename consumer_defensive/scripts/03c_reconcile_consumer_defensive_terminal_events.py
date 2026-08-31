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
from consumer_defensive.core.script_runtime import cache_only_environment, iso_date
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
    yahoo_successor_tickers,
)
from consumer_defensive.core.yahoo_prices import load_yahoo_prices


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reconcile Consumer Defensive terminal terms and successor total-return histories."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--market-policy", type=Path, default=DEFAULT_MARKET_POLICY)
    parser.add_argument("--terminal-policy", type=Path, default=DEFAULT_TERMINAL_POLICY)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--as-of", type=iso_date, default=date.today().isoformat())
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument(
        "--cache-only",
        action="store_true",
        help="Forbid Yahoo network access and fail on missing Yahoo cache entries.",
    )
    parser.add_argument(
        "--skip-successor-price-sync",
        action="store_true",
        help="Load reviewed terms and validate existing successor prices without network/provider refresh.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.cache_only and args.force_refresh:
        raise ValueError("--cache-only and --force-refresh are mutually exclusive.")
    bundle = load_config(args.config)
    market_policy = load_stage3_policy(args.market_policy)
    terminal_policy = load_terminal_event_policy(args.terminal_policy)
    events = load_terminal_event_ledger(terminal_policy)
    db_path = database_path(bundle, args.db)
    output_dir = stage3_output_dir(bundle, market_policy, as_of=args.as_of, override=args.output_dir)
    timeout = float(cfg_get(bundle.payload, "runtime.sqlite_timeout_sec", 30.0))
    yahoo: dict[str, object] = {"status": "SKIPPED"}
    norgate: dict[str, object] = {"status": "SKIPPED"}
    provider = None
    if not args.skip_successor_price_sync:
        try:
            import norgatedata  # type: ignore
        except ImportError as exc:
            raise SystemExit("norgatedata is unavailable; run with the base Miniconda interpreter.") from exc
        provider = norgatedata

    with connect(db_path, timeout_sec=timeout) as conn:
        bootstrap_stage3(conn, bundle)
        readiness = assert_stage2_ready(conn, bundle, as_of=args.as_of)
        run_id = start_run(
            conn,
            run_type="consumer_defensive_terminal_event_reconciliation",
            input_path=terminal_policy.ledger_path,
        )
        try:
            loaded = reconcile_terminal_events(conn, terminal_policy)
            if not args.skip_successor_price_sync:
                with cache_only_environment(args.cache_only):
                    effective_events = [
                        event for event in events if event.economic_event_date <= args.as_of
                    ]
                    yahoo_tickers = yahoo_successor_tickers(
                        effective_events, as_of=args.as_of
                    )
                    if yahoo_tickers:
                        yahoo_start = min(
                            event.successor_reference_date
                            for event in effective_events
                            if event.successor_ticker in yahoo_tickers
                        )
                        yahoo = load_yahoo_prices(
                            conn,
                            market_policy,
                            start=yahoo_start,
                            end=args.as_of,
                            tickers=yahoo_tickers,
                            force_refresh=args.force_refresh,
                        )
                    else:
                        yahoo = {"status": "SKIPPED_NO_EFFECTIVE_SUCCESSORS"}
                    norgate = load_norgate_successor_prices(
                        conn,
                        events,
                        provider=provider,
                        end=args.as_of,
                    )
            validation = validate_terminal_events(conn, terminal_policy, as_of=args.as_of)
            failed = bool(
                validation["status"] != "PASS"
                or yahoo.get("failures", [])
                or norgate.get("failures", [])
            )
            finish_run(
                conn,
                run_id=run_id,
                status="failed" if failed else "success",
                row_count=int(loaded["events_loaded"]),
                message=json.dumps(
                    {
                        "loaded": loaded,
                        "yahoo": yahoo,
                        "norgate": norgate,
                        "validation": {key: value for key, value in validation.items() if key != "checks"},
                    },
                    sort_keys=True,
                ),
            )
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", message=f"{type(exc).__name__}: {exc}")
            raise

    output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(output_dir / "terminal_event_reconciliation_checks.csv", validation["checks"])
    summary = {
        "database": str(db_path),
        "stage2": readiness,
        "ledger": str(terminal_policy.ledger_path),
        "loaded": loaded,
        "successor_price_sync": {"yahoo": yahoo, "norgate": norgate},
        **{key: value for key, value in validation.items() if key != "checks"},
    }
    write_json(output_dir / "terminal_event_reconciliation_summary.json", summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
