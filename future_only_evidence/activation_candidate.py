"""Independent-review bridge from passing evidence to manual change control."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .canonical_values import exact_utc
from .protocol import canonical_sha256, exact_sha256
from .trusted_receipts import PinnedEd25519Authority


REVIEW_REGISTRY = Path(__file__).with_name("canonical_review_roots.json")
APPROVED_REVIEW_REGISTRY_SHA256 = ""
REVIEW_REGISTRY_SCHEMA = "future_evidence_independent_review_registry_v1"
REVIEW_RECEIPT_SCHEMA = "future_evidence_independent_review_receipt_v1"
ACTIVATION_CANDIDATE_SCHEMA = "future_evidence_manual_activation_candidate_v1"
ALLOWED_EVALUATION_SCHEMAS = {
    "transportation": "transportation_future_only_evaluation_v6",
}


def _read_json_once(path: Path, *, label: str) -> tuple[bytes, str, dict[str, Any]]:
    resolved = Path(path).expanduser().resolve()
    raw = resolved.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must be one UTF-8 JSON object") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    return raw, digest, payload


def _utc(value: Any, *, label: str) -> datetime:
    return exact_utc(value, label=label)


def _is_exact_numeric_zero(value: Any) -> bool:
    return type(value) in {int, float} and float(value) == 0.0


def _review_authority(family: str, *, public_key_path: Path) -> tuple[PinnedEd25519Authority, str]:
    raw = REVIEW_REGISTRY.read_bytes()
    registry_hash = hashlib.sha256(raw).hexdigest()
    payload = json.loads(raw.decode("utf-8"))
    if (
        payload.get("schema_version") != REVIEW_REGISTRY_SCHEMA
        or payload.get("status") != "active_reviewed"
        or type(payload.get("registry_revision")) is not int
        or payload["registry_revision"] < 1
    ):
        raise ValueError("independent review authority is unconfigured; activation remains blocked")
    if not APPROVED_REVIEW_REGISTRY_SHA256 or registry_hash != exact_sha256(
        APPROVED_REVIEW_REGISTRY_SHA256,
        label="approved review registry sha256",
    ):
        raise ValueError("independent review registry lacks an approved deployment hash")
    definition = dict(payload.get("families", {}).get(family) or {})
    if (
        definition.get("algorithm") != "Ed25519"
        or not definition.get("authority_id")
        or not definition.get("public_key_sha256")
    ):
        raise ValueError(f"independent review authority is not configured for {family}")
    return (
        PinnedEd25519Authority(
            authority_id=str(definition["authority_id"]),
            public_key_path=public_key_path,
            expected_public_key_sha256=str(definition["public_key_sha256"]),
        ),
        registry_hash,
    )


def _scope_verdict(evaluation: dict[str, Any], *, family: str, scope_id: str) -> dict[str, Any]:
    if family != "transportation":
        raise ValueError("activation candidate family is unsupported")
    matches = [
        dict(row)
        for row in evaluation.get("sleeve_independent_verdicts", [])
        if row.get("sleeve_id") == scope_id
    ]
    verdict = matches[0] if len(matches) == 1 else {}
    if (
        not verdict
        or verdict.get("pass") is not True
        or verdict.get("action") != "eligible_for_independent_review"
        or verdict.get("production_activation_authorized") is not False
        or not _is_exact_numeric_zero(verdict.get("optimizer_cap"))
    ):
        raise ValueError("requested cohort/sleeve does not have one exact passing zero-cap verdict")
    return verdict


def build_activation_candidate(
    *,
    family: str,
    scope_id: str,
    evaluation_path: Path,
    expected_evaluation_sha256: str,
    review_receipt_path: Path,
    expected_review_receipt_sha256: str,
    review_public_key_path: Path,
    generated_at_utc: str,
) -> dict[str, Any]:
    if family not in ALLOWED_EVALUATION_SCHEMAS:
        raise ValueError("activation candidate family is unsupported")
    _, evaluation_hash, evaluation = _read_json_once(
        evaluation_path,
        label="passing evaluation",
    )
    if evaluation_hash != exact_sha256(
        expected_evaluation_sha256, label="passing evaluation sha256"
    ):
        raise ValueError("passing evaluation bytes changed")
    if evaluation.get("schema_version") != ALLOWED_EVALUATION_SCHEMAS[family]:
        raise ValueError("legacy/diagnostic evaluation cannot create an activation candidate")
    supplied_payload_hash = exact_sha256(
        evaluation.get("payload_sha256"), label="evaluation payload sha256"
    )
    evaluation_body = dict(evaluation)
    evaluation_body.pop("payload_sha256")
    if canonical_sha256(evaluation_body) != supplied_payload_hash:
        raise ValueError("evaluation verdict/metric payload was tampered")
    if (
        evaluation.get("family") != family
        or evaluation.get("production_activation_authorized") is not False
        or evaluation.get("portfolio_write_enabled") is not False
        or not _is_exact_numeric_zero(evaluation.get("optimizer_cap"))
    ):
        raise ValueError("evaluation is not canonical fail-closed evidence")
    verdict = _scope_verdict(evaluation, family=family, scope_id=scope_id)
    authority, review_registry_hash = _review_authority(
        family, public_key_path=review_public_key_path
    )
    review_identity = authority.identity()
    trust_audit = evaluation.get("canonical_trust_audit")
    if not isinstance(trust_audit, dict):
        raise ValueError("evaluation lacks the exact canonical trust identity census")
    trust_roles = [
        trust_audit.get("evidence_seal"),
        trust_audit.get("timestamp_log"),
        trust_audit.get("market_data_export"),
    ]
    if any(not isinstance(role, dict) for role in trust_roles):
        raise ValueError("evaluation canonical trust roles are incomplete")
    authority_ids = {str(role.get("authority_id") or "") for role in trust_roles}
    key_fingerprints = {
        str(role.get("public_key_spki_sha256") or "") for role in trust_roles
    }
    if (
        not all(authority_ids)
        or not all(key_fingerprints)
        or review_identity["authority_id"] in authority_ids
        or review_identity["public_key_spki_sha256"] in key_fingerprints
    ):
        raise ValueError(
            "independent reviewer is not separate from evidence/timestamp/market authorities"
        )
    receipt_path = Path(review_receipt_path).expanduser().resolve()
    receipt_bytes, receipt_hash, receipt = _read_json_once(
        receipt_path,
        label="independent review receipt",
    )
    if receipt_hash != exact_sha256(
        expected_review_receipt_sha256, label="independent review receipt sha256"
    ):
        raise ValueError("independent review receipt bytes changed")
    authority.verify_snapshot(receipt_bytes, receipt_hash, receipt)
    expected_receipt = {
        "schema_version": REVIEW_RECEIPT_SCHEMA,
        "family": family,
        "scope_id": scope_id,
        "evaluation_sha256": evaluation_hash,
        "evaluation_payload_sha256": supplied_payload_hash,
        "domain_contract_sha256": evaluation["domain_contract_sha256"],
        "scope_verdict_sha256": canonical_sha256(verdict),
        "decision": "accept_for_manual_activation_change_control",
        "automatic_config_write_authorized": False,
    }
    for field, expected in expected_receipt.items():
        if receipt.get(field) != expected:
            raise ValueError(f"independent review receipt changed field: {field}")
    reviewed_at = _utc(receipt.get("reviewed_at_utc"), label="independent review time")
    generated_at = _utc(generated_at_utc, label="activation candidate generation time")
    if not _utc(evaluation["evaluated_at_utc"], label="evaluation time") <= reviewed_at <= generated_at:
        raise ValueError("independent review chronology is invalid")
    body: dict[str, Any] = {
        "schema_version": ACTIVATION_CANDIDATE_SCHEMA,
        "family": family,
        "scope_id": scope_id,
        "status": "ready_for_manual_change_control_review",
        "generated_at_utc": generated_at.isoformat(),
        "evaluation_sha256": evaluation_hash,
        "evaluation_payload_sha256": supplied_payload_hash,
        "scope_verdict_sha256": canonical_sha256(verdict),
        "review_receipt_sha256": receipt_hash,
        "review_authority": review_identity,
        "review_registry_sha256": review_registry_hash,
        "automatic_config_write_authorized": False,
        "production_activation_authorized": False,
        "portfolio_write_enabled": False,
        "optimizer_cap": 0.0,
        "required_next_step": "manual_separation_of_duties_change_control",
    }
    body["payload_sha256"] = canonical_sha256(body)
    return body


__all__ = [
    "ACTIVATION_CANDIDATE_SCHEMA",
    "ALLOWED_EVALUATION_SCHEMAS",
    "APPROVED_REVIEW_REGISTRY_SHA256",
    "REVIEW_REGISTRY",
    "build_activation_candidate",
]
