"""Strict configuration loading for the Basic Materials package."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Any, Mapping

import yaml

from basic_materials import MODEL_FAMILY, SECTOR


class ConfigError(ValueError):
    """Raised when configuration violates the package contract."""


_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


@dataclass(frozen=True)
class ModelConfig:
    family: str
    sector: str
    schema_owner: str
    implemented_stage: int
    promotion_state: str
    portfolio_candidate_gate: bool
    oos_score_valid_flag: bool


@dataclass(frozen=True)
class PathConfig:
    database: Path
    output_root: Path
    cache_root: Path
    authoritative_input_manifest: Path
    source_registry: Path
    universe_policy: Path
    universe_csv: Path
    historical_candidate_policy: Path
    historical_candidate_manifest: Path
    historical_candidates_csv: Path
    historical_reconciliation_policy: Path
    historical_reconciliation_manifest: Path
    historical_membership_csv: Path
    ticker_aliases_csv: Path
    security_events_csv: Path
    terminal_events_csv: Path
    market_data_policy: Path
    market_data_manifest: Path
    market_instruments_csv: Path
    terminal_return_rules_csv: Path


@dataclass(frozen=True)
class RuntimeConfig:
    sqlite_timeout_seconds: float
    fail_closed: bool
    allow_cross_sector_imports: bool
    require_manifest_match_before_mutation: bool
    require_database_identity: bool


@dataclass(frozen=True)
class HistoricalContract:
    point_in_time_membership_required_for_calibration: bool
    current_universe_is_survivorship_corrected: bool
    current_universe_calibration_eligible: bool


@dataclass(frozen=True)
class BasicMaterialsConfig:
    config_version: int
    config_path: Path
    package_root: Path
    repository_root: Path
    model: ModelConfig
    paths: PathConfig
    runtime: RuntimeConfig
    historical_contract: HistoricalContract


def _strict_keys(mapping: Mapping[str, Any], expected: set[str], context: str) -> None:
    actual = set(mapping)
    missing = expected - actual
    extra = actual - expected
    if missing or extra:
        parts: list[str] = []
        if missing:
            parts.append(f"missing={sorted(missing)}")
        if extra:
            parts.append(f"unexpected={sorted(extra)}")
        raise ConfigError(f"Invalid keys for {context}: {'; '.join(parts)}")


def _require_mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{context} must be a mapping")
    return value


def _require_bool(value: Any, context: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{context} must be true or false")
    return value


def _expand_environment(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        resolved = os.environ.get(name, default)
        if resolved is None:
            raise ConfigError(f"Environment variable {name} is required")
        return resolved

    return _ENV_PATTERN.sub(replace, value)


def _resolve_path(value: Any, base: Path, context: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{context} must be a non-empty path string")
    expanded = Path(_expand_environment(value.strip())).expanduser()
    if not expanded.is_absolute():
        expanded = base / expanded
    return expanded.resolve(strict=False)


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def load_config(path: str | Path | None = None) -> BasicMaterialsConfig:
    """Load and validate the package configuration with no silent defaults."""

    package_root = Path(__file__).resolve().parents[1]
    config_path = Path(path).resolve() if path else package_root / "config.yaml"
    if not config_path.is_file():
        raise ConfigError(f"Configuration file not found: {config_path}")

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    root = _require_mapping(raw, "configuration")
    _strict_keys(root, {"config_version", "model", "paths", "runtime", "historical_contract"}, "configuration")

    if root["config_version"] != 1:
        raise ConfigError("config_version must be 1")

    model_raw = _require_mapping(root["model"], "model")
    _strict_keys(
        model_raw,
        {
            "family",
            "sector",
            "schema_owner",
            "implemented_stage",
            "promotion_state",
            "portfolio_candidate_gate",
            "oos_score_valid_flag",
        },
        "model",
    )
    model = ModelConfig(
        family=str(model_raw["family"]),
        sector=str(model_raw["sector"]),
        schema_owner=str(model_raw["schema_owner"]),
        implemented_stage=int(model_raw["implemented_stage"]),
        promotion_state=str(model_raw["promotion_state"]),
        portfolio_candidate_gate=_require_bool(model_raw["portfolio_candidate_gate"], "model.portfolio_candidate_gate"),
        oos_score_valid_flag=_require_bool(model_raw["oos_score_valid_flag"], "model.oos_score_valid_flag"),
    )

    paths_raw = _require_mapping(root["paths"], "paths")
    _strict_keys(
        paths_raw,
        {
            "database",
            "output_root",
            "cache_root",
            "authoritative_input_manifest",
            "source_registry",
            "universe_policy",
            "universe_csv",
            "historical_candidate_policy",
            "historical_candidate_manifest",
            "historical_candidates_csv",
            "historical_reconciliation_policy",
            "historical_reconciliation_manifest",
            "historical_membership_csv",
            "ticker_aliases_csv",
            "security_events_csv",
            "terminal_events_csv",
            "market_data_policy",
            "market_data_manifest",
            "market_instruments_csv",
            "terminal_return_rules_csv",
        },
        "paths",
    )
    base = config_path.parent
    paths = PathConfig(
        database=_resolve_path(paths_raw["database"], base, "paths.database"),
        output_root=_resolve_path(paths_raw["output_root"], base, "paths.output_root"),
        cache_root=_resolve_path(paths_raw["cache_root"], base, "paths.cache_root"),
        authoritative_input_manifest=_resolve_path(
            paths_raw["authoritative_input_manifest"], base, "paths.authoritative_input_manifest"
        ),
        source_registry=_resolve_path(paths_raw["source_registry"], base, "paths.source_registry"),
        universe_policy=_resolve_path(paths_raw["universe_policy"], base, "paths.universe_policy"),
        universe_csv=_resolve_path(paths_raw["universe_csv"], base, "paths.universe_csv"),
        historical_candidate_policy=_resolve_path(
            paths_raw["historical_candidate_policy"], base, "paths.historical_candidate_policy"
        ),
        historical_candidate_manifest=_resolve_path(
            paths_raw["historical_candidate_manifest"], base, "paths.historical_candidate_manifest"
        ),
        historical_candidates_csv=_resolve_path(
            paths_raw["historical_candidates_csv"], base, "paths.historical_candidates_csv"
        ),
        historical_reconciliation_policy=_resolve_path(
            paths_raw["historical_reconciliation_policy"], base, "paths.historical_reconciliation_policy"
        ),
        historical_reconciliation_manifest=_resolve_path(
            paths_raw["historical_reconciliation_manifest"], base, "paths.historical_reconciliation_manifest"
        ),
        historical_membership_csv=_resolve_path(
            paths_raw["historical_membership_csv"], base, "paths.historical_membership_csv"
        ),
        ticker_aliases_csv=_resolve_path(paths_raw["ticker_aliases_csv"], base, "paths.ticker_aliases_csv"),
        security_events_csv=_resolve_path(
            paths_raw["security_events_csv"], base, "paths.security_events_csv"
        ),
        terminal_events_csv=_resolve_path(
            paths_raw["terminal_events_csv"], base, "paths.terminal_events_csv"
        ),
        market_data_policy=_resolve_path(paths_raw["market_data_policy"], base, "paths.market_data_policy"),
        market_data_manifest=_resolve_path(
            paths_raw["market_data_manifest"], base, "paths.market_data_manifest"
        ),
        market_instruments_csv=_resolve_path(
            paths_raw["market_instruments_csv"], base, "paths.market_instruments_csv"
        ),
        terminal_return_rules_csv=_resolve_path(
            paths_raw["terminal_return_rules_csv"], base, "paths.terminal_return_rules_csv"
        ),
    )

    runtime_raw = _require_mapping(root["runtime"], "runtime")
    _strict_keys(
        runtime_raw,
        {
            "sqlite_timeout_seconds",
            "fail_closed",
            "allow_cross_sector_imports",
            "require_manifest_match_before_mutation",
            "require_database_identity",
        },
        "runtime",
    )
    runtime = RuntimeConfig(
        sqlite_timeout_seconds=float(runtime_raw["sqlite_timeout_seconds"]),
        fail_closed=_require_bool(runtime_raw["fail_closed"], "runtime.fail_closed"),
        allow_cross_sector_imports=_require_bool(
            runtime_raw["allow_cross_sector_imports"], "runtime.allow_cross_sector_imports"
        ),
        require_manifest_match_before_mutation=_require_bool(
            runtime_raw["require_manifest_match_before_mutation"],
            "runtime.require_manifest_match_before_mutation",
        ),
        require_database_identity=_require_bool(
            runtime_raw["require_database_identity"], "runtime.require_database_identity"
        ),
    )

    historical_raw = _require_mapping(root["historical_contract"], "historical_contract")
    _strict_keys(
        historical_raw,
        {
            "point_in_time_membership_required_for_calibration",
            "current_universe_is_survivorship_corrected",
            "current_universe_calibration_eligible",
        },
        "historical_contract",
    )
    historical = HistoricalContract(
        point_in_time_membership_required_for_calibration=_require_bool(
            historical_raw["point_in_time_membership_required_for_calibration"],
            "historical_contract.point_in_time_membership_required_for_calibration",
        ),
        current_universe_is_survivorship_corrected=_require_bool(
            historical_raw["current_universe_is_survivorship_corrected"],
            "historical_contract.current_universe_is_survivorship_corrected",
        ),
        current_universe_calibration_eligible=_require_bool(
            historical_raw["current_universe_calibration_eligible"],
            "historical_contract.current_universe_calibration_eligible",
        ),
    )

    repository_root = package_root.parent
    config = BasicMaterialsConfig(
        config_version=1,
        config_path=config_path,
        package_root=package_root,
        repository_root=repository_root,
        model=model,
        paths=paths,
        runtime=runtime,
        historical_contract=historical,
    )
    validate_config_contract(config)
    return config


def validate_config_contract(config: BasicMaterialsConfig) -> None:
    """Enforce the non-negotiable package identity and promotion boundaries."""

    if config.model.family != MODEL_FAMILY:
        raise ConfigError(f"model.family must be {MODEL_FAMILY!r}")
    if config.model.sector != SECTOR:
        raise ConfigError(f"model.sector must be {SECTOR!r}")
    if config.model.schema_owner != MODEL_FAMILY:
        raise ConfigError(f"model.schema_owner must be {MODEL_FAMILY!r}")
    if config.model.implemented_stage != 3:
        raise ConfigError("model.implemented_stage must be 3 for the adjusted-market-data release")
    if config.model.promotion_state != "shadow_monitor":
        raise ConfigError("model.promotion_state must remain 'shadow_monitor'")
    if config.model.portfolio_candidate_gate or config.model.oos_score_valid_flag:
        raise ConfigError("portfolio and out-of-sample validity gates must remain false")
    if not config.runtime.fail_closed:
        raise ConfigError("runtime.fail_closed must be true")
    if config.runtime.allow_cross_sector_imports:
        raise ConfigError("runtime.allow_cross_sector_imports must be false")
    if not config.runtime.require_manifest_match_before_mutation:
        raise ConfigError("runtime.require_manifest_match_before_mutation must be true")
    if not config.runtime.require_database_identity:
        raise ConfigError("runtime.require_database_identity must be true")
    if config.runtime.sqlite_timeout_seconds <= 0:
        raise ConfigError("runtime.sqlite_timeout_seconds must be positive")
    if not config.historical_contract.point_in_time_membership_required_for_calibration:
        raise ConfigError("point-in-time membership must be required for calibration")
    if config.historical_contract.current_universe_is_survivorship_corrected:
        raise ConfigError("the current universe must not be represented as survivorship corrected")
    if config.historical_contract.current_universe_calibration_eligible:
        raise ConfigError("the current universe must not be calibration eligible")

    expected_output = (config.repository_root / "output" / MODEL_FAMILY).resolve(strict=False)
    if config.paths.output_root != expected_output:
        raise ConfigError(f"paths.output_root must resolve to {expected_output}")
    if not _is_within(config.paths.cache_root, config.paths.output_root):
        raise ConfigError("paths.cache_root must be inside the Basic Materials output root")
    if config.paths.database.name.lower() != "basic_materials.sqlite":
        raise ConfigError("the configured database filename must be basic_materials.sqlite")

    expected_data_root = (config.package_root / "data").resolve(strict=False)
    for label, path in (
        ("authoritative_input_manifest", config.paths.authoritative_input_manifest),
        ("source_registry", config.paths.source_registry),
        ("universe_policy", config.paths.universe_policy),
        ("historical_candidate_policy", config.paths.historical_candidate_policy),
        ("historical_candidate_manifest", config.paths.historical_candidate_manifest),
        ("historical_reconciliation_policy", config.paths.historical_reconciliation_policy),
        ("historical_reconciliation_manifest", config.paths.historical_reconciliation_manifest),
        ("market_data_policy", config.paths.market_data_policy),
        ("market_data_manifest", config.paths.market_data_manifest),
    ):
        if not _is_within(path, expected_data_root):
            raise ConfigError(f"paths.{label} must be owned by basic_materials/data")

    expected_universe = (config.repository_root / "ticker_mapping" / "basic_materials.csv").resolve(strict=False)
    if config.paths.universe_csv != expected_universe:
        raise ConfigError(f"paths.universe_csv must resolve to {expected_universe}")

    expected_system_root = (config.package_root / "system_csvs").resolve(strict=False)
    expected_system_files = {
        "historical_candidates_csv": "basic_materials_deactivated_candidates.csv",
        "historical_membership_csv": "basic_materials_historical_membership.csv",
        "ticker_aliases_csv": "basic_materials_ticker_aliases.csv",
        "security_events_csv": "basic_materials_security_events.csv",
        "terminal_events_csv": "basic_materials_terminal_events.csv",
        "market_instruments_csv": "basic_materials_market_instruments.csv",
        "terminal_return_rules_csv": "basic_materials_terminal_return_rules.csv",
    }
    for label, filename in expected_system_files.items():
        path = getattr(config.paths, label)
        expected = expected_system_root / filename
        if path != expected:
            raise ConfigError(f"paths.{label} must resolve to {expected}")


def resolve_cli_path(value: str | Path | None, default: Path) -> Path:
    """Resolve an explicit CLI override without weakening configured ownership checks."""

    if value is None:
        return default
    return Path(value).expanduser().resolve(strict=False)
