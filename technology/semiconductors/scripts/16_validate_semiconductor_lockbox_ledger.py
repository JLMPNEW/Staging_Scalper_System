#!/usr/bin/env python3
"""Validate semiconductor lockbox artifacts and snapshot hash chaining."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import sys
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from technology.core.logging_utils import configure_utc_logging  # noqa: E402


LOGGER = logging.getLogger("semiconductor_lockbox_validator")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
CONFIG_KEY = "semiconductor_governance_reports"
CHAIN_FIELDS = ("previous_snapshot_path", "previous_snapshot_sha256", "ledger_content_sha256")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate semiconductor lockbox governance reports.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def sha256_file(path: Path | None) -> str:
    if path is None or not path.exists() or not path.is_file():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def content_sha256(payload: dict[str, Any]) -> str:
    content = {key: value for key, value in payload.items() if key not in CHAIN_FIELDS}
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def artifact_path(raw: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else PROJECT_ROOT / path


def validate_chain(snapshot_dir: Path) -> list[str]:
    errors: list[str] = []
    previous: Path | None = None
    for snapshot in sorted(snapshot_dir.glob("semiconductor_lockbox_ledger_*.json")):
        payload = read_json(snapshot)
        if not payload:
            errors.append(f"Unreadable lockbox snapshot: {snapshot}")
            previous = snapshot
            continue
        if "ledger_content_sha256" not in payload:
            LOGGER.warning("Legacy unchained semiconductor snapshot: %s", snapshot.name)
            previous = snapshot
            continue
        if payload.get("ledger_content_sha256") != content_sha256(payload):
            errors.append(f"Snapshot content hash mismatch: {snapshot.name}")
        if str(payload.get("previous_snapshot_sha256") or "") != sha256_file(previous):
            errors.append(f"Snapshot chain break: {snapshot.name}")
        previous = snapshot
    return errors


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else resolve_path(
        cfg_get(config, f"{CONFIG_KEY}.output_dir", "../output/technology_reports/governance"),
        base_dir=config_path.parent,
    )
    paths = {
        "registry_csv": output_dir / "semiconductor_signal_registry.csv",
        "registry_json": output_dir / "semiconductor_signal_registry.json",
        "ledger_csv": output_dir / "semiconductor_lockbox_ledger.csv",
        "ledger_json": output_dir / "semiconductor_lockbox_ledger.json",
        "manifest": output_dir / "semiconductor_governance_manifest.json",
    }
    errors = [f"Missing or empty governance artifact: {path}" for path in paths.values() if not path.exists() or path.stat().st_size == 0]
    ledger_rows = read_csv(paths["ledger_csv"])
    if not ledger_rows:
        errors.append("Semiconductor artifact ledger is empty.")
    for row in ledger_rows:
        if row.get("required_flag") == "1" and row.get("exists_flag") != "1":
            errors.append(f"Required ledger artifact is missing: {row.get('artifact_name')}")
        if row.get("exists_flag") != "1":
            continue
        expected = str(row.get("sha256") or "")
        actual = sha256_file(artifact_path(str(row.get("path") or "")))
        if not expected or actual != expected:
            errors.append(f"Ledger artifact hash mismatch: {row.get('artifact_name')}")

    ledger = read_json(paths["ledger_json"])
    if ledger:
        if ledger.get("ledger_content_sha256") != content_sha256(ledger):
            errors.append("Current semiconductor lockbox content hash mismatch.")
        if ledger.get("missing_required_artifacts"):
            errors.append(f"Lockbox reports missing required artifacts: {ledger.get('missing_required_artifacts')}")
        stage8 = ledger.get("stage8_research_decision") or {}
        walk_forward = ledger.get("walk_forward_decision") or {}
        if int(stage8.get("promotion_candidate") or 0) == 1:
            if int(stage8.get("stage8_gate_pass") or 0) != 1:
                errors.append("Final semiconductor candidate lacks a passing Stage 8 preliminary gate.")
            if int(walk_forward.get("final_promotion_eligible") or 0) != 1:
                errors.append("Final semiconductor candidate lacks walk-forward eligibility.")
    errors.extend(validate_chain(output_dir / "snapshots"))
    if errors:
        for error in errors:
            LOGGER.error(error)
        return 1
    LOGGER.info("Semiconductor lockbox validation passed: artifacts=%d output=%s", len(ledger_rows), output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
