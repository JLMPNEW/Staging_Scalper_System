"""Authoritative-input fingerprint validation."""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
import hashlib
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml


class ManifestError(ValueError):
    """Raised when an authoritative input differs from its reviewed manifest."""


@dataclass(frozen=True)
class ManifestValidation:
    manifest_version: str
    source_id: str
    path: Path
    sha256: str
    byte_size: int
    row_count: int
    unique_key: str
    unique_count: int
    header: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["path"] = str(self.path)
        payload["header"] = list(self.header)
        return payload


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ManifestError(f"{context} must be a mapping")
    return value


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], context: str) -> None:
    actual = set(value)
    missing = expected - actual
    extra = actual - expected
    if missing or extra:
        raise ManifestError(
            f"Invalid keys for {context}; missing={sorted(missing)}, unexpected={sorted(extra)}"
        )


def _require_int(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ManifestError(f"{context} must be a non-negative integer")
    return value


def validate_authoritative_input(
    manifest_path: str | Path,
    expected_universe_path: str | Path | None = None,
) -> ManifestValidation:
    """Fail unless the current CSV is byte- and structure-identical to its manifest."""

    manifest = Path(manifest_path).resolve()
    if not manifest.is_file():
        raise ManifestError(f"Authoritative input manifest not found: {manifest}")

    raw = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    root = _require_mapping(raw, "manifest")
    _require_exact_keys(root, {"manifest_version", "generated_at_utc", "files"}, "manifest")
    manifest_version = str(root["manifest_version"])
    if manifest_version != "basic_materials_authoritative_input_v1":
        raise ManifestError(f"Unsupported manifest_version: {manifest_version}")

    files = root["files"]
    if not isinstance(files, list) or len(files) != 1:
        raise ManifestError("The Basic Materials manifest must contain exactly one authoritative file")
    entry = _require_mapping(files[0], "manifest.files[0]")
    _require_exact_keys(
        entry,
        {
            "source_id",
            "path",
            "sha256",
            "byte_size",
            "row_count",
            "unique_key",
            "unique_count",
            "header",
        },
        "manifest.files[0]",
    )

    source_id = str(entry["source_id"])
    if source_id != "basic_materials_current_universe":
        raise ManifestError("Unexpected authoritative source_id")
    source_path = (manifest.parent / str(entry["path"])).resolve()
    if expected_universe_path is not None and source_path != Path(expected_universe_path).resolve():
        raise ManifestError(
            f"Manifest resolves to {source_path}, not configured universe {Path(expected_universe_path).resolve()}"
        )
    if not source_path.is_file():
        raise ManifestError(f"Authoritative universe file not found: {source_path}")

    expected_hash = str(entry["sha256"]).lower()
    expected_bytes = _require_int(entry["byte_size"], "byte_size")
    expected_rows = _require_int(entry["row_count"], "row_count")
    expected_unique = _require_int(entry["unique_count"], "unique_count")
    unique_key = str(entry["unique_key"])
    expected_header_raw = entry["header"]
    if not isinstance(expected_header_raw, Sequence) or isinstance(expected_header_raw, (str, bytes)):
        raise ManifestError("header must be a list of column names")
    expected_header = tuple(str(value) for value in expected_header_raw)

    actual_hash = file_sha256(source_path)
    actual_bytes = source_path.stat().st_size
    if actual_hash != expected_hash:
        raise ManifestError(f"SHA-256 mismatch for {source_path}: expected {expected_hash}, got {actual_hash}")
    if actual_bytes != expected_bytes:
        raise ManifestError(f"Byte-size mismatch for {source_path}: expected {expected_bytes}, got {actual_bytes}")

    with source_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        actual_header = tuple(reader.fieldnames or ())
        rows = list(reader)
    if actual_header != expected_header:
        raise ManifestError(f"Header mismatch: expected {expected_header}, got {actual_header}")
    if len(rows) != expected_rows:
        raise ManifestError(f"Row-count mismatch: expected {expected_rows}, got {len(rows)}")
    if unique_key not in actual_header:
        raise ManifestError(f"Unique key {unique_key!r} is absent from the CSV")

    values = [str(row.get(unique_key, "")).strip() for row in rows]
    if any(not value for value in values):
        raise ManifestError(f"Blank {unique_key!r} values are not allowed")
    actual_unique = len(set(values))
    if actual_unique != expected_unique or actual_unique != len(rows):
        raise ManifestError(
            f"Unique-count mismatch for {unique_key}: expected {expected_unique}, got {actual_unique}"
        )

    return ManifestValidation(
        manifest_version=manifest_version,
        source_id=source_id,
        path=source_path,
        sha256=actual_hash,
        byte_size=actual_bytes,
        row_count=len(rows),
        unique_key=unique_key,
        unique_count=actual_unique,
        header=actual_header,
    )

