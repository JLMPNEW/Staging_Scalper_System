from __future__ import annotations

import json
from pathlib import Path

import pytest

from industrials.core.refresh_resume import load_resume_plan


def _write_manifest(path: Path, *, asof: str, statuses: list[str]) -> None:
    steps = [
        {"step_id": step_id, "status": status}
        for step_id, status in zip(("one", "two", "three"), statuses, strict=True)
    ]
    path.write_text(
        json.dumps(
            {
                "acceptance": "FAIL",
                "dry_run": False,
                "asof_date": asof,
                "run_id": "failed-run",
                "steps": steps,
            }
        ),
        encoding="utf-8",
    )


def test_resume_starts_at_terminal_failed_step(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    _write_manifest(manifest, asof="2026-08-11", statuses=["PASS", "PASS", "FAIL"])
    plan = load_resume_plan(
        manifest,
        asof="2026-08-11",
        current_step_ids=["one", "two", "three", "four"],
    )
    assert plan.start_step == "three"
    assert plan.source_run_id == "failed-run"
    assert plan.completed_prefix_steps == ("one", "two")


def test_resume_rejects_pipeline_drift_and_nonterminal_failure(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    _write_manifest(manifest, asof="2026-08-11", statuses=["PASS", "FAIL", "PASS"])
    with pytest.raises(ValueError, match="terminal FAIL"):
        load_resume_plan(
            manifest,
            asof="2026-08-11",
            current_step_ids=["one", "two", "three"],
        )

    _write_manifest(manifest, asof="2026-08-11", statuses=["PASS", "PASS", "FAIL"])
    with pytest.raises(ValueError, match="step prefix differs"):
        load_resume_plan(
            manifest,
            asof="2026-08-11",
            current_step_ids=["one", "replacement", "three"],
        )
