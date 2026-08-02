from __future__ import annotations

from pathlib import Path

from technology.core.signal_diagnostics import (
    SignalDiagnosticsSettings,
    run_signal_diagnostics,
    validate_signal_diagnostics_outputs,
)


PACKAGE_ROOT = Path(__file__).resolve().parents[1]

SPECIALIZED_MEASUREMENT_SPECS = [
    (
        "annual_recurring_revenue_yoy_growth",
        "annual_recurring_revenue_yoy_growth_score",
        True,
        None,
    ),
    (
        "annual_recurring_revenue_to_revenue",
        "annual_recurring_revenue_to_revenue_score",
        True,
        lambda value: 0.0 <= value <= 5.0,
    ),
    (
        "net_revenue_retention_level",
        "net_revenue_retention_level_score",
        True,
        lambda value: 0.50 <= value <= 2.00,
    ),
    (
        "net_revenue_retention_yoy_change",
        "net_revenue_retention_yoy_change_score",
        True,
        None,
    ),
    (
        "disclosed_billings_yoy_growth",
        "disclosed_billings_yoy_growth_score",
        True,
        None,
    ),
    (
        "subscription_revenue_yoy_growth",
        "subscription_revenue_yoy_growth_score",
        True,
        None,
    ),
    (
        "subscription_revenue_mix",
        "subscription_revenue_mix_score",
        True,
        lambda value: 0.0 <= value <= 1.20,
    ),
]

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
    measurement_subfeatures=SPECIALIZED_MEASUREMENT_SPECS,
    measurement_metric_version="software_specialized_measurement_v1",
)


def run_software_infrastructure_signal_diagnostics() -> int:
    return run_signal_diagnostics(SETTINGS)


def validate_software_infrastructure_signal_diagnostics() -> int:
    return validate_signal_diagnostics_outputs(SETTINGS)
