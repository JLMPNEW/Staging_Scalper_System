#!/usr/bin/env python3
"""Validate Stage 10 technology-hardware dashboard/report outputs."""
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
from technology.core.oos_provenance import OOS_RANK_FIELDS, validate_oos_rank_rows  # noqa: E402
from technology.core.portfolio_candidate_fields import PORTFOLIO_CANDIDATE_FIELDS  # noqa: E402


LOGGER = logging.getLogger("technology_hardware_dashboard_validator")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
CONFIG_KEY = "technology_hardware_dashboard_reports"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate Stage 10 technology-hardware dashboard reports.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--asof", default="", help="Expected dashboard as-of date. When set, validates manifest and rank table dates.")
    parser.add_argument(
        "--historical-mode",
        action="store_true",
        help="Allow PIT historical reports to omit current full-history Stage 8/backtest research rows.",
    )
    return parser.parse_args()


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def require_file(errors: list[str], path: Path, label: str) -> None:
    if not path.exists() or path.stat().st_size == 0:
        errors.append(f"Missing or empty {label}: {path}")


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else resolve_path(
        cfg_get(config, f"{CONFIG_KEY}.output_dir", "../output/technology_reports/technology_hardware/dashboard"),
        base_dir=base_dir,
    )
    paths = {
        "rank_table": output_dir / "technology_hardware_final_rank_table.csv",
        "scorecards": output_dir / "technology_hardware_company_scorecards.csv",
        "cohort_summary": output_dir / "technology_hardware_cohort_rank_summary.csv",
        "component_summary": output_dir / "technology_hardware_component_summary.csv",
        "risk_flags": output_dir / "technology_hardware_risk_flags.csv",
        "review_queue": output_dir / "technology_hardware_review_queue.csv",
        "calibration_summary": output_dir / "technology_hardware_calibration_summary.csv",
        "backtest_leaders": output_dir / "technology_hardware_backtest_leaders.csv",
        "stage8_candidate_rank_table": output_dir / "technology_hardware_stage8_candidate_rank_table.csv",
        "html": output_dir / "index.html",
        "manifest": output_dir / "technology_hardware_dashboard_manifest.json",
    }
    errors: list[str] = []
    for label, path in paths.items():
        require_file(errors, path, label)

    rank_rows = read_rows(paths["rank_table"])
    scorecard_rows = read_rows(paths["scorecards"])
    cohort_rows = read_rows(paths["cohort_summary"])
    component_rows = read_rows(paths["component_summary"])
    calibration_rows = read_rows(paths["calibration_summary"])
    backtest_rows = read_rows(paths["backtest_leaders"])
    stage8_rows = read_rows(paths["stage8_candidate_rank_table"])

    if len(rank_rows) < 20:
        errors.append(f"Rank table has too few rows: {len(rank_rows)}")
    if len(scorecard_rows) != len(rank_rows):
        errors.append(f"Scorecard row count {len(scorecard_rows)} does not match rank rows {len(rank_rows)}")
    if len(cohort_rows) < 2:
        errors.append(f"Cohort summary has too few rows: {len(cohort_rows)}")
    if len(component_rows) < 6:
        errors.append(f"Component summary has too few rows: {len(component_rows)}")
    if not args.historical_mode and len(calibration_rows) < 5:
        errors.append(f"Calibration summary has too few rows: {len(calibration_rows)}")
    if not args.historical_mode and len(backtest_rows) < 4:
        errors.append(f"Backtest leaders has too few rows: {len(backtest_rows)}")
    if not args.historical_mode and len(stage8_rows) < 20:
        errors.append(f"Stage 8 candidate rank table has too few rows: {len(stage8_rows)}")

    if rank_rows:
        required_rank_fields = {
            "ticker",
            "score_model_version",
            "model_family",
            "company_name",
            "sector",
            "industry",
            "subsector",
            "final_rank",
            "final_score",
            "data_quality_confidence",
            "rank_ready_flag",
            "calibration_eligible_flag",
            "review_reason",
            "calibration_cohort_id",
            "calibration_cohort",
            "latest_sec_url",
            "quality_score",
            "valuation_score",
            "growth_score",
            "market_behavior_score",
            "positioning_score",
            "risk_control_score",
            "inventory_days",
        }
        required_rank_fields.update(OOS_RANK_FIELDS)
        required_rank_fields.update(PORTFOLIO_CANDIDATE_FIELDS)
        missing = sorted(required_rank_fields.difference(rank_rows[0].keys()))
        if missing:
            errors.append(f"Rank table missing fields: {', '.join(missing)}")
        ranks = [row.get("final_rank") for row in rank_rows if row.get("final_rank")]
        if len(ranks) != len(set(ranks)):
            errors.append("Rank table contains duplicate final_rank values.")
        if args.asof:
            bad_dates = sorted({str(row.get("asof_date") or "") for row in rank_rows if str(row.get("asof_date") or "") != args.asof})
            if bad_dates:
                errors.append(f"Rank table contains rows outside asof={args.asof}: {bad_dates[:5]}")
        if args.historical_mode:
            errors.extend(validate_oos_rank_rows(rank_rows, asof=args.asof or str(rank_rows[0].get("asof_date") or ""), historical_mode=True))

    if paths["manifest"].exists():
        try:
            manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
            if int(manifest.get("rank_rows") or 0) != len(rank_rows):
                errors.append(f"Manifest rank_rows mismatch: {manifest.get('rank_rows')} vs {len(rank_rows)}")
            if int(manifest.get("stage8_candidate_rows") or 0) != len(stage8_rows):
                errors.append(
                    f"Manifest stage8_candidate_rows mismatch: {manifest.get('stage8_candidate_rows')} vs {len(stage8_rows)}"
                )
            if str(manifest.get("model_family") or "") != "technology_hardware":
                errors.append(f"Manifest model_family is not technology_hardware: {manifest.get('model_family')}")
            if args.asof and str(manifest.get("asof_date") or "") != args.asof:
                errors.append(f"Manifest asof_date mismatch: {manifest.get('asof_date')} vs {args.asof}")
            expected_mode = "historical" if args.historical_mode else "current"
            if args.historical_mode and str(manifest.get("report_mode") or "") != expected_mode:
                errors.append(f"Manifest report_mode mismatch: {manifest.get('report_mode')} vs {expected_mode}")
            if args.historical_mode:
                if str(manifest.get("non_point_in_time_sections") or "") != "omitted":
                    errors.append("Historical manifest did not omit non-point-in-time sections.")
                if int(manifest.get("backtest_summary_rows") or 0) != 0:
                    errors.append(f"Historical manifest backtest_summary_rows should be 0: {manifest.get('backtest_summary_rows')}")
                if int(manifest.get("stage8_candidate_rows") or 0) != 0:
                    errors.append(f"Historical manifest stage8_candidate_rows should be 0: {manifest.get('stage8_candidate_rows')}")
                required_manifest_fields = {
                    "oos_standards_status",
                    "calibration_input_valid_flag",
                    "oos_score_valid_flag",
                    "feature_point_in_time_flag",
                    "future_return_excluded_flag",
                    "non_point_in_time_sections_omitted_flag",
                    "calibration_train_end_date",
                    "calibration_lock_date",
                    "calibration_production_start_date",
                    "calibration_validation_method",
                    "oos_assertion_basis",
                }
                missing_manifest = sorted(required_manifest_fields.difference(manifest.keys()))
                if missing_manifest:
                    errors.append(f"Historical manifest missing OOS fields: {', '.join(missing_manifest)}")
                if str(manifest.get("calibration_input_valid_flag") or "") != "1":
                    errors.append("Historical manifest calibration_input_valid_flag is not 1.")
        except json.JSONDecodeError as exc:
            errors.append(f"Invalid manifest JSON: {exc}")

    if paths["html"].exists():
        text = paths["html"].read_text(encoding="utf-8", errors="ignore")
        if "Technology Hardware Dashboard" not in text:
            errors.append("HTML report does not contain expected title.")

    if errors:
        for error in errors:
            LOGGER.error(error)
        return 1
    LOGGER.info(
        "Stage 10 technology-hardware dashboard validation passed: rank_rows=%d cohort_rows=%d backtest_rows=%d output=%s",
        len(rank_rows),
        len(cohort_rows),
        len(backtest_rows),
        output_dir,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
