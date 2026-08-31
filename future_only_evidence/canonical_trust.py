"""Code-routed trust roots, external timestamps, and market export attestations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from cryptography.hazmat.primitives import serialization

from .protocol import canonical_sha256, exact_sha256
from .canonical_values import exact_utc
from .trusted_receipts import PinnedEd25519Authority


CANONICAL_TRUST_REGISTRY = Path(__file__).with_name("canonical_trust_roots.json")
# Deliberately blank until a separately reviewed deployment release pins the
# exact registry bytes.  Editing the JSON beside this module cannot activate a
# trust root by itself.
APPROVED_CANONICAL_TRUST_REGISTRY_SHA256 = ""
TRUST_SCHEMA = "future_evidence_canonical_trust_registry_v1"
TIMESTAMP_SCHEMA = "future_external_timestamp_log_receipt_v1"
MARKET_EXPORT_SCHEMA = "future_market_data_export_receipt_v1"


def _utc(value: Any, *, label: str) -> datetime:
    return exact_utc(value, label=label)


def _strict_int(value: Any, *, label: str, minimum: int | None = None) -> int:
    if type(value) is not int:
        raise ValueError(f"{label} must be a canonical integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} is below its minimum")
    return value


@dataclass(frozen=True)
class CanonicalTrustBundle:
    family: str
    evidence_seal: PinnedEd25519Authority
    timestamp_log: PinnedEd25519Authority
    market_data_export: PinnedEd25519Authority
    log_id: str
    genesis_log_head_sha256: str
    genesis_log_sequence: int
    activated_at_utc: datetime
    allowed_provider_ids: frozenset[str]
    allowed_dataset_ids: frozenset[str]
    required_currency: str
    required_exchange_mic_policy: str
    required_adjustment_policy_id: str
    required_price_convention_id: str
    benchmark_asset_ids: Mapping[str, str]
    release_approval_id: str
    registry_revision: int
    registry_sha256: str

    def audit(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "registry_path": str(CANONICAL_TRUST_REGISTRY.resolve()),
            "registry_sha256": self.registry_sha256,
            "registry_revision": self.registry_revision,
            "activated_at_utc": self.activated_at_utc.isoformat(),
            "evidence_seal": self.evidence_seal.identity(),
            "timestamp_log": {**self.timestamp_log.identity(), "log_id": self.log_id},
            "market_data_export": {
                **self.market_data_export.identity(),
                "allowed_provider_ids": sorted(self.allowed_provider_ids),
                "allowed_dataset_ids": sorted(self.allowed_dataset_ids),
                "required_currency": self.required_currency,
                "required_exchange_mic_policy": self.required_exchange_mic_policy,
                "required_adjustment_policy_id": self.required_adjustment_policy_id,
                "required_price_convention_id": self.required_price_convention_id,
                "benchmark_asset_ids": dict(self.benchmark_asset_ids),
            },
            "genesis_log_head_sha256": self.genesis_log_head_sha256,
            "genesis_log_sequence": self.genesis_log_sequence,
            "release_approval_id": self.release_approval_id,
        }


def _authority(
    definition: Mapping[str, Any],
    *,
    key_path: Path,
    expected_purpose: str,
) -> PinnedEd25519Authority:
    if (
        definition.get("algorithm") != "Ed25519"
        or definition.get("purpose") != expected_purpose
        or not definition.get("authority_id")
        or not definition.get("public_key_sha256")
    ):
        raise ValueError(f"canonical trust role is unconfigured/invalid: {expected_purpose}")
    return PinnedEd25519Authority(
        authority_id=str(definition["authority_id"]),
        public_key_path=Path(key_path),
        expected_public_key_sha256=str(definition["public_key_sha256"]),
    )


def load_canonical_trust_bundle(
    family: str,
    *,
    evidence_public_key_path: Path,
    timestamp_public_key_path: Path,
    market_data_public_key_path: Path,
) -> CanonicalTrustBundle:
    # Registry path is intentionally not an argument: evidence submitters may
    # not substitute their own active registry and keys.
    registry_bytes = CANONICAL_TRUST_REGISTRY.read_bytes()
    actual_registry_hash = hashlib.sha256(registry_bytes).hexdigest()
    payload = json.loads(registry_bytes.decode("utf-8"))
    if payload.get("schema_version") != TRUST_SCHEMA:
        raise ValueError("unsupported canonical trust-root registry")
    if payload.get("status") != "active_reviewed":
        raise ValueError("canonical trust roots are unconfigured; future clock remains stopped")
    if not APPROVED_CANONICAL_TRUST_REGISTRY_SHA256:
        raise ValueError(
            "canonical trust registry has no independently approved deployment hash; "
            "future clock remains unconfigured"
        )
    if actual_registry_hash != exact_sha256(
        APPROVED_CANONICAL_TRUST_REGISTRY_SHA256,
        label="approved canonical trust registry sha256",
    ):
        raise ValueError("canonical trust registry bytes differ from the approved deployment release")
    revision = _strict_int(
        payload.get("registry_revision"),
        label="canonical trust registry revision",
        minimum=1,
    )
    activated_at = _utc(payload.get("activated_at_utc"), label="trust registry activation")
    families = payload.get("families")
    if not isinstance(families, dict) or not isinstance(families.get(family), dict):
        raise ValueError(f"canonical trust roots missing family={family}")
    family_log_ids = [
        str(definition.get("timestamp_log", {}).get("log_id") or "")
        for definition in families.values()
        if isinstance(definition, dict)
    ]
    if (
        len(family_log_ids) != len(families)
        or any(not value for value in family_log_ids)
        or len(set(family_log_ids)) != len(family_log_ids)
    ):
        raise ValueError("each future-evidence family requires a distinct append-only log")
    definition = families[family]
    evidence_definition = definition.get("evidence_seal")
    timestamp_definition = definition.get("timestamp_log")
    market_definition = definition.get("market_data_export")
    if not all(isinstance(item, dict) for item in (evidence_definition, timestamp_definition, market_definition)):
        raise ValueError("canonical family trust roles are incomplete")
    log_id = str(timestamp_definition.get("log_id") or "")
    genesis_head = str(timestamp_definition.get("genesis_log_head_sha256") or "")
    genesis_sequence = _strict_int(
        timestamp_definition.get("genesis_log_sequence"),
        label="timestamp-log genesis sequence",
        minimum=0,
    )
    providers = frozenset(str(value) for value in market_definition.get("allowed_provider_ids") or [])
    datasets = frozenset(str(value) for value in market_definition.get("allowed_dataset_ids") or [])
    release_approval_id = str(payload.get("release_approval_id") or "")
    required_currency = str(market_definition.get("required_currency") or "")
    required_exchange_policy = str(market_definition.get("required_exchange_mic_policy") or "")
    required_adjustment = str(market_definition.get("required_adjustment_policy_id") or "")
    required_price = str(market_definition.get("required_price_convention_id") or "")
    benchmark_asset_ids = {
        str(key).upper(): str(value)
        for key, value in dict(market_definition.get("benchmark_asset_ids") or {}).items()
    }
    if (
        not log_id
        or not genesis_head
        or not providers
        or not datasets
        or not release_approval_id
        or not required_currency
        or not required_exchange_policy
        or not required_adjustment
        or not required_price
        or not benchmark_asset_ids
    ):
        raise ValueError("canonical timestamp/market-data allowlists are unconfigured")
    exact_sha256(genesis_head, label="timestamp-log genesis head sha256")
    role_definitions = (evidence_definition, timestamp_definition, market_definition)
    authority_ids = [str(item.get("authority_id") or "") for item in role_definitions]
    authority_keys = [str(item.get("public_key_sha256") or "") for item in role_definitions]
    if len(set(authority_ids)) != 3 or len(set(authority_keys)) != 3:
        raise ValueError("canonical evidence, timestamp, and market authorities must be independent")
    canonical_key_fingerprints: list[str] = []
    for key_path in (
        evidence_public_key_path,
        timestamp_public_key_path,
        market_data_public_key_path,
    ):
        key_bytes = Path(key_path).read_bytes()
        public_key = serialization.load_pem_public_key(key_bytes)
        canonical_der = public_key.public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        canonical_key_fingerprints.append(hashlib.sha256(canonical_der).hexdigest())
    if len(set(canonical_key_fingerprints)) != 3:
        raise ValueError("canonical trust roles reuse the same Ed25519 public-key material")
    bundle = CanonicalTrustBundle(
        family=family,
        evidence_seal=_authority(
            evidence_definition,
            key_path=evidence_public_key_path,
            expected_purpose="evidence_content_sealing",
        ),
        timestamp_log=_authority(
            timestamp_definition,
            key_path=timestamp_public_key_path,
            expected_purpose="external_append_only_timestamping",
        ),
        market_data_export=_authority(
            market_definition,
            key_path=market_data_public_key_path,
            expected_purpose="independent_market_data_export_attestation",
        ),
        log_id=log_id,
        genesis_log_head_sha256=genesis_head,
        genesis_log_sequence=genesis_sequence,
        activated_at_utc=activated_at,
        allowed_provider_ids=providers,
        allowed_dataset_ids=datasets,
        required_currency=required_currency,
        required_exchange_mic_policy=required_exchange_policy,
        required_adjustment_policy_id=required_adjustment,
        required_price_convention_id=required_price,
        benchmark_asset_ids=benchmark_asset_ids,
        release_approval_id=release_approval_id,
        registry_revision=revision,
        registry_sha256=actual_registry_hash,
    )
    bundle.audit()
    return bundle


def validate_external_timestamp(
    *,
    subject_path: Path,
    timestamp_receipt_path: Path,
    expected_timestamp_receipt_sha256: str,
    expected_subject_sha256: str,
    bundle: CanonicalTrustBundle,
    expected_previous_log_head_sha256: str,
    expected_previous_log_sequence: int,
    expected_family: str,
    expected_policy_id: str,
    expected_subject_role: str,
    expected_slot_id: str,
    subject_snapshot_bytes: bytes | None = None,
) -> dict[str, Any]:
    subject_resolved = Path(subject_path).expanduser().resolve()
    subject_bytes = (
        bytes(subject_snapshot_bytes)
        if subject_snapshot_bytes is not None
        else subject_resolved.read_bytes()
    )
    subject_hash = hashlib.sha256(subject_bytes).hexdigest()
    if subject_hash != exact_sha256(
        expected_subject_sha256, label="timestamp subject sha256"
    ):
        raise ValueError("external timestamp subject differs from the previously verified bytes")
    receipt_path = Path(timestamp_receipt_path).expanduser().resolve()
    receipt_bytes = receipt_path.read_bytes()
    receipt_hash = hashlib.sha256(receipt_bytes).hexdigest()
    if receipt_hash != exact_sha256(
        expected_timestamp_receipt_sha256,
        label="external timestamp receipt sha256",
    ):
        raise ValueError("external timestamp receipt hash mismatch")
    try:
        payload = json.loads(receipt_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("external timestamp receipt must be valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("external timestamp receipt must be a JSON object")
    bundle.timestamp_log.verify_snapshot(receipt_bytes, receipt_hash, payload)
    if (
        payload.get("schema_version") != TIMESTAMP_SCHEMA
        or payload.get("log_id") != bundle.log_id
        or payload.get("subject_sha256") != subject_hash
        or _strict_int(
            payload.get("subject_bytes"),
            label="external timestamp subject byte count",
            minimum=0,
        )
        != len(subject_bytes)
    ):
        raise ValueError("external timestamp receipt does not bind exact subject bytes/log")
    sequence = _strict_int(
        payload.get("log_sequence"),
        label="external timestamp log sequence",
        minimum=0,
    )
    previous_sequence = _strict_int(
        expected_previous_log_sequence,
        label="expected previous timestamp-log sequence",
        minimum=0,
    )
    if sequence != previous_sequence + 1:
        raise ValueError("external timestamp log sequence is not the exact successor")
    observed_at = _utc(payload.get("observed_at_utc"), label="external timestamp")
    if observed_at < bundle.activated_at_utc:
        raise ValueError("external timestamp predates trusted-authority activation")
    previous = str(payload.get("previous_log_head_sha256") or "")
    if previous != exact_sha256(
        expected_previous_log_head_sha256,
        label="previous timestamp-log head sha256",
    ):
        raise ValueError("external timestamp receipt is not the expected append-only successor")
    expected_identity = {
        "family": expected_family,
        "policy_id": expected_policy_id,
        "subject_role": expected_subject_role,
        "unique_slot_id": expected_slot_id,
    }
    for field, expected in expected_identity.items():
        if payload.get(field) != expected:
            raise ValueError(f"external timestamp changed canonical slot identity: {field}")
    if _strict_int(
        payload.get("slot_inclusion_count"),
        label="timestamp slot inclusion count",
        minimum=0,
    ) != 1:
        raise ValueError("external timestamp log did not prove a unique slot")
    checkpoint_sha = exact_sha256(payload.get("checkpoint_sha256"), label="timestamp checkpoint sha256")
    inclusion_sha = exact_sha256(payload.get("inclusion_proof_sha256"), label="timestamp inclusion proof sha256")
    tree_size = _strict_int(
        payload.get("checkpoint_tree_size"),
        label="timestamp checkpoint tree size",
        minimum=0,
    )
    if tree_size < sequence or payload.get("checkpoint_inclusion_verified") is not True:
        raise ValueError(
            "external timestamp authority did not attest checkpoint inclusion"
        )
    checkpoint_at = _utc(payload.get("checkpoint_at_utc"), label="timestamp checkpoint")
    if checkpoint_at < observed_at:
        raise ValueError("timestamp checkpoint predates the logged observation")
    return {
        "timestamp_receipt_path": str(receipt_path),
        "timestamp_receipt_sha256": receipt_hash,
        "subject_sha256": subject_hash,
        "log_id": bundle.log_id,
        "log_sequence": sequence,
        "previous_log_head_sha256": previous,
        "observed_at_utc": observed_at.isoformat(),
        "family": expected_family,
        "policy_id": expected_policy_id,
        "subject_role": expected_subject_role,
        "unique_slot_id": expected_slot_id,
        "checkpoint_sha256": checkpoint_sha,
        "checkpoint_tree_size": tree_size,
        "checkpoint_at_utc": checkpoint_at.isoformat(),
        "inclusion_proof_sha256": inclusion_sha,
        "checkpoint_inclusion_authority_attested": True,
        "checkpoint_inclusion_proof_recomputed_locally": False,
        "timestamp_trust_model": (
            "pinned_external_authority_signed_checkpoint_inclusion_claim_v1"
        ),
        "slot_inclusion_count": 1,
        "external_timestamp_pass": True,
    }


def validate_market_data_export_receipt(
    *,
    source_sha256: Mapping[str, str],
    receipt_path: Path,
    expected_receipt_sha256: str,
    bundle: CanonicalTrustBundle,
    expected_benchmark_ticker: str,
    latest_exit_execution_at_utc: str,
    outcome_anchor_at_utc: str,
) -> dict[str, Any]:
    resolved = Path(receipt_path).expanduser().resolve()
    receipt_bytes = resolved.read_bytes()
    actual = hashlib.sha256(receipt_bytes).hexdigest()
    if actual != exact_sha256(expected_receipt_sha256, label="market export receipt sha256"):
        raise ValueError("market-data export receipt hash mismatch")
    try:
        payload = json.loads(receipt_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("market-data export receipt must be valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("market-data export receipt must be a JSON object")
    bundle.market_data_export.verify_snapshot(receipt_bytes, actual, payload)
    if payload.get("schema_version") != MARKET_EXPORT_SCHEMA:
        raise ValueError("unsupported market-data export receipt")
    if payload.get("family") != bundle.family or payload.get("source_sha256") != dict(source_sha256):
        raise ValueError("market-data export receipt does not bind exact source bytes/family")
    provider = str(payload.get("provider_id") or "")
    dataset = str(payload.get("dataset_id") or "")
    if provider not in bundle.allowed_provider_ids or dataset not in bundle.allowed_dataset_ids:
        raise ValueError("market-data provider/dataset is not allowlisted")
    required_identity = (
        "export_id",
        "exported_at_utc",
        "query_sha256",
        "asset_master_sha256",
        "corporate_actions_sha256",
        "currency",
        "exchange_mic_policy",
        "adjustment_policy_id",
        "price_convention_id",
    )
    if any(not payload.get(field) for field in required_identity):
        raise ValueError("market-data export provenance identity is incomplete")
    _utc(payload["exported_at_utc"], label="market-data export timestamp")
    for field in ("query_sha256", "asset_master_sha256", "corporate_actions_sha256"):
        exact_sha256(payload[field], label=field)
    if source_sha256.get("asset_master") != payload["asset_master_sha256"]:
        raise ValueError("market-data receipt asset-master hash differs from bound source")
    if source_sha256.get("corporate_actions") != payload["corporate_actions_sha256"]:
        raise ValueError("market-data receipt corporate-actions hash differs from bound source")
    exact_policy = {
        "currency": bundle.required_currency,
        "exchange_mic_policy": bundle.required_exchange_mic_policy,
        "adjustment_policy_id": bundle.required_adjustment_policy_id,
        "price_convention_id": bundle.required_price_convention_id,
    }
    for field, expected in exact_policy.items():
        if payload.get(field) != expected:
            raise ValueError(f"market-data export changed canonical policy: {field}")
    benchmark = str(expected_benchmark_ticker).upper()
    if payload.get("benchmark_asset_id") != bundle.benchmark_asset_ids.get(benchmark):
        raise ValueError("market-data export does not bind the canonical benchmark asset id")
    asset_ids = payload.get("asset_ids")
    normalized_asset_ids = (
        {str(ticker).upper(): str(asset_id) for ticker, asset_id in asset_ids.items()}
        if isinstance(asset_ids, dict)
        else {}
    )
    if not normalized_asset_ids or normalized_asset_ids.get(benchmark) != payload.get(
        "benchmark_asset_id"
    ):
        raise ValueError("market-data export lacks immutable asset identities")
    if normalized_asset_ids[benchmark] != bundle.benchmark_asset_ids.get(benchmark):
        raise ValueError("market-data export benchmark asset mapping is not pinned")
    exported_at = _utc(payload["exported_at_utc"], label="market-data export timestamp")
    if exported_at < _utc(latest_exit_execution_at_utc, label="latest outcome exit"):
        raise ValueError("market-data export predates the latest governed outcome exit")
    if exported_at > _utc(outcome_anchor_at_utc, label="outcome anchor"):
        raise ValueError("market-data export was produced after the outcome anchor")
    return {
        "receipt_path": str(resolved),
        "receipt_sha256": actual,
        "provider_id": provider,
        "dataset_id": dataset,
        "export_id": payload["export_id"],
        "exported_at_utc": payload["exported_at_utc"],
        "query_sha256": payload["query_sha256"],
        "asset_master_sha256": payload["asset_master_sha256"],
        "corporate_actions_sha256": payload["corporate_actions_sha256"],
        **exact_policy,
        "benchmark_ticker": benchmark,
        "benchmark_asset_id": payload["benchmark_asset_id"],
        "asset_ids": normalized_asset_ids,
        "asset_ids_sha256": canonical_sha256(normalized_asset_ids),
        "family": bundle.family,
        "source_sha256": dict(source_sha256),
        "market_data_export_attestation_pass": True,
    }


__all__ = [
    "APPROVED_CANONICAL_TRUST_REGISTRY_SHA256",
    "CANONICAL_TRUST_REGISTRY",
    "CanonicalTrustBundle",
    "MARKET_EXPORT_SCHEMA",
    "TIMESTAMP_SCHEMA",
    "load_canonical_trust_bundle",
    "validate_external_timestamp",
    "validate_market_data_export_receipt",
]
