"""Promotion-receipt creation and production binding for technology models."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from technology.core.calibration_governance import (
    MANUAL_PROMOTION_RECEIPT_SCHEMA_VERSION,
    build_promotion_receipt,
    canonical_sha256,
    read_json_object,
    sha256_file,
    validate_promotion_receipt,
)
from technology.core.config import cfg_get, resolve_path


SCORING_CONFIG_KEYS = {
    "semiconductors": "semiconductor_calibrated_scoring",
    "software_infrastructure": "software_infrastructure_calibrated_scoring",
    "technology_hardware": "technology_hardware_calibrated_scoring",
}


@dataclass(frozen=True)
class ProductionBinding:
    valid: bool
    status: str
    reasons: tuple[str, ...]
    receipt_path: str
    receipt_sha256: str


def production_weight_fingerprint(value: Mapping[str, Any]) -> str:
    return canonical_sha256(
        {
            "component_weights": value.get("component_weights") or {},
            "subfeature_weights": value.get("subfeature_weights") or {},
        }
    )


def _manifest_artifact_sha(manifest: Mapping[str, Any], name: str) -> str:
    for row in manifest.get("artifacts") or []:
        if isinstance(row, Mapping) and row.get("name") == name:
            return str(row.get("sha256") or "")
    return ""


def resolve_production_binding(
    config: dict[str, Any],
    *,
    config_path: Path,
    family: str,
    governance_config_key: str,
) -> ProductionBinding:
    family_cfg = cfg_get(config, f"oos_calibration_standards.families.{family}", {}) or {}
    production_status = str(cfg_get(config, f"{governance_config_key}.production_model_status", "stage7_active"))
    model_version = str(family_cfg.get("production_model_version") or "")
    effective_date = str(family_cfg.get("production_model_effective_date") or "")
    receipt_raw = str(cfg_get(config, f"{governance_config_key}.active_promotion_receipt_path", "") or "").strip()
    expected_receipt_hash = str(cfg_get(config, f"{governance_config_key}.active_promotion_receipt_sha256", "") or "").strip()
    legacy_version = str(cfg_get(config, f"{governance_config_key}.legacy_pre_receipt_model_version", "") or "").strip()

    if production_status != "stage8_active":
        return ProductionBinding(True, "static_production_model_not_stage8_promoted", (), "", "")
    if not receipt_raw:
        if model_version and model_version == legacy_version:
            return ProductionBinding(
                True,
                "legacy_pre_receipt_grandfathered",
                ("immutable_candidate_artifacts_not_available_for_legacy_promotion",),
                "",
                "",
            )
        return ProductionBinding(False, "missing_active_promotion_receipt", ("stage8_production_requires_receipt",), "", "")

    receipt_path = resolve_path(receipt_raw, base_dir=config_path.parent)
    receipt = read_json_object(receipt_path)
    reasons = validate_promotion_receipt(receipt, model_family=family)
    actual_hash = sha256_file(receipt_path)
    if not expected_receipt_hash or actual_hash != expected_receipt_hash:
        reasons.append("active_promotion_receipt_sha256_mismatch")
    if receipt.get("model_version") != model_version:
        reasons.append("promotion_receipt_model_version_mismatch")
    if receipt.get("effective_date") != effective_date:
        reasons.append("promotion_receipt_effective_date_mismatch")
    scoring = cfg_get(config, SCORING_CONFIG_KEYS[family], {}) or {}
    if receipt.get("production_weights_sha256") != production_weight_fingerprint(scoring):
        reasons.append("promotion_receipt_production_weights_mismatch")
    rollback_key = str(receipt.get("rollback_scoring_config_key") or "").strip()
    if rollback_key:
        rollback_scoring = cfg_get(config, rollback_key, {}) or {}
        if receipt.get("rollback_weights_sha256") != production_weight_fingerprint(rollback_scoring):
            reasons.append("promotion_receipt_rollback_weights_mismatch")
    return ProductionBinding(
        not reasons,
        "sealed_promotion_receipt_valid" if not reasons else "invalid_active_promotion_receipt",
        tuple(dict.fromkeys(reasons)),
        str(receipt_path),
        actual_hash,
    )


def create_promotion_receipt(
    *,
    family: str,
    model_version: str,
    effective_date: str,
    approved_by: str,
    approval_note: str,
    stage8_manifest_path: Path,
    walk_forward_manifest_path: Path,
    stage8_weights_path: Path,
    production_weights: Mapping[str, Any],
    output_path: Path,
) -> dict[str, Any]:
    if output_path.exists():
        raise RuntimeError(f"Promotion receipt is immutable and already exists: {output_path}")
    try:
        effective = datetime.strptime(effective_date, "%Y-%m-%d").date()
    except ValueError as exc:
        raise RuntimeError("effective_date must be YYYY-MM-DD") from exc
    if effective < date.today():
        raise RuntimeError("A new model version cannot be promoted retroactively; effective_date must be today or later.")
    stage8_manifest = read_json_object(stage8_manifest_path)
    walk_forward_manifest = read_json_object(walk_forward_manifest_path)
    stage8_weights = read_json_object(stage8_weights_path)
    if not stage8_manifest or not walk_forward_manifest or not stage8_weights:
        raise RuntimeError("Stage 8 and walk-forward sealed artifacts are required before promotion.")
    walk_forward_summary = read_json_object(walk_forward_manifest_path.parent / "walk_forward_summary.json")
    if int(walk_forward_summary.get("final_promotion_eligible") or 0) != 1:
        raise RuntimeError(
            "Candidate is not finally promotion-eligible: "
            + ";".join(str(value) for value in (walk_forward_summary.get("final_promotion_reasons") or []))
        )
    receipt = build_promotion_receipt(
        model_family=family,
        model_version=model_version,
        effective_date=effective_date,
        approved_by=approved_by,
        approval_note=approval_note,
        stage8_manifest=stage8_manifest,
        walk_forward_manifest=walk_forward_manifest,
        stage8_weights=stage8_weights,
    )
    receipt["production_weights_sha256"] = production_weight_fingerprint(production_weights)
    receipt["receipt_content_sha256"] = canonical_sha256(
        {key: value for key, value in receipt.items() if key != "receipt_content_sha256"}
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp = output_path.with_suffix(f"{output_path.suffix}.tmp")
    temp.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(output_path)
    return receipt


def create_manual_economic_override_receipt(
    *,
    family: str,
    model_version: str,
    effective_date: str,
    approved_by: str,
    approval_note: str,
    stage8_manifest_path: Path,
    walk_forward_manifest_path: Path,
    stage8_weights_path: Path,
    consolidated_decision_path: Path,
    production_weights: Mapping[str, Any],
    rollback_weights: Mapping[str, Any],
    rollback_scoring_config_key: str,
    probation_contract: Mapping[str, Any],
    output_path: Path,
) -> dict[str, Any]:
    """Seal an explicit human override without weakening statistical gates."""
    if family != "software_infrastructure":
        raise RuntimeError("Manual economic override is currently authorized only for software_infrastructure.")
    if output_path.exists():
        raise RuntimeError(f"Promotion receipt is immutable and already exists: {output_path}")
    try:
        effective = datetime.strptime(effective_date, "%Y-%m-%d").date()
    except ValueError as exc:
        raise RuntimeError("effective_date must be YYYY-MM-DD") from exc
    if effective < date.today():
        raise RuntimeError("A new model version cannot be promoted retroactively.")

    stage8_manifest = read_json_object(stage8_manifest_path)
    walk_forward_manifest = read_json_object(walk_forward_manifest_path)
    stage8_weights = read_json_object(stage8_weights_path)
    walk_forward_summary = read_json_object(walk_forward_manifest_path.parent / "walk_forward_summary.json")
    decision = read_json_object(consolidated_decision_path)
    if not all((stage8_manifest, walk_forward_manifest, stage8_weights, walk_forward_summary, decision)):
        raise RuntimeError("Sealed calibration, walk-forward, and consolidated decision artifacts are required.")
    if stage8_manifest.get("model_family") != family or stage8_manifest.get("stage") != "stage8":
        raise RuntimeError("Stage 8 manifest family/stage mismatch.")
    if walk_forward_manifest.get("model_family") != family or walk_forward_manifest.get("stage") != "walk_forward":
        raise RuntimeError("Walk-forward manifest family/stage mismatch.")
    if _manifest_artifact_sha(stage8_manifest, stage8_weights_path.name) != sha256_file(stage8_weights_path):
        raise RuntimeError("Stage 8 weights do not match the sealed run manifest.")
    if stage8_weights.get("calibration_run_id") != stage8_manifest.get("run_id"):
        raise RuntimeError("Stage 8 run id mismatch.")
    for key in ("config_sha256", "signal_panel_sha256"):
        values = {
            str(stage8_manifest.get(key) or ""),
            str(walk_forward_manifest.get(key) or ""),
            str(stage8_weights.get(key) or ""),
            str(walk_forward_summary.get(key) or ""),
        }
        if "" in values or len(values) != 1:
            raise RuntimeError(f"Calibration artifact {key} mismatch.")
    if walk_forward_summary.get("stage8_run_id") != stage8_manifest.get("run_id"):
        raise RuntimeError("Walk-forward summary is not linked to the sealed Stage 8 run.")
    if int(stage8_weights.get("stage8_gate_pass") or 0) == 1:
        raise RuntimeError("Use the standard promotion receipt path for a strict-gate passing candidate.")
    if decision.get("family") != family or int(decision.get("hard_safety_pass") or 0) != 1:
        raise RuntimeError("Consolidated economic decision did not pass hard safety gates.")
    decision_hash = str(decision.get("decision_content_sha256") or "")
    decision_content = {key: value for key, value in decision.items() if key != "decision_content_sha256"}
    if decision_hash != canonical_sha256(decision_content):
        raise RuntimeError("Consolidated economic decision content hash mismatch.")
    if str(probation_contract.get("effective_date") or "") != effective_date:
        raise RuntimeError("Probation effective_date must match the promotion effective_date.")
    if int(probation_contract.get("required_trading_sessions") or 0) != 21:
        raise RuntimeError("Manual software promotion requires exactly 21 trading sessions of probation.")
    if bool(probation_contract.get("automatic_reversion")):
        raise RuntimeError("Probation may recommend rollback but must not mutate production automatically.")

    receipt: dict[str, Any] = {
        "schema_version": MANUAL_PROMOTION_RECEIPT_SCHEMA_VERSION,
        "decision_type": "manual_economic_override",
        "model_family": family,
        "model_version": model_version,
        "effective_date": effective_date,
        "approved_by": approved_by,
        "approval_note": approval_note,
        "approved_at_utc": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "strict_gate_failure_acknowledged": 1,
        "strict_stage8_gate_pass": int(stage8_weights.get("stage8_gate_pass") or 0),
        "strict_walk_forward_eligible": int(walk_forward_summary.get("final_promotion_eligible") or 0),
        "strict_gate_reasons": list(stage8_weights.get("stage8_gate_reasons") or []),
        "walk_forward_reasons": list(walk_forward_summary.get("final_promotion_reasons") or []),
        "stage8_run_id": stage8_manifest.get("run_id", ""),
        "stage8_manifest_sha256": canonical_sha256(stage8_manifest),
        "walk_forward_run_id": walk_forward_manifest.get("run_id", ""),
        "walk_forward_manifest_sha256": canonical_sha256(walk_forward_manifest),
        "config_sha256": stage8_weights.get("config_sha256", ""),
        "signal_panel_sha256": stage8_weights.get("signal_panel_sha256", ""),
        "weights_sha256": canonical_sha256({
            "component_weights": stage8_weights.get("component_weights") or {},
            "subfeature_weights": stage8_weights.get("subfeature_weights") or {},
            "effective_subfeature_weights": stage8_weights.get("effective_subfeature_weights") or {},
        }),
        "production_weights_sha256": production_weight_fingerprint(production_weights),
        "rollback_weights_sha256": production_weight_fingerprint(rollback_weights),
        "rollback_scoring_config_key": rollback_scoring_config_key,
        "consolidated_decision_path": str(consolidated_decision_path.resolve()),
        "consolidated_decision_sha256": sha256_file(consolidated_decision_path),
        "consolidated_decision_content_sha256": decision_hash,
        "probation_contract": dict(probation_contract),
    }
    receipt["receipt_content_sha256"] = canonical_sha256(receipt)
    validation_errors = validate_promotion_receipt(receipt, model_family=family)
    if validation_errors:
        raise RuntimeError("Invalid manual promotion receipt: " + ";".join(validation_errors))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp = output_path.with_suffix(f"{output_path.suffix}.tmp")
    temp.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(output_path)
    return receipt
