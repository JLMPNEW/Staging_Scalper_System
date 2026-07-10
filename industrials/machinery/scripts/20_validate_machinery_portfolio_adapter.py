#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.machinery.scoring import parse_asof  # noqa: E402
from portfolio_layer.scores.adapters import run_adapter  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate machinery ingestion through the portfolio industrial adapter.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Accepted for orchestrator consistency.")
    parser.add_argument("--asof", required=True)
    parser.add_argument("--sector-output-root", type=Path, default=PROJECT_ROOT / "output")
    parser.add_argument("--expect-research-eligible", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    asof = parse_asof(args.asof)
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
        args.sector_output_root.expanduser().resolve(),
        asof,
    )
    errors: list[str] = []
    if not result.rows:
        errors.append("portfolio adapter returned no machinery rows")
    if result.source_asof_date != asof:
        errors.append(f"source_asof_date={result.source_asof_date} expected={asof}")
    if any(row.investable_eligible for row in result.rows):
        errors.append("shadow machinery rows must not be investable")
    if any(row.oos_score_valid_flag for row in result.rows):
        errors.append("shadow machinery rows must not be OOS valid")
    research_eligible = sum(row.calibration_research_eligible for row in result.rows)
    if args.expect_research_eligible and research_eligible == 0:
        errors.append("expected survivorship-corrected research rows but adapter returned zero")
    if not args.expect_research_eligible and research_eligible:
        errors.append("live shadow dashboard unexpectedly exposed research calibration rows")
    summary = {
        "acceptance": "PASS" if not errors else "FAIL",
        "adapter": result.adapter,
        "source_pipeline": result.source_pipeline,
        "source_asof_date": result.source_asof_date,
        "rows": len(result.rows),
        "investable_rows": sum(row.investable_eligible for row in result.rows),
        "research_eligible_rows": research_eligible,
        "errors": errors,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
