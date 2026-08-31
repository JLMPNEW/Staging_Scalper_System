"""Create-only content-addressed archives for prospective evidence inputs."""

from __future__ import annotations

import hashlib
import os
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from .protocol import exact_sha256, file_sha256


def _safe_component(value: str, *, label: str) -> str:
    cleaned = str(value).strip().lower().replace("-", "_")
    if not cleaned or any(not (char.isalnum() or char == "_") for char in cleaned):
        raise ValueError(f"{label} is not a safe archive component")
    return cleaned


def archive_file_once(
    source_path: Path,
    *,
    expected_sha256: str,
    archive_root: Path,
    family: str,
    asof_date: str,
    role: str,
) -> dict[str, Any]:
    """Snapshot one input into a create-only SHA-256 path and verify it twice."""

    source = Path(source_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"prospective source is missing: {source}")
    before = source.stat()
    payload = source.read_bytes()
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("prospective source changed while it was being archived")
    digest = hashlib.sha256(payload).hexdigest()
    if digest != exact_sha256(expected_sha256, label=f"{role} source sha256"):
        raise ValueError(f"prospective source hash mismatch: {role}")
    root = Path(archive_root).expanduser().resolve()
    family_part = _safe_component(family, label="family")
    role_part = _safe_component(role, label="role")
    if type(asof_date) is not str:
        raise ValueError("archive asof_date must be an exact YYYY-MM-DD string")
    try:
        parsed_asof = date.fromisoformat(asof_date)
    except ValueError as exc:
        raise ValueError("archive asof_date must be an exact YYYY-MM-DD string") from exc
    if parsed_asof.isoformat() != asof_date:
        raise ValueError("archive asof_date must be an exact YYYY-MM-DD string")
    date_part = asof_date
    suffix = source.suffix.lower() or ".bin"
    destination = root / family_part / date_part / role_part / digest[:2] / f"{digest}{suffix}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if file_sha256(destination) != digest or destination.read_bytes() != payload:
            raise FileExistsError("content-addressed archive path already holds different bytes")
    else:
        try:
            with destination.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            if file_sha256(destination) != digest or destination.read_bytes() != payload:
                raise FileExistsError(
                    "concurrent archive writer created different bytes"
                ) from None
    if file_sha256(destination) != digest:
        raise ValueError("archived evidence bytes failed post-write verification")
    try:
        destination.chmod(0o444)
    except OSError:
        # Read-only mode is defense in depth. Create-only naming plus rehashing
        # remains the governing cross-platform control.
        pass
    return {
        "role": role,
        "original_path": str(source),
        "archive_path": str(destination),
        "content_uri": f"sha256:{digest}",
        "sha256": digest,
        "bytes": len(payload),
        "create_only_archive_pass": True,
    }


def archive_source_set(
    source_paths: Mapping[str, Path],
    *,
    expected_sha256: Mapping[str, str],
    archive_root: Path,
    family: str,
    asof_date: str,
) -> tuple[dict[str, Path], dict[str, dict[str, Any]]]:
    if set(source_paths) != set(expected_sha256):
        raise ValueError("source/archive expected-hash role census differs")
    audit = {
        role: archive_file_once(
            path,
            expected_sha256=expected_sha256[role],
            archive_root=archive_root,
            family=family,
            asof_date=asof_date,
            role=role,
        )
        for role, path in sorted(source_paths.items())
    }
    return (
        {role: Path(item["archive_path"]) for role, item in audit.items()},
        audit,
    )


def require_content_addressed_archive(
    path: Path,
    *,
    expected_sha256: str,
) -> dict[str, Any]:
    resolved = Path(path).expanduser().resolve()
    expected = exact_sha256(expected_sha256, label="archived evidence sha256")
    if not resolved.is_file() or resolved.stem != expected:
        raise ValueError("evidence input is not an exact content-addressed archive path")
    actual = file_sha256(resolved)
    if actual != expected:
        raise ValueError("content-addressed evidence bytes changed")
    return {
        "archive_path": str(resolved),
        "content_uri": f"sha256:{expected}",
        "sha256": expected,
        "bytes": resolved.stat().st_size,
        "content_addressed_archive_pass": True,
    }


__all__ = [
    "archive_file_once",
    "archive_source_set",
    "require_content_addressed_archive",
]
