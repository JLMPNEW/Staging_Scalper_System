#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.universe_loader import UniverseLoadSettings, run_universe_load  # noqa: E402


MODEL_FAMILY = "software_infrastructure"
UNIVERSE_CSV = PROJECT_ROOT / "ticker_mapping" / "software_infrastructure_tickers.csv"
POLICY_PATH = PACKAGE_ROOT / "software_infrastructure" / "data" / "software_infrastructure_universe_policy.yaml"
COHORT_PATH = PACKAGE_ROOT / "software_infrastructure" / "data" / "software_infrastructure_cohorts.yaml"


SETTINGS = UniverseLoadSettings(
    description="Load the software-infrastructure universe and cohorts into technology.sqlite.",
    default_config=PACKAGE_ROOT / "config.yaml",
    default_model_family=MODEL_FAMILY,
    seed_source_id="software_infrastructure_ticker_seed",
    cohort_source_id="software_infrastructure_cohort_policy",
    default_unassigned_cohort_id="software_infra_unassigned",
    default_unassigned_cohort_name="Unassigned software infrastructure review",
    cohort_label="software infrastructure cohorts",
    source_of_truth_label="software infrastructure source of truth",
    missing_cik_issue_detail=(
        "CIK is missing from software_infrastructure_tickers.csv; rank eligibility is blocked until resolved."
    ),
    unassigned_issue_type="unassigned_software_infrastructure_cohort",
    unassigned_issue_detail="Ticker is not assigned to a core software-infrastructure calibration cohort.",
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
