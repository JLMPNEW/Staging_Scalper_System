"""Readiness gate over the SEALED Stage 1 run.

Stage 2 must never re-run or deeply inspect sector pipelines. It gates only on the Stage 1 artifacts:
the contract CSV, its sealed manifest, the manifest's hard-gate verdict and integrity hash, and the
source staleness recorded in the manifest.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from portfolio_layer.core.contracts import sha256_file


def latest_run_with(runs_root: Path, filename: str) -> str | None:
    if not runs_root.exists():
        return None
    dates = sorted(p.name for p in runs_root.iterdir() if p.is_dir() and (p / filename).exists())
    return dates[-1] if dates else None


def _staleness(run_as_of: str, source_asof: str) -> int | None:
    try:
        return (date.fromisoformat(run_as_of) - date.fromisoformat(source_asof)).days
    except (ValueError, TypeError):
        return None


def check_stage1_readiness(
    runs_root: Path,
    run_as_of: str,
    *,
    staleness_tolerance: int,
    expected_pipelines: list[str] | None = None,
    stale_status: str = "FAIL",
) -> list[dict[str, str]]:
    """Return a list of {check,status,detail}. status in {PASS, WARN, FAIL}."""
    run_dir = runs_root / run_as_of
    scores_path = run_dir / "stocks_scores.csv"
    manifest_path = run_dir / "manifest.json"
    checks: list[dict[str, str]] = []

    def rec(name: str, status: str, detail: str) -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    if not scores_path.exists():
        rec("stocks_scores_present", "FAIL", f"missing {scores_path}")
        return checks
    rec("stocks_scores_present", "PASS", str(scores_path))

    if not manifest_path.exists():
        rec("manifest_present", "FAIL", f"missing {manifest_path}")
        return checks
    try:
        manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        rec("manifest_present", "FAIL", f"unreadable manifest: {exc}")
        return checks
    rec("manifest_present", "PASS", str(manifest_path))

    hard = str(manifest.get("hard_gate_acceptance", ""))
    rec("stage1_hard_gates_passed", "PASS" if hard == "PASS" else "FAIL", f"hard_gate_acceptance={hard or 'missing'}")

    recorded = str(((manifest.get("files") or {}).get("stocks_scores.csv") or {}).get("sha256", ""))
    actual = sha256_file(scores_path)
    rec("stocks_scores_hash_matches_manifest", "PASS" if recorded and recorded == actual else "FAIL",
        "sha256 matches sealed manifest" if recorded == actual else f"manifest={recorded[:12]} actual={actual[:12]}")

    per_sector = manifest.get("per_sector") or {}
    if expected_pipelines:
        present = {str(pipe) for pipe in per_sector}
        missing = sorted(set(expected_pipelines) - present)
        rec("required_sector_outputs_present", "PASS" if not missing else "FAIL",
            "all enabled sectors present in sealed manifest" if not missing else f"missing sectors: {missing}")
    stale = {}
    for pipe, info in per_sector.items():
        d = _staleness(run_as_of, str(info.get("source_asof", "")))
        if d is None or d > staleness_tolerance or d < 0:
            stale[pipe] = d
    status = "PASS" if not stale else stale_status.upper()
    rec(
        "source_staleness_within_tolerance",
        status,
        f"tolerance={staleness_tolerance}d; out-of-tolerance={stale}"
        if stale
        else f"all within {staleness_tolerance}d",
    )
    return checks


def readiness_passed(checks: list[dict[str, str]]) -> bool:
    return all(c["status"] != "FAIL" for c in checks)
