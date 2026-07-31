from __future__ import annotations

from technology.software_infrastructure.software_metric_proposed_adjudication import (
    PROPOSALS,
    PROPOSAL_REVIEWER,
    build_proposed_rows,
)


def test_proposal_registry_has_expected_decision_mix() -> None:
    decisions = [spec.decision for spec in PROPOSALS.values()]
    assert len(PROPOSALS) == 62
    assert decisions.count("ACCEPTED") == 26
    assert decisions.count("CORRECTED") == 17
    assert decisions.count("REJECTED_POLICY") == 19
    assert sum(spec.calibration_eligible_flag for spec in PROPOSALS.values()) == 21


def test_reviewed_metric_corrections_are_encoded_fail_closed() -> None:
    infa_2023 = PROPOSALS[
        "d1f0b97e8da7194ee54718648a52ccf0254b4918171aeff3d1c5ffdb37dd4656"
    ]
    assert infa_2023.effective_metric == "deferred_revenue_current"
    assert infa_2023.effective_value == "767244000"
    assert infa_2023.definition_variant == "current_deferred_revenue"

    infa_2024 = PROPOSALS[
        "1e815ec3a5fb5d8b08a91b4aed9647557933776173c5e3005a58e583af6b01bf"
    ]
    assert infa_2024.effective_metric == "deferred_revenue_current"
    assert infa_2024.effective_value == "819367000"
    assert infa_2024.definition_variant == "current_deferred_revenue"

    hashicorp_billings = PROPOSALS[
        "4b858d51c5c6f3b551d24f0044c0108b340b3dafcc9dd8c3dfcc598aa1b99593"
    ]
    assert hashicorp_billings.calibration_eligible_flag == 0
    assert hashicorp_billings.definition_variant != "reported_billings"

    commvault_rpo = PROPOSALS[
        "47fefbfe1d55615c14316c7534d74a3d8781430b2289d7502de696b085cfa6d9"
    ]
    assert "reconciled total RPO" in commvault_rpo.review_notes


def test_proposed_rows_are_explicitly_not_human_approved() -> None:
    rows = build_proposed_rows(
        [{"source_evidence_key": key} for key in PROPOSALS],
        proposed_at_utc="2026-07-30T12:00:00Z",
    )
    assert rows[0]["reviewer"] == PROPOSAL_REVIEWER
    assert rows[0]["proposal_status"] == "PENDING_HUMAN_APPROVAL"
    assert "Human approval is required" in rows[0]["review_notes"]


def test_proposal_registry_rejects_source_drift() -> None:
    try:
        build_proposed_rows(
            [{"source_evidence_key": "unknown"}],
            proposed_at_utc="2026-07-30T12:00:00Z",
        )
    except ValueError as exc:
        assert "Proposal registry mismatch" in str(exc)
    else:
        raise AssertionError("source drift must fail closed")
