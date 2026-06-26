#!/usr/bin/env python3
"""Validate Stage 10 software-infrastructure dashboard/report outputs."""
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


LOGGER = logging.getLogger("software_infrastructure_dashboard_validator")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
CONFIG_KEY = "software_infrastructure_dashboard_reports"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate Stage 10 software-infrastructure dashboard reports.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
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
        cfg_get(config, f"{CONFIG_KEY}.output_dir", "../output/technology_reports/software_infrastructure/dashboard"),
        base_dir=base_dir,
    )
    paths = {
        "rank_table": output_dir / "software_infrastructure_final_rank_table.csv",
        "scorecards": output_dir / "software_infrastructure_company_scorecards.csv",
        "cohort_summary": output_dir / "software_infrastructure_cohort_rank_summary.csv",
        "component_summary": output_dir / "software_infrastructure_component_summary.csv",
        "risk_flags": output_dir / "software_infrastructure_risk_flags.csv",
        "review_queue": output_dir / "software_infrastructure_review_queue.csv",
        "calibration_summary": output_dir / "software_infrastructure_calibration_summary.csv",
        "backtest_leaders": output_dir / "software_infrastructure_backtest_leaders.csv",
        "stage8_candidate_rank_table": output_dir / "software_infrastructure_stage8_candidate_rank_table.csv",
        "html": output_dir / "index.html",
        "manifest": output_dir / "software_infrastructure_dashboard_manifest.json",
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
    if len(calibration_rows) < 5:
        errors.append(f"Calibration summary has too few rows: {len(calibration_rows)}")
    if len(backtest_rows) < 4:
        errors.append(f"Backtest leaders has too few rows: {len(backtest_rows)}")
    if len(stage8_rows) < 20:
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
        }
        missing = sorted(required_rank_fields.difference(rank_rows[0].keys()))
        if missing:
            errors.append(f"Rank table missing fields: {', '.join(missing)}")
        ranks = [row.get("final_rank") for row in rank_rows if row.get("final_rank")]
        if len(ranks) != len(set(ranks)):
            errors.append("Rank table contains duplicate final_rank values.")

    if paths["manifest"].exists():
        try:
            manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
            if int(manifest.get("rank_rows") or 0) != len(rank_rows):
                errors.append(f"Manifest rank_rows mismatch: {manifest.get('rank_rows')} vs {len(rank_rows)}")
            if int(manifest.get("stage8_candidate_rows") or 0) != len(stage8_rows):
                errors.append(
                    f"Manifest stage8_candidate_rows mismatch: {manifest.get('stage8_candidate_rows')} vs {len(stage8_rows)}"
                )
            if str(manifest.get("model_family") or "") != "software_infrastructure":
                errors.append(f"Manifest model_family is not software_infrastructure: {manifest.get('model_family')}")
        except json.JSONDecodeError as exc:
            errors.append(f"Invalid manifest JSON: {exc}")

    if paths["html"].exists():
        text = paths["html"].read_text(encoding="utf-8", errors="ignore")
        if "Software Infrastructure Dashboard" not in text:
            errors.append("HTML report does not contain expected title.")

    if errors:
        for error in errors:
            LOGGER.error(error)
        return 1
    LOGGER.info(
        "Stage 10 software-infrastructure dashboard validation passed: rank_rows=%d cohort_rows=%d backtest_rows=%d output=%s",
        len(rank_rows),
        len(cohort_rows),
        len(backtest_rows),
        output_dir,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
