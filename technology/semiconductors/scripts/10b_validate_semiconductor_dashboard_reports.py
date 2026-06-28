#!/usr/bin/env python3
"""Validate Stage 10 semiconductor dashboard/report outputs."""
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


LOGGER = logging.getLogger("semiconductor_dashboard_validator")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
CONFIG_KEY = "semiconductor_dashboard_reports"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate Stage 10 semiconductor dashboard reports.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--asof", default="", help="Expected dashboard as-of date. When set, validates manifest and rank table dates.")
    parser.add_argument(
        "--historical-mode",
        action="store_true",
        help="Validate a PIT historical report where non-PIT research/backtest sections are omitted.",
    )
    return parser.parse_args()


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def require_file(errors: list[str], path: Path, label: str, *, nonempty: bool = True) -> None:
    if not path.exists():
        errors.append(f"Missing {label}: {path}")
    elif nonempty and path.stat().st_size == 0:
        errors.append(f"Empty {label}: {path}")


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else resolve_path(
        cfg_get(config, f"{CONFIG_KEY}.output_dir", "../output/technology_reports/semi_dashboard"),
        base_dir=base_dir,
    )
    paths = {
        "rank_table": output_dir / "semiconductor_final_rank_table.csv",
        "scorecards": output_dir / "semiconductor_company_scorecards.csv",
        "cohort_summary": output_dir / "semiconductor_cohort_rank_summary.csv",
        "risk_flags": output_dir / "semiconductor_risk_flags.csv",
        "review_queue": output_dir / "semiconductor_review_queue.csv",
        "overlay_summary": output_dir / "semiconductor_overlay_summary.csv",
        "html": output_dir / "index.html",
        "manifest": output_dir / "semiconductor_dashboard_manifest.json",
    }
    errors: list[str] = []
    for label in ("rank_table", "scorecards", "cohort_summary", "overlay_summary", "html", "manifest"):
        require_file(errors, paths[label], label)
    for label in ("risk_flags", "review_queue"):
        require_file(errors, paths[label], label, nonempty=False)

    rank_rows = read_rows(paths["rank_table"])
    scorecard_rows = read_rows(paths["scorecards"])
    cohort_rows = read_rows(paths["cohort_summary"])
    overlay_rows = read_rows(paths["overlay_summary"])

    if len(rank_rows) < 20:
        errors.append(f"Rank table has too few rows: {len(rank_rows)}")
    if len(scorecard_rows) != len(rank_rows):
        errors.append(f"Scorecard row count {len(scorecard_rows)} does not match rank rows {len(rank_rows)}")
    if len(cohort_rows) < 2:
        errors.append(f"Cohort summary has too few rows: {len(cohort_rows)}")
    if len(overlay_rows) < 1:
        errors.append(f"Overlay summary has too few rows: {len(overlay_rows)}")

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
        if args.asof:
            bad_dates = sorted({str(row.get("asof_date") or "") for row in rank_rows if str(row.get("asof_date") or "") != args.asof})
            if bad_dates:
                errors.append(f"Rank table contains rows outside asof={args.asof}: {bad_dates[:5]}")

    if paths["manifest"].exists():
        try:
            manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
            if int(manifest.get("rank_rows") or 0) != len(rank_rows):
                errors.append(f"Manifest rank_rows mismatch: {manifest.get('rank_rows')} vs {len(rank_rows)}")
            if args.asof and str(manifest.get("asof_date") or "") != args.asof:
                errors.append(f"Manifest asof_date mismatch: {manifest.get('asof_date')} vs {args.asof}")
            expected_mode = "historical" if args.historical_mode else "current"
            if args.historical_mode and str(manifest.get("report_mode") or "") != expected_mode:
                errors.append(f"Manifest report_mode mismatch: {manifest.get('report_mode')} vs {expected_mode}")
        except json.JSONDecodeError as exc:
            errors.append(f"Invalid manifest JSON: {exc}")

    if paths["html"].exists():
        text = paths["html"].read_text(encoding="utf-8", errors="ignore")
        if "Semiconductor Dashboard" not in text:
            errors.append("HTML report does not contain expected title.")

    if errors:
        for error in errors:
            LOGGER.error(error)
        return 1
    LOGGER.info(
        "Stage 10 semiconductor dashboard validation passed: rank_rows=%d cohort_rows=%d overlay_rows=%d output=%s",
        len(rank_rows),
        len(cohort_rows),
        len(overlay_rows),
        output_dir,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
