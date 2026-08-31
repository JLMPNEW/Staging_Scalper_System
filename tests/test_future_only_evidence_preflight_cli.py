from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_canonical_preflight_cli_exposes_help() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "future_only_evidence.preflight_cli", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "usage:" in completed.stdout.casefold()
    assert "transportation" in completed.stdout
    assert "consumer_defensive" not in completed.stdout


def test_canonical_preflight_materializes_only_clock_not_started(tmp_path: Path) -> None:
    output = tmp_path / "transportation.json"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "future_only_evidence.preflight_cli",
            "--family",
            "transportation",
            "--asof",
            "2026-08-26",
            "--score",
            "missing-pre-effective-score.csv",
            "--rank",
            "missing-pre-effective-rank.csv",
            "--output",
            str(output),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["status"] == "clock_not_started"
    assert payload["clock_started"] is False
    assert payload["ready_for_capture"] is False
    assert payload["calendar_date_guarantee"] is False
    assert payload["remaining_nonoverlapping_observations_per_sleeve"] == {"21": 12, "63": 4}
    assert payload["current_diagnostic_artifacts_counted"] == 0
    assert payload["production_activation_authorized"] is False
    assert payload["portfolio_write_enabled"] is False
    assert payload["optimizer_cap"] == 0.0
    assert payload["blockers"]


def test_preflight_output_is_create_only(tmp_path: Path) -> None:
    output = tmp_path / "transportation.json"
    command = [
        sys.executable,
        "-m",
        "future_only_evidence.preflight_cli",
        "--family",
        "transportation",
        "--asof",
        "2026-08-26",
        "--score",
        "missing-pre-effective-score.csv",
        "--rank",
        "missing-pre-effective-rank.csv",
        "--output",
        str(output),
    ]
    first = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=30, check=False)
    assert first.returncode == 0, first.stderr
    original = output.read_bytes()
    second = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=30, check=False)
    assert second.returncode != 0
    assert "immutable artifact already exists" in second.stderr
    assert output.read_bytes() == original
