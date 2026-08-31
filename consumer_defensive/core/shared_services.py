"""Frozen and verifiable shared-service boundary for Consumer Defensive."""

from __future__ import annotations

import ast
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping

import yaml

from consumer_defensive.core.config import resolve_path


CONTRACT_SCHEMA = "consumer_defensive_shared_service_contract_v1"
MODEL_FAMILY = "consumer_defensive"
CODE_SERVICES = frozenset({"dedicated_parser", "factor_validation"})
PLATFORM_SERVICES = frozenset({"global_orchestrator", "portfolio_layer"})
READ_ONLY_DATA_SERVICES = frozenset({"sec_insider", "market_positioning"})
REFERENCE_DATA_SERVICES = frozenset({"ticker_mapping"})
EXTERNAL_PROVIDERS = frozenset({"sec_edgar", "yahoo_finance", "norgate"})
DOWNSTREAM_SERVICES = frozenset({"black_litterman", "execution", "macro_layer", "optimizer", "risk", "valuation"})
_ROOT_KEYS = frozenset(
    {
        "schema_version",
        "model_family",
        "status",
        "ownership",
        "code_services",
        "platform_services",
        "read_only_data_services",
        "reference_data_services",
        "external_providers",
        "downstream_portfolio_services",
    }
)
_EXPECTED_CODE_SERVICES = {
    "dedicated_parser": {
        "access_mode": "approved_package_api_and_sector_adapter",
        "adapter": "consumer_defensive.adapters.dedicated_parser_adapter",
        "allowed_import_modules": [
            "dedicated_parser.adapters",
            "dedicated_parser.adjudication",
            "dedicated_parser.atomic_io",
            "dedicated_parser.catalog",
            "dedicated_parser.cli",
            "dedicated_parser.contracts",
            "dedicated_parser.path_io",
            "dedicated_parser.review_replay",
            "dedicated_parser.schema",
            "dedicated_parser.sec_paths",
            "dedicated_parser.semantic",
            "dedicated_parser.storage",
        ],
    },
    "factor_validation": {
        "access_mode": "approved_package_api_and_sector_adapter",
        "adapter": "consumer_defensive.adapters.factor_validation",
        "allowed_import_modules": [
            "factor_validation",
            "factor_validation.artifacts",
        ],
    },
}
_EXPECTED_PLATFORM_SERVICES = {
    "global_orchestrator": {
        "access_mode": "command_execution_and_calendar_api",
        "registry": "orchestration/registry.yaml",
        "calendar_api": "orchestration.run_all.is_trading_day",
    },
    "portfolio_layer": {
        "access_mode": "immutable_file_handoff",
        "adapter": MODEL_FAMILY,
    },
}
_EXPECTED_EXTERNAL_PROVIDERS = {
    "sec_edgar": {"access_mode": "public_api_and_filing_archive"},
    "yahoo_finance": {"access_mode": "adjusted_price_and_fx_fallback"},
    "norgate": {"access_mode": "point_in_time_membership_and_market_data"},
}
_FORBIDDEN_SECTOR_ROOTS = frozenset(
    {"biotech_index", "future_only_evidence", "industrials", "med_devices", "technology", "transportation"}
)


def _mapping(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a mapping")
    return dict(value)


def _exact_keys(value: Any, expected: frozenset[str], *, label: str) -> dict[str, Any]:
    payload = _mapping(value, label=label)
    if set(payload) != expected:
        raise ValueError(f"{label} must contain exactly {sorted(expected)}")
    return payload


def canonical_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_shared_service_contract(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Fail closed on every code, data, platform, and downstream field."""

    contract = _exact_keys(payload, _ROOT_KEYS, label="shared-service contract")
    if contract["schema_version"] != CONTRACT_SCHEMA:
        raise ValueError("unsupported Consumer shared-service contract")
    if contract["model_family"] != MODEL_FAMILY or contract["status"] != "frozen":
        raise ValueError("Consumer shared-service contract identity changed")
    ownership = _exact_keys(
        contract["ownership"],
        frozenset(
            {
                "sector_database_owner",
                "sector_code_owner",
                "cross_sector_code_imports_allowed",
            }
        ),
        label="ownership",
    )
    if ownership != {
        "sector_database_owner": MODEL_FAMILY,
        "sector_code_owner": MODEL_FAMILY,
        "cross_sector_code_imports_allowed": False,
    }:
        raise ValueError("Consumer ownership boundary changed")

    code = _exact_keys(contract["code_services"], CODE_SERVICES, label="code_services")
    for name, expected in _EXPECTED_CODE_SERVICES.items():
        item = _exact_keys(code[name], frozenset(expected), label=f"code_services.{name}")
        if item != expected:
            raise ValueError(f"{name}: approved package/API boundary changed")
    platform = _exact_keys(contract["platform_services"], PLATFORM_SERVICES, label="platform_services")
    for name, expected in _EXPECTED_PLATFORM_SERVICES.items():
        item = _exact_keys(platform[name], frozenset(expected), label=f"platform_services.{name}")
        if item != expected:
            raise ValueError(f"{name}: platform boundary changed")

    read_only = _exact_keys(
        contract["read_only_data_services"],
        READ_ONLY_DATA_SERVICES,
        label="read_only_data_services",
    )
    expected_config_keys = {
        "sec_insider": "positioning.form4_upstream_db",
        "market_positioning": "positioning.market_positioning_upstream_db",
    }
    for name, config_key in expected_config_keys.items():
        expected = {"access_mode": "sqlite_read_only", "config_key": config_key}
        item = _exact_keys(read_only[name], frozenset(expected), label=f"read_only_data_services.{name}")
        if item != expected:
            raise ValueError(f"{name}: read-only data boundary changed")

    reference = _exact_keys(
        contract["reference_data_services"],
        REFERENCE_DATA_SERVICES,
        label="reference_data_services",
    )
    expected_reference = {
        "access_mode": "immutable_csv_read",
        "path": "ticker_mapping/consumer_defensive.csv",
    }
    item = _exact_keys(reference["ticker_mapping"], frozenset(expected_reference), label="ticker_mapping")
    if item != expected_reference:
        raise ValueError("ticker-mapping boundary changed")

    external = _exact_keys(contract["external_providers"], EXTERNAL_PROVIDERS, label="external_providers")
    for name, expected in _EXPECTED_EXTERNAL_PROVIDERS.items():
        item = _exact_keys(external[name], frozenset(expected), label=f"external_providers.{name}")
        if item != expected:
            raise ValueError(f"{name}: external-provider boundary changed")

    downstream = _exact_keys(
        contract["downstream_portfolio_services"],
        frozenset({"access_via", "services"}),
        label="downstream_portfolio_services",
    )
    services = downstream["services"]
    if downstream["access_via"] != "portfolio_layer_only":
        raise ValueError("downstream services must be accessed through Portfolio Layer")
    if (
        not isinstance(services, list)
        or len(services) != len(DOWNSTREAM_SERVICES)
        or set(services) != DOWNSTREAM_SERVICES
    ):
        raise ValueError("downstream Portfolio Layer service census changed")
    return contract


def load_shared_service_contract(path: Path) -> dict[str, Any]:
    resolved = Path(path).expanduser().resolve()
    payload = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    return validate_shared_service_contract(_mapping(payload, label="shared-service contract"))


def shared_service_contract_sha256(contract: Mapping[str, Any]) -> str:
    return canonical_sha256(validate_shared_service_contract(contract))


def audit_import_boundaries(*, repository_root: Path) -> dict[str, Any]:
    """AST-check all active Consumer imports against the frozen service boundary."""

    root = Path(repository_root).expanduser().resolve()
    consumer_root = root / "consumer_defensive"
    allowed = {
        module for definition in _EXPECTED_CODE_SERVICES.values() for module in definition["allowed_import_modules"]
    }
    allowed.add("orchestration.run_all")
    violations: list[str] = []
    checked = 0
    for path in sorted(consumer_root.rglob("*.py")):
        if path.name.startswith("_") and "audit_final" in path.name:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        checked += 1
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            else:
                names = []
            for name in names:
                top = name.split(".", 1)[0]
                if top in _FORBIDDEN_SECTOR_ROOTS:
                    violations.append(f"{path.relative_to(root)}:{node.lineno}:{name}:cross-sector")
                if top in CODE_SERVICES and name not in allowed:
                    violations.append(f"{path.relative_to(root)}:{node.lineno}:{name}:unapproved-api")
                if top == "orchestration" and name != "orchestration.run_all":
                    violations.append(f"{path.relative_to(root)}:{node.lineno}:{name}:unapproved-platform-api")
    if violations:
        raise ValueError("Consumer shared-service import violations: " + "; ".join(violations))
    for definition in _EXPECTED_CODE_SERVICES.values():
        module_path = root.joinpath(*definition["adapter"].split(".")).with_suffix(".py")
        if not module_path.is_file():
            raise ValueError(f"configured Consumer adapter is missing: {module_path}")
    return {"checked_python_files": checked, "violations": 0}


def _read_only_sqlite_probe(path: Path, *, label: str, required_tables: frozenset[str]) -> int:
    if not path.is_file():
        raise ValueError(f"{label} database does not exist: {path}")
    uri = path.as_uri() + "?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=5.0) as connection:
            connection.execute("PRAGMA query_only=ON")
            query_only = int(connection.execute("PRAGMA query_only").fetchone()[0])
            if query_only != 1:
                raise ValueError(f"{label} did not enter SQLite query-only mode")
            observed_tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
    except sqlite3.Error as exc:
        raise ValueError(f"{label} is not a readable SQLite database: {path}") from exc
    missing = sorted(required_tables - observed_tables)
    if missing:
        raise ValueError(f"{label} is missing required tables: {', '.join(missing)}")
    return len(observed_tables)


def audit_config_connections(config: Mapping[str, Any], *, repository_root: Path) -> dict[str, Any]:
    """Resolve and verify adapters, immutable files, and read-only SQLite inputs."""

    payload = dict(config)
    root = Path(repository_root).expanduser().resolve()
    positioning = _mapping(payload.get("positioning"), label="config.positioning")
    universe = _mapping(payload.get("universe"), label="config.universe")
    dedicated = _mapping(payload.get("dedicated_parser"), label="config.dedicated_parser")
    factor = _mapping(payload.get("factor_validation"), label="config.factor_validation")
    portfolio = _mapping(payload.get("portfolio_layer"), label="config.portfolio_layer")

    upstream = {
        "sec_insider": resolve_path(positioning.get("form4_upstream_db"), base_dir=root),
        "market_positioning": resolve_path(positioning.get("market_positioning_upstream_db"), base_dir=root),
    }
    required_tables = {
        "sec_insider": frozenset({"sec_ownership_submission", "form4_events_tier1"}),
        "market_positioning": frozenset(
            {
                "institutional_13f_ownership_snapshots",
                "short_interest_snapshots",
                "ibkr_borrow_fee_rate_daily",
                "ibkr_shortable_shares_snapshots",
            }
        ),
    }
    table_counts = {
        name: _read_only_sqlite_probe(path, label=name, required_tables=required_tables[name])
        for name, path in upstream.items()
    }
    expected_ticker_map = (root / "ticker_mapping/consumer_defensive.csv").resolve()
    configured_ticker_map = resolve_path(universe.get("authoritative_source"), base_dir=root)
    if configured_ticker_map != expected_ticker_map or not configured_ticker_map.is_file():
        raise ValueError("Consumer authoritative ticker mapping changed or is missing")
    if dedicated.get("model_family") != MODEL_FAMILY:
        raise ValueError("dedicated_parser is not bound to Consumer Defensive")
    if factor.get("sector_adapter") != "consumer_defensive.adapters.factor_validation":
        raise ValueError("factor_validation adapter boundary changed")
    if portfolio.get("adapter") != MODEL_FAMILY or portfolio.get("canonical_sector") != "Consumer Staples":
        raise ValueError("Portfolio Layer Consumer adapter boundary changed")
    imports = audit_import_boundaries(repository_root=root)
    return {
        "status": "PASS",
        "model_family": MODEL_FAMILY,
        "code_services": sorted(CODE_SERVICES),
        "platform_services": sorted(PLATFORM_SERVICES),
        "read_only_data_services": sorted(READ_ONLY_DATA_SERVICES),
        "reference_data_services": sorted(REFERENCE_DATA_SERVICES),
        "external_providers": sorted(EXTERNAL_PROVIDERS),
        "downstream_portfolio_services": sorted(DOWNSTREAM_SERVICES),
        "resolved_read_only_databases": {name: str(path) for name, path in upstream.items()},
        "read_only_database_table_counts": table_counts,
        "import_boundary_audit": imports,
    }


__all__ = [
    "CONTRACT_SCHEMA",
    "audit_config_connections",
    "audit_import_boundaries",
    "load_shared_service_contract",
    "shared_service_contract_sha256",
    "validate_shared_service_contract",
]
