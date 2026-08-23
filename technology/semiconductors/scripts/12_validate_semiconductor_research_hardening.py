#!/usr/bin/env python3
"""Hardening checks for semiconductor research/calibration infrastructure."""
from __future__ import annotations

import argparse
import csv
import importlib.util
import logging
import sys
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from technology.core.db import connect, init_db  # noqa: E402
from technology.core.logging_utils import configure_utc_logging  # noqa: E402
from technology.core.text_norm import normalize_ticker  # noqa: E402
from technology.core.universe_validator import expected_current_ticker_count  # noqa: E402
from technology.semiconductors.optuna_calibration import load_membership_intervals  # noqa: E402


LOGGER = logging.getLogger("validate_semiconductor_research_hardening")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_OUTPUT = PROJECT_ROOT / "output" / "technology_reports" / "audits" / "semiconductor_research_hardening.csv"
DIAGNOSTICS_SCRIPT = PACKAGE_ROOT / "semiconductors" / "scripts" / "07_run_semiconductor_signal_diagnostics.py"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate semiconductor Stage 8 hardening prerequisites.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def load_diagnostics_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("semiconductor_signal_diagnostics_hardening", DIAGNOSTICS_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {DIAGNOSTICS_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["check", "status", "detail"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def close_enough(actual: Any, expected: float, *, tolerance: float = 1e-9) -> bool:
    try:
        return abs(float(actual) - expected) <= tolerance
    except (TypeError, ValueError):
        return False


def split_repricing_check() -> tuple[str, str]:
    diag = load_diagnostics_module()
    series = diag.PriceSeries()
    series.dates = [date(2024, 6, 7), date(2024, 6, 10)]
    # Synthetic 10-for-1 split with no economic move: raw close drops 90%, but
    # adjusted close is flat. Market-cap-based valuation should remain flat.
    series.close = [1200.0, 120.0]
    series.adj = [120.0, 120.0]
    series.volume = [1_000_000.0, 10_000_000.0]
    feats: dict[str, Any] = {
        "_val_asof": "2024-06-07",
        "_market_cap_f": 1_000_000_000_000.0,
        "_net_cash_f": 0.0,
        "_fx_balance_rate_f": 1.0,
        "fcf_yield": 0.02,
        "ev_gross_profit": 10.0,
        "ev_operating_income": 20.0,
    }
    diag.reprice_valuation(feats, series, date(2024, 6, 10))
    if not close_enough(feats.get("fcf_yield"), 0.02):
        return "FAIL", f"fcf_yield changed across pure split: {feats.get('fcf_yield')}"
    if not close_enough(feats.get("ev_gross_profit"), 10.0):
        return "FAIL", f"ev_gross_profit changed across pure split: {feats.get('ev_gross_profit')}"
    if not close_enough(feats.get("ev_operating_income"), 20.0):
        return "FAIL", f"ev_operating_income changed across pure split: {feats.get('ev_operating_income')}"
    return "PASS", "pure split leaves market-cap valuation ratios unchanged"


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_csv = args.output_csv.expanduser().resolve()
    model_family = str(cfg_get(config, "technology_universe.initial_subsector", "semiconductors"))
    expected_universe = expected_current_ticker_count(
        config,
        base_dir=base_dir,
        effective_date=date.today(),
    )
    min_historical = int(cfg_get(config, "technology_universe.min_historical_membership_tickers", 20))

    rows: list[dict[str, Any]] = []
    split_status, split_detail = split_repricing_check()
    rows.append({"check": "split_adjusted_valuation_repricing", "status": split_status, "detail": split_detail})

    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        active_tickers = [
            normalize_ticker(row["ticker"])
            for row in conn.execute(
                """
                SELECT c.ticker
                FROM dim_company c
                JOIN dim_technology_taxonomy t
                  ON t.ticker = c.ticker
                 AND t.model_family = ?
                WHERE c.is_active = 1
                ORDER BY c.ticker
                """,
                (model_family,),
            ).fetchall()
            if normalize_ticker(row["ticker"])
        ]
        rows.append(
            {
                "check": "active_universe_count",
                "status": "PASS" if len(active_tickers) == expected_universe else "FAIL",
                "detail": f"{len(active_tickers)}/{expected_universe} active {model_family} tickers",
            }
        )
        membership, _cohorts, stats = load_membership_intervals(conn, model_family=model_family, include_inactive=False)
        missing_current = sorted(set(active_tickers).difference(membership))
        rows.append(
            {
                "check": "current_membership_coverage",
                "status": "PASS" if not missing_current and len(membership) == len(active_tickers) else "FAIL",
                "detail": f"{len(membership)}/{len(active_tickers)} current tickers; missing={missing_current[:20]}",
            }
        )
        pit_tickers = int(stats.get("point_in_time_membership_tickers") or 0)
        rows.append(
            {
                "check": "point_in_time_membership_backfill",
                "status": "PASS" if pit_tickers >= len(active_tickers) else "FAIL",
                "detail": f"{pit_tickers}/{len(active_tickers)} tickers have true PIT membership rows",
            }
        )
        historical_row = conn.execute(
            """
            SELECT COUNT(DISTINCT m.ticker) AS tickers
            FROM dim_universe_membership m
            LEFT JOIN dim_company c ON c.ticker = m.ticker
            WHERE m.model_family = ?
              AND m.point_in_time_flag = 1
              AND m.is_current_member = 0
              AND COALESCE(c.is_active, 0) = 0
            """,
            (model_family,),
        ).fetchone()
        historical_tickers = int(historical_row["tickers"] or 0) if historical_row is not None else 0
        rows.append(
            {
                "check": "historical_delisted_membership_backfill",
                "status": "PASS" if historical_tickers >= min_historical else "FAIL",
                "detail": f"{historical_tickers}/{min_historical} minimum inactive/delisted PIT tickers loaded",
            }
        )

    write_csv(output_csv, rows)
    failures = [row for row in rows if row["status"] == "FAIL"]
    for row in rows:
        if row["status"] == "FAIL":
            LOGGER.error("%s: %s", row["check"], row["detail"])
        elif row["status"] == "WARN":
            LOGGER.warning("%s: %s", row["check"], row["detail"])
        else:
            LOGGER.info("%s: %s", row["check"], row["detail"])
    LOGGER.info("Wrote semiconductor hardening validation report: %s", output_csv)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
