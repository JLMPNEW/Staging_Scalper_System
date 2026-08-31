"""Code-governed out-of-band trust roots for future evidence receipts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .trusted_receipts import PinnedEd25519Authority


DEFAULT_AUTHORITY_REGISTRY = Path(__file__).with_name("trusted_authorities.json")


def load_pinned_authority(
    family: str,
    *,
    public_key_path: Path,
    registry_path: Path = DEFAULT_AUTHORITY_REGISTRY,
) -> tuple[PinnedEd25519Authority, dict[str, Any]]:
    payload = json.loads(Path(registry_path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != "future_evidence_trusted_authority_registry_v1":
        raise ValueError("unsupported trusted-authority registry")
    if payload.get("status") != "active_reviewed":
        raise ValueError("trusted-authority registry is unconfigured; future clock remains stopped")
    authorities = payload.get("authorities")
    if not isinstance(authorities, dict) or family not in authorities:
        raise ValueError(f"trusted authority is not registered for {family}")
    identity = authorities[family]
    if not isinstance(identity, dict) or identity.get("algorithm") != "Ed25519":
        raise ValueError("trusted authority must be a registered Ed25519 identity")
    authority_id = str(identity.get("authority_id") or "")
    key_hash = str(identity.get("public_key_sha256") or "")
    if not authority_id or not key_hash:
        raise ValueError("trusted authority identity/hash is blank")
    authority = PinnedEd25519Authority(
        authority_id=authority_id,
        public_key_path=public_key_path,
        expected_public_key_sha256=key_hash,
    )
    verified = authority.identity()
    return authority, {
        "registry_path": str(Path(registry_path).expanduser().resolve()),
        "registry_schema_version": payload["schema_version"],
        "registry_status": payload["status"],
        "family": family,
        **verified,
    }


__all__ = ["DEFAULT_AUTHORITY_REGISTRY", "load_pinned_authority"]
