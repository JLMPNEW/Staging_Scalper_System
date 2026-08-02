#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib
import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from portfolio_layer.core.contracts import read_csv, write_manifest  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.risk.norgate_market_instruments import (  # noqa: E402
    configured_market_instruments,
    hydrate_market_instruments,
    hydration_rows,
    purge_cached_tickers,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Hydrate the portfolio market-instrument cache from local Norgate data."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--no-reexec", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args()


def _reexec(args: argparse.Namespace, python_executable: Path) -> int:
    command = [
        str(python_executable),
        str(Path(__file__).resolve()),
        "--config",
        str(args.config.expanduser().resolve()),
        "--as-of",
        args.as_of,
        "--no-reexec",
    ]
    if args.force:
        command.append("--force")
    return subprocess.run(command, check=False).returncode


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    cache_config = cfg_get(config, "risk_panel.norgate_market_instrument_cache", {}) or {}
    if not bool(cache_config.get("enabled", False)):
        print(json.dumps({"acceptance": "SKIPPED_DISABLED", "as_of": args.as_of}))
        return 0
    try:
        provider = importlib.import_module("norgatedata")
    except ModuleNotFoundError:
        if args.no_reexec:
            raise
        python_executable = Path(
            str(cache_config.get("python_executable") or "").strip()
        ).expanduser()
        if not python_executable.is_file():
            raise RuntimeError(
                "norgatedata is unavailable and configured Norgate Python does not exist: "
                f"{python_executable}"
            )
        return _reexec(args, python_executable)

    run_date = date.fromisoformat(args.as_of)
    risk_config = cfg_get(config, "risk_panel", {}) or {}
    alias_tickers = {
        str(ticker).strip().upper()
        for ticker in (risk_config.get("ticker_aliases") or {})
        if str(ticker).strip()
    }
    lookback = int(risk_config.get("lookback_trading_days", 504))
    start = run_date - timedelta(days=int(lookback * 1.6) + 40)
    database_path = resolve_path(
        cache_config.get("database_path"), base_dir=config_path.parent
    )
    required_tickers = configured_market_instruments(risk_config)
    summaries = hydrate_market_instruments(
        provider,
        database_path=database_path,
        tickers=required_tickers,
        start=start,
        end=run_date,
        source_id=str(
            cache_config.get("source_id")
            or "norgate_us_equities_total_return"
        ),
        price_adjustment=str(
            cache_config.get("price_adjustment")
            or "total_return_adjusted_close"
        ),
    )
    source_id = str(
        cache_config.get("source_id")
        or "norgate_us_equities_total_return"
    )
    purged_alias_row_count = purge_cached_tickers(
        database_path,
        tickers=alias_tickers,
        source_id=source_id,
    )
    scored_tickers: list[str] = []
    if bool(cache_config.get("include_scored_universe", False)):
        paths = resolve_runtime_paths(config, config_path)
        score_path = paths.output_dir / "runs" / args.as_of / "stocks_scores.csv"
        scored_tickers = sorted(
            {
                str(row.get("ticker") or "").strip().upper()
                for row in read_csv(score_path)
                if str(row.get("investable_eligible") or "").strip() == "1"
                and str(row.get("ticker") or "").strip()
            }
            - set(required_tickers)
            - alias_tickers
        )
        summaries.extend(
            hydrate_market_instruments(
                provider,
                database_path=database_path,
                tickers=scored_tickers,
                start=start,
                end=run_date,
                source_id=str(
                    cache_config.get("source_id")
                    or "norgate_us_equities_total_return"
                ),
                price_adjustment=str(
                    cache_config.get("price_adjustment")
                    or "total_return_adjusted_close"
                ),
                allow_missing=True,
            )
        )
    summaries.sort(key=lambda item: item.ticker)
    hydrated_tickers = {item.ticker for item in summaries}
    manifest_path = resolve_path(
        cache_config.get("manifest_path"), base_dir=config_path.parent
    )
    payload = {
        "acceptance": "PASS",
        "as_of": args.as_of,
        "database_path": str(database_path),
        "required_market_instrument_count": len(required_tickers),
        "scored_ticker_requested_count": len(scored_tickers),
        "hydrated_ticker_count": len(summaries),
        "missing_optional_scored_tickers": sorted(
            set(scored_tickers) - hydrated_tickers
        ),
        "excluded_alias_tickers": sorted(alias_tickers),
        "purged_alias_row_count": purged_alias_row_count,
        "instruments": hydration_rows(summaries),
    }
    write_manifest(manifest_path, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
