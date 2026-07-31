from __future__ import annotations

import csv
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from industrials.core.reports import write_csv_atomic
from industrials.defense.research_artifacts import (
    PANEL_SOURCE_CURRENT_UNIVERSE_REPLAY,
    PILLAR_SCORE_FIELDS,
    PRODUCTION_LOCK_REGISTRY_FIELDS,
    PRODUCTION_PROMOTION_METHOD,
    PRODUCTION_PROMOTION_STATUS,
    PRODUCTION_SCORING_CONTRACT_VERSION,
    as_float,
    fmt,
    normalize_weights,
    read_csv_rows,
    sha256_file,
    weighted_score,
)


EXPECTED_AUDIT_ACTIVATION_BLOCKERS = {
    "production_weights_identified",
    "promotable_candidate_activation_state",
}


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return payload


def verify_hashed_file(path: Path, expected_sha256: str, *, label: str) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"{label} not found: {path}")
    actual = sha256_file(path)
    if not expected_sha256 or actual != expected_sha256.lower():
        raise ValueError(
            f"{label} hash mismatch: expected={expected_sha256!r} actual={actual}"
        )


def candidate_evidence_issues(
    *,
    comparison_manifest_path: Path,
    audit_summary_path: Path,
    audit_manifest_path: Path,
    calibration_summary_path: Path,
    backtest_summary_path: Path,
    score_model_version: str,
) -> list[str]:
    issues: list[str] = []
    comparison = load_json(comparison_manifest_path)
    if comparison.get("promotable_evidence") is not True:
        issues.append("comparison manifest is not promotable")
    if comparison.get("failed_gates"):
        issues.append(
            f"comparison manifest failed_gates={comparison['failed_gates']}"
        )
    for artifact_name, sides in (comparison.get("inputs") or {}).items():
        if not isinstance(sides, dict):
            issues.append(f"comparison input {artifact_name} is malformed")
            continue
        for side in ("baseline", "candidate"):
            raw_path = str(sides.get(side) or "")
            expected = str(sides.get(f"{side}_sha256") or "")
            artifact = Path(raw_path)
            if not artifact.is_file():
                issues.append(f"{artifact_name}:{side}:missing")
            elif sha256_file(artifact) != expected:
                issues.append(f"{artifact_name}:{side}:sha256_mismatch")

    audit_manifest = load_json(audit_manifest_path)
    audit_summary = load_json(audit_summary_path)
    summary_file = (audit_manifest.get("files") or {}).get(
        audit_summary_path.name,
        {},
    )
    expected_summary_hash = str(summary_file.get("sha256") or "")
    if not expected_summary_hash or sha256_file(audit_summary_path) != expected_summary_hash:
        issues.append("audit summary hash does not match audit manifest")
    blockers = set(audit_summary.get("blocking_findings") or [])
    if blockers != EXPECTED_AUDIT_ACTIVATION_BLOCKERS:
        issues.append(
            f"audit blockers are not activation-only: {sorted(blockers)}"
        )
    if int((audit_summary.get("finding_counts") or {}).get("critical") or 0) != 0:
        issues.append("audit has critical failures")
    if audit_summary.get("candidate_promotable_evidence") is not True:
        issues.append("audit does not confirm promotable candidate evidence")

    calibration_rows = read_csv_rows(calibration_summary_path)
    backtest_rows = read_csv_rows(backtest_summary_path)
    if len(calibration_rows) != 1:
        issues.append("calibration summary must contain one row")
    if len(backtest_rows) != 1:
        issues.append("backtest summary must contain one row")
    if calibration_rows:
        calibration = calibration_rows[0]
        if str(calibration.get("selection_metric") or "") not in {
            "validation_ic",
            "validation_top_quantile_excess",
        }:
            issues.append("calibration selection metric is not validation-only")
        for field in ("validation_ic", "holdout_ic"):
            value = as_float(calibration.get(field))
            if value is None or value <= 0:
                issues.append(f"calibration {field} is not positive")
        try:
            weights_payload = json.loads(
                str(calibration.get("best_weights_json") or "")
            )
        except json.JSONDecodeError:
            weights_payload = {}
            issues.append("calibration best_weights_json is invalid")
        inactive = {
            field
            for field in str(calibration.get("inactive_pillars") or "").split(";")
            if field
        }
        for field in inactive:
            if abs(float(weights_payload.get(field, 0.0))) > 1e-12:
                issues.append(f"inactive pillar {field} has nonzero weight")
    if backtest_rows:
        holdout_excess = as_float(
            backtest_rows[0].get("holdout_mean_excess_vs_benchmark")
        )
        if holdout_excess is None or holdout_excess <= 0:
            issues.append("backtest holdout excess is not positive")

    candidate_panel_manifest_path = Path(
        str(
            (
                (comparison.get("inputs") or {}).get("panel_manifest") or {}
            ).get("candidate")
            or ""
        )
    )
    if not candidate_panel_manifest_path.is_file():
        issues.append("candidate panel manifest is missing")
    else:
        panel_manifest = load_json(candidate_panel_manifest_path)
        if str(panel_manifest.get("score_model_version") or "") != score_model_version:
            issues.append("candidate panel score_model_version mismatch")
        if str(panel_manifest.get("scoring_mode") or "") != "specialized_v1":
            issues.append("candidate panel scoring_mode mismatch")
        if panel_manifest.get("promotable") is not True:
            issues.append("candidate panel is not promotable")
    return issues


def load_weights(calibration_summary_path: Path) -> dict[str, float]:
    rows = read_csv_rows(calibration_summary_path)
    if len(rows) != 1:
        raise ValueError("calibration summary must contain one row")
    payload = json.loads(str(rows[0].get("best_weights_json") or ""))
    if not isinstance(payload, dict):
        raise ValueError("best_weights_json must be an object")
    missing = sorted(set(PILLAR_SCORE_FIELDS) - set(payload))
    if missing:
        raise ValueError(f"calibration weights missing pillars: {missing}")
    return normalize_weights(
        {str(field): float(value) for field, value in payload.items()}
    )


def row_is_candidate(row: dict[str, str]) -> bool:
    return (
        str(row.get("rank_ready_flag") or "") == "1"
        and str(row.get("model_status") or "").strip().lower() == "complete"
        and as_float(row.get("final_score")) is not None
    )


def noncandidate_reason(row: dict[str, str]) -> str:
    reason = str(
        row.get("review_reason") or row.get("eligibility_reason") or ""
    ).strip()
    if reason and reason.lower() not in {"ok", "shadow_only_oos_pending"}:
        return reason[:240]
    if str(row.get("rank_ready_flag") or "") != "1":
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
    effective_date: str,
    lock_date: str,
    train_start: str,
    train_end: str,
    score_model_version: str,
    lock_id: str,
) -> list[dict[str, str]]:
    scored: list[tuple[dict[str, str], float]] = []
    for source in rows:
        row = dict(source)
        score = weighted_score(row, weights)
        if score is None:
            raise ValueError(
                f"{row.get('ticker')}: no weighted production score available"
            )
        row["final_score"] = fmt(score)
        row["native_score_field"] = "final_score"
        row["native_score_value"] = fmt(score)
        row["portfolio_candidate_score"] = fmt(score)
        row["score_model_version"] = score_model_version
        scored.append((row, score))
    scored.sort(key=lambda item: (-item[1], str(item[0].get("ticker") or "")))
    total = len(scored)
    for rank, (row, _) in enumerate(scored, start=1):
        percentile = (
            100.0 if total == 1 else 100.0 * (total - rank) / (total - 1)
        )
        candidate = row_is_candidate(row)
        reason = "ok" if candidate else noncandidate_reason(row)
        row["final_rank"] = str(rank)
        row["final_percentile"] = fmt(percentile, 4)
        row["scoring_contract_version"] = PRODUCTION_SCORING_CONTRACT_VERSION
        row["calibration_usage"] = "production_oos"
        row["calibration_input_valid_flag"] = "1" if candidate else "0"
        row["calibration_eligible_flag"] = "1" if candidate else "0"
        row["oos_score_valid_flag"] = "1"
        row["oos_score_asof_date"] = effective_date
        row["oos_invalid_reason"] = ""
        row["scoring_weights_frozen_flag"] = "1"
        row["calibration_train_start_date"] = train_start
        row["calibration_train_end_date"] = train_end
        row["calibration_lock_date"] = lock_date
        row["calibration_production_start_date"] = effective_date
        row["calibration_validation_method"] = PRODUCTION_PROMOTION_METHOD
        row["calibration_provenance_version"] = score_model_version
        row["oos_assertion_basis"] = PRODUCTION_PROMOTION_METHOD
        row["portfolio_candidate_gate"] = "1" if candidate else "0"
        row["portfolio_candidate_status"] = (
            "eligible" if candidate else "not_eligible"
        )
        row["portfolio_candidate_reason"] = reason
        row["research_calibration_input_eligible_flag"] = (
            "1" if candidate else "0"
        )
        row["research_calibration_eligible_flag"] = (
            row["research_calibration_input_eligible_flag"]
        )
        row["research_calibration_status"] = (
            PRODUCTION_PROMOTION_STATUS if candidate else "not_eligible"
        )
        row["research_calibration_reason"] = reason
        row["calibration_sample_role"] = "strict_oos"
        row["calibration_status"] = (
            PRODUCTION_PROMOTION_STATUS if candidate else "not_eligible"
        )
        row["calibration_status_reason"] = reason
        row["survivorship_corrected_panel_flag"] = "0"
        row["stage11_calibration_panel_source"] = (
            PANEL_SOURCE_CURRENT_UNIVERSE_REPLAY
        )
        row["stage11_calibration_input_eligible_flag"] = (
            "1" if candidate else "0"
        )
        row["stage11_calibration_input_reason"] = reason
        row["eligibility_reason"] = reason
        row["score_zero_is_missing_flag"] = "0"
        row["model_version"] = f"{score_model_version}:{lock_id}"
    return [row for row, _ in scored]


def read_lock_registry(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if list(reader.fieldnames or []) != PRODUCTION_LOCK_REGISTRY_FIELDS:
            raise ValueError(f"Lock registry header mismatch: {path}")
        return [
            {str(key): str(value or "").strip() for key, value in row.items()}
            for row in reader
        ]


def register_effective_lock(
    *,
    registry_path: Path,
    lock_id: str,
    effective_from: str,
    lock_date: str,
    train_start: str,
    train_end: str,
    score_model_version: str,
    decision_manifest_path: str,
    decision_manifest_sha256: str,
    created_at_utc: str,
) -> None:
    effective = date.fromisoformat(effective_from)
    rows = read_lock_registry(registry_path)
    existing = next(
        (row for row in rows if row["lock_id"] == lock_id),
        None,
    )
    if existing is not None:
        expected = {
            "effective_from": effective_from,
            "lock_date": lock_date,
            "train_start_date": train_start,
            "train_end_date": train_end,
            "scoring_mode": "specialized_v1",
            "score_model_version": score_model_version,
            "decision_manifest_path": decision_manifest_path,
            "decision_manifest_sha256": decision_manifest_sha256,
            "enabled": "1",
        }
        mismatches = {
            field: (existing.get(field, ""), value)
            for field, value in expected.items()
            if existing.get(field, "") != value
        }
        if mismatches:
            raise ValueError(
                f"Existing lock {lock_id!r} conflicts with activation: {mismatches}"
            )
        return
    enabled = [
        row
        for row in rows
        if row["enabled"].lower() in {"1", "true", "yes", "y"}
    ]
    prior = [
        row
        for row in enabled
        if date.fromisoformat(row["effective_from"]) < effective
    ]
    future = [
        row
        for row in enabled
        if date.fromisoformat(row["effective_from"]) >= effective
    ]
    if future:
        raise ValueError(
            f"Cannot insert lock before existing effective locks: "
            f"{[row['lock_id'] for row in future]}"
        )
    if prior:
        previous = max(prior, key=lambda row: row["effective_from"])
        previous_to = previous["effective_to"]
        if previous_to and date.fromisoformat(previous_to) >= effective:
            raise ValueError(
                f"Prior lock {previous['lock_id']} already overlaps {effective_from}"
            )
        previous["effective_to"] = (effective - timedelta(days=1)).isoformat()
    rows.append(
        {
            "lock_id": lock_id,
            "effective_from": effective_from,
            "effective_to": "",
            "lock_date": lock_date,
            "train_start_date": train_start,
            "train_end_date": train_end,
            "scoring_mode": "specialized_v1",
            "score_model_version": score_model_version,
            "validation_method": PRODUCTION_PROMOTION_METHOD,
            "decision_manifest_path": decision_manifest_path,
            "decision_manifest_sha256": decision_manifest_sha256,
            "enabled": "1",
            "created_at_utc": created_at_utc,
        }
    )
    rows.sort(key=lambda row: row["effective_from"])
    write_csv_atomic(registry_path, PRODUCTION_LOCK_REGISTRY_FIELDS, rows)
