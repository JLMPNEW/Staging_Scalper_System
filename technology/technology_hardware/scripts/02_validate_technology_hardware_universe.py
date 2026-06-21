#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.universe_validator import UniverseValidationSettings, validate_universe  # noqa: E402


MODEL_FAMILY = "technology_hardware"
UNIVERSE_CSV = PROJECT_ROOT / "ticker_mapping" / "technology_hardware_cleaned.csv"
POLICY_PATH = PACKAGE_ROOT / "technology_hardware" / "data" / "technology_hardware_universe_policy.yaml"
COHORT_PATH = PACKAGE_ROOT / "technology_hardware" / "data" / "technology_hardware_cohorts.yaml"


SETTINGS = UniverseValidationSettings(
    description="Validate the loaded technology-hardware universe and cohort assignments.",
    default_config=PACKAGE_ROOT / "config.yaml",
    default_model_family=MODEL_FAMILY,
    default_unassigned_cohort_id="tech_hw_unassigned",
    unassigned_issue_type="unassigned_technology_hardware_cohort",
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

