#!/usr/bin/env python3
"""Load daily reporting-currency-to-USD FX rates required by SEC facts."""

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
    assert_stage4_raw_facts_ready,
    cache_only_environment,
    iso_date,
    require_date_window,
    stage4_output_dir,
)
from consumer_defensive.core.stage3_runtime import database_path
from consumer_defensive.core.stage4 import bootstrap_stage4, sync_fx_rates

DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--start", type=iso_date, default=None)
    parser.add_argument("--end", type=iso_date, default=date.today().isoformat())
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument("--cache-only", action="store_true", help="Forbid network access and report every missing cache entry.")
    parser.add_argument(
        "--allow-partial-history",
        action="store_true",
        help="Allow a start date later than the configured full-history start.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.cache_only:
        if args.force_refresh:
            raise ValueError("--cache-only and --force-refresh are mutually exclusive.")
    bundle = load_config(args.config)
    configured_start = iso_date(str(cfg_get(bundle.payload, "fx_rates.start_date")))
    start = args.start or configured_start
    require_date_window(start, args.end)
    if start > configured_start and not args.allow_partial_history:
        raise ValueError(
            f"FX start {start} truncates required history beginning {configured_start}; "
            "use --allow-partial-history only for an intentional diagnostic refresh."
        )
    db_path = database_path(bundle, args.db)
    output_dir = stage4_output_dir(bundle, as_of=args.end, override=args.output_dir)
    timeout = float(cfg_get(bundle.payload, "runtime.sqlite_timeout_sec", 30.0))

    with connect(db_path, timeout_sec=timeout) as conn:
        bootstrap_stage4(conn, bundle)
        run_id = start_run(
            conn,
            run_type="consumer_defensive_stage4_fx",
            input_path=bundle.path,
        )
        try:
            readiness = assert_stage4_raw_facts_ready(
                conn, bundle, as_of=args.end
            )
            with cache_only_environment(args.cache_only):
                result = sync_fx_rates(
                    conn,
                    bundle,
                    start=start,
                    end=args.end,
                    force_refresh=args.force_refresh,
                )
            failed = bool(
                result["failures"]
                or result["unknown_three_letter_units"]
                or (result["currencies"] and int(result["rows_written"]) == 0)
            )
            finish_run(
                conn,
                run_id=run_id,
                status="partial" if failed else "success",
                row_count=int(result["rows_written"]),
                message=json.dumps(result, sort_keys=True),
            )
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", message=f"{type(exc).__name__}: {exc}")
            raise

    payload = {
        "database": str(db_path),
        "start": start,
        "end": args.end,
        "partial_history_allowed": bool(args.allow_partial_history),
        "readiness": readiness,
        **result,
    }
    report_path = output_dir / "fx_rate_sync.json"
    write_json(report_path, payload)
    print(json.dumps({**payload, "report": str(report_path)}, indent=2, sort_keys=True))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
