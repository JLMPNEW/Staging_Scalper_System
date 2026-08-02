from __future__ import annotations

import json
from pathlib import Path

from dedicated_parser.contracts import stable_hash
from technology.software_infrastructure.software_arr_release import (
    AUTO_STRICT_RESEARCH_ONLY,
    build_arr_policy,
    validate_arr_rows,
)
from technology.software_infrastructure.software_metric_governance import (
    _row_payload,
)
from technology.software_infrastructure.software_specialized_metrics import (
    validate_policy_payload,
)


def _source() -> dict[str, object]:
    return {
        "evidence_key": "e1",
        "run_id": 25,
        "model_family": "software_infrastructure",
        "adapter_version": "software_parser_v1",
        "parser_release": "parser_v1",
        "ticker": "TEST",
        "cik": "0000000001",
        "accession_number": "0000000001-26-000001",
        "form_type": "8-K",
        "filing_date": "2026-05-01",
        "accepted_at": "2026-05-01T20:00:00Z",
        "metric_name": "annual_recurring_revenue",
        "candidate_value": 500_000_000.0,
        "unit": "USD",
        "period_start": "",
        "period_end": "2026-03-31",
        "source_document": "earnings.htm",
        "provenance_json": json.dumps({"document_sha256": "a" * 64}),
    }


def _review() -> dict[str, object]:
    return {
        **_source(),
        "proposal_decision": "ACCEPTED",
        "proposal_reason": "explicit_total_arr_level",
        "effective_metric": "annual_recurring_revenue",
        "effective_value": 500_000_000.0,
        "effective_unit": "USD",
        "effective_period_end": "2026-03-31",
        "effective_scope": "consolidated",
        "calibration_eligible_flag": 1,
        "canonical_candidate_flag": 1,
    }


def test_arr_release_validates_source_and_seals_research_status(
    tmp_path: Path,
) -> None:
    source = _source()
    review = _review()
    evidence = {"e1": source}
    assert validate_arr_rows(
        [review], source_evidence=evidence, expected_count=1
    ) == []
    workbook = tmp_path / "approved.csv"
    workbook.write_text("approved\n", encoding="utf-8")
    policy = build_arr_policy(
        rows=[review],
        source_evidence=evidence,
        release_id="test_arr_release",
        policy_id="test_arr_policy",
        approved_workbook_path=workbook,
        approved_workbook_sha256="b" * 64,
        reviewer="tester",
        reviewed_at_utc="2026-07-30T00:00:00Z",
        governance_status_by_key={"e1": AUTO_STRICT_RESEARCH_ONLY},
    )
    validate_policy_payload(policy)
    decision = policy["decisions"][0]
    assert decision["source_row_sha256"] == stable_hash(
        _row_payload(source)
    )
    assert decision["governance_status"] == AUTO_STRICT_RESEARCH_ONLY
    assert decision["production_use_prohibited_flag"] == 1
    assert policy["production_weight_modified_flag"] == 0


def test_arr_release_rejects_source_drift() -> None:
    source = _source()
    review = _review()
    review["candidate_value"] = 400_000_000.0
    errors = validate_arr_rows(
        [review],
        source_evidence={"e1": source},
        expected_count=1,
    )
    assert any("candidate_value_mismatch" in error for error in errors)
