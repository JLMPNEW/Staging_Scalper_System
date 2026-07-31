#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sys
from datetime import date
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.logging_utils import configure_utc_logging  # noqa: E402
from industrials.core.rank_table_contracts import defense_final_rank_header  # noqa: E402
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.defense.production_activation import (  # noqa: E402
    candidate_evidence_issues,
    load_json,
    load_weights,
    promote_rows,
    register_effective_lock,
)
from industrials.defense.research_artifacts import (  # noqa: E402
    MODEL_FAMILY,
    command_line,
    read_csv_rows,
    sha256_file,
    utc_now,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_EFFECTIVE_DATE = "2026-07-27"
DEFAULT_RESEARCH_ASOF = "2026-07-24"
DEFAULT_SCORE_MODEL_VERSION = "defense_specialized_v1"
REPORT_FIELDS = [
    "effective_date",
    "research_asof_date",
    "status",
    "lock_id",
    "score_model_version",
    "scoring_mode",
    "rows",
    "portfolio_candidate_rows",
    "rank_table_path",
    "rank_manifest_path",
    "decision_manifest_path",
    "issues",
]


def parse_args() -> argparse.Namespace:
    candidate_root = (
        PROJECT_ROOT
        / "output"
        / "industrials"
        / "defense"
        / "stage8"
        / "candidates"
        / "specialized_v1_matched"
    )
    audit_root = (
        PROJECT_ROOT
        / "output"
        / "industrials"
        / "defense"
        / "stage10"
        / "metric_promotion_audit"
        / DEFAULT_RESEARCH_ASOF
    )
    parser = argparse.ArgumentParser(
        description=(
            "Activate the validated defense specialized model using an "
            "effective-dated immutable production lock."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--effective-date", default=DEFAULT_EFFECTIVE_DATE)
    parser.add_argument("--research-asof", default=DEFAULT_RESEARCH_ASOF)
    parser.add_argument(
        "--lock-id",
        default="defense_specialized_v1_20260727",
    )
    parser.add_argument(
        "--score-model-version",
        default=DEFAULT_SCORE_MODEL_VERSION,
    )
    parser.add_argument("--source-rank-table", type=Path, required=True)
    parser.add_argument("--target-rank-table", type=Path, default=None)
    parser.add_argument(
        "--comparison-manifest",
        type=Path,
        default=(
            candidate_root
            / "baseline_vs_candidate"
            / "defense_baseline_vs_candidate_manifest.json"
        ),
    )
    parser.add_argument(
        "--audit-summary",
        type=Path,
        default=audit_root / "defense_metric_promotion_audit_summary.json",
    )
    parser.add_argument(
        "--audit-manifest",
        type=Path,
        default=audit_root / "defense_metric_promotion_audit_manifest.json",
    )
    parser.add_argument(
        "--calibration-summary",
        type=Path,
        default=(
            candidate_root
            / "optuna_calibration_weekly"
            / "defense_optuna_calibration_summary.csv"
        ),
    )
    parser.add_argument(
        "--backtest-summary",
        type=Path,
        default=(
            PROJECT_ROOT
            / "output"
            / "industrials"
            / "defense"
            / "stage9"
            / "candidates"
            / "specialized_v1_matched"
            / "score_backtest_weekly"
            / "defense_score_backtest_summary.csv"
        ),
    )
    parser.add_argument(
        "--panel-csv",
        type=Path,
        default=(
            candidate_root
            / "oos_calibration_panel_weekly"
            / "defense_oos_calibration_panel.csv"
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=(
            PROJECT_ROOT
            / "output"
            / "industrials"
            / "defense"
            / "stage10"
            / "production_promotion"
        ),
    )
    parser.add_argument("--allow-overwrite", action="store_true")
    return parser.parse_args()


def parse_iso(raw: str, *, field: str) -> str:
    try:
        return date.fromisoformat(raw.strip()).isoformat()
    except ValueError as exc:
        raise ValueError(f"{field} must be YYYY-MM-DD, got {raw!r}") from exc


def read_header(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(next(csv.reader(handle)))


def config_relative_path(path: Path, *, base_dir: Path) -> str:
    return Path(os.path.relpath(path, start=base_dir)).as_posix()


def train_date_range(panel_rows: list[dict[str, str]]) -> tuple[str, str]:
    dates = sorted(
        {
            str(row.get("asof_date") or "")
            for row in panel_rows
            if row.get("split_name") == "train"
        }
    )
    if not dates:
        raise ValueError("candidate panel has no train snapshots")
    return dates[0], dates[-1]


def validate_source_rank(
    *,
    rank_path: Path,
    effective_date: str,
    score_model_version: str,
) -> tuple[list[str], list[dict[str, str]], dict[str, Any]]:
    issues: list[str] = []
    manifest_path = rank_path.with_name("defense_final_rank_table_manifest.json")
    if not rank_path.is_file() or not manifest_path.is_file():
        return [f"source rank table/manifest missing: {rank_path}"], [], {}
    manifest = load_json(manifest_path)
    rows = read_csv_rows(rank_path)
    if str(manifest.get("sha256") or "") != sha256_file(rank_path):
        issues.append("source rank manifest hash mismatch")
    if manifest.get("research_candidate") is not True:
        issues.append("source rank is not sealed as a research candidate")
    if str(manifest.get("scoring_mode") or "") != "specialized_v1":
        issues.append("source rank scoring_mode is not specialized_v1")
    if str(manifest.get("score_model_version") or "") != score_model_version:
        issues.append("source rank score_model_version mismatch")
    if str(manifest.get("asof_date") or "") != effective_date:
        issues.append("source rank manifest effective-date mismatch")
    if {str(row.get("asof_date") or "") for row in rows} != {effective_date}:
        issues.append("source rank rows are not uniform on effective date")
    if {str(row.get("score_model_version") or "") for row in rows} != {
        score_model_version
    }:
        issues.append("source rank row score_model_version mismatch")
    if any(
        str(row.get(field) or "") != "0"
        for row in rows
        for field in (
            "oos_score_valid_flag",
            "portfolio_candidate_gate",
            "calibration_eligible_flag",
        )
    ):
        issues.append("source research rank has an open production gate")
    if len(rows) != 94:
        issues.append(f"source rank expected 94 rows; found {len(rows)}")
    return issues, rows, manifest


def restore_target(
    *,
    target: Path,
    target_manifest: Path,
    backup_rank: Path | None,
    backup_manifest: Path | None,
) -> None:
    if backup_rank is not None and backup_rank.is_file():
        shutil.copy2(backup_rank, target)
    else:
        target.unlink(missing_ok=True)
    if backup_manifest is not None and backup_manifest.is_file():
        shutil.copy2(backup_manifest, target_manifest)
    else:
        target_manifest.unlink(missing_ok=True)


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    effective_date = parse_iso(args.effective_date, field="--effective-date")
    research_asof = parse_iso(args.research_asof, field="--research-asof")
    if effective_date <= research_asof:
        raise ValueError(
            "Production effective date must be after the sealed research as-of date"
        )
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    source_rank = args.source_rank_table.expanduser().resolve()
    target_rank = (
        args.target_rank_table.expanduser().resolve()
        if args.target_rank_table
        else (
            PROJECT_ROOT
            / "output"
            / "industrials"
            / "defense"
            / "dashboard"
            / effective_date
            / "defense_final_rank_table.csv"
        )
    )
    comparison_manifest = args.comparison_manifest.expanduser().resolve()
    audit_summary = args.audit_summary.expanduser().resolve()
    audit_manifest = args.audit_manifest.expanduser().resolve()
    calibration_summary = args.calibration_summary.expanduser().resolve()
    backtest_summary = args.backtest_summary.expanduser().resolve()
    panel_csv = args.panel_csv.expanduser().resolve()
    evidence_paths = [
        comparison_manifest,
        audit_summary,
        audit_manifest,
        calibration_summary,
        backtest_summary,
        panel_csv,
    ]
    missing = [str(path) for path in evidence_paths if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Activation evidence missing: {missing}")
    audit_payload = load_json(audit_summary)
    if str(audit_payload.get("asof_date") or "") != research_asof:
        raise ValueError("Audit as-of date does not match --research-asof")
    evidence_issues = candidate_evidence_issues(
        comparison_manifest_path=comparison_manifest,
        audit_summary_path=audit_summary,
        audit_manifest_path=audit_manifest,
        calibration_summary_path=calibration_summary,
        backtest_summary_path=backtest_summary,
        score_model_version=args.score_model_version,
    )
    source_issues, source_rows, source_manifest = validate_source_rank(
        rank_path=source_rank,
        effective_date=effective_date,
        score_model_version=args.score_model_version,
    )
    issues = evidence_issues + source_issues
    if issues:
        raise ValueError("Defense specialized activation blocked: " + "; ".join(issues))

    expected_header = defense_final_rank_header(PROJECT_ROOT)
    source_header = read_header(source_rank)
    if source_header != expected_header:
        raise ValueError("Source rank header does not match defense contract")
    panel_rows = read_csv_rows(panel_csv)
    train_start, train_end = train_date_range(panel_rows)
    weights = load_weights(calibration_summary)
    promoted_rows = promote_rows(
        source_rows,
        weights=weights,
        effective_date=effective_date,
        lock_date=research_asof,
        train_start=train_start,
        train_end=train_end,
        score_model_version=args.score_model_version,
        lock_id=args.lock_id,
    )

    output_dir = args.output_dir.expanduser().resolve() / effective_date
    output_dir.mkdir(parents=True, exist_ok=True)
    target_rank.parent.mkdir(parents=True, exist_ok=True)
    target_manifest = target_rank.with_name("defense_final_rank_table_manifest.json")
    backup_rank: Path | None = None
    backup_manifest: Path | None = None
    if target_rank.exists() or target_manifest.exists():
        if not args.allow_overwrite:
            raise FileExistsError(
                f"Target rank artifact already exists: {target_rank}; "
                "use --allow-overwrite for an explicit model transition"
            )
        if target_rank.exists():
            rank_backup_path = (
                output_dir / "defense_final_rank_table_pre_activation.csv"
            )
            shutil.copy2(target_rank, rank_backup_path)
            backup_rank = rank_backup_path
        if target_manifest.exists():
            manifest_backup_path = (
                output_dir
                / "defense_final_rank_table_pre_activation_manifest.json"
            )
            shutil.copy2(target_manifest, manifest_backup_path)
            backup_manifest = manifest_backup_path

    created_at = utc_now()
    promotion_payload: dict[str, Any] = {
        "command": command_line(),
        "lock_id": args.lock_id,
        "research_asof_date": research_asof,
        "effective_date": effective_date,
        "score_model_version": args.score_model_version,
        "scoring_mode": "specialized_v1",
        "weights": weights,
        "source_rank_table": str(source_rank),
        "source_rank_table_sha256": sha256_file(source_rank),
        "source_rank_manifest": str(
            source_rank.with_name("defense_final_rank_table_manifest.json")
        ),
        "source_rank_manifest_sha256": sha256_file(
            source_rank.with_name("defense_final_rank_table_manifest.json")
        ),
        "comparison_manifest": str(comparison_manifest),
        "comparison_manifest_sha256": sha256_file(comparison_manifest),
        "metric_audit_summary": str(audit_summary),
        "metric_audit_summary_sha256": sha256_file(audit_summary),
        "metric_audit_manifest": str(audit_manifest),
        "metric_audit_manifest_sha256": sha256_file(audit_manifest),
        "calibration_summary": str(calibration_summary),
        "calibration_summary_sha256": sha256_file(calibration_summary),
        "backtest_summary": str(backtest_summary),
        "backtest_summary_sha256": sha256_file(backtest_summary),
        "panel_csv": str(panel_csv),
        "panel_csv_sha256": sha256_file(panel_csv),
        "train_start_date": train_start,
        "train_end_date": train_end,
    }
    write_csv_atomic(
        target_rank,
        expected_header,
        [
            {field: row.get(field, "") for field in expected_header}
            for row in promoted_rows
        ],
    )
    rank_manifest = dict(source_manifest)
    rank_manifest.update(
        {
            "artifact": str(target_rank),
            "asof_date": effective_date,
            "rows": len(promoted_rows),
            "sha256": sha256_file(target_rank),
            "model_family": MODEL_FAMILY,
            "score_model_version": args.score_model_version,
            "scoring_mode": "specialized_v1",
            "research_candidate": False,
            "calibration_mode": "production",
            "shadow_only": False,
            "production_promoted": True,
            "production_promotion_status": "production_oos_validated",
            "production_promotion_method": (
                "weekly_pit_panel_validation_selected_holdout_backtest"
            ),
            "production_lock_id": args.lock_id,
            "calibration_lock_date": research_asof,
            "calibration_production_start_date": effective_date,
            "promotion_payload": promotion_payload,
            "sealed_at_utc": created_at,
        }
    )
    write_text_atomic(
        target_manifest,
        json.dumps(rank_manifest, indent=2, sort_keys=True) + "\n",
    )

    report_path = output_dir / "defense_production_promotion_report.csv"
    decision_path = output_dir / "defense_production_promotion_manifest.json"
    candidate_count = sum(
        row.get("portfolio_candidate_gate") == "1" for row in promoted_rows
    )
    report_row = {
        "effective_date": effective_date,
        "research_asof_date": research_asof,
        "status": "pass",
        "lock_id": args.lock_id,
        "score_model_version": args.score_model_version,
        "scoring_mode": "specialized_v1",
        "rows": str(len(promoted_rows)),
        "portfolio_candidate_rows": str(candidate_count),
        "rank_table_path": str(target_rank),
        "rank_manifest_path": str(target_manifest),
        "decision_manifest_path": str(decision_path),
        "issues": "",
    }
    write_csv_atomic(report_path, REPORT_FIELDS, [report_row])
    decision = {
        "artifact_family": "defense_production_promotion",
        "model_family": MODEL_FAMILY,
        "created_at_utc": created_at,
        "status": "pass",
        "promoted": True,
        "asof_date": effective_date,
        "research_asof_date": research_asof,
        "lock_id": args.lock_id,
        "score_model_version": args.score_model_version,
        "scoring_mode": "specialized_v1",
        "rank_table": str(target_rank),
        "rank_table_sha256": sha256_file(target_rank),
        "rank_manifest": str(target_manifest),
        "rank_manifest_sha256": sha256_file(target_manifest),
        "report_csv": str(report_path),
        "report_csv_sha256": sha256_file(report_path),
        "portfolio_candidate_rows": candidate_count,
        "promotion_payload": promotion_payload,
    }
    write_text_atomic(
        decision_path,
        json.dumps(decision, indent=2, sort_keys=True) + "\n",
    )

    registry_path = resolve_path(
        cfg_get(
            config,
            "oos_calibration_standards.families.defense.production_lock_registry_csv",
        ),
        base_dir=base_dir,
    )
    try:
        register_effective_lock(
            registry_path=registry_path,
            lock_id=args.lock_id,
            effective_from=effective_date,
            lock_date=research_asof,
            train_start=train_start,
            train_end=train_end,
            score_model_version=args.score_model_version,
            decision_manifest_path=config_relative_path(
                decision_path,
                base_dir=base_dir,
            ),
            decision_manifest_sha256=sha256_file(decision_path),
            created_at_utc=created_at,
        )
    except BaseException:
        restore_target(
            target=target_rank,
            target_manifest=target_manifest,
            backup_rank=backup_rank,
            backup_manifest=backup_manifest,
        )
        raise

    print(
        f"Activated {args.score_model_version} effective {effective_date}: "
        f"{target_rank}"
    )
    print(f"Wrote lock registry: {registry_path}")
    print(f"Wrote decision: {decision_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
