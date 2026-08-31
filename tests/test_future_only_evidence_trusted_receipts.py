from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from future_only_evidence.protocol import canonical_sha256, file_sha256
from future_only_evidence.trusted_receipts import PinnedEd25519Authority


def _receipt(tmp_path: Path) -> tuple[Path, Path, PinnedEd25519Authority]:
    private = Ed25519PrivateKey.generate()
    public_path = tmp_path / "authority.pem"
    public_path.write_bytes(
        private.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    unsigned = {
        "schema_version": "test_receipt_v1",
        "authority_id": "independent-model-risk",
        "captured_at_utc": "2026-08-24T20:00:00+00:00",
    }
    signed_hash = canonical_sha256(unsigned)
    signed_bytes = json.dumps(
        unsigned,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    payload = {
        **unsigned,
        "signed_payload_sha256": signed_hash,
        "signature_base64": base64.b64encode(private.sign(signed_bytes)).decode("ascii"),
    }
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(payload), encoding="utf-8")
    authority = PinnedEd25519Authority(
        authority_id="independent-model-risk",
        public_key_path=public_path,
        expected_public_key_sha256=file_sha256(public_path),
    )
    return receipt_path, public_path, authority


def test_pinned_ed25519_receipt_verifies(tmp_path: Path) -> None:
    receipt, _, authority = _receipt(tmp_path)
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert authority.verify(receipt, file_sha256(receipt), payload) is True


def test_unregistered_always_true_authority_cannot_substitute(tmp_path: Path) -> None:
    receipt, public_key, _ = _receipt(tmp_path)
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    wrong = PinnedEd25519Authority(
        authority_id="self-reported-always-true",
        public_key_path=public_key,
        expected_public_key_sha256=file_sha256(public_key),
    )
    with pytest.raises(ValueError, match="not the pinned authority"):
        wrong.verify(receipt, file_sha256(receipt), payload)


def test_tampered_signed_receipt_is_rejected(tmp_path: Path) -> None:
    receipt, _, authority = _receipt(tmp_path)
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["captured_at_utc"] = "2026-08-25T20:00:00+00:00"
    receipt.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="digest mismatch"):
        authority.verify(receipt, file_sha256(receipt), payload)


def test_caller_payload_cannot_differ_from_hashed_receipt_bytes(tmp_path: Path) -> None:
    receipt, _, authority = _receipt(tmp_path)
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["captured_at_utc"] = "2026-08-25T20:00:00+00:00"
    with pytest.raises(ValueError, match="caller-supplied.*differs"):
        authority.verify(receipt, file_sha256(receipt), payload)


def test_verify_reads_public_key_and_receipt_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt, public_key, authority = _receipt(tmp_path)
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    original = Path.read_bytes
    counts = {str(receipt.resolve()): 0, str(public_key.resolve()): 0}

    def counted(path: Path) -> bytes:
        resolved = str(path.resolve())
        if resolved in counts:
            counts[resolved] += 1
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", counted)
    assert authority.verify(receipt, file_sha256(receipt), payload) is True
    assert counts[str(receipt.resolve())] == 1
    assert counts[str(public_key.resolve())] == 1
