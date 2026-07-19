#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.db import connect  # noqa: E402
from industrials.core.reports import write_csv_atomic  # noqa: E402
from industrials.machinery.scoring import (  # noqa: E402
    build_scoring_feature_rows,
    finalize_rank_rows,
    parse_asof,
    publish_dashboard,
    survivorship_sidecar,
    write_json_atomic,
)
from portfolio_layer.scores.adapters import run_adapter  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
REPORT_FIELDS = [
    "asof_date",
    "status",
    "row_count",
    "rank_ready_count",
    "portfolio_adapter_row_count",
    "output_dir",
    "error",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build survivorship-corrected machinery rank files for portfolio calibration history."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default=date.today().isoformat())
    parser.add_argument(
        "--exclude-end-date",
        action="store_true",
        help="Omit the exact end date so the current-date publisher can own that immutable snapshot.",
    )
    parser.add_argument("--frequency", choices=("daily", "weekly"), default="daily")
    parser.add_argument("--max-dates", type=int, default=0)
    parser.add_argument("--rebuild-features", action="store_true")
    parser.add_argument("--allow-zero-eligible", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--continue-on-error", action="store_true")
    return parser.parse_args()


def available_dates(
    conn: sqlite3.Connection,
    *,
    start_date: str,
    end_date: str,
    benchmark: str,
    primary_source: str,
) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT bar_date
        FROM fact_price_ohlcv
        WHERE ticker = ? AND source_id = ? AND bar_date BETWEEN ? AND ?
        ORDER BY bar_date
        """,
        (benchmark, primary_source, start_date, end_date),
    ).fetchall()
    dates = [str(row["bar_date"]) for row in rows]
    if dates:
        return dates
    fallback = conn.execute(
        """
        SELECT DISTINCT asof_date
        FROM feature_market_technical
        WHERE model_family = ? AND asof_date BETWEEN ? AND ?
        ORDER BY asof_date
        """,
        ("machinery", start_date, end_date),
    ).fetchall()
    return [str(row["asof_date"]) for row in fallback]


def weekly_dates(dates: list[str]) -> list[str]:
    by_week: dict[tuple[int, int], str] = {}
    for raw in dates:
        parsed = date.fromisoformat(raw)
        iso_year, iso_week, _ = parsed.isocalendar()
        by_week[(iso_year, iso_week)] = raw
    return [by_week[key] for key in sorted(by_week)]


def rebuild_features(
    *,
    config_path: Path,
    db_path: Path,
    asof: str,
    report_root: Path,
) -> None:
    report_root.mkdir(parents=True, exist_ok=True)
    scripts = [
        (
            "industrials/machinery/scripts/05_build_machinery_market_features.py",
            ["--asof", asof, "--output-csv", str(report_root / "market_feature_coverage.csv")],
        ),
        (
            "industrials/machinery/scripts/08_build_machinery_financial_features.py",
            [
                "--asof",
                asof,
                "--output-csv",
                str(report_root / "financial_feature_coverage.csv"),
                "--availability-output-csv",
                str(report_root / "financial_metric_availability.csv"),
                "--suppress-data-quality-issues",
            ],
        ),
        (
            "industrials/machinery/scripts/09_import_machinery_positioning.py",
            ["--asof", asof, "--output-csv", str(report_root / "positioning_import_coverage.csv")],
        ),
    ]
    for script, extra in scripts:
        subprocess.run(
            [sys.executable, script, "--config", str(config_path), "--db", str(db_path), *extra],
            cwd=PROJECT_ROOT,
            check=True,
        )


def validate_portfolio_handoff(*, sector_output_root: Path, asof: str) -> int:
    result = run_adapter(
        {
            "model_family": "machinery",
            "adapter": "industrial_family",
            "file_mode": "dated",
            "file_path": "industrials/machinery/dashboard/{yyyy-mm-dd}/machinery_final_rank_table.csv",
            "sector": "Industrials",
            "industry": "Machinery",
            "industry_aggregate": "Machinery",
            "require_oos_score_valid": True,
        },
        sector_output_root,
        asof,
    )
    if not result.rows:
        raise ValueError("Portfolio adapter returned no machinery rows")
    if result.source_asof_date != asof:
        raise ValueError(
            f"Portfolio adapter source_asof_date={result.source_asof_date} expected={asof}"
        )
    if any(row.investable_eligible for row in result.rows):
        raise ValueError("Shadow machinery rows must not be investable")
    if any(row.oos_score_valid_flag for row in result.rows):
        raise ValueError("Shadow machinery rows must not be OOS-valid")
    return len(result.rows)


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    start_date = parse_asof(args.start_date or str(cfg_get(config, "machinery_scoring.history_start_date", "2019-01-02")))
    end_date = parse_asof(args.end_date)
    if end_date < start_date:
        raise ValueError("end-date must be on or after start-date")
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    dashboard_root = resolve_path(cfg_get(config, "machinery_scoring.dashboard_root"), base_dir=base_dir)
    sector_output_root = dashboard_root.parents[2]
    benchmark = str(cfg_get(config, "industrials_universe.benchmark_ticker", "XLI"))
    primary_source = str(cfg_get(config, "market_data_policy.scoring_primary_source", "yahoo_finance_adjusted"))
    timeout = float(cfg_get(config, "runtime.sqlite_timeout_sec", 120.0))
    with connect(db_path, timeout_sec=timeout) as conn:
        dates = available_dates(
            conn,
            start_date=start_date,
            end_date=end_date,
            benchmark=benchmark,
            primary_source=primary_source,
        )
    # Exclude the end date BEFORE weekly downsampling: if the end date is a
    # week's representative (last trading day), excluding it afterwards would
    # drop that week entirely instead of falling back to an earlier day.
    if args.exclude_end_date:
        dates = [asof for asof in dates if asof != end_date]
    if args.frequency == "weekly":
        dates = weekly_dates(dates)
    if args.max_dates > 0:
        dates = dates[: args.max_dates]
    if not dates:
        raise ValueError(
            f"No {benchmark} price dates or machinery feature dates available from {start_date} through {end_date}"
        )
    weights = cfg_get(config, "machinery_scoring.component_weights", {}) or {}
    if not isinstance(weights, dict):
        raise ValueError("machinery_scoring.component_weights must be a mapping")
    report: list[dict[str, Any]] = []
    for asof in dates:
        output_dir = dashboard_root / asof
        try:
            if args.rebuild_features:
                rebuild_features(
                    config_path=config_path,
                    db_path=db_path,
                    asof=asof,
                    report_root=(
                        dashboard_root.parent
                        / "historical_backfill"
                        / "stage_reports"
                        / asof
                    ),
                )
            with connect(db_path, timeout_sec=timeout) as conn:
                feature_rows = build_scoring_feature_rows(
                    conn,
                    asof=asof,
                    component_weights=weights,
                    min_score_confidence=float(cfg_get(config, "machinery_scoring.min_score_confidence", 0.40)),
                    max_staleness_days=int(cfg_get(config, "market_data_policy.max_staleness_days", 7)),
                    min_avg_dollar_volume=float(
                        cfg_get(config, "market_data_policy.min_avg_dollar_volume_60d_for_full_features", 5000000)
                    ),
                )
            rank_rows = finalize_rank_rows(
                feature_rows,
                score_model_version=str(cfg_get(config, "machinery_scoring.score_model_version")),
                model_version=str(cfg_get(config, "machinery_scoring.model_version")),
                scoring_contract_version=str(cfg_get(config, "machinery_scoring.contract_version")),
            )
            historical_rows = survivorship_sidecar(rank_rows)
            rank_ready_count = sum(row["rank_ready_flag"] == "1" for row in historical_rows)
            if rank_ready_count == 0 and not args.allow_zero_eligible:
                raise ValueError("No rank-ready machinery rows; build point-in-time source features before publishing")
            publish_dashboard(
                output_dir=output_dir,
                rows=historical_rows,
                asof=asof,
                allow_overwrite=args.force,
            )
            portfolio_adapter_row_count = validate_portfolio_handoff(
                sector_output_root=sector_output_root,
                asof=asof,
            )
            report.append(
                {
                    "asof_date": asof,
                    "status": "PASS",
                    "row_count": len(historical_rows),
                    "rank_ready_count": rank_ready_count,
                    "portfolio_adapter_row_count": portfolio_adapter_row_count,
                    "output_dir": str(output_dir),
                    "error": "",
                }
            )
        except Exception as exc:
            report.append(
                {
                    "asof_date": asof,
                    "status": "FAIL",
                    "row_count": 0,
                    "rank_ready_count": 0,
                    "portfolio_adapter_row_count": 0,
                    "output_dir": str(output_dir),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            if not args.continue_on_error:
                break
    report_root = dashboard_root.parent / "historical_backfill"
    report_root.mkdir(parents=True, exist_ok=True)
    report_csv = report_root / f"machinery_history_{start_date}_{end_date}_{args.frequency}.csv"
    write_csv_atomic(report_csv, REPORT_FIELDS, report)
    failures = [row for row in report if row["status"] != "PASS"]
    summary = {
        "acceptance": "PASS" if not failures and len(report) == len(dates) else "FAIL",
        "start_date": start_date,
        "end_date": end_date,
        "frequency": args.frequency,
        "end_date_excluded": bool(args.exclude_end_date),
        "planned_dates": len(dates),
        "completed_dates": len(report),
        "failed_dates": len(failures),
        "report_csv": str(report_csv),
    }
    write_json_atomic(report_csv.with_suffix(".json"), summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["acceptance"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
