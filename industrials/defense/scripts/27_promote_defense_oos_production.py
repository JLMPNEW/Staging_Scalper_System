#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
from datetime import date
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.logging_utils import configure_utc_logging  # noqa: E402
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.defense.research_artifacts import (  # noqa: E402
    MODEL_FAMILY,
    PANEL_SOURCE_CURRENT_UNIVERSE_REPLAY,
    PANEL_SOURCE_SURVIVORSHIP_CORRECTED,
    as_float,
    command_line,
    fmt,
    normalize_weights,
    read_csv_rows,
    sha256_file,
    utc_now,
    weighted_score,
)


PROMOTION_STATUS = "production_oos_validated"
PROMOTION_METHOD = "weekly_pit_panel_validation_ic_holdout_backtest"
PRODUCTION_SCORING_CONTRACT_VERSION = "tech_family_final_rank_table_v1_production"
DEFAULT_ASOF = "2026-07-02"
REPORT_FIELDS = [
    "asof_date",
    "status",
    "promoted",
    "rows",
    "portfolio_candidate_rows",
    "research_eligible_rows",
    "rank_table_path",
    "manifest_path",
    "validation_ic",
    "holdout_ic",
    "holdout_mean_excess_vs_benchmark",
    "overlapping_forward_windows_flag",
    "overlap_warning_accepted",
    "issues",
]


def parse_args() -> argparse.Namespace:
    default_rank = (
        PROJECT_ROOT
        / "output"
        / "industrials"
        / "defense"
        / "dashboard"
        / DEFAULT_ASOF
        / "defense_final_rank_table.csv"
    )
    default_panel = (
        PROJECT_ROOT
        / "output"
        / "industrials"
        / "defense"
        / "stage8"
        / "oos_calibration_panel_weekly"
        / "defense_oos_calibration_panel.csv"
    )
    default_panel_check = (
        PROJECT_ROOT
        / "output"
        / "industrials"
        / "defense"
        / "stage8"
        / "weekly_oos_calibration_artifact_promotion_check_report.csv"
    )
    default_snapshot_readiness = (
        PROJECT_ROOT
        / "output"
        / "industrials"
        / "defense"
        / "stage8"
        / "weekly_snapshot_history_promotion_readiness_report.csv"
    )
    default_calibration = (
        PROJECT_ROOT
        / "output"
        / "industrials"
        / "defense"
        / "stage8"
        / "optuna_calibration_weekly"
        / "defense_optuna_calibration_summary.csv"
    )
    default_backtest = (
        PROJECT_ROOT
        / "output"
        / "industrials"
        / "defense"
        / "stage9"
        / "score_backtest_weekly"
        / "defense_score_backtest_summary.csv"
    )
    default_output = PROJECT_ROOT / "output" / "industrials" / "defense" / "stage10" / "production_promotion"
    parser = argparse.ArgumentParser(description="Promote a defense dashboard rank table from shadow to production OOS.")
    parser.add_argument("--asof", default=DEFAULT_ASOF)
    parser.add_argument("--rank-table", type=Path, default=default_rank)
    parser.add_argument("--panel-csv", type=Path, default=default_panel)
    parser.add_argument("--panel-promotion-check", type=Path, default=default_panel_check)
    parser.add_argument("--snapshot-readiness-report", type=Path, default=default_snapshot_readiness)
    parser.add_argument("--calibration-summary-csv", type=Path, default=default_calibration)
    parser.add_argument("--backtest-summary-csv", type=Path, default=default_backtest)
    parser.add_argument("--output-dir", type=Path, default=default_output)
    parser.add_argument("--min-validation-ic", type=float, default=0.0)
    parser.add_argument("--min-holdout-ic", type=float, default=0.0)
    parser.add_argument("--min-holdout-excess", type=float, default=0.0)
    parser.add_argument(
        "--accept-overlap-warning",
        action="store_true",
        help="Allow promotion when the weekly 63-day backtest windows overlap; the report records the waiver.",
    )
    parser.add_argument("--allow-overwrite", action="store_true")
    return parser.parse_args()


def read_header(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(next(csv.reader(handle)))


def one_row(path: Path, *, label: str) -> dict[str, str]:
    rows = read_csv_rows(path)
    if not rows:
        raise ValueError(f"{label} is empty: {path}")
    return rows[0]


def require_file(path: Path, *, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"{label} not found: {resolved}")
    return resolved


def flag(raw: object) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "y"}


def parse_iso(raw: str, *, field: str) -> str:
    try:
        return date.fromisoformat(raw.strip()).isoformat()
    except ValueError as exc:
        raise ValueError(f"{field} must be YYYY-MM-DD, got {raw!r}") from exc


def load_weights(calibration_summary: dict[str, str]) -> dict[str, float]:
    raw = str(calibration_summary.get("best_weights_json") or "").strip()
    if not raw:
        raise ValueError("calibration summary missing best_weights_json")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"calibration best_weights_json is invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("calibration best_weights_json must decode to an object")
    return normalize_weights({str(key): float(value) for key, value in payload.items()})


def panel_date_range(panel_rows: list[dict[str, str]], split_name: str) -> tuple[str, str]:
    dates = sorted({str(row.get("asof_date") or "") for row in panel_rows if row.get("split_name") == split_name})
    return (dates[0], dates[-1]) if dates else ("", "")


def promotion_issues(
    *,
    asof: str,
    panel_check: dict[str, str],
    snapshot_readiness_rows: list[dict[str, str]],
    calibration_summary: dict[str, str],
    backtest_summary: dict[str, str],
    accept_overlap_warning: bool,
    min_validation_ic: float,
    min_holdout_ic: float,
    min_holdout_excess: float,
) -> list[str]:
    issues: list[str] = []
    if str(panel_check.get("status") or "") != "pass" or str(panel_check.get("promotable") or "") != "1":
        issues.append(f"panel promotion check not promotable: {panel_check.get('issues') or panel_check}")
    bad_snapshots = [
        str(row.get("asof_date") or "")
        for row in snapshot_readiness_rows
        if str(row.get("status") or "") != "pass"
    ]
    if bad_snapshots:
        issues.append(f"weekly snapshot readiness failures: {bad_snapshots[:10]}")
    if not any(str(row.get("asof_date") or "") == asof for row in snapshot_readiness_rows):
        issues.append(f"snapshot readiness report does not include promotion asof {asof}")
    validation_ic = as_float(calibration_summary.get("validation_ic"))
    holdout_ic = as_float(calibration_summary.get("holdout_ic"))
    if validation_ic is None or validation_ic <= min_validation_ic:
        issues.append(f"validation_ic={validation_ic} is not above threshold {min_validation_ic}")
    if holdout_ic is None or holdout_ic <= min_holdout_ic:
        issues.append(f"holdout_ic={holdout_ic} is not above threshold {min_holdout_ic}")
    if str(calibration_summary.get("selection_metric") or "") != "validation_ic":
        issues.append(f"calibration selection_metric is not validation_ic: {calibration_summary.get('selection_metric')}")
    holdout_excess = as_float(backtest_summary.get("holdout_mean_excess_vs_benchmark"))
    if holdout_excess is None or holdout_excess <= min_holdout_excess:
        issues.append(f"holdout_mean_excess_vs_benchmark={holdout_excess} is not above threshold {min_holdout_excess}")
    selected_excess = as_float(backtest_summary.get("selected_mean_excess_vs_benchmark"))
    if selected_excess is None or selected_excess <= 0.0:
        issues.append(f"selected_mean_excess_vs_benchmark={selected_excess} is not positive")
    if flag(backtest_summary.get("overlapping_forward_windows_flag")) and not accept_overlap_warning:
        issues.append("weekly backtest windows overlap; rerun with --accept-overlap-warning to promote with audit waiver")
    return issues


def production_score(row: dict[str, str], weights: dict[str, float]) -> float:
    score = weighted_score(row, weights)
    if score is None:
        fallback = as_float(row.get("final_score"))
        if fallback is None:
            raise ValueError(f"{row.get('ticker')}: missing production score and fallback final_score")
        return max(0.0, min(100.0, fallback))
    return score


def row_is_candidate(row: dict[str, str]) -> bool:
    return (
        flag(row.get("rank_ready_flag"))
        and str(row.get("model_status") or "").strip().lower() == "complete"
        and as_float(row.get("final_score")) is not None
    )


def noncandidate_reason(row: dict[str, str]) -> str:
    reason = str(row.get("review_reason") or row.get("eligibility_reason") or "").strip()
    if reason and reason.lower() not in {"ok", "shadow_only_oos_pending"}:
        return reason[:240]
    if not flag(row.get("rank_ready_flag")):
        return "not_rank_ready"
    if str(row.get("model_status") or "").strip().lower() != "complete":
        return "model_incomplete"
    if as_float(row.get("final_score")) is None:
        return "missing_score"
    return "not_portfolio_candidate"


def promote_rows(
    rows: list[dict[str, str]],
    *,
    weights: dict[str, float],
    asof: str,
    train_start: str,
    train_end: str,
    provenance_version: str,
) -> list[dict[str, str]]:
    scored: list[tuple[dict[str, str], float]] = []
    for row in rows:
        score = production_score(row, weights)
        row["final_score"] = fmt(score)
        row["native_score_field"] = "final_score"
        row["native_score_value"] = fmt(score)
        row["portfolio_candidate_score"] = fmt(score)
        scored.append((row, score))
    scored.sort(key=lambda item: (-item[1], str(item[0].get("ticker") or "")))
    total = len(scored)
    for rank, (row, _) in enumerate(scored, start=1):
        percentile = 100.0 if total == 1 else 100.0 * (total - rank) / (total - 1)
        candidate = row_is_candidate(row)
        reason = "ok" if candidate else noncandidate_reason(row)
        oos_valid = as_float(row.get("final_score")) is not None
        row["final_rank"] = str(rank)
        row["final_percentile"] = fmt(percentile, 4)
        row["scoring_contract_version"] = PRODUCTION_SCORING_CONTRACT_VERSION
        row["calibration_usage"] = "production_oos"
        row["calibration_input_valid_flag"] = "1" if candidate else "0"
        row["calibration_eligible_flag"] = "1" if candidate else "0"
        row["oos_score_valid_flag"] = "1" if oos_valid else "0"
        row["oos_score_asof_date"] = asof if oos_valid else ""
        row["oos_invalid_reason"] = "" if oos_valid else "missing_production_score"
        row["scoring_weights_frozen_flag"] = "1"
        row["calibration_train_start_date"] = train_start
        row["calibration_train_end_date"] = train_end
        row["calibration_lock_date"] = asof
        row["calibration_production_start_date"] = asof
        row["calibration_validation_method"] = PROMOTION_METHOD
        row["calibration_provenance_version"] = provenance_version
        row["oos_assertion_basis"] = PROMOTION_METHOD
        row["portfolio_candidate_gate"] = "1" if candidate else "0"
        row["portfolio_candidate_status"] = "eligible" if candidate else "not_eligible"
        row["portfolio_candidate_reason"] = reason
        row["research_calibration_input_eligible_flag"] = "1" if candidate else "0"
        row["research_calibration_eligible_flag"] = row["research_calibration_input_eligible_flag"]
        row["research_calibration_status"] = PROMOTION_STATUS if candidate else "not_eligible"
        row["research_calibration_reason"] = reason
        row["calibration_sample_role"] = "strict_oos" if oos_valid else "excluded"
        row["calibration_status"] = PROMOTION_STATUS if candidate else "not_eligible"
        row["calibration_status_reason"] = reason
        row["survivorship_corrected_panel_flag"] = "0"
        row["stage11_calibration_panel_source"] = PANEL_SOURCE_CURRENT_UNIVERSE_REPLAY
        row["stage11_calibration_input_eligible_flag"] = "1" if candidate else "0"
        row["stage11_calibration_input_reason"] = reason
        row["eligibility_reason"] = reason
        row["score_zero_is_missing_flag"] = "0"
    return [row for row, _ in scored]


def write_manifest(
    path: Path,
    *,
    asof: str,
    rows: int,
    promotion_payload: dict[str, Any],
    source_manifest: dict[str, Any],
) -> Path:
    manifest = dict(source_manifest)
    manifest.update(
        {
            "artifact": str(path),
            "asof_date": asof,
            "rows": rows,
            "sha256": sha256_file(path),
            "model_family": MODEL_FAMILY,
            "shadow_only": False,
            "production_promoted": True,
            "production_promotion_status": PROMOTION_STATUS,
            "production_promotion_method": PROMOTION_METHOD,
            "promotion_payload": promotion_payload,
            "sealed_at_utc": utc_now(),
        }
    )
    manifest_path = path.with_name("defense_final_rank_table_manifest.json")
    write_text_atomic(manifest_path, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest_path


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    asof = parse_iso(args.asof, field="--asof")
    rank_table = require_file(args.rank_table, label="rank table")
    panel_csv = require_file(args.panel_csv, label="panel csv")
    panel_check_csv = require_file(args.panel_promotion_check, label="panel promotion check")
    snapshot_report_csv = require_file(args.snapshot_readiness_report, label="snapshot readiness report")
    calibration_csv = require_file(args.calibration_summary_csv, label="calibration summary")
    backtest_csv = require_file(args.backtest_summary_csv, label="backtest summary")
    manifest_path = rank_table.with_name("defense_final_rank_table_manifest.json")
    require_file(manifest_path, label="rank table manifest")

    header = read_header(rank_table)
    rows = read_csv_rows(rank_table)
    if not rows:
        raise ValueError(f"rank table is empty: {rank_table}")
    bad_asof = sorted({str(row.get("asof_date") or "") for row in rows if str(row.get("asof_date") or "") != asof})
    if bad_asof:
        raise ValueError(f"rank table has rows outside --asof {asof}: {bad_asof[:10]}")

    panel_rows = read_csv_rows(panel_csv)
    panel_check = one_row(panel_check_csv, label="panel promotion check")
    snapshot_rows = read_csv_rows(snapshot_report_csv)
    calibration_summary = one_row(calibration_csv, label="calibration summary")
    backtest_summary = one_row(backtest_csv, label="backtest summary")
    issues = promotion_issues(
        asof=asof,
        panel_check=panel_check,
        snapshot_readiness_rows=snapshot_rows,
        calibration_summary=calibration_summary,
        backtest_summary=backtest_summary,
        accept_overlap_warning=args.accept_overlap_warning,
        min_validation_ic=args.min_validation_ic,
        min_holdout_ic=args.min_holdout_ic,
        min_holdout_excess=args.min_holdout_excess,
    )
    if issues:
        raise ValueError("Defense production promotion blocked: " + "; ".join(issues))

    train_start, train_end = panel_date_range(panel_rows, "train")
    weights = load_weights(calibration_summary)
    output_dir = args.output_dir.expanduser().resolve() / asof
    output_dir.mkdir(parents=True, exist_ok=True)
    pre_promotion_copy = output_dir / "defense_final_rank_table_pre_promotion.csv"
    if pre_promotion_copy.exists() and not args.allow_overwrite:
        raise FileExistsError(f"Promotion artifact already exists: {pre_promotion_copy}; use --allow-overwrite")
    shutil.copy2(rank_table, pre_promotion_copy)
    promoted_rows = promote_rows(
        rows,
        weights=weights,
        asof=asof,
        train_start=train_start,
        train_end=train_end,
        provenance_version=PROMOTION_STATUS,
    )
    write_csv_atomic(rank_table, header, [{field: row.get(field, "") for field in header} for row in promoted_rows])

    source_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    promotion_payload: dict[str, Any] = {
        "command": command_line(),
        "promoted_at_utc": utc_now(),
        "accepted_overlap_warning": bool(args.accept_overlap_warning),
        "source_shadow_rank_table_sha256": sha256_file(pre_promotion_copy),
        "panel_csv": str(panel_csv),
        "panel_csv_sha256": sha256_file(panel_csv),
        "panel_promotion_check_csv": str(panel_check_csv),
        "panel_promotion_check_sha256": sha256_file(panel_check_csv),
        "snapshot_readiness_report_csv": str(snapshot_report_csv),
        "snapshot_readiness_report_sha256": sha256_file(snapshot_report_csv),
        "calibration_summary_csv": str(calibration_csv),
        "calibration_summary_sha256": sha256_file(calibration_csv),
        "backtest_summary_csv": str(backtest_csv),
        "backtest_summary_sha256": sha256_file(backtest_csv),
        "selection_metric": calibration_summary.get("selection_metric", ""),
        "validation_ic": calibration_summary.get("validation_ic", ""),
        "holdout_ic": calibration_summary.get("holdout_ic", ""),
        "holdout_mean_excess_vs_benchmark": backtest_summary.get("holdout_mean_excess_vs_benchmark", ""),
        "overlapping_forward_windows_flag": backtest_summary.get("overlapping_forward_windows_flag", ""),
        "weights": weights,
        "stage11_calibration_panel_source_for_dashboard_rows": PANEL_SOURCE_CURRENT_UNIVERSE_REPLAY,
        "stage11_calibration_panel_source_for_research_panel": PANEL_SOURCE_SURVIVORSHIP_CORRECTED,
    }
    new_manifest_path = write_manifest(
        rank_table,
        asof=asof,
        rows=len(promoted_rows),
        promotion_payload=promotion_payload,
        source_manifest=source_manifest,
    )
    report_path = output_dir / "defense_production_promotion_report.csv"
    decision_path = output_dir / "defense_production_promotion_manifest.json"
    candidate_count = sum(1 for row in promoted_rows if row.get("portfolio_candidate_gate") == "1")
    research_count = sum(1 for row in promoted_rows if row.get("stage11_calibration_input_eligible_flag") == "1")
    report_row = {
        "asof_date": asof,
        "status": "pass",
        "promoted": "1",
        "rows": str(len(promoted_rows)),
        "portfolio_candidate_rows": str(candidate_count),
        "research_eligible_rows": str(research_count),
        "rank_table_path": str(rank_table),
        "manifest_path": str(new_manifest_path),
        "validation_ic": str(calibration_summary.get("validation_ic") or ""),
        "holdout_ic": str(calibration_summary.get("holdout_ic") or ""),
        "holdout_mean_excess_vs_benchmark": str(backtest_summary.get("holdout_mean_excess_vs_benchmark") or ""),
        "overlapping_forward_windows_flag": str(backtest_summary.get("overlapping_forward_windows_flag") or ""),
        "overlap_warning_accepted": "1" if args.accept_overlap_warning else "0",
        "issues": "",
    }
    write_csv_atomic(report_path, REPORT_FIELDS, [report_row])
    decision_payload = {
        "artifact_family": "defense_production_promotion",
        "model_family": MODEL_FAMILY,
        "created_at_utc": utc_now(),
        "status": "pass",
        "promoted": True,
        "asof_date": asof,
        "rank_table": str(rank_table),
        "rank_table_sha256": sha256_file(rank_table),
        "rank_manifest": str(new_manifest_path),
        "rank_manifest_sha256": sha256_file(new_manifest_path),
        "report_csv": str(report_path),
        "report_csv_sha256": sha256_file(report_path),
        "portfolio_candidate_rows": candidate_count,
        "research_eligible_rows": research_count,
        "promotion_payload": promotion_payload,
    }
    write_text_atomic(decision_path, json.dumps(decision_payload, indent=2, sort_keys=True) + "\n")
    print(f"Promoted defense production rank table: {rank_table}")
    print(f"Wrote {new_manifest_path}")
    print(f"Wrote {report_path}")
    print(f"Wrote {decision_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
