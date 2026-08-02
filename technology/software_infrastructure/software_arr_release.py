from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from dedicated_parser.contracts import file_sha256, stable_hash
from technology.software_infrastructure.software_metric_governance import (
    MODEL_FAMILY,
    _row_payload,
)
from technology.software_infrastructure.software_specialized_metrics import (
    validate_policy_payload,
)


ARR_METRIC = "annual_recurring_revenue"
ARR_DEFINITION_VARIANT = "total_arr"
ARR_POLICY_SCHEMA_VERSION = "software_arr_policy_v1"
APPROVED_RELEASE_ID = "software_arr_census_2026_07_30"
APPROVED_POLICY_ID = "software_arr_adjudication_v1"
RESEARCH_RELEASE_ID = "software_arr_historical_research_v1"
RESEARCH_POLICY_ID = "software_arr_historical_research_v1"
HUMAN_APPROVED = "HUMAN_APPROVED"
AUTO_STRICT_RESEARCH_ONLY = "AUTO_STRICT_RESEARCH_ONLY"


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _source_evidence_key(row: dict[str, Any]) -> str:
    return str(
        row.get("source_evidence_key") or row.get("evidence_key") or ""
    )


def _source_document_sha256(row: dict[str, Any]) -> str:
    try:
        provenance = json.loads(str(row.get("provenance_json") or "{}"))
    except json.JSONDecodeError:
        return ""
    return str(provenance.get("document_sha256") or "")


def _period_kind(row: dict[str, Any]) -> str:
    try:
        provenance = json.loads(str(row.get("provenance_json") or "{}"))
    except json.JSONDecodeError:
        provenance = {}
    declared = str(provenance.get("period_kind") or "").lower()
    if declared in {"annual", "quarterly"}:
        return declared
    base_form = str(row.get("form_type") or "").upper().replace("/A", "")
    return "annual" if base_form in {"10-K", "20-F", "40-F"} else "quarterly"


def _number(value: object) -> float:
    return float(str(value or "0").replace(",", ""))


def validate_arr_rows(
    rows: list[dict[str, Any]],
    *,
    source_evidence: dict[str, dict[str, Any]],
    expected_count: int | None = None,
) -> list[str]:
    errors: list[str] = []
    if expected_count is not None and len(rows) != expected_count:
        errors.append(
            f"row_count_mismatch:expected={expected_count}:actual={len(rows)}"
        )
    seen: set[str] = set()
    for index, row in enumerate(rows, start=1):
        key = _source_evidence_key(row)
        prefix = f"row={index}:key={key or '<missing>'}"
        if not key:
            errors.append(f"{prefix}:evidence_key_missing")
            continue
        if key in seen:
            errors.append(f"{prefix}:duplicate_evidence_key")
            continue
        seen.add(key)
        source = source_evidence.get(key)
        if source is None:
            errors.append(f"{prefix}:source_evidence_missing")
            continue
        checks = {
            "ticker": str(row.get("ticker") or ""),
            "accession_number": str(row.get("accession_number") or ""),
            "source_document": str(row.get("source_document") or ""),
            "metric_name": str(row.get("metric_name") or ""),
            "period_end": str(row.get("period_end") or ""),
            "unit": str(row.get("unit") or ""),
        }
        for field, expected in checks.items():
            actual = str(source.get(field) or "")
            if actual != expected:
                errors.append(
                    f"{prefix}:source_{field}_mismatch:"
                    f"expected={expected}:actual={actual}"
                )
        try:
            candidate_value = _number(row.get("candidate_value"))
            source_value = _number(source.get("candidate_value"))
        except ValueError:
            errors.append(f"{prefix}:candidate_value_invalid")
        else:
            tolerance = max(1.0, abs(source_value) * 1e-9)
            if abs(candidate_value - source_value) > tolerance:
                errors.append(f"{prefix}:candidate_value_mismatch")
        if str(row.get("effective_metric") or "") != ARR_METRIC:
            errors.append(f"{prefix}:effective_metric_not_arr")
        if str(row.get("effective_scope") or "") != "consolidated":
            errors.append(f"{prefix}:effective_scope_not_consolidated")
        if str(row.get("effective_unit") or "").upper() != "USD":
            errors.append(f"{prefix}:effective_unit_not_usd")
        if int(str(row.get("calibration_eligible_flag") or "0")) != 1:
            errors.append(f"{prefix}:calibration_eligible_flag_not_one")
        if int(str(row.get("canonical_candidate_flag") or "0")) != 1:
            errors.append(f"{prefix}:canonical_candidate_flag_not_one")
        document_hash = _source_document_sha256(source)
        if len(document_hash) != 64:
            errors.append(f"{prefix}:source_document_not_sha256_sealed")
    return errors


def build_arr_policy(
    *,
    rows: list[dict[str, Any]],
    source_evidence: dict[str, dict[str, Any]],
    release_id: str,
    policy_id: str,
    approved_workbook_path: Path,
    approved_workbook_sha256: str,
    reviewer: str,
    reviewed_at_utc: str,
    governance_status_by_key: dict[str, str] | None = None,
    registry_path: Path | None = None,
    adapter_path: Path | None = None,
) -> dict[str, Any]:
    previous = "0" * 64
    decisions: list[dict[str, Any]] = []
    governance_status_by_key = governance_status_by_key or {}
    for sequence, row in enumerate(rows, start=1):
        key = _source_evidence_key(row)
        source = source_evidence[key]
        decision_name = str(
            row.get("proposal_decision")
            or row.get("decision")
            or "ACCEPTED"
        )
        if decision_name not in {"ACCEPTED", "CORRECTED"}:
            raise ValueError(f"ARR policy cannot materialize {decision_name}: {key}")
        run_id = source.get("run_id")
        payload: dict[str, Any] = {
            "release_id": release_id,
            "sequence": sequence,
            "previous_decision_hash": previous,
            "source_run_ids": (
                [] if run_id in {None, ""} else [int(str(run_id))]
            ),
            "source_adapter_version": str(source.get("adapter_version") or ""),
            "source_parser_release": str(source.get("parser_release") or ""),
            "ticker": str(source.get("ticker") or ""),
            "cik": str(source.get("cik") or ""),
            "accession_number": str(source.get("accession_number") or ""),
            "form_type": str(source.get("form_type") or ""),
            "filing_date": str(source.get("filing_date") or ""),
            "accepted_at": str(source.get("accepted_at") or ""),
            "source_document": str(source.get("source_document") or ""),
            "source_document_sha256": _source_document_sha256(source),
            "source_evidence_key": key,
            "source_row_sha256": stable_hash(_row_payload(source)),
            "source_metric": ARR_METRIC,
            "source_value": _number(source.get("candidate_value")),
            "source_unit": str(source.get("unit") or ""),
            "source_period_start": str(source.get("period_start") or ""),
            "source_period_end": str(source.get("period_end") or ""),
            "decision": decision_name,
            "decision_reason": str(
                row.get("proposal_reason")
                or row.get("decision_reason")
                or "approved_arr_canonical_observation"
            ),
            "effective_metric": ARR_METRIC,
            "effective_value": _number(row.get("effective_value")),
            "effective_unit": "USD",
            "effective_period_start": "",
            "effective_period_end": str(
                row.get("effective_period_end") or ""
            ),
            "effective_scope": "consolidated",
            "period_kind": _period_kind(row),
            "definition_variant": ARR_DEFINITION_VARIANT,
            "calibration_eligible_flag": 1,
            "adjudication_reviewer": reviewer,
            "adjudicated_at_utc": reviewed_at_utc,
            "review_notes": str(
                row.get("human_review_detail")
                or row.get("review_notes")
                or ""
            ),
            "approved_workbook_sha256": approved_workbook_sha256,
            "governance_status": governance_status_by_key.get(
                key, HUMAN_APPROVED
            ),
            "production_use_prohibited_flag": int(
                governance_status_by_key.get(key, HUMAN_APPROVED)
                == AUTO_STRICT_RESEARCH_ONLY
            ),
        }
        payload["decision_hash"] = stable_hash(payload)
        previous = str(payload["decision_hash"])
        decisions.append(payload)
    counts = Counter(str(row["decision"]) for row in decisions)
    governance_counts = Counter(
        str(row["governance_status"]) for row in decisions
    )
    policy: dict[str, Any] = {
        "policy_schema_version": ARR_POLICY_SCHEMA_VERSION,
        "policy_id": policy_id,
        "policy_version": policy_id,
        "release_id": release_id,
        "model_family": MODEL_FAMILY,
        "approved_by": reviewer,
        "approved_at_utc": reviewed_at_utc,
        "approved_workbook_path": str(approved_workbook_path.resolve()),
        "approved_workbook_sha256": approved_workbook_sha256,
        "decision_count": len(decisions),
        "decision_counts": dict(sorted(counts.items())),
        "governance_counts": dict(sorted(governance_counts.items())),
        "chain_root_sha256": previous,
        "production_weight_modified_flag": 0,
        "measurement_only_flag": 1,
        "decisions": decisions,
    }
    if registry_path is not None:
        policy["registry_path"] = str(registry_path.resolve())
        policy["registry_sha256"] = file_sha256(registry_path)
    if adapter_path is not None:
        policy["adapter_path"] = str(adapter_path.resolve())
        policy["adapter_sha256"] = file_sha256(adapter_path)
    return validate_policy_payload(
        policy, source=str(approved_workbook_path)
    )


def source_keys(rows: Iterable[dict[str, Any]]) -> list[str]:
    return [_source_evidence_key(row) for row in rows]
