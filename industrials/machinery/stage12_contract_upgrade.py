from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from industrials.core.config import cfg_get, load_yaml, resolve_path
from industrials.machinery.scoring import (
    FINAL_RANK_FIELDS,
    file_sha256,
    parse_asof,
    read_rows,
    write_json_atomic,
)
from industrials.machinery.stage12_activation import (
    ACTIVATION_STATUS_FULLY_VALIDATED,
    PRODUCTION_POLICY_STATUS_ACTIVE,
    ActivationPaths,
    _active_cycle_root,
    _write_bytes_atomic,
    apply_active_production_policy,
    production_policy_source_hashes,
)
from industrials.machinery.stage12_governance import (
    MODEL_FAMILY,
    Stage12Paths,
    _portfolio_family,
    machinery_portfolio_policy_fingerprint,
)
from industrials.machinery.stage8_calibration import utc_now


PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parents[1]


def _require_hash(path: Path, expected: object, *, label: str) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"{label} is missing: {path}")
    actual = file_sha256(path)
    if actual != str(expected or ""):
        raise ValueError(f"{label} hash mismatch: expected={expected!r} actual={actual}")


def validate_active_portfolio_contract(
    portfolio_config: dict[str, Any],
    *,
    expected_cap: float,
    expected_policy_sha256: str,
) -> None:
    """Validate machinery activation invariants without freezing other sleeves."""
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
        family.get("enabled") is not True
        or family.get("required") is not True
        or family.get("require_oos_score_valid") is not True
        or cap != expected_cap
        or MODEL_FAMILY not in fixed_equal
        or machinery_portfolio_policy_fingerprint(portfolio_config)
        != expected_policy_sha256
    ):
        raise ValueError(
            "Current portfolio configuration no longer satisfies the sealed "
            "machinery activation contract"
        )


def _run_validator(command: list[str], *, label: str) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    output = completed.stdout.strip()
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} returned invalid JSON: {output or completed.stderr}") from exc
    if completed.returncode != 0 or payload.get("acceptance") != "PASS":
        raise ValueError(f"{label} failed: {payload}")
    return payload


def _validate_live_outputs(
    *,
    config_path: Path,
    asof: str,
) -> dict[str, dict[str, Any]]:
    dashboard = _run_validator(
        [
            sys.executable,
            str(PACKAGE_ROOT / "scripts" / "10b_validate_machinery_dashboard_reports.py"),
            "--config",
            str(config_path),
            "--asof",
            asof,
        ],
        label="machinery dashboard validation",
    )
    adapter = _run_validator(
        [
            sys.executable,
            str(PACKAGE_ROOT / "scripts" / "20_validate_machinery_portfolio_adapter.py"),
            "--config",
            str(config_path),
            "--asof",
            asof,
            "--sector-output-root",
            str(PROJECT_ROOT / "output"),
            "--expect-production",
        ],
        label="machinery portfolio adapter validation",
    )
    return {"dashboard": dashboard, "adapter": adapter}


def upgrade_active_contract(
    config: dict[str, Any],
    *,
    config_path: Path,
    governance_root: Path,
    asof: str,
) -> dict[str, Any]:
    """Upgrade only activation metadata after verifying the sealed 7/24 run."""
    asof = parse_asof(asof)
    state_store_paths = Stage12Paths(governance_root)
    state_path = state_store_paths.activation_state_json
    state = json.loads(state_path.read_text(encoding="utf-8"))
    active_root = _active_cycle_root(state, default_root=governance_root)
    stage12_paths = Stage12Paths(active_root)
    activation_paths = ActivationPaths(active_root, asof)
    result_path = activation_paths.activation_json
    candidate_path = activation_paths.rank_csv
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if (
        state.get("acceptance") != "PASS"
        or state.get("production_policy_status") != PRODUCTION_POLICY_STATUS_ACTIVE
        or state.get("activation_asof") != asof
    ):
        raise ValueError("Machinery activation state is not active for this date")
    if (
        result.get("acceptance") != "PASS"
        or result.get("activation_status") != ACTIVATION_STATUS_FULLY_VALIDATED
        or result.get("asof_date") != asof
        or result.get("full_portfolio_smoke_required") is not False
    ):
        raise ValueError("Machinery activation result is not fully validated")

    current_source_hashes = production_policy_source_hashes()
    previous_source_hashes = state.get("production_source_sha256")
    if not isinstance(previous_source_hashes, dict):
        raise ValueError("Machinery activation state has no source seal")
    semantic_source_keys = {
        "scoring.py",
        "08_build_industrials_financial_features.py",
        "financial_metric_contract.py",
        "06a_build_machinery_scoring_features.py",
        "stage8_calibration.py",
        "stage9_backtest.py",
        "production_universe.py",
        "stage12_governance.py",
    }
    changed_semantic_sources = sorted(
        key
        for key in semantic_source_keys
        if previous_source_hashes.get(key) != current_source_hashes.get(key)
    )
    if changed_semantic_sources:
        raise ValueError(
            "Scoring or selection semantics changed; run a new Stage 8/9/12 "
            "calibration and activation: " + ",".join(changed_semantic_sources)
        )

    _require_hash(
        stage12_paths.lock_json,
        state.get("governance_lock_sha256"),
        label="governance lock",
    )
    _require_hash(
        candidate_path,
        state.get("candidate_rank_sha256"),
        label="activation candidate",
    )
    _require_hash(
        result_path,
        state.get("activation_result_sha256"),
        label="activation result",
    )
    portfolio_config_path = resolve_path(
        cfg_get(config, "machinery_stage12.portfolio_config_path"),
        base_dir=config_path.parent,
    )
    portfolio_config = load_yaml(portfolio_config_path)
    governance_lock = json.loads(
        stage12_paths.lock_json.read_text(encoding="utf-8")
    )
    validate_active_portfolio_contract(
        portfolio_config,
        expected_cap=float(governance_lock["proposed_portfolio_cap"]),
        expected_policy_sha256=str(
            governance_lock["machinery_portfolio_policy_sha256"]
        ),
    )
    current_portfolio_config_sha256 = file_sha256(portfolio_config_path)
    # The portfolio run directory is shared and may be rebuilt after
    # activation. Its original hashes remain transitively sealed inside the
    # unchanged activation result verified above; do not compare those mutable
    # paths to later rerun content during a source-contract upgrade.

    live_rank = Path(str(result.get("rank_table") or ""))
    live_manifest = Path(str(result.get("rank_manifest") or ""))
    _require_hash(
        live_rank,
        result.get("rank_table_sha256"),
        label="live machinery rank table",
    )
    if file_sha256(live_rank) != file_sha256(candidate_path):
        raise ValueError("Live machinery rank table differs from sealed candidate")
    _require_hash(
        live_manifest,
        result.get("rank_manifest_sha256"),
        label="live machinery rank manifest",
    )
    sidecar_path = live_manifest.with_name("machinery_stage11_survivorship_calibration_panel.csv")
    manifest = json.loads(live_manifest.read_text(encoding="utf-8"))
    _require_hash(
        sidecar_path,
        manifest.get("sidecar_sha256"),
        label="shadow calibration sidecar",
    )

    candidate_rows = read_rows(candidate_path)
    sidecar_rows = read_rows(sidecar_path)
    if sorted(row["ticker"] for row in candidate_rows) != sorted(row["ticker"] for row in sidecar_rows):
        raise ValueError("Production and calibration ticker universes differ")

    backup_root = governance_root / "activation_contract_upgrades" / asof
    backup_root.mkdir(parents=True, exist_ok=True)
    backups = {
        live_manifest: backup_root / "rank_manifest_before_upgrade.json",
        result_path: backup_root / "activation_result_before_upgrade.json",
        state_path: backup_root / "activation_state_before_upgrade.json",
    }
    originals = {path: path.read_bytes() for path in backups}
    for source, backup in backups.items():
        if not backup.exists():
            _write_bytes_atomic(backup, originals[source])

    upgraded_at = utc_now()
    try:
        activation_metadata = dict(manifest.get("activation_metadata") or {})
        activation_metadata.update(
            {
                "activation_status": ACTIVATION_STATUS_FULLY_VALIDATED,
                "activation_asof": asof,
                "contract_upgraded_at_utc": upgraded_at,
            }
        )
        manifest.update(
            {
                "acceptance": "PASS",
                "asof_date": asof,
                "row_count": len(candidate_rows),
                "rank_ready_count": sum(row.get("rank_ready_flag") == "1" for row in candidate_rows),
                "portfolio_candidate_count": sum(row.get("portfolio_candidate_gate") == "1" for row in candidate_rows),
                "selected_sleeve_count": sum(
                    row.get("portfolio_sleeve_selected_flag") == "1" for row in candidate_rows
                ),
                "sidecar_calibration_eligible_count": sum(
                    row.get("stage11_calibration_input_eligible_flag") == "1" for row in sidecar_rows
                ),
                "contract_fields": FINAL_RANK_FIELDS,
                "scoring_contract_versions": sorted(
                    {row.get("scoring_contract_version", "") for row in candidate_rows}
                ),
                "rank_table_sha256": file_sha256(live_rank),
                "sidecar_sha256": file_sha256(sidecar_path),
                "production_promoted": True,
                "production_policy_active": True,
                "production_promotion_status": (ACTIVATION_STATUS_FULLY_VALIDATED),
                "sidecar_retained_shadow": True,
                "activation_metadata": activation_metadata,
            }
        )
        write_json_atomic(live_manifest, manifest)
        validations = _validate_live_outputs(
            config_path=config_path,
            asof=asof,
        )

        result["rank_manifest_sha256"] = file_sha256(live_manifest)
        result["contract_upgrade"] = {
            "acceptance": "PASS",
            "upgraded_at_utc": upgraded_at,
            "dashboard_validation": validations["dashboard"]["acceptance"],
            "adapter_validation": validations["adapter"]["acceptance"],
        }
        write_json_atomic(result_path, result)

        history = list(state.get("source_upgrade_history") or [])
        history.append(
            {
                "upgraded_at_utc": upgraded_at,
                "reason": "production_dashboard_sidecar_contract_alignment",
                "previous_activation_result_sha256": (file_sha256(backups[result_path])),
            }
        )
        state.update(
            {
                "activation_result_sha256": file_sha256(result_path),
                "portfolio_config_sha256_at_activation": (
                    current_portfolio_config_sha256
                ),
                "production_source_sha256": current_source_hashes,
                "contract_upgraded_at_utc": upgraded_at,
                "source_upgrade_history": history,
            }
        )
        write_json_atomic(state_path, state)

        regenerated_rows, policy_metadata = apply_active_production_policy(
            config,
            config_path=config_path,
            governance_root=governance_root,
            asof=asof,
            shadow_rows=sidecar_rows,
        )
        if regenerated_rows != candidate_rows:
            raise ValueError("Resealed production policy does not reproduce the candidate")
    except BaseException:
        for path, payload in originals.items():
            _write_bytes_atomic(path, payload)
        raise

    report = {
        "acceptance": "PASS",
        "asof_date": asof,
        "upgraded_at_utc": upgraded_at,
        "historical_rebuild_performed": False,
        "portfolio_rerun_performed": False,
        "row_count": len(candidate_rows),
        "selected_sleeve_count": sum(row.get("portfolio_sleeve_selected_flag") == "1" for row in candidate_rows),
        "rank_manifest_sha256": file_sha256(live_manifest),
        "activation_result_sha256": file_sha256(result_path),
        "activation_state_sha256": file_sha256(state_path),
        "production_policy_status": policy_metadata["production_policy_status"],
        "validations": validations,
        "backup_root": str(backup_root),
    }
    write_json_atomic(
        backup_root / "machinery_activation_contract_upgrade.json",
        report,
    )
    return report
