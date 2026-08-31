from __future__ import annotations

import hashlib
import inspect
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from future_only_evidence.canonical_trust import (
    CANONICAL_TRUST_REGISTRY,
    TIMESTAMP_SCHEMA,
    _strict_int,
    load_canonical_trust_bundle,
    validate_external_timestamp,
)


def test_default_canonical_trust_registry_is_fail_closed() -> None:
    with pytest.raises(ValueError, match="unconfigured"):
        load_canonical_trust_bundle(
            "transportation",
            evidence_public_key_path=Path("missing-evidence.pem"),
            timestamp_public_key_path=Path("missing-timestamp.pem"),
            market_data_public_key_path=Path("missing-market.pem"),
        )


def test_evidence_caller_cannot_substitute_an_alternate_registry() -> None:
    parameters = inspect.signature(load_canonical_trust_bundle).parameters
    assert "registry_path" not in parameters
    assert CANONICAL_TRUST_REGISTRY.name == "canonical_trust_roots.json"


@pytest.mark.parametrize("value", [True, "1", 1.5])
def test_registry_and_genesis_integer_parser_rejects_coercible_values(value: object) -> None:
    with pytest.raises(ValueError, match="canonical integer"):
        _strict_int(value, label="signed registry integer", minimum=0)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("subject_bytes", True),
        ("log_sequence", "1"),
        ("slot_inclusion_count", 1.0),
        ("checkpoint_tree_size", "1"),
    ],
)
def test_signed_timestamp_integer_fields_reject_noncanonical_types(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    subject = tmp_path / "subject.json"
    subject.write_bytes(b"x")
    subject_sha = hashlib.sha256(b"x").hexdigest()
    previous = "1" * 64
    payload: dict[str, object] = {
        "schema_version": TIMESTAMP_SCHEMA,
        "log_id": "test-log",
        "subject_sha256": subject_sha,
        "subject_bytes": 1,
        "log_sequence": 1,
        "observed_at_utc": "2026-09-01T00:00:00+00:00",
        "previous_log_head_sha256": previous,
        "family": "consumer_defensive",
        "policy_id": "policy-v1",
        "subject_role": "registration_receipt",
        "unique_slot_id": "consumer_defensive:policy-v1:registration",
        "slot_inclusion_count": 1,
        "checkpoint_sha256": "2" * 64,
        "inclusion_proof_sha256": "3" * 64,
        "checkpoint_tree_size": 1,
        "checkpoint_inclusion_verified": True,
        "checkpoint_at_utc": "2026-09-01T00:00:01+00:00",
    }
    payload[field] = value
    receipt = tmp_path / "timestamp.json"
    raw = json.dumps(payload).encode("utf-8")
    receipt.write_bytes(raw)

    class SnapshotAuthority:
        def verify_snapshot(self, *_args: object) -> bool:
            return True

    bundle = SimpleNamespace(
        log_id="test-log",
        activated_at_utc=datetime(2026, 8, 1, tzinfo=timezone.utc),
        timestamp_log=SnapshotAuthority(),
    )
    with pytest.raises(ValueError, match="canonical integer"):
        validate_external_timestamp(
            subject_path=subject,
            timestamp_receipt_path=receipt,
            expected_timestamp_receipt_sha256=hashlib.sha256(raw).hexdigest(),
            expected_subject_sha256=subject_sha,
            bundle=bundle,  # type: ignore[arg-type]
            expected_previous_log_head_sha256=previous,
            expected_previous_log_sequence=0,
            expected_family="consumer_defensive",
            expected_policy_id="policy-v1",
            expected_subject_role="registration_receipt",
            expected_slot_id="consumer_defensive:policy-v1:registration",
            subject_snapshot_bytes=b"x",
        )
