from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any


ENV_DEFAULT_RE = re.compile(
    re.escape("$" + "{")
    + r"([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?"
    + re.escape("}")
)
CONSUMER_COHORT_IDS = frozenset(
    {
        "beverages",
        "consumer_staples_distribution_retail",
        "household_personal_tobacco",
        "packaged_foods_agricultural_products",
    }
)
ALLOWED_ROOT_KEYS = frozenset(
    {
        'positioning',
        'scoring_features',
        'stage7_scoring',
        'stage8_calibration',
        'calibration_scope',
        'stage6b',
        "paths",
        "runtime",
        "source_registry",
        "specialized_metrics",
        "sec_fundamentals",
        "fx_rates",
        "financial_features",
        "specialized_disclosure_census",
        "universe",
        "historical_contract",
        "oos_provenance",
        "market_data_policy",
        "dedicated_parser",
        "factor_validation",
        "portfolio_layer",
        "promotion_framework_v2",
        "promotion_framework_v3",
        "production_score_publisher_v3",
    }
)
REQUIRED_ROOT_KEYS = ALLOWED_ROOT_KEYS
ALLOWED_SECTION_KEYS = {
    'positioning': {
        'ownership_source_id', 'market_positioning_source_id', 'feature_source_id',
        'form4_upstream_db', 'market_positioning_upstream_db', 'upstream_universe_csv',
        'source_identifier_map', 'market_positioning_cache_root',
        'start_date', 'source_birthdates', 'maximum_age_days',
        'minimum_current_coverage', 'lookback_days', 'upstream_source_names',
    },
    'scoring_features': {
        'source_id', 'definition_version', 'minimum_normalization_peer_count',
        'normalize_within_cohort', 'minimum_rank_ready_fraction',
        'accepted_market_quality_statuses', 'accepted_financial_quality_statuses',
        'accepted_positioning_quality_statuses', 'specialized_default_availability',
        'component_weight_default',
    },
    'stage7_scoring': {
        'source_id', 'baseline_source_id', 'model_version', 'promotion_state',
        'portfolio_candidate_gate', 'oos_score_valid_flag', 'neutral_score',
        'minimum_data_quality_confidence',
        'maximum_missing_component_weight', 'minimum_rank_ready_fraction',
        'require_stage6b_measurement_overlay', 'specialized_weight_default',
        'specialized_weight_policy', 'factor_validation_campaign_id',
        'factor_validation_verdict', 'component_weights',
    },
    'stage8_calibration': {
        'mode', 'production_promotion_enabled', 'portfolio_write_enabled',
        'candidate_seed', 'candidate_count_per_scope',
        'candidate_perturbation_scale', 'component_weight_cap',
        'weight_l1_turnover_cap', 'minimum_factor_breadth',
        'maximum_factor_breadth', 'maximum_specialized_weight',
        'cohort_shrinkage_strength', 'maximum_cohort_deviation_fraction',
        'minimum_train_dates', 'validation_dates', 'holdout_dates',
        'embargo_panel_dates', 'walk_forward_initial_train_dates',
        'walk_forward_test_dates', 'minimum_sector_cross_section',
        'minimum_cohort_cross_section', 'top_quantile',
        'minimum_top_positions', 'maximum_top_turnover',
        'maximum_top_cohort_share', 'transaction_cost_bps',
        'horizon_weights', 'minimum_validation_objective_improvement',
        'minimum_holdout_objective_improvement',
        'minimum_holdout_mean_ic', 'minimum_walk_forward_win_fraction',
    },
    'calibration_scope': {
        'mode', 'enforcement_stage', 'selection_basis',
        'evidence_classification', 'strict_oos_eligible',
        'preserve_source_history',
        'production_promotion_requires_fresh_post_scope_evidence',
        'reviewed_as_of', 'expected_excluded_ticker_count',
        'expected_remaining_current_ticker_count',
        'expected_remaining_current_tickers_sha256',
        'expected_remaining_current_by_cohort',
        'excluded_tickers_by_cohort',
    },
    'stage6b': {
        'adapter_path', 'parser_source_id', 'measurement_source_id',
        'definition_version', 'minimum_parser_confidence',
        'maximum_filings_per_ticker', 'maximum_documents_per_filing',
        'maximum_event_documents_per_filing',
        'event_hydration_workers',
        'enable_pdf_ocr', 'maximum_pdf_pages', 'maximum_pdf_bytes',
        'pdf_extraction_timeout_seconds',
        'maximum_ocr_pages', 'ocr_dpi', 'ocr_page_timeout_seconds',
        'maximum_ocr_pixels_per_page',
        'historical_inventory_start', 'production_weight',
        'production_promotion_enabled',
    },
    "paths": {"database_path", "output_dir", "authoritative_input_manifest"},
    "runtime": {"sqlite_timeout_sec", "model_family", "internal_sector", "portfolio_sector", "timezone"},
    "source_registry": {"path", "stage2_path"},
    "specialized_metrics": {"registry_path", "default_status", "production_default_weight"},
    "sec_fundamentals": {
        "submissions_source_id", "companyfacts_source_id", "inline_xbrl_source_id", "start_date",
        "cache_dir", "submissions_url_template", "submissions_archive_url_template",
        "companyfacts_url_template", "include_submission_archives", "hydrate_documents",
        "documents_per_issuer", "companyfacts_lag_days", "user_agent", "sleep_sec",
        "timeout_sec", "retries",
    },
    "fx_rates": {
        "source_id", "start_date", "supported_currencies", "non_monetary_three_letter_units",
        "cache_dir", "chart_url_template", "user_agent", "sleep_sec", "timeout_sec", "retries",
        "outlier_window", "outlier_minimum_history", "outlier_robust_z_threshold",
        "outlier_relative_deviation_threshold", "redenomination_exemptions",
    },
    "financial_features": {
        "concept_map", "point_in_time_acceptance_required", "flow_fx_method",
        "balance_sheet_fx_method", "maximum_period_age_days",
    },
    "specialized_disclosure_census": {
        "applicability_csv", "terms_path", "expected_applicability_rows", "parser_version",
        "discovery_only", "production_weight",
    },
    "universe": {
        "policy_path", "expected_current_rows", "authoritative_source", "minimum_price",
        "core_minimum_median_dollar_volume_63d", "support_minimum_median_dollar_volume_63d",
        "recognized_membership_required", "recognized_membership_source_id",
        "recognized_membership_effective_start", "recognized_membership_vehicles",
        "current_holdings_validation_etfs", "allowed_security_types", "cohorts",
    },
    "historical_contract": {
        "requested_snapshot_start", "market_history_buffer_calendar_days", "minimum_market_history_start",
        "trading_calendar_ticker", "sector_benchmark_ticker", "broad_benchmark_ticker", "frequency",
        "require_point_in_time_membership", "require_terminal_event_for_delisted_calibration",
        "current_universe_replay_is_survivorship_correct",
    },
    "oos_provenance": {
        "strict_oos_start_date", "calibration_lock_date", "allow_replay_oos_within_days",
        "deep_replay_role", "deep_replay_oos_score_valid_flag",
    },
    "market_data_policy": {
        "policy_path", "adjusted_price_source_priority", "benchmark_tickers",
        "raw_unadjusted_fallback_allowed",
    },
    "dedicated_parser": {"model_family", "parser_schema_owner", "forms", "production_promotion_enabled"},
    "factor_validation": {
        "sector_adapter", "primary_target", "robustness_target", "horizons",
        "sector_minimum_cross_section", "cohort_target_cross_section",
        "cohort_exploratory_minimum_cross_section",
        "subtype_exploratory_minimum_cross_section",
    },
    "portfolio_layer": {
        "adapter", "canonical_sector", "sector_etf", "file_mode", "final_rank_path",
        "require_oos_score_valid", "enabled", "required", "sector_weight_cap", "promotion_state",
    },
    "promotion_framework_v2": {
        "framework_path", "shared_service_contract_path", "status", "legacy_protocol_status",
    },
    "promotion_framework_v3": {
        "framework_path", "engine_module", "status",
        "portfolio_activation_requires_pinned_registry",
    },
    "production_score_publisher_v3": {
        "schema_version", "source_database_path", "output_root",
        "activation_registry_path", "activation_registry_file_sha256",
        "activation_registry_payload_sha256", "candidate_registry_path",
        "candidate_registry_file_sha256", "candidate_registry_payload_sha256",
        "scoring_contract_version", "entry_lag_trading_sessions",
        "selected_candidate_id_by_cohort", "model_contract_sha256_by_cohort",
    },
}


@dataclass(frozen=True)
class ConfigBundle:
    path: Path
    base_dir: Path
    payload: dict[str, Any]


def expand_env_vars(raw: Any) -> str:
    """Expand environment variables, including brace-default syntax."""
    text = str(raw)
    if "$" not in text and "%" not in text:
        return text

    def replace_default(match: re.Match[str]) -> str:
        name = match.group(1)
        default = match.group(2)
        value = os.environ.get(name)
        if value is not None:
            return value
        return default if default is not None else match.group(0)

    return os.path.expandvars(ENV_DEFAULT_RE.sub(replace_default, text))


def load_yaml(path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"Consumer Defensive config YAML not found: {resolved}")
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to load Consumer Defensive configuration.") from exc
    try:
        payload = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML in {resolved}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Config YAML root must be a mapping: {resolved}")
    return payload


def cfg_get(config: dict[str, Any], dotted_key: str, default: Any = None) -> Any:
    current: Any = config
    for part in dotted_key.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def resolve_path(raw: Any, *, base_dir: Path) -> Path:
    if raw is None or str(raw).strip() == "":
        raise ValueError("Consumer Defensive path configuration value is empty.")
    path = Path(expand_env_vars(raw)).expanduser()
    return path.resolve() if path.is_absolute() else (base_dir / path).resolve()


def _validate_nested_keys(config: dict[str, Any]) -> None:
    """Reject misspelled nested settings before any pipeline mutation."""
    for section, allowed in ALLOWED_SECTION_KEYS.items():
        payload = config.get(section)
        if not isinstance(payload, dict):
            raise ValueError(f"Consumer Defensive config section {section!r} must be a mapping.")
        unknown = sorted(set(payload).difference(allowed))
        if unknown:
            raise ValueError(
                f"Unknown Consumer Defensive config keys under {section}: {', '.join(unknown)}"
            )

    vehicles = cfg_get(config, "universe.recognized_membership_vehicles")
    if not isinstance(vehicles, list) or not vehicles:
        raise ValueError("universe.recognized_membership_vehicles must be a non-empty list.")
    vehicle_keys = {"vehicle_id", "display_name", "norgate_index_name", "vehicle_type"}
    for position, vehicle in enumerate(vehicles):
        if not isinstance(vehicle, dict) or set(vehicle) != vehicle_keys:
            raise ValueError(
                "Each recognized-membership vehicle must contain exactly "
                f"{sorted(vehicle_keys)}; row {position} was {vehicle!r}."
            )

    cohorts = cfg_get(config, "universe.cohorts")
    cohort_keys = {"display_name", "target_cross_section", "exploratory_minimum_cross_section", "applicability_subtypes"}
    if isinstance(cohorts, dict):
        for cohort_id, cohort in cohorts.items():
            if not isinstance(cohort, dict) or set(cohort) != cohort_keys:
                raise ValueError(
                    f"universe.cohorts.{cohort_id} must contain exactly {sorted(cohort_keys)}."
                )
    subtype_minimum = int(cfg_get(
        config,
        'factor_validation.subtype_exploratory_minimum_cross_section',
        0,
    ))
    cohort_minimum = int(cfg_get(
        config,
        'factor_validation.cohort_exploratory_minimum_cross_section',
        0,
    ))
    cohort_target = int(cfg_get(
        config, 'factor_validation.cohort_target_cross_section', 0
    ))
    sector_minimum = int(cfg_get(
        config, 'factor_validation.sector_minimum_cross_section', 0
    ))
    if not (
        3 <= subtype_minimum <= cohort_minimum
        <= cohort_target <= sector_minimum
    ):
        raise ValueError(
            'Factor-validation cross-section floors must satisfy '
            '3 <= subtype <= cohort minimum <= cohort target <= sector.'
        )
    exemptions = cfg_get(config, "fx_rates.redenomination_exemptions")
    if not isinstance(exemptions, list):
        raise ValueError("fx_rates.redenomination_exemptions must be a list.")
    exemption_keys = {"currency", "start_date", "end_date", "reason"}
    for position, exemption in enumerate(exemptions):
        if not isinstance(exemption, dict) or set(exemption) != exemption_keys:
            raise ValueError(
                f"FX redenomination exemption {position} must contain exactly {sorted(exemption_keys)}."
            )


def _validate_positioning_contract(config: dict[str, Any]) -> None:
    expected_maps = {
        'source_birthdates': {'sec_form4', 'institutional_13f', 'short_interest', 'borrow'},
        'maximum_age_days': {'sec_form4', 'institutional_13f', 'short_interest', 'borrow'},
        'minimum_current_coverage': {'institutional_13f', 'short_interest', 'borrow'},
        'lookback_days': {'insider'},
        'upstream_source_names': {'institutional_13f', 'short_interest', 'borrow'},
    }
    for key, expected in expected_maps.items():
        value = cfg_get(config, f'positioning.{key}')
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError(f'positioning.{key} must contain exactly {sorted(expected)}.')
    try:
        start = date.fromisoformat(str(cfg_get(config, 'positioning.start_date')))
        births = [date.fromisoformat(str(value)) for value in cfg_get(config, 'positioning.source_birthdates').values()]
    except (TypeError, ValueError) as exc:
        raise ValueError('Stage 5 positioning start/source birthdates must be ISO dates.') from exc
    contract_start = date(2019, 1, 2)
    if start < contract_start or any(value < contract_start for value in births):
        raise ValueError('Stage 5 positioning dates cannot predate 2019-01-02.')
    for key, raw in cfg_get(config, 'positioning.minimum_current_coverage').items():
        value = float(raw)
        if not 0.0 <= value <= 1.0:
            raise ValueError(f'positioning.minimum_current_coverage.{key} must be in [0,1].')
    for key, raw in cfg_get(config, 'positioning.upstream_source_names').items():
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f'positioning.upstream_source_names.{key} must be nonblank.')
    identifier_map = cfg_get(config, 'positioning.source_identifier_map')
    if not isinstance(identifier_map, str) or not identifier_map.strip():
        raise ValueError('positioning.source_identifier_map must be a nonblank path.')
    cache_root = cfg_get(config, 'positioning.market_positioning_cache_root')
    if not isinstance(cache_root, str) or not cache_root.strip():
        raise ValueError('positioning.market_positioning_cache_root must be a nonblank path.')


def _validate_scoring_feature_contract(config: dict[str, Any]) -> None:
    source_id = cfg_get(config, 'scoring_features.source_id')
    version = cfg_get(config, 'scoring_features.definition_version')
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValueError('scoring_features.source_id must be nonblank.')
    if version != 'consumer_defensive_scoring_features_v3':
        raise ValueError(
            'scoring_features.definition_version must be '
            "'consumer_defensive_scoring_features_v3'."
        )
    if int(cfg_get(config, 'scoring_features.minimum_normalization_peer_count', 0)) < 2:
        raise ValueError('scoring_features.minimum_normalization_peer_count must be at least 2.')
    if cfg_get(config, 'scoring_features.normalize_within_cohort') is not True:
        raise ValueError('scoring_features.normalize_within_cohort must be true.')
    rank_fraction = float(cfg_get(config, 'scoring_features.minimum_rank_ready_fraction', -1))
    if not 0.0 <= rank_fraction <= 1.0:
        raise ValueError('scoring_features.minimum_rank_ready_fraction must be in [0,1].')
    expected_statuses = {
        'accepted_market_quality_statuses': ['full'],
        'accepted_financial_quality_statuses': ['complete', 'partial'],
        'accepted_positioning_quality_statuses': ['complete'],
    }
    for key, expected in expected_statuses.items():
        if cfg_get(config, f'scoring_features.{key}') != expected:
            raise ValueError(f'scoring_features.{key} must be exactly {expected!r}.')
    if cfg_get(config, 'scoring_features.specialized_default_availability') != 'not_loaded':
        raise ValueError('Specialized scoring components must default to not_loaded.')
    if float(cfg_get(config, 'scoring_features.component_weight_default', 1.0)) != 0.0:
        raise ValueError('Stage 6A scoring component weights must remain zero.')


def _validate_stage7_contract(config: dict[str, Any]) -> None:
    expected = {
        'source_id': 'consumer_defensive_stage7_baseline_v4',
        'baseline_source_id': 'consumer_defensive_scoring_contract',
        'model_version': 'consumer_defensive_stage7_baseline_v4',
        'promotion_state': 'shadow_monitor',
        'portfolio_candidate_gate': 0,
        'oos_score_valid_flag': 0,
        'require_stage6b_measurement_overlay': True,
        'specialized_weight_default': 0.0,
        'specialized_weight_policy': (
            'shared_factor_validation_acceptance_required'
        ),
        'factor_validation_verdict': (
            'corrected_campaign_zero_accepted_8_metrics_testable_1_fdr_wrong_direction'
        ),
    }
    for key, required in expected.items():
        actual = cfg_get(config, f'stage7_scoring.{key}')
        if actual != required:
            raise ValueError(
                f'stage7_scoring.{key} must be {required!r}; got {actual!r}'
            )
    campaign_id = str(
        cfg_get(config, 'stage7_scoring.factor_validation_campaign_id', '')
        or ''
    )
    if not campaign_id.strip():
        raise ValueError(
            'stage7_scoring.factor_validation_campaign_id must be nonblank.'
        )
    neutral = float(cfg_get(config, 'stage7_scoring.neutral_score', -1.0))
    if not 0.0 <= neutral <= 100.0:
        raise ValueError('stage7_scoring.neutral_score must be in [0,100].')
    minimum_quality = float(
        cfg_get(config, 'stage7_scoring.minimum_data_quality_confidence', -1.0)
    )
    maximum_missing = float(
        cfg_get(config, 'stage7_scoring.maximum_missing_component_weight', -1.0)
    )
    minimum_ready = float(
        cfg_get(config, 'stage7_scoring.minimum_rank_ready_fraction', -1.0)
    )
    for label, value in {
        'minimum_data_quality_confidence': minimum_quality,
        'maximum_missing_component_weight': maximum_missing,
        'minimum_rank_ready_fraction': minimum_ready,
    }.items():
        if not 0.0 <= value <= 1.0:
            raise ValueError(f'stage7_scoring.{label} must be in [0,1].')
    weights = cfg_get(config, 'stage7_scoring.component_weights')
    if not isinstance(weights, dict) or not weights:
        raise ValueError('stage7_scoring.component_weights must be non-empty.')
    values: list[float] = []
    for name, raw in weights.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError(
                'stage7_scoring.component_weights has a blank component name.'
            )
        value = float(raw)
        if value < 0.0 or value > 1.0:
            raise ValueError(
                f'stage7_scoring.component_weights.{name} must be in [0,1].'
            )
        values.append(value)
    if abs(sum(values) - 1.0) > 1e-12:
        raise ValueError(
            'stage7_scoring.component_weights must sum exactly to 1.0; '
            f'found {sum(values):.12f}.'
        )


def _validate_stage8_contract(config: dict[str, Any]) -> None:
    expected = {
        'mode': 'report_only',
        'production_promotion_enabled': False,
        'portfolio_write_enabled': False,
    }
    for key, required in expected.items():
        actual = cfg_get(config, f'stage8_calibration.{key}')
        if actual != required:
            raise ValueError(
                f'stage8_calibration.{key} must be {required!r}; '
                f'got {actual!r}'
            )
    positive_integers = (
        'candidate_count_per_scope',
        'minimum_factor_breadth',
        'maximum_factor_breadth',
        'minimum_train_dates',
        'validation_dates',
        'holdout_dates',
        'embargo_panel_dates',
        'walk_forward_initial_train_dates',
        'walk_forward_test_dates',
        'minimum_sector_cross_section',
        'minimum_cohort_cross_section',
        'minimum_top_positions',
    )
    for key in positive_integers:
        if int(cfg_get(config, f'stage8_calibration.{key}', 0)) < 1:
            raise ValueError(
                f'stage8_calibration.{key} must be a positive integer.'
            )
    if int(cfg_get(
        config, 'stage8_calibration.minimum_factor_breadth'
    )) > int(cfg_get(config, 'stage8_calibration.maximum_factor_breadth')):
        raise ValueError(
            'stage8_calibration minimum_factor_breadth cannot exceed '
            'maximum_factor_breadth.'
        )
    bounded = (
        'component_weight_cap',
        'weight_l1_turnover_cap',
        'maximum_specialized_weight',
        'maximum_cohort_deviation_fraction',
        'top_quantile',
        'maximum_top_turnover',
        'maximum_top_cohort_share',
        'minimum_walk_forward_win_fraction',
    )
    for key in bounded:
        value = float(cfg_get(config, f'stage8_calibration.{key}', -1.0))
        if not 0.0 <= value <= 1.0:
            raise ValueError(f'stage8_calibration.{key} must be in [0,1].')
    if float(cfg_get(
        config, 'stage8_calibration.candidate_perturbation_scale', 0.0
    )) <= 0.0:
        raise ValueError(
            'stage8_calibration.candidate_perturbation_scale must be positive.'
        )
    if float(cfg_get(
        config, 'stage8_calibration.cohort_shrinkage_strength', 0.0
    )) <= 0.0:
        raise ValueError(
            'stage8_calibration.cohort_shrinkage_strength must be positive.'
        )
    if float(cfg_get(
        config, 'stage8_calibration.transaction_cost_bps', -1.0
    )) < 0.0:
        raise ValueError(
            'stage8_calibration.transaction_cost_bps cannot be negative.'
        )
    horizons = cfg_get(config, 'stage8_calibration.horizon_weights')
    if not isinstance(horizons, dict) or set(horizons) != {'21', '63', '126'}:
        raise ValueError(
            'stage8_calibration.horizon_weights must contain exactly '
            '21, 63, and 126.'
        )
    horizon_total = sum(float(value) for value in horizons.values())
    if abs(horizon_total - 1.0) > 1e-12:
        raise ValueError(
            'stage8_calibration.horizon_weights must sum exactly to 1.0.'
        )
    holdout_ic = cfg_get(config, 'stage8_calibration.minimum_holdout_mean_ic')
    if not isinstance(holdout_ic, dict) or set(holdout_ic) != {'63', '126'}:
        raise ValueError(
            'stage8_calibration.minimum_holdout_mean_ic must contain '
            'exactly 63 and 126.'
        )
    minimum_embargo = math.ceil(126 / 21) + 1
    if int(cfg_get(
        config, 'stage8_calibration.embargo_panel_dates'
    )) < minimum_embargo:
        raise ValueError(
            'stage8_calibration.embargo_panel_dates is shorter than the '
            '126-session label-isolation minimum.'
        )
    minimum_top = int(cfg_get(
        config, 'stage8_calibration.minimum_top_positions'
    ))
    if minimum_top < 3:
        raise ValueError(
            'stage8_calibration.minimum_top_positions must be at least 3.'
        )
    for scope in ('sector', 'cohort'):
        cross_section = int(cfg_get(
            config, f'stage8_calibration.minimum_{scope}_cross_section'
        ))
        if cross_section < 2 * minimum_top:
            raise ValueError(
                f'stage8_calibration.minimum_{scope}_cross_section must '
                'support disjoint top and bottom portfolios.'
            )
    walk_initial = int(cfg_get(
        config, 'stage8_calibration.walk_forward_initial_train_dates'
    ))
    minimum_train = int(cfg_get(
        config, 'stage8_calibration.minimum_train_dates'
    ))
    if walk_initial >= minimum_train:
        raise ValueError(
            'stage8_calibration.walk_forward_initial_train_dates must be '
            'shorter than minimum_train_dates.'
        )


def _validate_calibration_scope_contract(config: dict[str, Any]) -> None:
    expected = {
        'mode': 'explicit_ticker_exclusions',
        'enforcement_stage': 'before_cross_section_normalization',
        'selection_basis': (
            'user_directed_after_review_of_realized_ticker_performance'
        ),
        'evidence_classification': 'performance_informed_model_selection',
        'strict_oos_eligible': False,
        'preserve_source_history': True,
        'production_promotion_requires_fresh_post_scope_evidence': True,
    }
    for key, required in expected.items():
        actual = cfg_get(config, f'calibration_scope.{key}')
        if actual != required:
            raise ValueError(
                f'calibration_scope.{key} must be {required!r}; '
                f'got {actual!r}'
            )

    reviewed_as_of = cfg_get(config, 'calibration_scope.reviewed_as_of')
    if not isinstance(reviewed_as_of, str):
        raise ValueError(
            'calibration_scope.reviewed_as_of must be a canonical ISO date.'
        )
    try:
        parsed_review_date = date.fromisoformat(reviewed_as_of)
    except ValueError as exc:
        raise ValueError(
            'calibration_scope.reviewed_as_of must be a canonical ISO date.'
        ) from exc
    if parsed_review_date.isoformat() != reviewed_as_of:
        raise ValueError(
            'calibration_scope.reviewed_as_of must be a canonical ISO date.'
        )

    cohort_ids = set(cfg_get(config, 'universe.cohorts'))
    excluded = cfg_get(config, 'calibration_scope.excluded_tickers_by_cohort')
    remaining = cfg_get(
        config, 'calibration_scope.expected_remaining_current_by_cohort'
    )
    if not isinstance(excluded, dict) or set(excluded) != cohort_ids:
        raise ValueError(
            'calibration_scope.excluded_tickers_by_cohort must contain '
            'exactly the configured cohort ids.'
        )
    if not isinstance(remaining, dict) or set(remaining) != cohort_ids:
        raise ValueError(
            'calibration_scope.expected_remaining_current_by_cohort must '
            'contain exactly the configured cohort ids.'
        )

    observed: set[str] = set()
    excluded_count = 0
    ticker_pattern = re.compile(r'^[A-Z0-9][A-Z0-9.\-]*$')
    for cohort_id in sorted(cohort_ids):
        tickers = excluded[cohort_id]
        if not isinstance(tickers, list):
            raise ValueError(
                'calibration_scope.excluded_tickers_by_cohort.'
                f'{cohort_id} must be a list.'
            )
        if tickers != sorted(tickers):
            raise ValueError(
                'Calibration-scope ticker exclusions must be sorted.'
            )
        if len(tickers) != len(set(tickers)):
            raise ValueError(
                'Calibration-scope exclusions contain duplicates in '
                f'{cohort_id}.'
            )
        for ticker in tickers:
            if (
                not isinstance(ticker, str)
                or not ticker_pattern.fullmatch(ticker)
            ):
                raise ValueError(
                    f'Invalid calibration-scope ticker {ticker!r}.'
                )
            if ticker in observed:
                raise ValueError(
                    f'Calibration-scope ticker {ticker} appears in more '
                    'than one cohort.'
                )
            observed.add(ticker)
        excluded_count += len(tickers)
        if int(remaining[cohort_id]) < 1:
            raise ValueError(
                'Each cohort must retain at least one current ticker after '
                'calibration exclusions.'
            )

    expected_excluded = int(cfg_get(
        config, 'calibration_scope.expected_excluded_ticker_count'
    ))
    if excluded_count != expected_excluded:
        raise ValueError(
            'calibration_scope.expected_excluded_ticker_count does not tie '
            'to excluded_tickers_by_cohort.'
        )
    expected_remaining = int(cfg_get(
        config, 'calibration_scope.expected_remaining_current_ticker_count'
    ))
    if sum(int(value) for value in remaining.values()) != expected_remaining:
        raise ValueError(
            'calibration_scope.expected_remaining_current_ticker_count does '
            'not tie to expected_remaining_current_by_cohort.'
        )
    remaining_sha = cfg_get(
        config,
        'calibration_scope.expected_remaining_current_tickers_sha256',
    )
    if (
        not isinstance(remaining_sha, str)
        or not re.fullmatch(r'[0-9a-f]{64}', remaining_sha)
    ):
        raise ValueError(
            'calibration_scope.expected_remaining_current_tickers_sha256 '
            'must be a lowercase SHA-256 digest.'
        )


def _validate_stage6b_contract(config: dict[str, Any]) -> None:
    expected = {
        'adapter_path': (
            'consumer_defensive.adapters.dedicated_parser_adapter:'
            'extract_metric_evidence'
        ),
        'parser_source_id': 'shared_dedicated_sec_parser',
        'measurement_source_id': (
            'consumer_defensive_stage6b_specialized_measurement'
        ),
        'definition_version': 'consumer_defensive_specialized_measurements_v1',
        'historical_inventory_start': '2019-01-02',
        'production_weight': 0.0,
        'production_promotion_enabled': False,
    }
    for key, required in expected.items():
        actual = cfg_get(config, f'stage6b.{key}')
        if actual != required:
            raise ValueError(
                f'stage6b.{key} must be {required!r}; got {actual!r}'
            )
    confidence = float(
        cfg_get(config, 'stage6b.minimum_parser_confidence', -1.0)
    )
    if not 0.0 <= confidence <= 1.0:
        raise ValueError('stage6b.minimum_parser_confidence must be in [0,1].')
    if int(cfg_get(config, 'stage6b.maximum_filings_per_ticker', 0)) < 1:
        raise ValueError('stage6b.maximum_filings_per_ticker must be positive.')
    if int(cfg_get(config, 'stage6b.maximum_documents_per_filing', 0)) < 1:
        raise ValueError(
            'stage6b.maximum_documents_per_filing must be positive.'
        )
    event_limit = int(
        cfg_get(config, 'stage6b.maximum_event_documents_per_filing', 0)
    )
    if not 1 <= event_limit <= 8:
        raise ValueError(
            'stage6b.maximum_event_documents_per_filing must be in [1,8].'
        )
    event_workers = int(cfg_get(config, 'stage6b.event_hydration_workers', 0))
    if not 1 <= event_workers <= 16:
        raise ValueError('stage6b.event_hydration_workers must be in [1,16].')
    if int(cfg_get(config, 'stage6b.maximum_pdf_pages', 0)) < 1:
        raise ValueError('stage6b.maximum_pdf_pages must be positive.')
    if int(cfg_get(config, 'stage6b.maximum_pdf_bytes', 0)) < 1:
        raise ValueError('stage6b.maximum_pdf_bytes must be positive.')
    if float(cfg_get(config, 'stage6b.pdf_extraction_timeout_seconds', 0)) <= 0:
        raise ValueError(
            'stage6b.pdf_extraction_timeout_seconds must be positive.'
        )
    ocr_pages = int(cfg_get(config, 'stage6b.maximum_ocr_pages', 0))
    if not 1 <= ocr_pages <= int(
        cfg_get(config, 'stage6b.maximum_pdf_pages', 0)
    ):
        raise ValueError(
            'stage6b.maximum_ocr_pages must be within the PDF page limit.'
        )
    ocr_dpi = int(cfg_get(config, 'stage6b.ocr_dpi', 0))
    if not 72 <= ocr_dpi <= 600:
        raise ValueError('stage6b.ocr_dpi must be in [72,600].')
    ocr_timeout = float(
        cfg_get(config, 'stage6b.ocr_page_timeout_seconds', 0)
    )
    if not 0 < ocr_timeout <= float(
        cfg_get(config, 'stage6b.pdf_extraction_timeout_seconds', 0)
    ):
        raise ValueError(
            'stage6b.ocr_page_timeout_seconds must be positive and within '
            'the total PDF timeout.'
        )
    if int(cfg_get(config, 'stage6b.maximum_ocr_pixels_per_page', 0)) < 1:
        raise ValueError(
            'stage6b.maximum_ocr_pixels_per_page must be positive.'
        )


def _validate_production_score_publisher_contract(config: dict[str, Any]) -> None:
    section = cfg_get(config, "production_score_publisher_v3")
    if not isinstance(section, dict):
        raise ValueError("production_score_publisher_v3 must be a mapping.")
    if section.get("schema_version") != "consumer_defensive_production_score_publisher_v3":
        raise ValueError(
            "production_score_publisher_v3.schema_version must identify the v3 publisher."
        )
    for key in (
        "source_database_path",
        "output_root",
        "activation_registry_path",
        "candidate_registry_path",
    ):
        if not isinstance(section.get(key), str) or not str(section[key]).strip():
            raise ValueError(f"production_score_publisher_v3.{key} must be a nonblank path.")
    if section["source_database_path"] != cfg_get(config, "paths.database_path"):
        raise ValueError(
            "production_score_publisher_v3.source_database_path must use the canonical "
            "Consumer database."
        )
    for key in (
        "activation_registry_file_sha256",
        "activation_registry_payload_sha256",
        "candidate_registry_file_sha256",
        "candidate_registry_payload_sha256",
        "scoring_contract_version",
    ):
        value = section.get(key)
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError(
                f"production_score_publisher_v3.{key} must be a lowercase SHA-256 digest."
            )
    if section.get("entry_lag_trading_sessions") != 1:
        raise ValueError(
            "production_score_publisher_v3.entry_lag_trading_sessions must equal one."
        )
    for key in (
        "selected_candidate_id_by_cohort",
        "model_contract_sha256_by_cohort",
    ):
        values = section.get(key)
        if not isinstance(values, dict) or set(values) != CONSUMER_COHORT_IDS:
            raise ValueError(
                f"production_score_publisher_v3.{key} must contain exactly the four "
                "Consumer cohorts."
            )
    candidate_ids = section["selected_candidate_id_by_cohort"]
    if any(not isinstance(value, str) or not value.strip() for value in candidate_ids.values()):
        raise ValueError(
            "production_score_publisher_v3 selected candidate IDs must be nonblank."
        )
    model_hashes = section["model_contract_sha256_by_cohort"]
    if any(
        not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
        for value in model_hashes.values()
    ):
        raise ValueError(
            "production_score_publisher_v3 model-contract pins must be lowercase SHA-256 digests."
        )


def _assert_contract_equal(label: str, left: Any, right: Any) -> None:
    if left != right:
        raise ValueError(f"Consumer Defensive contract drift for {label}: config={left!r}, policy={right!r}")


def validate_contract_bundle(config: dict[str, Any], *, base_dir: Path) -> None:
    """Fail closed when duplicated universe/market invariants disagree."""
    universe_policy = load_yaml(resolve_path(cfg_get(config, "universe.policy_path"), base_dir=base_dir))
    market_policy = load_yaml(resolve_path(cfg_get(config, "market_data_policy.policy_path"), base_dir=base_dir))

    comparisons = {
        "universe.model_family": (cfg_get(config, "runtime.model_family"), universe_policy.get("model_family")),
        "universe.internal_sector": (cfg_get(config, "runtime.internal_sector"), universe_policy.get("internal_sector")),
        "universe.portfolio_sector": (cfg_get(config, "runtime.portfolio_sector"), universe_policy.get("portfolio_sector")),
        "universe.expected_current_rows": (cfg_get(config, "universe.expected_current_rows"), universe_policy.get("expected_current_rows")),
        "universe.history_start": (cfg_get(config, "historical_contract.minimum_market_history_start"), universe_policy.get("history_start")),
        "universe.requested_snapshot_start": (cfg_get(config, "historical_contract.requested_snapshot_start"), universe_policy.get("requested_snapshot_start")),
        "universe.recognized_membership_required": (cfg_get(config, "universe.recognized_membership_required"), universe_policy.get("recognized_membership_required")),
        "universe.recognized_membership_source_id": (cfg_get(config, "universe.recognized_membership_source_id"), universe_policy.get("recognized_membership_source_id")),
        "universe.current_holdings_validation_etfs": (cfg_get(config, "universe.current_holdings_validation_etfs"), universe_policy.get("current_holdings_validation_only")),
        "universe.allowed_security_types": (cfg_get(config, "universe.allowed_security_types"), universe_policy.get("allowed_security_types")),
        "universe.minimum_price": (cfg_get(config, "universe.minimum_price"), universe_policy.get("minimum_price")),
        "universe.core_adv": (cfg_get(config, "universe.core_minimum_median_dollar_volume_63d"), universe_policy.get("core_minimum_median_dollar_volume_63d")),
        "universe.support_adv": (cfg_get(config, "universe.support_minimum_median_dollar_volume_63d"), universe_policy.get("support_minimum_median_dollar_volume_63d")),
        "market.model_family": (cfg_get(config, "runtime.model_family"), market_policy.get("model_family")),
        "market.history_start": (cfg_get(config, "historical_contract.minimum_market_history_start"), market_policy.get("history_start")),
        "market.history_buffer_calendar_days": (cfg_get(config, "historical_contract.market_history_buffer_calendar_days"), market_policy.get("history_buffer_calendar_days")),
        "market.requested_snapshot_start": (cfg_get(config, "historical_contract.requested_snapshot_start"), market_policy.get("requested_snapshot_start")),
        "market.benchmarks": (cfg_get(config, "market_data_policy.benchmark_tickers"), [market_policy.get("benchmarks", {}).get("sector"), market_policy.get("benchmarks", {}).get("broad")]),
        "market.source_priority": (cfg_get(config, "market_data_policy.adjusted_price_source_priority"), [market_policy.get("sources", {}).get("active_primary"), market_policy.get("sources", {}).get("historical_delisted_primary")]),
    }
    for label, (left, right) in comparisons.items():
        _assert_contract_equal(label, left, right)

    configured_vehicles = [
        {key: row[key] for key in ("vehicle_id", "norgate_index_name", "vehicle_type")}
        for row in cfg_get(config, "universe.recognized_membership_vehicles")
    ]
    policy_vehicles = [
        {key: row[key] for key in ("vehicle_id", "norgate_index_name", "vehicle_type")}
        for row in universe_policy.get("approved_membership_vehicles", [])
    ]
    _assert_contract_equal("universe.recognized_membership_vehicles", configured_vehicles, policy_vehicles)
    _assert_contract_equal(
        "universe.cohort_ids",
        set(cfg_get(config, "universe.cohorts")),
        {str(row.get("cohort_id")) for row in universe_policy.get("cohorts", {}).values()},
    )


def validate_config(config: dict[str, Any]) -> None:
    unknown = sorted(set(config).difference(ALLOWED_ROOT_KEYS))
    if unknown:
        raise ValueError(f"Unknown Consumer Defensive config root keys: {', '.join(unknown)}")
    missing = sorted(REQUIRED_ROOT_KEYS.difference(config))
    if missing:
        raise ValueError(f"Missing Consumer Defensive config root keys: {', '.join(missing)}")
    _validate_nested_keys(config)
    _validate_positioning_contract(config)
    _validate_scoring_feature_contract(config)
    _validate_stage7_contract(config)
    _validate_stage8_contract(config)
    _validate_calibration_scope_contract(config)
    _validate_stage6b_contract(config)
    _validate_production_score_publisher_contract(config)

    expected_values = {
        "runtime.model_family": "consumer_defensive",
        "runtime.internal_sector": "Consumer Defensive",
        "runtime.portfolio_sector": "Consumer Staples",
        "historical_contract.requested_snapshot_start": "2019-01-02",
        "historical_contract.minimum_market_history_start": "2017-11-28",
        "historical_contract.trading_calendar_ticker": "SPY",
        "historical_contract.sector_benchmark_ticker": "XLP",
        "dedicated_parser.model_family": "consumer_defensive",
        "portfolio_layer.adapter": "consumer_defensive",
        "portfolio_layer.canonical_sector": "Consumer Staples",
        "promotion_framework_v2.framework_path": "data/consumer_defensive_promotion_framework_v2.yaml",
        "promotion_framework_v2.shared_service_contract_path": "data/consumer_defensive_shared_service_contract_v1.yaml",
        "promotion_framework_v2.status": "recalibration_required",
        "promotion_framework_v2.legacy_protocol_status": "retired_archived",
        "promotion_framework_v3.framework_path": "data/consumer_defensive_promotion_framework_v3.yaml",
        "promotion_framework_v3.engine_module": "consumer_defensive.core.promotion_engine_v3",
        "promotion_framework_v3.status": "active_standard_allocation_pinned_registry",
        "promotion_framework_v3.portfolio_activation_requires_pinned_registry": True,
        "portfolio_layer.enabled": True,
        "portfolio_layer.required": True,
        "portfolio_layer.sector_weight_cap": 0.125,
        "portfolio_layer.promotion_state": "active",
        "production_score_publisher_v3.output_root": "../output",
    }
    for dotted_key, expected in expected_values.items():
        actual = cfg_get(config, dotted_key)
        if actual != expected:
            raise ValueError(f"{dotted_key} must be {expected!r}; got {actual!r}")

    if bool(cfg_get(config, "historical_contract.current_universe_replay_is_survivorship_correct", True)):
        raise ValueError("Current-universe replay cannot be declared survivorship-correct.")
    if not bool(cfg_get(config, "historical_contract.require_point_in_time_membership", False)):
        raise ValueError("Historical snapshots must require point-in-time membership.")
    if not bool(cfg_get(config, "historical_contract.require_terminal_event_for_delisted_calibration", False)):
        raise ValueError("Delisted calibration must require a reconciled terminal event.")
    if bool(cfg_get(config, "market_data_policy.raw_unadjusted_fallback_allowed", True)):
        raise ValueError("Raw unadjusted prices cannot be used as a return-series fallback.")
    if not bool(cfg_get(config, "universe.recognized_membership_required", False)):
        raise ValueError("Recognized index membership is mandatory for the calibration universe.")
    minimum_price = float(cfg_get(config, "universe.minimum_price", 0.0))
    support_adv = float(cfg_get(config, "universe.support_minimum_median_dollar_volume_63d", 0.0))
    core_adv = float(cfg_get(config, "universe.core_minimum_median_dollar_volume_63d", 0.0))
    if minimum_price <= 0 or support_adv <= 0 or core_adv < support_adv:
        raise ValueError("Universe price/liquidity review thresholds are invalid.")
    strict_oos = str(cfg_get(config, "oos_provenance.strict_oos_start_date", "") or "")
    calibration_lock = str(cfg_get(config, "oos_provenance.calibration_lock_date", "") or "")
    if bool(strict_oos) != bool(calibration_lock):
        raise ValueError("strict_oos_start_date and calibration_lock_date must be set together.")
    if int(cfg_get(config, "oos_provenance.deep_replay_oos_score_valid_flag", 1)) != 0:
        raise ValueError("Deep reconstructed history must default oos_score_valid_flag to 0.")
    promotion_state = str(cfg_get(config, "portfolio_layer.promotion_state", ""))
    portfolio_enabled = bool(cfg_get(config, "portfolio_layer.enabled", False))
    portfolio_required = bool(cfg_get(config, "portfolio_layer.required", False))
    sector_cap = float(cfg_get(config, "portfolio_layer.sector_weight_cap", 0.0))
    if promotion_state not in {"deferred", "shadow", "active"}:
        raise ValueError("portfolio_layer.promotion_state must be deferred, shadow, or active.")
    if promotion_state != "active" and (portfolio_enabled or portfolio_required or sector_cap != 0.0):
        raise ValueError("A deferred/shadow Portfolio Layer must be disabled, optional, and have a zero cap.")
    if promotion_state == "active" and (not portfolio_enabled or not portfolio_required or not 0.0 < sector_cap <= 1.0):
        raise ValueError("An active Portfolio Layer must be enabled, required, and have a cap in (0, 1].")

    cohorts = cfg_get(config, "universe.cohorts", {})
    if not isinstance(cohorts, dict) or set(cohorts) != CONSUMER_COHORT_IDS:
        raise ValueError("Consumer Defensive config must define exactly the four reviewed cohort IDs.")


def load_config(path: Path) -> ConfigBundle:
    resolved = path.expanduser().resolve()
    payload = load_yaml(resolved)
    validate_config(payload)
    validate_contract_bundle(payload, base_dir=resolved.parent)
    from consumer_defensive.core.input_manifest import validate_authoritative_input_manifest

    validate_authoritative_input_manifest(
        resolve_path(payload["paths"]["authoritative_input_manifest"], base_dir=resolved.parent),
        repository_root=resolved.parent.parent,
    )
    return ConfigBundle(path=resolved, base_dir=resolved.parent, payload=payload)
