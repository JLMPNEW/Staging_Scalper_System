#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.scoring_features import ScoringFeatureSettings, run_scoring_feature_build  # noqa: E402


SETTINGS = ScoringFeatureSettings(
    description="Build the technology hardware Stage 6A scoring feature contract.",
    default_config=PACKAGE_ROOT / "config.yaml",
    config_key="technology_hardware_scoring_features",
    default_model_family="technology_hardware",
    default_source_id="technology_hardware_scoring_contract",
    run_type="build_technology_hardware_scoring_features",
    validation_run_type="validate_technology_hardware_scoring_features",
)


if __name__ == "__main__":
    run_scoring_feature_build(SETTINGS)
