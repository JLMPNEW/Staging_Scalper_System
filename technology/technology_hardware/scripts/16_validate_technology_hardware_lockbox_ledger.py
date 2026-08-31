#!/usr/bin/env python3
"""Validate technology-hardware governance outputs."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from technology.core.logging_utils import configure_utc_logging  # noqa: E402


LOGGER = logging.getLogger("technology_hardware_governance_validator")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
CONFIG_KEY = "technology_hardware_governance_reports"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate technology-hardware governance reports.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def read_json(path: Path) -> dict:
    if not path.exists() or path.stat().st_size == 0:
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def require_file(errors: list[str], path: Path, label: str) -> None:
    if not path.exists() or path.stat().st_size == 0:
        errors.append(f"Missing or empty {label}: {path}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_artifact_path(raw: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else PROJECT_ROOT / path


def ledger_self_hash(payload: dict) -> str:
    # Mirrors the publisher: the self-hash is computed over the ledger JSON
    # serialized with ledger_content_sha256 blanked.
    clone = dict(payload)
    clone["ledger_content_sha256"] = ""
    return hashlib.sha256(json.dumps(clone, indent=2, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def verify_snapshot_chain(errors: list[str], snapshot_dir: Path) -> int:
    if not snapshot_dir.is_dir():
        errors.append(f"Missing lockbox snapshot directory: {snapshot_dir}")
        return 0
    snapshots = sorted(snapshot_dir.glob("technology_hardware_lockbox_ledger_*.json"))
    if not snapshots:
        errors.append(f"No lockbox snapshots found in: {snapshot_dir}")
        return 0
    previous: Path | None = None
    for snapshot in snapshots:
        try:
            payload = json.loads(snapshot.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"Invalid lockbox snapshot JSON: {snapshot.name}: {exc}")
            previous = snapshot
            continue
        recorded_hash = str(payload.get("ledger_content_sha256") or "")
        if recorded_hash and ledger_self_hash(payload) != recorded_hash:
            errors.append(f"Snapshot self-hash mismatch (content changed after publish): {snapshot.name}")
        if "previous_snapshot_sha256" in payload:
            expected = sha256_file(previous) if previous is not None else ""
            if str(payload.get("previous_snapshot_sha256") or "") != expected:
                errors.append(
                    f"Snapshot chain broken at {snapshot.name}: previous_snapshot_sha256 does not match "
                    f"{previous.name if previous is not None else '<no previous snapshot>'}"
                )
        previous = snapshot
    return len(snapshots)


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else resolve_path(
        cfg_get(config, f"{CONFIG_KEY}.output_dir", "../output/technology_reports/technology_hardware/governance"),
        base_dir=base_dir,
    )
    paths = {
        "signal_registry_csv": output_dir / "technology_hardware_signal_registry.csv",
        "signal_registry_json": output_dir / "technology_hardware_signal_registry.json",
        "lockbox_csv": output_dir / "technology_hardware_lockbox_ledger.csv",
        "lockbox_json": output_dir / "technology_hardware_lockbox_ledger.json",
        "manifest": output_dir / "technology_hardware_governance_manifest.json",
    }
    errors: list[str] = []
    for label, path in paths.items():
        require_file(errors, path, label)

    registry_rows = read_csv_rows(paths["signal_registry_csv"])
    artifact_rows = read_csv_rows(paths["lockbox_csv"])
    if len(registry_rows) < 20:
        errors.append(f"Signal registry has too few rows: {len(registry_rows)}")
    if len(artifact_rows) < 10:
        errors.append(f"Lockbox artifact ledger has too few rows: {len(artifact_rows)}")
    if registry_rows:
        statuses = {row.get("decision_status") for row in registry_rows}
        if "production_locked" not in statuses:
            errors.append("Signal registry has no production_locked rows.")
        if not any(row.get("stage8_candidate_flag") == "1" for row in registry_rows):
            errors.append("Signal registry has no Stage 8 candidate-flagged rows.")
    for row in artifact_rows:
        if row.get("required_flag") == "1" and row.get("exists_flag") != "1":
            errors.append(f"Required artifact missing in ledger: {row.get('artifact_name')} -> {row.get('path')}")
        if row.get("exists_flag") == "1" and not row.get("sha256"):
            errors.append(f"Existing artifact missing sha256: {row.get('artifact_name')}")
        if row.get("exists_flag") == "1" and row.get("sha256"):
            artifact_path = resolve_artifact_path(str(row.get("path") or ""))
            if not artifact_path.is_file():
                errors.append(f"Ledger artifact no longer exists on disk: {row.get('artifact_name')} -> {artifact_path}")
            elif sha256_file(artifact_path) != str(row.get("sha256")):
                errors.append(
                    f"Ledger artifact sha256 mismatch (content changed since publish): "
                    f"{row.get('artifact_name')} -> {artifact_path}"
                )

    if paths["lockbox_json"].exists():
        try:
            lockbox = read_json(paths["lockbox_json"])
            if lockbox.get("production_model_status") != "stage7_active":
                errors.append(f"Unexpected production_model_status: {lockbox.get('production_model_status')}")
            if lockbox.get("stage8_candidate_status") != "report_only_not_promoted":
                errors.append(f"Unexpected stage8_candidate_status: {lockbox.get('stage8_candidate_status')}")
            if int(lockbox.get("manual_promotion_approved") or 0) != 0:
                errors.append("Governance report records manual promotion approval; expected 0.")
            if int(lockbox.get("automatic_promotion_applied") or 0) != 0:
                errors.append("Governance report applied automatic promotion; expected review-only mode.")
            latest_status = lockbox.get("latest_stage8_research_candidate_status")
            if latest_status not in {"report_only_not_promoted", "promotable_pending_manual_review"}:
                errors.append(f"Unexpected latest_stage8_research_candidate_status: {latest_status}")
            promotion = lockbox.get("promotion_decision") or {}
            if promotion.get("decision") != "stage7_remains_production":
                errors.append(f"Unexpected promotion decision: {promotion.get('decision')}")
            if int(promotion.get("stage8_is_production") or 0) != 0:
                errors.append("Stage 8 is marked production; expected 0.")
            if int(promotion.get("latest_research_candidate_promoted") or 0) != 0:
                errors.append("Latest research candidate was promoted automatically; expected 0.")
            if int(lockbox.get("production_binding_valid") or 0) != 1:
                errors.append(f"Invalid production binding: {lockbox.get('production_binding_status')} {lockbox.get('production_binding_reasons')}")
            if lockbox.get("missing_required_artifacts") or []:
                errors.append(f"Lockbox reports missing required artifacts: {lockbox.get('missing_required_artifacts')}")
            registry_summary = lockbox.get("signal_registry_summary") or {}
            if int(registry_summary.get("stage8_candidate_flag_rows") or 0) <= 0:
                errors.append("Lockbox signal registry summary has no Stage 8 candidate-flagged rows.")
            stage7_top = lockbox.get("top10_stage7_rank_ready") or []
            stage8_top = lockbox.get("top10_stage8_candidate") or []
            if len(stage7_top) < 10:
                errors.append(f"Lockbox top10_stage7_rank_ready too short: {len(stage7_top)}")
            if len(stage8_top) < 10:
                errors.append(f"Lockbox top10_stage8_candidate too short: {len(stage8_top)}")
            recorded_self_hash = str(lockbox.get("ledger_content_sha256") or "")
            if not recorded_self_hash:
                errors.append("Lockbox ledger missing ledger_content_sha256 self-hash.")
            elif ledger_self_hash(lockbox) != recorded_self_hash:
                errors.append("Lockbox ledger_content_sha256 does not match recomputed content hash.")
            if "previous_snapshot_sha256" not in lockbox:
                errors.append("Lockbox ledger missing previous_snapshot_sha256 chain field.")
        except json.JSONDecodeError as exc:
            errors.append(f"Invalid lockbox JSON: {exc}")

    snapshot_count = verify_snapshot_chain(errors, output_dir / "snapshots")

    if paths["manifest"].exists():
        try:
            manifest = read_json(paths["manifest"])
            if int(manifest.get("automatic_promotion_applied") or 0) != 0:
                errors.append("Manifest indicates automatic promotion; expected 0.")
            if manifest.get("production_model_status") != "stage7_active":
                errors.append(f"Unexpected manifest production status: {manifest.get('production_model_status')}")
            if manifest.get("stage8_candidate_status") != "report_only_not_promoted":
                errors.append(f"Unexpected manifest Stage 8 candidate status: {manifest.get('stage8_candidate_status')}")
            latest_status = manifest.get("latest_stage8_research_candidate_status")
            if latest_status not in {"report_only_not_promoted", "promotable_pending_manual_review"}:
                errors.append(f"Unexpected manifest latest research status: {latest_status}")
        except json.JSONDecodeError as exc:
            errors.append(f"Invalid governance manifest JSON: {exc}")

    if errors:
        for error in errors:
            LOGGER.error(error)
        return 1
    LOGGER.info(
        "Technology-hardware governance validation passed: signal_rows=%d artifact_rows=%d snapshots=%d output=%s",
        len(registry_rows),
        len(artifact_rows),
        snapshot_count,
        output_dir,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
