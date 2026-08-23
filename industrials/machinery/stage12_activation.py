from __future__ import annotations

import json
import os
import tempfile
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping

from industrials.core.config import cfg_get, load_yaml, resolve_path
from industrials.machinery.scoring import (
    FINAL_RANK_FIELDS,
    file_sha256,
    read_rows,
    survivorship_sidecar,
    write_json_atomic,
    write_rank_rows,
)
from industrials.machinery.stage12_governance import (
    ACTIVATION_MODE_INITIAL,
    ACTIVATION_MODE_REPLACE_ACTIVE,
    MODEL_FAMILY,
    Stage12Paths,
    _portfolio_family,
    _validate_preview_rows,
    portfolio_activation_fingerprint,
    machinery_portfolio_policy_fingerprint,
    production_preview_rows,
    validate_stage12_lock,
)
from industrials.machinery.stage8_calibration import (
    parse_date,
    stage8_paths,
    utc_now,
)
from industrials.machinery.stage9_backtest import (
    stage9_paths,
    strategy_spec_by_name,
)
from portfolio_layer.scores.adapters import run_adapter
from portfolio_layer.scores.adapter_semantics import (
    industrial_adapter_semantic_sha256,
)


ACTIVATION_STATUS_PREPARED = "PREPARED_NOT_ACTIVATED"
ACTIVATION_STATUS_PUBLISHED = "ACTIVATED_ADAPTER_VALIDATED"
ACTIVATION_STATUS_ROLLED_BACK = "ROLLED_BACK_AFTER_SMOKE_FAILURE"
ACTIVATION_STATUS_FULLY_VALIDATED = "ACTIVATED_FULL_PORTFOLIO_VALIDATED"
PRODUCTION_POLICY_STATUS_ACTIVE = "ACTIVE"


def source_file_sha256(path: Path) -> str:
    """Hash source semantics without platform-specific newline noise."""
    payload = (
        path.read_bytes()
        .replace(bytes((13, 10)), bytes((10,)))
        .replace(bytes((13,)), bytes((10,)))
    )
    return sha256(payload).hexdigest()


def production_policy_source_paths() -> dict[str, Path]:
    package_root = Path(__file__).resolve().parent
    industrials_root = package_root.parent
    return {
        "stage12_activation.py": package_root / "stage12_activation.py",
        "stage12_activation_transaction.py": (
            package_root / "stage12_activation_transaction.py"
        ),
        "stage12_governance.py": package_root / "stage12_governance.py",
        "adapter_semantics.py": (
            package_root.parents[1] / "portfolio_layer" / "scores" / "adapter_semantics.py"
        ),
        "stage8_calibration.py": package_root / "stage8_calibration.py",
        "stage9_backtest.py": package_root / "stage9_backtest.py",
        "production_universe.py": package_root / "production_universe.py",
        "scoring.py": package_root / "scoring.py",
        "08_build_industrials_financial_features.py": (
            industrials_root / "scripts" / "08_build_industrials_financial_features.py"
        ),
        "financial_metric_contract.py": (
            industrials_root / "core" / "financial_metric_contract.py"
        ),
        "db.py": industrials_root / "core" / "db.py",
        "06a_build_machinery_scoring_features.py": (
            package_root / "scripts" / "06a_build_machinery_scoring_features.py"
        ),
        "10_build_machinery_calibrated_scores.py": (
            package_root / "scripts" / "10_build_machinery_calibrated_scores.py"
        ),
        "10b_publish_machinery_dashboard_reports.py": (
            package_root / "scripts" / "10b_publish_machinery_dashboard_reports.py"
        ),
        "10b_validate_machinery_dashboard_reports.py": (
            package_root / "scripts" / "10b_validate_machinery_dashboard_reports.py"
        ),
        "20_validate_machinery_portfolio_adapter.py": (
            package_root / "scripts" / "20_validate_machinery_portfolio_adapter.py"
        ),
    }


def production_policy_source_hashes() -> dict[str, str]:
    """Return canonical source hashes for newly written activation seals."""
    return {
        name: source_file_sha256(path)
        for name, path in production_policy_source_paths().items()
    }


def changed_production_policy_sources(expected: Mapping[str, object]) -> list[str]:
    """Compare a source seal while remaining compatible with legacy raw hashes.

    Activation states written before canonical newline hashing store the raw-byte
    digest. Accept that digest when the current bytes still match, or the
    canonical digest when only platform line endings changed. Any semantic byte
    change remains blocking.
    """
    paths = production_policy_source_paths()
    changed: list[str] = []
    for name in sorted(set(expected) | set(paths)):
        path = paths.get(name)
        if path is None or not path.is_file():
            changed.append(name)
            continue
        sealed = str(expected.get(name) or "")
        raw = path.read_bytes()
        normalized = raw.replace(bytes((13, 10)), bytes((10,))).replace(
            bytes((13,)), bytes((10,))
        )
        accepted = {
            sha256(raw).hexdigest(),
            sha256(normalized).hexdigest(),
            sha256(normalized.replace(bytes((10,)), bytes((13, 10)))).hexdigest(),
            sha256(normalized.replace(bytes((10,)), bytes((13,)))).hexdigest(),
        }
        if sealed not in accepted:
            changed.append(name)
    return changed


class ActivationPaths:
    def __init__(self, root: Path, asof: str) -> None:
        self.root = root / "activation_candidates" / asof
        self.rank_csv = self.root / "machinery_final_rank_table.csv"
        self.manifest_json = self.root / "machinery_activation_candidate.json"
        self.validation_json = self.root / "machinery_activation_candidate_validation.json"
        self.shadow_backup_csv = self.root / "machinery_final_rank_table_shadow_backup.csv"
        self.shadow_sidecar_backup_csv = (
            self.root
            / "machinery_stage11_survivorship_calibration_panel_shadow_backup.csv"
        )
        self.shadow_manifest_backup_json = self.root / "machinery_final_rank_table_shadow_manifest_backup.json"
        self.shadow_state_backup_json = (
            self.root / "machinery_dashboard_pre_activation_state.json"
        )
        self.activation_json = self.root / "machinery_activation_result.json"


def _write_bytes_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=str(path.parent),
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _capture_dashboard_state(
    paths: ActivationPaths,
    *,
    live_rank: Path,
    live_sidecar: Path,
    live_manifest: Path,
) -> dict[str, Any]:
    """Seal the exact pre-activation state, including absent files."""
    targets = {
        "rank": (live_rank, paths.shadow_backup_csv),
        "sidecar": (live_sidecar, paths.shadow_sidecar_backup_csv),
        "manifest": (live_manifest, paths.shadow_manifest_backup_json),
    }
    records: dict[str, dict[str, Any]] = {}
    for name, (target, backup) in targets.items():
        if target.exists() and not target.is_file():
            raise ValueError(f"Activation dashboard target is not a file: {target}")
        existed = target.is_file()
        payload = target.read_bytes() if existed else None
        if payload is None:
            backup.unlink(missing_ok=True)
        else:
            _write_bytes_atomic(backup, payload)
        records[name] = {
            "path": str(target.resolve()),
            "backup": str(backup.resolve()),
            "existed": existed,
            "sha256": sha256(payload).hexdigest() if payload is not None else "",
        }
    snapshot = {
        "artifact_family": "machinery_dashboard_pre_activation_state",
        "complete_dashboard": all(row["existed"] for row in records.values()),
        "files": records,
    }
    write_json_atomic(paths.shadow_state_backup_json, snapshot)
    return snapshot


def _restore_dashboard_state(
    paths: ActivationPaths,
    *,
    live_rank: Path,
    live_sidecar: Path,
    live_manifest: Path,
) -> dict[str, Any]:
    """Restore files to their exact pre-activation existence and bytes."""
    if not paths.shadow_state_backup_json.is_file():
        raise FileNotFoundError(paths.shadow_state_backup_json)
    snapshot = json.loads(
        paths.shadow_state_backup_json.read_text(encoding="utf-8")
    )
    records = snapshot.get("files")
    if not isinstance(records, dict):
        raise ValueError("Activation dashboard state has no file records")
    targets = {
        "rank": live_rank,
        "sidecar": live_sidecar,
        "manifest": live_manifest,
    }
    for name, target in targets.items():
        record = records.get(name)
        if not isinstance(record, dict):
            raise ValueError(f"Activation dashboard state is missing {name}")
        if Path(str(record.get("path") or "")).resolve() != target.resolve():
            raise ValueError(f"Activation dashboard state path changed for {name}")
        if bool(record.get("existed")):
            backup = Path(str(record.get("backup") or ""))
            if not backup.is_file():
                raise FileNotFoundError(backup)
            payload = backup.read_bytes()
            if sha256(payload).hexdigest() != str(record.get("sha256") or ""):
                raise ValueError(f"Activation dashboard backup hash changed for {name}")
            _write_bytes_atomic(target, payload)
        else:
            target.unlink(missing_ok=True)
    return snapshot


def _activation_date_checks(lock: Mapping[str, Any], asof: str) -> None:
    candidate_date = parse_date(
        str(lock.get("promotion_candidate_asof") or ""),
        field="promotion_candidate_asof",
    )
    production_start = parse_date(
        str(lock.get("production_start_date") or ""),
        field="production_start_date",
    )
    target = parse_date(asof, field="activation_asof")
    if target < candidate_date:
        raise ValueError("Activation cannot predate the governance candidate")
    if target < production_start:
        raise ValueError(f"Activation date {asof} predates production start {production_start.isoformat()}")


def _source_dashboard_paths(
    config: dict[str, Any],
    *,
    config_path: Path,
    asof: str,
) -> tuple[Path, Path]:
    dashboard_root = resolve_path(
        cfg_get(config, "machinery_scoring.dashboard_root"),
        base_dir=config_path.parent,
    )
    source_dir = dashboard_root / asof
    return (
        source_dir / "machinery_final_rank_table.csv",
        source_dir / "machinery_final_rank_table_manifest.json",
    )


def _lock_source_dashboard_paths(
    lock: Mapping[str, Any],
    config: dict[str, Any],
    *,
    config_path: Path,
    asof: str,
) -> tuple[Path, Path]:
    rank = str(lock.get("source_dashboard_rank") or "").strip()
    manifest = str(lock.get("source_dashboard_manifest") or "").strip()
    if bool(rank) != bool(manifest):
        raise ValueError("Governance lock source dashboard paths are incomplete")
    if rank:
        return Path(rank), Path(manifest)
    return _source_dashboard_paths(config, config_path=config_path, asof=asof)


def _active_cycle_root(
    state: Mapping[str, Any],
    *,
    default_root: Path,
) -> Path:
    configured = str(state.get("governance_root") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    lock_path = str(state.get("governance_lock") or "").strip()
    if lock_path:
        return Path(lock_path).expanduser().resolve().parent
    return default_root


def _verify_source_shadow(
    rank_path: Path,
    manifest_path: Path,
    *,
    asof: str,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    if not rank_path.exists() or not manifest_path.exists():
        raise FileNotFoundError(f"Activation source dashboard is incomplete for {asof}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("acceptance") != "PASS" or manifest.get("rank_table_sha256") != file_sha256(rank_path):
        raise ValueError("Activation source dashboard manifest is invalid")
    rows = read_rows(rank_path)
    if not rows:
        raise ValueError("Activation source dashboard is empty")
    if {row.get("asof_date", "") for row in rows} != {asof}:
        raise ValueError("Activation source dashboard as-of mismatch")
    if any(row.get("portfolio_candidate_gate") != "0" or row.get("oos_score_valid_flag") != "0" for row in rows):
        raise ValueError("Activation source must be an unpromoted shadow file")
    return rows, manifest


def _sealed_governance(
    config: dict[str, Any],
    *,
    config_path: Path,
    governance_root: Path,
) -> tuple[dict[str, Any], Stage12Paths]:
    validation = validate_stage12_lock(output_root=governance_root)
    if validation.get("acceptance") != "PASS":
        raise ValueError("Stage 12 governance validation failed: " + ";".join(validation.get("issues", [])))
    paths = Stage12Paths(governance_root)
    lock = json.loads(paths.lock_json.read_text(encoding="utf-8"))
    stage8_manifest_raw = str(
        lock.get("stage8_run_manifest") or ""
    ).strip()
    stage9_manifest_raw = str(
        lock.get("stage9_run_manifest") or ""
    ).strip()
    stage8_manifest = (
        Path(stage8_manifest_raw)
        if stage8_manifest_raw
        else stage8_paths(
            resolve_path(
                cfg_get(config, "machinery_stage8.output_root"),
                base_dir=config_path.parent,
            )
        ).run_manifest_json
    )
    stage9_manifest = (
        Path(stage9_manifest_raw)
        if stage9_manifest_raw
        else stage9_paths(
            resolve_path(
                cfg_get(config, "machinery_stage9.output_root"),
                base_dir=config_path.parent,
            )
        ).run_manifest_json
    )
    checks = (
        (
            stage8_manifest,
            "stage8_run_manifest_sha256",
        ),
        (
            stage9_manifest,
            "stage9_run_manifest_sha256",
        ),
        (
            config_path.parents[2] / "portfolio_layer" / "optimizer" / "optimizer_core.py",
            "portfolio_optimizer_sha256",
        ),
        (
            config_path.parents[2] / "portfolio_layer" / "optimizer" / "09_run_portfolio_optimizer.py",
            "portfolio_optimizer_runner_sha256",
        ),
    )
    for path, field in checks:
        if not path.exists() or file_sha256(path) != lock.get(field):
            raise ValueError(f"Sealed governance dependency changed: {path}")
    semantic_expected = str(
        lock.get("portfolio_adapter_semantic_sha256") or ""
    ).strip()
    adapter_path = (
        config_path.parents[2] / "portfolio_layer" / "scores" / "adapters.py"
    )
    if semantic_expected:
        if industrial_adapter_semantic_sha256() != semantic_expected:
            raise ValueError(
                "Sealed industrial adapter semantics changed: "
                f"{adapter_path}"
            )
    elif not adapter_path.exists() or file_sha256(adapter_path) != lock.get(
        "portfolio_adapter_sha256"
    ):
        # Legacy locks predate scoped semantic sealing. They remain fail-closed
        # until migrated through the explicit activation-contract rail.
        raise ValueError(f"Sealed governance dependency changed: {adapter_path}")
    return lock, paths


def _adapter_config(
    portfolio_config: Mapping[str, Any],
    *,
    rank_path: Path,
) -> dict[str, Any]:
    adapter_config = _portfolio_family(dict(portfolio_config))
    adapter_config["file_mode"] = "flat"
    adapter_config["file_path"] = str(rank_path)
    return adapter_config


def apply_active_production_policy(
    config: dict[str, Any],
    *,
    config_path: Path,
    governance_root: Path,
    asof: str,
    shadow_rows: list[dict[str, str]],
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Apply the sealed Stage 12 policy after a successful live activation."""
    state_paths = Stage12Paths(governance_root)
    if not state_paths.activation_state_json.exists():
        return shadow_rows, {
            "production_policy_active": False,
            "production_policy_status": "SHADOW_NOT_ACTIVATED",
            "activation_state": str(state_paths.activation_state_json),
        }
    state = json.loads(
        state_paths.activation_state_json.read_text(encoding="utf-8")
    )
    if state.get("acceptance") != "PASS" or state.get("production_policy_status") != PRODUCTION_POLICY_STATUS_ACTIVE:
        raise ValueError("Machinery production activation state is not active")
    activation_asof = parse_date(
        str(state.get("activation_asof") or ""),
        field="activation_asof",
    )
    target = parse_date(asof, field="asof")
    if target < activation_asof:
        return shadow_rows, {
            "production_policy_active": False,
            "production_policy_status": "SHADOW_BEFORE_ACTIVATION",
            "activation_asof": activation_asof.isoformat(),
            "activation_state": str(state_paths.activation_state_json),
            "activation_state_sha256": file_sha256(
                state_paths.activation_state_json
            ),
        }
    expected_source_hashes = state.get("production_source_sha256")
    expected = expected_source_hashes if isinstance(expected_source_hashes, Mapping) else {}
    changed = changed_production_policy_sources(expected)
    if changed:
        raise ValueError(
            "Machinery production policy source changed: " + ",".join(changed)
        )
    active_root = _active_cycle_root(state, default_root=governance_root)
    active_paths = Stage12Paths(active_root)
    lock, _ = _sealed_governance(
        config,
        config_path=config_path,
        governance_root=active_root,
    )
    _activation_date_checks(lock, activation_asof.isoformat())
    if file_sha256(active_paths.lock_json) != state.get(
        "governance_lock_sha256"
    ):
        raise ValueError("Machinery activation governance lock changed")
    activation_paths = ActivationPaths(
        active_root,
        activation_asof.isoformat(),
    )
    candidate_rank = activation_paths.rank_csv
    if (
        not candidate_rank.is_file()
        or file_sha256(candidate_rank) != state.get("candidate_rank_sha256")
        or Path(str(state.get("candidate_rank") or "")).resolve() != candidate_rank.resolve()
    ):
        raise ValueError("Machinery activation candidate changed")
    activation_result_path = activation_paths.activation_json
    if (
        not activation_result_path.is_file()
        or file_sha256(activation_result_path) != state.get("activation_result_sha256")
        or Path(str(state.get("activation_result") or "")).resolve() != activation_result_path.resolve()
    ):
        raise ValueError("Machinery activation result changed")
    activation_result = json.loads(activation_result_path.read_text(encoding="utf-8"))
    if (
        activation_result.get("acceptance") != "PASS"
        or activation_result.get("activation_status") != ACTIVATION_STATUS_FULLY_VALIDATED
        or activation_result.get("asof_date") != activation_asof.isoformat()
        or activation_result.get("full_portfolio_smoke_required") is not False
    ):
        raise ValueError("Machinery activation result is not fully validated")
    portfolio_config_path = resolve_path(
        cfg_get(config, "machinery_stage12.portfolio_config_path"),
        base_dir=config_path.parent,
    )
    portfolio_config = load_yaml(portfolio_config_path)
    if machinery_portfolio_policy_fingerprint(portfolio_config) != lock.get(
        "machinery_portfolio_policy_sha256"
    ):
        raise ValueError("Machinery portfolio policy changed after activation")
    family = _portfolio_family(portfolio_config)
    cap = float(
        cfg_get(
            portfolio_config,
            f"optimizer.sector_weight_caps.{MODEL_FAMILY}",
            -1.0,
        )
    )
    fixed_equal = {
        str(value)
        for value in cfg_get(
            portfolio_config,
            "optimizer.fixed_equal_weight_sleeves",
            [],
        )
    }
    if (
        family.get("required") is not True
        or cap != float(lock["proposed_portfolio_cap"])
        or MODEL_FAMILY not in fixed_equal
    ):
        raise ValueError("Machinery activation state conflicts with portfolio settings")
    selection_policy = lock.get("production_selection_policy")
    if not isinstance(selection_policy, Mapping):
        raise ValueError("Governance lock has no production selection policy")
    selection_spec = strategy_spec_by_name(
        config,
        str(selection_policy.get("variant") or ""),
    )
    rows = production_preview_rows(
        shadow_rows,
        weights={str(key): float(value) for key, value in lock["recommended_weights"].items()},
        asof=asof,
        lock_date=str(lock["lockbox_start_date"]),
        score_model_version=str(cfg_get(config, "machinery_stage12.score_model_version")),
        model_version=str(cfg_get(config, "machinery_stage12.model_version")),
        scoring_contract_version=str(
            cfg_get(
                config,
                "machinery_stage12.scoring_contract_version",
            )
        ),
        selection_spec=selection_spec,
        minimum_positions=int(selection_policy["minimum_positions"]),
        universe_policy=str(selection_policy["universe_policy"]),
    )
    issues = _validate_preview_rows(
        rows,
        asof=asof,
        selection_policy=selection_policy,
    )
    if issues:
        raise ValueError("Active machinery production policy failed: " + ";".join(issues))
    selected_count = sum(row["portfolio_sleeve_selected_flag"] == "1" for row in rows)
    broad_count = sum(row["portfolio_universe_eligible_flag"] == "1" for row in rows)
    return rows, {
        "production_policy_active": True,
        "production_policy_status": PRODUCTION_POLICY_STATUS_ACTIVE,
        "activation_asof": activation_asof.isoformat(),
        "activation_state": str(state_paths.activation_state_json),
        "activation_state_sha256": file_sha256(
            state_paths.activation_state_json
        ),
        "governance_root": str(active_root),
        "governance_lock_sha256": file_sha256(active_paths.lock_json),
        "production_selection_policy": dict(selection_policy),
        "broad_eligible_count": broad_count,
        "selected_sleeve_count": selected_count,
    }


def prepare_activation_candidate(
    config: dict[str, Any],
    *,
    config_path: Path,
    governance_root: Path,
    asof: str,
    force: bool,
) -> dict[str, Any]:
    lock, governance_paths = _sealed_governance(
        config,
        config_path=config_path,
        governance_root=governance_root,
    )
    _activation_date_checks(lock, asof)
    portfolio_config_path = resolve_path(
        cfg_get(config, "machinery_stage12.portfolio_config_path"),
        base_dir=config_path.parent,
    )
    portfolio_config = load_yaml(portfolio_config_path)
    family = _portfolio_family(portfolio_config)
    current_cap = float(
        cfg_get(
            portfolio_config,
            f"optimizer.sector_weight_caps.{MODEL_FAMILY}",
            -1.0,
        )
    )
    activation_mode = str(
        lock.get("activation_mode") or ACTIVATION_MODE_INITIAL
    )
    if activation_mode == ACTIVATION_MODE_INITIAL:
        settings_valid = (
            family.get("required") is False and current_cap == 0.0
        )
    elif activation_mode == ACTIVATION_MODE_REPLACE_ACTIVE:
        settings_valid = (
            family.get("required") is True
            and current_cap == float(lock["proposed_portfolio_cap"])
        )
        active_state_path = Path(
            str(lock.get("active_activation_state") or "")
        )
        if (
            not active_state_path.is_file()
            or file_sha256(active_state_path)
            != str(lock.get("previous_activation_state_sha256") or "")
        ):
            raise ValueError(
                "Current machinery activation state changed after the "
                "replacement governance cycle was sealed"
            )
    else:
        raise ValueError(f"Unknown machinery activation mode: {activation_mode}")
    if not settings_valid:
        raise ValueError(
            "Portfolio settings do not match the sealed machinery activation mode"
        )
    if portfolio_activation_fingerprint(portfolio_config) != lock.get("portfolio_non_activation_config_sha256"):
        raise ValueError("Portfolio configuration changed outside activation settings")
    source_rank, source_manifest = _lock_source_dashboard_paths(
        lock,
        config,
        config_path=config_path,
        asof=asof,
    )
    publish_rank, publish_manifest = _source_dashboard_paths(
        config,
        config_path=config_path,
        asof=asof,
    )
    publish_sidecar = publish_rank.with_name(
        "machinery_stage11_survivorship_calibration_panel.csv"
    )
    source_rows, _ = _verify_source_shadow(
        source_rank,
        source_manifest,
        asof=asof,
    )
    selection_policy = lock.get("production_selection_policy")
    if not isinstance(selection_policy, Mapping):
        raise ValueError("Governance lock has no production selection policy")
    selection_spec = strategy_spec_by_name(
        config,
        str(selection_policy.get("variant") or ""),
    )
    rows = production_preview_rows(
        source_rows,
        weights={str(key): float(value) for key, value in lock["recommended_weights"].items()},
        asof=asof,
        lock_date=str(lock["lockbox_start_date"]),
        score_model_version=str(cfg_get(config, "machinery_stage12.score_model_version")),
        model_version=str(cfg_get(config, "machinery_stage12.model_version")),
        scoring_contract_version=str(
            cfg_get(
                config,
                "machinery_stage12.scoring_contract_version",
            )
        ),
        selection_spec=selection_spec,
        minimum_positions=int(selection_policy["minimum_positions"]),
        universe_policy=str(selection_policy["universe_policy"]),
    )
    issues = _validate_preview_rows(
        rows,
        asof=asof,
        selection_policy=selection_policy,
    )
    if issues:
        raise ValueError("Activation candidate failed: " + ";".join(issues))
    paths = ActivationPaths(governance_root, asof)
    if paths.root.exists() and any(paths.root.iterdir()) and not force:
        raise FileExistsError(f"Activation candidate already exists: {paths.root}")
    paths.root.mkdir(parents=True, exist_ok=True)
    write_rank_rows(paths.rank_csv, rows)
    sector_output_root = resolve_path(
        cfg_get(portfolio_config, "score_contract.sector_output_root"),
        base_dir=portfolio_config_path.parent,
    )
    adapter_result = run_adapter(
        _adapter_config(portfolio_config, rank_path=paths.rank_csv),
        sector_output_root,
        asof,
    )
    selected = {row["ticker"] for row in rows if row["portfolio_sleeve_selected_flag"] == "1"}
    adapted = {row.ticker for row in adapter_result.rows if row.investable_eligible == 1}
    if adapted != selected:
        raise ValueError("Adapter changed activation candidate membership")
    manifest = {
        "acceptance": "PASS",
        "activation_status": ACTIVATION_STATUS_PREPARED,
        "created_at_utc": utc_now(),
        "asof_date": asof,
        "governance_lock": str(governance_paths.lock_json),
        "governance_lock_sha256": file_sha256(governance_paths.lock_json),
        "source_shadow_rank": str(source_rank),
        "source_shadow_rank_sha256": file_sha256(source_rank),
        "source_shadow_manifest": str(source_manifest),
        "source_shadow_manifest_sha256": file_sha256(source_manifest),
        "publish_rank": str(publish_rank),
        "publish_sidecar": str(publish_sidecar),
        "publish_manifest": str(publish_manifest),
        "candidate_rank": str(paths.rank_csv),
        "candidate_rank_sha256": file_sha256(paths.rank_csv),
        "row_count": len(rows),
        "broad_eligible_count": sum(row["portfolio_universe_eligible_flag"] == "1" for row in rows),
        "selected_sleeve_count": len(selected),
        "adapter_investable_count": len(adapted),
        "production_selection_policy": dict(selection_policy),
        "activation_mode": activation_mode,
        "portfolio_non_activation_config_sha256": (portfolio_activation_fingerprint(portfolio_config)),
        "required_activation_settings": {
            "score_contract.sectors.machinery.required": True,
            "optimizer.sector_weight_caps.machinery": float(lock["proposed_portfolio_cap"]),
            "optimizer.fixed_equal_weight_sleeves": MODEL_FAMILY,
        },
        "production_promotion_performed": False,
    }
    write_json_atomic(paths.manifest_json, manifest)
    return manifest


def validate_activation_candidate(
    config: dict[str, Any],
    *,
    config_path: Path,
    governance_root: Path,
    asof: str,
) -> dict[str, Any]:
    paths = ActivationPaths(governance_root, asof)
    issues: list[str] = []
    for path in (paths.rank_csv, paths.manifest_json):
        if not path.exists() or path.stat().st_size == 0:
            issues.append(f"missing activation candidate artifact {path}")
    if issues:
        result = {"acceptance": "FAIL", "issues": issues}
        write_json_atomic(paths.validation_json, result)
        return result
    manifest = json.loads(paths.manifest_json.read_text(encoding="utf-8"))
    rows = read_rows(paths.rank_csv)
    if file_sha256(paths.rank_csv) != manifest.get("candidate_rank_sha256"):
        issues.append("activation candidate rank hash mismatch")
    governance_paths = Stage12Paths(governance_root)
    if file_sha256(governance_paths.lock_json) != manifest.get("governance_lock_sha256"):
        issues.append("activation governance lock hash mismatch")
    selection_policy = manifest.get("production_selection_policy")
    if not isinstance(selection_policy, Mapping):
        issues.append("activation selection policy is missing")
        selection_policy = {}
    issues.extend(
        _validate_preview_rows(
            rows,
            asof=asof,
            selection_policy=selection_policy,
        )
    )
    source_rank = Path(str(manifest.get("source_shadow_rank") or ""))
    source_manifest = Path(str(manifest.get("source_shadow_manifest") or ""))
    if (
        not source_rank.exists()
        or file_sha256(source_rank) != manifest.get("source_shadow_rank_sha256")
        or not source_manifest.exists()
        or file_sha256(source_manifest) != manifest.get("source_shadow_manifest_sha256")
    ):
        issues.append("activation source shadow artifacts changed")
    portfolio_config_path = resolve_path(
        cfg_get(config, "machinery_stage12.portfolio_config_path"),
        base_dir=config_path.parent,
    )
    portfolio_config = load_yaml(portfolio_config_path)
    if portfolio_activation_fingerprint(portfolio_config) != manifest.get("portfolio_non_activation_config_sha256"):
        issues.append("portfolio configuration changed outside activation settings")
    sector_output_root = resolve_path(
        cfg_get(portfolio_config, "score_contract.sector_output_root"),
        base_dir=portfolio_config_path.parent,
    )
    try:
        adapter_result = run_adapter(
            _adapter_config(portfolio_config, rank_path=paths.rank_csv),
            sector_output_root,
            asof,
        )
        adapted = {row.ticker for row in adapter_result.rows if row.investable_eligible == 1}
        selected = {row["ticker"] for row in rows if row["portfolio_sleeve_selected_flag"] == "1"}
        if adapted != selected:
            issues.append("activation adapter membership mismatch")
    except (FileNotFoundError, ValueError) as exc:
        issues.append(f"activation adapter failed: {type(exc).__name__}: {exc}")
    result = {
        "acceptance": "PASS" if not issues else "FAIL",
        "activation_status": manifest.get("activation_status"),
        "asof_date": asof,
        "rows": len(rows),
        "selected_sleeve_count": manifest.get("selected_sleeve_count"),
        "production_promotion_performed": False,
        "issues": issues,
    }
    write_json_atomic(paths.validation_json, result)
    return result


def activate_candidate(
    config: dict[str, Any],
    *,
    config_path: Path,
    governance_root: Path,
    asof: str,
    approval_token: str,
) -> dict[str, Any]:
    configured_token = str(cfg_get(config, "machinery_stage12.activation_approval_token", ""))
    if not configured_token or approval_token != configured_token:
        raise PermissionError("Explicit machinery activation token is invalid")
    validation = validate_activation_candidate(
        config,
        config_path=config_path,
        governance_root=governance_root,
        asof=asof,
    )
    if validation.get("acceptance") != "PASS":
        raise ValueError("Activation candidate validation failed: " + ";".join(validation.get("issues", [])))
    paths = ActivationPaths(governance_root, asof)
    manifest = json.loads(paths.manifest_json.read_text(encoding="utf-8"))
    lock = json.loads(Stage12Paths(governance_root).lock_json.read_text(encoding="utf-8"))
    portfolio_config_path = resolve_path(
        cfg_get(config, "machinery_stage12.portfolio_config_path"),
        base_dir=config_path.parent,
    )
    portfolio_config = load_yaml(portfolio_config_path)
    family = _portfolio_family(portfolio_config)
    cap = float(
        cfg_get(
            portfolio_config,
            f"optimizer.sector_weight_caps.{MODEL_FAMILY}",
            -1.0,
        )
    )
    fixed_equal = {
        str(value)
        for value in cfg_get(
            portfolio_config,
            "optimizer.fixed_equal_weight_sleeves",
            [],
        )
    }
    if (
        family.get("required") is not True
        or cap != float(lock["proposed_portfolio_cap"])
        or MODEL_FAMILY not in fixed_equal
    ):
        raise ValueError(
            "Portfolio activation settings are not committed: machinery must "
            "be required, use the approved cap, and preserve equal weights"
        )
    live_rank = Path(
        str(manifest.get("publish_rank") or manifest["source_shadow_rank"])
    )
    live_manifest = Path(
        str(
            manifest.get("publish_manifest")
            or manifest["source_shadow_manifest"]
        )
    )
    live_sidecar = Path(
        str(
            manifest.get("publish_sidecar")
            or live_rank.with_name(
                "machinery_stage11_survivorship_calibration_panel.csv"
            )
        )
    )
    dashboard_state = _capture_dashboard_state(
        paths,
        live_rank=live_rank,
        live_sidecar=live_sidecar,
        live_manifest=live_manifest,
    )
    source_manifest = Path(str(manifest["source_shadow_manifest"]))
    manifest_template = (
        live_manifest.read_bytes()
        if bool(dashboard_state["complete_dashboard"])
        else source_manifest.read_bytes()
    )
    candidate_rows = read_rows(paths.rank_csv)
    sidecar_rows = survivorship_sidecar(candidate_rows)
    try:
        _write_bytes_atomic(live_rank, paths.rank_csv.read_bytes())
        write_rank_rows(live_sidecar, sidecar_rows)
        dashboard_manifest = json.loads(manifest_template.decode("utf-8"))
        dashboard_manifest.update(
            {
                "acceptance": "PASS",
                "asof_date": asof,
                "rank_table_sha256": file_sha256(live_rank),
                "row_count": len(candidate_rows),
                "rank_ready_count": sum(row["rank_ready_flag"] == "1" for row in candidate_rows),
                "portfolio_candidate_count": sum(row["portfolio_candidate_gate"] == "1" for row in candidate_rows),
                "selected_sleeve_count": sum(row["portfolio_sleeve_selected_flag"] == "1" for row in candidate_rows),
                "sidecar": str(live_sidecar),
                "sidecar_sha256": file_sha256(live_sidecar),
                "sidecar_calibration_eligible_count": sum(
                    row["stage11_calibration_input_eligible_flag"] == "1"
                    for row in sidecar_rows
                ),
                "contract_fields": FINAL_RANK_FIELDS,
                "scoring_contract_versions": sorted({row["scoring_contract_version"] for row in candidate_rows}),
                "sidecar_retained_shadow": True,
                "production_promoted": True,
                "production_promotion_status": ACTIVATION_STATUS_PUBLISHED,
                "production_policy_active": True,
                "activation_metadata": {
                    "activation_status": ACTIVATION_STATUS_PUBLISHED,
                    "activation_asof": asof,
                    "candidate_rank_sha256": file_sha256(paths.rank_csv),
                    "governance_lock_sha256": file_sha256(Stage12Paths(governance_root).lock_json),
                },
                "production_selection_policy": lock["production_selection_policy"],
                "governance_lock_sha256": file_sha256(Stage12Paths(governance_root).lock_json),
                "published_at_utc": utc_now(),
            }
        )
        write_json_atomic(live_manifest, dashboard_manifest)
        sector_output_root = resolve_path(
            cfg_get(portfolio_config, "score_contract.sector_output_root"),
            base_dir=portfolio_config_path.parent,
        )
        adapter_result = run_adapter(
            _portfolio_family(portfolio_config),
            sector_output_root,
            asof,
        )
        expected_count = int(manifest["selected_sleeve_count"])
        actual_count = sum(row.investable_eligible == 1 for row in adapter_result.rows)
        if actual_count != expected_count:
            raise ValueError(
                f"Published dashboard adapter count mismatch expected={expected_count} actual={actual_count}"
            )
    except BaseException:
        _restore_dashboard_state(
            paths,
            live_rank=live_rank,
            live_sidecar=live_sidecar,
            live_manifest=live_manifest,
        )
        raise
    result = {
        "acceptance": "PASS",
        "activation_status": ACTIVATION_STATUS_PUBLISHED,
        "activated_at_utc": utc_now(),
        "asof_date": asof,
        "rank_table": str(live_rank),
        "rank_table_sha256": file_sha256(live_rank),
        "rank_manifest": str(live_manifest),
        "rank_manifest_sha256": file_sha256(live_manifest),
        "selected_sleeve_count": int(manifest["selected_sleeve_count"]),
        "portfolio_cap": cap,
        "adapter_validation": "PASS",
        "full_portfolio_smoke_required": True,
        "rollback_rank": str(paths.shadow_backup_csv),
        "rollback_sidecar": str(paths.shadow_sidecar_backup_csv),
        "rollback_manifest": str(paths.shadow_manifest_backup_json),
        "rollback_state": str(paths.shadow_state_backup_json),
    }
    write_json_atomic(paths.activation_json, result)
    return result


def rollback_published_candidate(
    *,
    governance_root: Path,
    asof: str,
    reason: str,
) -> dict[str, Any]:
    """Restore the exact shadow dashboard after a failed post-publish smoke."""
    paths = ActivationPaths(governance_root, asof)
    required = (paths.manifest_json,)
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise FileNotFoundError("Activation rollback artifacts are incomplete: " + ", ".join(missing))
    manifest = json.loads(paths.manifest_json.read_text(encoding="utf-8"))
    live_rank = Path(
        str(
            manifest.get("publish_rank")
            or manifest.get("source_shadow_rank")
            or ""
        )
    )
    live_manifest = Path(
        str(
            manifest.get("publish_manifest")
            or manifest.get("source_shadow_manifest")
            or ""
        )
    )
    live_sidecar = Path(
        str(
            manifest.get("publish_sidecar")
            or live_rank.with_name(
                "machinery_stage11_survivorship_calibration_panel.csv"
            )
        )
    )
    if paths.shadow_state_backup_json.is_file():
        snapshot = _restore_dashboard_state(
            paths,
            live_rank=live_rank,
            live_sidecar=live_sidecar,
            live_manifest=live_manifest,
        )
        restore_sidecar = bool(snapshot["files"]["sidecar"]["existed"])
        complete_dashboard = bool(snapshot.get("complete_dashboard"))
    else:
        legacy_required = (
            paths.shadow_backup_csv,
            paths.shadow_manifest_backup_json,
        )
        legacy_missing = [str(path) for path in legacy_required if not path.exists()]
        if legacy_missing:
            raise FileNotFoundError(
                "Activation rollback artifacts are incomplete: "
                + ", ".join(legacy_missing)
            )
        restore_sidecar = paths.shadow_sidecar_backup_csv.is_file()
        _write_bytes_atomic(live_rank, paths.shadow_backup_csv.read_bytes())
        if restore_sidecar:
            _write_bytes_atomic(
                live_sidecar,
                paths.shadow_sidecar_backup_csv.read_bytes(),
            )
        _write_bytes_atomic(
            live_manifest,
            paths.shadow_manifest_backup_json.read_bytes(),
        )
        complete_dashboard = True
    if complete_dashboard:
        restored_manifest = json.loads(live_manifest.read_text(encoding="utf-8"))
        if (
            restored_manifest.get("acceptance") != "PASS"
            or restored_manifest.get("rank_table_sha256") != file_sha256(live_rank)
            or restored_manifest.get("asof_date") != asof
        ):
            raise ValueError("Restored dashboard failed manifest validation")
    result = {
        "acceptance": "PASS",
        "activation_status": ACTIVATION_STATUS_ROLLED_BACK,
        "rolled_back_at_utc": utc_now(),
        "asof_date": asof,
        "reason": reason,
        "rank_table": str(live_rank),
        "rank_table_sha256": file_sha256(live_rank) if live_rank.is_file() else "",
        "rank_manifest": str(live_manifest),
        "rank_manifest_sha256": (
            file_sha256(live_manifest) if live_manifest.is_file() else ""
        ),
        "sidecar": str(live_sidecar) if restore_sidecar else "",
        "sidecar_sha256": (
            file_sha256(live_sidecar) if restore_sidecar else ""
        ),
        "pre_activation_dashboard_complete": complete_dashboard,
        "production_promotion_performed": False,
        "full_portfolio_smoke_required": True,
    }
    write_json_atomic(paths.activation_json, result)
    return result
