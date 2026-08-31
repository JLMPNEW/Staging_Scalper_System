"""Fail-closed Consumer Defensive production refresh orchestration.

This module deliberately owns orchestration only.  Every data mutation is
performed by an existing Consumer Defensive stage script and the final score
publication is delegated to the calibrated, authority-pinned Stage 31
publisher.  It has no Portfolio Layer or foreign-sector dependency.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from consumer_defensive.core.atomic_io import atomic_write_text
from consumer_defensive.core.calibration_scope import calibration_scope_contract
from consumer_defensive.core.config import cfg_get, load_config, resolve_path
from consumer_defensive.core.promotion_engine_v3 import (
    REQUIRED_COHORTS,
    build_production_model_contract,
    canonical_sha256,
)
from consumer_defensive.core.production_scores_v3 import (
    RANK_COLUMNS,
    load_bound_artifacts,
    publisher_bindings,
    rank_row_sha256,
)
from consumer_defensive.core.stage3_runtime import database_path
from consumer_defensive.core.trading_calendar_v1 import (
    assert_one_session_lag,
    prior_xnys_session,
)


SCHEMA_VERSION = "consumer_defensive_production_refresh_manifest_v3"
DEPENDENCY_INVENTORY_SCHEMA_VERSION = (
    "consumer_defensive_refresh_dependency_inventory_v1"
)
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_ROOT.parent
SCRIPTS_ROOT = PACKAGE_ROOT / "scripts"
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
PUBLISHER_SCRIPT = "31_publish_consumer_defensive_production_scores_v3.py"
RANK_FILENAME = "consumer_defensive_final_rank_table.csv"
PUBLISHER_MANIFEST_FILENAME = "consumer_defensive_production_score_manifest_v3.json"
TERMINAL_MANIFEST_FILENAME = "consumer_defensive_production_refresh_manifest_v3.json"


class ProductionRefreshError(RuntimeError):
    """Raised when a production refresh contract cannot be satisfied."""


@dataclass(frozen=True)
class RefreshStep:
    """One Consumer-owned script invocation in an immutable refresh plan."""

    name: str
    script: Path
    arguments: tuple[str, ...]
    network: bool = False

    def argv(self, python_executable: Path) -> tuple[str, ...]:
        return (str(python_executable), str(self.script), *self.arguments)


@dataclass(frozen=True)
class RefreshPlan:
    """Resolved production refresh plan for one allocation session."""

    allocation_asof_date: str
    signal_asof_date: str
    config_path: Path
    database_path: Path
    output_root: Path
    consumer_output_dir: Path
    python_executable: Path
    steps: tuple[RefreshStep, ...]
    rank_path: Path
    publisher_manifest_path: Path
    terminal_manifest_path: Path
    cache_only: bool
    force_refresh: bool

    def contract_dict(self) -> dict[str, Any]:
        dependency_inventory = _implementation_dependency_inventory()
        return {
            "schema_version": SCHEMA_VERSION,
            "implementation": {
                "path": str(Path(__file__).resolve()),
                "sha256": _sha256(Path(__file__).resolve()),
                "dependency_inventory": dependency_inventory,
            },
            "asof_date": self.allocation_asof_date,
            "signal_asof_date": self.signal_asof_date,
            "config_path": str(self.config_path),
            "database_path": str(self.database_path),
            "output_root": str(self.output_root),
            "consumer_output_dir": str(self.consumer_output_dir),
            "cache_only": self.cache_only,
            "force_refresh": self.force_refresh,
            "rank_path": str(self.rank_path),
            "publisher_manifest_path": str(self.publisher_manifest_path),
            "terminal_manifest_path": str(self.terminal_manifest_path),
            "steps": [
                {
                    "sequence": index,
                    "name": step.name,
                    "script": str(step.script),
                    "script_sha256": _sha256(step.script),
                    "network": step.network,
                    "argv": list(step.argv(self.python_executable)),
                }
                for index, step in enumerate(self.steps, start=1)
            ],
        }

    def payload_sha256(self) -> str:
        return canonical_sha256(self.contract_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.contract_dict(),
            "status": "PLAN_ONLY",
            "plan_sha256": self.payload_sha256(),
        }


def _implementation_dependency_inventory() -> dict[str, Any]:
    """Hash Consumer implementation code and curated data contracts.

    Stage scripts are already hashed individually in the refresh plan. They
    import package code and consume package data files, however, so script-only
    identity is insufficient: a change to a concept map or an imported helper
    must invalidate an existing PASS terminal. Keep this inventory deliberately
    Consumer-owned and extension-restricted so caches and generated outputs can
    never enter the production identity.
    """

    groups = (
        ("core", frozenset({".py"})),
        ("adapters", frozenset({".py"})),
        ("data", frozenset({".csv", ".yaml", ".yml"})),
    )
    files: list[dict[str, Any]] = []
    group_counts: dict[str, int] = {}
    for group, suffixes in groups:
        root = (PACKAGE_ROOT / group).resolve()
        try:
            root.relative_to(PACKAGE_ROOT.resolve())
        except ValueError as exc:  # defensive guard if package layout drifts
            raise ProductionRefreshError(
                f"Consumer dependency root escaped the package: {root}"
            ) from exc
        if not root.is_dir():
            raise ProductionRefreshError(
                f"Consumer dependency root is missing: {root}"
            )
        paths = sorted(
            (
                path.resolve()
                for path in root.rglob("*")
                if path.is_file() and path.suffix.casefold() in suffixes
            ),
            key=lambda path: path.relative_to(PACKAGE_ROOT).as_posix(),
        )
        if not paths:
            raise ProductionRefreshError(
                f"Consumer dependency group has no contract files: {group}"
            )
        group_counts[group] = len(paths)
        files.extend(
            {
                "path": path.relative_to(PACKAGE_ROOT).as_posix(),
                "sha256": _sha256(path),
                "bytes": path.stat().st_size,
            }
            for path in paths
        )

    files.sort(key=lambda record: str(record["path"]))
    inventory: dict[str, Any] = {
        "schema_version": DEPENDENCY_INVENTORY_SCHEMA_VERSION,
        "package_root": str(PACKAGE_ROOT.resolve()),
        "group_counts": group_counts,
        "file_count": len(files),
        "files": files,
    }
    inventory["payload_sha256"] = canonical_sha256(inventory)
    return inventory


Executor = Callable[..., subprocess.CompletedProcess[str]]


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _value_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    atomic_write_text(path, _canonical_json(payload), encoding="utf-8")


def _require_consumer_script(script_name: str) -> Path:
    scripts_root = SCRIPTS_ROOT.resolve()
    script = (scripts_root / script_name).resolve()
    if script.parent != scripts_root:
        raise ProductionRefreshError(
            f"Consumer refresh script escaped the scripts directory: {script_name}"
        )
    if not script.is_file():
        raise ProductionRefreshError(f"Required Consumer refresh script is missing: {script}")
    return script


def _stage_args(
    *,
    config_path: Path,
    database: Path,
    signal_asof_date: str,
    output_dir: Path,
) -> list[str]:
    return [
        "--config",
        str(config_path),
        "--db",
        str(database),
        "--as-of",
        signal_asof_date,
        "--output-dir",
        str(output_dir),
    ]


def _step(
    name: str,
    script_name: str,
    arguments: Sequence[str],
    *,
    network: bool = False,
) -> RefreshStep:
    return RefreshStep(
        name=name,
        script=_require_consumer_script(script_name),
        arguments=tuple(str(value) for value in arguments),
        network=network,
    )


def build_refresh_plan(
    *,
    allocation_asof_date: str,
    signal_asof_date: str | None = None,
    config_path: Path = DEFAULT_CONFIG,
    database: Path | None = None,
    output_root: Path | None = None,
    cache_only: bool = False,
    force_refresh: bool = False,
    python_executable: Path | None = None,
) -> RefreshPlan:
    """Build the exact argv-only refresh plan without executing a subprocess."""

    if cache_only and force_refresh:
        raise ProductionRefreshError(
            "cache_only and force_refresh are mutually exclusive"
        )
    allocation = str(allocation_asof_date)
    signal = signal_asof_date or prior_xnys_session(allocation)
    assert_one_session_lag(
        signal_asof_date=signal,
        allocation_asof_date=allocation,
    )

    resolved_config = Path(config_path).expanduser().resolve()
    if not resolved_config.is_file():
        raise ProductionRefreshError(f"Consumer config is missing: {resolved_config}")
    bundle = load_config(resolved_config)
    resolved_database = database_path(bundle, database).resolve()
    configured_consumer_output_dir = resolve_path(
        cfg_get(bundle.payload, "paths.output_dir"), base_dir=bundle.base_dir
    ).resolve()
    configured_output_root = resolve_path(
        cfg_get(bundle.payload, "production_score_publisher_v3.output_root"),
        base_dir=bundle.base_dir,
    ).resolve()
    if configured_consumer_output_dir != configured_output_root / "consumer_defensive":
        raise ProductionRefreshError(
            "Consumer stage output and production publisher output roots are disconnected"
        )
    resolved_output_root = (
        Path(output_root).expanduser().resolve()
        if output_root is not None
        else configured_output_root
    )
    consumer_output_dir = resolved_output_root / "consumer_defensive"
    resolved_python = Path(python_executable or sys.executable).expanduser().resolve()
    if not resolved_python.is_file():
        raise ProductionRefreshError(f"Python executable is missing: {resolved_python}")

    stage3_dir = consumer_output_dir / "stage3" / signal
    stage2_dir = consumer_output_dir / "stage2" / "universe" / signal
    stage4_dir = consumer_output_dir / "stage4" / signal
    stage5_dir = consumer_output_dir / "stage5" / signal
    stage6a_dir = consumer_output_dir / "stage6a" / signal

    network_refresh_args = (
        ["--cache-only"]
        if cache_only
        else ["--force-refresh"] if force_refresh else []
    )
    steps: list[RefreshStep] = []

    steps.append(
        _step(
            "stage2_readiness_sync",
            "02b_ensure_consumer_defensive_stage2.py",
            [
                "--config",
                str(resolved_config),
                "--db",
                str(resolved_database),
                "--as-of",
                signal,
                "--output-dir",
                str(stage2_dir),
            ],
        )
    )

    market_base = _stage_args(
        config_path=resolved_config,
        database=resolved_database,
        signal_asof_date=signal,
        output_dir=stage3_dir,
    )
    steps.extend(
        [
            _step(
                "market_price_sync",
                "03a_sync_consumer_defensive_yahoo_prices.py",
                [*market_base, *network_refresh_args],
                network=True,
            ),
            _step(
                "market_norgate_price_sync",
                "03b_import_consumer_defensive_norgate_prices.py",
                market_base,
            ),
            _step(
                "market_policy_audit",
                "04_audit_consumer_defensive_market_data_policy.py",
                market_base,
            ),
            _step(
                "market_feature_build",
                "05_build_consumer_defensive_market_features.py",
                market_base,
            ),
            _step(
                "market_stage_validation",
                "06_validate_consumer_defensive_market_stage.py",
                market_base,
            ),
        ]
    )

    financial_base = _stage_args(
        config_path=resolved_config,
        database=resolved_database,
        signal_asof_date=signal,
        output_dir=stage4_dir,
    )
    steps.append(
        _step(
            "sec_financial_sync",
            "07_sync_consumer_defensive_sec_fundamentals.py",
            [*financial_base, *network_refresh_args],
            network=True,
        )
    )
    fx_args = [
        "--config",
        str(resolved_config),
        "--db",
        str(resolved_database),
        "--end",
        signal,
        "--output-dir",
        str(stage4_dir),
        *network_refresh_args,
    ]
    steps.extend(
        [
            _step(
                "financial_fx_sync",
                "11_sync_consumer_defensive_fx_rates.py",
                fx_args,
                network=True,
            ),
            _step(
                "financial_feature_build",
                "08_build_consumer_defensive_financial_features.py",
                financial_base,
            ),
        ]
    )

    positioning_base = _stage_args(
        config_path=resolved_config,
        database=resolved_database,
        signal_asof_date=signal,
        output_dir=stage5_dir,
    )
    steps.extend(
        [
            _step(
                "positioning_upstream_audit",
                "09a_sync_consumer_defensive_positioning_upstream.py",
                positioning_base,
            ),
            _step(
                "positioning_cache_rematch",
                "09b_rematch_consumer_defensive_positioning_cache.py",
                positioning_base,
            ),
            _step(
                "sec_ownership_import",
                "09_sync_consumer_defensive_sec_ownership.py",
                positioning_base,
            ),
            _step(
                "positioning_feature_import",
                "10_import_consumer_defensive_positioning.py",
                positioning_base,
            ),
            _step(
                "positioning_stage_validation",
                "10a_validate_consumer_defensive_sec_positioning.py",
                positioning_base,
            ),
        ]
    )

    scoring_base = _stage_args(
        config_path=resolved_config,
        database=resolved_database,
        signal_asof_date=signal,
        output_dir=stage6a_dir,
    )
    steps.extend(
        [
            _step(
                "stage6a_scoring_feature_build",
                "12_build_consumer_defensive_scoring_features.py",
                scoring_base,
            ),
            _step(
                "stage6a_scoring_feature_validation",
                "12a_validate_consumer_defensive_scoring_features.py",
                scoring_base,
            ),
        ]
    )

    publisher_args = [
        "--asof",
        allocation,
        "--signal-asof-date",
        signal,
        "--config",
        str(resolved_config),
        "--db",
        str(resolved_database),
        "--output-root",
        str(resolved_output_root),
    ]
    steps.append(
        _step(
            "calibrated_score_publish",
            PUBLISHER_SCRIPT,
            publisher_args,
        )
    )

    dashboard_dir = consumer_output_dir / "dashboard" / allocation
    terminal_dir = consumer_output_dir / "orchestration" / allocation
    return RefreshPlan(
        allocation_asof_date=allocation,
        signal_asof_date=signal,
        config_path=resolved_config,
        database_path=resolved_database,
        output_root=resolved_output_root,
        consumer_output_dir=consumer_output_dir,
        python_executable=resolved_python,
        steps=tuple(steps),
        rank_path=dashboard_dir / RANK_FILENAME,
        publisher_manifest_path=dashboard_dir / PUBLISHER_MANIFEST_FILENAME,
        terminal_manifest_path=terminal_dir / TERMINAL_MANIFEST_FILENAME,
        cache_only=bool(cache_only),
        force_refresh=bool(force_refresh),
    )


def _publisher_asof(payload: Mapping[str, Any]) -> str:
    for key in ("asof_date", "allocation_asof_date", "asof"):
        value = str(payload.get(key) or "")
        if value:
            return value
    return ""


def _strict_json_file(path: Path, *, label: str) -> dict[str, Any]:
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise ProductionRefreshError(
                    f"{label} contains duplicate JSON key {key!r}"
                )
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ProductionRefreshError(
            f"{label} contains non-finite JSON constant {value!r}"
        )

    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=pairs,
            parse_constant=reject_constant,
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductionRefreshError(f"{label} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ProductionRefreshError(f"{label} must be a JSON object")
    return payload


def _truthy(value: Any) -> bool:
    return str(value or "").strip().casefold() in {"1", "1.0", "true", "yes", "pass"}


def _publisher_authority(
    plan: RefreshPlan,
    publisher: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Reload and reconstruct the config-pinned promotion authority."""

    bundle = load_config(plan.config_path)
    bindings = publisher_bindings(bundle)
    try:
        activation, candidates, identities = load_bound_artifacts(
            activation_registry_path=bindings["activation_registry_path"],
            trusted_activation_registry_file_sha256=bindings[
                "activation_registry_file_sha256"
            ],
            trusted_activation_registry_payload_sha256=bindings[
                "activation_registry_payload_sha256"
            ],
            candidate_registry_path=bindings["candidate_registry_path"],
            trusted_candidate_registry_file_sha256=bindings[
                "candidate_registry_file_sha256"
            ],
            trusted_candidate_registry_payload_sha256=bindings[
                "candidate_registry_payload_sha256"
            ],
        )
    except (FileNotFoundError, ValueError) as exc:
        raise ProductionRefreshError(
            f"Stage 31 pinned promotion authority is invalid: {exc}"
        ) from exc

    identity_drift = {
        key: {
            "manifest": publisher.get(key),
            "authoritative": value,
        }
        for key, value in identities.items()
        if publisher.get(key) != value
    }
    if identity_drift:
        raise ProductionRefreshError(
            "Stage 31 publisher promotion-artifact identities drifted: "
            + ", ".join(sorted(identity_drift))
        )

    candidate_by_id = {
        str(candidate["candidate_id"]): dict(candidate)
        for candidate in candidates["candidates"]
    }
    contracts: dict[str, dict[str, Any]] = {}
    for cohort in sorted(REQUIRED_COHORTS):
        lock = activation["cohorts"][cohort]
        candidate_id = str(lock["selected_candidate_id"])
        if candidate_id != str(
            bindings["selected_candidate_id_by_cohort"][cohort]
        ):
            raise ProductionRefreshError(
                f"Stage 31 {cohort} selected candidate differs from config"
            )
        candidate = candidate_by_id.get(candidate_id)
        if candidate is None or str(candidate.get("cohort") or "") != cohort:
            raise ProductionRefreshError(
                f"Stage 31 {cohort} selected candidate is absent or mis-scoped"
            )
        if int(candidate.get("horizon_sessions", -1)) != 63:
            raise ProductionRefreshError(
                f"Stage 31 {cohort} selected candidate has the wrong horizon"
            )
        if candidate.get("specialized_weights"):
            raise ProductionRefreshError(
                f"Stage 31 {cohort} requires unsupported specialized weights"
            )
        contract = build_production_model_contract(
            cohort=cohort,
            selected_candidate_id=candidate_id,
            candidate_definition=candidate,
            candidate_registry_sha256=candidates["payload_sha256"],
            score_model_version=str(lock["score_model_version"]),
            scoring_contract_version=str(lock["scoring_contract_version"]),
        )
        configured_contract = str(
            bindings["model_contract_sha256_by_cohort"][cohort]
        )
        if (
            contract["payload_sha256"] != lock["model_contract_sha256"]
            or contract["payload_sha256"] != configured_contract
            or str(lock["scoring_contract_version"])
            != str(bindings["scoring_contract_version"])
        ):
            raise ProductionRefreshError(
                f"Stage 31 {cohort} model contract is not config-pinned"
            )
        contracts[cohort] = contract

    declared_contracts = publisher.get("model_contract_sha256_by_cohort")
    expected_contracts = {
        cohort: contracts[cohort]["payload_sha256"]
        for cohort in sorted(REQUIRED_COHORTS)
    }
    if declared_contracts != expected_contracts:
        raise ProductionRefreshError(
            "Stage 31 publisher model-contract identities drifted"
        )
    return activation, contracts


def _verify_publisher_row_authority(
    rows: Sequence[Mapping[str, Any]],
    *,
    activation: Mapping[str, Any],
    contracts: Mapping[str, Mapping[str, Any]],
) -> None:
    exact_fields = {
        "consumer_defensive_production_lock_id": "lock_id",
        "consumer_defensive_production_lock_sha256": "payload_sha256",
        "consumer_defensive_model_contract_sha256": "model_contract_sha256",
        "consumer_defensive_decision_sha256": "decision_sha256",
        "consumer_defensive_selected_candidate_id": "selected_candidate_id",
        "consumer_defensive_deployment_state": "deployment_state",
        "score_model_version": "score_model_version",
        "model_version": "score_model_version",
        "scoring_contract_version": "scoring_contract_version",
        "promotion_state": "promotion_state",
    }
    for position, row in enumerate(rows, start=1):
        ticker = str(row.get("ticker") or "").strip() or f"row_{position}"
        cohort = str(row.get("calibration_cohort") or "").strip()
        lock = dict(activation["cohorts"].get(cohort) or {})
        if not lock or cohort not in contracts:
            raise ProductionRefreshError(
                f"Stage 31 {ticker} has no authoritative cohort lock"
            )
        mismatches = [
            field
            for field, lock_field in exact_fields.items()
            if str(row.get(field) or "").strip()
            != str(lock[lock_field]).strip()
        ]
        if str(row.get("consumer_defensive_model_contract_sha256") or "") != str(
            contracts[cohort]["payload_sha256"]
        ):
            mismatches.append("consumer_defensive_model_contract_sha256")
        if str(row.get("calibration_lock_date") or "") != str(
            activation["asof_date"]
        ):
            mismatches.append("calibration_lock_date")
        for field, lock_field in (
            ("consumer_defensive_optimizer_cap", "optimizer_cap"),
            (
                "consumer_defensive_confidence_multiplier",
                "confidence_multiplier",
            ),
        ):
            try:
                observed = float(row.get(field))
                expected = float(lock[lock_field])
            except (TypeError, ValueError):
                mismatches.append(field)
                continue
            if (
                not math.isfinite(observed)
                or not math.isclose(
                    observed,
                    expected,
                    rel_tol=0.0,
                    abs_tol=1e-12,
                )
            ):
                mismatches.append(field)

        expected_gate = bool(lock["investable"]) and _truthy(
            row.get("rank_ready_flag")
        ) and _truthy(row.get("oos_score_valid_flag"))
        if _truthy(row.get("portfolio_candidate_gate")) is not expected_gate:
            mismatches.append("portfolio_candidate_gate")
        expected_status = "eligible" if expected_gate else "not_eligible"
        expected_reason = "ok" if expected_gate else "promotion_or_rank_gate"
        if str(row.get("portfolio_candidate_status") or "") != expected_status:
            mismatches.append("portfolio_candidate_status")
        if str(row.get("portfolio_candidate_reason") or "") != expected_reason:
            mismatches.append("portfolio_candidate_reason")
        if mismatches:
            raise ProductionRefreshError(
                f"Stage 31 {ticker} promotion authority mismatch: "
                + ", ".join(sorted(set(mismatches)))
            )


def verify_publisher_outputs(plan: RefreshPlan) -> dict[str, Any]:
    """Verify the physical rank table and Stage 31 PASS manifest."""

    if not plan.rank_path.is_file() or plan.rank_path.stat().st_size <= 0:
        raise ProductionRefreshError(f"Stage 31 rank table is missing or empty: {plan.rank_path}")
    if not plan.publisher_manifest_path.is_file():
        raise ProductionRefreshError(
            f"Stage 31 publisher manifest is missing: {plan.publisher_manifest_path}"
        )
    publisher = _strict_json_file(
        plan.publisher_manifest_path,
        label="Stage 31 publisher manifest",
    )
    if publisher.get("schema_version") != "consumer_defensive_production_score_manifest_v3":
        raise ProductionRefreshError("Stage 31 publisher manifest schema is not v3")
    if str(publisher.get("status") or "").upper() != "PASS":
        raise ProductionRefreshError("Stage 31 publisher manifest status is not PASS")
    publisher_asof = _publisher_asof(publisher)
    if publisher_asof != plan.allocation_asof_date:
        raise ProductionRefreshError(
            "Stage 31 publisher manifest allocation date mismatch: "
            f"{publisher_asof!r} != {plan.allocation_asof_date!r}"
        )
    manifest_signal = str(publisher.get("signal_asof_date") or "")
    if manifest_signal != plan.signal_asof_date:
        raise ProductionRefreshError(
            "Stage 31 publisher manifest signal date mismatch: "
            f"{manifest_signal!r} != {plan.signal_asof_date!r}"
        )

    declared_rank_path = Path(str(publisher.get("rank_csv_path") or "")).resolve()
    if declared_rank_path != plan.rank_path.resolve():
        raise ProductionRefreshError("Stage 31 manifest points to the wrong rank table")
    if str(publisher.get("rank_csv_file_sha256") or "") != _sha256(plan.rank_path):
        raise ProductionRefreshError("Stage 31 rank-table hash does not match the manifest")
    payload_hash = str(publisher.get("payload_sha256") or "")
    unsigned_publisher = dict(publisher)
    unsigned_publisher.pop("payload_sha256", None)
    if payload_hash != canonical_sha256(unsigned_publisher):
        raise ProductionRefreshError("Stage 31 publisher manifest self-hash mismatch")
    scope_contract = calibration_scope_contract(load_config(plan.config_path))
    manifest_scope = publisher.get("calibration_scope_contract")
    if manifest_scope != scope_contract or str(
        publisher.get("calibration_scope_sha256") or ""
    ) != str(scope_contract["payload_sha256"]):
        raise ProductionRefreshError(
            "Stage 31 calibration-scope contract differs from the reviewed config"
        )
    if (
        int(publisher.get("entry_lag_trading_sessions", 0)) != 1
        or publisher.get("database_access_mode") != "read_only"
        or int(publisher.get("database_write_count", -1)) != 0
        or publisher.get("portfolio_write_performed") is not False
    ):
        raise ProductionRefreshError("Stage 31 publisher safety contract is invalid")
    declared_database = Path(str(publisher.get("source_database_path") or "")).resolve()
    if declared_database != plan.database_path.resolve():
        raise ProductionRefreshError("Stage 31 publisher used the wrong source database")
    main_hash = str(publisher.get("source_database_file_sha256") or "")
    wal_hash = str(publisher.get("source_database_wal_file_sha256") or "")
    if (
        len(main_hash) != 64
        or any(char not in "0123456789abcdef" for char in main_hash)
        or (
            wal_hash
            and (
                len(wal_hash) != 64
                or any(char not in "0123456789abcdef" for char in wal_hash)
            )
        )
        or int(publisher.get("source_database_data_version", -1)) < 0
    ):
        raise ProductionRefreshError("Stage 31 database provenance is invalid")
    activation, contracts = _publisher_authority(plan, publisher)

    with plan.rank_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = tuple(reader.fieldnames or ())
        if fieldnames != RANK_COLUMNS:
            raise ProductionRefreshError(
                "Stage 31 rank table schema differs from the exact producer contract"
            )
        rows = list(reader)
    if not rows:
        raise ProductionRefreshError("Stage 31 rank table has no rows")
    raw_tickers = [str(row.get("ticker") or "").strip() for row in rows]
    tickers = [ticker.upper() for ticker in raw_tickers]
    if (
        any(not ticker for ticker in tickers)
        or raw_tickers != tickers
        or len(tickers) != len(set(tickers))
    ):
        raise ProductionRefreshError(
            "Stage 31 rank table has noncanonical, blank, or duplicate tickers"
        )
    excluded = set(str(value) for value in scope_contract["excluded_tickers"])
    leaked = sorted(excluded.intersection(tickers))
    if leaked:
        raise ProductionRefreshError(
            "Stage 31 rank table contains reviewed excluded tickers: "
            + ", ".join(leaked[:10])
        )
    if any(
        str(row.get("consumer_defensive_calibration_scope_sha256") or "")
        != str(scope_contract["payload_sha256"])
        for row in rows
    ):
        raise ProductionRefreshError(
            "Stage 31 rank rows are not bound to the reviewed calibration scope"
        )
    stale_row_hashes = [
        ticker
        for ticker, row in zip(tickers, rows, strict=True)
        if str(row.get("row_sha256") or "") != rank_row_sha256(row)
    ]
    if stale_row_hashes:
        raise ProductionRefreshError(
            "Stage 31 rank rows failed their self-hash: "
            + ", ".join(stale_row_hashes[:10])
        )
    if any(
        str(row.get("asof_date") or "") != plan.allocation_asof_date
        or str(row.get("allocation_asof_date") or "") != plan.allocation_asof_date
        or str(row.get("signal_asof_date") or "") != plan.signal_asof_date
        or str(row.get("entry_lag_trading_sessions") or "") not in {"1", "1.0"}
        for row in rows
    ):
        raise ProductionRefreshError(
            "Stage 31 rank rows violate the signal/allocation chronology contract"
        )
    _verify_publisher_row_authority(
        rows,
        activation=activation,
        contracts=contracts,
    )
    invalid_candidates = [
        str(row.get("ticker") or "")
        for row in rows
        if _truthy(row.get("portfolio_candidate_gate"))
        and (
            not _truthy(row.get("oos_score_valid_flag"))
            or not _truthy(row.get("rank_ready_flag"))
        )
    ]
    if invalid_candidates:
        raise ProductionRefreshError(
            "Stage 31 gated non-rank-ready or OOS-invalid rows: "
            + ", ".join(invalid_candidates[:10])
        )
    cohort_rows: dict[str, list[dict[str, str]]] = {
        cohort: [] for cohort in REQUIRED_COHORTS
    }
    for row in rows:
        cohort = str(row.get("calibration_cohort") or "")
        if cohort not in cohort_rows:
            raise ProductionRefreshError(f"Stage 31 emitted unsupported cohort {cohort!r}")
        cohort_rows[cohort].append(row)
    if any(not values for values in cohort_rows.values()):
        raise ProductionRefreshError("Stage 31 omitted at least one Consumer cohort")
    for cohort, values in cohort_rows.items():
        states = {str(row["consumer_defensive_deployment_state"]) for row in values}
        if len(states) != 1:
            raise ProductionRefreshError(f"Stage 31 {cohort} deployment state is inconsistent")
        state = next(iter(states))
        gated = sum(_truthy(row["portfolio_candidate_gate"]) for row in values)
        if state == "active_full" and gated <= 0:
            raise ProductionRefreshError(f"Stage 31 active cohort {cohort} has no eligible rows")
        if state != "active_full" and (
            gated > 0
            or any(float(row["consumer_defensive_optimizer_cap"]) != 0.0 for row in values)
        ):
            raise ProductionRefreshError(f"Stage 31 inactive cohort {cohort} is allocatable")

    published_by_cohort = {
        cohort: len(values) for cohort, values in sorted(cohort_rows.items())
    }
    expected_by_cohort = {
        str(cohort): int(count)
        for cohort, count in scope_contract[
            "expected_remaining_current_by_cohort"
        ].items()
    }
    if (
        len(rows) != int(scope_contract["expected_remaining_current_ticker_count"])
        or published_by_cohort != expected_by_cohort
    ):
        raise ProductionRefreshError(
            "Stage 31 rank-table census differs from the reviewed production scope"
        )
    if (
        int(publisher.get("source_live_ticker_count", -1))
        != int(scope_contract["expected_remaining_current_ticker_count"])
        + int(scope_contract["excluded_ticker_count"])
        or publisher.get("observed_excluded_tickers")
        != scope_contract["excluded_tickers"]
        or int(publisher.get("observed_excluded_ticker_count", -1))
        != int(scope_contract["excluded_ticker_count"])
        or int(publisher.get("published_ticker_count", -1)) != len(rows)
        or str(publisher.get("published_tickers_sha256") or "")
        != _value_sha256(sorted(tickers))
        or str(publisher.get("published_tickers_sha256") or "")
        != str(scope_contract["expected_remaining_current_tickers_sha256"])
        or publisher.get("published_tickers_by_cohort") != published_by_cohort
    ):
        raise ProductionRefreshError(
            "Stage 31 publisher scope census does not tie to the rank table"
        )

    rank_ready_count = sum(_truthy(row["rank_ready_flag"]) for row in rows)
    oos_valid_count = sum(_truthy(row["oos_score_valid_flag"]) for row in rows)
    rank_ready_by_cohort = {
        cohort: sum(_truthy(row["rank_ready_flag"]) for row in values)
        for cohort, values in sorted(cohort_rows.items())
    }
    if (
        int(publisher.get("rank_row_count", -1)) != len(rows)
        or int(publisher.get("rank_ready_count", -1)) != rank_ready_count
        or int(publisher.get("oos_valid_count", -1)) != oos_valid_count
        or publisher.get("rank_ready_by_cohort") != rank_ready_by_cohort
    ):
        raise ProductionRefreshError("Stage 31 publisher row counts do not tie to the CSV")
    return {
        "rank_table": {
            "path": str(plan.rank_path),
            "sha256": _sha256(plan.rank_path),
            "rows": len(rows),
            "rank_ready_rows": rank_ready_count,
            "oos_valid_rows": oos_valid_count,
            "calibration_scope_sha256": scope_contract["payload_sha256"],
        },
        "publisher_manifest": {
            "path": str(plan.publisher_manifest_path),
            "sha256": _sha256(plan.publisher_manifest_path),
            "status": "PASS",
        },
    }


def _verify_terminal_identity(
    plan: RefreshPlan,
    payload: Mapping[str, Any],
    *,
    allowed_statuses: set[str],
) -> str:
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ProductionRefreshError("Existing terminal manifest schema drift")
    status = str(payload.get("status") or "").upper()
    if status not in allowed_statuses:
        raise ProductionRefreshError(
            f"Existing terminal manifest status {status!r} cannot be used"
        )
    if str(payload.get("asof_date") or "") != plan.allocation_asof_date:
        raise ProductionRefreshError("Existing terminal manifest allocation date drift")
    if str(payload.get("signal_asof_date") or "") != plan.signal_asof_date:
        raise ProductionRefreshError("Existing terminal manifest signal date drift")
    if str(payload.get("plan_sha256") or "") != plan.payload_sha256():
        raise ProductionRefreshError("Existing terminal manifest refresh-plan drift")
    if str(payload.get("database_path") or "") != str(plan.database_path):
        raise ProductionRefreshError("Existing terminal manifest database path drift")
    if str(payload.get("output_root") or "") != str(plan.output_root):
        raise ProductionRefreshError("Existing terminal manifest output-root drift")
    if str(payload.get("consumer_output_dir") or "") != str(plan.consumer_output_dir):
        raise ProductionRefreshError("Existing terminal manifest Consumer output-dir drift")
    if bool(payload.get("cache_only")) is not plan.cache_only:
        raise ProductionRefreshError("Existing terminal manifest cache policy drift")
    if bool(payload.get("force_refresh")) is not plan.force_refresh:
        raise ProductionRefreshError("Existing terminal manifest force-refresh policy drift")
    config = payload.get("config")
    if not isinstance(config, dict) or str(config.get("sha256") or "") != _sha256(
        plan.config_path
    ):
        raise ProductionRefreshError("Existing terminal manifest config hash drift")
    return status


def _verify_log_record(
    record: Any,
    *,
    expected_path: Path,
    label: str,
) -> None:
    if not isinstance(record, dict):
        raise ProductionRefreshError(f"{label} provenance is missing")
    declared = Path(str(record.get("path") or "")).resolve()
    if declared != expected_path.resolve():
        raise ProductionRefreshError(f"{label} path drift")
    if not declared.is_file():
        raise ProductionRefreshError(f"{label} is missing")
    if (
        str(record.get("sha256") or "") != _sha256(declared)
        or int(record.get("bytes", -1)) != declared.stat().st_size
    ):
        raise ProductionRefreshError(f"{label} hash or size drift")


def _verify_step_row(
    plan: RefreshPlan,
    observed: Any,
    *,
    sequence: int,
    require_status: str,
) -> None:
    expected = plan.steps[sequence - 1]
    if not isinstance(observed, dict) or (
        int(observed.get("sequence", -1)) != sequence
        or observed.get("name") != expected.name
        or observed.get("script") != str(expected.script)
        or observed.get("script_sha256") != _sha256(expected.script)
        or bool(observed.get("network")) is not expected.network
        or observed.get("argv") != list(expected.argv(plan.python_executable))
        or not str(observed.get("started_at_utc") or "")
        or not str(observed.get("completed_at_utc") or "")
    ):
        raise ProductionRefreshError(
            f"Existing terminal manifest step {sequence} identity drift"
        )
    return_code = int(observed.get("return_code", -1))
    status = str(observed.get("status") or "").upper()
    if require_status == "PASS" and (return_code != 0 or status != "PASS"):
        raise ProductionRefreshError(
            f"Existing terminal manifest step {sequence} is not PASS"
        )
    if require_status == "FAIL" and (return_code == 0 or status != "FAIL"):
        raise ProductionRefreshError(
            f"Existing terminal manifest step {sequence} is not FAIL"
        )
    log_dir = plan.terminal_manifest_path.parent / "logs"
    _verify_log_record(
        observed.get("stdout_log"),
        expected_path=log_dir / f"{sequence:02d}_{expected.name}.stdout.log",
        label=f"step {sequence} stdout log",
    )
    _verify_log_record(
        observed.get("stderr_log"),
        expected_path=log_dir / f"{sequence:02d}_{expected.name}.stderr.log",
        label=f"step {sequence} stderr log",
    )


def _archive_resume_source(
    plan: RefreshPlan,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    payload_hash = canonical_sha256(payload)
    path = plan.terminal_manifest_path.parent / "resume_history" / f"{payload_hash}.json"
    canonical = _canonical_json(payload)
    if path.is_file():
        if path.read_text(encoding="utf-8") != canonical:
            raise ProductionRefreshError("Resume-history artifact content drift")
    else:
        _write_json(path, payload)
    return {
        "path": str(path),
        "sha256": _sha256(path),
        "source_payload_sha256": payload_hash,
    }


def _resume_prefix(
    plan: RefreshPlan,
    payload: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], int, dict[str, Any]]:
    status = _verify_terminal_identity(
        plan,
        payload,
        allowed_statuses={"FAIL", "RUNNING"},
    )
    observed_steps = payload.get("steps")
    if not isinstance(observed_steps, list) or len(observed_steps) > len(plan.steps):
        raise ProductionRefreshError("Existing terminal manifest step census drift")
    preserved: list[dict[str, Any]] = []
    start_sequence = 1
    for sequence, observed in enumerate(observed_steps, start=1):
        is_last = sequence == len(observed_steps)
        observed_status = (
            str(observed.get("status") or "").upper()
            if isinstance(observed, dict)
            else ""
        )
        if status == "FAIL" and is_last and observed_status == "FAIL":
            _verify_step_row(
                plan,
                observed,
                sequence=sequence,
                require_status="FAIL",
            )
            start_sequence = sequence
            break
        _verify_step_row(
            plan,
            observed,
            sequence=sequence,
            require_status="PASS",
        )
        preserved.append(dict(observed))
        start_sequence = sequence + 1
    if status == "FAIL" and not str(payload.get("failure") or ""):
        raise ProductionRefreshError("Existing FAIL terminal has no failure reason")
    if status == "RUNNING" and payload.get("completed_at_utc") is not None:
        raise ProductionRefreshError("Existing RUNNING terminal has a completion time")
    return preserved, start_sequence, _archive_resume_source(plan, payload)


def _verify_existing_terminal(plan: RefreshPlan, payload: Mapping[str, Any]) -> None:
    _verify_terminal_identity(plan, payload, allowed_statuses={"PASS"})
    verified = verify_publisher_outputs(plan)
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, dict) or artifacts != verified:
        raise ProductionRefreshError("Existing terminal manifest artifact hashes drift")
    observed_steps = payload.get("steps")
    if not isinstance(observed_steps, list) or len(observed_steps) != len(plan.steps):
        raise ProductionRefreshError("Existing terminal manifest step census drift")
    for sequence, observed in enumerate(observed_steps, start=1):
        _verify_step_row(
            plan,
            observed,
            sequence=sequence,
            require_status="PASS",
        )


def _write_step_log(path: Path, value: str) -> dict[str, Any]:
    atomic_write_text(path, value, encoding="utf-8")
    return {"path": str(path), "sha256": _sha256(path), "bytes": path.stat().st_size}


def _invoke(
    executor: Executor,
    argv: Sequence[str],
    *,
    timeout_seconds: float,
) -> subprocess.CompletedProcess[str]:
    return executor(
        list(argv),
        cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHONUTF8": "1"},
        shell=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )


def run_refresh(
    plan: RefreshPlan,
    *,
    dry_run: bool = False,
    resume: bool = False,
    executor: Executor = subprocess.run,
    timeout_seconds: float = 3600.0,
) -> dict[str, Any]:
    """Execute a plan sequentially, stopping on the first failed contract."""

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if dry_run:
        return plan.to_dict()

    existing: dict[str, Any] | None = None
    if plan.terminal_manifest_path.is_file():
        existing = _strict_json_file(
            plan.terminal_manifest_path,
            label="Consumer refresh terminal manifest",
        )
        if isinstance(existing, dict) and str(existing.get("status") or "").upper() == "PASS":
            _verify_existing_terminal(plan, existing)
            return dict(existing)

    step_rows: list[dict[str, Any]] = []
    start_sequence = 1
    resume_source: dict[str, Any] | None = None
    if resume and existing is not None:
        step_rows, start_sequence, resume_source = _resume_prefix(plan, existing)

    started_at = _utc_now()
    config_sha256 = _sha256(plan.config_path)
    base_payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "RUNNING",
        "plan_sha256": plan.payload_sha256(),
        "asof_date": plan.allocation_asof_date,
        "signal_asof_date": plan.signal_asof_date,
        "started_at_utc": started_at,
        "completed_at_utc": None,
        "cache_only": plan.cache_only,
        "force_refresh": plan.force_refresh,
        "config": {"path": str(plan.config_path), "sha256": config_sha256},
        "database_path": str(plan.database_path),
        "output_root": str(plan.output_root),
        "consumer_output_dir": str(plan.consumer_output_dir),
        "resume": {
            "requested": bool(resume),
            "resumed": resume_source is not None,
            "start_sequence": start_sequence,
            "preserved_step_count": len(step_rows),
            "source": resume_source,
        },
        "steps": step_rows,
    }
    _write_json(plan.terminal_manifest_path, base_payload)
    log_dir = plan.terminal_manifest_path.parent / "logs"
    failure: str | None = None

    for sequence in range(start_sequence, len(plan.steps) + 1):
        step = plan.steps[sequence - 1]
        step_started = _utc_now()
        argv = step.argv(plan.python_executable)
        try:
            completed = _invoke(executor, argv, timeout_seconds=timeout_seconds)
            return_code = int(completed.returncode)
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
        except subprocess.TimeoutExpired as exc:
            return_code = 124
            stdout = str(exc.stdout or "")
            stderr = str(exc.stderr or "") + f"\nTimed out after {timeout_seconds} seconds.\n"
        except BaseException as exc:  # preserve a terminal FAIL artifact on executor faults
            return_code = 125
            stdout = ""
            stderr = f"{type(exc).__name__}: {exc}\n"

        stdout_log = _write_step_log(
            log_dir / f"{sequence:02d}_{step.name}.stdout.log", stdout
        )
        stderr_log = _write_step_log(
            log_dir / f"{sequence:02d}_{step.name}.stderr.log", stderr
        )
        row = {
            "sequence": sequence,
            "name": step.name,
            "script": str(step.script),
            "script_sha256": _sha256(step.script),
            "network": step.network,
            "argv": list(argv),
            "started_at_utc": step_started,
            "completed_at_utc": _utc_now(),
            "return_code": return_code,
            "status": "PASS" if return_code == 0 else "FAIL",
            "stdout_log": stdout_log,
            "stderr_log": stderr_log,
        }
        step_rows.append(row)
        _write_json(
            plan.terminal_manifest_path,
            {**base_payload, "steps": step_rows},
        )
        if return_code != 0:
            failure = f"{step.name} exited with return code {return_code}"
            break

    artifacts: dict[str, Any] | None = None
    if failure is None:
        try:
            if _sha256(plan.config_path) != config_sha256:
                raise ProductionRefreshError("Consumer config changed during the refresh")
            artifacts = verify_publisher_outputs(plan)
        except BaseException as exc:
            failure = f"{type(exc).__name__}: {exc}"

    terminal: dict[str, Any] = {
        **base_payload,
        "status": "PASS" if failure is None else "FAIL",
        "completed_at_utc": _utc_now(),
        "steps": step_rows,
        "artifacts": artifacts,
        "failure": failure,
    }
    _write_json(plan.terminal_manifest_path, terminal)
    return terminal


__all__ = [
    "DEFAULT_CONFIG",
    "ProductionRefreshError",
    "RefreshPlan",
    "RefreshStep",
    "build_refresh_plan",
    "run_refresh",
    "verify_publisher_outputs",
]
