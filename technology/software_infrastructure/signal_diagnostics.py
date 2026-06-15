from __future__ import annotations

from pathlib import Path

from technology.core.signal_diagnostics import (
    SignalDiagnosticsSettings,
    run_signal_diagnostics,
    validate_signal_diagnostics_outputs,
)


PACKAGE_ROOT = Path(__file__).resolve().parents[1]

SETTINGS = SignalDiagnosticsSettings(
    description="Run Stage 8A historical signal diagnostics for software infrastructure.",
    validate_description="Validate Stage 8A historical signal diagnostics for software infrastructure.",
    default_config=PACKAGE_ROOT / "config.yaml",
    config_key="software_infrastructure_signal_diagnostics",
    default_model_family="software_infrastructure",
    default_output_dir="../output/technology_reports/software_infrastructure/signal_diagnostics",
    default_benchmark_ticker="QQQ",
    default_calibrated_config_key="software_infrastructure_calibrated_scoring",
    default_price_source_config_key="software_infrastructure_research",
    default_excluded_subfeatures=[
        "wsts_cycle_exposure",
        "inventory_days_yoy_change",
        "inventory_to_revenue_growth_gap",
    ],
)


def run_software_infrastructure_signal_diagnostics() -> int:
    return run_signal_diagnostics(SETTINGS)


def validate_software_infrastructure_signal_diagnostics() -> int:
    return validate_signal_diagnostics_outputs(SETTINGS)
