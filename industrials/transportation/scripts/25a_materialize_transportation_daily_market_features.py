#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import (  # noqa: E402
    cfg_get,
    family_config,
    load_yaml,
    resolve_path,
)
from industrials.core.db import connect, init_db  # noqa: E402
from industrials.core.historical_score_history import (  # noqa: E402
    benchmark_trading_dates,
    select_dates,
)
from industrials.core.market_feature_history import (  # noqa: E402
    load_shared_market_module,
    materialize_bulk_market_history,
)
from industrials.transportation.contracts import write_manifest  # noqa: E402
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Bulk-materialize daily transportation market features while "
            "loading each raw price series once and reusing shared formulas."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--max-dates", type=int, default=0)
    parser.add_argument(
        "--selection", choices=("oldest", "newest"), default="oldest"
    )
    parser.add_argument("--rebuild-existing", action="store_true")
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--output-json", type=Path, default=None)
    return parser.parse_args()


def iso_date(raw: str, *, label: str) -> str:
    value = str(raw or "")[:10]
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError(f"invalid {label}={raw!r}") from exc


def main() -> int:
    args = parse_args()
    if args.max_dates < 0:
        raise ValueError("--max-dates cannot be negative")
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    family = family_config(config, MODEL_FAMILY)
    historical = family["historical_features"]
    score_history = family["historical_scores"]
    base_dir = config_path.parent
    db_path = (
        args.db.expanduser().resolve()
        if args.db
        else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    )
    start_date = iso_date(
        str(args.start_date or score_history["start_date"]), label="start date"
    )
    end_date = iso_date(args.end_date, label="end date")
    if start_date > end_date:
        raise ValueError("--start-date cannot be after --end-date")
    feature_root = resolve_path(historical["output_root"], base_dir=base_dir)
    report_path = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(
            "../output/industrials/transportation/score_history/"
            "transportation_daily_market_history_build.csv",
            base_dir=base_dir,
        )
    )
    manifest_path = (
        args.output_json.expanduser().resolve()
        if args.output_json
        else resolve_path(
            "../output/industrials/transportation/score_history/"
            "transportation_daily_market_history_build.json",
            base_dir=base_dir,
        )
    )
    primary_source = str(
        cfg_get(config, "market_data_policy.scoring_primary_source")
    )
    fallback_sources = [
        str(value)
        for value in cfg_get(
            config, "market_data_policy.scoring_fallback_sources", []
        )
    ]
    source_ids = list(dict.fromkeys([primary_source, *fallback_sources]))
    benchmark_tickers = [
        str(value) for value in family["market"]["benchmark_tickers"]
    ]
    primary_benchmark = str(family["market"]["primary_benchmark"])
    secondary_benchmarks = [
        value for value in benchmark_tickers if value != primary_benchmark
    ][:3]
    windows = {
        key: int(value)
        for key, value in cfg_get(config, "market_feature_build.windows").items()
    }
    with sqlite3.connect(
        f"file:{db_path.as_posix()}?mode=ro", uri=True
    ) as read_connection:
        dates = benchmark_trading_dates(
            read_connection,
            ticker=str(score_history["benchmark_ticker"]),
            source_id=str(score_history["benchmark_source_id"]),
            start_date=start_date,
            end_date=end_date,
        )
    dates = select_dates(
        dates, maximum=args.max_dates, selection=args.selection
    )
    shared = load_shared_market_module(PROJECT_ROOT)
    with connect(
        db_path,
        timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 120.0)),
    ) as connection:
        init_db(connection)
        result = materialize_bulk_market_history(
            connection,
            shared=shared,
            model_family=MODEL_FAMILY,
            dates=dates,
            source_ids=source_ids,
            benchmark_source_ids=source_ids,
            benchmark_tickers=benchmark_tickers,
            primary_benchmark=primary_benchmark,
            secondary_benchmarks=secondary_benchmarks,
            maximum_staleness_days=int(
                cfg_get(config, "market_data_policy.max_staleness_days", 7)
            ),
            minimum_days=int(
                cfg_get(
                    config,
                    "market_data_policy.min_trading_days_for_full_features",
                    252,
                )
            ),
            minimum_dollar_volume=float(
                cfg_get(
                    config,
                    "market_data_policy.min_avg_dollar_volume_60d_for_full_features",
                    0,
                )
            ),
            minimum_source_bars=int(
                cfg_get(config, "market_data_policy.min_source_bars_for_selection", 20)
            ),
            windows=windows,
            output_root=feature_root,
            report_path=report_path,
            rebuild_existing=args.rebuild_existing,
        )
    result.update(
        {
            "gate": "TRANSPORTATION_DAILY_MARKET_HISTORY",
            "report_csv": str(report_path),
            "shared_formula_module": str(
                PROJECT_ROOT
                / "industrials"
                / "scripts"
                / "05_build_industrials_market_features.py"
            ),
            "network_requests": 0,
            "parser_invocations": 0,
        }
    )
    write_manifest(manifest_path, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["acceptance"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
