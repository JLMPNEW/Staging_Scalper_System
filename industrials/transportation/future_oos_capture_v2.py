"""Semantic contract binding for governing-v7 Transportation captures."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Mapping

import yaml

from future_only_evidence.protocol import (
    TrustedReceiptVerifier,
    canonical_sha256,
    file_sha256,
)

from .future_oos_protocol_v1 import (
    REQUIRED_CAPTURE_ROLES,
    capture_signal as _capture_v1,
    validate_fresh_sources,
)


def _json(path: Path, *, label: str) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    return dict(payload)


def validate_governing_contracts(
    *,
    v8_policy_path: Path,
    v7_research_decision_path: Path,
) -> dict[str, Any]:
    policy = yaml.safe_load(Path(v8_policy_path).read_text(encoding="utf-8"))
    if not isinstance(policy, dict):
        raise ValueError("v8 policy must be a mapping")
    required_policy = {
        "policy_version": "transportation_subgroup_score_policy_v8",
        "model_version": "transportation_hierarchical_subgroup_score_v8",
        "effective_from": "2026-08-21",
        "evidence_class": "outcome_blind_economic_specification",
    }
    for field, expected in required_policy.items():
        if str(policy.get(field)) != expected:
            raise ValueError(f"governing v8 policy changed {field}")
    controls = policy.get("controls")
    governance = policy.get("governance")
    if not isinstance(controls, dict) or not isinstance(governance, dict):
        raise ValueError("v8 controls/governance are missing")
    false_controls = (
        "group_weights_use_outcomes",
        "component_weights_use_outcomes",
        "historical_results_can_authorize_production",
    )
    if any(controls.get(field) is not False for field in false_controls):
        raise ValueError("v8 outcome-blind controls changed")
    if governance.get("cohort_promotion_independent") is not True:
        raise ValueError("v8 independent cohort promotion changed")
    if governance.get("group_failure_cannot_be_hidden_by_aggregate_result") is not True:
        raise ValueError("v8 group-failure isolation changed")
    if governance.get("production_activation_authorized") is not False:
        raise ValueError("v8 policy cannot self-authorize production")

    decision = _json(v7_research_decision_path, label="v7 research decision")
    if decision.get("production_activation_authorized") is not False:
        raise ValueError("v7 design decision cannot self-authorize production")
    specification = decision.get("research_specification")
    if not isinstance(specification, dict):
        raise ValueError("v7 research specification is missing")
    if specification.get("contract_version") != "transportation_v7_research_specification_v1":
        raise ValueError("unsupported governing v7 research contract")
    if specification.get("first_future_signal_date") != "2026-08-24":
        raise ValueError("v7 first future signal date changed")
    gate = specification.get("promotion_gate")
    expected_gate = {
        "minimum_future_21_session_non_overlapping_outcomes": 12,
        "minimum_future_63_session_non_overlapping_outcomes": 4,
        "minimum_ic": 0.0,
        "minimum_top_minus_cohort_net": 0.0,
        "minimum_top_minus_bottom_gross": 0.0,
        "minimum_hit_rate": 0.55,
        "cohort_isolation_required": True,
        "independent_promotion_readiness_audit_required": True,
    }
    if not isinstance(gate, dict) or any(gate.get(field) != expected for field, expected in expected_gate.items()):
        raise ValueError("governing v7 future gate changed")
    return {
        "v8_policy_sha256": file_sha256(v8_policy_path),
        "v7_research_decision_sha256": file_sha256(v7_research_decision_path),
        "v7_future_gate_sha256": canonical_sha256(expected_gate),
    }


def _membership_rows(path: Path) -> tuple[str, list[dict[str, Any]]]:
    resolved = Path(path)
    if resolved.suffix.lower() == ".json":
        payload = _json(resolved, label="future membership snapshot")
        if payload.get("schema_version") != "transportation_future_membership_snapshot_v1":
            raise ValueError("unsupported future membership snapshot")
        rows = payload.get("rows")
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise ValueError("future membership rows are invalid")
        return str(payload.get("asof_date") or "")[:10], [dict(row) for row in rows]
    with resolved.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = [dict(row) for row in csv.DictReader(handle)]
    asofs = {str(row.get("asof_date") or "")[:10] for row in rows}
    if len(asofs) != 1:
        raise ValueError("future membership CSV must have one exact asof date")
    return next(iter(asofs)), rows


def validate_membership_snapshot(
    *,
    asof_date: str,
    membership_path: Path,
    score_path: Path,
    rank_path: Path,
    source_manifest_path: Path,
) -> dict[str, Any]:
    score_rows, _, _ = validate_fresh_sources(
        asof_date=asof_date,
        capture_date=asof_date,
        score_path=score_path,
        rank_path=rank_path,
        source_manifest_path=source_manifest_path,
    )
    membership_asof, rows = _membership_rows(membership_path)
    if membership_asof != str(asof_date)[:10]:
        raise ValueError("future membership snapshot asof mismatch")
    index: dict[str, dict[str, Any]] = {}
    for row in rows:
        ticker = str(row.get("ticker") or "").strip().upper()
        if not ticker or ticker in index:
            raise ValueError("future membership has blank/duplicate ticker")
        index[ticker] = row
    score_index = {str(row["ticker"]).strip().upper(): row for row in score_rows}
    if set(index) != set(score_index):
        raise ValueError("future membership ticker census does not exactly match v8 score")
    for ticker, score in score_index.items():
        membership = index[ticker]
        if str(membership.get("sleeve_id")) != str(score["calibration_cohort"]):
            raise ValueError(f"{ticker}: membership sleeve mismatch")
        if str(membership.get("group_id")) != str(score["v8_group_id"]):
            raise ValueError(f"{ticker}: membership group mismatch")
        if str(membership.get("eligible_at_entry_flag")) not in {"1", "True", "true"}:
            raise ValueError(f"{ticker}: membership is not eligible at entry")
    return {
        "asof_date": membership_asof,
        "ticker_count": len(index),
        "ticker_census_sha256": canonical_sha256(sorted(index)),
        "membership_snapshot_sha256": file_sha256(membership_path),
    }


def capture_signal(
    *,
    asof_date: str,
    capture_source_paths: Mapping[str, Path],
    expected_capture_source_sha256: Mapping[str, str],
    trusted_capture_receipt_path: Path,
    expected_trusted_capture_receipt_sha256: str,
    trusted_capture_receipt_verifier: TrustedReceiptVerifier | None,
) -> dict[str, Any]:
    if set(capture_source_paths) != REQUIRED_CAPTURE_ROLES:
        raise ValueError("Transportation capture source roles do not exactly match the contract")
    governance = validate_governing_contracts(
        v8_policy_path=capture_source_paths["v8_policy"],
        v7_research_decision_path=capture_source_paths["v7_research_decision"],
    )
    membership = validate_membership_snapshot(
        asof_date=asof_date,
        membership_path=capture_source_paths["membership_snapshot"],
        score_path=capture_source_paths["canonical_v8_score"],
        rank_path=capture_source_paths["canonical_v8_rank"],
        source_manifest_path=capture_source_paths["source_manifest"],
    )
    payload = _capture_v1(
        asof_date=asof_date,
        capture_source_paths=capture_source_paths,
        expected_capture_source_sha256=expected_capture_source_sha256,
        trusted_capture_receipt_path=trusted_capture_receipt_path,
        expected_trusted_capture_receipt_sha256=expected_trusted_capture_receipt_sha256,
        trusted_capture_receipt_verifier=trusted_capture_receipt_verifier,
    )
    payload.pop("capture_id")
    payload.pop("payload_sha256")
    payload.update(
        domain_schema_version="transportation_future_only_signal_capture_v2",
        governing_contract_audit=governance,
        membership_audit=membership,
    )
    payload["capture_id"] = canonical_sha256(payload)
    payload["payload_sha256"] = canonical_sha256(payload)
    return payload


__all__ = [
    "capture_signal",
    "validate_governing_contracts",
    "validate_membership_snapshot",
]
