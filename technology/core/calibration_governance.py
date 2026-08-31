"""Fail-closed provenance and promotion helpers for technology calibration.

Calibration runners keep compatibility copies at their historical output paths,
but every run is also copied into an immutable ``runs/<run_id>`` directory.  A
validator accepts the compatibility files only when their hashes match both the
root manifest and the immutable copy.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


RUN_MANIFEST_SCHEMA_VERSION = "technology_calibration_run_manifest_v1"
PROMOTION_RECEIPT_SCHEMA_VERSION = "technology_promotion_receipt_v1"
MANUAL_PROMOTION_RECEIPT_SCHEMA_VERSION = "technology_manual_economic_override_receipt_v1"


@dataclass(frozen=True)
class GateDecision:
    passed: bool
    reasons: tuple[str, ...]


def sha256_file(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def weight_fingerprint(weights: Mapping[str, Any]) -> str:
    """Fingerprint only the complete component/subfeature weight contract."""
    return canonical_sha256(
        {
            "component_weights": weights.get("component_weights") or {},
            "subfeature_weights": weights.get("subfeature_weights") or {},
            "effective_subfeature_weights": weights.get("effective_subfeature_weights") or {},
        }
    )


def new_run_id(model_family: str, stage: str, *, config_sha256: str, panel_sha256: str) -> str:
    # Keep immutable directory names short enough for Windows paths under the
    # repository's already-deep OneDrive output tree. The timestamp plus both
    # content hashes retain traceability and practical uniqueness.
    family_aliases = {
        "semiconductors": "semi",
        "technology_hardware": "hw",
        "software_infrastructure": "sw",
    }
    stage_aliases = {"stage8": "s8", "walk_forward": "wf"}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    family = family_aliases.get(model_family, model_family)
    stage_name = stage_aliases.get(stage, stage)
    family = "".join(char if char.isalnum() or char in "-_" else "_" for char in family)[:16]
    stage_name = "".join(char if char.isalnum() or char in "-_" else "_" for char in stage_name)[:12]
    return f"{family}_{stage_name}_{stamp}_{config_sha256[:8]}_{panel_sha256[:8]}"


def incumbent_relative_cohort_cap(
    configured_cap: float,
    incumbent_share: float,
    tolerance: float,
) -> float:
    """Return a bounded cap that is feasible relative to the PIT incumbent."""
    for name, value in (
        ("configured_cap", configured_cap),
        ("incumbent_share", incumbent_share),
        ("tolerance", tolerance),
    ):
        if not math.isfinite(value):
            raise ValueError(f"{name} must be finite")
    if not 0.0 <= configured_cap <= 1.0:
        raise ValueError("configured_cap must be between 0 and 1")
    if not 0.0 <= incumbent_share <= 1.0:
        raise ValueError("incumbent_share must be between 0 and 1")
    if tolerance < 0.0:
        raise ValueError("tolerance must be non-negative")
    return min(1.0, max(configured_cap, incumbent_share + tolerance))


def stamp_rows(rows: Sequence[Mapping[str, Any]], **fields: Any) -> list[dict[str, Any]]:
    return [{**dict(row), **fields} for row in rows]


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # The destination filename is already represented by path. Repeating it
    # in the temporary name needlessly consumes Windows' path budget.
    fd, temp_name = tempfile.mkstemp(prefix=".tmp.", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, default=str)
            handle.write("\n")
        os.replace(temp_name, path)
    finally:
        try:
            Path(temp_name).unlink(missing_ok=True)
        except OSError:
            pass


def _csv_shape(path: Path) -> tuple[int, list[str]]:
    if path.suffix.lower() != ".csv" or not path.exists() or path.stat().st_size == 0:
        return 0, []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return sum(1 for _ in reader), list(reader.fieldnames or [])


def seal_calibration_run(
    *,
    output_dir: Path,
    manifest_filename: str,
    run_id: str,
    model_family: str,
    stage: str,
    config_path: Path,
    panel_path: Path,
    artifact_names: Sequence[str],
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Seal current outputs and copy them into a write-once run directory."""
    output_dir = output_dir.resolve()
    immutable_dir = output_dir / "runs" / run_id
    if immutable_dir.exists():
        raise RuntimeError(f"Calibration run_id already exists and is immutable: {immutable_dir}")

    records: list[dict[str, Any]] = []
    for name in artifact_names:
        source = output_dir / name
        if not source.exists() or not source.is_file() or source.stat().st_size == 0:
            raise RuntimeError(f"Cannot seal missing or empty calibration artifact: {source}")
        row_count, columns = _csv_shape(source)
        records.append(
            {
                "name": name,
                "sha256": sha256_file(source),
                "size_bytes": source.stat().st_size,
                "row_count": row_count if source.suffix.lower() == ".csv" else "",
                "columns": columns if source.suffix.lower() == ".csv" else [],
            }
        )

    manifest: dict[str, Any] = {
        "schema_version": RUN_MANIFEST_SCHEMA_VERSION,
        "run_id": run_id,
        "model_family": model_family,
        "stage": stage,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "config_path": str(config_path.resolve()),
        "config_sha256": sha256_file(config_path),
        "signal_panel_path": str(panel_path.resolve()),
        "signal_panel_sha256": sha256_file(panel_path),
        "immutable_run_dir": str(immutable_dir.resolve()),
        "artifacts": records,
        "metadata": dict(metadata or {}),
    }
    manifest["manifest_content_sha256"] = canonical_sha256(manifest)

    immutable_dir.mkdir(parents=True, exist_ok=False)
    try:
        for record in records:
            shutil.copy2(output_dir / str(record["name"]), immutable_dir / str(record["name"]))
        _atomic_write_json(immutable_dir / manifest_filename, manifest)
        _atomic_write_json(output_dir / manifest_filename, manifest)
    except Exception:
        shutil.rmtree(immutable_dir, ignore_errors=True)
        raise
    return manifest


def read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def validate_calibration_run_manifest(
    manifest_path: Path,
    *,
    expected_model_family: str,
    expected_stage: str,
    current_config_path: Path,
    current_panel_path: Path,
) -> list[str]:
    """Validate freshness plus root/immutable artifact identity."""
    errors: list[str] = []
    manifest = read_json_object(manifest_path)
    if not manifest:
        return [f"Missing or invalid calibration run manifest: {manifest_path}"]
    if manifest.get("schema_version") != RUN_MANIFEST_SCHEMA_VERSION:
        errors.append(f"Unsupported run manifest schema: {manifest.get('schema_version')}")
    if manifest.get("model_family") != expected_model_family:
        errors.append(f"Run manifest model_family mismatch: {manifest.get('model_family')}")
    if manifest.get("stage") != expected_stage:
        errors.append(f"Run manifest stage mismatch: {manifest.get('stage')}")
    run_id = str(manifest.get("run_id") or "")
    if not run_id:
        errors.append("Run manifest is missing run_id.")
    config_hash = sha256_file(current_config_path)
    panel_hash = sha256_file(current_panel_path)
    if manifest.get("config_sha256") != config_hash:
        errors.append("Calibration output is stale: config_sha256 does not match the current config.")
    if manifest.get("signal_panel_sha256") != panel_hash:
        errors.append("Calibration output is stale: signal_panel_sha256 does not match the current panel.")
    expected_content_hash = str(manifest.get("manifest_content_sha256") or "")
    content = {key: value for key, value in manifest.items() if key != "manifest_content_sha256"}
    if expected_content_hash != canonical_sha256(content):
        errors.append("Calibration run manifest content hash mismatch.")

    root_dir = manifest_path.parent
    immutable_dir = Path(str(manifest.get("immutable_run_dir") or ""))
    if not immutable_dir.is_absolute():
        immutable_dir = root_dir / immutable_dir
    immutable_manifest = immutable_dir / manifest_path.name
    if not immutable_manifest.exists() or sha256_file(immutable_manifest) != sha256_file(manifest_path):
        errors.append("Immutable run manifest is missing or differs from the compatibility manifest.")
    for record in manifest.get("artifacts") or []:
        if not isinstance(record, dict):
            errors.append("Run manifest contains a malformed artifact record.")
            continue
        name = str(record.get("name") or "")
        expected_hash = str(record.get("sha256") or "")
        if not name or not expected_hash:
            errors.append("Run manifest artifact is missing name or sha256.")
            continue
        root_hash = sha256_file(root_dir / name)
        immutable_hash = sha256_file(immutable_dir / name)
        if root_hash != expected_hash:
            errors.append(f"Compatibility artifact hash mismatch: {name}")
        if immutable_hash != expected_hash:
            errors.append(f"Immutable artifact hash mismatch: {name}")
    return errors


def stage8_gate_decision(
    *,
    candidate: Mapping[str, Any],
    baseline: Mapping[str, Any],
    primary_horizon: int,
    secondary_horizon: int,
    min_objective_improvement: float,
    min_ic_primary: float,
    min_ic_secondary: float,
    min_newey_west_t_primary: float,
    min_newey_west_t_secondary: float,
    min_hit_rate: float,
    min_spread_primary: float,
    min_spread_secondary: float,
    max_turnover: float,
    max_cohort_share: float,
    fold_win_fraction: float,
    min_fold_win_fraction: float,
    post_lock_data_included: bool,
) -> GateDecision:
    reasons: list[str] = []

    def value(source: Mapping[str, Any], key: str, default: float = 0.0) -> float:
        try:
            return float(source.get(key, default) or 0.0)
        except (TypeError, ValueError):
            return default

    if post_lock_data_included:
        reasons.append("post_lock_research_override")
    if value(candidate, "objective") < value(baseline, "objective") + min_objective_improvement:
        reasons.append("objective_improvement_below_minimum")
    if value(candidate, f"mean_ic_{primary_horizon}") < min_ic_primary:
        reasons.append(f"mean_ic_{primary_horizon}_below_minimum")
    if value(candidate, f"mean_ic_{secondary_horizon}") < min_ic_secondary:
        reasons.append(f"mean_ic_{secondary_horizon}_below_minimum")
    if value(candidate, f"newey_west_t_stat_{primary_horizon}") < min_newey_west_t_primary:
        reasons.append(f"newey_west_t_stat_{primary_horizon}_below_minimum")
    if value(candidate, f"newey_west_t_stat_{secondary_horizon}") < min_newey_west_t_secondary:
        reasons.append(f"newey_west_t_stat_{secondary_horizon}_below_minimum")
    if value(candidate, f"hit_rate_{primary_horizon}") < min_hit_rate:
        reasons.append(f"hit_rate_{primary_horizon}_below_minimum")
    if value(candidate, f"mean_spread_net_{primary_horizon}") < min_spread_primary:
        reasons.append(f"mean_spread_net_{primary_horizon}_below_minimum")
    if value(candidate, f"mean_spread_net_{secondary_horizon}") < min_spread_secondary:
        reasons.append(f"mean_spread_net_{secondary_horizon}_below_minimum")
    if value(candidate, "avg_top_turnover", 1.0) > max_turnover:
        reasons.append("turnover_above_maximum")
    if value(candidate, "avg_top_cohort_share", 1.0) > max_cohort_share:
        reasons.append("cohort_share_above_maximum")
    if fold_win_fraction < min_fold_win_fraction:
        reasons.append("untouched_holdout_fold_win_fraction_below_minimum")
    return GateDecision(passed=not reasons, reasons=tuple(reasons))


def walk_forward_gate_decision(
    summary: Mapping[str, Any],
    *,
    min_win_rate: float,
    min_gate_pass_rate: float,
    min_constraint_pass_rate: float,
    min_paired_t: float,
    min_mean_improvement: float = 0.0,
) -> GateDecision:
    reasons: list[str] = []

    def value(key: str) -> float:
        try:
            return float(summary.get(key) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    if bool(summary.get("post_lock_data_included")):
        reasons.append("post_lock_research_override")
    if value("refit_win_rate") < min_win_rate:
        reasons.append("refit_win_rate_below_minimum")
    if value("promotion_gate_pass_rate") < min_gate_pass_rate:
        reasons.append("promotion_gate_pass_rate_below_minimum")
    if value("constraint_pass_rate") < min_constraint_pass_rate:
        reasons.append("constraint_pass_rate_below_minimum")
    if value("improvement_paired_t") < min_paired_t:
        reasons.append("improvement_paired_t_below_minimum")
    if value("mean_objective_improvement") <= min_mean_improvement:
        reasons.append("mean_objective_improvement_not_positive")
    return GateDecision(passed=not reasons, reasons=tuple(reasons))


def final_promotion_decision(
    stage8: Mapping[str, Any],
    walk_forward: Mapping[str, Any],
    *,
    min_paired_t: float,
    min_gate_pass_rate: float,
    min_win_rate: float,
    min_constraint_pass_rate: float,
) -> GateDecision:
    reasons: list[str] = []
    if int(stage8.get("stage8_gate_pass") or 0) != 1:
        reasons.append("stage8_preliminary_gate_failed")
    if bool(stage8.get("post_lock_data_included")):
        reasons.append("stage8_post_lock_research_override")
    if stage8.get("config_sha256") != walk_forward.get("config_sha256"):
        reasons.append("stage8_walk_forward_config_hash_mismatch")
    if stage8.get("signal_panel_sha256") != walk_forward.get("signal_panel_sha256"):
        reasons.append("stage8_walk_forward_panel_hash_mismatch")
    walk_forward_decision = walk_forward_gate_decision(
        walk_forward,
        min_win_rate=min_win_rate,
        min_gate_pass_rate=min_gate_pass_rate,
        min_constraint_pass_rate=min_constraint_pass_rate,
        min_paired_t=min_paired_t,
    )
    reasons.extend(walk_forward_decision.reasons)
    return GateDecision(passed=not reasons, reasons=tuple(dict.fromkeys(reasons)))


def build_promotion_receipt(
    *,
    model_family: str,
    model_version: str,
    effective_date: str,
    approved_by: str,
    approval_note: str,
    stage8_manifest: Mapping[str, Any],
    walk_forward_manifest: Mapping[str, Any],
    stage8_weights: Mapping[str, Any],
) -> dict[str, Any]:
    """Build an immutable approval receipt; caller is responsible for policy checks."""
    receipt: dict[str, Any] = {
        "schema_version": PROMOTION_RECEIPT_SCHEMA_VERSION,
        "model_family": model_family,
        "model_version": model_version,
        "effective_date": effective_date,
        "approved_by": approved_by,
        "approval_note": approval_note,
        "approved_at_utc": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "stage8_run_id": stage8_manifest.get("run_id", ""),
        "stage8_manifest_sha256": canonical_sha256(stage8_manifest),
        "walk_forward_run_id": walk_forward_manifest.get("run_id", ""),
        "walk_forward_manifest_sha256": canonical_sha256(walk_forward_manifest),
        "config_sha256": stage8_weights.get("config_sha256", ""),
        "signal_panel_sha256": stage8_weights.get("signal_panel_sha256", ""),
        "weights_sha256": weight_fingerprint(stage8_weights),
    }
    receipt["receipt_content_sha256"] = canonical_sha256(receipt)
    return receipt


def validate_promotion_receipt(receipt: Mapping[str, Any], *, model_family: str) -> list[str]:
    errors: list[str] = []
    schema = receipt.get("schema_version")
    supported = {PROMOTION_RECEIPT_SCHEMA_VERSION, MANUAL_PROMOTION_RECEIPT_SCHEMA_VERSION}
    if schema not in supported:
        errors.append("Missing or unsupported promotion receipt schema.")
    if receipt.get("model_family") != model_family:
        errors.append("Promotion receipt model_family mismatch.")
    for key in (
        "model_version",
        "effective_date",
        "approved_by",
        "stage8_run_id",
        "walk_forward_run_id",
        "config_sha256",
        "signal_panel_sha256",
        "weights_sha256",
    ):
        if not str(receipt.get(key) or "").strip():
            errors.append(f"Promotion receipt missing {key}.")
    if schema == MANUAL_PROMOTION_RECEIPT_SCHEMA_VERSION:
        if receipt.get("decision_type") != "manual_economic_override":
            errors.append("Manual promotion receipt has an invalid decision_type.")
        if int(receipt.get("strict_gate_failure_acknowledged") or 0) != 1:
            errors.append("Manual promotion receipt must acknowledge strict gate failure.")
        for key in (
            "consolidated_decision_sha256",
            "rollback_weights_sha256",
            "rollback_scoring_config_key",
            "probation_contract",
        ):
            if not receipt.get(key):
                errors.append(f"Manual promotion receipt missing {key}.")
    recorded = str(receipt.get("receipt_content_sha256") or "")
    content = {key: value for key, value in receipt.items() if key != "receipt_content_sha256"}
    if recorded != canonical_sha256(content):
        errors.append("Promotion receipt content hash mismatch.")
    return errors
