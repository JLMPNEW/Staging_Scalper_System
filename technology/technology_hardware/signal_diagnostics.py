from __future__ import annotations

from pathlib import Path

from technology.core.signal_diagnostics import (
    SignalDiagnosticsSettings,
    run_signal_diagnostics,
    validate_signal_diagnostics_outputs,
)


PACKAGE_ROOT = Path(__file__).resolve().parents[1]

SETTINGS = SignalDiagnosticsSettings(
    description="Run Stage 8A historical signal diagnostics for technology hardware.",
    validate_description="Validate Stage 8A historical signal diagnostics for technology hardware.",
    default_config=PACKAGE_ROOT / "config.yaml",
    config_key="technology_hardware_signal_diagnostics",
    default_model_family="technology_hardware",
    default_output_dir="../output/technology_reports/technology_hardware/signal_diagnostics",
    default_benchmark_ticker="QQQ",
    default_calibrated_config_key="technology_hardware_calibrated_scoring",
    default_price_source_config_key="technology_hardware_research",
    default_excluded_subfeatures=[
        "wsts_cycle_exposure",
        "deferred_revenue_yoy_growth",
        "rpo_yoy_growth",
        "rpo_to_revenue",
    ],
)


def run_technology_hardware_signal_diagnostics() -> int:
    return run_signal_diagnostics(SETTINGS)


def validate_technology_hardware_signal_diagnostics() -> int:
    return validate_signal_diagnostics_outputs(SETTINGS)
