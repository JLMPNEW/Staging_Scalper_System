from __future__ import annotations

# ruff: noqa: E402

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.calibrated_scoring import (
    CalibratedScoringSettings,
    build_calibrated_scores,
    component_weight_specs as core_component_weight_specs,
    subfeature_weight_specs as core_subfeature_weight_specs,
    validate_calibrated_scores,
)
from technology.core.scoring_features import DEFAULT_OVERLAY_COMPONENTS


PACKAGE_ROOT = Path(__file__).resolve().parents[1]

SETTINGS = CalibratedScoringSettings(
    build_description="Build Stage 7 calibrated semiconductor scores.",
    validate_description="Validate Stage 7 calibrated semiconductor scores.",
    default_config=PACKAGE_ROOT / "config.yaml",
    config_key="semiconductor_calibrated_scoring",
    default_model_family="semiconductors",
    default_source_id="semiconductor_calibrated_score_v1",
    default_baseline_source_id="semiconductor_scoring_contract",
    default_model_version="semiconductor_stage7_calibrated_v1",
    build_run_type="build_semiconductor_calibrated_scores",
    validation_run_type="validate_semiconductor_calibrated_scores",
    default_component_weights={
        "valuation": 0.30,
        "quality": 0.25,
        "risk_control": 0.25,
        "positioning": 0.10,
        "market_behavior": 0.10,
        "growth": 0.00,
    },
    default_overlay_components=list(DEFAULT_OVERLAY_COMPONENTS),
)


def build_semiconductor_calibrated_scores() -> None:
    build_calibrated_scores(SETTINGS)


def component_weight_specs(config: dict) -> dict[str, float]:
    return core_component_weight_specs(config, SETTINGS)


def subfeature_weight_specs(config: dict) -> dict[str, list[tuple[str, float]]]:
    return core_subfeature_weight_specs(config, SETTINGS)


def validate_semiconductor_calibrated_scores() -> int:
    return validate_calibrated_scores(SETTINGS)


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "validate":
        sys.argv.pop(1)
        raise SystemExit(validate_semiconductor_calibrated_scores())
    build_semiconductor_calibrated_scores()
