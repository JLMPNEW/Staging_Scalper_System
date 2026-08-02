from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from industrials.core.config import resolve_path
from industrials.core.oos_research import artifact_sha256


def load_prebuild_contract(
    config_path: Path,
    family: Mapping[str, Any],
) -> dict[str, Any]:
    scoring = family["scoring"]
    manifest_path = resolve_path(
        scoring["prebuild_contract_manifest"],
        base_dir=config_path.parent,
    )
    if not manifest_path.is_file():
        raise FileNotFoundError(
            f"transportation prebuild contract is required: {manifest_path}"
        )
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    issues: list[str] = []
    if payload.get("acceptance") != "PASS":
        issues.append("prebuild acceptance is not PASS")
    if payload.get("full_historical_rebuild_authorized") is not True:
        issues.append("historical rebuild is not authorized")
    if payload.get("broad_parser_rerun_authorized") is not False:
        issues.append("broad parser rerun posture is not fail-closed")
    candidates = payload.get("enabled_candidate_registry") or {}
    if not 2 <= len(candidates) <= 3:
        issues.append("enabled candidate registry must contain two or three candidates")
    for group in ("source_artifacts", "input_artifacts"):
        for artifact_id, artifact in (payload.get(group) or {}).items():
            path = Path(str(artifact.get("path") or ""))
            expected = str(artifact.get("sha256") or "")
            if not path.is_file():
                issues.append(f"{group}:{artifact_id}:missing")
            elif artifact_sha256(path) != expected:
                issues.append(f"{group}:{artifact_id}:hash_mismatch")
    if issues:
        raise ValueError("stale transportation prebuild contract: " + "; ".join(issues))
    payload["manifest_path"] = str(manifest_path)
    payload["manifest_sha256"] = artifact_sha256(manifest_path)
    return payload
