#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.logging_utils import configure_utc_logging  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
MODEL_FAMILY = "defense"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the defense daily refresh fast path for one market as-of date.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--asof", required=True, help="Market/PIT as-of date, YYYY-MM-DD.")
    parser.add_argument("--positioning-history-start", default="2018-01-01")
    return parser.parse_args()


def parse_asof(raw: str) -> str:
    return datetime.strptime(raw.strip(), "%Y-%m-%d").date().isoformat()


def run_step(label: str, args: list[str]) -> None:
    cmd = [sys.executable, *args]
    print(f"[defense_daily_refresh] {label}: {' '.join(cmd)}", flush=True)
    subprocess.run(cmd, cwd=str(PROJECT_ROOT), check=True)


def coverage_audit(config_path: Path, asof: str) -> None:
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    active_sql = """
        SELECT DISTINCT c.ticker
        FROM dim_company c
        JOIN dim_industrials_taxonomy t ON t.company_id = c.company_id
        WHERE c.is_active = 1 AND t.model_family = 'defense'
    """
    checks = [
        ("fact_price_ohlcv", "bar_date", "ticker"),
        ("fact_market_snapshot", "asof_date", "ticker"),
        ("feature_market_technical", "asof_date", "ticker"),
        ("feature_financial_statement", "asof_date", "ticker"),
        ("feature_positioning", "asof_date", "ticker"),
        ("fact_fx_rate", "rate_date", "currency_pair"),
    ]
    with sqlite3.connect(db_path) as conn:
        active_count = int(conn.execute(f"SELECT COUNT(*) FROM ({active_sql})").fetchone()[0] or 0)
        print(f"[defense_daily_refresh] active_defense_tickers={active_count}", flush=True)
        for table, date_col, id_col in checks:
            max_date = conn.execute(f"SELECT MAX({date_col}) FROM {table}").fetchone()[0]
            rows_on_asof = int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {date_col} = ?", (asof,)).fetchone()[0] or 0)
            if id_col == "ticker":
                covered = int(
                    conn.execute(
                        f"""
                        SELECT COUNT(DISTINCT x.ticker)
                        FROM {table} x
                        JOIN ({active_sql}) a ON a.ticker = x.ticker
                        WHERE x.{date_col} = ?
                        """,
                        (asof,),
                    ).fetchone()[0]
                    or 0
                )
                print(
                    f"[defense_daily_refresh] {table}.{date_col}: max={max_date} rows_on_{asof}={rows_on_asof} "
                    f"active_tickers_on_{asof}={covered}/{active_count}",
                    flush=True,
                )
            else:
                distinct_count = int(
                    conn.execute(
                        f"SELECT COUNT(DISTINCT {id_col}) FROM {table} WHERE {date_col} = ?",
                        (asof,),
                    ).fetchone()[0]
                    or 0
                )
                print(
                    f"[defense_daily_refresh] {table}.{date_col}: max={max_date} rows_on_{asof}={rows_on_asof} "
                    f"distinct_{id_col}_on_{asof}={distinct_count}",
                    flush=True,
                )


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    asof = parse_asof(args.asof)
    history_start = parse_asof(args.positioning_history_start)

    run_step("sync prices", ["industrials/defense/scripts/03_sync_defense_prices.py", "--asof", asof, "--allow-partial"])
    run_step("build market features", ["industrials/defense/scripts/05_build_defense_market_features.py", "--asof", asof])
    run_step("validate market stage", ["industrials/defense/scripts/06_validate_defense_market_stage.py", "--asof", asof])
    run_step("sync FX", ["industrials/defense/scripts/11_sync_defense_yahoo_fx_rates.py", "--end-date", asof])
    run_step("sync SEC fundamentals incremental", ["industrials/defense/scripts/07_sync_defense_sec_fundamentals.py", "--incremental", "--allow-partial"])
    run_step("build financial features", ["industrials/defense/scripts/08_build_defense_financial_features.py", "--asof", asof])
    run_step("validate financial stage", ["industrials/defense/scripts/08_validate_defense_financial_stage.py", "--asof", asof])
    run_step(
        "refresh positioning daily",
        [
            "industrials/scripts/13_sync_industrials_positioning_upstream.py",
            "--daily-refresh",
            "--history-start",
            history_start,
            "--end-date",
            asof,
        ],
    )
    run_step("validate positioning stage", ["industrials/scripts/14_validate_industrials_sec_positioning_stages.py", "--model-family", MODEL_FAMILY])
    run_step(
        "validate scoring eligibility",
        ["industrials/defense/scripts/10_validate_defense_scoring_eligibility_policy.py", "--asof", asof],
    )
    run_step("publish shadow rank table", ["industrials/defense/scripts/17_publish_defense_shadow_rank_table.py", "--asof", asof])
    run_step("validate shadow rank table", ["industrials/defense/scripts/18_validate_defense_shadow_rank_table.py", "--asof", asof])
    run_step("validate portfolio adapter shadow", ["industrials/defense/scripts/20_validate_defense_portfolio_adapter_shadow.py", "--asof", asof])
    coverage_audit(config_path, asof)


if __name__ == "__main__":
    main()
