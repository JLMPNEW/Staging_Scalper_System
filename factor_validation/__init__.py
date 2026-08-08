"""Shared, production-neutral factor validation primitives.

The package deliberately contains no sector adapters, configuration loaders, or file I/O.
Sector pipelines remain responsible for point-in-time data construction and promotion policy.
"""

from factor_validation.core import (
    CONTRACT_VERSION,
    EvaluationCadence,
    FactorObservation,
    FactorValidationConfig,
    FactorValidationResult,
    HACInference,
    IndependentWindowInference,
    PerDateDiagnostic,
    QuantileDiagnostics,
    RegimeDiagnostic,
    average_ranks,
    evaluation_cadence,
    hac_lag_for_overlapping_labels,
    infer_evaluation_step_trading_days,
    independent_window_mean_inference,
    newey_west_mean_inference,
    quantile_diagnostics,
    spearman_rank_correlation,
    validate_factor,
)
from factor_validation.fdr import (
    FDRDecision,
    FDRFamily,
    apply_benjamini_hochberg,
)

__all__ = [
    "CONTRACT_VERSION",
    "EvaluationCadence",
    "FDRDecision",
    "FDRFamily",
    "FactorObservation",
    "FactorValidationConfig",
    "FactorValidationResult",
    "HACInference",
    "IndependentWindowInference",
    "PerDateDiagnostic",
    "QuantileDiagnostics",
    "RegimeDiagnostic",
    "apply_benjamini_hochberg",
    "average_ranks",
    "evaluation_cadence",
    "hac_lag_for_overlapping_labels",
    "infer_evaluation_step_trading_days",
    "independent_window_mean_inference",
    "newey_west_mean_inference",
    "quantile_diagnostics",
    "spearman_rank_correlation",
    "validate_factor",
]
