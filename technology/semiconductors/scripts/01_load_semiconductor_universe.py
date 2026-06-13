#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.universe_loader import UniverseLoadSettings, run_universe_load  # noqa: E402


SETTINGS = UniverseLoadSettings(
    description="Load the semiconductor universe and cohorts into technology.sqlite.",
    default_config=PACKAGE_ROOT / "config.yaml",
    default_model_family="semiconductors",
    seed_source_id="semiconductor_ticker_seed",
    cohort_source_id="semiconductor_cohort_policy",
    default_unassigned_cohort_id="semi_unassigned",
    default_unassigned_cohort_name="Unassigned semiconductor review",
    cohort_label="semiconductor cohorts",
    source_of_truth_label="semiconductor source of truth",
    missing_cik_issue_detail=(
        "CIK is missing from semiconductor_tickers.csv; permitted for some foreign OTC/ADR names "
        "but excluded from SEC-fundamental coverage until resolved."
    ),
    unassigned_issue_type="unassigned_semiconductor_cohort",
    unassigned_issue_detail="Ticker is not assigned to one of the four core semiconductor calibration cohorts.",
)


if __name__ == "__main__":
    run_universe_load(SETTINGS)
