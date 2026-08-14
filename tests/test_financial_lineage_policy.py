from __future__ import annotations

from orchestration_contracts.financial_lineage import (
    LINEAGE_FIELDS,
    POLICY_CANDIDATE_ONLY,
    POLICY_DISABLED,
    POLICY_STRICT_UNIVERSE,
    evaluate_financial_lineage_rows,
    policy_for_model_family,
)


ASOF = "2026-08-13"


def _row(*, gate: str, candidate: str) -> dict[str, str]:
    row = {
        "ticker": "TEST",
        "portfolio_candidate_gate": candidate,
        "financial_lineage_checked_asof_date": ASOF,
        "financial_lineage_status": "INCORPORATED" if gate == "1" else "REVIEW_REQUIRED",
        "financial_lineage_gate": gate,
        "financial_lineage_classification": (
            "INCORPORATED" if gate == "1" else "CANONICALIZATION_GAP"
        ),
        "latest_material_financial_filing_date": "2026-08-12",
        "latest_material_financial_form": "10-Q",
        "latest_material_financial_accession": "latest" if gate == "1" else "unresolved",
        "latest_material_financial_report_date": "2026-06-30",
        "incorporated_financial_filing_date": "2026-08-12" if gate == "1" else "",
        "incorporated_financial_accession": "latest" if gate == "1" else "",
        "incorporated_financial_report_date": "2026-06-30" if gate == "1" else "",
        "incorporated_financial_core_metric_count": "3" if gate == "1" else "0",
        "financial_lineage_reason": "test",
    }
    assert not set(LINEAGE_FIELDS).difference(row)
    return row


def test_central_registry_enables_only_pilot_industrial_families() -> None:
    defense = policy_for_model_family("defense")
    machinery = policy_for_model_family("machinery")
    transportation = policy_for_model_family("transportation")

    assert defense.mode_for("production") == POLICY_STRICT_UNIVERSE
    assert machinery.mode_for("production") == POLICY_STRICT_UNIVERSE
    assert defense.mode_for("research") == POLICY_CANDIDATE_ONLY
    assert transportation.mode_for("production") == POLICY_DISABLED


def test_strict_universe_blocks_an_unresolved_noncandidate() -> None:
    evaluation = evaluate_financial_lineage_rows(
        [_row(gate="0", candidate="0")],
        policy_mode=POLICY_STRICT_UNIVERSE,
        expected_asof=ASOF,
    )

    assert evaluation.acceptance == "FAIL"
    assert evaluation.unresolved_count == 1
    assert any("material_financial_filing_unresolved" in error for error in evaluation.errors)


def test_candidate_only_retains_unresolved_noncandidate_as_nonblocking_evidence() -> None:
    evaluation = evaluate_financial_lineage_rows(
        [_row(gate="0", candidate="0")],
        policy_mode=POLICY_CANDIDATE_ONLY,
        expected_asof=ASOF,
    )

    assert evaluation.acceptance == "PASS"
    assert evaluation.unresolved_count == 1
    assert evaluation.issue_counts == {"noncandidate_financial_lineage_unresolved": 1}


def test_incorporated_row_passes_both_policies() -> None:
    row = _row(gate="1", candidate="1")

    for mode in (POLICY_STRICT_UNIVERSE, POLICY_CANDIDATE_ONLY):
        evaluation = evaluate_financial_lineage_rows(
            [row],
            policy_mode=mode,
            expected_asof=ASOF,
        )
        assert evaluation.acceptance == "PASS"
        assert evaluation.incorporated_count == 1
        assert evaluation.errors == []
