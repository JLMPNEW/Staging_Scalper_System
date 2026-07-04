#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml  # noqa: E402
from industrials.core.logging_utils import configure_utc_logging  # noqa: E402
from industrials.core.reports import write_csv_atomic  # noqa: E402
from industrials.defense.research_artifacts import (  # noqa: E402
    MODEL_FAMILY,
    PANEL_SOURCE_SURVIVORSHIP_CORRECTED,
    PILLAR_SCORE_FIELDS,
    as_float,
    csv_header,
    parse_date,
    read_csv_rows,
    sha256_file,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
REQUIRED_PANEL_FIELDS = [
    "ticker",
    "asof_date",
    "model_family",
    "score_model_version",
    "calibration_cohort_id",
    "final_score",
    "native_score_value",
    *PILLAR_SCORE_FIELDS,
    "source_snapshot_asof_date",
    "price_data_asof_date",
    "market_feature_asof_date",
    "financial_feature_asof_date",
    "positioning_feature_asof_date",
    "stage11_calibration_panel_source",
    "survivorship_corrected_panel_flag",
    "source_rank_table_sha256",
    "source_rank_manifest_sha256",
    "price_ticker",
    "price_source_id",
    "price_asof_date",
    "price_forward_date",
    "forward_days",
    "forward_return",
    "benchmark_ticker",
    "benchmark_asof_date",
    "benchmark_forward_date",
    "benchmark_forward_return",
    "forward_excess_return_vs_sector",
    "return_available_flag",
    "return_unavailable_reason",
    "panel_row_eligible_flag",
    "panel_row_eligible_reason",
    "split_name",
]
SOURCE_DATE_FIELDS = [
    "source_snapshot_asof_date",
    "price_data_asof_date",
    "market_feature_asof_date",
    "financial_feature_asof_date",
    "positioning_feature_asof_date",
    "feature_data_asof_date",
]
REPORT_FIELDS = [
    "artifact",
    "status",
    "rows",
    "eligible_rows",
    "return_available_rows",
    "snapshot_count",
    "promotable",
    "issues",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate defense Stage 8 calibration research artifacts.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--panel-csv", type=Path, default=None)
    parser.add_argument("--splits-csv", type=Path, default=None)
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--promotion-check", action="store_true")
    return parser.parse_args()


def default_artifact_paths() -> tuple[Path, Path, Path]:
    root = PROJECT_ROOT / "output" / "industrials" / "defense" / "stage8" / "oos_calibration_panel"
    return (
        root / "defense_oos_calibration_panel.csv",
        root / "defense_oos_calibration_splits.csv",
        root / "defense_oos_calibration_panel_manifest.json",
    )


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid manifest JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Manifest root must be an object: {path}")
    return payload


def validate_file_hashes(panel_csv: Path, splits_csv: Path, manifest: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    files = manifest.get("files")
    if not isinstance(files, dict):
        return ["manifest missing files block"]
    for path in [panel_csv, splits_csv]:
        meta = files.get(path.name)
        if not isinstance(meta, dict):
            issues.append(f"manifest missing file metadata for {path.name}")
            continue
        expected = str(meta.get("sha256") or "")
        actual = sha256_file(path)
        if expected != actual:
            issues.append(f"{path.name} sha256 mismatch")
    return issues


def split_names(rows: list[dict[str, str]]) -> set[str]:
    return {str(row.get("split_name") or "") for row in rows if str(row.get("split_name") or "")}


def safe_parse_date(raw: object, *, field: str, malformed: list[str]) -> date | None:
    """parse_date that records malformed values instead of crashing the validator.

    A corrupted panel is exactly when this validator must still produce a
    report; research_artifacts.parse_date raises on non-empty unparseable text.
    """
    try:
        return parse_date(raw, field=field)
    except ValueError:
        malformed.append(f"{field}={str(raw)[:30]!r}")
        return None


def validate_panel_rows(rows: list[dict[str, str]], splits: list[dict[str, str]], manifest: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    split_set = split_names(splits)
    benchmark = str(manifest.get("benchmark_ticker") or "")
    forward_days = str(manifest.get("forward_days") or "")
    source_hashes = {
        str(item.get("rank_table_sha256") or "")
        for item in manifest.get("source_snapshots", [])
        if isinstance(item, dict)
    }
    manifest_hashes = {
        str(item.get("rank_manifest_sha256") or "")
        for item in manifest.get("source_snapshots", [])
        if isinstance(item, dict)
    }
    future_source_rows: list[str] = []
    future_return_rows: list[str] = []
    bad_scores: list[str] = []
    bad_native_alias: list[str] = []
    bad_benchmark: list[str] = []
    bad_hash_refs: list[str] = []
    bad_splits: list[str] = []
    bad_eligible_rows: list[str] = []
    bad_forward_days: list[str] = []
    malformed_dates: list[str] = []
    for row in rows:
        ticker = str(row.get("ticker") or "")
        asof = safe_parse_date(row.get("asof_date"), field=f"{ticker}:asof_date", malformed=malformed_dates)
        if asof is None:
            future_source_rows.append(f"{ticker}:missing_asof")
            continue
        score = as_float(row.get("final_score"))
        if score is None or score < 0.0 or score > 100.0:
            bad_scores.append(ticker)
        native = as_float(row.get("native_score_value"))
        if native is not None and score is not None and abs(native - score) > 1e-8:
            bad_native_alias.append(ticker)
        if str(row.get("benchmark_ticker") or "") != benchmark:
            bad_benchmark.append(ticker)
        if str(row.get("forward_days") or "") != forward_days:
            bad_forward_days.append(ticker)
        for field in SOURCE_DATE_FIELDS:
            raw = str(row.get(field) or "").strip()
            if not raw:
                continue
            parsed = safe_parse_date(raw, field=f"{ticker}:{field}", malformed=malformed_dates)
            if parsed and parsed > asof:
                future_source_rows.append(f"{ticker}:{field}={parsed.isoformat()}")
        if str(row.get("return_available_flag") or "") == "1":
            forward_date = safe_parse_date(row.get("price_forward_date"), field=f"{ticker}:price_forward_date", malformed=malformed_dates)
            benchmark_forward_date = safe_parse_date(row.get("benchmark_forward_date"), field=f"{ticker}:benchmark_forward_date", malformed=malformed_dates)
            if forward_date is None or benchmark_forward_date is None:
                future_return_rows.append(f"{ticker}:missing_forward_date")
            elif forward_date <= asof or benchmark_forward_date <= asof:
                future_return_rows.append(f"{ticker}:forward_date_not_after_asof")
            if as_float(row.get("forward_excess_return_vs_sector")) is None:
                future_return_rows.append(f"{ticker}:missing_forward_excess_return")
        if str(row.get("source_rank_table_sha256") or "") not in source_hashes:
            bad_hash_refs.append(f"{ticker}:rank_table")
        if str(row.get("source_rank_manifest_sha256") or "") not in manifest_hashes:
            bad_hash_refs.append(f"{ticker}:rank_manifest")
        if str(row.get("split_name") or "") not in split_set:
            bad_splits.append(ticker)
        if str(row.get("panel_row_eligible_flag") or "") == "1":
            if str(row.get("return_available_flag") or "") != "1":
                bad_eligible_rows.append(f"{ticker}:eligible_without_return")
            if str(row.get("panel_row_eligible_reason") or "") != "eligible":
                bad_eligible_rows.append(f"{ticker}:eligible_reason_not_clean")
    if malformed_dates:
        issues.append(f"malformed date values: {malformed_dates[:20]}")
    if bad_forward_days:
        issues.append(f"forward_days mismatch vs manifest: {bad_forward_days[:20]}")
    if bad_scores:
        issues.append(f"final_score outside 0..100: {bad_scores[:20]}")
    if bad_native_alias:
        issues.append(f"native_score_value differs from final_score: {bad_native_alias[:20]}")
    if bad_benchmark:
        issues.append(f"benchmark_ticker mismatch: {bad_benchmark[:20]}")
    if future_source_rows:
        issues.append(f"source date after asof: {future_source_rows[:20]}")
    if future_return_rows:
        issues.append(f"invalid return windows: {future_return_rows[:20]}")
    if bad_hash_refs:
        issues.append(f"row source hash not listed in manifest: {bad_hash_refs[:20]}")
    if bad_splits:
        issues.append(f"row split not defined in split file: {bad_splits[:20]}")
    if bad_eligible_rows:
        issues.append(f"bad eligible-row contract: {bad_eligible_rows[:20]}")
    return issues


def validate_promotion(
    rows: list[dict[str, str]],
    manifest: dict[str, Any],
    *,
    min_snapshots: int,
    promotion_check: bool,
) -> list[str]:
    issues: list[str] = []
    source_modes = {str(row.get("stage11_calibration_panel_source") or "") for row in rows}
    snapshot_count = int(manifest.get("snapshot_count") or 0)
    eligible_rows = sum(1 for row in rows if str(row.get("panel_row_eligible_flag") or "") == "1")
    survivorship_flags = {str(row.get("survivorship_corrected_panel_flag") or "") for row in rows}
    if snapshot_count < min_snapshots:
        issues.append(f"snapshot_count {snapshot_count} below promotion minimum {min_snapshots}")
    if source_modes != {PANEL_SOURCE_SURVIVORSHIP_CORRECTED}:
        issues.append(f"panel source is not survivorship-corrected PIT recompute: {sorted(source_modes)}")
    if survivorship_flags != {"1"}:
        issues.append(f"survivorship_corrected_panel_flag not all 1: {sorted(survivorship_flags)}")
    if eligible_rows == 0:
        issues.append("no eligible calibration rows")
    if promotion_check and manifest.get("promotable") is not True:
        blockers = manifest.get("promotion_blockers")
        issues.append(f"manifest promotable is not true; blockers={blockers}")
    return issues


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    family_cfg = cfg_get(config, "oos_calibration_standards.families.defense", {}) or {}
    min_snapshots = int(cfg_get(family_cfg, "min_shadow_snapshots_for_promotion", 60) or 60)
    default_panel, default_splits, default_manifest = default_artifact_paths()
    panel_csv = args.panel_csv.expanduser().resolve() if args.panel_csv else default_panel
    splits_csv = args.splits_csv.expanduser().resolve() if args.splits_csv else default_splits
    manifest_path = args.manifest.expanduser().resolve() if args.manifest else default_manifest
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else PROJECT_ROOT / "output" / "industrials" / "defense" / "stage8" / "oos_calibration_artifact_validation_report.csv"
    )
    for path in [panel_csv, splits_csv, manifest_path]:
        if not path.exists():
            raise FileNotFoundError(path)
    manifest = load_manifest(manifest_path)
    rows = read_csv_rows(panel_csv)
    splits = read_csv_rows(splits_csv)
    issues: list[str] = []
    if manifest.get("model_family") != MODEL_FAMILY:
        issues.append("manifest model_family mismatch")
    if manifest.get("benchmark_ticker") != "XAR":
        issues.append(f"manifest benchmark_ticker must be XAR, got {manifest.get('benchmark_ticker')!r}")
    header = csv_header(panel_csv)
    missing_fields = [field for field in REQUIRED_PANEL_FIELDS if field not in header]
    if missing_fields:
        issues.append(f"panel missing required fields: {missing_fields}")
    issues.extend(validate_file_hashes(panel_csv, splits_csv, manifest))
    if not missing_fields:
        issues.extend(validate_panel_rows(rows, splits, manifest))
    promotion_issues = validate_promotion(rows, manifest, min_snapshots=min_snapshots, promotion_check=args.promotion_check)
    if args.promotion_check:
        issues.extend(promotion_issues)
    status = "pass" if not issues else "fail"
    promotable = "1" if not promotion_issues else "0"
    report_row = {
        "artifact": str(panel_csv),
        "status": status,
        "rows": len(rows),
        "eligible_rows": sum(1 for row in rows if str(row.get("panel_row_eligible_flag") or "") == "1"),
        "return_available_rows": sum(1 for row in rows if str(row.get("return_available_flag") or "") == "1"),
        "snapshot_count": manifest.get("snapshot_count", ""),
        "promotable": promotable,
        "issues": ";".join(issues if args.promotion_check else [*issues, *[f"promotion_blocked:{item}" for item in promotion_issues]]),
    }
    write_csv_atomic(output_csv, REPORT_FIELDS, [report_row])
    print(
        f"Stage 8 artifact validation: status={status} rows={report_row['rows']} "
        f"eligible={report_row['eligible_rows']} promotable={bool(not promotion_issues)}"
    )
    print(f"Wrote {output_csv}")
    return 0 if status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
