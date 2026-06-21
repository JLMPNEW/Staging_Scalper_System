#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.universe_loader import UniverseLoadSettings, run_universe_load  # noqa: E402


MODEL_FAMILY = "technology_hardware"
UNIVERSE_CSV = PROJECT_ROOT / "ticker_mapping" / "technology_hardware_cleaned.csv"
POLICY_PATH = PACKAGE_ROOT / "technology_hardware" / "data" / "technology_hardware_universe_policy.yaml"
COHORT_PATH = PACKAGE_ROOT / "technology_hardware" / "data" / "technology_hardware_cohorts.yaml"


SETTINGS = UniverseLoadSettings(
    description="Load the technology-hardware universe and cohorts into technology.sqlite.",
    default_config=PACKAGE_ROOT / "config.yaml",
    default_model_family=MODEL_FAMILY,
    seed_source_id="technology_hardware_ticker_seed",
    cohort_source_id="technology_hardware_cohort_policy",
    default_unassigned_cohort_id="tech_hw_unassigned",
    default_unassigned_cohort_name="Unassigned technology hardware review",
    cohort_label="technology hardware cohorts",
    source_of_truth_label="technology hardware source of truth",
    missing_cik_issue_detail=(
        "CIK is missing from technology_hardware_cleaned.csv; rank eligibility is blocked until resolved."
    ),
    unassigned_issue_type="unassigned_technology_hardware_cohort",
    unassigned_issue_detail="Ticker is not assigned to a core technology-hardware calibration cohort.",
)


def default_args(argv: list[str]) -> list[str]:
    return [
        "--universe-csv",
        str(UNIVERSE_CSV),
        "--policy",
        str(POLICY_PATH),
        "--cohorts",
        str(COHORT_PATH),
        "--model-family",
        MODEL_FAMILY,
        *argv,
    ]


if __name__ == "__main__":
    run_universe_load(SETTINGS, default_args(sys.argv[1:]))

