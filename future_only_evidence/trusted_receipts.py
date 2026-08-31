"""Pinned Ed25519 authority verification for prospective evidence receipts."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .protocol import canonical_sha256, exact_sha256


def _signed_bytes(payload: Mapping[str, Any]) -> bytes:
    unsigned = dict(payload)
    unsigned.pop("signature_base64", None)
    unsigned.pop("signed_payload_sha256", None)
    return json.dumps(
        unsigned,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


@dataclass(frozen=True)
class PinnedEd25519Authority:
    authority_id: str
    public_key_path: Path
    expected_public_key_sha256: str

    def _pinned_key_bytes(self) -> tuple[Path, bytes, str]:
        resolved = self.public_key_path.expanduser().resolve()
        key_bytes = resolved.read_bytes()
        actual = hashlib.sha256(key_bytes).hexdigest()
        expected = exact_sha256(
            self.expected_public_key_sha256,
            label="trusted public-key sha256",
        )
        if actual != expected:
            raise ValueError("trusted public-key SHA-256 mismatch")
        return resolved, key_bytes, actual

    def identity(self) -> dict[str, str]:
        resolved, key_bytes, actual = self._pinned_key_bytes()
        key = serialization.load_pem_public_key(key_bytes)
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError("trusted receipt key is not Ed25519")
        canonical_der = key.public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return {
            "authority_id": self.authority_id,
            "public_key_path": str(resolved),
            "public_key_sha256": actual,
            "public_key_spki_sha256": hashlib.sha256(canonical_der).hexdigest(),
            "algorithm": "Ed25519",
        }

    def verify_snapshot(
        self,
        receipt_bytes: bytes,
        actual_sha256: str,
        payload: Mapping[str, Any],
    ) -> bool:
        """Verify the exact receipt bytes already hashed and parsed by the caller."""

        receipt_hash = hashlib.sha256(receipt_bytes).hexdigest()
        if receipt_hash != exact_sha256(actual_sha256, label="receipt sha256"):
            raise ValueError("receipt snapshot differs from the claimed SHA-256")
        try:
            parsed_payload = json.loads(receipt_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("receipt bytes are not one valid UTF-8 JSON object") from exc
        if not isinstance(parsed_payload, dict) or parsed_payload != dict(payload):
            raise ValueError("caller-supplied receipt payload differs from hashed receipt bytes")
        if str(payload.get("authority_id") or "") != self.authority_id:
            raise ValueError("receipt authority is not the pinned authority")
        signed = _signed_bytes(payload)
        if payload.get("signed_payload_sha256") != canonical_sha256(
            {
                key: value
                for key, value in payload.items()
                if key not in {"signature_base64", "signed_payload_sha256"}
            }
        ):
            raise ValueError("receipt signed-payload digest mismatch")
        try:
            signature = base64.b64decode(str(payload["signature_base64"]), validate=True)
        except (KeyError, ValueError) as exc:
            raise ValueError("receipt signature is not valid base64") from exc
        _, key_bytes, _ = self._pinned_key_bytes()
        key = serialization.load_pem_public_key(key_bytes)
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError("trusted receipt key is not Ed25519")
        try:
            key.verify(signature, signed)
        except InvalidSignature as exc:
            raise ValueError("receipt Ed25519 signature is invalid") from exc
        return True

    def verify(self, path: Path, actual_sha256: str, payload: Mapping[str, Any]) -> bool:
        """Backward-compatible path API; the file is read exactly once."""

        resolved_receipt = Path(path).expanduser().resolve()
        return self.verify_snapshot(
            resolved_receipt.read_bytes(),
            actual_sha256,
            payload,
        )


def registered_authority(
    plan: Mapping[str, Any],
    *,
    public_key_path: Path,
) -> PinnedEd25519Authority:
    identity = plan.get("trusted_receipt_authority")
    if not isinstance(identity, dict):
        raise ValueError("plan does not register a trusted receipt authority")
    if identity.get("algorithm") != "Ed25519":
        raise ValueError("registered receipt authority must use Ed25519")
    return PinnedEd25519Authority(
        authority_id=str(identity.get("authority_id") or ""),
        public_key_path=public_key_path,
        expected_public_key_sha256=str(identity.get("public_key_sha256") or ""),
    )


__all__ = ["PinnedEd25519Authority", "registered_authority"]
