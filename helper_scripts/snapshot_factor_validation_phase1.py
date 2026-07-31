from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_AUDIT_DIR = PROJECT_ROOT / ".audit" / "factor_validation_phase1"

SNAPSHOT_PATHS = (
    "med_devices/config.yaml",
    "med_devices/scripts/13_build_med_device_daily_scores.py",
    "med_devices/scripts/24_analyze_med_device_component_ic_by_cohort.py",
    "med_devices/scripts/51_analyze_med_device_signal_decay.py",
    "output/med_devices_reports/calibration/med_device_cohort_neutral_backtest.csv",
    "output/med_devices_reports/calibration/med_device_component_ic_by_cohort.csv",
    "output/med_devices_reports/calibration/med_device_signal_decay_analysis.csv",
    "output/med_devices_reports/med_device_daily_composite_scores.csv",
    "biotech_index/config.yaml",
    "biotech_index/scripts/43_validate_biotech_feature_ic_monotonicity.py",
    "biotech_index/scripts/45_run_biotech_clean_historical_sequence.py",
    "biotech_index/scripts/46_optuna_biotech_candidate_optimizer.py",
    "output/biotech_index_reports/feature_ic_monitor/feature_ic_monitor_manifest.csv",
    "output/biotech_index_reports/feature_ic_monitor/feature_ic_summary.csv",
    "output/biotech_index_reports/feature_ic_monitor/feature_ic_by_cohort.csv",
    "output/biotech_index_reports/feature_ic_monitor/feature_ic_quintiles.csv",
    "output/biotech_index_reports/feature_ic_monitor/feature_ic_classification.csv",
    "technology/core/signal_diagnostics.py",
    "portfolio_layer/research/stage11_common.py",
    "portfolio_layer/research/72_component_ic_by_regime.py",
    "portfolio_layer/research/74_factor_payoff_diagnostics.py",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Write a deterministic Phase-1 factor-validation baseline manifest. "
            "The manifest records hashes and small safety summaries; it never mutates source artifacts."
        )
    )
    parser.add_argument("--label", required=True, help="Stable snapshot label, for example pre_change or post_change.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_AUDIT_DIR)
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def file_record(path: Path) -> dict[str, Any]:
    relative = path.relative_to(PROJECT_ROOT).as_posix()
    if not path.exists():
        return {"path": relative, "status": "missing"}
    stat = path.stat()
    return {
        "path": relative,
        "status": "present",
        "size_bytes": stat.st_size,
        "mtime_utc": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        "sha256": sha256_file(path),
    }


def read_csv_rows(path: Path) -> Iterable[dict[str, str]]:
    if not path.exists():
        return ()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return tuple(dict(row) for row in csv.DictReader(handle))


def to_float(raw: object) -> float | None:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value == value and abs(value) != float("inf") else None


def med_device_score_summary() -> dict[str, Any]:
    path = PROJECT_ROOT / "output/med_devices_reports/med_device_daily_composite_scores.csv"
    if not path.exists():
        return {"status": "missing", "path": path.relative_to(PROJECT_ROOT).as_posix()}
    mode_counts: Counter[str] = Counter()
    row_count = 0
    changed_count = 0
    max_abs_change = 0.0
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            row_count += 1
            mode = str(row.get("ic_tilted_composite_mode") or "").strip() or "blank"
            mode_counts[mode] += 1
            raw_score = to_float(row.get("raw_composite_score"))
            composite_score = to_float(row.get("composite_score"))
            if raw_score is None or composite_score is None:
                continue
            difference = abs(composite_score - raw_score)
            if mode == "replace_raw" and difference > 0.005:
                changed_count += 1
                max_abs_change = max(max_abs_change, difference)
    return {
        "status": "present",
        "path": path.relative_to(PROJECT_ROOT).as_posix(),
        "row_count": row_count,
        "ic_tilted_mode_counts": dict(sorted(mode_counts.items())),
        "replace_raw_actual_composite_changed_count": changed_count,
        "replace_raw_max_abs_composite_change": round(max_abs_change, 6),
    }


def biotech_classification_summary() -> dict[str, Any]:
    path = PROJECT_ROOT / "output/biotech_index_reports/feature_ic_monitor/feature_ic_classification.csv"
    if not path.exists():
        return {"status": "missing", "path": path.relative_to(PROJECT_ROOT).as_posix()}
    rows = read_csv_rows(path)
    classification_counts: Counter[str] = Counter()
    evidence_status_counts: Counter[str] = Counter()
    promotion_eligible_count = 0
    for row in rows:
        classification_counts[str(row.get("classification") or "").strip() or "blank"] += 1
        evidence_status_counts[str(row.get("evidence_status") or "").strip() or "missing"] += 1
        if str(row.get("promotion_eligible") or "").strip().lower() in {"1", "true", "yes"}:
            promotion_eligible_count += 1
    return {
        "status": "present",
        "path": path.relative_to(PROJECT_ROOT).as_posix(),
        "row_count": len(tuple(rows)),
        "classification_counts": dict(sorted(classification_counts.items())),
        "evidence_status_counts": dict(sorted(evidence_status_counts.items())),
        "promotion_eligible_count": promotion_eligible_count,
    }


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main() -> int:
    args = parse_args()
    label = str(args.label).strip()
    if not label or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for character in label):
        raise ValueError("--label must contain only letters, numbers, underscores, or hyphens")
    output_dir = args.output_dir.expanduser().resolve()
    manifest_path = output_dir / f"{label}.json"
    records = [file_record(PROJECT_ROOT / relative) for relative in SNAPSHOT_PATHS]
    payload = {
        "schema_version": "factor_validation_phase1_baseline_v1",
        "label": label,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "project_root": str(PROJECT_ROOT),
        "files": records,
        "summaries": {
            "med_device_production_score_state": med_device_score_summary(),
            "biotech_feature_ic_state": biotech_classification_summary(),
        },
    }
    write_json_atomic(manifest_path, payload)
    print(f"phase1_baseline_manifest={manifest_path}")
    print(f"present_files={sum(record['status'] == 'present' for record in records)} missing_files={sum(record['status'] == 'missing' for record in records)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
