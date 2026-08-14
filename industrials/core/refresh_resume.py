"""Fail-closed resume planning for industrial sector refresh runners."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ResumePlan:
    start_step: str
    source_run_id: str
    completed_prefix_steps: tuple[str, ...]


def load_resume_plan(
    manifest_path: Path,
    *,
    asof: str,
    current_step_ids: list[str],
) -> ResumePlan:
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Resume manifest is missing: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Resume manifest is unreadable: {manifest_path}") from exc
    if manifest.get("acceptance") != "FAIL" or manifest.get("dry_run"):
        raise ValueError("Resume requires a real failed manifest")
    if str(manifest.get("asof_date") or "") != asof:
        raise ValueError(
            "Resume manifest as-of mismatch: "
            f"expected={asof} actual={manifest.get('asof_date')}"
        )
    rows = manifest.get("steps")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Resume manifest has no executed step rows")
    executed_ids = [str(row.get("step_id") or "") for row in rows]
    expected_prefix = current_step_ids[: len(executed_ids)]
    if executed_ids != expected_prefix:
        raise ValueError(
            "Resume manifest step prefix differs from the current pipeline; "
            "start a new full run after reviewing the pipeline change"
        )
    statuses = [str(row.get("status") or "").upper() for row in rows]
    failed = [index for index, status in enumerate(statuses) if status != "PASS"]
    if len(failed) != 1 or failed[0] != len(rows) - 1 or statuses[-1] != "FAIL":
        raise ValueError(
            "Resume manifest must contain a PASS prefix followed by one terminal FAIL"
        )
    start_step = executed_ids[-1]
    if start_step not in current_step_ids:
        raise ValueError(f"Failed resume step is no longer available: {start_step}")
    return ResumePlan(
        start_step=start_step,
        source_run_id=str(manifest.get("run_id") or ""),
        completed_prefix_steps=tuple(executed_ids[:-1]),
    )
