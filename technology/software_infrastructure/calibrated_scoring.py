from __future__ import annotations

import sys
from pathlib import Path

from technology.core.calibrated_scoring import (
    CalibratedScoringSettings,
    build_calibrated_scores,
    validate_calibrated_scores,
)


PACKAGE_ROOT = Path(__file__).resolve().parents[1]

SETTINGS = CalibratedScoringSettings(
    build_description="Build Stage 7 calibrated software infrastructure scores.",
    validate_description="Validate Stage 7 calibrated software infrastructure scores.",
    default_config=PACKAGE_ROOT / "config.yaml",
    config_key="software_infrastructure_calibrated_scoring",
    default_model_family="software_infrastructure",
    default_source_id="software_infrastructure_calibrated_score_v1",
    default_baseline_source_id="software_infrastructure_scoring_contract",
    default_model_version="software_infrastructure_stage7_calibrated_v1",
    build_run_type="build_software_infrastructure_calibrated_scores",
    validation_run_type="validate_software_infrastructure_calibrated_scores",
    default_component_weights={
        "quality": 0.30,
        "valuation": 0.20,
        "growth": 0.15,
        "market_behavior": 0.15,
        "positioning": 0.10,
        "risk_control": 0.10,
    },
    default_overlay_components=[],
)

CHALLENGER_SETTINGS = CalibratedScoringSettings(
    build_description="Build Stage 7 challenger software infrastructure scores.",
    validate_description="Validate Stage 7 challenger software infrastructure scores.",
    default_config=PACKAGE_ROOT / "config.yaml",
    config_key="software_infrastructure_stage7_challenger_scoring",
    default_model_family="software_infrastructure",
    default_source_id="software_infrastructure_stage7_challenger_score_v1",
    default_baseline_source_id="software_infrastructure_scoring_contract",
    default_model_version="software_infrastructure_stage7_calibrated_v1",
    build_run_type="build_software_infrastructure_stage7_challenger_scores",
    validation_run_type="validate_software_infrastructure_stage7_challenger_scores",
    default_component_weights={
        "quality": 0.30,
        "valuation": 0.20,
        "growth": 0.15,
        "market_behavior": 0.15,
        "positioning": 0.10,
        "risk_control": 0.10,
    },
    default_overlay_components=[],
)


def build_software_infrastructure_calibrated_scores() -> None:
    build_calibrated_scores(SETTINGS)


def validate_software_infrastructure_calibrated_scores() -> int:
    return validate_calibrated_scores(SETTINGS)


def build_software_infrastructure_stage7_challenger_scores() -> None:
    build_calibrated_scores(CHALLENGER_SETTINGS)


def validate_software_infrastructure_stage7_challenger_scores() -> int:
    return validate_calibrated_scores(CHALLENGER_SETTINGS)


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "validate":
        sys.argv.pop(1)
        raise SystemExit(validate_software_infrastructure_calibrated_scores())
    if command == "challenger":
        sys.argv.pop(1)
        build_software_infrastructure_stage7_challenger_scores()
    elif command == "validate-challenger":
        sys.argv.pop(1)
        raise SystemExit(validate_software_infrastructure_stage7_challenger_scores())
    else:
        build_software_infrastructure_calibrated_scores()
