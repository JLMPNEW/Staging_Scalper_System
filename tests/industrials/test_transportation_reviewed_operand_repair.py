from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from industrials.core.config import load_yaml
from industrials.core.db import init_db
from industrials.transportation.reviewed_operand_repair import (
    POLICY_VERSION,
    SOURCE_ID,
    ResolvedFact,
    ResolvedOverride,
    persist_policy,
    validate_policy_contract,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = (
    PROJECT_ROOT
    / "industrials"
    / "transportation"
    / "review_policies"
    / "transportation_required_metric_operand_repairs.json"
)


def _policy() -> dict:
    return json.loads(POLICY_PATH.read_text(encoding="utf-8-sig"))


def test_reviewed_operand_policy_is_narrow_and_arithmetically_sealed() -> None:
    policy = _policy()
    validate_policy_contract(policy)
    facts = {row["repair_id"]: row for row in policy["fact_repairs"]}
    overrides = {
        row["override_id"]: row for row in policy["availability_overrides"]
    }

    assert len(facts) == 16
    assert len(overrides) == 2
    assert {
        row["availability_status"] for row in overrides.values()
    } == {"NOT_APPLICABLE"}
    assert {
        row["ticker"] for row in overrides.values()
    } == {"AER", "R"}

    edry = facts["EDRY_CAPEX_FY2025"]
    assert edry["value"] == 165_461 + 7_197_946
    assert not (
        set(edry["fact_fingerprints"])
        & set(edry["excluded_fact_fingerprints"])
    )

    asc = facts["ASC_OPERATING_INCOME_FY2025"]
    assert sum(
        component["signed_value"]
        for component in asc["formula_components"]
    ) == asc["value"]
    cross = asc["cross_check"]
    assert (
        cross["pretax_income"]
        + cross["interest_expense"]
        + cross["loss_on_debt_extinguishment"]
        - cross["interest_income"]
    ) == asc["value"]

    pbi = facts["PBI_OPERATING_INCOME_FY2025"]
    assert sum(
        component["signed_value"]
        for component in pbi["formula_components"]
    ) == pbi["value"]
    assert sum(
        component["signed_value"]
        for component in pbi["cross_check"]["signed_components"]
    ) == pbi["value"]

    assert facts["ASR_REVENUE_FY2025"]["value"] == 37_237_431_000
    assert (
        facts["ASR_OPERATING_CASH_FLOW_FY2025"]["value"]
        == 12_348_613_000
    )

    hshp = facts["HSHP_CAPEX_FY2025_ZERO"]
    assert hshp["value"] == 0
    assert hshp["derivation_type"] == "document_explicit_zero"
    assert len(hshp["text_anchors"]) >= 2

    deferred = {row["ticker"] for row in policy["deferred_scope"]}
    assert deferred == {"CIIT", "CTNT", "RUBI"}


def test_policy_rejects_automatic_extension_promotion() -> None:
    policy = _policy()
    policy["controls"]["automatic_extension_promotion_allowed"] = True
    with pytest.raises(ValueError, match="automatic issuer-extension"):
        validate_policy_contract(policy)


def test_transportation_build_uses_only_transportation_supplemental_sources() -> None:
    config = load_yaml(PROJECT_ROOT / "industrials" / "config.yaml")
    sources = config["model_families"]["transportation"]["financial"][
        "supplemental_disclosure_source_ids"
    ]
    assert sources == [
        "dedicated_parser_transportation_required_metric_repair_v1",
        SOURCE_ID,
    ]


def test_reviewed_operand_persistence_is_idempotent(tmp_path: Path) -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    init_db(connection)
    policy_path = tmp_path / "policy.json"
    policy_path.write_text('{"sealed":true}\n', encoding="utf-8")
    fact = ResolvedFact(
        repair_id="TEST_CAPEX",
        ticker="TEST",
        cik="0000000001",
        accession_number="0000000001-26-000001",
        form_type="10-K",
        filing_date="2026-02-01",
        accepted_at="2026-02-01T12:00:00.000Z",
        fiscal_year=2025,
        fiscal_period="FY",
        period_start="2025-01-01",
        period_end="2025-12-31",
        canonical_metric="capex",
        financial_statement="cash_flow",
        period_type="duration",
        unit="USD",
        value=10.0,
        taxonomy="transportation-reviewed",
        concept_name="ReviewedCapex",
        derivation_type="document_reviewed_formula",
        rationale="test",
        provenance={"sealed": True},
    )
    override = ResolvedOverride(
        override_id="TEST_MARGIN_NA",
        ticker="TEST",
        metric_name="operating_margin",
        availability_status="NOT_APPLICABLE",
        status_reason=f"{POLICY_VERSION}:test",
        valid_from="2026-02-01",
        evidence_key="test-evidence",
        rationale="test",
        provenance={"sealed": True},
    )

    first = persist_policy(
        connection,
        facts=[fact],
        overrides=[override],
        policy_path=policy_path,
        source_priority=5,
    )
    second = persist_policy(
        connection,
        facts=[fact],
        overrides=[override],
        policy_path=policy_path,
        source_priority=5,
    )

    assert first["fact_count"] == second["fact_count"] == 1
    assert first["active_override_count"] == second["active_override_count"] == 1
    assert connection.execute(
        "SELECT COUNT(*) FROM fact_sec_xbrl_fact WHERE source_id=?",
        (SOURCE_ID,),
    ).fetchone()[0] == 1
    assert connection.execute(
        """
        SELECT COUNT(*)
        FROM sec_parser_production_metric_override
        WHERE evidence_key='test-evidence' AND active=1
        """
    ).fetchone()[0] == 1
