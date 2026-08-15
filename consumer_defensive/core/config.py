from __future__ import annotations

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
ALLOWED_ROOT_KEYS = frozenset(
    {
        'positioning',
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
    }
)
REQUIRED_ROOT_KEYS = ALLOWED_ROOT_KEYS
ALLOWED_SECTION_KEYS = {
    'positioning': {
        'ownership_source_id', 'market_positioning_source_id', 'feature_source_id',
        'form4_upstream_db', 'market_positioning_upstream_db', 'upstream_universe_csv',
        'source_identifier_map',
        'start_date', 'source_birthdates', 'maximum_age_days',
        'minimum_current_coverage', 'lookback_days', 'upstream_source_names',
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
    },
    "portfolio_layer": {
        "adapter", "canonical_sector", "sector_etf", "file_mode", "final_rank_path",
        "require_oos_score_valid", "enabled", "required", "sector_weight_cap", "promotion_state",
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
    expected_cohorts = {
        "beverages",
        "consumer_staples_distribution_retail",
        "household_personal_tobacco",
        "packaged_foods_agricultural_products",
    }
    if not isinstance(cohorts, dict) or set(cohorts) != expected_cohorts:
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
