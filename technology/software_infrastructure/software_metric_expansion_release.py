from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from dedicated_parser.contracts import file_sha256, stable_hash
from technology.software_infrastructure.software_metric_governance import (
    MODEL_FAMILY,
    _row_payload,
)
from technology.software_infrastructure.software_metric_proposed_adjudication import (
    PROPOSAL_NOTICE,
)
from technology.software_infrastructure.software_metric_review import (
    DECISION_FIELDS,
    SOURCE_FIELDS,
)


RELEASE_ID = "software_metrics_v3"
POLICY_VERSION = "software_metrics_adjudication_v3"
RELEASE_SCHEMA_VERSION = "software_metric_expansion_release_v1"


def approval_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def promote_proposal_rows(
    *,
    proposal_rows: list[dict[str, Any]],
    official_rows: list[dict[str, Any]],
    reviewer: str,
    reviewed_at_utc: str,
) -> list[dict[str, Any]]:
    reviewer = reviewer.strip()
    if not reviewer:
        raise ValueError("reviewer is required")
    proposal_by_key = {
        str(row.get("source_evidence_key") or ""): row
        for row in proposal_rows
    }
    official_by_key = {
        str(row.get("source_evidence_key") or ""): row
        for row in official_rows
    }
    if "" in proposal_by_key or "" in official_by_key:
        raise ValueError("Review rows require source_evidence_key")
    if len(proposal_by_key) != len(proposal_rows):
        raise ValueError("Proposal contains duplicate evidence keys")
    if len(official_by_key) != len(official_rows):
        raise ValueError("Official review contains duplicate evidence keys")
    if set(proposal_by_key) != set(official_by_key):
        raise ValueError("Proposal and official review evidence sets differ")

    promoted: list[dict[str, Any]] = []
    immutable_fields = (*SOURCE_FIELDS, "source_document_sha256", "review_source_sha256")
    for official in official_rows:
        key = str(official["source_evidence_key"])
        proposal = proposal_by_key[key]
        if str(proposal.get("proposal_status") or "") != (
            "PENDING_HUMAN_APPROVAL"
        ):
            raise ValueError(f"Unexpected proposal status for {key}")
        for field in immutable_fields:
            if str(proposal.get(field) or "").strip() != str(
                official.get(field) or ""
            ).strip():
                raise ValueError(
                    f"Proposal changed immutable review field {field}: {key}"
                )
        row = dict(official)
        row.update(
            {
                field: str(proposal.get(field) or "")
                for field in DECISION_FIELDS
            }
        )
        row["reviewer"] = reviewer
        row["reviewed_at_utc"] = reviewed_at_utc
        notes = str(row.get("review_notes") or "").strip()
        if notes.startswith(PROPOSAL_NOTICE):
            notes = notes[len(PROPOSAL_NOTICE) :].strip()
        row["review_notes"] = notes
        promoted.append(row)
    return promoted


def _numeric_or_text(value: object) -> float | str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return float(text)
    except ValueError:
        return text


def build_expansion_decisions(
    *,
    approved_rows: list[dict[str, Any]],
    source_evidence: dict[str, dict[str, Any]],
    first_sequence: int,
    previous_decision_hash: str,
    approved_workbook_sha256: str,
) -> list[dict[str, Any]]:
    decisions: list[dict[str, Any]] = []
    previous = previous_decision_hash
    for offset, review in enumerate(approved_rows):
        key = str(review["source_evidence_key"])
        source = source_evidence.get(key)
        if source is None:
            raise ValueError(f"Missing source evidence for {key}")
        run_id = source.get("run_id")
        source_run_ids = (
            []
            if run_id is None or str(run_id).strip() == ""
            else [int(str(run_id))]
        )
        payload: dict[str, Any] = {
            "release_id": RELEASE_ID,
            "sequence": first_sequence + offset,
            "previous_decision_hash": previous,
            "source_run_ids": source_run_ids,
            "source_adapter_version": str(source.get("adapter_version") or ""),
            "source_parser_release": str(source.get("parser_release") or ""),
            "ticker": str(source.get("ticker") or ""),
            "cik": str(source.get("cik") or ""),
            "accession_number": str(source.get("accession_number") or ""),
            "form_type": str(source.get("form_type") or ""),
            "filing_date": str(source.get("filing_date") or ""),
            "accepted_at": str(source.get("accepted_at") or ""),
            "source_document": str(source.get("source_document") or ""),
            "source_document_sha256": str(
                review.get("source_document_sha256") or ""
            ),
            "source_evidence_key": key,
            "source_row_sha256": str(review.get("source_row_sha256") or ""),
            "source_metric": str(review.get("source_metric") or ""),
            "source_value": _numeric_or_text(review.get("candidate_value")),
            "source_unit": str(review.get("unit") or ""),
            "source_period_start": str(source.get("period_start") or ""),
            "source_period_end": str(review.get("period_end") or ""),
            "decision": str(review.get("decision") or ""),
            "decision_reason": str(review.get("decision_reason") or ""),
            "effective_metric": str(review.get("effective_metric") or ""),
            "effective_value": _numeric_or_text(review.get("effective_value")),
            "effective_unit": str(review.get("effective_unit") or ""),
            "effective_period_start": str(
                review.get("effective_period_start") or ""
            ),
            "effective_period_end": str(
                review.get("effective_period_end") or ""
            ),
            "effective_scope": str(review.get("effective_scope") or ""),
            "period_kind": str(review.get("period_kind") or ""),
            "definition_variant": str(review.get("definition_variant") or ""),
            "calibration_eligible_flag": int(
                str(review.get("calibration_eligible_flag") or "0")
            ),
            "adjudication_reviewer": str(review.get("reviewer") or ""),
            "adjudicated_at_utc": str(review.get("reviewed_at_utc") or ""),
            "review_notes": str(review.get("review_notes") or ""),
            "approved_workbook_sha256": approved_workbook_sha256,
        }
        payload["decision_hash"] = stable_hash(payload)
        previous = str(payload["decision_hash"])
        decisions.append(payload)
    return decisions


def build_cumulative_policy(
    *,
    base_policy: dict[str, Any],
    expansion_decisions: list[dict[str, Any]],
    approved_workbook_path: Path,
    approved_workbook_sha256: str,
    official_review_path: Path,
    official_review_sha256: str,
    registry_path: Path,
    adapter_path: Path,
    reviewer: str,
    reviewed_at_utc: str,
) -> dict[str, Any]:
    base_decisions = [dict(row) for row in base_policy["decisions"]]
    base_keys = {str(row["source_evidence_key"]) for row in base_decisions}
    expansion_keys = {
        str(row["source_evidence_key"]) for row in expansion_decisions
    }
    overlap = sorted(base_keys & expansion_keys)
    if overlap:
        raise ValueError(f"Expansion duplicates base evidence: {overlap[:5]}")
    decisions = [*base_decisions, *expansion_decisions]
    counts = Counter(str(row.get("decision") or "") for row in decisions)
    return {
        "policy_schema_version": RELEASE_SCHEMA_VERSION,
        "policy_id": POLICY_VERSION,
        "policy_version": POLICY_VERSION,
        "release_id": RELEASE_ID,
        "model_family": MODEL_FAMILY,
        "approved_by": reviewer,
        "approved_at_utc": reviewed_at_utc,
        "approved_workbook_path": str(approved_workbook_path.resolve()),
        "approved_workbook_sha256": approved_workbook_sha256,
        "official_review_path": str(official_review_path.resolve()),
        "official_review_sha256": official_review_sha256,
        "base_release_id": str(base_policy.get("release_id") or ""),
        "base_chain_root_sha256": str(base_policy["chain_root_sha256"]),
        "base_decision_count": len(base_decisions),
        "expansion_decision_count": len(expansion_decisions),
        "decision_count": len(decisions),
        "decision_counts": dict(sorted(counts.items())),
        "chain_root_sha256": str(decisions[-1]["decision_hash"]),
        "registry_path": str(registry_path.resolve()),
        "registry_sha256": file_sha256(registry_path),
        "adapter_path": str(adapter_path.resolve()),
        "adapter_sha256": file_sha256(adapter_path),
        "production_weight_modified_flag": 0,
        "measurement_only_flag": 1,
        "decisions": decisions,
    }


def validate_policy_sources(
    policy: dict[str, Any],
    *,
    source_evidence: dict[str, dict[str, Any]],
) -> list[str]:
    errors: list[str] = []
    for decision in policy["decisions"]:
        key = str(decision["source_evidence_key"])
        source = source_evidence.get(key)
        if source is None:
            errors.append(f"missing source evidence: {key}")
        elif stable_hash(_row_payload(source)) != str(
            decision["source_row_sha256"]
        ):
            errors.append(f"source row hash mismatch: {key}")
    return errors


def policy_csv_rows(
    decisions: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for decision in decisions:
        sequence = int(decision["sequence"])
        rows.append(
            {
                "policy_id": f"software_v3_{sequence:03d}",
                "policy_version": POLICY_VERSION,
                "enabled": 1,
                "model_family": MODEL_FAMILY,
                "decision_release_id": decision.get("release_id", ""),
                "sequence": sequence,
                "previous_decision_hash": decision.get(
                    "previous_decision_hash", ""
                ),
                "source_evidence_key": decision.get("source_evidence_key", ""),
                "ticker": decision.get("ticker", ""),
                "accession_number": decision.get("accession_number", ""),
                "source_document": decision.get("source_document", ""),
                "source_metric": decision.get("source_metric", ""),
                "source_value": decision.get("source_value", ""),
                "source_unit": decision.get("source_unit", ""),
                "source_period_end": decision.get("source_period_end", ""),
                "decision": decision.get("decision", ""),
                "decision_reason": decision.get("decision_reason", ""),
                "effective_metric": decision.get("effective_metric", ""),
                "effective_value": decision.get("effective_value", ""),
                "effective_unit": decision.get("effective_unit", ""),
                "effective_period_end": decision.get(
                    "effective_period_end", ""
                ),
                "effective_scope": decision.get("effective_scope", ""),
                "period_kind": decision.get("period_kind", ""),
                "definition_variant": decision.get("definition_variant", ""),
                "calibration_eligible_flag": decision.get(
                    "calibration_eligible_flag", 0
                ),
                "adjudication_reviewer": decision.get(
                    "adjudication_reviewer", ""
                ),
                "adjudicated_at_utc": decision.get("adjudicated_at_utc", ""),
                "approved_workbook_sha256": decision.get(
                    "approved_workbook_sha256", ""
                ),
                "decision_hash": decision.get("decision_hash", ""),
            }
        )
    return rows
