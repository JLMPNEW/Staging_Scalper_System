"""Fail-closed evidence acceptance rules and immutable state transitions."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from factor_validation.core import CONTRACT_VERSION, FactorValidationResult
from factor_validation.fdr import FDRDecision, apply_benjamini_hochberg
from factor_validation.registry import (
    CampaignRegistry,
    ValidationCellRegistration,
    canonical_json_bytes,
    sha256_bytes,
)


ACCEPTANCE_SCHEMA_VERSION = "factor_validation_acceptance_v1"
EvidenceState = Literal["draft", "validated", "accepted", "rejected", "superseded"]

_ALLOWED_TRANSITIONS: dict[EvidenceState, frozenset[EvidenceState]] = {
    "draft": frozenset({"validated", "rejected"}),
    "validated": frozenset({"accepted", "rejected"}),
    "accepted": frozenset({"superseded"}),
    "rejected": frozenset(),
    "superseded": frozenset(),
}


def transition_evidence_state(current: EvidenceState, target: EvidenceState) -> EvidenceState:
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise ValueError(f"invalid evidence state transition {current!r} -> {target!r}")
    return target


@dataclass(frozen=True)
class AcceptanceGate:
    name: str
    passed: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"detail": self.detail, "name": self.name, "passed": self.passed}


@dataclass(frozen=True)
class AcceptanceRecord:
    campaign_id: str
    cell_id: str
    cell_registration_sha256: str
    registry_sha256: str
    family_registration_sha256: str
    state: Literal["accepted", "rejected"]
    state_history: tuple[EvidenceState, ...]
    gates: tuple[AcceptanceGate, ...]
    supersedes_manifest_sha256: str | None = None
    schema_version: str = ACCEPTANCE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "campaign_id": self.campaign_id,
            "cell_id": self.cell_id,
            "cell_registration_sha256": self.cell_registration_sha256,
            "family_registration_sha256": self.family_registration_sha256,
            "gates": [item.to_dict() for item in self.gates],
            "registry_sha256": self.registry_sha256,
            "schema_version": self.schema_version,
            "state": self.state,
            "state_history": list(self.state_history),
            "supersedes_manifest_sha256": self.supersedes_manifest_sha256,
        }

    @property
    def record_sha256(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.to_dict()))


def _same_optional_float(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return left is right
    return left == right


def _validate_structural_contract(
    registry: CampaignRegistry,
    cell: ValidationCellRegistration,
    result: FactorValidationResult,
    decision: FDRDecision,
) -> None:
    family = registry.family(cell.fdr_family_id)
    mismatches: list[str] = []
    if result.contract_version != CONTRACT_VERSION:
        mismatches.append("contract_version")
    if result.factor_id != cell.factor_id:
        mismatches.append("factor_id")
    if result.target_name != cell.target_name:
        mismatches.append("target_name")
    if result.horizon_trading_days != cell.horizon_trading_days:
        mismatches.append("horizon_trading_days")
    if result.entry_lag_trading_days != cell.entry_lag_trading_days:
        mismatches.append("entry_lag_trading_days")
    if result.primary_inference != "independent_window":
        mismatches.append("primary_inference")
    if decision.family_id != family.family_id:
        mismatches.append("fdr_family_id")
    if decision.family_registration_sha256 != family.registration_sha256:
        mismatches.append("fdr_family_registration_sha256")
    if not math.isclose(decision.alpha, family.alpha, rel_tol=0.0, abs_tol=0.0):
        mismatches.append("fdr_alpha")
    if decision.member_id != cell.fdr_member_id:
        mismatches.append("fdr_member_id")
    if not _same_optional_float(decision.p_value, result.primary_p_value):
        mismatches.append("primary_p_value")
    if not math.isfinite(decision.q_value) or not 0.0 <= decision.q_value <= 1.0:
        mismatches.append("fdr_q_value")
    expected_accepted = (
        decision.testable
        and decision.p_value is not None
        and decision.q_value <= decision.alpha
    )
    if decision.accepted != expected_accepted:
        mismatches.append("fdr_accepted")
    if decision.testable != (decision.p_value is not None):
        mismatches.append("fdr_testable")
    if mismatches:
        raise ValueError(f"registered evidence contract mismatch: {sorted(mismatches)}")


def build_acceptance_record(
    registry: CampaignRegistry,
    *,
    cell_id: str,
    result: FactorValidationResult,
    family_results: Mapping[str, FactorValidationResult],
    supersedes_manifest_sha256: str | None = None,
) -> AcceptanceRecord:
    """Create a deterministic terminal acceptance record for one registered cell.

    Acceptance means the evidence package is statistically testable and passes its
    pre-registered FDR decision. It is not, by itself, a sector promotion decision.
    """

    cell = registry.cell(cell_id)
    decision = registered_fdr_decision(
        registry,
        cell_id=cell_id,
        result=result,
        family_results=family_results,
    )
    if supersedes_manifest_sha256 is not None:
        normalized_supersedes = str(supersedes_manifest_sha256).strip().lower()
        if len(normalized_supersedes) != 64 or any(
            character not in "0123456789abcdef" for character in normalized_supersedes
        ):
            raise ValueError("supersedes_manifest_sha256 must be a lowercase SHA-256 digest")
    else:
        normalized_supersedes = None

    direction_passed = result.mean_ic is not None and (
        (cell.factor_direction == "higher_is_better" and result.mean_ic > 0.0)
        or (cell.factor_direction == "lower_is_better" and result.mean_ic < 0.0)
    )
    gates = (
        AcceptanceGate(
            "evidence_eligible",
            result.evidence_eligible,
            "kernel minimum dates, windows, and primary inference are available",
        ),
        AcceptanceGate(
            "primary_p_value_available",
            result.primary_p_value is not None,
            "promotion-facing p-value is present only after kernel eligibility",
        ),
        AcceptanceGate(
            "fdr_testable",
            decision.testable,
            "registered FDR member has a valid p-value",
        ),
        AcceptanceGate(
            "fdr_accepted",
            decision.accepted,
            "registered BH q-value is at or below the sealed family alpha",
        ),
        AcceptanceGate(
            "factor_direction_consistent",
            direction_passed,
            "mean IC sign agrees with the pre-registered factor direction",
        ),
    )
    history: list[EvidenceState] = ["draft"]
    state: EvidenceState = transition_evidence_state("draft", "validated")
    history.append(state)
    terminal: Literal["accepted", "rejected"] = (
        "accepted" if all(item.passed for item in gates) else "rejected"
    )
    state = transition_evidence_state(state, terminal)
    history.append(state)
    family = registry.family(cell.fdr_family_id)
    return AcceptanceRecord(
        campaign_id=registry.campaign_id,
        cell_id=cell.cell_id,
        cell_registration_sha256=cell.registration_sha256,
        registry_sha256=registry.registration_sha256,
        family_registration_sha256=family.registration_sha256,
        state=terminal,
        state_history=tuple(history),
        gates=gates,
        supersedes_manifest_sha256=normalized_supersedes,
    )


def registered_fdr_decision(
    registry: CampaignRegistry,
    *,
    cell_id: str,
    result: FactorValidationResult,
    family_results: Mapping[str, FactorValidationResult],
) -> FDRDecision:
    """Derive every sibling p-value from registered result objects and select one."""

    cell = registry.cell(cell_id)
    decisions = registered_fdr_decisions(
        registry,
        cell_id=cell_id,
        family_results=family_results,
    )
    if family_results[cell.cell_id] is not result:
        raise ValueError("result must be the exact registered family_results object for cell_id")
    return next(item for item in decisions if item.member_id == cell.fdr_member_id)


def registered_fdr_decisions(
    registry: CampaignRegistry,
    *,
    cell_id: str,
    family_results: Mapping[str, FactorValidationResult],
) -> tuple[FDRDecision, ...]:
    """Validate a complete registered family and derive its BH decisions."""

    cell = registry.cell(cell_id)
    family = registry.family(cell.fdr_family_id)
    cells = tuple(
        item for item in registry.cells if item.fdr_family_id == family.family_id
    )
    expected = {item.cell_id for item in cells}
    supplied = set(family_results)
    if supplied != expected:
        raise ValueError(
            "registered family result membership mismatch: "
            f"missing={sorted(expected - supplied)}; extra={sorted(supplied - expected)}"
        )
    if any(
        not isinstance(family_results[item.cell_id], FactorValidationResult)
        for item in cells
    ):
        raise TypeError("family_results must contain FactorValidationResult instances")
    p_values = {
        item.fdr_member_id: family_results[item.cell_id].primary_p_value
        for item in cells
    }
    decisions = apply_benjamini_hochberg(family, p_values)
    by_member = {decision.member_id: decision for decision in decisions}
    for item in cells:
        _validate_structural_contract(
            registry,
            item,
            family_results[item.cell_id],
            by_member[item.fdr_member_id],
        )
    return decisions
