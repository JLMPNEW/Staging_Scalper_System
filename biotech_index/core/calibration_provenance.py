from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from biotech_index.core.config import cfg_get, resolve_path


def _file_sha256(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_mtime_ns(path: Path) -> int:
    return path.stat().st_mtime_ns if path.exists() else 0


def observation_scoring_config_payload(
    config: dict[str, Any],
    *,
    base_dir: Path,
) -> dict[str, object]:
    """Return all config inputs that can change cached calibration observations."""
    calibration_cohorts = cfg_get(config, "biotech_scoring.calibration_cohorts", {}) or {}
    if not isinstance(calibration_cohorts, Mapping):
        calibration_cohorts = {}
    calibration_cohorts_csv = resolve_path(
        calibration_cohorts.get("csv", "data/biotech_calibration_cohorts.csv"),
        base_dir=base_dir,
    )
    calibration_cohort_migration_csv = resolve_path(
        calibration_cohorts.get(
            "migration_csv",
            "data/biotech_cohort_migration_20260831.csv",
        ),
        base_dir=base_dir,
    )
    return {
        # Hash the complete scoring and feature subtrees. Over-invalidating an
        # observation cache is safe; retaining it across a score-definition
        # change is not.
        "biotech_scoring": cfg_get(config, "biotech_scoring", {}) or {},
        "biotech_features": cfg_get(config, "biotech_features", {}) or {},
        "biotech_taxonomy": cfg_get(config, "biotech_taxonomy", {}) or {},
        "calibration_tier1": cfg_get(config, "calibration.tier1", {}) or {},
        "sec_event_parser": cfg_get(config, "sec_event_parser", {}) or {},
        "commercial_value": cfg_get(config, "commercial_value", {}) or {},
        "forward_guidance": cfg_get(config, "forward_guidance", {}) or {},
        "financial_survival": cfg_get(config, "financial_survival", {}) or {},
        "governance_event_features": cfg_get(config, "governance_event_features", {}) or {},
        "biotech_reports_borrow_validation": cfg_get(
            config,
            "biotech_reports.borrow_availability_validation",
            {},
        )
        or {},
        "calibration_cohorts_csv_path": str(calibration_cohorts_csv),
        "calibration_cohorts_csv_sha256": _file_sha256(calibration_cohorts_csv),
        "calibration_cohorts_csv_mtime_ns": _file_mtime_ns(calibration_cohorts_csv),
        "calibration_cohort_migration_csv_path": str(calibration_cohort_migration_csv),
        "calibration_cohort_migration_csv_sha256": _file_sha256(
            calibration_cohort_migration_csv
        ),
        "calibration_cohort_migration_csv_mtime_ns": _file_mtime_ns(
            calibration_cohort_migration_csv
        ),
    }


def observation_scoring_config_hash(config: dict[str, Any], *, base_dir: Path) -> str:
    payload = observation_scoring_config_payload(config, base_dir=base_dir)
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()
