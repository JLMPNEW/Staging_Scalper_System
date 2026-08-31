"""Evaluator-compatible governing-v7 Transportation signal capture."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from future_only_evidence.protocol import TrustedReceiptVerifier, canonical_sha256

from .future_oos_capture_v2 import (
    validate_governing_contracts,
    validate_membership_snapshot,
)
from .future_oos_protocol_v1 import (
    REQUIRED_CAPTURE_ROLES,
    capture_signal as _capture_v1,
)


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
    # Keep the shared schema so the generic hash/chronology validator can read
    # this artifact; domain versioning is an additive field.
    payload.update(
        domain_schema_version="transportation_future_only_signal_capture_v3",
        governing_contract_audit=governance,
        membership_audit=membership,
    )
    payload["capture_id"] = canonical_sha256(payload)
    payload["payload_sha256"] = canonical_sha256(payload)
    return payload


__all__ = ["capture_signal"]
