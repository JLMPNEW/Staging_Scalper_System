"""Hash-sealed authoritative local-input validation."""

from __future__ import annotations

import csv
import hashlib
from pathlib import Path
from typing import Any


def validate_authoritative_input_manifest(
    manifest_path: Path,
    *,
    repository_root: Path,
) -> dict[str, Any]:
    """Verify paths, parsed CSV counts, hashes, and authoritative inventory."""

    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise RuntimeError("PyYAML is required for authoritative-input validation.") from exc

    manifest = manifest_path.expanduser().resolve()
    root = repository_root.expanduser().resolve()
    payload = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    if payload.get("manifest_version") != "consumer_defensive_authoritative_inputs_v1":
        raise ValueError("Unknown Consumer Defensive authoritative-input manifest version.")
    if payload.get("hash_algorithm") != "sha256":
        raise ValueError("Consumer Defensive authoritative inputs must use SHA-256.")
    rows = payload.get("inputs")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Authoritative-input manifest must contain a non-empty inputs list.")

    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    verified: list[dict[str, Any]] = []
    for position, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"Authoritative-input row {position} must be a mapping.")
        input_id = str(row.get("input_id") or "").strip()
        relative = str(row.get("path") or "").replace("\\", "/").strip()
        if not input_id or input_id in seen_ids:
            raise ValueError(f"Missing or duplicate authoritative input_id at row {position}: {input_id!r}")
        if not relative or relative in seen_paths:
            raise ValueError(f"Missing or duplicate authoritative path at row {position}: {relative!r}")
        seen_ids.add(input_id)
        seen_paths.add(relative)
        path = (root / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"Authoritative input escapes repository root: {relative}") from exc
        if not path.is_file():
            raise FileNotFoundError(f"Authoritative Consumer Defensive input is missing: {path}")
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        expected_digest = str(row.get("sha256") or "").lower()
        if digest != expected_digest:
            raise ValueError(
                f"Authoritative input SHA-256 mismatch for {relative}: expected={expected_digest} actual={digest}"
            )
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            parsed = list(reader)
        if not parsed or not any(str(value).strip() for value in parsed[0]):
            raise ValueError(f"Authoritative CSV has no header: {relative}")
        record_count = sum(1 for values in parsed[1:] if any(str(value).strip() for value in values))
        expected_count = int(row.get("record_count", -1))
        if record_count != expected_count:
            raise ValueError(
                f"Authoritative input record-count mismatch for {relative}: "
                f"expected={expected_count} actual={record_count}"
            )
        if str(row.get("review_status") or "") != "reviewed":
            raise ValueError(f"Authoritative input is not reviewed: {relative}")
        if not str(row.get("schema_version") or "").strip():
            raise ValueError(f"Authoritative input has no schema version: {relative}")
        verified.append(
            {
                "input_id": input_id,
                "path": relative,
                "record_count": record_count,
                "sha256": digest,
            }
        )

    inventory = {
        path.resolve().relative_to(root).as_posix()
        for directory in (root / "consumer_defensive" / "data", root / "consumer_defensive" / "system_csvs")
        for path in directory.glob("*.csv")
    }
    current_universe = root / "ticker_mapping" / "consumer_defensive.csv"
    if current_universe.is_file():
        inventory.add(current_universe.resolve().relative_to(root).as_posix())
    if inventory != seen_paths:
        raise ValueError(
            "Authoritative Consumer Defensive CSV inventory differs from the manifest: "
            f"unlisted={sorted(inventory - seen_paths)} missing={sorted(seen_paths - inventory)}"
        )

    manifest_digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    return {
        "manifest_path": str(manifest),
        "manifest_sha256": manifest_digest,
        "verified_inputs": len(verified),
        "inputs": verified,
    }


__all__ = ["validate_authoritative_input_manifest"]
