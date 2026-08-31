"""Signed, effective-dated bridge from reviewed evidence to production locks.

The prospective evaluator and independent reviewer intentionally stop at a
zero-cap activation candidate.  This module implements the later,
separation-of-duties change-control step.  It never edits sector or portfolio
configuration.  Callers must explicitly publish and pin a registry containing
the returned lock before a production adapter may consume it.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping

from .activation_candidate import ACTIVATION_CANDIDATE_SCHEMA
from .canonical_values import exact_utc
from .protocol import canonical_sha256, exact_sha256
from .trusted_receipts import PinnedEd25519Authority


CHANGE_CONTROL_REGISTRY = Path(__file__).with_name(
    "canonical_change_control_roots.yaml"
)
# Deliberately blank until an independently reviewed deployment pins the exact
# registry bytes. Editing the adjacent registry cannot activate an authority.
APPROVED_CHANGE_CONTROL_REGISTRY_SHA256 = ""
CHANGE_CONTROL_REGISTRY_SCHEMA = "future_evidence_production_change_registry_v1"
CHANGE_CONTROL_RECEIPT_SCHEMA = "future_evidence_production_change_receipt_v1"
PRODUCTION_ACTIVATION_LOCK_SCHEMA = "future_evidence_production_activation_lock_v1"
PRODUCTION_ACTIVATION_REGISTRY_SCHEMA = (
    "future_evidence_production_activation_registry_v1"
)


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


def _finite_positive(value: Any, *, label: str, maximum: float | None = None) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a positive finite number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a positive finite number") from exc
    if not math.isfinite(parsed) or parsed <= 0.0:
        raise ValueError(f"{label} must be a positive finite number")
    if maximum is not None and parsed > maximum:
        raise ValueError(f"{label} exceeds maximum {maximum}")
    return parsed


def _iso_date(value: Any, *, label: str, allow_blank: bool = False) -> date | None:
    text = str(value or "").strip()
    if not text and allow_blank:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO date") from exc


def _self_hash(payload: Mapping[str, Any], field: str = "payload_sha256") -> str:
    return canonical_sha256({key: value for key, value in payload.items() if key != field})


def _change_control_authority(
    family: str,
    *,
    public_key_path: Path,
) -> tuple[PinnedEd25519Authority, str]:
    raw = CHANGE_CONTROL_REGISTRY.read_bytes()
    registry_hash = hashlib.sha256(raw).hexdigest()
    payload = json.loads(raw.decode("utf-8"))
    if (
        payload.get("schema_version") != CHANGE_CONTROL_REGISTRY_SCHEMA
        or payload.get("status") != "active_reviewed"
        or type(payload.get("registry_revision")) is not int
        or payload["registry_revision"] < 1
    ):
        raise ValueError(
            "production change-control authority is unconfigured; activation remains blocked"
        )
    if (
        not APPROVED_CHANGE_CONTROL_REGISTRY_SHA256
        or registry_hash
        != exact_sha256(
            APPROVED_CHANGE_CONTROL_REGISTRY_SHA256,
            label="approved production change-control registry sha256",
        )
    ):
        raise ValueError(
            "production change-control registry lacks an approved deployment hash"
        )
    definition = dict(payload.get("families", {}).get(family) or {})
    if (
        definition.get("algorithm") != "Ed25519"
        or definition.get("purpose") != "manual_production_change_control"
        or not definition.get("authority_id")
        or not definition.get("public_key_sha256")
    ):
        raise ValueError(
            f"production change-control authority is not configured for {family}"
        )
    return (
        PinnedEd25519Authority(
            authority_id=str(definition["authority_id"]),
            public_key_path=public_key_path,
            expected_public_key_sha256=str(definition["public_key_sha256"]),
        ),
        registry_hash,
    )


def _validate_activation_candidate(
    candidate: Mapping[str, Any],
    *,
    family: str,
    scope_id: str,
) -> None:
    supplied_hash = exact_sha256(
        candidate.get("payload_sha256"), label="activation candidate payload sha256"
    )
    if _self_hash(candidate) != supplied_hash:
        raise ValueError("activation candidate payload was tampered")
    if (
        candidate.get("schema_version") != ACTIVATION_CANDIDATE_SCHEMA
        or candidate.get("family") != family
        or candidate.get("scope_id") != scope_id
        or candidate.get("status") != "ready_for_manual_change_control_review"
        or candidate.get("automatic_config_write_authorized") is not False
        or candidate.get("production_activation_authorized") is not False
        or candidate.get("portfolio_write_enabled") is not False
        or type(candidate.get("optimizer_cap")) not in {int, float}
        or float(candidate["optimizer_cap"]) != 0.0
    ):
        raise ValueError("activation candidate is not the canonical zero-cap bridge")


def build_production_activation_lock(
    *,
    family: str,
    scope_id: str,
    activation_candidate_path: Path,
    expected_activation_candidate_sha256: str,
    change_receipt_path: Path,
    expected_change_receipt_sha256: str,
    change_control_public_key_path: Path,
    model_contract_sha256: str,
    score_model_version: str,
    scoring_contract_version: str,
    effective_from: str,
    effective_to: str = "",
    expected_alpha_at_full: float,
    optimizer_cap: float,
    generated_at_utc: str,
) -> dict[str, Any]:
    """Verify a separately signed manual change and build one bounded lock."""

    _, candidate_file_hash, candidate = _read_json_once(
        activation_candidate_path,
        label="manual activation candidate",
    )
    if candidate_file_hash != exact_sha256(
        expected_activation_candidate_sha256,
        label="manual activation candidate sha256",
    ):
        raise ValueError("manual activation candidate bytes changed")
    _validate_activation_candidate(candidate, family=family, scope_id=scope_id)

    model_hash = exact_sha256(model_contract_sha256, label="model contract sha256")
    model_version = str(score_model_version or "").strip()
    scoring_version = str(scoring_contract_version or "").strip()
    if not model_version or not scoring_version:
        raise ValueError("score/scoring contract versions must be non-empty")
    effective_start = _iso_date(effective_from, label="effective_from")
    effective_end = _iso_date(effective_to, label="effective_to", allow_blank=True)
    assert effective_start is not None
    if effective_end is not None and effective_end < effective_start:
        raise ValueError("effective_to precedes effective_from")
    alpha = _finite_positive(
        expected_alpha_at_full,
        label="expected_alpha_at_full",
        maximum=1.0,
    )
    cap = _finite_positive(optimizer_cap, label="optimizer_cap", maximum=1.0)

    authority, change_registry_hash = _change_control_authority(
        family,
        public_key_path=change_control_public_key_path,
    )
    authority_identity = authority.identity()
    review_identity = dict(candidate.get("review_authority") or {})
    if (
        not review_identity.get("authority_id")
        or not review_identity.get("public_key_spki_sha256")
        or authority_identity["authority_id"] == review_identity["authority_id"]
        or authority_identity["public_key_spki_sha256"]
        == review_identity["public_key_spki_sha256"]
    ):
        raise ValueError(
            "production change-control authority is not separate from evidence review"
        )

    receipt_bytes, receipt_hash, receipt = _read_json_once(
        change_receipt_path,
        label="production change-control receipt",
    )
    canonical_receipt_bytes = (
        json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    if receipt_bytes != canonical_receipt_bytes:
        raise ValueError(
            "production change-control receipt must use canonical JSON bytes"
        )
    if receipt_hash != exact_sha256(
        expected_change_receipt_sha256,
        label="production change-control receipt sha256",
    ):
        raise ValueError("production change-control receipt bytes changed")
    authority.verify_snapshot(receipt_bytes, receipt_hash, receipt)
    expected_receipt = {
        "schema_version": CHANGE_CONTROL_RECEIPT_SCHEMA,
        "family": family,
        "scope_id": scope_id,
        "activation_candidate_sha256": candidate_file_hash,
        "activation_candidate_payload_sha256": candidate["payload_sha256"],
        "evaluation_sha256": candidate["evaluation_sha256"],
        "scope_verdict_sha256": candidate["scope_verdict_sha256"],
        "review_receipt_sha256": candidate["review_receipt_sha256"],
        "decision": "authorize_bounded_production_activation",
        "model_contract_sha256": model_hash,
        "score_model_version": model_version,
        "scoring_contract_version": scoring_version,
        "effective_from": effective_start.isoformat(),
        "effective_to": effective_end.isoformat() if effective_end else "",
        "approved_expected_alpha_at_full": alpha,
        "approved_optimizer_cap": cap,
        "automatic_config_write_authorized": False,
    }
    for field, expected in expected_receipt.items():
        if receipt.get(field) != expected:
            raise ValueError(f"production change-control receipt changed field: {field}")

    candidate_time = exact_utc(
        candidate.get("generated_at_utc"), label="activation candidate generation time"
    )
    approved_time = exact_utc(
        receipt.get("approved_at_utc"), label="production change approval time"
    )
    generated_time = exact_utc(
        generated_at_utc, label="production lock generation time"
    )
    if not candidate_time <= approved_time <= generated_time:
        raise ValueError("production change-control chronology is invalid")
    if effective_start <= approved_time.date():
        raise ValueError("effective_from must follow production change approval date")

    body: dict[str, Any] = {
        "schema_version": PRODUCTION_ACTIVATION_LOCK_SCHEMA,
        "family": family,
        "scope_id": scope_id,
        "status": "active_approved",
        "generated_at_utc": generated_time.isoformat(),
        "effective_from": effective_start.isoformat(),
        "effective_to": effective_end.isoformat() if effective_end else "",
        "activation_candidate": candidate,
        "activation_candidate_sha256": candidate_file_hash,
        "change_control_receipt": receipt,
        "change_control_receipt_sha256": receipt_hash,
        "change_control_authority": authority_identity,
        "change_control_registry_sha256": change_registry_hash,
        "model_contract_sha256": model_hash,
        "score_model_version": model_version,
        "scoring_contract_version": scoring_version,
        "expected_alpha_at_full": alpha,
        "optimizer_cap": cap,
        "automatic_config_write_authorized": False,
        "production_activation_authorized": True,
        "portfolio_write_enabled": True,
    }
    body["lock_id"] = "future_lock_" + canonical_sha256(body)[:24]
    body["payload_sha256"] = canonical_sha256(body)
    return body


def validate_production_activation_lock(
    payload: Mapping[str, Any],
    *,
    change_control_public_key_path: Path,
) -> dict[str, Any]:
    """Independently revalidate one embedded, signed production lock."""

    if payload.get("schema_version") != PRODUCTION_ACTIVATION_LOCK_SCHEMA:
        raise ValueError("unsupported production activation lock schema")
    supplied_hash = exact_sha256(
        payload.get("payload_sha256"), label="production lock payload sha256"
    )
    if _self_hash(payload) != supplied_hash:
        raise ValueError("production activation lock payload was tampered")
    family = str(payload.get("family") or "")
    scope_id = str(payload.get("scope_id") or "")
    if not family or not scope_id:
        raise ValueError("production activation lock lacks family/scope identity")
    candidate = dict(payload.get("activation_candidate") or {})
    _validate_activation_candidate(candidate, family=family, scope_id=scope_id)
    receipt = dict(payload.get("change_control_receipt") or {})
    receipt_bytes = (
        json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    receipt_hash = hashlib.sha256(receipt_bytes).hexdigest()
    if receipt_hash != exact_sha256(
        payload.get("change_control_receipt_sha256"),
        label="embedded change-control receipt sha256",
    ):
        raise ValueError("embedded change-control receipt bytes changed")
    authority, registry_hash = _change_control_authority(
        family,
        public_key_path=change_control_public_key_path,
    )
    if registry_hash != payload.get("change_control_registry_sha256"):
        raise ValueError("production lock uses a different change-control registry")
    if authority.identity() != dict(payload.get("change_control_authority") or {}):
        raise ValueError("production lock change-control identity changed")
    authority.verify_snapshot(receipt_bytes, receipt_hash, receipt)

    expected = {
        "schema_version": CHANGE_CONTROL_RECEIPT_SCHEMA,
        "family": family,
        "scope_id": scope_id,
        "activation_candidate_sha256": payload.get("activation_candidate_sha256"),
        "activation_candidate_payload_sha256": candidate.get("payload_sha256"),
        "evaluation_sha256": candidate.get("evaluation_sha256"),
        "scope_verdict_sha256": candidate.get("scope_verdict_sha256"),
        "review_receipt_sha256": candidate.get("review_receipt_sha256"),
        "decision": "authorize_bounded_production_activation",
        "model_contract_sha256": payload.get("model_contract_sha256"),
        "score_model_version": payload.get("score_model_version"),
        "scoring_contract_version": payload.get("scoring_contract_version"),
        "effective_from": payload.get("effective_from"),
        "effective_to": payload.get("effective_to"),
        "approved_expected_alpha_at_full": payload.get("expected_alpha_at_full"),
        "approved_optimizer_cap": payload.get("optimizer_cap"),
        "automatic_config_write_authorized": False,
    }
    for field, value in expected.items():
        if receipt.get(field) != value:
            raise ValueError(f"production lock/receipt mismatch: {field}")
    if (
        payload.get("status") != "active_approved"
        or payload.get("production_activation_authorized") is not True
        or payload.get("portfolio_write_enabled") is not True
        or payload.get("automatic_config_write_authorized") is not False
    ):
        raise ValueError("production activation lock does not authorize bounded use")
    _finite_positive(
        payload.get("expected_alpha_at_full"),
        label="lock expected_alpha_at_full",
        maximum=1.0,
    )
    _finite_positive(payload.get("optimizer_cap"), label="lock optimizer_cap", maximum=1.0)
    start = _iso_date(payload.get("effective_from"), label="lock effective_from")
    end = _iso_date(
        payload.get("effective_to"), label="lock effective_to", allow_blank=True
    )
    assert start is not None
    if end is not None and end < start:
        raise ValueError("production lock effective range is invalid")
    return dict(payload)


def build_activation_registry(
    locks: Iterable[Mapping[str, Any]],
    *,
    family: str,
    generated_at_utc: str,
) -> dict[str, Any]:
    """Build a deterministic registry; individual signatures are rechecked on read."""

    material = [dict(item) for item in locks]
    if not material:
        raise ValueError("production activation registry cannot be empty")
    keys: set[tuple[str, str]] = set()
    for lock in material:
        if lock.get("family") != family:
            raise ValueError("production activation registry mixes model families")
        key = (str(lock.get("scope_id") or ""), str(lock.get("effective_from") or ""))
        if not all(key) or key in keys:
            raise ValueError("production activation registry has duplicate/blank lock identity")
        keys.add(key)
    material.sort(key=lambda item: (str(item["scope_id"]), str(item["effective_from"])))
    body: dict[str, Any] = {
        "schema_version": PRODUCTION_ACTIVATION_REGISTRY_SCHEMA,
        "family": family,
        "status": "active_reviewed",
        "generated_at_utc": exact_utc(
            generated_at_utc, label="activation registry generation time"
        ).isoformat(),
        "locks": material,
    }
    body["payload_sha256"] = canonical_sha256(body)
    return body


def validate_activation_registry(
    payload: Mapping[str, Any],
    *,
    family: str,
    change_control_public_key_path: Path,
) -> dict[str, Any]:
    if (
        payload.get("schema_version") != PRODUCTION_ACTIVATION_REGISTRY_SCHEMA
        or payload.get("family") != family
        or payload.get("status") != "active_reviewed"
    ):
        raise ValueError("unsupported/inactive production activation registry")
    supplied_hash = exact_sha256(
        payload.get("payload_sha256"), label="activation registry payload sha256"
    )
    if _self_hash(payload) != supplied_hash:
        raise ValueError("production activation registry payload was tampered")
    raw_locks = payload.get("locks")
    if not isinstance(raw_locks, list) or not raw_locks:
        raise ValueError("production activation registry has no locks")
    validated = [
        validate_production_activation_lock(
            dict(lock),
            change_control_public_key_path=change_control_public_key_path,
        )
        for lock in raw_locks
        if isinstance(lock, Mapping)
    ]
    if len(validated) != len(raw_locks):
        raise ValueError("production activation registry contains a non-object lock")
    identities = [
        (str(lock["scope_id"]), str(lock["effective_from"])) for lock in validated
    ]
    if len(set(identities)) != len(identities):
        raise ValueError("production activation registry contains duplicate lock identities")
    return dict(payload)


def effective_scope_locks(
    registry: Mapping[str, Any],
    *,
    asof_date: str,
) -> dict[str, dict[str, Any]]:
    asof = _iso_date(asof_date, label="activation registry as-of")
    assert asof is not None
    selected: dict[str, dict[str, Any]] = {}
    for raw in registry.get("locks", []):
        lock = dict(raw)
        start = _iso_date(lock.get("effective_from"), label="lock effective_from")
        end = _iso_date(lock.get("effective_to"), label="lock effective_to", allow_blank=True)
        assert start is not None
        if start <= asof and (end is None or asof <= end):
            scope = str(lock["scope_id"])
            previous = selected.get(scope)
            if previous is not None and str(previous["effective_from"]) >= start.isoformat():
                continue
            selected[scope] = lock
    return selected


__all__ = [
    "APPROVED_CHANGE_CONTROL_REGISTRY_SHA256",
    "CHANGE_CONTROL_RECEIPT_SCHEMA",
    "CHANGE_CONTROL_REGISTRY",
    "PRODUCTION_ACTIVATION_LOCK_SCHEMA",
    "PRODUCTION_ACTIVATION_REGISTRY_SCHEMA",
    "build_activation_registry",
    "build_production_activation_lock",
    "effective_scope_locks",
    "validate_activation_registry",
    "validate_production_activation_lock",
]
