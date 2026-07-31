from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from dedicated_parser.contracts import stable_hash
from technology.software_infrastructure.software_metric_expansion_release import (
    build_cumulative_policy,
    build_expansion_decisions,
    promote_proposal_rows,
)
from technology.software_infrastructure.software_metric_proposed_adjudication import (
    PROPOSAL_NOTICE,
)
from technology.software_infrastructure.software_metric_review import (
    DECISION_FIELDS,
    SOURCE_FIELDS,
)
from technology.software_infrastructure.software_specialized_metrics import (
    validate_policy_payload,
)


def _review_row() -> dict[str, Any]:
    row = {field: "" for field in SOURCE_FIELDS}
    row.update(
        {
            "source_evidence_key": "e2",
            "ticker": "TEST",
            "accession_number": "0000000001-24-000001",
            "form_type": "10-Q",
            "accepted_at": "2024-05-01T20:00:00Z",
            "source_document": "test.htm",
            "source_metric": "deferred_revenue_total",
            "candidate_value": "100",
            "unit": "USD",
            "period_end": "2024-03-31",
            "source_row_sha256": "a" * 64,
            "source_document_sha256": "b" * 64,
            "review_source_sha256": "c" * 64,
        }
    )
    row.update({field: "" for field in DECISION_FIELDS})
    return row


def test_proposal_promotion_is_hash_bound_and_strips_proposal_notice() -> None:
    official = _review_row()
    proposal = dict(official)
    proposal.update(
        {
            "proposal_status": "PENDING_HUMAN_APPROVAL",
            "decision": "CORRECTED",
            "decision_reason": "confirmed current balance",
            "effective_metric": "deferred_revenue_current",
            "effective_value": "90",
            "effective_unit": "USD",
            "effective_period_end": "2024-03-31",
            "effective_scope": "consolidated",
            "period_kind": "instant",
            "definition_variant": "current_deferred_revenue",
            "calibration_eligible_flag": "1",
            "review_notes": f"{PROPOSAL_NOTICE} Reviewed note.",
        }
    )
    promoted = promote_proposal_rows(
        proposal_rows=[proposal],
        official_rows=[official],
        reviewer="reviewer",
        reviewed_at_utc="2026-07-30T12:00:00Z",
    )
    assert promoted[0]["reviewer"] == "reviewer"
    assert promoted[0]["review_notes"] == "Reviewed note."

    proposal["review_source_sha256"] = "d" * 64
    with pytest.raises(ValueError, match="immutable review field"):
        promote_proposal_rows(
            proposal_rows=[proposal],
            official_rows=[official],
            reviewer="reviewer",
            reviewed_at_utc="2026-07-30T12:00:00Z",
        )


def test_cumulative_policy_preserves_base_chain_and_validates_count(
    tmp_path: Path,
) -> None:
    base_decision: dict[str, Any] = {
        "release_id": "software_metrics_v1",
        "sequence": 1,
        "previous_decision_hash": "0" * 64,
        "source_evidence_key": "e1",
        "decision": "ACCEPTED",
    }
    base_decision["decision_hash"] = stable_hash(base_decision)
    base_policy = {
        "policy_id": "software_metrics_adjudication_v1",
        "release_id": "software_metrics_v1",
        "decision_count": 1,
        "decision_counts": {"ACCEPTED": 1},
        "chain_root_sha256": base_decision["decision_hash"],
        "decisions": [base_decision],
    }
    validate_policy_payload(base_policy)

    review = _review_row()
    review.update(
        {
            "reviewer": "reviewer",
            "reviewed_at_utc": "2026-07-30T12:00:00Z",
            "decision": "CORRECTED",
            "decision_reason": "confirmed current balance",
            "effective_metric": "deferred_revenue_current",
            "effective_value": "90",
            "effective_unit": "USD",
            "effective_period_end": "2024-03-31",
            "effective_scope": "consolidated",
            "period_kind": "instant",
            "definition_variant": "current_deferred_revenue",
            "calibration_eligible_flag": "1",
        }
    )
    source = {
        "evidence_key": "e2",
        "ticker": "TEST",
        "cik": "0000000001",
        "accession_number": "0000000001-24-000001",
        "form_type": "10-Q",
        "filing_date": "2024-05-01",
        "accepted_at": "2024-05-01T20:00:00Z",
        "source_document": "test.htm",
        "adapter_version": "v1",
        "parser_release": "0.4.6",
        "period_start": "",
    }
    expansion = build_expansion_decisions(
        approved_rows=[review],
        source_evidence={"e2": source},
        first_sequence=2,
        previous_decision_hash=str(base_decision["decision_hash"]),
        approved_workbook_sha256="d" * 64,
    )
    proposal_path = tmp_path / "proposal.csv"
    official_path = tmp_path / "official.csv"
    registry_path = tmp_path / "registry.yaml"
    adapter_path = tmp_path / "adapter.py"
    for path in (proposal_path, official_path, registry_path, adapter_path):
        path.write_text("test\n", encoding="utf-8")
    policy = build_cumulative_policy(
        base_policy=base_policy,
        expansion_decisions=expansion,
        approved_workbook_path=proposal_path,
        approved_workbook_sha256="d" * 64,
        official_review_path=official_path,
        official_review_sha256="e" * 64,
        registry_path=registry_path,
        adapter_path=adapter_path,
        reviewer="reviewer",
        reviewed_at_utc="2026-07-30T12:00:00Z",
    )
    validate_policy_payload(policy)
    assert policy["decision_count"] == 2
    assert policy["decisions"][0] == base_decision
    assert policy["decisions"][1]["previous_decision_hash"] == (
        base_decision["decision_hash"]
    )

    policy["decision_count"] = 3
    with pytest.raises(ValueError, match="decision_count"):
        validate_policy_payload(policy)
