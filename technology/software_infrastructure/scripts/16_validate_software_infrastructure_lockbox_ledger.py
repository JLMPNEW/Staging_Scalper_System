#!/usr/bin/env python3
"""Validate software-infrastructure LCR governance outputs."""
from __future__ import annotations

import argparse
import csv
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


LOGGER = logging.getLogger("software_infrastructure_lcr_validator")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
CONFIG_KEY = "software_infrastructure_governance_reports"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate software-infrastructure LCR governance reports.")
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


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else resolve_path(
        cfg_get(config, f"{CONFIG_KEY}.output_dir", "../output/technology_reports/software_infrastructure/governance"),
        base_dir=base_dir,
    )
    paths = {
        "signal_registry_csv": output_dir / "software_infrastructure_signal_registry.csv",
        "signal_registry_json": output_dir / "software_infrastructure_signal_registry.json",
        "lockbox_csv": output_dir / "software_infrastructure_lockbox_ledger.csv",
        "lockbox_json": output_dir / "software_infrastructure_lockbox_ledger.json",
        "manifest": output_dir / "software_infrastructure_governance_manifest.json",
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

    if paths["lockbox_json"].exists():
        try:
            lockbox = read_json(paths["lockbox_json"])
            if lockbox.get("production_model_status") != "stage8_active":
                errors.append(f"Unexpected production_model_status: {lockbox.get('production_model_status')}")
            if lockbox.get("stage7_challenger_status") != "active_challenger":
                errors.append(f"Unexpected stage7_challenger_status: {lockbox.get('stage7_challenger_status')}")
            if int(lockbox.get("manual_promotion_approved") or 0) != 1:
                errors.append("LCR does not record manual promotion approval.")
            if int(lockbox.get("automatic_promotion_applied") or 0) != 0:
                errors.append("LCR applied automatic promotion; expected review-only mode.")
            latest_status = lockbox.get("latest_stage8_research_candidate_status")
            if latest_status not in {"report_only_not_promoted", "promotable_pending_manual_review"}:
                errors.append(f"Unexpected latest_stage8_research_candidate_status: {latest_status}")
            promotion = lockbox.get("promotion_decision") or {}
            if promotion.get("decision") != "stage8_promoted_to_production":
                errors.append(f"Unexpected promotion decision: {promotion.get('decision')}")
            if int(promotion.get("latest_research_candidate_promoted") or 0) != 0:
                errors.append("Latest research candidate was promoted automatically; expected 0.")
            if (lockbox.get("missing_required_artifacts") or []):
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
        except json.JSONDecodeError as exc:
            errors.append(f"Invalid lockbox JSON: {exc}")

    if paths["manifest"].exists():
        try:
            manifest = read_json(paths["manifest"])
            if int(manifest.get("automatic_promotion_applied") or 0) != 0:
                errors.append("Manifest indicates automatic promotion; expected 0.")
            if manifest.get("production_model_status") != "stage8_active":
                errors.append(f"Unexpected manifest production status: {manifest.get('production_model_status')}")
            if manifest.get("stage8_candidate_status") != "promoted_to_production":
                errors.append(f"Unexpected stage8 candidate status: {manifest.get('stage8_candidate_status')}")
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
        "Software-infrastructure LCR validation passed: signal_rows=%d artifact_rows=%d output=%s",
        len(registry_rows),
        len(artifact_rows),
        output_dir,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
