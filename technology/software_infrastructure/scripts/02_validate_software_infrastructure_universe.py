#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.universe_validator import UniverseValidationSettings, validate_universe  # noqa: E402


MODEL_FAMILY = "software_infrastructure"
UNIVERSE_CSV = PROJECT_ROOT / "ticker_mapping" / "software_infrastructure_tickers.csv"
POLICY_PATH = PACKAGE_ROOT / "software_infrastructure" / "data" / "software_infrastructure_universe_policy.yaml"
COHORT_PATH = PACKAGE_ROOT / "software_infrastructure" / "data" / "software_infrastructure_cohorts.yaml"


SETTINGS = UniverseValidationSettings(
    description="Validate the loaded software-infrastructure universe and cohort assignments.",
    default_config=PACKAGE_ROOT / "config.yaml",
    default_model_family=MODEL_FAMILY,
    default_unassigned_cohort_id="software_infra_unassigned",
    unassigned_issue_type="unassigned_software_infrastructure_cohort",
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
    raise SystemExit(validate_universe(SETTINGS, default_args(sys.argv[1:])))
