#!/usr/bin/env python3
"""Audit med-devices calibration/backfill freshness and governance cadence."""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
CONFIG_KEY = "med_devices_calibration_governance"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit med-devices calibration governance cadence.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", default="")
    parser.add_argument("--fail-on-stale", action="store_true")
    return parser.parse_args()


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def path_from_config(config: dict[str, Any], key: str, *, base_dir: Path) -> Path | None:
    raw = cfg_get(config, key)
    if raw is None or str(raw).strip() == "":
        return None
    return resolve_path(raw, base_dir=base_dir)


def latest_score_asof(db_path: Path, timeout_sec: float) -> str:
    if not db_path.exists():
        return ""
    with sqlite3.connect(db_path, timeout=timeout_sec) as conn:
        row = conn.execute("SELECT MAX(asof_date) FROM med_device_daily_scores").fetchone()
        return str(row[0] or "") if row else ""


def artifact_row(
    *,
    artifact_id: str,
    path: Path | None,
    max_age_days: int,
    today: date,
    required: bool = True,
) -> dict[str, Any]:
    if path is None:
        return {
            "artifact_id": artifact_id,
            "status": "MISSING_CONFIG" if required else "SKIPPED",
            "path": "",
            "modified_at": "",
            "age_days": "",
            "max_age_days": max_age_days,
            "action": "add_config_path" if required else "",
        }
    if not path.exists():
        return {
            "artifact_id": artifact_id,
            "status": "MISSING" if required else "SKIPPED",
            "path": str(path),
            "modified_at": "",
            "age_days": "",
            "max_age_days": max_age_days,
            "action": "run_required_calibration_stage" if required else "",
        }
    modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    age_days = max(0, (today - modified.date()).days)
    stale = age_days > max_age_days
    return {
        "artifact_id": artifact_id,
        "status": "STALE" if stale else "CURRENT",
        "path": str(path),
        "modified_at": modified.isoformat(timespec="seconds"),
        "age_days": age_days,
        "max_age_days": max_age_days,
        "action": "refresh_calibration_artifact" if stale else "",
    }


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    today = datetime.now(timezone.utc).date()
    timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0) or 30.0)
    asof = str(args.asof or "").strip() or latest_score_asof(db_path, timeout_sec)

    artifact_specs = [
        (
            "historical_backfill_manifest",
            "historical_backfill.manifest_csv",
            int(cfg_get(config, f"{CONFIG_KEY}.max_days_since_historical_backfill", 30) or 30),
        ),
        (
            "component_ic",
            "calibration.component_ic_csv",
            int(cfg_get(config, f"{CONFIG_KEY}.max_days_since_component_ic", 30) or 30),
        ),
        (
            "component_promotion_review",
            "calibration.component_promotion_review.output_csv",
            int(cfg_get(config, f"{CONFIG_KEY}.max_days_since_component_promotion_review", 30) or 30),
        ),
        (
            "cohort_policy_recommendations",
            "calibration.cohort_policy_recommendations.output_csv",
            int(cfg_get(config, f"{CONFIG_KEY}.max_days_since_policy_recommendations", 30) or 30),
        ),
        (
            "optuna_policy_summary",
            "calibration.optuna_policy_optimizer.summary_csv",
            int(cfg_get(config, f"{CONFIG_KEY}.max_days_since_optuna", 60) or 60),
        ),
        (
            "safe_core_threshold_recommendations",
            "calibration.safe_core_threshold_sensitivity.recommendation_csv",
            int(cfg_get(config, f"{CONFIG_KEY}.max_days_since_safe_core_thresholds", 60) or 60),
        ),
        (
            "calibrated_baseline_freeze",
            "calibration.calibrated_baseline.frozen_baseline_csv",
            int(cfg_get(config, f"{CONFIG_KEY}.max_days_since_baseline_freeze", 90) or 90),
        ),
    ]
    rows = [
        artifact_row(
            artifact_id=artifact_id,
            path=path_from_config(config, key, base_dir=base_dir),
            max_age_days=max_age_days,
            today=today,
        )
        for artifact_id, key, max_age_days in artifact_specs
    ]
    stale_or_missing = [row for row in rows if row["status"] in {"STALE", "MISSING", "MISSING_CONFIG"}]
    status = "REFRESH_DUE" if stale_or_missing else "CURRENT"
    fail_on_stale = bool(args.fail_on_stale or cfg_get(config, f"{CONFIG_KEY}.fail_on_stale", False))
    output_csv = resolve_path(
        cfg_get(config, f"{CONFIG_KEY}.output_csv", "../output/med_devices_reports/calibration/med_device_calibration_governance.csv"),
        base_dir=base_dir,
    )
    output_json = resolve_path(
        cfg_get(config, f"{CONFIG_KEY}.output_json", "../output/med_devices_reports/calibration/med_device_calibration_governance.json"),
        base_dir=base_dir,
    )
    fields = ["artifact_id", "status", "path", "modified_at", "age_days", "max_age_days", "action"]
    write_csv(output_csv, rows, fields)
    summary = {
        "asof": asof,
        "status": status,
        "refresh_due_count": len(stale_or_missing),
        "fail_on_stale": fail_on_stale,
        "score_model_version": cfg_get(config, "scoring.model_version", ""),
        "calibrated_baseline_version": cfg_get(config, "calibration.calibrated_baseline.baseline_version", ""),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "output_csv": str(output_csv),
        "artifacts": rows,
    }
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(summary, indent=2, sort_keys=True, default=str), encoding="utf-8")
    print(f"calibration_governance_status={status} refresh_due={len(stale_or_missing)} output={output_csv}")
    return 1 if fail_on_stale and stale_or_missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
