#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from technology.core.logging_utils import configure_utc_logging  # noqa: E402


LOGGER = logging.getLogger("software_infrastructure_portfolio_backtest_validator")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
CONFIG_KEY = "software_infrastructure_portfolio_backtest"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate Stage 9 software infrastructure portfolio backtest outputs.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else resolve_path(
        cfg_get(config, f"{CONFIG_KEY}.output_dir", "../output/technology_reports/software_infrastructure/backtests"),
        base_dir=base_dir,
    )
    summary_path = output_dir / "software_infrastructure_portfolio_backtest_summary.csv"
    periods_path = output_dir / "software_infrastructure_portfolio_backtest_periods.csv"
    holdings_path = output_dir / "software_infrastructure_portfolio_backtest_holdings.csv"
    manifest_path = output_dir / "software_infrastructure_portfolio_backtest_manifest.json"
    errors: list[str] = []
    for path in (summary_path, periods_path, holdings_path, manifest_path):
        if not path.exists() or path.stat().st_size == 0:
            errors.append(f"Missing or empty Stage 9 output: {path}")
    summary_rows = read_rows(summary_path)
    period_rows = read_rows(periods_path)
    holding_rows = read_rows(holdings_path)
    if len(summary_rows) < 8:
        errors.append(f"Too few backtest summary rows: {len(summary_rows)}")
    if len(period_rows) < 100:
        errors.append(f"Too few backtest period rows: {len(period_rows)}")
    if len(holding_rows) < 1000:
        errors.append(f"Too few backtest holding rows: {len(holding_rows)}")
    if summary_rows:
        models = {row.get("model_name") for row in summary_rows}
        production_model = str(cfg_get(config, f"{CONFIG_KEY}.production_model_name", "stage8_promoted_production_v1"))
        challenger_model = str(cfg_get(config, f"{CONFIG_KEY}.stage7_challenger_model_name", "stage7_challenger_v1"))
        if production_model not in models:
            errors.append(f"Production model missing from backtest summary: {production_model}")
        if challenger_model not in models:
            errors.append(f"Stage 7 challenger missing from backtest summary: {challenger_model}")
        for field in ("annualized_return", "max_drawdown", "avg_turnover", "avg_excess_return_vs_qqq", "avg_excess_return_vs_equal_weight"):
            if field not in summary_rows[0]:
                errors.append(f"Backtest summary missing field: {field}")
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if int(manifest.get("panel_dates") or 0) < 100:
                errors.append(f"Manifest panel_dates too low: {manifest.get('panel_dates')}")
        except json.JSONDecodeError as exc:
            errors.append(f"Invalid manifest JSON: {exc}")
    if errors:
        for error in errors:
            LOGGER.error(error)
        return 1
    LOGGER.info(
        "Stage 9 portfolio backtest validation passed: summary_rows=%d period_rows=%d holding_rows=%d output=%s",
        len(summary_rows),
        len(period_rows),
        len(holding_rows),
        output_dir,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
